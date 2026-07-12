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
import io
import json
import os
import re
import shutil
import signal
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


# ------------------------------- 默认路径 -----------------------------------

DEFAULT_DATA_DRIVE = "Z:"
DEFAULT_DATA_PREFIX = "sjbz_"
DEFAULT_OUT_SUBDIR = "切帧结果"


def default_out_root(data_drive: str) -> Path:
    return Path(f"{data_drive}\\{DEFAULT_OUT_SUBDIR}")


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


def resolve_self_exe() -> str:
    """自身 exe 路径，供 detach 时 spawn 新的 worker 进程。"""
    if getattr(sys, "frozen", False):
        return sys.executable
    return sys.executable + " " + str(Path(__file__).resolve())


# ------------------------------- 状态 --------------------------------------

@dataclass
class SubStatus:
    name: str
    stage: str = "pending"        # pending / extracting / dedup_dryrun / dedup_apply / done / failed / skipped
    started_at: str | None = None
    ended_at: str | None = None
    extract_rc: int | None = None
    dedup_rc: int | None = None
    note: str = ""


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

def interactive_pick(data_drive: str, data_prefix: str) -> tuple[Path, list[str]]:
    """交互选择 sjbz 根目录 + 子目录列表。"""
    drive = Path(f"{data_drive}\\")
    if not drive.is_dir():
        print(f"[错误] 数据盘 {data_drive} 不存在。")
        sys.exit(2)

    # 找 sjbz_*
    candidates = sorted([p for p in drive.iterdir() if p.is_dir() and p.name.startswith(data_prefix)])
    if not candidates:
        raw = input(f"[提示] 在 {data_drive}\\ 下没找到 {data_prefix}* 目录。\n请输入源目录: ").strip()
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
        return src_root, ["."]

    print(f"\n[子目录] {src_root} 下的一级子目录（共 {len(subs)} 个）：")
    for i, name in enumerate(subs, 1):
        print(f"    [{i}] {name}")
    print()
    print("输入方式：序号列表 (1,2) / 区间 (1-3) / 全部 (all)")
    sel = input("请输入要处理的子目录: ").strip()
    picked = parse_selection(sel, subs)
    if not picked:
        print(f"[错误] 输入 {sel!r} 无法解析。")
        sys.exit(2)
    print(f"\n[已选] {len(picked)} 个子目录：{', '.join(picked)}\n")
    return src_root, picked


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
    out_root = Path(args.out_root) if args.out_root else default_out_root(data_drive)

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
        src_root, subs = interactive_pick(data_drive, data_prefix)

    # --- 建 job_id + 目录 ---
    job_id = f"job_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    job_dir = _job_dir(out_root, job_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "job_id": job_id,
        "created_at": _now_iso(),
        "src_root": str(src_root),
        "out_root": str(out_root),
        "subs": subs,
        "threshold": args.threshold,
        "fps": args.fps,
        "ext": args.ext,
        "apply_delete": args.apply,
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

def _pipeline_log(job_dir: Path, msg: str) -> None:
    line = f"[{_now_iso()}] {msg}"
    print(line, flush=True)
    with (job_dir / "pipeline.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def cmd_worker(args: argparse.Namespace) -> int:
    """[内部] detach 后台执行体。"""
    out_root = Path(args.out_root).resolve()
    job_dir = _job_dir(out_root, args.job_id)
    if not job_dir.is_dir():
        print(f"[FATAL] job 目录不存在: {job_dir}", file=sys.stderr)
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

    extract_exe = resolve_worker_exe("extract_frames")
    dedupe_exe = resolve_worker_exe("dedupe_pic")

    reports_dir = job_dir / "reports"
    reports_dir.mkdir(exist_ok=True)

    overall_rc = 0
    for i, sub in enumerate(subs, 1):
        sub_status = status.subs[i - 1]
        status.current_sub_idx = i
        status.last_message = f"处理子目录 [{i}/{len(subs)}] {sub}"
        sub_status.stage = "extracting"
        sub_status.started_at = _now_iso()
        _save_status(job_dir, status)
        _pipeline_log(job_dir, status.last_message)

        if sub == ".":
            sub_src = src_root
            sub_dst = Path(manifest["out_root"]) / src_root.name
        else:
            sub_src = src_root / sub
            sub_dst = Path(manifest["out_root"]) / src_root.name / sub
        sub_dst.mkdir(parents=True, exist_ok=True)

        # --- 抽帧 ---
        rc = _run_child(
            [extract_exe, str(sub_src), str(sub_dst),
             "--fps", str(fps), "--ext", ext],
            job_dir,
        )
        sub_status.extract_rc = rc
        if rc != 0:
            sub_status.stage = "failed"
            sub_status.note = f"抽帧失败 rc={rc}"
            sub_status.ended_at = _now_iso()
            overall_rc = 1
            _save_status(job_dir, status)
            _pipeline_log(job_dir, f"[FAIL] {sub} 抽帧失败 rc={rc}")
            continue

        # --- 去重 dry-run ---
        sub_status.stage = "dedup_dryrun"
        _save_status(job_dir, status)
        report_csv = reports_dir / f"{sub}_report.csv"
        rc = _run_child(
            [dedupe_exe, str(sub_dst), "--threshold", str(threshold),
             "--report", str(report_csv)],
            job_dir,
        )
        sub_status.dedup_rc = rc
        if rc != 0:
            sub_status.stage = "failed"
            sub_status.note = f"dedup dry-run 失败 rc={rc}"
            sub_status.ended_at = _now_iso()
            overall_rc = 1
            _save_status(job_dir, status)
            _pipeline_log(job_dir, f"[FAIL] {sub} dedup dry-run 失败 rc={rc}")
            continue

        # --- 真删（可选） ---
        if apply_delete:
            sub_status.stage = "dedup_apply"
            _save_status(job_dir, status)
            trash_dir = sub_dst / f"_trash_{manifest['job_id']}"
            rc = _run_child(
                [dedupe_exe, str(sub_dst), "--threshold", str(threshold),
                 "--apply", "--trash-dir", str(trash_dir),
                 "--report", str(report_csv)],
                job_dir,
            )
            sub_status.dedup_rc = rc
            if rc != 0:
                sub_status.stage = "failed"
                sub_status.note = f"dedup apply 失败 rc={rc}"
                sub_status.ended_at = _now_iso()
                overall_rc = 1
                _save_status(job_dir, status)
                _pipeline_log(job_dir, f"[FAIL] {sub} dedup apply 失败 rc={rc}")
                continue

        sub_status.stage = "done"
        sub_status.ended_at = _now_iso()
        _save_status(job_dir, status)
        _pipeline_log(job_dir, f"[OK] 子目录 {sub} 完成")

    status.state = "done" if overall_rc == 0 else "failed"
    status.ended_at = _now_iso()
    status.last_message = "全部完成" if overall_rc == 0 else "部分失败，见各子目录状态"
    _save_status(job_dir, status)
    _pipeline_log(job_dir, f"worker 结束，overall_rc={overall_rc}")
    return overall_rc


def _run_child(cmd: list[str], job_dir: Path) -> int:
    """把子进程 stdout+stderr 追加写到 job_dir/worker.log；返回 exit code。"""
    _pipeline_log(job_dir, "$ " + " ".join(_shq(c) for c in cmd))
    try:
        with (job_dir / "worker.log").open("ab") as logf:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=subprocess.STDOUT,
                cwd=str(job_dir),
            )
            return proc.wait()
    except FileNotFoundError as e:
        _pipeline_log(job_dir, f"[ERROR] 子进程启动失败: {e}")
        return 127


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
        print(f"   {marker}[{i}] {s.name:<20} stage={s.stage:<14} "
              f"extract_rc={s.extract_rc}  dedup_rc={s.dedup_rc}  {s.note}")
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
    sp.add_argument("--auto", action="store_true", help="无交互，必须配合 --src")
    sp.add_argument("--src", default=None, help="源目录（--auto 模式必填）")
    sp.add_argument("--subs", default=None,
                    help="子目录选择：'1,2' / '1-3' / 'all' / 具体名字（--auto 模式）")
    sp.add_argument("--data-prefix", default=None,
                    help="源目录前缀（默认 sjbz_）")
    sp.add_argument("--threshold", type=int, default=3, help="dedupe 阈值，默认 3")
    sp.add_argument("--fps", type=float, default=1.0, help="抽帧频率，默认 1.0")
    sp.add_argument("--ext", default=".h265", help="视频扩展名，默认 .h265")
    sp.add_argument("--apply", action="store_true",
                    help="真删（默认只 dry-run 出报告，需要人工再启动 apply）")

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

    if args.cmd == "submit":
        return cmd_submit(args)
    elif args.cmd == "worker":
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
