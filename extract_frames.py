# -*- coding: utf-8 -*-
"""
extract_frames.py — 递归遍历一个视频根目录，把每个 .h265 文件按 1 帧/秒抽帧成 JPEG，
写入镜像目录结构下的"同名子文件夹"。

举例：
    SRC/1a/2a/3a/4a/6a/video1.h265
        → DST/1a/2a/3a/4a/6a/video1/frame_000001.jpg
                                    frame_000002.jpg
                                    ...

图片文件名支持通过 --name-style / --name-template / --name-digits 配置：
  legacy 规则（默认）：frame_000001.jpg
  parent 规则       ：video1_0001.jpg（parent = 视频同名子文件夹）
  custom 规则       ：--name-template '{parent}_{seq}' 之类，
                     占位符 {parent}/{seq} 会分别替换成父目录名和补零序号。

硬规则：任何叫 VLM 的目录（含子目录）整棵子树跳过。

依赖：与本 exe 同目录（或系统 PATH）里的 ffmpeg.exe。
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

# 长路径 / tkinter 混斜杠 helper (v0.4.61 新接入).
# 参见 docs/windows_long_path.md; extract_frames 是 v0.4.60 前的漏网之鱼.
from winpath_util import (
    normalize_windows_path as _normalize_windows_path,
    to_long_path as _to_long_path,
    safe_stat as _safe_stat,
    safe_unlink as _safe_unlink,
    safe_exists as _safe_exists,
    safe_is_file as _safe_is_file,
    safe_mkdir as _safe_mkdir,
    safe_read_text as _safe_read_text,
    safe_write_text as _safe_write_text,
    safe_glob as _safe_glob,
    safe_os_open as _safe_os_open,
)


def _log_err(msg: str) -> None:
    """stderr 打 [ERROR] 日志. 用来替换掉那些 marker/lock 静默 except: pass 的坑."""
    try:
        sys.stderr.write("[ERROR] " + msg + "\n")
        sys.stderr.flush()
    except Exception:
        pass


# stats_db 可选; 打包时 hidden-import, 缺失时不影响主流程
try:
    import stats_db as _stats_db  # type: ignore
except Exception:  # pragma: no cover
    _stats_db = None  # type: ignore

# 抽帧任务级上下文 (main 里赋值一次, extract_one 结束时读取).
# 用全局是因为 extract_one 现有签名穿透到很多调用点, 加参数改动面太大.
_STATS_CTX: dict = {
    "task_id": None,
    "src_root": None,
    "dst_root": None,
    "fps": None,
    "quality": None,
    "naming_style": None,
    "seq_digits": None,
    "version": None,
}


# ---------------------- UTF-8 stdio（同 dedupe_pic 逻辑）---------------------

def _force_utf8_stdio() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            buf = getattr(stream, "buffer", None)
            if buf is not None:
                setattr(sys, name, io.TextIOWrapper(buf, encoding="utf-8", errors="replace"))


_force_utf8_stdio()


def _disable_windows_quickedit() -> None:
    """关闭 Windows 控制台的"快速编辑模式"，避免鼠标点击导致 stdout 阻塞。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        STD_INPUT_HANDLE = -10
        ENABLE_EXTENDED_FLAGS = 0x0080
        ENABLE_QUICK_EDIT_MODE = 0x0040
        ENABLE_INSERT_MODE = 0x0020
        ENABLE_MOUSE_INPUT = 0x0010

        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        if not handle or handle == wintypes.HANDLE(-1).value:
            return
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        new_mode = mode.value
        new_mode |= ENABLE_EXTENDED_FLAGS
        new_mode &= ~ENABLE_QUICK_EDIT_MODE
        new_mode &= ~ENABLE_MOUSE_INPUT
        new_mode &= ~ENABLE_INSERT_MODE
        kernel32.SetConsoleMode(handle, new_mode)
    except Exception:
        pass


_disable_windows_quickedit()


# ------------------------------- ffmpeg 定位 --------------------------------

def resolve_ffmpeg(user_path: str | None) -> Path | None:
    """
    查找 ffmpeg 可执行文件，顺序：
      1. --ffmpeg 参数
      2. exe 同目录
      3. PyInstaller 内嵌目录 sys._MEIPASS
      4. 系统 PATH（shutil.which）
    """
    candidates: list[Path] = []
    if user_path:
        candidates.append(Path(user_path))

    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "ffmpeg.exe")
        candidates.append(Path(sys.executable).resolve().parent / "ffmpeg")
    try:
        script_dir = Path(sys.argv[0]).resolve().parent
        candidates.append(script_dir / "ffmpeg.exe")
        candidates.append(script_dir / "ffmpeg")
    except Exception:
        pass

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "ffmpeg.exe")
        candidates.append(Path(meipass) / "ffmpeg")

    for c in candidates:
        if c.is_file():
            return c

    which = shutil.which("ffmpeg")
    if which:
        return Path(which)

    return None


# ------------------------------ 目录扫描 ------------------------------------

@dataclass
class VideoTask:
    src_path: Path       # 视频源路径
    out_dir: Path        # 抽帧输出目录（视频同名文件夹）
    rel_path: Path       # 相对于 src_root 的路径（打印用）
    marker_dir: Path     # marker/lock 存放目录（集中在 markers_root 下的镜像位置）


# ------------------------------ 命名规则 ------------------------------------
#
# 抽帧输出的图片文件名以前是硬编码的 frame_000001.jpg（6 位补零），
# 现在改成可配置：
#   --name-style legacy    → frame_{seq}.jpg（旧规则）
#   --name-style parent    → {parent}_{seq}.jpg（新规则，parent 是 out_dir.name）
#   --name-template "..."  → 完全自定义，占位符 {parent}/{seq}
#   --name-digits N        → {seq} 的补零位数（对 legacy/parent/custom 都生效）
#
# 内部会算出两样东西：
#   ffmpeg_pattern : 传给 ffmpeg -i 的输出路径，序号用 %0Nd 表示
#   glob_pattern   : 用来找当前规则下已生成的帧，形如 "prefix*.jpg"

NAME_STYLE_LEGACY = "legacy"
NAME_STYLE_PARENT = "parent"
NAME_STYLE_CUSTOM = "custom"

_ALLOWED_TEMPLATE_CHARS_EXTRA = " -_.()[]（）【】"


def _resolve_template(style: str, template: str | None) -> str:
    """按 style / template 组合返回最终模板字符串（含 {parent} / {seq} 占位符）。"""
    if template:
        return template
    if style == NAME_STYLE_PARENT:
        return "{parent}_{seq}"
    # 兜底：legacy
    return "frame_{seq}"


def _validate_template(template: str) -> None:
    """轻量校验：不允许出现路径分隔符 / ffmpeg 会误解的 % 号 / 控制字符。
    允许中文、字母、数字、下划线、空格、连字符、点、括号等常见装饰字符。"""
    if not template:
        raise ValueError("命名模板不能为空")
    if "{seq}" not in template:
        raise ValueError("命名模板必须包含 {seq} 占位符")
    bad = set("/\\%\n\r\t\0:*?\"<>|")
    for ch in template:
        if ch in bad:
            raise ValueError(f"命名模板中不允许的字符: {ch!r}")


def build_name_pattern(
    out_dir: Path,
    style: str,
    template: str | None,
    digits: int,
) -> tuple[str, str]:
    """根据规则算出 (ffmpeg_pattern, glob_pattern)。

    - ffmpeg_pattern: 传给 ffmpeg 的完整输出路径，形如
        DST/.../video1/video1_%04d.jpg
    - glob_pattern  : 供 Path.glob() 用来枚举已生成帧，形如
        "video1_*.jpg"（不含目录部分）
    """
    digits = max(1, min(8, int(digits or 4)))
    tmpl = _resolve_template(style, template)
    _validate_template(tmpl)
    parent_name = out_dir.name
    # 先替换 {parent}；{seq} 单独处理成 %0Nd / *
    resolved = tmpl.replace("{parent}", parent_name)
    ffmpeg_name = resolved.replace("{seq}", f"%0{digits}d") + ".jpg"
    glob_name = resolved.replace("{seq}", "*") + ".jpg"
    ffmpeg_pattern = str(out_dir / ffmpeg_name)
    return ffmpeg_pattern, glob_name


def collect_tasks(
    src_root: Path,
    dst_root: Path,
    extensions: set[str],
    skip_dirs: set[str],
    markers_root: Path,
) -> list[VideoTask]:
    """
    递归扫描 src_root，收集所有需要抽帧的视频任务。
    - 名字在 skip_dirs 里的目录整棵子树跳过（对比不区分大小写）
    - 只收扩展名在 extensions 内的文件
    - 输出目录用视频文件的 stem 作为子文件夹名
    """
    tasks: list[VideoTask] = []
    skip_lower = {s.lower() for s in skip_dirs}

    for dirpath, dirnames, filenames in os.walk(src_root):
        # 就地修改 dirnames 让 os.walk 不再进入被跳过的子目录
        pruned = []
        for d in list(dirnames):
            if d.lower() in skip_lower:
                pruned.append(d)
                dirnames.remove(d)
        if pruned:
            print(f"  [skip] {Path(dirpath).relative_to(src_root)} 下跳过: {pruned}", flush=True)

        for name in filenames:
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if ext not in extensions:
                continue
            src = Path(dirpath) / name
            rel_dir = Path(dirpath).relative_to(src_root)
            # 输出到 DST/<rel_dir>/<视频名不带后缀>/
            out_dir = dst_root / rel_dir / Path(name).stem
            # marker 集中放到 markers_root 下的镜像位置，跟 out_dir 结构一致
            marker_dir = markers_root / rel_dir / Path(name).stem
            tasks.append(
                VideoTask(
                    src_path=src,
                    out_dir=out_dir,
                    rel_path=rel_dir / name,
                    marker_dir=marker_dir,
                )
            )
    return tasks


# --------------------------- 单个视频抽帧 -----------------------------------

# 抽帧锁：多机共享盘下互斥，抽完删除，超过 TTL 视为对方崩溃可抢占。
_LOCK_NAME = "_extract.lock"
_HOSTNAME = socket.gethostname()


def _lock_payload() -> str:
    """锁内容：hostname|pid|开始时间戳。方便运维观察谁在抽。"""
    return f"{_HOSTNAME}|{os.getpid()}|{int(time.time())}"


def _lock_is_stale(lock_path: Path, ttl_seconds: float) -> bool:
    """锁是否已过期（对方可能崩了 / 断网）。读不到内容也视为 stale。"""
    try:
        content = _safe_read_text(lock_path, encoding="utf-8", errors="replace").strip()
    except OSError:
        return True
    parts = content.split("|")
    if len(parts) < 3:
        return True
    try:
        ts = int(parts[2])
    except Exception:
        return True
    return (time.time() - ts) > ttl_seconds


def _acquire_lock(lock_path: Path, ttl_seconds: float) -> bool:
    """
    原子抢占锁。返回 True 表示抢到，False 表示别人正在跑。
    SMB/CIFS/NFS/本地 FS 都保证 O_CREAT|O_EXCL 的原子性。
    """
    try:
        _safe_mkdir(lock_path.parent, parents=True, exist_ok=True)
    except OSError as e:
        _log_err(f"锁父目录 mkdir 失败: {lock_path.parent} -> {type(e).__name__}: {e}")
        return False
    payload = _lock_payload().encode("utf-8")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = _safe_os_open(lock_path, flags, 0o644)
    except FileExistsError:
        # 已存在：看看是不是过期锁
        if _lock_is_stale(lock_path, ttl_seconds):
            try:
                _safe_unlink(lock_path)
            except FileNotFoundError:
                pass
            except OSError as e:
                _log_err(f"清理 stale 锁失败: {lock_path} -> {type(e).__name__}: {e}")
                return False
            # 再抢一次；这次还失败就让给别人
            try:
                fd = _safe_os_open(lock_path, flags, 0o644)
            except Exception:
                return False
        else:
            return False
    except Exception as e:
        _log_err(f"抢锁 os.open 异常: {lock_path} -> {type(e).__name__}: {e}")
        return False
    try:
        os.write(fd, payload)
    finally:
        try:
            os.close(fd)
        except Exception:
            pass
    return True


def _release_lock(lock_path: Path) -> None:
    try:
        _safe_unlink(lock_path)
    except FileNotFoundError:
        pass
    except OSError as e:
        _log_err(f"释放锁 unlink 失败: {lock_path} -> {type(e).__name__}: {e}")


def extract_one(
    task: VideoTask,
    ffmpeg: Path,
    fps: float,
    quality: int,
    skip_existing: bool,
    lock_ttl: float = 900.0,
    name_style: str = NAME_STYLE_LEGACY,
    name_template: str | None = None,
    name_digits: int = 6,
) -> tuple[str, int, str]:
    """
    对一个视频执行抽帧。返回 (stage, 帧数, 说明)。stage 有三种：
      - "ok"     抽出至少一帧，写 marker 内容 'done'
      - "empty"  视频有效但抽不出帧（太短 / 没关键帧 / 头坏但 ffmpeg 判无致命错误）
                 或 ffmpeg 报可预期的"无帧可解码"错误。也写 marker（内容 'empty'），
                 下次轮询到会直接跳过，不再浪费时间重试
      - "failed" 真正的失败（ffmpeg 不存在 / 崩溃 / IO 错误等），不写 marker，下次会重试
      - "locked" 别的机器/进程正在抽这个视频（多机并发），直接跳过，不写 marker
    """
    _t_start = time.time()
    stage, n, msg = _extract_one_impl(
        task, ffmpeg, fps, quality, skip_existing,
        lock_ttl, name_style, name_template, name_digits,
    )
    _elapsed = time.time() - _t_start
    # 落库 (静默失败, 出问题不影响抽帧主流程)
    if _stats_db is not None:
        try:
            _stats_db.record_extract(
                video_path=str(task.src_path),
                output_dir=str(task.out_dir),
                rel_path=str(task.rel_path),
                frames=int(n or 0),
                fps=_STATS_CTX.get("fps"),
                quality=_STATS_CTX.get("quality"),
                naming_style=name_style,
                seq_digits=int(name_digits or 0),
                elapsed_sec=_elapsed,
                stage=stage,
                exit_code=0 if stage in ("ok", "empty", "locked") else 1,
                msg=msg,
                src_root=_STATS_CTX.get("src_root"),
                dst_root=_STATS_CTX.get("dst_root"),
                task_id=_STATS_CTX.get("task_id"),
                version=_STATS_CTX.get("version"),
            )
        except Exception:
            pass
    return stage, n, msg


def _extract_one_impl(
    task: VideoTask,
    ffmpeg: Path,
    fps: float,
    quality: int,
    skip_existing: bool,
    lock_ttl: float,
    name_style: str,
    name_template: str | None,
    name_digits: int,
) -> tuple[str, int, str]:
    """真正的抽帧主体, 从 extract_one 拆出来纯净版, 便于外层统一计时+落库."""
    out_dir = task.out_dir
    marker_dir = task.marker_dir
    try:
        _safe_mkdir(marker_dir, parents=True, exist_ok=True)
    except OSError as e:
        return "failed", 0, f"marker_dir mkdir 失败: {type(e).__name__}: {e}"
    marker = marker_dir / "_done.marker"
    lock_path = marker_dir / _LOCK_NAME
    # 计算本次运行使用的命名 pattern；out_dir 一定存在（或即将创建），name 只用到 out_dir.name
    ffmpeg_pattern, glob_pattern = build_name_pattern(
        out_dir, name_style, name_template, name_digits,
    )
    if skip_existing and _safe_is_file(marker):
        # marker 存在时按当前命名规则数帧；也兜底扫一下旧的 frame_*.jpg
        existing = _safe_glob(out_dir, glob_pattern)
        if not existing and glob_pattern != "frame_*.jpg":
            existing = _safe_glob(out_dir, "frame_*.jpg")
        # 老 marker 里可能写着 'done' 或 'empty'，都当已处理，直接跳过
        try:
            content = _safe_read_text(marker, encoding="utf-8", errors="replace").strip().lower()
        except OSError:
            content = "done"
        if content == "empty" or len(existing) == 0:
            return "empty", 0, "跳过（已完成，历史标记为 empty / 目录中无帧）"
        return "ok", len(existing), f"跳过（已完成，marker 存在，{len(existing)} 帧）"

    # 抢锁：多机共享盘下同一视频只能被一台机器抽。
    # 抢不到就直接返回 locked，交给其它进程处理。
    try:
        _safe_mkdir(out_dir, parents=True, exist_ok=True)
    except OSError as e:
        return "failed", 0, f"out_dir mkdir 失败: {type(e).__name__}: {e}"
    if not _acquire_lock(lock_path, lock_ttl):
        return "locked", 0, "跳过（其他机器/进程正在抽，锁存在且未过期）"

    # 抢到锁之后再清理半成品（此时只有本进程会碰这个目录，安全）
    if skip_existing:
        stale = _safe_glob(out_dir, glob_pattern)
        # 若切换过命名规则，把老前缀的半成品也清掉，避免混着两套图
        if glob_pattern != "frame_*.jpg":
            stale += _safe_glob(out_dir, "frame_*.jpg")
        if stale:
            for f in stale:
                try:
                    _safe_unlink(f)
                except OSError as e:
                    _log_err(f"清半成品 unlink 失败: {f} -> {type(e).__name__}: {e}")

    try:
        return _do_extract(
            task, ffmpeg, fps, quality, out_dir, marker,
            ffmpeg_pattern, glob_pattern,
        )
    finally:
        _release_lock(lock_path)


def _do_extract(
    task: VideoTask,
    ffmpeg: Path,
    fps: float,
    quality: int,
    out_dir: Path,
    marker: Path,
    ffmpeg_pattern: str,
    glob_pattern: str,
) -> tuple[str, int, str]:
    """真正跑 ffmpeg 的部分。已在锁保护内。"""
    # ffmpeg 在 Windows 上仍走 CRT fopen, 长路径要显式 \\?\; Linux/Mac 原样返回.
    src_arg = _to_long_path(str(task.src_path))
    out_pattern = _to_long_path(ffmpeg_pattern)

    # ffmpeg 命令：
    #   -hide_banner / -loglevel error：静默
    #   -y：覆盖已存在文件
    #   -vf fps=N：每秒抽 N 帧
    #   -q:v Q：JPEG 质量（2-31，越小越好；q=2 约等于视觉无损，q=5 约等于 quality 90+）
    # 用户想要 "q=90" 概念，映射到 ffmpeg 的 -q:v 3（大约 92）
    q_map = max(2, min(31, int((100 - quality) / 3)))

    cmd = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", src_arg,
        "-vf", f"fps={fps}",
        "-q:v", str(q_map),
        "-an",   # 丢弃音频
        out_pattern,
    ]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=None,
            creationflags=(0x08000000 if sys.platform == "win32" else 0),
        )
    except FileNotFoundError:
        return "failed", 0, f"ffmpeg 不存在: {ffmpeg}"
    except Exception as e:
        return "failed", 0, f"ffmpeg 调用异常: {e}"

    stderr_text = proc.stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        err = stderr_text.strip()
        # 保留末尾一段，避免刷屏
        if len(err) > 400:
            err = "..." + err[-400:]
        # 判断是不是可预期的"没帧可解码"错误——这类视频本身就没内容，
        # 应当当作 empty 记 marker 永久跳过，而不是当失败让下次再试
        if _is_no_frame_error(stderr_text):
            # 空目录也写 marker，防止 pipeline 下次轮询到又跑一遍
            try:
                _safe_write_text(marker, "empty", encoding="utf-8")
            except OSError as e:
                _log_err(f"写 empty marker 失败(rc!=0 分支): {marker} -> {type(e).__name__}: {e}")
            return "empty", 0, (
                f"视频无可解码帧（ffmpeg rc={proc.returncode}），已记 empty 标记"
                f"，说明：{err[:200]}"
            )
        return "failed", 0, f"ffmpeg 返回 {proc.returncode}: {err}"

    frames = sorted(_safe_glob(out_dir, glob_pattern))
    if not frames:
        # rc=0 但真的一帧没出来（比如极短视频 / fps 太低）——也算 empty，写 marker
        try:
            _safe_write_text(marker, "empty", encoding="utf-8")
        except OSError as e:
            _log_err(f"写 empty marker 失败(rc=0 无帧分支): {marker} -> {type(e).__name__}: {e}")
        return "empty", 0, "ffmpeg 成功退出但未产出任何帧（视频可能极短），已记 empty 标记"
    try:
        _safe_write_text(marker, "done", encoding="utf-8")
    except OSError as e:
        _log_err(f"写 done marker 失败: {marker} -> {type(e).__name__}: {e}")
    return "ok", len(frames), "OK"


_NO_FRAME_PATTERNS = (
    "no frame decoded",
    "no frames decoded",
    "does not contain any stream",
    "no video stream",
    "invalid data found when processing input",
    "output file is empty",
    "output file #0 does not contain any stream",
    "at least one output file must be specified",
)


def _is_no_frame_error(stderr_text: str) -> bool:
    """判断 ffmpeg stderr 是不是可预期的'视频没帧可抽'类错误。"""
    if not stderr_text:
        return False
    low = stderr_text.lower()
    return any(p in low for p in _NO_FRAME_PATTERNS)


# ------------------------------ CLI + main ---------------------------------

def _read_version() -> str:
    try:
        from _version import VERSION
        return VERSION
    except Exception:
        return "dev"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "递归遍历视频目录，按 fps 抽帧成 JPEG，输出到镜像目录结构下的同名子文件夹。"
            "遇到名叫 VLM 的目录整棵子树跳过。"
        )
    )
    p.add_argument("--version", action="version",
                   version=f"extract_frames {_read_version()}")
    p.add_argument("src_root", type=Path, help="视频源根目录")
    p.add_argument("dst_root", type=Path, help="抽帧输出根目录（自动创建）")
    p.add_argument(
        "--fps", type=float, default=1.0,
        help="每秒抽多少帧，默认: %(default)s",
    )
    p.add_argument(
        "--ext", default="h265",
        help="要处理的扩展名（逗号分隔）。默认: %(default)s",
    )
    p.add_argument(
        "--skip-dir", default="VLM",
        help="要跳过的目录名（逗号分隔，大小写不敏感）。默认: %(default)s",
    )
    p.add_argument(
        "--quality", type=int, default=90,
        help="JPEG 质量 1-100，越大越清晰。默认: %(default)s",
    )
    p.add_argument(
        "--no-skip-existing", action="store_true",
        help="不跳过已完成的视频，全部重抽（默认：目录里有 _done.marker 才算完成，跳过；半成品会自动清空重抽）",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="只列出会做什么，不执行 ffmpeg",
    )
    p.add_argument(
        "--ffmpeg", default=None,
        help="ffmpeg 可执行路径。默认自动查找 exe 同目录 / PATH",
    )
    p.add_argument(
        "-j", "--jobs", type=int, default=1,
        help=(
            "并发抽帧的视频数（线程池）。默认 1（串行）。"
            "推荐 4-8；机器 CPU 多且盘快可以到 16。太大反而会因磁盘竞争变慢。"
        ),
    )
    p.add_argument(
        "--lock-ttl", type=float, default=900.0,
        help=(
            "视频锁 TTL（秒），默认 900（15 分钟）。"
            "多机共享盘时，某台机器抽某视频前会原子创建 _extract.lock，"
            "锁存在超过 TTL 视为对方崩了，可抢占。"
            "值应 >= 你手上最长视频的抽帧耗时。"
        ),
    )
    p.add_argument(
        "--markers-root", type=Path, required=True,
        help=(
            "marker/lock 集中存放的根目录（推荐指向多机共享盘上的目录，"
            "例如 Z:\\pic-clear-markers）。"
            "会按视频输出的层级建镜像子目录。"
        ),
    )
    p.add_argument(
        "--name-style", default=NAME_STYLE_LEGACY,
        choices=[NAME_STYLE_LEGACY, NAME_STYLE_PARENT, NAME_STYLE_CUSTOM],
        help=(
            "图片命名规则。"
            "legacy=frame_{seq}.jpg（旧默认，兼容历史）；"
            "parent={parent}_{seq}.jpg（parent 为视频同名文件夹）；"
            "custom=使用 --name-template 指定的自定义模板。"
            "默认: %(default)s"
        ),
    )
    p.add_argument(
        "--name-template", default=None,
        help=(
            "自定义命名模板，支持占位符 {parent} 和 {seq}，"
            "示例：'{parent}_{seq}' 或 'frame_{seq}'。"
            "填了本参数即视同 --name-style custom。"
        ),
    )
    p.add_argument(
        "--name-digits", type=int, default=6,
        help="序号 {seq} 的补零位数，范围 1-8。旧版是 6，新版一般用 4。默认: %(default)s",
    )
    return p.parse_args()


def _check_license_or_die() -> None:
    """与 dedupe_pic.exe 使用同一份 license.lic。"""
    try:
        from licensing import get_fingerprint, verify_license
    except ImportError as e:
        print(f"[FATAL] 无法加载 licensing 模块: {e}", file=sys.stderr)
        sys.exit(2)

    env_lic = os.environ.get("DEDUPE_LICENSE")
    if env_lic:
        license_path = Path(env_lic).expanduser().resolve()
    elif getattr(sys, "frozen", False):
        license_path = Path(sys.executable).resolve().parent / "license.lic"
    else:
        license_path = Path.cwd() / "license.lic"

    ok, msg = verify_license(license_path)
    if ok:
        print(f"[授权] {msg}", flush=True)
        return

    fp = get_fingerprint()
    print("=" * 60)
    print("[授权] 程序未获得有效授权，无法运行。")
    print(f"[授权] 原因: {msg}")
    print(f"[授权] license 期望位置: {license_path}")
    print()
    print(f"[授权] 本机指纹: {fp}")
    print("[授权] 请将上面这行指纹发给作者，获取 license.lic，")
    print("       并放到 extract_frames.exe 同目录后重新运行。")
    print("=" * 60)
    sys.exit(3)


def _fmt_time(seconds: float) -> str:
    if seconds < 0 or seconds != seconds:
        return "?"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.1f}m"
    return f"{seconds/3600:.1f}h"


def main() -> int:
    # --fingerprint 子命令：只打印指纹后退出
    if "--fingerprint" in sys.argv:
        try:
            from licensing import get_fingerprint
            print(get_fingerprint())
        except Exception as e:
            print(f"[ERROR] 无法计算指纹: {e}", file=sys.stderr)
            return 2
        return 0

    _check_license_or_die()

    args = parse_args()

    if not args.src_root.is_dir():
        print(f"[ERROR] 视频源目录不存在: {args.src_root}", file=sys.stderr)
        return 2

    ffmpeg = resolve_ffmpeg(args.ffmpeg)
    if ffmpeg is None:
        print(
            "[FATAL] 找不到 ffmpeg 可执行文件。\n"
            "        请把 ffmpeg.exe 与本 exe 放到同一目录，或用 --ffmpeg PATH 指定。",
            file=sys.stderr,
        )
        return 2

    extensions = {
        e.strip().lower().lstrip(".")
        for e in args.ext.split(",") if e.strip()
    }
    skip_dirs = {
        d.strip() for d in args.skip_dir.split(",") if d.strip()
    }

    # 命名规则参数：template 一填即视为 custom；先做一次静态校验（不带 {parent} 实值）
    name_style = args.name_style
    name_template = args.name_template
    if name_template:
        name_style = NAME_STYLE_CUSTOM
    name_digits = max(1, min(8, int(args.name_digits)))
    try:
        _validate_template(_resolve_template(name_style, name_template))
    except ValueError as e:
        print(f"[FATAL] 命名模板非法: {e}", file=sys.stderr)
        return 2

    print("=" * 60)
    print(f"  视频源目录: {args.src_root}")
    print(f"  输出根目录: {args.dst_root}")

    # 填 stats_db 任务上下文 (一次任务级, 每条视频记录复用)
    _STATS_CTX["task_id"] = os.environ.get("PICCLEAR_TASK_ID") or None
    _STATS_CTX["src_root"] = str(args.src_root)
    _STATS_CTX["dst_root"] = str(args.dst_root)
    _STATS_CTX["fps"] = float(args.fps)
    _STATS_CTX["quality"] = int(args.quality)
    _STATS_CTX["naming_style"] = name_style
    _STATS_CTX["seq_digits"] = int(name_digits)
    _STATS_CTX["version"] = _read_version()
    if _stats_db is not None:
        try:
            _stats_db.open_stats_db()
        except Exception:
            pass
    print(f"  扩展名过滤: {extensions}")
    print(f"  跳过目录名: {skip_dirs}")
    print(f"  抽帧频率  : {args.fps} fps")
    print(f"  JPEG 质量 : {args.quality}")
    print(f"  ffmpeg    : {ffmpeg}")
    print(f"  已抽跳过  : {'否' if args.no_skip_existing else '是'}")
    print(f"  dry-run   : {'是' if args.dry_run else '否'}")
    print(f"  并发数    : {args.jobs}")
    print(f"  锁 TTL    : {int(args.lock_ttl)}s")
    print(f"  markers   : {args.markers_root}")
    print(f"  命名规则  : {name_style}  模板={_resolve_template(name_style, name_template)!r}  位数={name_digits}")
    print(f"  hostname  : {_HOSTNAME}")
    print("=" * 60)

    print("[扫描] 正在收集视频文件...", flush=True)
    t0 = time.time()
    _safe_mkdir(args.markers_root, parents=True, exist_ok=True)
    tasks = collect_tasks(
        args.src_root, args.dst_root, extensions, skip_dirs, args.markers_root,
    )
    print(
        f"[扫描] 找到 {len(tasks)} 个待处理视频，耗时 {time.time()-t0:.1f}s",
        flush=True,
    )

    if not tasks:
        print("[结束] 没有可处理的视频。")
        return 0

    if args.dry_run:
        print("\n[dry-run] 将会做的事：")
        for i, t in enumerate(tasks, 1):
            _, gp = build_name_pattern(t.out_dir, name_style, name_template, name_digits)
            print(f"  {i:4d}. {t.src_path}")
            print(f"        → {t.out_dir}{os.sep}{gp}")
        print(f"\n[dry-run] 共 {len(tasks)} 个视频，未真正执行。")
        return 0

    ok_cnt = 0
    empty_cnt = 0
    fail_cnt = 0
    locked_cnt = 0
    total_frames = 0
    fails: list[tuple[VideoTask, str]] = []
    empties: list[tuple[VideoTask, str]] = []
    t_start = time.time()
    print_lock = threading.Lock()
    counter_lock = threading.Lock()
    done_count = {"n": 0}
    interrupted = {"v": False}

    def _run_one(task: VideoTask) -> tuple[VideoTask, str, int, str, float]:
        t0 = time.time()
        try:
            stage, n, msg = extract_one(
                task, ffmpeg, args.fps, args.quality,
                skip_existing=not args.no_skip_existing,
                lock_ttl=args.lock_ttl,
                name_style=name_style,
                name_template=name_template,
                name_digits=name_digits,
            )
        except Exception as e:
            return task, "failed", 0, f"内部异常: {type(e).__name__}: {e}", time.time() - t0
        return task, stage, n, msg, time.time() - t0

    def _handle_result(task: VideoTask, stage: str, n: int, msg: str, dt: float) -> None:
        nonlocal ok_cnt, empty_cnt, fail_cnt, locked_cnt, total_frames
        with counter_lock:
            done_count["n"] += 1
            idx = done_count["n"]
            if stage == "ok":
                ok_cnt += 1
                total_frames += n
            elif stage == "empty":
                empty_cnt += 1
                empties.append((task, msg))
            elif stage == "locked":
                locked_cnt += 1
            else:
                fail_cnt += 1
                fails.append((task, msg))

        elapsed = time.time() - t_start
        rate = idx / elapsed if elapsed > 0 else 0
        remain = (len(tasks) - idx) / rate if rate > 0 else float("nan")
        tag = {"ok": "✓", "empty": "⊘", "locked": "◇", "failed": "✗"}.get(stage, "?")
        with print_lock:
            print(
                f"[{idx}/{len(tasks)}] {tag} {task.rel_path}  "
                f"帧={n} 耗时={dt:.1f}s  "
                f"(已用 {_fmt_time(elapsed)}, 剩余 ~{_fmt_time(remain)})  {msg}",
                flush=True,
            )

    jobs = max(1, int(args.jobs))
    if jobs == 1:
        # 单线程分支：保持老日志格式，方便对比
        for i, task in enumerate(tasks, 1):
            if interrupted["v"]:
                break
            elapsed = time.time() - t_start
            rate = (i - 1) / elapsed if elapsed > 0 else 0
            remain = (len(tasks) - i + 1) / rate if rate > 0 else float("nan")
            print(
                f"\n[{i}/{len(tasks)}] {task.rel_path}   "
                f"(已用 {_fmt_time(elapsed)}, 剩余 ~{_fmt_time(remain)})",
                flush=True,
            )
            print(f"    → {task.out_dir}", flush=True)
            print("    ...抽帧中，请稍候（ffmpeg 静默运行，视频越长等得越久）", flush=True)
            try:
                stage, n, msg = extract_one(
                    task, ffmpeg, args.fps, args.quality,
                    skip_existing=not args.no_skip_existing,
                    lock_ttl=args.lock_ttl,
                    name_style=name_style,
                    name_template=name_template,
                    name_digits=name_digits,
                )
            except KeyboardInterrupt:
                interrupted["v"] = True
                print("\n[中断] 收到 Ctrl+C，本视频锁已释放，后续跳过。", flush=True)
                break
            if stage == "ok":
                ok_cnt += 1
                total_frames += n
                print(f"    ✓ {msg}，帧数 {n}", flush=True)
            elif stage == "empty":
                empty_cnt += 1
                empties.append((task, msg))
                print(f"    ⊘ 跳过（无帧）: {msg}", flush=True)
            elif stage == "locked":
                locked_cnt += 1
                print(f"    ◇ 跳过（其他机器/进程正在抽）: {msg}", flush=True)
            else:
                fail_cnt += 1
                fails.append((task, msg))
                print(f"    ✗ 失败: {msg}", flush=True)
    else:
        # 并发分支：完成一个打一行
        print(f"\n[并发] 启动线程池 workers={jobs}，视频粒度并发抽帧", flush=True)
        with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="extract") as ex:
            futures = {ex.submit(_run_one, t): t for t in tasks}
            try:
                for fut in as_completed(futures):
                    task, stage, n, msg, dt = fut.result()
                    _handle_result(task, stage, n, msg, dt)
            except KeyboardInterrupt:
                interrupted["v"] = True
                with print_lock:
                    print("\n[中断] 收到 Ctrl+C，取消未开始的任务，等待正在跑的收尾...",
                          flush=True)
                for f in futures:
                    f.cancel()

    print()
    print("=" * 60)
    print(
        f"[完成] 成功 {ok_cnt} / 空视频 {empty_cnt} / 其他机器占用 {locked_cnt} / 失败 {fail_cnt}，"
        f"共生成 {total_frames} 帧，总耗时 {_fmt_time(time.time()-t_start)}"
    )
    if empties:
        print(f"\n[空视频清单]（已记 empty 标记，下次运行会自动跳过）")
        for task, msg in empties[:30]:
            print(f"  - {task.src_path}")
            print(f"    {msg}")
        if len(empties) > 30:
            print(f"  ... 另外 {len(empties)-30} 条省略")
    if fails:
        print("\n[失败列表]")
        for task, msg in fails[:30]:
            print(f"  - {task.src_path}")
            print(f"    {msg}")
        if len(fails) > 30:
            print(f"  ... 另外 {len(fails)-30} 条省略")
    # 只有真失败才非 0；empty 不影响退出码
    return 0 if fail_cnt == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
