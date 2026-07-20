# -*- coding: utf-8 -*-
"""
gui_log_util.py —— 4 个 GUI (extract_gui / dedupe_gui / classify_gui /
stats_viewer_gui) 共用的日志落盘 + 工具条 + preload 上次 tail 工具集.

v0.4.87 抽出公共实现, 避免 4 份重复代码.

设计要点 (2026-07-20 跟用户对齐):
- 每次 GUI 启动新建 log 文件, 存
  `~/.pic-clear/gui_logs/<gui_name>/<gui_name>_YYYY-MM-DD_HHMMSS.log`
- **不做保留策略, 不物理删除历史 log** (用户明确要求, 由用户自己清理)
- **启动时 Tab preload 上次日志末尾 200 行** + 分隔线 (D2 模式)
- **"清空日志(Tab)" 按钮**只清 Tab 显示, 不动磁盘文件
- **"当前日志"** 在 Tab 顶部作为灰色小字显示 (4 个 GUI 全要)

设计边界:
- **Text 组件由 GUI 自己创建**, 保留 extract_gui / dedupe_gui 的
  "智能滚动" 精细 hook (公共模块不接管).
- 公共模块负责: 落盘 (线程安全) + queue + 工具条按钮 + preload helper.
- GUI 在 `_build_log_tab` 里:
    1. 调 `ctl.build_toolbar(parent)` 建顶部按钮条
    2. 自己建 Text + Scrollbar (原来怎么写就怎么写)
    3. 调 `ctl.attach_text(text)` 把 Text 绑给控制器
    4. 调 `ctl.preload_prev_tail_to_text()` 灌入上次末尾内容
- 主循环 `after` 里定期调 `ctl.pump()` 拉 queue 灌 Text.

历史目录兼容:
  extract_gui 老日志目录 `~/.pic-clear/extract_gui_logs/`.
  迁移到公共模块后新目录 `~/.pic-clear/gui_logs/extract_gui/`, 老日志
  首次找不到时会从 legacy_dir 兜底找一次, 见 `latest_prev_log`.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk
except Exception:  # 无 GUI 环境测试导入不炸
    tk = None  # type: ignore
    ttk = None  # type: ignore


# -------------------------------- 路径 -------------------------------------

def gui_log_root() -> Path:
    """所有 GUI 落盘日志的公共根: ~/.pic-clear/gui_logs/"""
    return Path(os.path.expanduser("~")) / ".pic-clear" / "gui_logs"


def gui_log_dir(app_name: str) -> Path:
    """单个 GUI 的子目录: ~/.pic-clear/gui_logs/<app_name>/"""
    return gui_log_root() / app_name


def new_log_path(app_name: str) -> Path:
    """
    本次启动的 log 文件路径.

    命名格式: `<app_name>_YYYY-MM-DD_HHMMSS.log`
    - 精确到秒, 避免 1 分钟内重启撞名
    - 用 `-` 分隔日期, 更易读
    """
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    d = gui_log_dir(app_name)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{app_name}_{ts}.log"


def latest_prev_log(app_name: str, exclude: Path,
                    legacy_dir: Path | None = None,
                    legacy_pattern: str | None = None) -> Path | None:
    """
    找上次留下的最新日志文件 (时间戳最大, 排除本次刚建的).

    参数:
      legacy_dir / legacy_pattern: 迁移场景兜底. 新目录没历史时,
        从旧目录里再找一次.
        例: extract_gui 迁移前是 `~/.pic-clear/extract_gui_logs/extract_gui_*.log`,
        迁移后新目录空时用它拿老 tail preload.
    """
    d = gui_log_dir(app_name)
    if d.is_dir():
        candidates = sorted(
            (p for p in d.glob(f"{app_name}_*.log") if p != exclude),
            key=lambda p: p.name,
            reverse=True,
        )
        if candidates:
            return candidates[0]
    # legacy 兜底
    if legacy_dir is not None and legacy_pattern is not None and legacy_dir.is_dir():
        legacy_cands = sorted(legacy_dir.glob(legacy_pattern),
                              key=lambda p: p.name, reverse=True)
        if legacy_cands:
            return legacy_cands[0]
    return None


def tail_lines(path: Path, n: int = 200) -> list[str]:
    """读文件末尾 n 行; 文件不大直接 readlines, 够用."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return []
    return lines[-n:] if len(lines) > n else lines


def open_log_dir(app_name: str) -> None:
    """跨平台打开日志目录 (Windows/Mac/Linux)."""
    d = gui_log_dir(app_name)
    d.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            os.startfile(str(d))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(d)])
        else:
            subprocess.Popen(["xdg-open", str(d)])
    except Exception as e:
        sys.stderr.write(f"[gui_log_util] 打开日志目录失败: {e}\n")


# ------------------------------- 控制器 -------------------------------------

class GuiLogController:
    """
    日志控制器: 落盘 + queue + 工具条按钮 + preload helper.

    Text 组件由 GUI 自己建, 通过 `attach_text` 绑给控制器.
    这样每个 GUI 的智能滚动 / 事件绑定行为不受公共模块干扰.

    典型用法:
        self._log_ctl = GuiLogController(
            app_name="extract_gui",
            legacy_dir=Path("~/.pic-clear/extract_gui_logs").expanduser(),
            legacy_pattern="extract_gui_*.log",
        )

        def _build_log_tab(self, page):
            self._log_ctl.build_toolbar(page,
                extra_toolbar=lambda tb: ttk.Checkbutton(
                    tb, text="自动滚到底",
                    variable=self._auto_scroll_var).pack(side="left"))
            # ... 自建 Text + Scrollbar ...
            self._log_ctl.attach_text(self._log_text)
            self._log_ctl.preload_prev_tail_to_text()

        def _log(self, msg): self._log_ctl.log(msg)
        # tk after 循环里:
        def _drain(self):
            if self._log_ctl.pump() and self._auto_scroll_var.get():
                self._log_text.see("end")
            self.root.after(200, self._drain)
    """

    def __init__(self, app_name: str, *,
                 tail_preload: int = 200,
                 legacy_dir: Path | None = None,
                 legacy_pattern: str | None = None) -> None:
        self.app_name = app_name
        self.tail_preload = tail_preload
        self.legacy_dir = legacy_dir
        self.legacy_pattern = legacy_pattern

        self.log_path: Path = new_log_path(app_name)
        try:
            self._fp = self.log_path.open("a", encoding="utf-8")
        except Exception as e:
            sys.stderr.write(
                f"[gui_log_util] 打开日志文件失败 {self.log_path}: {e}\n"
            )
            self._fp = None
        self._fp_lock = threading.Lock()

        self._queue: "queue.Queue[str]" = queue.Queue()
        self._text: "tk.Text | None" = None

    # ---------- 日志写入 ----------

    def log(self, msg: str) -> None:
        """
        线程安全: 加时间戳 -> 同步 append 到磁盘 -> 塞 queue 供主线程刷 UI.

        - 传进来的 msg 不要自带换行, 内部会补 `\\n`.
        - 时间戳格式统一 `YYYY-MM-DD HH:MM:SS`, 跟 dedupe_gui v0.4.86 对齐.
        """
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self._write_disk(line)
        self._queue.put(line)

    def raw_put(self, chunk: str, *, to_disk: bool = False) -> None:
        """
        直接把原始 chunk (可能多行, 可能不带时间戳) 塞进 queue.

        to_disk=False (默认): 只 UI 显示不落盘. preload 上次 tail 是这个场景.
        to_disk=True: UI 显示且落盘. 一般用不到.
        """
        if to_disk:
            self._write_disk(chunk)
        self._queue.put(chunk)

    def _write_disk(self, chunk: str) -> None:
        if self._fp is not None:
            try:
                with self._fp_lock:
                    self._fp.write(chunk)
                    self._fp.flush()
            except Exception:
                pass

    # ---------- Text 绑定 ----------

    def attach_text(self, text) -> None:
        """把 GUI 自建的 Text 组件绑给控制器, 供 pump / clear / preload 用."""
        self._text = text

    def pump(self) -> bool:
        """
        主线程周期性调用: 把 queue 里的行刷进已 attach 的 Text.

        返回: 本次是否有新内容 (方便 GUI 决定是否触发自动滚).
        """
        if self._text is None:
            return False
        appended = False
        try:
            while True:
                line = self._queue.get_nowait()
                self._text.config(state="normal")
                self._text.insert("end", line)
                self._text.config(state="disabled")
                appended = True
        except queue.Empty:
            pass
        return appended

    def clear_tab(self) -> None:
        """
        只清 Tab 显示, 不动磁盘文件 (v0.4.87 用户硬要求: 不物理删除).
        """
        if self._text is None:
            return
        self._text.config(state="normal")
        self._text.delete("1.0", "end")
        self._text.config(state="disabled")

    def preload_prev_tail_to_text(self) -> None:
        """
        启动时把上次日志末尾 tail_preload 行灌进已 attach 的 Text + 分隔线.

        - 首次跑或没历史 -> 静默跳过, 不打分隔线.
        - 只 UI 显示, 不写回本次 log 文件.
        - GUI 侧一般在 `_build_log_tab` 尾部 `attach_text` 之后调一次.
        """
        if self._text is None:
            return
        prev = latest_prev_log(
            self.app_name, exclude=self.log_path,
            legacy_dir=self.legacy_dir, legacy_pattern=self.legacy_pattern,
        )
        if prev is None:
            return
        lines = tail_lines(prev, self.tail_preload)
        if not lines:
            return
        self._text.config(state="normal")
        self._text.insert(
            "end",
            f"===== 上次日志 tail {self.tail_preload} 行：{prev.name} =====\n",
        )
        for line in lines:
            self._text.insert("end", line)
        self._text.insert(
            "end",
            f"===== 上次日志结束 · 当前日志：{self.log_path.name} =====\n",
        )
        self._text.see("end")
        self._text.config(state="disabled")

    # ---------- 工具条 ----------

    def build_toolbar(self, parent, *,
                      show_open_dir: bool = True,
                      extra_toolbar=None) -> "ttk.Frame":
        """
        铺日志 Tab 顶部工具条: [清空日志(Tab)] [打开日志文件夹] + 当前日志文件名 label.

        参数:
          show_open_dir: 是否显示"打开日志文件夹"按钮.
          extra_toolbar: 回调 `(toolbar_frame) -> None`, GUI 在这里往
              工具条追加自己的按钮 / 复选框 (例如"自动滚到底", "跳到底部").

        返回: 工具条 Frame, 方便 GUI 后续继续 pack 别的东西.
        """
        assert tk is not None and ttk is not None, "tkinter 不可用"
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill="x", padx=6, pady=4)

        ttk.Button(toolbar, text="清空日志(Tab)",
                   command=self.clear_tab).pack(side="left", padx=4)
        if show_open_dir:
            ttk.Button(toolbar, text="打开日志文件夹",
                       command=lambda: open_log_dir(self.app_name)
                       ).pack(side="left", padx=4)

        if extra_toolbar is not None:
            try:
                extra_toolbar(toolbar)
            except Exception as e:
                sys.stderr.write(
                    f"[gui_log_util] extra_toolbar 回调失败: {e}\n"
                )

        ttk.Label(toolbar, text=f"当前日志：{self.log_path.name}",
                  foreground="#888").pack(side="left", padx=8)
        return toolbar

    # ---------- 收尾 ----------

    def close(self) -> None:
        """GUI 退出时可选调, 关掉文件句柄. 不调也行 (进程退出会关)."""
        if self._fp is not None:
            try:
                with self._fp_lock:
                    self._fp.close()
            except Exception:
                pass
            self._fp = None
