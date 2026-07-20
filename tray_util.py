# -*- coding: utf-8 -*-
"""
tray_util.py —— 4 个 GUI (extract_gui / dedupe_gui / classify_gui /
stats_viewer_gui) 共用的系统托盘 + 关闭最小化 + 全局快捷键 封装.

v0.4.88 抽出公共实现, 避免 4 份重复代码.

抽出前 extract_gui 和 dedupe_gui 里各自维护了几乎一模一样的
`hide_to_tray` / `show_main` / `quit_all` / `_start_tray_if_needed` /
`_register_hotkey` / `_maybe_show_close_hint`, 差异只有 3 处:
  - 图标 fallback 颜色 / 字母
  - 托盘 Icon 内部 name
  - quit_all 里要触发的 shutdown hook (worker_stop_flag / subproc_group /
    loop_stop_event ...)

设计要点:
- 每个 GUI 通过 TrayController 实例化, 传自己的 app_id / tooltip / 图标兜底
- shutdown_hooks 由 GUI 主动注入 callable, quit_all 里逐个跑
- 关闭时的"已最小化到托盘"提示弹窗 (可选, 通过 cfg get/set 记住"以后不再提示")
- 全局快捷键 keyboard 库注册, 缺库时 messagebox 提示不 crash
- pystray / Pillow / keyboard 全是**软依赖**, 缺任何一个只降级不 crash

用法 (最小接入):
    self._tray = TrayController(
        root=self.root,
        app_id="pic-clear-classify",
        tooltip="二次过滤工具",
        fallback_glyph=((155, 89, 182, 255), "CL"),
        hotkey_default="ctrl+alt+f",
        app_title="pic-clear 二次分类工具",  # 关闭提示弹窗标题用
        ui_scale=getattr(self.root, "__ui_scale__", 1.0),
    )
    self._tray.add_shutdown_hook(lambda: self._worker_stop_flag.set())
    self._tray.add_shutdown_hook(
        lambda: self._subproc_group.terminate_all(wait_timeout=2.0))
    self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        _save_config(...)
        if self._minimize_to_tray_var.get():
            self._tray.hide_to_tray()
            self._tray.maybe_show_close_hint(cfg_get=..., cfg_set=...)
        else:
            self._tray.quit_all()
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except Exception:  # 无 GUI 环境测试导入不炸
    tk = None  # type: ignore
    ttk = None  # type: ignore
    messagebox = None  # type: ignore


def _resource_path(name: str) -> str:
    """
    在 dev 模式和 PyInstaller onefile 打包后都能定位到资源文件.

    跟 pipe_gui._resource_path 语义一致, 但不依赖 pipe_gui, 让 stats_viewer 这种
    不 import pipe_gui 的 GUI 也能独立用 tray_util.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidate = os.path.join(base, name)
        if os.path.exists(candidate):
            return candidate
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, name)


# ------------------------------------------------------------------ 托盘

class TrayController:
    def __init__(
        self,
        root,
        *,
        app_id: str,
        tooltip: str,
        fallback_glyph: tuple = ((0, 102, 204, 255), "PC"),
        hotkey_default: str = "",
        app_title: str = "",
        ui_scale: float = 1.0,
    ) -> None:
        """
        参数:
          root: tk.Tk 主窗口.
          app_id: pystray 托盘图标的内部 name (Windows 通知区域用它区分实例),
              比如 "pic-clear-extract".
          tooltip: 鼠标 hover 托盘图标显示的文本. 按用户 v0.4.88 规范
              统一去掉 "pic-clear" 前缀, 4 个 GUI 分别是:
              切帧工具 / 去重工具 / 二次过滤工具 / 统计工具.
          fallback_glyph: 找不到 icon.png 时 Pillow 现画一个 64x64 图标兜底,
              (rgba, 2 字母) 二元组. rgba 是 RGBA 背景色, 字母写在中央.
          hotkey_default: 全局快捷键, 例如 "ctrl+alt+e"; 空则不注册快捷键.
              register_hotkey 里可以覆盖.
          app_title: 关闭提示弹窗 (`_maybe_show_close_hint`) 里作为标题使用,
              比如 "pic-clear 抽帧工具". 空则不显示.
          ui_scale: DPI 缩放比例 (从 root.__ui_scale__ 拿). 弹窗几何用.
        """
        self.root = root
        self.app_id = app_id
        self.tooltip = tooltip
        self.fallback_glyph = fallback_glyph
        self.hotkey_default = hotkey_default
        self.app_title = app_title
        self.ui_scale = ui_scale

        self._tray_icon = None
        self._tray_thread: "threading.Thread | None" = None
        self._hotkey_registered = False
        self._shutdown_hooks: list = []

    # ---------- 生命周期动作 ----------

    def add_shutdown_hook(self, fn) -> None:
        """
        注入退出时要跑的动作 (worker stop / subproc_group.terminate_all / ...).

        每个 GUI 自己业务不一样, 通过 hook 列表把差异塞进公共退出流程.
        quit_all 里会逐个 try 调这些回调, 单个异常不打断后续.
        """
        self._shutdown_hooks.append(fn)

    def hide_to_tray(self) -> None:
        self._start_tray_if_needed()
        try:
            self.root.withdraw()
        except Exception:
            pass

    def show_main(self) -> None:
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def quit_all(self) -> None:
        """
        真正退出. 顺序:
          1. 停托盘图标 (让 pystray.run() 那个线程退出)
          2. 反注册全局快捷键
          3. 逐个跑 shutdown_hooks (GUI 自己的 worker_stop / 子进程组 kill 等)
          4. root.destroy()
          5. 兜底 os._exit(0) (0.4s 后, 让 UI 有时间释放)
        """
        try:
            if self._tray_icon is not None:
                self._tray_icon.stop()
        except Exception:
            pass
        try:
            if self._hotkey_registered:
                import keyboard  # type: ignore
                keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        for hook in list(self._shutdown_hooks):
            try:
                hook()
            except Exception as e:
                # hook 挂了不能挡着后面的退出流程
                sys.stderr.write(
                    f"[tray_util] shutdown hook 失败: "
                    f"{type(e).__name__}: {e}\n"
                )
        try:
            self.root.destroy()
        except Exception:
            pass
        try:
            threading.Timer(0.4, lambda: os._exit(0)).start()
        except Exception:
            os._exit(0)

    # ---------- 托盘图标 ----------

    def _load_icon_image(self):
        """
        依次尝试:
          1. sys._MEIPASS / cwd 下的 icon.png
          2. Pillow 现画 fallback_glyph 兜底
        """
        try:
            from PIL import Image
        except Exception:
            return None
        try:
            icon_path = _resource_path("icon.png")
            if os.path.exists(icon_path):
                return Image.open(icon_path)
        except Exception:
            pass
        # fallback: 现画一个
        try:
            from PIL import Image, ImageDraw
            rgba, glyph = self.fallback_glyph
            img = Image.new("RGBA", (64, 64), rgba)
            d = ImageDraw.Draw(img)
            d.text((16, 20), glyph, fill="white")
            return img
        except Exception:
            return None

    def _start_tray_if_needed(self) -> None:
        if self._tray_icon is not None:
            return
        try:
            import pystray  # type: ignore
        except Exception as e:
            if messagebox is not None:
                messagebox.showwarning("托盘不可用",
                                       f"缺少 pystray：{e}")
            return
        img = self._load_icon_image()
        if img is None:
            if messagebox is not None:
                messagebox.showwarning("托盘不可用",
                                       "无法加载图标 (Pillow 缺失或 icon.png 不可用)")
            return

        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口",
                             lambda: self.root.after(0, self.show_main)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出",
                             lambda: self.root.after(0, self.quit_all)),
        )
        self._tray_icon = pystray.Icon(self.app_id, img, self.tooltip, menu)

        def _run_tray():
            try:
                self._tray_icon.run()  # type: ignore[union-attr]
            except Exception:
                pass

        self._tray_thread = threading.Thread(
            target=_run_tray, daemon=True, name=f"tray-{self.app_id}"
        )
        self._tray_thread.start()

    # ---------- 全局快捷键 ----------

    def register_hotkey(self, hotkey: str = "") -> tuple[bool, str]:
        """
        注册全局快捷键, 触发 show_main.

        返回 (成功?, 用户可见消息).
        缺 keyboard 库不 crash, 只返回 (False, "...").
        """
        hk = (hotkey or self.hotkey_default or "").strip()
        if not hk:
            return False, "未设置快捷键"
        try:
            import keyboard  # type: ignore
        except Exception as e:
            return False, f"缺少 keyboard 库：{e}"
        try:
            if self._hotkey_registered:
                keyboard.unhook_all_hotkeys()
            keyboard.add_hotkey(hk, lambda: self.root.after(0, self.show_main))
            self._hotkey_registered = True
            return True, f"快捷键 {hk} 已注册"
        except Exception as e:
            return False, f"注册失败: {type(e).__name__}: {e}"

    # ---------- 关闭提示弹窗 ----------

    def maybe_show_close_hint(
        self,
        *,
        cfg_get,           # callable() -> bool, 读"以后不再提示"
        cfg_set,           # callable(True) -> None, 写"以后不再提示"
        hotkey_display: str = "",
    ) -> None:
        """
        第一次最小化到托盘时弹一个说明窗口, 教用户"右下角图标 + 快捷键呼出".

        cfg_get / cfg_set 让 GUI 把"已勾选以后不再提示"存到自己的 config.

        - cfg_get() 返回 True 就直接跳过 (用户已勾过)
        - hotkey_display 用于弹窗里显示当前快捷键 (可选)
        """
        if tk is None or ttk is None:
            return
        try:
            if cfg_get():
                return
        except Exception:
            pass
        try:
            top = tk.Toplevel(self.root)
        except Exception:
            return
        title = f"{self.app_title} 已最小化到托盘" if self.app_title \
            else f"{self.tooltip} 已最小化到托盘"
        top.title(title)
        try:
            top.transient(self.root)
        except Exception:
            pass
        try:
            top.attributes("-topmost", True)
        except Exception:
            pass
        scale = self.ui_scale or 1.0
        try:
            w = int(520 * scale)
            h = int(260 * scale)
            top.geometry(f"{w}x{h}")
            top.resizable(False, False)
        except Exception:
            pass

        try:
            tk.Label(top, text="ⓘ  程序已最小化到系统托盘",
                     font=("Microsoft YaHei", 14, "bold"),
                     foreground="#0066cc").pack(pady=(18, 6))
            hk_line = ""
            if hotkey_display:
                hk_line = f"• 快捷键 {hotkey_display} 可呼出主窗口\n"
            tk.Label(top, text=(
                "点右上角 × 只是把窗口收起来了，程序仍在后台运行。\n\n"
                "• 通知区域（时间旁边）有本程序图标，右键 → 退出\n"
                f"{hk_line}\n"
                "不想最小化，请取消勾选『关闭时最小化到托盘』。"),
                font=("Microsoft YaHei", 10), justify="left",
                wraplength=int(480 * scale), foreground="#333"
                ).pack(padx=18, pady=(0, 8), anchor="w")

            hide_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(top, text="以后不再提示",
                            variable=hide_var).pack(anchor="w",
                                                    padx=18, pady=(0, 6))

            def _confirm():
                if hide_var.get():
                    try:
                        cfg_set(True)
                    except Exception:
                        pass
                try:
                    top.destroy()
                except Exception:
                    pass

            ttk.Button(top, text="知道了", command=_confirm, width=12
                       ).pack(pady=(4, 14))
            top.protocol("WM_DELETE_WINDOW", _confirm)
        except Exception:
            try:
                top.destroy()
            except Exception:
                pass


# 4 个 GUI 的 tooltip 常量, 供导入方使用 (v0.4.88 用户明确要求, 去掉 pic-clear 前缀).
TOOLTIP_EXTRACT = "切帧工具"
TOOLTIP_DEDUPE = "去重工具"
TOOLTIP_CLASSIFY = "二次过滤工具"
TOOLTIP_STATS_VIEWER = "统计工具"
