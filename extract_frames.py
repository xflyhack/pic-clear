# -*- coding: utf-8 -*-
"""
extract_frames.py — 递归遍历一个视频根目录，把每个 .h265 文件按 1 帧/秒抽帧成 JPEG，
写入镜像目录结构下的"同名子文件夹"。

举例：
    SRC/1a/2a/3a/4a/6a/video1.h265
        → DST/1a/2a/3a/4a/6a/video1/frame_000001.jpg
                                    frame_000002.jpg
                                    ...

硬规则：任何叫 VLM 的目录（含子目录）整棵子树跳过。

依赖：与本 exe 同目录（或系统 PATH）里的 ffmpeg.exe。
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


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


def collect_tasks(
    src_root: Path,
    dst_root: Path,
    extensions: set[str],
    skip_dirs: set[str],
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
            tasks.append(
                VideoTask(
                    src_path=src,
                    out_dir=out_dir,
                    rel_path=rel_dir / name,
                )
            )
    return tasks


# --------------------------- 单个视频抽帧 -----------------------------------

def extract_one(
    task: VideoTask,
    ffmpeg: Path,
    fps: float,
    quality: int,
    skip_existing: bool,
) -> tuple[bool, int, str]:
    """
    对一个视频执行抽帧。返回 (是否成功, 生成的帧数量, 错误/说明信息)。
    """
    out_dir = task.out_dir
    marker = out_dir / "_done.marker"
    if skip_existing and marker.is_file():
        existing = list(out_dir.glob("frame_*.jpg"))
        return True, len(existing), f"跳过（已完成，marker 存在，{len(existing)} 帧）"
    if skip_existing and out_dir.is_dir():
        # 存在半成品目录（有 frame_ 文件但没 marker）→ 判为上次未完成，清空重抽
        stale = list(out_dir.glob("frame_*.jpg"))
        if stale:
            for f in stale:
                try:
                    f.unlink()
                except Exception:
                    pass

    out_dir.mkdir(parents=True, exist_ok=True)
    out_pattern = str(out_dir / "frame_%06d.jpg")

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
        "-i", str(task.src_path),
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
        return False, 0, f"ffmpeg 不存在: {ffmpeg}"
    except Exception as e:
        return False, 0, f"ffmpeg 调用异常: {e}"

    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        # 保留末尾一段，避免刷屏
        if len(err) > 400:
            err = "..." + err[-400:]
        return False, 0, f"ffmpeg 返回 {proc.returncode}: {err}"

    frames = sorted(out_dir.glob("frame_*.jpg"))
    try:
        marker.write_text("done", encoding="utf-8")
    except Exception:
        pass
    return True, len(frames), "OK"


# ------------------------------ CLI + main ---------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "递归遍历视频目录，按 fps 抽帧成 JPEG，输出到镜像目录结构下的同名子文件夹。"
            "遇到名叫 VLM 的目录整棵子树跳过。"
        )
    )
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

    print("=" * 60)
    print(f"  视频源目录: {args.src_root}")
    print(f"  输出根目录: {args.dst_root}")
    print(f"  扩展名过滤: {extensions}")
    print(f"  跳过目录名: {skip_dirs}")
    print(f"  抽帧频率  : {args.fps} fps")
    print(f"  JPEG 质量 : {args.quality}")
    print(f"  ffmpeg    : {ffmpeg}")
    print(f"  已抽跳过  : {'否' if args.no_skip_existing else '是'}")
    print(f"  dry-run   : {'是' if args.dry_run else '否'}")
    print("=" * 60)

    print("[扫描] 正在收集视频文件...", flush=True)
    t0 = time.time()
    tasks = collect_tasks(args.src_root, args.dst_root, extensions, skip_dirs)
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
            print(f"  {i:4d}. {t.src_path}")
            print(f"        → {t.out_dir}{os.sep}frame_*.jpg")
        print(f"\n[dry-run] 共 {len(tasks)} 个视频，未真正执行。")
        return 0

    ok_cnt = 0
    fail_cnt = 0
    total_frames = 0
    fails: list[tuple[VideoTask, str]] = []
    t_start = time.time()

    for i, task in enumerate(tasks, 1):
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
        ok, n, msg = extract_one(
            task, ffmpeg, args.fps, args.quality,
            skip_existing=not args.no_skip_existing,
        )
        if ok:
            ok_cnt += 1
            total_frames += n
            print(f"    ✓ {msg}，帧数 {n}", flush=True)
        else:
            fail_cnt += 1
            fails.append((task, msg))
            print(f"    ✗ 失败: {msg}", flush=True)

    print()
    print("=" * 60)
    print(
        f"[完成] 成功 {ok_cnt} / 失败 {fail_cnt}，"
        f"共生成 {total_frames} 帧，总耗时 {_fmt_time(time.time()-t_start)}"
    )
    if fails:
        print("\n[失败列表]")
        for task, msg in fails[:30]:
            print(f"  - {task.src_path}")
            print(f"    {msg}")
        if len(fails) > 30:
            print(f"  ... 另外 {len(fails)-30} 条省略")
    return 0 if fail_cnt == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
