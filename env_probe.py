r"""env_probe.py —— 运行环境自动探测.

背景 & 硬规则详见 docs/env_probe.md.

一句话: 上游反复改磁盘挂载 (Z: 映射盘 / \\filestor UNC 直连 / 网络位置 / Samba 服务端 /
LongPath 开关), 每次改一次我们下游软件就要修一次. 与其反复打补丁, 不如**运行时探测**
环境画像, 让代码按画像挑策略.

所有新增 GUI (extract_gui / dedupe_gui / classify_gui / pipe_gui / ...) 启动时
调用 probe_and_log() 一行搞定, 日志开头就有环境画像, 排查零成本.

对外 API:
    probe_env() -> Env             # 结构化环境画像 (纯读, 无副作用)
    probe_and_log(logger=None)     # 探测 + 打印到 logger 或 stderr, 供 GUI 启动 hook
    get_env() -> Env               # 单例, 首次调用触发探测, 后续复用缓存

Env 字段:
    mount_kind      : 'unc_direct' / 'mapped_drive' / 'local' / 'unknown'
    server_is_samba : bool  (启发式判断, 用于开关 Samba 缓存 retry)
    long_paths_enabled : bool  (Windows 10+ 全局长路径开关)
    smb_dir_cache_secs : int   (SMB Directory Cache Lifetime, 默认 10)
    long_prefix_needed : bool  (>=180 字符路径需要 \\?\ 前缀)
    platform        : 'nt' / 'posix'
    probe_time_ms   : int   (探测耗时)

非 Windows 平台 (Mac / Linux) 上, 所有探测原样退回默认值,
零回归、零性能损失.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional


# --------------- 数据结构 ---------------


@dataclass
class Env:
    r"""运行环境画像. 字段全部只读."""
    platform: str = "posix"
    mount_kind: str = "local"
    server_is_samba: bool = False
    long_paths_enabled: bool = False
    smb_dir_cache_secs: int = 10
    smb_file_notfound_cache_secs: int = 5
    smb_file_info_cache_secs: int = 10
    long_prefix_needed: bool = False
    probe_time_ms: int = 0
    # 原始诊断字符串, 打日志用
    diag: dict = field(default_factory=dict)

    def summary_line(self) -> str:
        r"""一行画像, 打日志开头."""
        return (
            f"[ENV] platform={self.platform} "
            f"mount={self.mount_kind} "
            f"samba={'yes' if self.server_is_samba else 'no'} "
            f"long_prefix={'need' if self.long_prefix_needed else 'no'} "
            f"long_paths_enabled={'yes' if self.long_paths_enabled else 'no'} "
            f"dir_cache={self.smb_dir_cache_secs}s "
            f"probe={self.probe_time_ms}ms"
        )


# --------------- 内部工具 ---------------


def _reg_read_dword(key: str, name: str, default: int) -> int:
    r"""读一个 REG_DWORD, 读不到用默认值. 只读, 不写."""
    if os.name != "nt":
        return default
    try:
        import winreg  # type: ignore
    except ImportError:
        return default
    try:
        # key 形如 HKLM\SYSTEM\CurrentControlSet\Services\...
        hive_name, sub = key.split("\\", 1)
        hive_map = {
            "HKLM": winreg.HKEY_LOCAL_MACHINE,
            "HKCU": winreg.HKEY_CURRENT_USER,
        }
        hive = hive_map.get(hive_name)
        if hive is None:
            return default
        with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ) as h:
            val, _typ = winreg.QueryValueEx(h, name)
            return int(val)
    except OSError:
        return default
    except Exception:
        return default


def _detect_network_shortcuts_has_samba() -> bool:
    r"""看 Network Shortcuts 里是否有 'Samba Server' 提示 (Explorer UI 层证据)."""
    if os.name != "nt":
        return False
    try:
        ns = os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Network Shortcuts")
        if not os.path.isdir(ns):
            return False
        for item in os.listdir(ns):
            if "samba" in item.lower():
                return True
    except OSError:
        return False
    return False


def _detect_mount_kind() -> str:
    r"""判断挂载方式: local / mapped_drive / unc_direct.

    简化启发:
      - 有 net use 映射到 \\...\ 的盘符 -> mapped_drive
      - 无映射, 但 Network Shortcuts 有 UNC 快捷方式 -> unc_direct
      - 都没有 -> local
    """
    if os.name != "nt":
        return "local"
    # 有映射盘?
    try:
        r = subprocess.run(
            "net use", shell=True, timeout=4,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        out = r.stdout.decode("gbk", errors="replace") if r.stdout else ""
        for line in out.splitlines():
            s = line.strip()
            # 典型行: "OK           Z:        \\filestor01\share    ..."
            if "\\\\" in s and " " in s:
                # 有形如 X: 的盘符
                for tok in s.split():
                    if len(tok) == 2 and tok[1] == ":" and tok[0].isalpha():
                        return "mapped_drive"
    except Exception:
        pass
    # 网络位置有 UNC?
    if _detect_network_shortcuts_has_samba():
        return "unc_direct"
    ns = os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Network Shortcuts")
    if os.path.isdir(ns):
        try:
            if os.listdir(ns):
                return "unc_direct"
        except OSError:
            pass
    return "local"


def _detect_samba_server() -> bool:
    r"""启发式判断服务端是不是 Samba.

    证据 (任一即认为是 Samba):
      1) Explorer Network Shortcuts 里明示 'Samba Server'
      2) TODO: SMB dialect 协商结果 (需 Get-SmbConnection, 但普通用户拒绝访问)

    宁可误报 Samba (多做 retry, 慢一点), 不可漏报 (漏报导致 marker miss 重抽).
    """
    if os.name != "nt":
        return False
    return _detect_network_shortcuts_has_samba()


def _detect_long_paths_enabled() -> bool:
    r"""Windows 10+ 全局长路径开关 (LongPathsEnabled)."""
    if os.name != "nt":
        return True  # POSIX 无 MAX_PATH 限制, 视为已启用
    val = _reg_read_dword(
        r"HKLM\SYSTEM\CurrentControlSet\Control\FileSystem",
        "LongPathsEnabled",
        0,
    )
    return val == 1


def _detect_smb_cache(name: str, default: int) -> int:
    return _reg_read_dword(
        r"HKLM\SYSTEM\CurrentControlSet\Services\LanmanWorkstation\Parameters",
        name,
        default,
    )


# --------------- 主入口 ---------------


_ENV_CACHE: Optional[Env] = None
_ENV_LOCK = threading.Lock()


def probe_env(force: bool = False) -> Env:
    r"""探测运行环境画像. 结果缓存到进程单例, 反复调用零开销.

    force=True 时强制重新探测 (供 GUI '重新体检' 按钮).
    """
    global _ENV_CACHE
    with _ENV_LOCK:
        if _ENV_CACHE is not None and not force:
            return _ENV_CACHE
        t0 = time.time()
        env = Env()
        env.platform = os.name
        if os.name == "nt":
            env.mount_kind = _detect_mount_kind()
            env.server_is_samba = _detect_samba_server()
            env.long_paths_enabled = _detect_long_paths_enabled()
            env.smb_dir_cache_secs = _detect_smb_cache("DirectoryCacheLifetime", 10)
            env.smb_file_notfound_cache_secs = _detect_smb_cache(
                "FileNotFoundCacheLifetime", 5)
            env.smb_file_info_cache_secs = _detect_smb_cache(
                "FileInfoCacheLifetime", 10)
            env.long_prefix_needed = not env.long_paths_enabled
        else:
            env.mount_kind = "local"
            env.server_is_samba = False
            env.long_paths_enabled = True
            env.long_prefix_needed = False
        env.probe_time_ms = int((time.time() - t0) * 1000)
        _ENV_CACHE = env
        return env


def get_env() -> Env:
    r"""单例入口. 首次调用触发探测, 后续用缓存."""
    return probe_env(force=False)


def probe_and_log(logger=None) -> Env:
    r"""GUI 启动 hook: 探测环境 + 打印一行画像.

    参数:
      logger: 可 callable(str) (如 self._log) 或 logging.Logger (走 .info)
              None 时打到 stderr.

    返回值: Env 实例 (方便后续用).
    """
    env = probe_env(force=False)
    line = env.summary_line()
    if logger is None:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    elif callable(logger):
        try:
            logger(line)
        except Exception:
            sys.stderr.write(line + "\n")
    elif hasattr(logger, "info"):
        try:
            logger.info(line)
        except Exception:
            sys.stderr.write(line + "\n")
    else:
        sys.stderr.write(line + "\n")
    return env


def should_do_samba_retry() -> bool:
    r"""判定当前环境是否需要"假 miss 时 sleep 后 retry".

    仅 Samba + UNC 直连时开启; Windows Server 环境 / 本地盘一律关闭 (省时间).
    """
    env = get_env()
    return env.server_is_samba or env.mount_kind == "unc_direct"


def samba_retry_wait_secs() -> float:
    r"""假 miss retry 前 sleep 多少秒. 取 DirectoryCache + 1s 兜底."""
    env = get_env()
    # 只在需要时才 sleep; 不需要的返回 0
    if not should_do_samba_retry():
        return 0.0
    return float(env.smb_dir_cache_secs) + 1.0
