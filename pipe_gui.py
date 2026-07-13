# -*- coding: utf-8 -*-
"""
pipe_gui.py —— pic-clear 的图形前端

- 主窗口：tkinter 表单，选盘/选源目录/选输出/选子目录/调阈值/一键运行
- 系统托盘：pystray + Pillow 生成图标
- 全局快捷键：keyboard，默认 Ctrl+Alt+P 呼出状态浮层
- 环境自检：查 extract_frames.exe / dedupe_pic.exe / license.lic
- 后台运行：内部调 pipeline.cmd_submit()，跟 pipeline.exe 一样 detach worker
- 状态刷新：读 jobs 目录的 status.json + tasklist 检查 3 个 exe

编译成独立的 pipe_gui.exe，不影响现有 pipeline.exe / dedupe_pic.exe / extract_frames.exe。
"""

from __future__ import annotations

import argparse
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# --- 依赖检查（GUI 打包时都会带上，但开发时给个友好报错） ---
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except Exception as e:
    print(f"[FATAL] 缺少 tkinter：{e}", file=sys.stderr)
    sys.exit(1)

# 复用 pipeline 内部函数（同目录 py 文件；PyInstaller 也会一起打包）
import pipeline  # noqa: E402


# =========================================================================
# 环境自检
# =========================================================================

REQUIRED_EXES = ["extract_frames.exe", "dedupe_pic.exe"]
REQUIRED_FILES = ["license.lic"]

# Windows 常见部署位置：System32 + pipe_gui.exe 同目录 + PATH
def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    if os.name == "nt":
        sysroot = os.environ.get("SystemRoot", r"C:\Windows")
        dirs.append(Path(sysroot) / "System32")
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent)
    else:
        dirs.append(Path(__file__).resolve().parent)
    return dirs


def _find_pipeline_exe() -> str | None:
    """在 System32 / pipe_gui.exe 同目录 / PATH 里找 pipeline.exe。找不到返回 None。"""
    import shutil
    for d in _candidate_dirs():
        cand = d / "pipeline.exe"
        if cand.is_file():
            return str(cand)
    hit = shutil.which("pipeline.exe")
    return hit


def check_environment() -> tuple[bool, list[tuple[str, str, str]]]:
    """
    返回 (all_ok, rows)
    rows: [(name, status, path_or_hint), ...]
        status = "OK" | "MISS"
    """
    rows: list[tuple[str, str, str]] = []
    all_ok = True
    for name in REQUIRED_EXES + REQUIRED_FILES:
        found = None
        for d in _candidate_dirs():
            p = d / name
            if p.is_file():
                found = p
                break
        # 再兜底 PATH
        if not found and name.endswith(".exe"):
            import shutil
            hit = shutil.which(name)
            if hit:
                found = Path(hit)
        if found:
            rows.append((name, "OK", str(found)))
        else:
            rows.append((name, "MISS", "  ".join(str(d) for d in _candidate_dirs())))
            all_ok = False
    return all_ok, rows


# =========================================================================
# 数据盘/目录辅助
# =========================================================================

def list_drives() -> list[str]:
    """Windows：返回 ['C:', 'D:', 'Z:'] 之类。非 Windows：返回 ['/']。"""
    if os.name != "nt":
        return ["/"]
    from string import ascii_uppercase
    out = []
    for letter in ascii_uppercase:
        d = f"{letter}:\\"
        if Path(d).exists():
            out.append(f"{letter}:")
    return out


def list_subdirs(root: Path) -> list[str]:
    try:
        return sorted([p.name for p in root.iterdir() if p.is_dir()])
    except Exception:
        return []


# =========================================================================
# 状态查询：进程 + 最新 job
# =========================================================================

# pipe_gui.exe 也算进来：因为 pipe_gui 提交任务时 detach 出的 worker 就是自身副本
PROC_NAMES = ["pipe_gui.exe", "pipeline.exe", "extract_frames.exe", "dedupe_pic.exe"]


def query_processes() -> dict[str, dict]:
    """返回 { name: {"running": bool, "count": int, "pids": [..], "mem_kb": int} }"""
    result: dict[str, dict] = {n: {"running": False, "count": 0, "pids": [], "mem_kb": 0} for n in PROC_NAMES}
    if os.name != "nt":
        # 非 Windows 场景（开发调试），用 ps
        try:
            out = subprocess.check_output(["ps", "-A", "-o", "pid=,comm="], text=True)
            for line in out.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split(None, 1)
                if len(parts) < 2:
                    continue
                pid, comm = parts
                for n in PROC_NAMES:
                    if comm.endswith(n) or comm.endswith(n.replace(".exe", "")):
                        r = result[n]
                        r["running"] = True
                        r["count"] += 1
                        r["pids"].append(int(pid))
        except Exception:
            pass
        return result

    try:
        # tasklist /FO CSV /NH，逗号分隔，双引号包字段
        raw = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"],
            creationflags=0x08000000,  # CREATE_NO_WINDOW
            text=True, encoding="gbk", errors="ignore",
        )
    except Exception:
        return result

    import csv, io
    reader = csv.reader(io.StringIO(raw))
    for row in reader:
        if len(row) < 5:
            continue
        name = row[0].strip()
        if name not in PROC_NAMES:
            continue
        try:
            pid = int(row[1].strip())
        except Exception:
            continue
        mem_raw = row[4].strip().replace(" K", "").replace(",", "").replace(",", "")
        try:
            mem_kb = int(mem_raw)
        except Exception:
            mem_kb = 0
        r = result[name]
        r["running"] = True
        r["count"] += 1
        r["pids"].append(pid)
        r["mem_kb"] += mem_kb
    return result


def find_latest_job(out_root: Path):
    jobs_root = out_root / ".pipeline" / "jobs"
    if not jobs_root.is_dir():
        return None
    subs = [p for p in jobs_root.iterdir() if p.is_dir()]
    if not subs:
        return None
    subs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return pipeline._load_status(subs[0])


def format_kb(kb: int) -> str:
    if kb >= 1024:
        return f"{kb/1024:.1f} MB"
    return f"{kb} K"


# =========================================================================
# 主 GUI
# =========================================================================

class PipeGUI:
    APP_TITLE = "pic-clear 图形界面"
    REFRESH_MS = 5000

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(self.APP_TITLE)
        self.root.geometry("720x680")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # 状态
        self._tray_icon = None
        self._tray_thread = None
        self._hotkey_registered = False
        self._status_toplevel: tk.Toplevel | None = None
        self._status_labels: dict = {}
        self._minimize_to_tray_var = tk.BooleanVar(value=True)
        self._hide_children_var = tk.BooleanVar(value=True)
        self._apply_var = tk.BooleanVar(value=True)
        self._hard_delete_var = tk.BooleanVar(value=True)

        # 表单变量
        self._drive_var = tk.StringVar()
        self._src_var = tk.StringVar()
        self._out_var = tk.StringVar()
        self._threshold_var = tk.IntVar(value=3)
        self._motion_var = tk.DoubleVar(value=0.12)
        self._fps_var = tk.DoubleVar(value=1.0)
        self._hotkey_var = tk.StringVar(value="ctrl+alt+p")

        self._sub_vars: list[tuple[str, tk.BooleanVar]] = []

        self._build_ui()
        self._refresh_env()
        self._auto_pick_drive()

    # ---------- UI 布局 ----------

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # 环境检测
        f_env = ttk.LabelFrame(self.root, text="▶ 环境检测")
        f_env.pack(fill="x", **pad)
        self._env_text = tk.Text(f_env, height=4, width=90, font=("Consolas", 9))
        self._env_text.pack(fill="x", padx=6, pady=4)
        self._env_text.config(state="disabled")

        # 数据配置
        f_data = ttk.LabelFrame(self.root, text="▶ 数据配置")
        f_data.pack(fill="x", **pad)

        row = ttk.Frame(f_data); row.pack(fill="x", padx=6, pady=3)
        ttk.Label(row, text="数据盘：", width=10).pack(side="left")
        self._drive_combo = ttk.Combobox(row, textvariable=self._drive_var,
                                         values=list_drives(), width=8, state="readonly")
        self._drive_combo.pack(side="left")
        ttk.Button(row, text="刷新盘符", command=self._refresh_drives).pack(side="left", padx=6)
        self._drive_combo.bind("<<ComboboxSelected>>", lambda e: self._on_drive_change())

        row = ttk.Frame(f_data); row.pack(fill="x", padx=6, pady=3)
        ttk.Label(row, text="源目录：", width=10).pack(side="left")
        ttk.Entry(row, textvariable=self._src_var, width=60).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览...", command=self._browse_src).pack(side="left", padx=6)

        row = ttk.Frame(f_data); row.pack(fill="x", padx=6, pady=3)
        ttk.Label(row, text="输出根：", width=10).pack(side="left")
        ttk.Entry(row, textvariable=self._out_var, width=60).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览...", command=self._browse_out).pack(side="left", padx=6)

        # 子目录选择
        f_subs = ttk.LabelFrame(self.root, text="▶ 子目录（勾选要处理的）")
        f_subs.pack(fill="both", expand=True, **pad)
        top = ttk.Frame(f_subs); top.pack(fill="x", padx=6, pady=3)
        ttk.Button(top, text="扫描/刷新", command=self._rescan_subs).pack(side="left")
        ttk.Button(top, text="全选", command=lambda: self._sub_toggle_all(True)).pack(side="left", padx=6)
        ttk.Button(top, text="全不选", command=lambda: self._sub_toggle_all(False)).pack(side="left")

        self._subs_canvas_frame = ttk.Frame(f_subs)
        self._subs_canvas_frame.pack(fill="both", expand=True, padx=6, pady=3)
        self._subs_canvas = tk.Canvas(self._subs_canvas_frame, height=110, highlightthickness=0)
        self._subs_scroll = ttk.Scrollbar(self._subs_canvas_frame, orient="vertical",
                                          command=self._subs_canvas.yview)
        self._subs_inner = ttk.Frame(self._subs_canvas)
        self._subs_inner.bind(
            "<Configure>",
            lambda e: self._subs_canvas.configure(scrollregion=self._subs_canvas.bbox("all")),
        )
        self._subs_canvas.create_window((0, 0), window=self._subs_inner, anchor="nw")
        self._subs_canvas.configure(yscrollcommand=self._subs_scroll.set)
        self._subs_canvas.pack(side="left", fill="both", expand=True)
        self._subs_scroll.pack(side="right", fill="y")

        # 处理选项
        f_opt = ttk.LabelFrame(self.root, text="▶ 处理选项")
        f_opt.pack(fill="x", **pad)

        row = ttk.Frame(f_opt); row.pack(fill="x", padx=6, pady=3)
        ttk.Label(row, text="相似度阈值 (-t)：", width=18).pack(side="left")
        ttk.Spinbox(row, from_=0, to=32, textvariable=self._threshold_var, width=6).pack(side="left")
        ttk.Label(row, text="   车运动阈值 (-m)：").pack(side="left")
        ttk.Spinbox(row, from_=0.0, to=1.0, increment=0.01,
                    textvariable=self._motion_var, width=6, format="%.2f").pack(side="left")
        ttk.Label(row, text="   抽帧 fps：").pack(side="left")
        ttk.Spinbox(row, from_=0.1, to=30.0, increment=0.5,
                    textvariable=self._fps_var, width=6, format="%.1f").pack(side="left")

        row = ttk.Frame(f_opt); row.pack(fill="x", padx=6, pady=3)
        ttk.Checkbutton(row, text="真删除（-y，取消勾选则 dry-run）",
                        variable=self._apply_var).pack(side="left")
        ttk.Checkbutton(row, text="永久删除，不落 _trash（-H）",
                        variable=self._hard_delete_var).pack(side="left", padx=12)

        # 后台选项
        f_bg = ttk.LabelFrame(self.root, text="▶ 后台选项")
        f_bg.pack(fill="x", **pad)
        row = ttk.Frame(f_bg); row.pack(fill="x", padx=6, pady=3)
        ttk.Checkbutton(row, text="点 × 时最小化到托盘（不退出）",
                        variable=self._minimize_to_tray_var).pack(side="left")
        ttk.Checkbutton(row, text="隐藏所有子进程黑窗口（pipeline 已内建，此项仅提示）",
                        variable=self._hide_children_var).pack(side="left", padx=12)
        row = ttk.Frame(f_bg); row.pack(fill="x", padx=6, pady=3)
        ttk.Label(row, text="呼出快捷键：").pack(side="left")
        ttk.Entry(row, textvariable=self._hotkey_var, width=20).pack(side="left")
        ttk.Button(row, text="注册快捷键", command=self._register_hotkey).pack(side="left", padx=6)

        # 底部按钮
        f_btn = ttk.Frame(self.root); f_btn.pack(fill="x", **pad)
        ttk.Button(f_btn, text="运行", command=self._on_run, width=14).pack(side="left", padx=4)
        ttk.Button(f_btn, text="停止当前任务", command=self._on_stop, width=14).pack(side="left", padx=4)
        ttk.Button(f_btn, text="查看状态", command=self.show_status_window, width=14).pack(side="left", padx=4)
        ttk.Button(f_btn, text="最小化到托盘", command=self.hide_to_tray, width=14).pack(side="left", padx=4)
        ttk.Button(f_btn, text="退出", command=self.quit_all, width=10).pack(side="right", padx=4)

    # ---------- 环境检测 ----------

    def _refresh_env(self):
        ok, rows = check_environment()
        self._env_text.config(state="normal")
        self._env_text.delete("1.0", "end")
        for name, status, hint in rows:
            mark = "[√]" if status == "OK" else "[×]"
            self._env_text.insert("end", f"  {mark} {name:<24} {hint}\n")
        self._env_text.config(state="disabled")
        if not ok:
            self._env_ok = False
        else:
            self._env_ok = True

    # ---------- 盘符/目录 ----------

    def _refresh_drives(self):
        self._drive_combo["values"] = list_drives()
        self._auto_pick_drive()

    def _auto_pick_drive(self):
        drives = list_drives()
        if not drives:
            return
        # 优先 Z:
        if "Z:" in drives:
            self._drive_var.set("Z:")
        else:
            self._drive_var.set(drives[0])
        self._on_drive_change()

    def _on_drive_change(self):
        drive = self._drive_var.get()
        if not drive:
            return
        # 默认 out_root
        if not self._out_var.get():
            self._out_var.set(str(pipeline.default_out_root(drive)))
        # 尝试猜 src
        if not self._src_var.get():
            try:
                root = Path(drive + "\\") if os.name == "nt" else Path("/")
                if root.is_dir():
                    guesses = [p for p in root.iterdir()
                               if p.is_dir() and p.name.startswith("sjbz_")]
                    if len(guesses) == 1:
                        self._src_var.set(str(guesses[0]))
            except Exception:
                pass

    def _browse_src(self):
        init = self._src_var.get() or (self._drive_var.get() + "\\" if os.name == "nt" else "/")
        p = filedialog.askdirectory(initialdir=init, title="选择源目录")
        if p:
            self._src_var.set(p)
            self._rescan_subs()

    def _browse_out(self):
        init = self._out_var.get() or (self._drive_var.get() + "\\" if os.name == "nt" else "/")
        p = filedialog.askdirectory(initialdir=init, title="选择输出根")
        if p:
            self._out_var.set(p)

    def _rescan_subs(self):
        for w in self._subs_inner.winfo_children():
            w.destroy()
        self._sub_vars.clear()
        src = self._src_var.get()
        if not src:
            return
        subs = list_subdirs(Path(src))
        if not subs:
            ttk.Label(self._subs_inner, text="（该目录下没有子目录，将直接处理整个目录）",
                      foreground="gray").pack(anchor="w")
            return
        # 6 列布局
        cols = 6
        for i, name in enumerate(subs):
            var = tk.BooleanVar(value=True)
            cb = ttk.Checkbutton(self._subs_inner, text=name, variable=var)
            cb.grid(row=i // cols, column=i % cols, sticky="w", padx=6, pady=2)
            self._sub_vars.append((name, var))

    def _sub_toggle_all(self, value: bool):
        for _, v in self._sub_vars:
            v.set(value)

    # ---------- 运行/停止 ----------

    def _on_run(self):
        if not getattr(self, "_env_ok", False):
            if not messagebox.askyesno("环境缺失", "有必需文件缺失，仍然继续吗？"):
                return
        src = self._src_var.get().strip()
        out = self._out_var.get().strip()
        if not src or not Path(src).is_dir():
            messagebox.showerror("参数错误", f"源目录无效：{src}"); return
        if not out:
            messagebox.showerror("参数错误", "输出根不能为空"); return

        selected = [name for name, v in self._sub_vars if v.get()]
        if self._sub_vars and not selected:
            messagebox.showerror("参数错误", "至少勾选一个子目录"); return

        # 构造 argparse.Namespace 直接调 pipeline.cmd_submit
        args = argparse.Namespace(
            cmd="submit",
            out_root=out,
            data_drive=self._drive_var.get() or None,
            auto=True,
            src=src,
            subs=",".join(selected) if selected else None,
            data_prefix=None,
            threshold=int(self._threshold_var.get()),
            fps=float(self._fps_var.get()),
            ext=".h265,.mp4",
            apply=bool(self._apply_var.get()),
            hard_delete=bool(self._hard_delete_var.get()),
            motion_threshold=float(self._motion_var.get()),
            daily_remain_limit=80000,
            fingerprint=False,
        )

        # 优先让 detach 出来的 worker 是 pipeline.exe，而不是 pipe_gui.exe 自身副本。
        # 这样进程列表和状态浮层显示更干净，也避免自身副本被误当成 GUI 再次弹窗
        # （虽然 _looks_like_cli() 已经兜住，但用 pipeline.exe 更符合直觉）。
        pipeline_exe = _find_pipeline_exe()
        if pipeline_exe:
            os.environ["PIPELINE_WORKER_EXE_OVERRIDE"] = pipeline_exe

        # 在后台线程调 submit（内部会 detach worker，很快返回）
        def run_submit():
            try:
                rc = pipeline.cmd_submit(args)
                self.root.after(0, lambda: messagebox.showinfo(
                    "已提交", f"任务已提交（rc={rc}）。可点『查看状态』查看进度。"))
            except SystemExit as se:
                self.root.after(0, lambda: messagebox.showerror(
                    "提交失败", f"pipeline 退出码 {se.code}"))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror(
                    "提交失败", f"{type(e).__name__}: {e}"))

        threading.Thread(target=run_submit, daemon=True).start()

    def _on_stop(self):
        out = self._out_var.get().strip()
        if not out:
            messagebox.showerror("参数错误", "先填输出根"); return
        st = find_latest_job(Path(out))
        if not st:
            messagebox.showinfo("无任务", "没找到可停止的任务"); return
        if not messagebox.askyesno("确认",
                                    f"停止任务 {st.job_id}（PID {st.pid}）吗？"):
            return
        args = argparse.Namespace(cmd="stop", out_root=out,
                                  data_drive=self._drive_var.get() or None,
                                  job_id=st.job_id, fingerprint=False)
        try:
            rc = pipeline.cmd_stop(args)
            messagebox.showinfo("已停止", f"rc={rc}")
        except Exception as e:
            messagebox.showerror("停止失败", str(e))

    # ---------- 托盘 ----------

    def hide_to_tray(self):
        try:
            self._ensure_tray()
        except Exception as e:
            messagebox.showerror("托盘失败", f"无法创建托盘：{e}")
            return
        self.root.withdraw()

    def _ensure_tray(self):
        if self._tray_icon is not None:
            return
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception as e:
            raise RuntimeError(f"缺少 pystray/Pillow：{e}")

        img = Image.new("RGB", (64, 64), color=(30, 100, 200))
        d = ImageDraw.Draw(img)
        d.rectangle((12, 20, 52, 44), fill=(255, 255, 255))
        d.text((22, 24), "PC", fill=(30, 100, 200))

        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口", lambda: self.root.after(0, self.show_main)),
            pystray.MenuItem("显示状态", lambda: self.root.after(0, self.show_status_window)),
            pystray.MenuItem("停止当前任务", lambda: self.root.after(0, self._on_stop)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda: self.root.after(0, self.quit_all)),
        )
        self._tray_icon = pystray.Icon("pic-clear", img, "pic-clear", menu)

        def run_tray():
            try:
                self._tray_icon.run()
            except Exception:
                pass

        self._tray_thread = threading.Thread(target=run_tray, daemon=True)
        self._tray_thread.start()

    def show_main(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def on_close(self):
        if self._minimize_to_tray_var.get():
            self.hide_to_tray()
        else:
            self.quit_all()

    def quit_all(self):
        try:
            if self._tray_icon is not None:
                self._tray_icon.stop()
        except Exception:
            pass
        try:
            if self._hotkey_registered:
                import keyboard
                keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        self.root.destroy()

    # ---------- 状态浮层 ----------

    def show_status_window(self):
        if self._status_toplevel is not None and self._status_toplevel.winfo_exists():
            self._status_toplevel.deiconify()
            self._status_toplevel.lift()
            self._status_toplevel.focus_force()
            return
        w = tk.Toplevel(self.root)
        self._status_toplevel = w
        w.title("pic-clear 运行状态")
        w.geometry("520x300")
        w.protocol("WM_DELETE_WINDOW", lambda: (w.withdraw()))

        info = tk.Text(w, font=("Consolas", 10))
        info.pack(fill="both", expand=True, padx=8, pady=8)
        info.config(state="disabled")

        btn = ttk.Frame(w); btn.pack(fill="x", padx=8, pady=4)
        ttk.Button(btn, text="立即刷新", command=lambda: self._refresh_status_text(info)).pack(side="left")
        ttk.Button(btn, text="关闭", command=lambda: w.withdraw()).pack(side="right")

        self._status_labels = {"info": info}
        self._refresh_status_text(info)
        self._schedule_status_refresh()

    def _schedule_status_refresh(self):
        w = self._status_toplevel
        if w is None or not w.winfo_exists():
            return
        info = self._status_labels.get("info")
        if info and w.winfo_viewable():
            self._refresh_status_text(info)
        self.root.after(self.REFRESH_MS, self._schedule_status_refresh)

    def _refresh_status_text(self, info: tk.Text):
        procs = query_processes()
        lines = []
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"pic-clear 运行状态     {ts}")
        lines.append("=" * 56)
        for name in PROC_NAMES:
            r = procs[name]
            if r["running"]:
                pids = ",".join(str(p) for p in r["pids"])
                mem = format_kb(r["mem_kb"])
                if r["count"] > 1:
                    lines.append(f"  [ OK  ] {name:<22} 运行中  x{r['count']}  PID {pids}  {mem}")
                else:
                    lines.append(f"  [ OK  ] {name:<22} 运行中   PID {pids}   内存 {mem}")
            else:
                lines.append(f"  [ --  ] {name:<22} 未运行")
        lines.append("-" * 56)

        out = self._out_var.get().strip()
        if out and Path(out).is_dir():
            st = find_latest_job(Path(out))
            if st:
                done = sum(1 for s in st.subs if s.stage == "done")
                failed = sum(1 for s in st.subs if s.stage == "failed")
                lines.append(f"  最新任务  : {st.job_id}")
                lines.append(f"  状态      : {st.state}    worker PID: {st.pid}")
                lines.append(f"  进度      : {done} / {st.total_subs}   (失败 {failed})")
                lines.append(f"  最后消息  : {st.last_message}")
            else:
                lines.append("  最新任务  : （无）")
        else:
            lines.append("  最新任务  : （输出目录未设置或不存在，无法查询）")

        info.config(state="normal")
        info.delete("1.0", "end")
        info.insert("end", "\n".join(lines))
        info.config(state="disabled")

    # ---------- 全局快捷键 ----------

    def _register_hotkey(self):
        try:
            import keyboard
        except Exception as e:
            messagebox.showerror("快捷键失败",
                                 f"缺少 keyboard 库：{e}"); return
        hk = self._hotkey_var.get().strip()
        if not hk:
            messagebox.showerror("快捷键失败", "请填快捷键，如 ctrl+alt+p"); return
        try:
            if self._hotkey_registered:
                keyboard.unhook_all_hotkeys()
            keyboard.add_hotkey(hk, lambda: self.root.after(0, self.show_status_window))
            self._hotkey_registered = True
            messagebox.showinfo("已注册", f"快捷键：{hk}\n呼出状态浮层")
        except Exception as e:
            messagebox.showerror("快捷键失败", f"注册失败：{e}\n有的堡垒机 RDP 会话可能拦截全局钩子。")


# =========================================================================
# 入口
# =========================================================================

# 会被识别为 pipeline CLI 的子命令；命中就绕开 GUI 直接把 argv 交给 pipeline.main()
_PIPELINE_CLI_CMDS = {"submit", "worker", "list", "status", "logs", "stop"}


def _looks_like_cli() -> bool:
    """判断 argv 是不是 pipeline 的 CLI 调用（尤其是 detach 出来的 worker）。

    典型场景：pipeline.cmd_submit() 里 detach 时会用 sys.executable 重新拉起自己，
    命令行长成  pipe_gui.exe worker --job-id xxx --out-root xxx。
    这时候不能进 GUI，要走 CLI，否则会：
      1) 再弹一个 GUI 窗口（用户看到的问题一）
      2) worker 根本没跑起来（用户看到的问题二）
    """
    if "--fingerprint" in sys.argv[1:]:
        return True
    for a in sys.argv[1:]:
        if a.startswith("-"):
            continue
        # 第一个非选项 token 是子命令名
        return a in _PIPELINE_CLI_CMDS
    return False


def main() -> int:
    # ---- CLI 短路：命中 pipeline 子命令时，直接把控制权交给 pipeline.main() ----
    # 这是修复"detach worker 又开 GUI"和"worker 未运行"两个问题的关键。
    if _looks_like_cli():
        return pipeline.main()

    # ---- 正常 GUI 启动 ----
    # 授权检查（跟 pipeline 一样，如果签名过期直接报错）
    try:
        pipeline._check_license_or_die()
    except SystemExit:
        # _check_license_or_die 内部会 sys.exit()，让它自然带着 exit code 结束
        raise
    except Exception as e:
        print(f"[WARN] 授权检查异常: {e}", file=sys.stderr)

    root = tk.Tk()
    try:
        # Windows 高 DPI 适配
        if os.name == "nt":
            try:
                from ctypes import windll
                windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
        app = PipeGUI(root)
        root.mainloop()
    except Exception as e:
        try:
            messagebox.showerror("崩溃", f"{type(e).__name__}: {e}")
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
