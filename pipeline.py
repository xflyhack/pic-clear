# -*- coding: utf-8 -*-
"""
pipeline.py —— 图片流水线编排器
==============================

把 bat 编排层升级成 exe：
- 交互 / 无交互提交任务
- 后台 detach 运行（关掉窗口继续跑）
- 集中日志、可查询状态
- 每个子目录一个 job step，串行调用 extract_frames.exe + dedupe_pic.exe

子命令：
  submit   提交新任务（前台交互 / 或 --auto）
  worker   [内部] detach 后台执行体，别手动调
  list     列出所有任务
  status   查看某个任务状态（不给 job_id 就看最近一个）
  logs     打印/tail 任务日志
  stop     优雅停止一个任务

任务目录：
  ``<OUT_ROOT>\\.pipeline\\jobs\\<job_id>\\``

      - manifest.json   任务参数
      - status.json     实时状态
      - pipeline.log    编排器日志
      - worker.log      子进程 stdout+stderr
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path


# --------------------------- stdio / console -------------------------------

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


# ------------------------------- 授权 --------------------------------------

def _check_license_or_die() -> None:
    # 开发/测试后门：只在设置了 PIPELINE_SKIP_LICENSE=1 时跳过。
    # 真正 build 出的 exe 里也可用，但没人会去堡垒机上设这个 env。
    if os.environ.get("PIPELINE_SKIP_LICENSE") == "1":
        print("[授权] PIPELINE_SKIP_LICENSE=1，跳过授权（开发模式）", flush=True)
        return
    try:
        from licensing import get_fingerprint, verify_license
    except ImportError as e:
        print(f"[FATAL] 无法加载 licensing 模块: {e}", file=sys.stderr)
        sys.exit(2)

    env_lic = os.environ.get("PIPELINE_LICENSE") or os.environ.get("DEDUPE_LICENSE")
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
    print("[授权] pipeline 未获得有效授权，无法运行。")
    print(f"[授权] 原因: {msg}")
    print(f"[授权] license 期望位置: {license_path}")
    print()
    print(f"[授权] 本机指纹: {fp}")
    print("[授权] 请把这行指纹发给作者，获取 license.lic 后放到 pipeline.exe 同目录。")
    print("=" * 60)
    sys.exit(3)


# --------------------- Windows 后台托管加固 --------------------------------

# 单实例锁的互斥体句柄，保持进程存活期间存在
_single_instance_handle = None


def _suppress_windows_error_dialogs() -> None:
    """屏蔽 Windows 崩溃对话框：worker 后台跑时不弹窗打断，
    避免有人误关或 GUI 弹框把后台进程 hang 住。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        SEM_FAILCRITICALERRORS = 0x0001
        SEM_NOGPFAULTERRORBOX = 0x0002
        SEM_NOOPENFILEERRORBOX = 0x8000
        ctypes.windll.kernel32.SetErrorMode(
            SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
        )
    except Exception:
        pass


def _acquire_single_instance_lock(name: str) -> bool:
    """尝试拿全局命名互斥体，拿到返回 True，已被占用返回 False。
    进程退出时 handle 会被自动释放。"""
    global _single_instance_handle
    if os.name != "nt":
        # 非 Windows：用文件锁兜底
        try:
            import tempfile, fcntl  # type: ignore
            lock_path = Path(tempfile.gettempdir()) / f"{name}.lock"
            fh = open(lock_path, "w")
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _single_instance_handle = fh
            return True
        except Exception:
            return False
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        ERROR_ALREADY_EXISTS = 183
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        # 用 Local\ 而不是 Global\，避免 Session 0 权限问题
        handle = kernel32.CreateMutexW(None, True, f"Local\\{name}")
        if not handle:
            return False
        err = kernel32.GetLastError()
        if err == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        _single_instance_handle = handle
        return True
    except Exception:
        return False


# ------------------------------- 默认路径 -----------------------------------

DEFAULT_DATA_DRIVE = "Z:"
DEFAULT_DATA_PREFIX = "sjbz_"
DEFAULT_OUT_SUBDIR = "切帧结果"


def default_out_root(data_drive: str) -> Path:
    return Path(f"{data_drive}\\{DEFAULT_OUT_SUBDIR}")


def default_markers_root(data_drive: str) -> Path:
    """marker 根目录默认在数据盘的 pic-clear-markers 下。"""
    return Path(f"{data_drive}\\pic-clear-markers")


def jobs_root(out_root: Path) -> Path:
    return out_root / ".pipeline" / "jobs"


# ------------------------------- exe 定位 -----------------------------------

def resolve_worker_exe(name: str) -> str:
    """在 exe 同目录 / PATH 里找 extract_frames.exe / dedupe_pic.exe。"""
    # 开发测试后门：允许通过环境变量覆盖，方便本地用 shell 脚本冒充 exe
    env_key = f"PIPELINE_{name.upper()}_EXE"
    envv = os.environ.get(env_key)
    if envv:
        return envv
    if getattr(sys, "frozen", False):
        same_dir = Path(sys.executable).resolve().parent / f"{name}.exe"
        if same_dir.is_file():
            return str(same_dir)
    found = shutil.which(f"{name}.exe") or shutil.which(name)
    if found:
        return found
    return f"{name}.exe"  # 让子进程报错


# ------------------------- worker 一致性预检 ---------------------------
#
# 规则：pipeline.exe 同目录 与 系统 PATH（比如 System32）里都可能存在
# extract_frames.exe / dedupe_pic.exe。为避免"一个走同目录、一个走 System32"
# 的版本错配（导致奇怪的 rc 或找不到参数），启动前做一次强一致性检查：
#
#   同目录 2/2 都在  →  锁定同目录，都用这份
#   同目录 0/2 都不在 →  统一走 PATH（相当于 System32）
#   同目录 1/2 缺一个 →  报错拒绝启动，让用户补齐
#
# 环境变量后门 PIPELINE_XXX_EXE 仍然最优先，但要求"要么都指定要么都不指定"。

WORKER_NAMES = ("extract_frames", "dedupe_pic")


def _pipeline_dir() -> Path | None:
    """pipeline 自身可执行文件所在目录；开发模式（未 freeze）时返回 None。"""
    if getattr(sys, "frozen", False):
        try:
            return Path(sys.executable).resolve().parent
        except Exception:
            return None
    return None


def preflight_check_workers() -> tuple[bool, dict, list[str]]:
    """在启动 submit / worker 之前检查 2 个 worker exe 的一致性。

    返回 (ok, resolved, errors)：
      - ok:        True 表示可以启动
      - resolved:  {'extract_frames': 'C:\\...', 'dedupe_pic': 'C:\\...'} 决议后的绝对路径
      - errors:    ok=False 时给出的多行中文诊断

    检查逻辑（优先级从高到低）：
      1. 若两个 worker 都由 env 变量 PIPELINE_*_EXE 指定 → 采信；
         若只指定其中一个 → 报错（不允许半指定）
      2. pipeline 自身目录里两个都有 → 用同目录
      3. pipeline 自身目录里一个都没有 → 走 PATH（找不到再报错）
      4. pipeline 自身目录里只有一个 → 报错，让用户补齐或都挪走
    """
    errors: list[str] = []
    resolved: dict[str, str] = {}

    # ---- Step 1: env override ----
    env_pairs = {}
    for name in WORKER_NAMES:
        v = os.environ.get(f"PIPELINE_{name.upper()}_EXE")
        if v:
            env_pairs[name] = v
    if env_pairs:
        if len(env_pairs) != len(WORKER_NAMES):
            missing = [n for n in WORKER_NAMES if n not in env_pairs]
            errors.append(
                "[预检] 环境变量 PIPELINE_*_EXE 只指定了部分 worker，"
                "为避免版本错配，要求'要么都指定，要么都不指定'。"
            )
            errors.append(f"       已指定: {list(env_pairs.keys())}")
            errors.append(f"       缺失: {missing}")
            return False, {}, errors
        for name, path in env_pairs.items():
            if not Path(path).is_file():
                errors.append(f"[预检] 环境变量 PIPELINE_{name.upper()}_EXE 指向的文件不存在: {path}")
        if errors:
            return False, {}, errors
        return True, {k: str(Path(v).resolve()) for k, v in env_pairs.items()}, []

    # ---- Step 2/3/4: 检查 pipeline 同目录 vs PATH ----
    pipe_dir = _pipeline_dir()
    same_dir_hits: dict[str, Path] = {}
    if pipe_dir is not None:
        for name in WORKER_NAMES:
            p = pipe_dir / f"{name}.exe"
            if p.is_file():
                same_dir_hits[name] = p

    # 情况 A：同目录都有 → 用同目录
    if pipe_dir is not None and len(same_dir_hits) == len(WORKER_NAMES):
        return True, {n: str(p.resolve()) for n, p in same_dir_hits.items()}, []

    # 情况 B：同目录只有一部分 → 报错
    if pipe_dir is not None and 0 < len(same_dir_hits) < len(WORKER_NAMES):
        missing = [n for n in WORKER_NAMES if n not in same_dir_hits]
        hit_list = "、".join(f"{n}.exe" for n in same_dir_hits)
        miss_list = "、".join(f"{n}.exe" for n in missing)
        errors.append("[预检] pipeline 同目录下 worker exe 不完整，为避免'一个走同目录、")
        errors.append("       另一个走 System32'的版本错配，拒绝启动。")
        errors.append(f"       pipeline 目录: {pipe_dir}")
        errors.append(f"       同目录已有  : {hit_list}")
        errors.append(f"       同目录缺失  : {miss_list}")
        errors.append("")
        errors.append("       解决方案二选一：")
        errors.append(f"         A. 把缺失的 {miss_list} 也复制到 {pipe_dir}")
        errors.append(f"         B. 把已有的 {hit_list} 从 {pipe_dir} 移走，")
        errors.append("            让所有 worker 统一从 System32（或 PATH）加载")
        return False, {}, errors

    # 情况 C：同目录一个都没有 → 走 PATH
    path_hits: dict[str, str] = {}
    missing_in_path: list[str] = []
    for name in WORKER_NAMES:
        p = shutil.which(f"{name}.exe") or shutil.which(name)
        if p:
            path_hits[name] = str(Path(p).resolve())
        else:
            missing_in_path.append(name)
    if missing_in_path:
        errors.append("[预检] 未在 pipeline 同目录、也未在系统 PATH 中找到全部 worker exe：")
        for name in missing_in_path:
            errors.append(f"       ✘ 找不到 {name}.exe")
        errors.append("")
        errors.append("       解决方案二选一：")
        errors.append("         A. 把 extract_frames.exe 和 dedupe_pic.exe 一起放到")
        errors.append(f"            {pipe_dir if pipe_dir else '（pipeline 同目录）'}")
        errors.append("         B. 或一起放到 C:\\Windows\\System32（管理员权限）")
        return False, {}, errors

    return True, path_hits, []


def _print_preflight_errors(errors: list[str]) -> None:
    print("=" * 64, file=sys.stderr)
    for line in errors:
        print(line, file=sys.stderr)
    print("=" * 64, file=sys.stderr)


def resolve_self_exe() -> str:
    """自身 exe 路径，供 detach 时 spawn 新的 worker 进程。

    允许通过环境变量 PIPELINE_WORKER_EXE_OVERRIDE 指定一个替代 exe（例如 pipe_gui
    调用 cmd_submit 时，希望 detach 出的 worker 是同目录/System32 里的 pipeline.exe
    而不是 pipe_gui.exe 自身副本，以免进程列表里看不出是 pipeline 的 worker）。
    """
    override = os.environ.get("PIPELINE_WORKER_EXE_OVERRIDE")
    if override and Path(override).is_file():
        return override
    if getattr(sys, "frozen", False):
        return sys.executable
    return sys.executable + " " + str(Path(__file__).resolve())


# ------------------------------- 状态 --------------------------------------

@dataclass
class SubStatus:
    name: str
    stage: str = "pending"        # pending / extracting / done / failed / skipped
    started_at: str | None = None
    ended_at: str | None = None
    extract_rc: int | None = None
    dedup_rc: int | None = None
    note: str = ""
    # marker 驱动模式下的视频级进度
    videos_extracted: int = 0     # 已抽完（有 _done.marker）
    videos_deduped: int = 0       # 已去重（有 _dedup_done.marker）


@dataclass
class JobStatus:
    job_id: str
    pid: int
    state: str = "pending"        # pending / running / done / failed / stopped
    created_at: str = ""
    started_at: str | None = None
    ended_at: str | None = None
    src_root: str = ""
    out_root: str = ""
    threshold: int = 3
    fps: float = 1.0
    ext: str = ".h265"
    apply_delete: bool = False
    current_sub_idx: int = 0
    total_subs: int = 0
    subs: list[SubStatus] = field(default_factory=list)
    last_message: str = ""


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _job_dir(out_root: Path, job_id: str) -> Path:
    return jobs_root(out_root) / job_id


def _save_status(job_dir: Path, status: JobStatus) -> None:
    p = job_dir / "status.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(status), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _load_status(job_dir: Path) -> JobStatus | None:
    p = job_dir / "status.json"
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    subs = [SubStatus(**s) for s in raw.get("subs", [])]
    raw["subs"] = subs
    return JobStatus(**raw)


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            STILL_ACTIVE = 259
            return bool(ok) and code.value == STILL_ACTIVE
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


# ------------------------------- 交互选择 -----------------------------------

def _list_available_drives() -> list[str]:
    """Windows: 列出当前系统上能访问的所有盘符（'C:', 'D:', ...）。
    非 Windows 返回空列表（走绝对路径分支）。"""
    if os.name != "nt":
        return []
    drives: list[str] = []
    try:
        import ctypes
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if bitmask & (1 << i):
                letter = chr(ord("A") + i)
                drives.append(f"{letter}:")
    except Exception:
        pass
    return drives


def pick_drive(default_drive: str) -> str:
    """交互选盘：每次都让用户选，避免固定 Z: 导致不灵活。
    default_drive 只作为"推荐默认项"（用户直接回车就选它）。"""
    drives = _list_available_drives()
    if not drives:
        # 非 Windows 或拿不到盘符：让用户手输
        prompt = f"请输入盘符或绝对路径（默认 {default_drive}）: "
        raw = input(prompt).strip()
        return raw or default_drive
    # 把默认盘放到第 1 位（如果存在），方便回车即选
    default_upper = default_drive.rstrip("\\/").upper()
    if default_upper in drives:
        drives.remove(default_upper)
        drives.insert(0, default_upper)
    print("[选择] 请选择数据盘：")
    for i, d in enumerate(drives, 1):
        tag = "  (默认)" if i == 1 else ""
        print(f"    [{i}] {d}{tag}")
    pick = input("请输入编号（默认 1）: ").strip() or "1"
    try:
        return drives[int(pick) - 1]
    except (ValueError, IndexError):
        print("[错误] 无效编号，退出。")
        sys.exit(2)


def interactive_pick(data_drive: str, data_prefix: str) -> tuple[Path, list[str], str]:
    """交互选择 数据盘 + sjbz 根目录 + 子目录列表。
    返回 (源目录, 子目录名列表, 最终选中的数据盘)。"""
    # 每次都让用户选盘（避免固定 Z: 不灵活）
    data_drive = pick_drive(data_drive)
    drive = Path(f"{data_drive}\\")
    if not drive.is_dir():
        print(f"[错误] 数据盘 {data_drive} 不存在。")
        sys.exit(2)
    print(f"[数据盘] {data_drive}")

    # 找 sjbz_*
    candidates = sorted([p for p in drive.iterdir() if p.is_dir() and p.name.startswith(data_prefix)])
    if not candidates:
        # 兜底：列出当前盘的所有一级目录让用户选，也允许手输绝对路径
        top_dirs = sorted([p for p in drive.iterdir() if p.is_dir()])
        if top_dirs:
            print(f"[提示] 在 {data_drive}\\ 下没找到 {data_prefix}* 目录，可从以下顶层目录选择：")
            for i, d in enumerate(top_dirs, 1):
                print(f"    [{i}] {d.name}")
            print("    [0] 手工输入其它路径")
            raw = input("请输入编号（默认 0）: ").strip() or "0"
            if raw == "0":
                raw = input("请输入源目录（绝对路径）: ").strip()
                if not raw:
                    print("[错误] 未输入源目录。")
                    sys.exit(2)
                src_root = Path(raw).resolve()
            else:
                try:
                    src_root = top_dirs[int(raw) - 1]
                except (ValueError, IndexError):
                    print("[错误] 无效编号，退出。")
                    sys.exit(2)
        else:
            raw = input(f"[提示] {data_drive}\\ 下什么目录都没有，请输入源目录: ").strip()
            if not raw:
                print("[错误] 未输入源目录。")
                sys.exit(2)
            src_root = Path(raw).resolve()
    elif len(candidates) == 1:
        src_root = candidates[0]
        print(f"[自动] 唯一 sjbz 目录：{src_root}")
    else:
        print(f"[选择] 找到多个 {data_prefix}* 目录：")
        for i, d in enumerate(candidates, 1):
            print(f"    [{i}] {d.name}")
        pick = input("请输入编号: ").strip()
        try:
            src_root = candidates[int(pick) - 1]
        except (ValueError, IndexError):
            print("[错误] 无效编号。")
            sys.exit(2)

    # 列子目录
    subs = sorted([p.name for p in src_root.iterdir() if p.is_dir()])
    if not subs:
        print(f"[提示] {src_root} 下没有一级子目录，将直接对整个目录处理。")
        return src_root, ["."], data_drive

    print(f"\n[子目录] {src_root} 下的一级子目录（共 {len(subs)} 个）：")
    print(f"    [0] 就选当前目录 {src_root}（不再往下钻）")
    for i, name in enumerate(subs, 1):
        print(f"    [{i}] {name}")
    print()
    print("输入方式：0 = 当前目录 / 序号列表 (1,2) / 区间 (1-3) / 全部 (all)")
    sel = input("请输入要处理的子目录: ").strip()
    if sel == "0":
        print(f"\n[已选] 当前目录 {src_root}\n")
        return src_root, ["."], data_drive
    picked = parse_selection(sel, subs)
    if not picked:
        print(f"[错误] 输入 {sel!r} 无法解析。")
        sys.exit(2)
    print(f"\n[已选] {len(picked)} 个子目录：{', '.join(picked)}\n")
    return src_root, picked, data_drive


def parse_selection(sel: str, subs: list[str]) -> list[str]:
    """解析 '1,2' / '1-3' / 'all' 为具体子目录名列表。"""
    if not sel:
        return []
    sel = sel.strip()
    if sel.lower() == "all":
        return list(subs)
    picked: list[str] = []
    for token in sel.split(","):
        token = token.strip()
        if not token:
            continue
        m = re.match(r"^(\d+)-(\d+)$", token)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            for k in range(min(a, b), max(a, b) + 1):
                if 1 <= k <= len(subs) and subs[k - 1] not in picked:
                    picked.append(subs[k - 1])
        elif token.isdigit():
            k = int(token)
            if 1 <= k <= len(subs) and subs[k - 1] not in picked:
                picked.append(subs[k - 1])
        elif token in subs and token not in picked:
            picked.append(token)
    return picked


# ------------------------------- submit ------------------------------------

def cmd_submit(args: argparse.Namespace) -> int:
    data_drive = args.data_drive or DEFAULT_DATA_DRIVE
    data_prefix = args.data_prefix or DEFAULT_DATA_PREFIX

    # 启动前一致性预检：extract_frames.exe / dedupe_pic.exe 必须"要么都在 pipeline
    # 同目录，要么都走 System32/PATH"，不允许一个走同目录、另一个走 PATH，避免版本错配
    ok, resolved, errors = preflight_check_workers()
    if not ok:
        _print_preflight_errors(errors)
        return 2

    # --- 决定 src_root / subs ---
    if args.auto:
        if not args.src:
            print("[错误] --auto 必须给 --src")
            return 2
        src_root = Path(args.src).resolve()
        if not src_root.is_dir():
            print(f"[错误] 源目录不存在: {src_root}")
            return 2
        available_subs = sorted([p.name for p in src_root.iterdir() if p.is_dir()])
        if args.subs:
            subs = parse_selection(args.subs, available_subs)
            if not subs:
                print(f"[错误] 无法解析 --subs {args.subs!r}")
                return 2
        else:
            subs = available_subs if available_subs else ["."]
    else:
        # 交互模式会让用户选盘，返回值里带出真实选中的盘
        src_root, subs, data_drive = interactive_pick(data_drive, data_prefix)

    # --- 决定 out_root ---
    # 优先 --out-root，其次交互问一下，最后回落到 data_drive\切帧结果
    if args.out_root:
        out_root = Path(args.out_root)
    elif args.auto:
        out_root = default_out_root(data_drive)
    else:
        default_out = default_out_root(data_drive)
        print(f"\n[输出] 默认输出根目录: {default_out}")
        raw = input("回车用默认，或输入自定义路径: ").strip()
        out_root = Path(raw) if raw else default_out
    # 检查输出根目录所在盘是否存在，避免走到后面 mkdir 才炸
    if os.name == "nt":
        anchor = Path(out_root).anchor  # 'D:\\' 之类
        if anchor and not Path(anchor).is_dir():
            print(f"[错误] 输出目录所在盘不存在: {anchor}", file=sys.stderr)
            return 2

    # --- 建 job_id + 目录 ---
    job_id = f"job_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    job_dir = _job_dir(out_root, job_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    # --- 决定 markers_root ---
    if getattr(args, "markers_root", None):
        markers_root = Path(args.markers_root)
    else:
        markers_root = default_markers_root(data_drive)
    markers_root.mkdir(parents=True, exist_ok=True)

    manifest = {
        "job_id": job_id,
        "created_at": _now_iso(),
        "src_root": str(src_root),
        "out_root": str(out_root),
        "markers_root": str(markers_root),
        "subs": subs,
        "threshold": args.threshold,
        "fps": args.fps,
        "ext": args.ext,
        "apply_delete": args.apply,
        "hard_delete": args.hard_delete,
        "motion_threshold": args.motion_threshold,
        "daily_remain_limit": args.daily_remain_limit,
        "scene_protect": bool(args.scene_protect),
        "watch_interval": float(args.watch_interval),
        "protect": (args.protect or "").strip() or None,
        "extract_jobs": int(getattr(args, "extract_jobs", 1) or 1),
        "extract_lock_ttl": float(getattr(args, "extract_lock_ttl", 900.0) or 900.0),
        "dedupe_jobs": int(getattr(args, "dedupe_jobs", 1) or 1),
        "dedupe_lock_ttl": float(getattr(args, "dedupe_lock_ttl", 900.0) or 900.0),
    }
    (job_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # --- 起后台 worker ---
    self_exe = resolve_self_exe()
    if getattr(sys, "frozen", False):
        cmd = [self_exe, "worker", "--job-id", job_id, "--out-root", str(out_root)]
    else:
        # 开发模式下 self_exe 是 "python xxx.py"
        parts = self_exe.split(" ", 1)
        cmd = [parts[0], parts[1], "worker", "--job-id", job_id, "--out-root", str(out_root)]

    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        flags = 0

    # submit 只把 detach 那一瞬间的 stdio 落到 bootstrap.log；
    # worker 内部会自己往 worker.log 追加，避免两个句柄互抢
    with (job_dir / "bootstrap.log").open("wb") as logf:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=subprocess.STDOUT,
            creationflags=flags if os.name == "nt" else 0,
            close_fds=True,
            cwd=str(job_dir),
        )

    # 记录初始 status.json，pid 记录 detach 出来的 worker
    status = JobStatus(
        job_id=job_id,
        pid=proc.pid,
        state="pending",
        created_at=manifest["created_at"],
        src_root=str(src_root),
        out_root=str(out_root),
        threshold=args.threshold,
        fps=args.fps,
        ext=args.ext,
        apply_delete=args.apply,
        total_subs=len(subs),
        subs=[SubStatus(name=s) for s in subs],
        last_message="已提交，后台启动中...",
    )
    _save_status(job_dir, status)

    print("=" * 60)
    print(f"[OK] 已提交任务：{job_id}")
    print(f"     后台 PID：{proc.pid}")
    print(f"     子目录数：{len(subs)}    ({', '.join(subs)})")
    print(f"     日志：{job_dir / 'pipeline.log'}")
    print(f"     状态：{job_dir / 'status.json'}")
    print()
    print(f"随时查看进度：  pipeline.exe status {job_id}")
    print(f"实时看日志：    pipeline.exe logs -f {job_id}")
    print(f"停止任务：      pipeline.exe stop {job_id}")
    print("=" * 60)
    return 0


# ------------------------------- worker ------------------------------------

_pipeline_log_lock = None


def _pipeline_log(job_dir: Path, msg: str) -> None:
    global _pipeline_log_lock
    if _pipeline_log_lock is None:
        import threading
        _pipeline_log_lock = threading.Lock()
    line = f"[{_now_iso()}] {msg}"
    with _pipeline_log_lock:
        print(line, flush=True)
        with (job_dir / "pipeline.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")


# 集中统计：每个刚去重完的目录追加一行，返回当日累计 remain
# CSV 位置：Z:\data_source\YYYYMMDD\machine_id_{COMPUTERNAME}.csv
# 每机每天一份文件，多机器天然无锁写
_IMG_EXT = {".jpg", ".jpeg", ".png"}
_stats_lock = None


def _append_stats(target_dir: Path, data_drive: str = "Z:") -> int:
    """给一个刚 dedupe 完的目录写统计，返回当日累计 remain。
    出错返回 -1（表示不参与阈值判断）。"""
    global _stats_lock
    if _stats_lock is None:
        import threading
        _stats_lock = threading.Lock()

    report = target_dir / "dedupe_report.csv"
    if not report.is_file():
        return -1

    # 数图片总数（不递归，一个视频对应一个目录）
    total = 0
    try:
        for entry in target_dir.iterdir():
            if entry.is_file() and entry.suffix.lower() in _IMG_EXT:
                total += 1
    except Exception:
        return -1

    # 数 DELETE 行（csv 表头之后每行第 2 列是 action）
    deleted = 0
    try:
        import csv as _csv
        with report.open("r", encoding="utf-8-sig", newline="") as f:
            reader = _csv.reader(f)
            header = next(reader, None)  # 跳过表头
            for row in reader:
                if len(row) >= 2 and row[1] == "DELETE":
                    deleted += 1
    except Exception:
        return -1

    remain = max(total - deleted, 0)
    today = datetime.now().strftime("%Y%m%d")
    machine = os.environ.get("COMPUTERNAME") or socket.gethostname() or "unknown"
    stats_dir = Path(f"{data_drive}\\data_source\\{today}")
    stats_csv = stats_dir / f"machine_id_{machine}.csv"

    # 加进程内锁，避免同一 worker 里多线程同时写自己那份 CSV
    with _stats_lock:
        try:
            stats_dir.mkdir(parents=True, exist_ok=True)
            new_file = not stats_csv.exists()
            with stats_csv.open("a", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                if new_file:
                    w.writerow(["folder_name", "total", "deleted", "remain",
                                "abs_path", "timestamp"])
                w.writerow([
                    target_dir.name,
                    total,
                    deleted,
                    remain,
                    str(target_dir),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ])
            # 读回累计 remain
            cum = 0
            with stats_csv.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.reader(f)
                next(reader, None)  # 表头
                for row in reader:
                    if len(row) >= 4:
                        try:
                            cum += int(row[3])
                        except ValueError:
                            pass
            return cum
        except Exception as e:
            print(f"[append_stats] 写入失败: {e}", file=sys.stderr)
            return -1


def cmd_worker(args: argparse.Namespace) -> int:
    """[内部] detach 后台执行体。"""
    out_root = Path(args.out_root).resolve()
    job_dir = _job_dir(out_root, args.job_id)
    if not job_dir.is_dir():
        print(f"[FATAL] job 目录不存在: {job_dir}", file=sys.stderr)
        return 2

    # worker 自身也做一次预检；如果失败，把错误写进 pipeline.log 和 status 便于排查
    ok, resolved_workers, errors = preflight_check_workers()
    if not ok:
        _print_preflight_errors(errors)
        for line in errors:
            _pipeline_log(job_dir, f"[预检失败] {line}")
        # 尝试把 status 标为 failed，方便 pipe_gui/pipeline status 一眼看到
        try:
            st = _load_status(job_dir)
            if st is not None:
                st.state = "failed"
                st.last_message = "预检失败：worker exe 一致性检查未通过"
                st.ended_at = _now_iso()
                _save_status(job_dir, st)
        except Exception:
            pass
        return 2

    manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
    status = _load_status(job_dir)
    if status is None:
        print(f"[FATAL] 无法读取 status.json", file=sys.stderr)
        return 2

    status.state = "running"
    status.started_at = _now_iso()
    status.pid = os.getpid()
    status.last_message = "worker 启动"
    _save_status(job_dir, status)
    _pipeline_log(job_dir, f"worker 启动，job_id={args.job_id}，PID={os.getpid()}")

    src_root = Path(manifest["src_root"])
    subs = manifest["subs"]
    threshold = manifest["threshold"]
    fps = manifest["fps"]
    ext = manifest["ext"]
    apply_delete = manifest["apply_delete"]
    # 新增字段：老 manifest 没有时用兼容默认值
    hard_delete = bool(manifest.get("hard_delete", False))
    motion_threshold = float(manifest.get("motion_threshold", 0.12))
    daily_remain_limit = int(manifest.get("daily_remain_limit", 80000))
    scene_protect = bool(manifest.get("scene_protect", False))
    watch_interval = float(manifest.get("watch_interval", 3.0))
    protect_arg = manifest.get("protect")  # 老 manifest 没有则为 None，走 dedupe_pic 默认
    if watch_interval <= 0:
        watch_interval = 3.0
    extract_jobs = max(1, int(manifest.get("extract_jobs", 1) or 1))
    extract_lock_ttl = float(
        manifest.get("extract_lock_ttl", manifest.get("lock_ttl", 900.0)) or 900.0
    )
    dedupe_jobs = max(1, int(manifest.get("dedupe_jobs", 1) or 1))
    dedupe_lock_ttl = float(manifest.get("dedupe_lock_ttl", 900.0) or 900.0)
    markers_root_str = manifest.get("markers_root")
    if not markers_root_str:
        _pipeline_log(job_dir, "[FATAL] manifest 缺 markers_root，无法继续")
        return 2
    markers_root = Path(markers_root_str)
    markers_root.mkdir(parents=True, exist_ok=True)

    # 用 preflight 返回的绝对路径，保证跟 submit 时期望的位置一致
    extract_exe = resolved_workers.get("extract_frames") or resolve_worker_exe("extract_frames")
    dedupe_exe = resolved_workers.get("dedupe_pic") or resolve_worker_exe("dedupe_pic")
    _pipeline_log(job_dir, f"[预检] extract_frames.exe = {extract_exe}")
    _pipeline_log(job_dir, f"[预检] dedupe_pic.exe     = {dedupe_exe}")

    reports_dir = job_dir / "reports"
    reports_dir.mkdir(exist_ok=True)

    # ------------------------------------------------------------
    # 新版：marker 驱动的并行编排
    # ------------------------------------------------------------
    # 主线程负责抽帧（对每个子目录串行调 extract_frames.exe，
    # 该 exe 内部自己遍历视频，每抽完一个视频写 _done.marker）
    #
    # watcher 线程负责去重：循环扫描每个子目录下所有 _done.marker，
    # 找到未处理的视频输出目录就跑 dedupe_pic.exe，成功后写 _dedup_done.marker
    #
    # 两者天然并行，磁盘占用最小，视频粒度即抽即删。
    # ------------------------------------------------------------
    import threading

    out_root_manifest = Path(manifest["out_root"])
    # 各子目录的目标目录，watcher 只在这些目录里扫，避免误伤
    sub_dsts: list[Path] = []
    sub_marker_roots: list[Path] = []
    for sub in subs:
        if sub == ".":
            sub_dsts.append(out_root_manifest / src_root.name)
            sub_marker_roots.append(markers_root / src_root.name)
        else:
            sub_dsts.append(out_root_manifest / src_root.name / sub)
            sub_marker_roots.append(markers_root / src_root.name / sub)
    for d in sub_dsts:
        d.mkdir(parents=True, exist_ok=True)
    for d in sub_marker_roots:
        d.mkdir(parents=True, exist_ok=True)

    status_lock = threading.Lock()
    stop_watcher = threading.Event()
    producer_done = threading.Event()
    overall_rc = {"value": 0}
    daily_limit_hit = {"value": False}
    from concurrent.futures import ThreadPoolExecutor
    dedupe_executor = ThreadPoolExecutor(max_workers=dedupe_jobs,
                                         thread_name_prefix="dedupe")
    inflight: set[Path] = set()      # 正在跑或已排入 executor 的 target 目录
    inflight_lock = threading.Lock()

    def save_status_locked():
        with status_lock:
            _save_status(job_dir, status)

    def _dedupe_one(i: int, target: Path, marker_dir: Path) -> None:
        """跑一个视频目录的去重（在 dedupe_executor 线程池里执行）。"""
        _pipeline_log(job_dir, f"[watcher] 去重 {target}  (marker={marker_dir})")
        report_csv = target / "dedupe_report.csv"
        cmd = [dedupe_exe, str(target),
               "--threshold", str(threshold),
               "--motion-threshold", str(motion_threshold),
               "--report", str(report_csv),
               "--marker-dir", str(marker_dir),
               "--lock-ttl", str(dedupe_lock_ttl)]
        if scene_protect:
            cmd.append("--scene-protect")
        if protect_arg:
            cmd.extend(["--protect", protect_arg])
        if apply_delete:
            cmd.append("--apply")
            if hard_delete:
                cmd.append("--hard-delete")
            else:
                cmd.extend(["--trash-dir", str(target / "_trash")])
        rc = _run_child(cmd, job_dir)
        if rc == 0:
            with status_lock:
                status.subs[i].videos_deduped += 1
            _pipeline_log(job_dir, f"[watcher] [OK] {target}")
            try:
                cum = _append_stats(target, data_drive=args.data_drive or "Z:")
            except Exception as e:
                cum = -1
                _pipeline_log(job_dir, f"[watcher] 统计失败: {e}")
            if cum >= 0:
                _pipeline_log(job_dir,
                    f"[watcher] 当日累计剩余 {cum} / 阈值 {daily_remain_limit}")
                if daily_remain_limit > 0 and cum >= daily_remain_limit:
                    _pipeline_log(job_dir,
                        f"[watcher] 已达当日剩余阈值 {daily_remain_limit}，"
                        "停止 watcher + 抽帧")
                    stop_watcher.set()
                    daily_limit_hit["value"] = True
                    with status_lock:
                        status.last_message = (
                            f"已达当日剩余阈值 {daily_remain_limit}，自动停止"
                        )
                    save_status_locked()
        else:
            _pipeline_log(job_dir, f"[watcher] [FAIL rc={rc}] {target}")
            overall_rc["value"] = 1
        save_status_locked()

    # ---- watcher 线程 ----
    def watcher_loop():
        _pipeline_log(job_dir,
            f"[watcher] 启动，扫描 markers_root，并发={dedupe_jobs}")
        while not stop_watcher.is_set():
            any_work = False
            for i, sub_mr in enumerate(sub_marker_roots):
                if not sub_mr.is_dir():
                    continue
                # 递归找所有 _done.marker
                try:
                    markers = list(sub_mr.rglob("_done.marker"))
                except Exception as e:
                    _pipeline_log(job_dir, f"[watcher] rglob 失败 {sub_mr}: {e}")
                    continue
                for m in markers:
                    marker_dir_i = m.parent
                    # 已完成：跳过
                    if (marker_dir_i / "_dedup_done.marker").is_file():
                        continue
                    # marker 相对 sub_mr 的路径 → 套到 sub_dsts[i] 得到真实帧目录
                    try:
                        rel = marker_dir_i.relative_to(sub_mr)
                    except Exception:
                        continue
                    target = sub_dsts[i] / rel
                    if not target.is_dir():
                        continue
                    with inflight_lock:
                        if target in inflight:
                            continue
                        inflight.add(target)
                    any_work = True
                    dedupe_executor.submit(_dedupe_one, i, target, marker_dir_i)
                    if stop_watcher.is_set():
                        return

            # 更新 videos_extracted 计数（不管 watcher 有没有活干都更新）
            with status_lock:
                for i, sub_mr in enumerate(sub_marker_roots):
                    try:
                        cnt = sum(1 for _ in sub_mr.rglob("_done.marker"))
                        status.subs[i].videos_extracted = cnt
                    except Exception:
                        pass
            save_status_locked()

            # 停止条件：producer 完成 且 本轮没干活
            if producer_done.is_set() and not any_work:
                _pipeline_log(job_dir, "[watcher] 生产者已完成且无剩余任务，退出")
                return
            time.sleep(watch_interval)

    watcher_thread = threading.Thread(target=watcher_loop, name="dedupe-watcher", daemon=False)
    watcher_thread.start()

    # ---- 主线程（生产者）：串行对每个子目录跑抽帧 ----
    for i, sub in enumerate(subs, 1):
        # 每个子目录开始前检查日限：一旦命中，剩下的子目录不再抽
        if daily_limit_hit["value"]:
            _pipeline_log(job_dir,
                f"[主线程] 已达日限，跳过剩余 {len(subs)-i+1} 个子目录的抽帧")
            break
        sub_status = status.subs[i - 1]
        status.current_sub_idx = i
        status.last_message = f"抽帧子目录 [{i}/{len(subs)}] {sub}"
        sub_status.stage = "extracting"
        sub_status.started_at = _now_iso()
        save_status_locked()
        _pipeline_log(job_dir, status.last_message)

        sub_src = src_root if sub == "." else src_root / sub
        sub_dst = sub_dsts[i - 1]
        # markers_root 下按 <src_root.name>/<sub> 建镜像子树，跟 sub_dst 的结构一致
        if sub == ".":
            sub_marker_root = markers_root / src_root.name
        else:
            sub_marker_root = markers_root / src_root.name / sub
        sub_marker_root.mkdir(parents=True, exist_ok=True)

        rc = _run_child(
            [extract_exe, str(sub_src), str(sub_dst),
             "--fps", str(fps), "--ext", ext,
             "--jobs", str(extract_jobs),
             "--lock-ttl", str(extract_lock_ttl),
             "--markers-root", str(sub_marker_root)],
            job_dir,
        )
        sub_status.extract_rc = rc
        if rc != 0:
            sub_status.stage = "failed"
            sub_status.note = f"抽帧失败 rc={rc}"
            sub_status.ended_at = _now_iso()
            overall_rc["value"] = 1
            save_status_locked()
            _pipeline_log(job_dir, f"[FAIL] {sub} 抽帧失败 rc={rc}")
            continue

        sub_status.stage = "done"
        sub_status.ended_at = _now_iso()
        save_status_locked()
        _pipeline_log(job_dir, f"[OK] 子目录 {sub} 抽帧完成，去重由 watcher 处理")

    # 通知 watcher：所有抽帧完成，处理完剩余任务后退出
    producer_done.set()
    status.last_message = "抽帧完成，等待 watcher 去重剩余任务..."
    save_status_locked()
    _pipeline_log(job_dir, "[主线程] 抽帧完成，等待 watcher...")

    watcher_thread.join()
    # 等待线程池里正在跑的 dedupe 收尾
    _pipeline_log(job_dir, "[watcher] 等待去重线程池收尾...")
    dedupe_executor.shutdown(wait=True)

    status.state = "done" if overall_rc["value"] == 0 else "failed"
    status.ended_at = _now_iso()
    status.last_message = "全部完成" if overall_rc["value"] == 0 else "部分失败，见 pipeline.log"
    save_status_locked()
    _pipeline_log(job_dir, f"worker 结束，overall_rc={overall_rc['value']}")
    return overall_rc["value"]


_child_log_counter = 0
_child_log_lock = None  # 延迟初始化


def _run_child(cmd: list[str], job_dir: Path) -> int:
    """启动子进程，stdout+stderr 写到 job_dir/children/<seq>_<name>.log；
    完成后把该 log 追加到 job_dir/worker.log。这样多线程并发调用时，
    每个子进程有独立的 fd，避免 macOS/Windows 上并发写同一个日志文件的坑。"""
    global _child_log_counter, _child_log_lock
    if _child_log_lock is None:
        import threading
        _child_log_lock = threading.Lock()

    _pipeline_log(job_dir, "$ " + " ".join(_shq(c) for c in cmd))

    children_dir = job_dir / "children"
    children_dir.mkdir(exist_ok=True)
    with _child_log_lock:
        _child_log_counter += 1
        seq = _child_log_counter
    exe_name = Path(cmd[0]).name
    child_log = children_dir / f"{seq:04d}_{exe_name}.log"

    # Windows 上禁止给子 exe 弹黑窗口：worker 本身无控制台，Popen 默认会给
    # 每个子控制台程序分配新窗口。stdout/stderr 已重定向到 child_log 文件，
    # exit code 走进程退出码，跟窗口无关，静默完全不影响统计。
    creationflags = 0
    if os.name == "nt":
        CREATE_NO_WINDOW = 0x08000000
        creationflags = CREATE_NO_WINDOW

    try:
        with child_log.open("wb") as logf:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=subprocess.STDOUT,
                cwd=str(job_dir),
                creationflags=creationflags,
            )
            rc = proc.wait()
    except FileNotFoundError as e:
        _pipeline_log(job_dir, f"[ERROR] 子进程启动失败: {e}")
        return 127

    # 追加到 worker.log（用锁串行化，只在这里合并）
    try:
        with _child_log_lock:
            with (job_dir / "worker.log").open("ab") as merged:
                merged.write(f"\n===== [{seq:04d}] {exe_name}  rc={rc} =====\n".encode("utf-8"))
                merged.write(child_log.read_bytes())
    except Exception as e:
        _pipeline_log(job_dir, f"[WARN] 日志合并失败: {e}")

    return rc


def _shq(s: str) -> str:
    if not s or any(c.isspace() for c in s) or '"' in s:
        return '"' + s.replace('"', '\\"') + '"'
    return s


# ------------------------------- list / status ------------------------------

def _find_all_jobs(out_root: Path) -> list[Path]:
    root = jobs_root(out_root)
    if not root.is_dir():
        return []
    return sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name)


def _resolve_job_dir(out_root: Path, job_id: str | None) -> Path | None:
    all_jobs = _find_all_jobs(out_root)
    if not all_jobs:
        return None
    if job_id:
        for p in all_jobs:
            if p.name == job_id:
                return p
        return None
    return all_jobs[-1]


def cmd_list(args: argparse.Namespace) -> int:
    out_root = Path(args.out_root) if args.out_root else default_out_root(args.data_drive or DEFAULT_DATA_DRIVE)
    all_jobs = _find_all_jobs(out_root)
    if not all_jobs:
        print(f"[空] 未找到任何任务（{jobs_root(out_root)}）")
        return 0
    print(f"共 {len(all_jobs)} 个任务：")
    print(f"{'JOB_ID':<28} {'STATE':<10} {'PROGRESS':<14} {'CREATED_AT':<20} {'ALIVE':<6}")
    for p in all_jobs:
        st = _load_status(p)
        if not st:
            print(f"{p.name:<28} {'??':<10} {'-':<14} {'-':<20} {'-':<6}")
            continue
        done = sum(1 for s in st.subs if s.stage == "done")
        prog = f"{done}/{st.total_subs}"
        alive = "yes" if _process_alive(st.pid) else "no"
        print(f"{st.job_id:<28} {st.state:<10} {prog:<14} {st.created_at:<20} {alive:<6}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    out_root = Path(args.out_root) if args.out_root else default_out_root(args.data_drive or DEFAULT_DATA_DRIVE)
    job_dir = _resolve_job_dir(out_root, args.job_id)
    if not job_dir:
        print("[错误] 未找到任务。")
        return 2
    st = _load_status(job_dir)
    if not st:
        print(f"[错误] 无法读取 status.json：{job_dir}")
        return 2

    alive = _process_alive(st.pid)
    print("=" * 60)
    print(f"  job_id       : {st.job_id}")
    print(f"  state        : {st.state}    (worker PID {st.pid} alive={alive})")
    print(f"  created_at   : {st.created_at}")
    print(f"  started_at   : {st.started_at or '-'}")
    print(f"  ended_at     : {st.ended_at or '-'}")
    print(f"  src_root     : {st.src_root}")
    print(f"  out_root     : {st.out_root}")
    print(f"  apply_delete : {st.apply_delete}    threshold={st.threshold}  fps={st.fps}  ext={st.ext}")
    print(f"  last_message : {st.last_message}")
    print("-" * 60)
    print(f"  子目录进度 ({st.current_sub_idx}/{st.total_subs})：")
    for i, s in enumerate(st.subs, 1):
        marker = "*" if i == st.current_sub_idx and st.state == "running" else " "
        print(f"   {marker}[{i}] {s.name:<20} stage={s.stage:<10} "
              f"抽帧={s.videos_extracted}  去重={s.videos_deduped}  "
              f"extract_rc={s.extract_rc}  {s.note}")
    print("=" * 60)
    return 0


# ------------------------------- logs / stop -------------------------------

def cmd_logs(args: argparse.Namespace) -> int:
    out_root = Path(args.out_root) if args.out_root else default_out_root(args.data_drive or DEFAULT_DATA_DRIVE)
    job_dir = _resolve_job_dir(out_root, args.job_id)
    if not job_dir:
        print("[错误] 未找到任务。")
        return 2
    log_file = job_dir / ("pipeline.log" if args.which == "pipeline" else "worker.log")
    if not log_file.is_file():
        print(f"[错误] 日志不存在: {log_file}")
        return 2

    if not args.follow:
        with log_file.open("rb") as f:
            data = f.read()
        sys.stdout.buffer.write(data)
        sys.stdout.flush()
        return 0

    # tail -f
    with log_file.open("rb") as f:
        f.seek(0, os.SEEK_END)
        try:
            while True:
                chunk = f.read(4096)
                if chunk:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.flush()
                else:
                    time.sleep(0.5)
                    st = _load_status(job_dir)
                    if st and st.state in ("done", "failed", "stopped"):
                        time.sleep(0.5)
                        chunk = f.read()
                        if chunk:
                            sys.stdout.buffer.write(chunk)
                            sys.stdout.flush()
                        break
        except KeyboardInterrupt:
            pass
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    out_root = Path(args.out_root) if args.out_root else default_out_root(args.data_drive or DEFAULT_DATA_DRIVE)
    job_dir = _resolve_job_dir(out_root, args.job_id)
    if not job_dir:
        print("[错误] 未找到任务。")
        return 2
    st = _load_status(job_dir)
    if not st:
        return 2
    if not _process_alive(st.pid):
        print(f"[提示] 任务已不在运行（PID {st.pid} 不存在）。")
        return 0
    try:
        if os.name == "nt":
            import ctypes
            PROCESS_TERMINATE = 0x0001
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, st.pid)
            if h:
                ctypes.windll.kernel32.TerminateProcess(h, 1)
                ctypes.windll.kernel32.CloseHandle(h)
        else:
            os.kill(st.pid, signal.SIGTERM)
        st.state = "stopped"
        st.ended_at = _now_iso()
        st.last_message = "被 stop 命令终止"
        _save_status(job_dir, st)
        print(f"[OK] 已停止任务 {st.job_id} (PID {st.pid})")
        return 0
    except Exception as e:
        print(f"[错误] 停止失败: {e}")
        return 1


# ------------------------------- 主入口 -------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pipeline",
        description="图片流水线编排器：后台跑抽帧+去重，可查状态、可看日志。",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    common_out = argparse.ArgumentParser(add_help=False)
    common_out.add_argument("--out-root", default=None,
                            help="输出根目录（默认 Z:\\切帧结果）")
    common_out.add_argument("--data-drive", default=None,
                            help="数据盘盘符（默认 Z:）")

    # submit
    sp = sub.add_parser("submit", parents=[common_out], help="提交新任务并后台运行")
    sp.add_argument("--auto", action="store_true", help="无交互，必须配合 -s/--src")
    sp.add_argument("-s", "--src", default=None, help="源目录（--auto 模式必填）")
    sp.add_argument("-n", "--subs", default=None,
                    help="子目录选择：'1,2' / '1-3' / 'all' / 具体名字（--auto 模式）")
    sp.add_argument("--data-prefix", default=None,
                    help="源目录前缀（默认 sjbz_）")
    sp.add_argument("-t", "--threshold", type=int, default=3,
                    help="dedupe 相似阈值（Hamming 距离），默认 3")
    sp.add_argument("--fps", type=float, default=1.0, help="抽帧频率，默认 1.0")
    sp.add_argument("--extract-jobs", type=int, default=1,
                    help="抽帧并发数（视频粒度，透传给 extract_frames.exe --jobs）。默认 1")
    sp.add_argument("--extract-lock-ttl", type=float, default=900.0,
                    help="抽帧视频锁 TTL 秒（多机共享盘用），默认 900（15 分钟）")
    sp.add_argument("--dedupe-jobs", type=int, default=1,
                    help="去重并发数（目录粒度），默认 1")
    sp.add_argument("--dedupe-lock-ttl", type=float, default=900.0,
                    help="去重锁 TTL 秒，默认 900（15 分钟）")
    sp.add_argument("--markers-root", type=str, default=None,
                    help=(
                        "marker/lock 集中存放的根目录（推荐指向多机共享盘，"
                        "例如 Z:\\pic-clear-markers）。不传则默认 <数据盘>\\pic-clear-markers"
                    ))
    sp.add_argument("--ext", default=".h265,.mp4",
                    help="视频扩展名（逗号分隔可多值）。默认 .h265,.mp4")
    sp.add_argument("-y", "--apply", action="store_true",
                    help="真删（默认只 dry-run 出报告）")
    sp.add_argument("-H", "--hard-delete", action="store_true",
                    help="真删时直接永久删除，不落 _trash 目录（默认落 _trash）")
    sp.add_argument("-m", "--motion-threshold", type=float, default=0.12,
                    help="车运动保护阈值，越大越严格。默认 0.12")
    sp.add_argument("-L", "--daily-remain-limit", type=int, default=80000,
                    help="当日累计剩余达此值 pipeline 自动停止（0=禁用）。默认 80000")
    sp.add_argument("-S", "--scene-protect", action="store_true",
                    help="场景保护：纯色屏/渐变屏等异常帧强制保留（推荐开）")
    sp.add_argument("--watch-interval", type=float, default=3.0,
                    help="watcher 扫描 _done.marker 的间隔秒。默认 3.0")
    sp.add_argument("--protect", default=None,
                    help=(
                        "传给 dedupe_pic.exe 的 --protect 参数（COCO 英文类名，逗号分隔）。"
                        "留空则用 dedupe_pic 自己的默认值（person,bicycle,car,motorcycle,bus,train,truck）。"
                        "人硬保护；车类要相邻帧运动才保留；其它类别静止不动等于永久保留。"
                    ))

    # worker (internal)
    wp = sub.add_parser("worker", parents=[common_out], help="[内部] 后台执行体，不要手动调")
    wp.add_argument("--job-id", required=True)

    # list / status / logs / stop
    sub.add_parser("list", parents=[common_out], help="列出所有任务")

    stp = sub.add_parser("status", parents=[common_out], help="查看任务状态")
    stp.add_argument("job_id", nargs="?", default=None, help="不给就看最近一个")

    lp = sub.add_parser("logs", parents=[common_out], help="查看/tail 任务日志")
    lp.add_argument("job_id", nargs="?", default=None)
    lp.add_argument("-f", "--follow", action="store_true", help="tail -f 模式")
    lp.add_argument("--which", choices=["pipeline", "worker"], default="worker",
                    help="pipeline=编排日志，worker=子进程 stdout（默认）")

    stp2 = sub.add_parser("stop", parents=[common_out], help="停止任务")
    stp2.add_argument("job_id", nargs="?", default=None)

    # 单独 --fingerprint（跟另外两个 exe 保持一致）
    p.add_argument("--fingerprint", action="store_true",
                   help="打印本机指纹并退出（用来申请 license.lic）")
    return p


def main() -> int:
    # --fingerprint 短路，不校验授权
    if "--fingerprint" in sys.argv:
        try:
            from licensing import get_fingerprint
            print(get_fingerprint())
            return 0
        except Exception as e:
            print(f"[ERROR] 无法计算指纹: {e}", file=sys.stderr)
            return 2

    _check_license_or_die()

    parser = build_parser()
    args = parser.parse_args()

    # 单实例：submit 和 worker 都要抢锁；查看类命令 (list/status/logs/stop) 不锁
    if args.cmd == "submit":
        if not _acquire_single_instance_lock("pic-clear-pipeline-submit"):
            print("=" * 60)
            print("[ERROR] 已有一个 pipeline submit 正在跑，本次拒绝启动。")
            print("        请等前一个跑完，或用 pipeline.exe list 查看当前任务。")
            print("        （只锁 submit 阶段，后台 worker 一旦启动就会释放）")
            print("=" * 60)
            return 4
        return cmd_submit(args)
    elif args.cmd == "worker":
        # worker 是 detach 后台进程：屏蔽崩溃对话框，避免有人手滑关掉
        _suppress_windows_error_dialogs()
        # 单实例：同 job_id 只能有一个 worker
        lock_name = f"pic-clear-pipeline-worker-{args.job_id}"
        if not _acquire_single_instance_lock(lock_name):
            print(f"[ERROR] job_id={args.job_id} 已有 worker 在跑，本次拒绝启动。",
                  file=sys.stderr)
            return 4
        return cmd_worker(args)
    elif args.cmd == "list":
        return cmd_list(args)
    elif args.cmd == "status":
        return cmd_status(args)
    elif args.cmd == "logs":
        return cmd_logs(args)
    elif args.cmd == "stop":
        return cmd_stop(args)
    else:
        parser.print_help()
        return 2


if __name__ == "__main__":
    sys.exit(main())
