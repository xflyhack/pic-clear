"""env_probe.py —— 运行环境自动探测.

背景 & 硬规则详见 docs/env_probe.md.

一句话: 上游反复改磁盘挂载 (Z: 映射盘 / \\filestor UNC 直连 / 网络位置 / Samba 服务端 /
LongPath 开关), 每次改一次我们下游软件就要修一次. 与其反复打补丁, 不如**运行时探测**
环境画像, 让代码按画像挑策略.

所有新增 GUI (extract_gui / dedupe_gui / classify_gui / pipe_gui / ...) 启动时
调用 probe_and_log() 一行搞定, 日志开头就有环境画像, 排查零成本.

对外 API:
    probe_env() -> Env             # 结构化环境画像 (纯读, 无副作用)
    probe_and_log(logger=None)     # 探测 + 打印多行画像到 logger 或 stderr
    get_env() -> Env               # 单例, 首次调用触发探测, 后续复用缓存
    should_do_samba_retry() -> bool
    samba_retry_wait_secs() -> float

Env 字段: 见 dataclass 定义 (v0.4.73 起大扩展).

非 Windows 平台 (Mac / Linux) 上, 所有探测原样退回默认值,
零回归、零性能损失.
"""
from __future__ import annotations

import getpass
import os
import platform
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional


# --------------- 数据结构 ---------------


@dataclass
class DriveInfo:
    r"""单个盘符信息."""
    letter: str = ""         # 'C:', 'D:', 'Z:'
    drive_type: int = 0      # Win32 DriveType 0-6
    drive_type_name: str = "Unknown"
    file_system: str = ""    # NTFS/FAT32/exFAT/(空 for 网络)
    volume_name: str = ""
    provider_name: str = ""  # UNC 原始路径 (若网络盘)
    free_bytes: int = 0
    total_bytes: int = 0

    def summary(self) -> str:
        gib_free = self.free_bytes / (1024**3) if self.free_bytes else 0
        gib_total = self.total_bytes / (1024**3) if self.total_bytes else 0
        parts = [f"{self.letter} {self.drive_type_name}"]
        if self.file_system:
            parts.append(self.file_system)
        if self.volume_name:
            parts.append(f'"{self.volume_name}"')
        if self.provider_name:
            parts.append(f"-> {self.provider_name}")
        if self.total_bytes:
            parts.append(f"{gib_free:.1f}G / {gib_total:.1f}G")
        return "  ".join(parts)


@dataclass
class NetMount:
    r"""一条网络挂载 (映射盘 / UNC / Network Shortcuts)."""
    kind: str = ""           # 'mapped_drive' / 'network_shortcut' / 'unc_alive'
    drive: str = ""          # 'Z:' 或 ''
    unc: str = ""            # '\\server\share' 或空
    server_hint: str = ""    # 'Samba Server' / 'Windows Server' / 'Unknown'
    smb_dialect: str = ""    # '3.1.1' 等, 拿不到留空
    readable: str = "?"      # 'True' / 'False' / '?'
    writable: str = "?"


@dataclass
class Env:
    r"""运行环境画像. 字段全部只读."""
    # 主机
    platform: str = "posix"
    platform_full: str = ""
    hostname: str = ""
    username: str = ""
    python_version: str = ""
    pid: int = 0
    # 挂载概况
    mount_kind: str = "local"
    server_is_samba: bool = False
    drives: list[DriveInfo] = field(default_factory=list)
    net_mounts: list[NetMount] = field(default_factory=list)
    # SMB 缓存注册表
    long_paths_enabled: bool = False
    smb_dir_cache_secs: int = 10
    smb_file_notfound_cache_secs: int = 5
    smb_file_info_cache_secs: int = 10
    smb_dormant_file_limit: int = -1     # -1 = 未显式设置
    smb_disable_bandwidth_throttling: int = -1
    smb_disable_large_mtu: int = -1
    # 策略结论
    long_prefix_needed: bool = False
    # 元数据
    probe_time_ms: int = 0
    diag_errors: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        r"""一行画像 (向后兼容 v0.4.72 用户)."""
        return (
            f"[ENV] platform={self.platform} "
            f"mount={self.mount_kind} "
            f"samba={'yes' if self.server_is_samba else 'no'} "
            f"long_prefix={'need' if self.long_prefix_needed else 'no'} "
            f"long_paths_enabled={'yes' if self.long_paths_enabled else 'no'} "
            f"dir_cache={self.smb_dir_cache_secs}s "
            f"probe={self.probe_time_ms}ms"
        )

    def full_report(self, probe_paths: list[str] | None = None) -> list[str]:
        r"""多行画像, 供 probe_and_log 输出到日志.

        probe_paths: 额外要探测可达性的路径列表 (源/dst/markers 三根等).
        返回 list[str], 每行一条 (调用方决定用什么 logger 打).
        """
        L: list[str] = []
        L.append("=" * 68)
        L.append("[ENV] pic-clear 运行环境画像 (v0.4.73+)")
        L.append("=" * 68)
        # ----- 主机 -----
        L.append(f"[ENV] host={self.hostname}  user={self.username}  pid={self.pid}")
        L.append(f"[ENV] os={self.platform_full}")
        L.append(f"[ENV] python={self.python_version}")
        # ----- 挂载概况 -----
        L.append(f"[ENV] mount_kind={self.mount_kind}   "
                 f"server_is_samba={'yes' if self.server_is_samba else 'no'}   "
                 f"long_paths_enabled={'yes' if self.long_paths_enabled else 'no'}")

        # ----- 逻辑盘符 -----
        if self.drives:
            L.append(f"[ENV] 逻辑盘符 ({len(self.drives)} 个):")
            for d in self.drives:
                L.append(f"[ENV]   {d.summary()}")
        else:
            L.append("[ENV] 逻辑盘符: (未探测)")

        # ----- 网络挂载 -----
        if self.net_mounts:
            L.append(f"[ENV] 网络挂载 ({len(self.net_mounts)} 个):")
            for m in self.net_mounts:
                seg = f"  {m.kind}"
                if m.drive:
                    seg += f"  {m.drive}"
                if m.unc:
                    seg += f"  {m.unc}"
                if m.server_hint:
                    seg += f"  hint={m.server_hint}"
                if m.smb_dialect:
                    seg += f"  smb_dialect={m.smb_dialect}"
                if m.readable != "?" or m.writable != "?":
                    seg += f"  r/w={m.readable}/{m.writable}"
                L.append("[ENV] " + seg)
        else:
            L.append("[ENV] 网络挂载: 无 (纯本地盘)")

        # ----- SMB 缓存 -----
        L.append("[ENV] SMB 客户端缓存 (LanmanWorkstation\\Parameters, 只读):")
        L.append(f"[ENV]   DirectoryCacheLifetime      = {self.smb_dir_cache_secs}s  "
                 f"(默认 10; Samba 假 miss 的元凶)")
        L.append(f"[ENV]   FileNotFoundCacheLifetime   = {self.smb_file_notfound_cache_secs}s  "
                 f"(默认 5)")
        L.append(f"[ENV]   FileInfoCacheLifetime       = {self.smb_file_info_cache_secs}s  "
                 f"(默认 10)")
        if self.smb_dormant_file_limit >= 0:
            L.append(f"[ENV]   DormantFileLimit            = {self.smb_dormant_file_limit}  "
                     f"(空闲文件上限)")
        if self.smb_disable_bandwidth_throttling >= 0:
            L.append(f"[ENV]   DisableBandwidthThrottling  = {self.smb_disable_bandwidth_throttling}")
        if self.smb_disable_large_mtu >= 0:
            L.append(f"[ENV]   DisableLargeMtu             = {self.smb_disable_large_mtu}")

        # ----- 常用路径可达性 -----
        if probe_paths:
            L.append(f"[ENV] pic-clear 常用路径可达性 ({len(probe_paths)} 条):")
            for p in probe_paths:
                exists_short = "?"
                try:
                    exists_short = "True" if os.path.exists(p) else "False"
                except Exception as e:
                    exists_short = f"ERR:{type(e).__name__}"
                seg = f"  {p}   exists(短)={exists_short}"
                # 长路径版
                if os.name == "nt" and exists_short == "True":
                    try:
                        # 局部 import 防循环
                        from winpath_util import to_long_path  # type: ignore
                        lp = to_long_path(p)
                        if lp != p:
                            try:
                                lex = "True" if os.path.exists(lp) else "False"
                            except Exception as e:
                                lex = f"ERR:{type(e).__name__}"
                            seg += f"   exists(长)={lex}"
                    except Exception:
                        pass
                L.append("[ENV] " + seg)

        # ----- 策略结论 -----
        L.append("[ENV] 决策提示:")
        if self.long_prefix_needed:
            L.append(r"[ENV]   ✓ 需要 \\?\ 长路径前缀 (LongPathsEnabled=0, 系统级未开)")
        else:
            L.append(r"[ENV]   ✗ 不需 \\?\ 前缀 (LongPathsEnabled=1 或 非 Windows)")
        if self.server_is_samba:
            L.append(f"[ENV]   ✓ Samba 环境: marker 假 miss 时会 sleep {self.smb_dir_cache_secs + 1}s 重试")
        else:
            L.append("[ENV]   ✗ 非 Samba/UNC 直连: marker 判定不做 sleep 重试")

        # ----- 诊断错误 (如果有) -----
        if self.diag_errors:
            L.append(f"[ENV] 探测阶段吞掉的异常 ({len(self.diag_errors)} 条):")
            for e in self.diag_errors:
                L.append(f"[ENV]   ! {e}")

        L.append(f"[ENV] 探测耗时 {self.probe_time_ms}ms")
        L.append("=" * 68)
        return L


# --------------- 内部工具 ---------------


_DRIVE_TYPES = {
    0: "Unknown", 1: "NoRoot", 2: "Removable",
    3: "Local", 4: "Network", 5: "CDROM", 6: "RAMDisk",
}


def _reg_read_dword(key: str, name: str, default: int) -> Optional[int]:
    r"""读一个 REG_DWORD; 未设置返回 None (让上层区分'默认'和'显式设为默认值')."""
    if os.name != "nt":
        return None
    try:
        import winreg  # type: ignore
    except ImportError:
        return None
    try:
        hive_name, sub = key.split("\\", 1)
        hive_map = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}
        hive = hive_map.get(hive_name)
        if hive is None:
            return None
        with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ) as h:
            val, _typ = winreg.QueryValueEx(h, name)
            return int(val)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    except Exception:
        return None


def _run_win_cmd(cmd: str, timeout: int = 4) -> tuple[str, int]:
    r"""跑 Windows shell 命令拿 stdout+stderr. 静默不弹窗."""
    if os.name != "nt":
        return ("", -1)
    try:
        r = subprocess.run(
            cmd, shell=True, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        out = r.stdout.decode("gbk", errors="replace") if r.stdout else ""
        return (out, r.returncode)
    except subprocess.TimeoutExpired:
        return (f"[TIMEOUT after {timeout}s]", -1)
    except Exception as e:
        return (f"[EXCEPTION {type(e).__name__}: {e}]", -1)


def _enum_drives() -> list[DriveInfo]:
    r"""枚举所有逻辑盘符. Windows only."""
    if os.name != "nt":
        return []
    drives: list[DriveInfo] = []
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # 1. 拿盘符列表 (bitmask)
        mask = k32.GetLogicalDrives()
        letters = []
        for i in range(26):
            if mask & (1 << i):
                letters.append(chr(ord("A") + i) + ":")
        # 2. 每个盘查 type / free space / volume info
        for letter in letters:
            di = DriveInfo(letter=letter)
            root = letter + "\\"
            try:
                dt = int(k32.GetDriveTypeW(root))
                di.drive_type = dt
                di.drive_type_name = _DRIVE_TYPES.get(dt, "Unknown")
            except Exception:
                pass
            # Free / Total (仅本地/网络盘, 光驱可能空盘)
            if di.drive_type in (3, 4):
                try:
                    free_avail = ctypes.c_ulonglong(0)
                    total_bytes = ctypes.c_ulonglong(0)
                    total_free = ctypes.c_ulonglong(0)
                    if k32.GetDiskFreeSpaceExW(root,
                                               ctypes.byref(free_avail),
                                               ctypes.byref(total_bytes),
                                               ctypes.byref(total_free)):
                        # 语义: free_bytes = 当前用户还能写多少 (avail_to_caller)
                        #       total_bytes = 磁盘/卷的总容量
                        di.free_bytes = free_avail.value
                        di.total_bytes = total_bytes.value
                except Exception:
                    pass
                try:
                    vol_buf = ctypes.create_unicode_buffer(261)
                    fs_buf = ctypes.create_unicode_buffer(261)
                    if k32.GetVolumeInformationW(
                        root, vol_buf, 260, None, None, None, fs_buf, 260,
                    ):
                        di.volume_name = vol_buf.value
                        di.file_system = fs_buf.value
                except Exception:
                    pass
            # 若网络盘, 探测 UNC provider
            if di.drive_type == 4:
                try:
                    from winpath_util import resolve_mapped_drive_to_unc_verbose
                    rc, unc = resolve_mapped_drive_to_unc_verbose(letter)
                    if rc == 0 and unc:
                        di.provider_name = unc
                except Exception:
                    pass
            drives.append(di)
    except Exception:
        pass
    return drives


def _hostname_is_samba(host: str) -> str:
    r"""启发式判服务端是不是 Samba (仅根据 hostname 猜, 拿不到就 Unknown).

    真实判定要用 SmbConnection dialect + 服务端 vendor string,
    但普通用户拒绝访问, 只能启发式.
    """
    if not host:
        return "Unknown"
    h = host.lower()
    # 已知 Linux/Samba 命名习惯
    if any(k in h for k in ("filestor", "linux", "storage", "nas", "netapp")):
        return "Samba/Linux (启发式)"
    if any(k in h for k in ("wfs", "winfs", "dfs")):
        return "Windows Server (启发式)"
    return "Unknown"


def _enum_net_mounts(drives: list[DriveInfo]) -> list[NetMount]:
    r"""归纳所有网络挂载: 映射盘 + Network Shortcuts.

    尝试拿 SMB dialect (Get-SmbConnection), 拿不到留空.
    """
    mounts: list[NetMount] = []
    if os.name != "nt":
        return mounts

    # 1) 映射盘
    for d in drives:
        if d.drive_type == 4 and d.provider_name:
            m = NetMount(kind="mapped_drive",
                         drive=d.letter,
                         unc=d.provider_name)
            # 尝试拿服务端 hostname
            unc = d.provider_name.lstrip("\\")
            server = unc.split("\\", 1)[0] if "\\" in unc else unc
            m.server_hint = _hostname_is_samba(server)
            mounts.append(m)

    # 2) Network Shortcuts (Explorer UI 层)
    ns = os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Network Shortcuts")
    if os.path.isdir(ns):
        try:
            for item in os.listdir(ns):
                # 从名字里抓服务端提示
                hint = "Unknown"
                if "samba" in item.lower():
                    hint = "Samba Server (Explorer 明示)"
                elif "windows" in item.lower() and "server" in item.lower():
                    hint = "Windows Server (Explorer 明示)"
                m = NetMount(kind="network_shortcut",
                             drive="",
                             unc=item,   # UI 里的显示名, 不是真 UNC
                             server_hint=hint)
                mounts.append(m)
        except OSError:
            pass

    # 3) 尝试拿 SMB dialect (可能因权限失败, 静默)
    out, rc = _run_win_cmd(
        'powershell -NoProfile -Command '
        '"Get-SmbConnection | ForEach-Object { $_.ServerName + \'|\' + $_.Dialect }"',
        timeout=6,
    )
    if rc == 0 and out and "|" in out and "拒绝访问" not in out:
        for line in out.splitlines():
            s = line.strip()
            if "|" in s:
                server, dialect = s.split("|", 1)
                for m in mounts:
                    if server.lower() in (m.unc or "").lower():
                        m.smb_dialect = dialect.strip()

    return mounts


def _detect_mount_kind(drives: list[DriveInfo], mounts: list[NetMount]) -> str:
    r"""判定主要挂载方式."""
    if os.name != "nt":
        return "local"
    if any(m.kind == "mapped_drive" for m in mounts):
        return "mapped_drive"
    if any(m.kind == "network_shortcut" for m in mounts):
        return "unc_direct"
    return "local"


def _detect_samba(mounts: list[NetMount]) -> bool:
    r"""启发式判 Samba (任一 mount 带 Samba/Linux hint 即认为 Samba)."""
    for m in mounts:
        if "samba" in m.server_hint.lower() or "linux" in m.server_hint.lower():
            return True
    return False


def _detect_long_paths_enabled() -> bool:
    if os.name != "nt":
        return True
    val = _reg_read_dword(
        r"HKLM\SYSTEM\CurrentControlSet\Control\FileSystem",
        "LongPathsEnabled", 0,
    )
    return val == 1


# --------------- 主入口 ---------------


_ENV_CACHE: Optional[Env] = None
_ENV_LOCK = threading.Lock()


def probe_env(force: bool = False) -> Env:
    r"""探测运行环境画像. 结果缓存到进程单例, 反复调用零开销."""
    global _ENV_CACHE
    with _ENV_LOCK:
        if _ENV_CACHE is not None and not force:
            return _ENV_CACHE
        t0 = time.time()
        env = Env()
        env.diag_errors = []

        # ---- 主机 ----
        env.platform = os.name
        try:
            env.platform_full = platform.platform()
        except Exception as e:
            env.diag_errors.append(f"platform.platform: {type(e).__name__}: {e}")
        try:
            env.hostname = socket.gethostname()
        except Exception as e:
            env.diag_errors.append(f"gethostname: {type(e).__name__}: {e}")
        try:
            env.username = getpass.getuser()
        except Exception as e:
            env.diag_errors.append(f"getuser: {type(e).__name__}: {e}")
        env.python_version = sys.version.split()[0]
        env.pid = os.getpid()

        # ---- Windows 专属 ----
        if os.name == "nt":
            try:
                env.drives = _enum_drives()
            except Exception as e:
                env.diag_errors.append(f"_enum_drives: {type(e).__name__}: {e}")
            try:
                env.net_mounts = _enum_net_mounts(env.drives)
            except Exception as e:
                env.diag_errors.append(f"_enum_net_mounts: {type(e).__name__}: {e}")
            env.mount_kind = _detect_mount_kind(env.drives, env.net_mounts)
            env.server_is_samba = _detect_samba(env.net_mounts)
            env.long_paths_enabled = _detect_long_paths_enabled()

            # SMB 缓存注册表
            reg_key = r"HKLM\SYSTEM\CurrentControlSet\Services\LanmanWorkstation\Parameters"
            v = _reg_read_dword(reg_key, "DirectoryCacheLifetime", 10)
            env.smb_dir_cache_secs = 10 if v is None else v
            v = _reg_read_dword(reg_key, "FileNotFoundCacheLifetime", 5)
            env.smb_file_notfound_cache_secs = 5 if v is None else v
            v = _reg_read_dword(reg_key, "FileInfoCacheLifetime", 10)
            env.smb_file_info_cache_secs = 10 if v is None else v
            v = _reg_read_dword(reg_key, "DormantFileLimit", -1)
            env.smb_dormant_file_limit = -1 if v is None else v
            v = _reg_read_dword(reg_key, "DisableBandwidthThrottling", -1)
            env.smb_disable_bandwidth_throttling = -1 if v is None else v
            v = _reg_read_dword(reg_key, "DisableLargeMtu", -1)
            env.smb_disable_large_mtu = -1 if v is None else v

            env.long_prefix_needed = not env.long_paths_enabled
        else:
            env.mount_kind = "local"
            env.long_paths_enabled = True
            env.long_prefix_needed = False

        env.probe_time_ms = int((time.time() - t0) * 1000)
        _ENV_CACHE = env
        return env


def get_env() -> Env:
    return probe_env(force=False)


def probe_and_log(logger=None,
                  probe_paths: list[str] | None = None) -> Env:
    r"""GUI 启动 hook: 探测环境 + 打印**多行**画像.

    参数:
      logger: 可 callable(str) (如 self._log) / logging.Logger / None (stderr)
      probe_paths: 额外要探测可达性的路径 (例如 [args.src_root, args.dst_root, args.markers_root])

    返回值: Env 实例.
    """
    env = probe_env(force=False)
    lines = env.full_report(probe_paths=probe_paths)
    _emit_lines(lines, logger)
    return env


def _emit_lines(lines: list[str], logger) -> None:
    if logger is None:
        for ln in lines:
            sys.stderr.write(ln + "\n")
        sys.stderr.flush()
    elif callable(logger):
        for ln in lines:
            try:
                logger(ln)
            except Exception:
                sys.stderr.write(ln + "\n")
    elif hasattr(logger, "info"):
        for ln in lines:
            try:
                logger.info(ln)
            except Exception:
                sys.stderr.write(ln + "\n")
    else:
        for ln in lines:
            sys.stderr.write(ln + "\n")


def should_do_samba_retry() -> bool:
    r"""判定当前环境是否需要"假 miss 时 sleep 后 retry"."""
    env = get_env()
    return env.server_is_samba or env.mount_kind == "unc_direct"


def samba_retry_wait_secs() -> float:
    r"""假 miss retry 前 sleep 多少秒."""
    env = get_env()
    if not should_do_samba_retry():
        return 0.0
    return float(env.smb_dir_cache_secs) + 1.0
