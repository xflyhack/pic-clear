# -*- coding: utf-8 -*-
"""
diag_pic.py —— pic-clear 长路径 / 映射盘 打开诊断 GUI

单文件 tkinter GUI，用来定位 dedupe_pic 在堡垒机 Z: 长路径下打不开图片的根因。

用法:
    双击 diag_pic.exe -> 点"浏览"选一张真实的问题图片 -> 点"开始诊断" ->
    结果自动填到大文本框 -> 点"复制全部日志"或 Ctrl+A/Ctrl+C 复制文本 -> 贴给作者.

不做实际去重, 只做只读探测.
"""
from __future__ import annotations

import io
import os
import platform
import subprocess
import sys
import traceback
from datetime import datetime

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except Exception as e:
    print(f"[FATAL] 缺少 tkinter: {e}", file=sys.stderr)
    sys.exit(1)

# 版本号: CI 会覆盖 _version.py 里的 VERSION 成 tag 名
try:
    from _version import VERSION as _V
except Exception:
    _V = "dev"
APP_VERSION = _V

APP_TITLE = f"pic-clear 图片打开诊断工具  {APP_VERSION}"


# ---------------- WNetGetConnectionW (Windows only) ----------------

def _resolve_mapped_drive_to_unc(drive_letter: str) -> tuple[int, str | None]:
    """返回 (rc, unc). 非 Windows 返回 (-1, None)."""
    if os.name != "nt":
        return (-1, None)
    key = drive_letter.upper().rstrip("\\/")
    if not (len(key) == 2 and key[1] == ":"):
        return (-2, None)
    try:
        import ctypes
        from ctypes import wintypes

        mpr = ctypes.WinDLL("mpr", use_last_error=True)
        WNetGetConnectionW = mpr.WNetGetConnectionW
        WNetGetConnectionW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        WNetGetConnectionW.restype = wintypes.DWORD

        buf_len = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(buf_len.value)
        rc = WNetGetConnectionW(key, buf, ctypes.byref(buf_len))
        return (int(rc), buf.value if rc == 0 else None)
    except Exception as e:
        return (-99, f"ctypes exception: {e!r}")


def _long_path_prefix(p: str) -> str:
    r"""对映射盘符 / 本地盘直接套 \\?\, 对 UNC 套 \\?\UNC\."""
    if os.name != "nt":
        return p
    if p.startswith("\\\\?\\") or p.startswith("\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p.lstrip("\\")
    return "\\\\?\\" + p


def _to_unc_full(p: str, unc_root: str) -> str:
    r"""Z:\aaa\bbb -> \\?\UNC\server\share\aaa\bbb, 供第 4 种打开方式使用."""
    rest = p[2:].lstrip("\\")
    return "\\\\?\\UNC\\" + unc_root.lstrip("\\") + "\\" + rest


# ---------------- 诊断主体 ----------------

def run_diagnostics(img_path: str) -> str:
    """跑一遍所有诊断项, 返回一个纯文本报告 (完整可复制)."""
    lines: list[str] = []
    def w(s: str = "") -> None:
        lines.append(s)

    def try_step(label: str, fn):
        w(f"--- {label} ---")
        try:
            result = fn()
            if result is not None:
                w(f"OK  {result}")
            else:
                w("OK")
        except Exception as e:
            w(f"ERR {type(e).__name__}: {e}")
        w("")

    w("=" * 60)
    w(f"pic-clear 图片打开诊断报告")
    w(f"生成时间 : {datetime.now().isoformat(timespec='seconds')}")
    w(f"diag_pic 版本 : {APP_VERSION}")
    w(f"Python 版本 : {sys.version.replace(chr(10), ' ')}")
    w(f"平台 : {platform.platform()}")
    try:
        from PIL import Image
        import PIL
        w(f"Pillow 版本 : {PIL.__version__}")
    except Exception as e:
        w(f"Pillow 版本 : (加载失败) {e}")
        return "\n".join(lines) + "\n[FATAL] 无 Pillow, 诊断中止"
    w("=" * 60)
    w("")

    # ---------- 基础信息 ----------
    w(f"[输入路径] {img_path}")
    w(f"[路径长度] {len(img_path)}")
    w("")

    try_step("os.path.exists", lambda: os.path.exists(img_path))
    try_step("os.path.isfile", lambda: os.path.isfile(img_path))
    try_step("os.path.getsize", lambda: os.path.getsize(img_path))
    try_step("os.stat", lambda: str(os.stat(img_path)))

    # ---------- 映射盘展开 ----------
    if os.name == "nt" and len(img_path) >= 2 and img_path[1] == ":":
        drive = img_path[:2]
        rc, unc = _resolve_mapped_drive_to_unc(drive)
        w(f"[WNetGetConnectionW({drive})] rc={rc}  unc={unc!r}")
        if os.name == "nt":
            try:
                out = subprocess.check_output(
                    ["net", "use", drive],
                    stderr=subprocess.STDOUT,
                    encoding="mbcs",
                    errors="replace",
                    timeout=10,
                )
                w(f"[net use {drive}]")
                for ln in out.splitlines():
                    w("  " + ln)
            except Exception as e:
                w(f"[net use {drive}] ERR {type(e).__name__}: {e}")
        w("")
    else:
        unc = None

    # ---------- 6 种打开方式 ----------
    long_p = _long_path_prefix(img_path)
    w(f"[加前缀后] {long_p}")
    w(f"[加前缀长度] {len(long_p)}")
    w("")

    from PIL import Image  # noqa: E402

    # 1) 原路径 Image.open
    try_step("[1] Image.open(原路径)",
             lambda: str(Image.open(img_path).size))

    # 2) \\?\ 前缀 Image.open
    try_step("[2] Image.open(\\\\?\\ 前缀)",
             lambda: str(Image.open(long_p).size))

    # 3) UNC 展开 Image.open
    if unc:
        try_step(f"[3] Image.open(\\\\?\\UNC\\{unc.lstrip(chr(92))})",
                 lambda: str(Image.open(_to_unc_full(img_path, unc)).size))
    else:
        w("--- [3] Image.open(\\\\?\\UNC\\...) ---")
        w("SKIP 无 UNC 展开信息")
        w("")

    # 4) open('rb', 原路径) + BytesIO
    def open_bio_original():
        with open(img_path, "rb") as f:
            data = f.read()
        return f"read {len(data)} bytes, Image.open(BytesIO) size={Image.open(io.BytesIO(data)).size}"
    try_step("[4] open(原路径,'rb') -> BytesIO -> Image.open",
             open_bio_original)

    # 5) open('rb', \\?\ 前缀) + BytesIO  ← 最有可能是 dedupe_pic 修复方向
    def open_bio_long():
        with open(long_p, "rb") as f:
            data = f.read()
        return f"read {len(data)} bytes, Image.open(BytesIO) size={Image.open(io.BytesIO(data)).size}"
    try_step("[5] open(\\\\?\\前缀,'rb') -> BytesIO -> Image.open",
             open_bio_long)

    # 6) open('rb', UNC 展开)
    if unc:
        unc_full = _to_unc_full(img_path, unc)
        def open_bio_unc():
            with open(unc_full, "rb") as f:
                data = f.read()
            return f"read {len(data)} bytes, Image.open(BytesIO) size={Image.open(io.BytesIO(data)).size}"
        try_step("[6] open(\\\\?\\UNC\\...,'rb') -> BytesIO -> Image.open",
                 open_bio_unc)
    else:
        w("--- [6] open(\\\\?\\UNC\\...,'rb') -> BytesIO -> Image.open ---")
        w("SKIP 无 UNC 展开信息")
        w("")

    # ---------- 文件头字节 hex (最后手段: 看看内容是不是被 SMB 拦成 0 字节 / HTML) ----------
    for label, path_try in [("原路径", img_path), ("\\\\?\\ 前缀", long_p)]:
        w(f"--- 文件头 hex ({label}) ---")
        try:
            with open(path_try, "rb") as f:
                head = f.read(32)
            w(f"OK  {len(head)} bytes: " + " ".join(f"{b:02x}" for b in head))
        except Exception as e:
            w(f"ERR {type(e).__name__}: {e}")
        w("")

    w("=" * 60)
    w("诊断结束. 请把上面全部内容复制并贴给作者.")
    w("=" * 60)
    return "\n".join(lines)


# ---------------- GUI ----------------

class DiagApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("980x680")

        top = ttk.Frame(root, padding=8)
        top.pack(fill=tk.X)

        ttk.Label(top, text="图片路径:").pack(side=tk.LEFT)
        self.path_var = tk.StringVar()
        self.entry = ttk.Entry(top, textvariable=self.path_var)
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(top, text="浏览...", command=self.on_browse).pack(side=tk.LEFT, padx=2)

        btns = ttk.Frame(root, padding=(8, 0))
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="开始诊断", command=self.on_run).pack(side=tk.LEFT)
        ttk.Button(btns, text="清空日志", command=self.on_clear).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="复制全部日志", command=self.on_copy).pack(side=tk.LEFT)
        ttk.Label(btns, text=f"  {APP_TITLE}", foreground="#888").pack(side=tk.RIGHT)

        body = ttk.Frame(root, padding=8)
        body.pack(fill=tk.BOTH, expand=True)
        self.txt = tk.Text(body, wrap=tk.NONE, font=("Consolas", 10))
        yscroll = ttk.Scrollbar(body, orient=tk.VERTICAL, command=self.txt.yview)
        xscroll = ttk.Scrollbar(body, orient=tk.HORIZONTAL, command=self.txt.xview)
        self.txt.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.txt.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)

        # 允许 Ctrl+A / Ctrl+C
        self.txt.bind("<Control-a>", self._select_all)
        self.txt.bind("<Control-A>", self._select_all)

        self._append(f"欢迎使用 {APP_TITLE}\n")
        self._append("使用步骤:\n")
        self._append("  1) 点 [浏览...] 选一张真实存在的图片 (jpg/png/bmp/webp)\n")
        self._append("  2) 点 [开始诊断]\n")
        self._append("  3) 结果自动填到本框, 用 [复制全部日志] 或 Ctrl+A / Ctrl+C 复制\n")
        self._append("  4) 把结果贴给作者定位\n\n")

    def _select_all(self, _event=None):
        self.txt.tag_add("sel", "1.0", "end")
        return "break"

    def _append(self, s: str) -> None:
        self.txt.insert(tk.END, s)
        self.txt.see(tk.END)

    def on_browse(self):
        p = filedialog.askopenfilename(
            title="选一张图片做诊断",
            filetypes=[
                ("图片文件", "*.jpg *.jpeg *.png *.bmp *.webp *.gif"),
                ("所有文件", "*.*"),
            ],
        )
        if p:
            self.path_var.set(p)

    def on_clear(self):
        self.txt.delete("1.0", tk.END)

    def on_copy(self):
        try:
            content = self.txt.get("1.0", tk.END)
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            self.root.update()
            messagebox.showinfo("已复制", "全部日志已复制到剪贴板, 直接粘贴给作者即可")
        except Exception as e:
            messagebox.showerror("复制失败", str(e))

    def on_run(self):
        p = self.path_var.get().strip().strip('"').strip("'")
        if not p:
            messagebox.showwarning("请选图片", "请先点 [浏览...] 选一张真实存在的图片")
            return
        self._append(f"\n>>> 开始诊断: {p}\n\n")
        self.root.update()
        try:
            report = run_diagnostics(p)
        except Exception:
            report = "诊断脚本自身抛异常:\n" + traceback.format_exc()
        self._append(report + "\n")


def main():
    root = tk.Tk()
    try:
        # DPI 兼容: 高分屏不糊
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
    except Exception:
        pass
    DiagApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
