"""child_exe_ver.py —— 子 exe 版本探测.

背景 & 硬规则详见 docs/child_exe_ver.md.

一句话: 之前多次踩坑 "GUI 是新版, 但它调用的干活 exe (extract_frames.exe /
dedupe_pic.exe) 还是老版, 里面的 marker/长路径修复没进来, 用户以为升级了
其实没升级". 解决办法: **GUI 启动时**跑一次 <子 exe> --version, 把版本 +
路径 + 与 GUI 版本的一致性 打到日志开头 [CORE] 段, 用户一眼能看出来.

对外 API:
    probe_child_exe(exe_path, gui_version="") -> ChildExeInfo
    probe_and_log(logger, *, exe_finder, exe_name, gui_version)

非 Windows 平台不特殊化, 走同一套 subprocess (Mac/Linux 上一般跑不到
干活 exe, 但也不出错).

**硬规则**: 本模块 **不**进 pyarmor gen, 明文常量, 跟 env_probe.py 同规格.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Optional


# --------------- 数据结构 ---------------


@dataclass
class ChildExeInfo:
    r"""单次子 exe 探测结果."""
    exe_name: str = ""       # 逻辑名: "extract_frames.exe"
    exe_path: str = ""       # 找到的绝对路径; 空字符串 = 没找到
    version: str = ""        # 从 --version 拿到的版本字符串 (一行)
    gui_version: str = ""    # 传进来的 GUI 版本, 便于对比
    matches_gui: bool = True # 是否一致
    error: str = ""          # 探测失败原因; 空 = 成功


_VER_RE = re.compile(r"v?\d+\.\d+\.\d+(?:[.\-][A-Za-z0-9]+)*")


def _extract_ver(text: str) -> str:
    r"""从一行 --version 输出里抽出 vX.Y.Z 形式的版本号.

    输入 "extract_frames v0.4.75" -> "v0.4.75"
    输入 "v0.4.75"                -> "v0.4.75"
    输入 "dev"                    -> ""
    """
    m = _VER_RE.search(text or "")
    return m.group(0) if m else ""


def probe_child_exe(exe_path: Optional[str],
                    gui_version: str = "",
                    *,
                    exe_name: str = "",
                    timeout_secs: float = 15.0) -> ChildExeInfo:
    r"""跑 <exe_path> --version, 拿版本行.

    exe_path=None 或找不到, 返回 error="not_found" 的 ChildExeInfo.
    """
    info = ChildExeInfo(exe_name=exe_name, gui_version=gui_version)
    if not exe_path:
        info.error = "not_found"
        info.matches_gui = False
        return info
    info.exe_path = str(exe_path)
    if not os.path.exists(info.exe_path):
        info.error = f"path_not_exists: {info.exe_path}"
        info.matches_gui = False
        return info

    creationflags = 0
    if os.name == "nt":
        # CREATE_NO_WINDOW, 避免 exe 弹一下黑框
        creationflags = 0x08000000

    try:
        result = subprocess.run(
            [info.exe_path, "--version"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout_secs,
            creationflags=creationflags,
        )
    except FileNotFoundError:
        info.error = "file_not_found_on_exec"
        info.matches_gui = False
        return info
    except subprocess.TimeoutExpired:
        info.error = f"timeout_{timeout_secs:.0f}s"
        info.matches_gui = False
        return info
    except Exception as e:
        info.error = f"{type(e).__name__}: {e}"
        info.matches_gui = False
        return info

    out = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0 and not out:
        info.error = f"exit {result.returncode}, no_output"
        info.matches_gui = False
        return info
    info.version = out.splitlines()[0] if out else "(无输出)"

    # 版本一致性判定: 从 GUI/子 exe 各抽一个 vX.Y.Z 出来对比
    gui_ver = _extract_ver(gui_version)
    child_ver = _extract_ver(info.version)
    if not gui_ver or not child_ver:
        # 有 dev / branch-shaXXXX 场景, 不做严格判定, 只做展示
        info.matches_gui = True
    else:
        info.matches_gui = (gui_ver == child_ver)
    return info


def probe_and_log(logger,
                  *,
                  exe_finder: Callable[[], Optional[str]],
                  exe_name: str,
                  gui_version: str = "") -> ChildExeInfo:
    r"""GUI 启动 hook: 探测子 exe 版本 + 打 [CORE] 多行日志.

    参数:
      logger: 可 callable(str) / logging.Logger / None (走 stderr)
      exe_finder: 无参函数, 返回 exe 绝对路径 str/Path 或 None
      exe_name: 逻辑名, 例如 "extract_frames.exe" (打日志用)
      gui_version: GUI 自己的版本号 (APP_VERSION), 用于一致性对比

    返回: ChildExeInfo (调用方一般用不到, 只是拿来做后续判定的兜底).
    """
    exe_path: Optional[str] = None
    finder_err = ""
    try:
        exe_path = exe_finder()
    except Exception as e:
        finder_err = f"{type(e).__name__}: {e}"

    info = probe_child_exe(exe_path, gui_version, exe_name=exe_name)
    if finder_err and not info.error:
        info.error = f"finder_error: {finder_err}"
        info.matches_gui = False

    _emit_lines(_format_report(info), logger)
    return info


def _format_report(info: ChildExeInfo) -> list[str]:
    lines: list[str] = []
    lines.append("=" * 68)
    lines.append(f"[CORE] {info.exe_name} 版本探测")
    lines.append("=" * 68)
    if info.error == "not_found":
        lines.append(f"[CORE]   路径: (未找到 {info.exe_name})")
        lines.append(f"[CORE]   版本: N/A")
        lines.append("[CORE]   ✘ 缺失内核 exe, 请把它放到 GUI 同目录 / System32 / PATH 后重启")
        lines.append("=" * 68)
        return lines
    if info.error:
        lines.append(f"[CORE]   路径: {info.exe_path or '(空)'}")
        lines.append(f"[CORE]   版本: (探测失败)")
        lines.append(f"[CORE]   错误: {info.error}")
        lines.append("=" * 68)
        return lines
    lines.append(f"[CORE]   路径: {info.exe_path}")
    lines.append(f"[CORE]   版本: {info.version}")
    if info.gui_version:
        if info.matches_gui:
            lines.append(f"[CORE]   一致性: ✓ (跟 GUI {info.gui_version} 一致)")
        else:
            lines.append(
                f"[CORE]   一致性: ⚠ GUI 版本 {info.gui_version} vs 子 exe 版本 "
                f"{_extract_ver(info.version) or info.version}, **子 exe 落后, 请更新!**"
            )
    lines.append("=" * 68)
    return lines


def _emit_lines(lines: list[str], logger) -> None:
    if logger is None:
        for ln in lines:
            sys.stderr.write(ln + "\n")
        sys.stderr.flush()
        return
    if callable(logger):
        for ln in lines:
            try:
                logger(ln)
            except Exception:
                sys.stderr.write(ln + "\n")
        return
    if hasattr(logger, "info"):
        for ln in lines:
            try:
                logger.info(ln)
            except Exception:
                sys.stderr.write(ln + "\n")
        return
    for ln in lines:
        sys.stderr.write(ln + "\n")
