"""跨平台"父进程死了子进程一起死"子进程组管理.

Windows 上用 Job Object + JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. 只要 GUI 进程
死 (不管正常 exit / os._exit / 任务管理器强杀 / OOM), 内核会自动把 Job 里
所有还活着的子进程 (dedupe_pic.exe / ffmpeg.exe / ...) 一并 terminate.

POSIX (mac / Linux) 上用进程组 + os.killpg 兜底. 强杀不保证, 但比裸 Popen 好.

用法::

    grp = SubprocGroup()
    proc = grp.popen(cmd, stdout=..., stderr=...)   # 自动加入 Job
    ...
    grp.terminate_all()      # 主动全杀 (超时 kill)
    grp.close()              # 释放 Job handle (可选, GUI 退出前调)

线程安全: popen / terminate_all / close 都加了锁, 可跨线程调.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
from typing import Any

_IS_WINDOWS = sys.platform.startswith("win")

# ============ Windows Job Object 常量 (winnt.h) ============
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9
_PROCESS_ALL_ACCESS = 0x1F0FFF
_CREATE_SUSPENDED = 0x00000004
_CREATE_NO_WINDOW = 0x08000000
_ERROR_ACCESS_DENIED = 5


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _win_create_job() -> int | None:
    """创建一个 Job Object, 设置 KILL_ON_JOB_CLOSE. 失败返回 None."""
    if not _IS_WINDOWS:
        return None
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    h = kernel32.CreateJobObjectW(None, None)
    if not h:
        return None

    info = _JobObjectExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    ok = kernel32.SetInformationJobObject(
        ctypes.c_void_p(h),
        _JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        kernel32.CloseHandle(ctypes.c_void_p(h))
        return None
    return h


def _win_assign_pid_to_job(job_handle: int, pid: int) -> bool:
    """把 pid 加进 Job. Windows 8+ 支持嵌套 Job, 子进程自己在 Job 里也 OK.
    Access Denied 通常是子进程已在别的 Job 且不允许再套 -> 忽略."""
    if not _IS_WINDOWS or not job_handle:
        return False
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    hp = kernel32.OpenProcess(_PROCESS_ALL_ACCESS, False, pid)
    if not hp:
        return False
    try:
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        ok = kernel32.AssignProcessToJobObject(
            ctypes.c_void_p(job_handle), ctypes.c_void_p(hp))
        if not ok:
            err = ctypes.GetLastError()
            if err == _ERROR_ACCESS_DENIED:
                # 已经在别的 Job (Windows 7 不支持嵌套), 就算了
                return False
        return bool(ok)
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(hp))


def _win_resume_process_main_thread(proc: subprocess.Popen) -> None:
    """CREATE_SUSPENDED 启动的进程, 挂 Job 后要 ResumeThread 让它跑起来."""
    if not _IS_WINDOWS:
        return
    kernel32 = ctypes.windll.kernel32
    # subprocess.Popen 在 Windows 上会把主线程 handle 存在 _handle 里, 但没直接给
    # 出主线程 handle. 走 OpenThread(tid) 拿 handle.
    # tid 在 Popen 内部存的属性名不稳定, 用 CreateToolhelp32Snapshot 找也太重.
    # 简单办法: Popen 的 Windows 实现暴露了 _handle (proc handle), 不暴露 thread handle.
    # 但 subprocess._winapi 里 create_process 返回过 (hp, ht, pid, tid).
    # 我们绕开: 用 NtResumeProcess 一步搞定所有线程.
    try:
        ntdll = ctypes.windll.ntdll
        ntdll.NtResumeProcess.restype = ctypes.c_uint32
        ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
        # proc._handle 是进程 handle (int), 直接传
        h = int(proc._handle)  # type: ignore[attr-defined]
        ntdll.NtResumeProcess(ctypes.c_void_p(h))
    except Exception:
        # 兜底: 尽力 ResumeThread. subprocess._winapi 里 _handle 是 HANDLE
        # 拿不到主线程 handle, 只能靠 NtResumeProcess. 失败就算了, 后果是进程一直挂着.
        pass


def _win_close_handle(h: int) -> None:
    if not _IS_WINDOWS or not h:
        return
    try:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))
    except Exception:
        pass


class SubprocGroup:
    """
    Windows: 内部持有一个 Job Object, 所有 popen 出来的子进程都加入 Job.
             Job handle 一 close (或进程死), 子进程全被 kernel kill.
    POSIX  : 记录所有 Popen, terminate_all 时统一发信号; 也 setsid 让它们
             成为独立进程组便于 killpg.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: list[subprocess.Popen] = []
        self._closed = False
        if _IS_WINDOWS:
            self._job = _win_create_job()
        else:
            self._job = None

    def popen(self, cmd: list[str], **kwargs: Any) -> subprocess.Popen:
        """行为等价 subprocess.Popen, 但会把子进程绑到 Job / 独立进程组."""
        with self._lock:
            if self._closed:
                raise RuntimeError("SubprocGroup is closed")

        if _IS_WINDOWS:
            # 先 CREATE_SUSPENDED 起来, 挂 Job 再 Resume, 避免子进程刚跑就 fork 出孙进程
            # 逃出 Job
            flags = int(kwargs.pop("creationflags", 0))
            flags |= _CREATE_SUSPENDED | _CREATE_NO_WINDOW
            proc = subprocess.Popen(cmd, creationflags=flags, **kwargs)
            if self._job is not None:
                try:
                    _win_assign_pid_to_job(self._job, proc.pid)
                except Exception:
                    pass
            _win_resume_process_main_thread(proc)
        else:
            # POSIX: 起独立进程组, 便于 killpg
            def _preexec() -> None:
                try:
                    os.setsid()
                except Exception:
                    pass
            kwargs.setdefault("preexec_fn", _preexec)
            proc = subprocess.Popen(cmd, **kwargs)

        with self._lock:
            self._procs.append(proc)
        return proc

    def _list_active(self) -> list[subprocess.Popen]:
        with self._lock:
            return [p for p in self._procs if p.poll() is None]

    def terminate_all(self, wait_timeout: float = 3.0) -> None:
        """主动全杀. 先 terminate, 超时再 kill.

        非阻塞设计: 单次调用最多 wait_timeout 秒.
        """
        actives = self._list_active()
        if not actives:
            return

        # 1) 温柔 terminate
        for p in actives:
            try:
                if _IS_WINDOWS:
                    p.terminate()
                else:
                    try:
                        os.killpg(os.getpgid(p.pid), 15)  # SIGTERM 到整组
                    except Exception:
                        p.terminate()
            except Exception:
                pass

        # 2) 等一下
        deadline = time.monotonic() + wait_timeout
        while time.monotonic() < deadline:
            actives = self._list_active()
            if not actives:
                return
            time.sleep(0.1)

        # 3) 强杀
        for p in self._list_active():
            try:
                if _IS_WINDOWS:
                    p.kill()
                else:
                    try:
                        os.killpg(os.getpgid(p.pid), 9)  # SIGKILL
                    except Exception:
                        p.kill()
            except Exception:
                pass

    def close(self) -> None:
        """释放 Job handle (Windows). 关 handle 会立刻杀掉 Job 里所有进程.

        调用后此 group 不可再 popen.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            job = self._job
            self._job = None
        if job is not None:
            _win_close_handle(job)

    # 兼容 with 语法
    def __enter__(self) -> "SubprocGroup":
        return self

    def __exit__(self, *a: Any) -> None:
        self.terminate_all()
        self.close()
