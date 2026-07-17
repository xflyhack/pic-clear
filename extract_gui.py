# -*- coding: utf-8 -*-
"""
extract_gui.py —— pic-clear 的抽帧独立 GUI

- 只做一件事：选视频源目录 + 选切帧输出根 + 选一级子目录（可多选） → 后台调
  extract_frames.exe，把源目录里的 .h265 / .mp4 按 1 帧/秒抽成 JPG，
  输出结构镜像源目录（`sub/videoA/frame_000001.jpg` ...）。
- 不做去重：想去重请打开 dedupe_gui.exe。
- 后台线程调 subprocess，实时把 stdout 显示到日志区，进度条按视频数汇总。
- 托盘 + 快捷键：托盘图标常驻，Ctrl+Alt+E 呼出主窗口。
- DPI 自适应、图标、授权、配置文件复用 pipe_gui 的现成实现。

编译成独立的 extract_gui.exe，与现有 pipe_gui.exe / extract_frames.exe /
dedupe_gui.exe 完全独立。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except Exception as e:
    print(f"[FATAL] 缺少 tkinter：{e}", file=sys.stderr)
    sys.exit(1)

# 复用 pipe_gui 里的工具函数（DPI / 授权 / 图标 / 配置 / 托盘辅助）
# pipe_gui.py 会跟这个 GUI 一起被 PyInstaller 打包
import pipe_gui as _pg  # noqa: E402
import pipeline  # noqa: E402


APP_TITLE = "pic-clear 抽帧工具"
APP_VERSION = "v0.3.1"
APP_COMPANY = "山东数旗信息科技有限公司"
CONFIG_NAME = "extract_gui.json"
HOTKEY_DEFAULT = "ctrl+alt+e"
VIDEO_EXTS = (".h265", ".mp4")


# ---------- 配置文件（独立于 pipe_gui.json） ----------

def _config_path() -> Path:
    return Path(os.path.expanduser("~")) / ".pic-clear" / CONFIG_NAME


def _log_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".pic-clear" / "extract_gui_logs"


def _new_log_path() -> Path:
    """本次启动的日志文件路径：extract_gui_YYYYMMDD_HHMM.log。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    d = _log_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / f"extract_gui_{ts}.log"


def _latest_prev_log(exclude: Path) -> Path | None:
    """找上次留下的最新日志文件（时间戳最大，排除本次刚建的）。"""
    d = _log_dir()
    if not d.is_dir():
        return None
    candidates = sorted(
        (p for p in d.glob("extract_gui_*.log") if p != exclude),
        key=lambda p: p.name,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _tail_lines(path: Path, n: int = 200) -> list[str]:
    """读文件末尾 n 行；文件不大直接 readlines，够用。"""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return lines[-n:]
    except Exception:
        return []


def _load_config() -> dict:
    p = _config_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config(cfg: dict) -> None:
    try:
        p = _config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, p)
    except Exception as e:
        print(f"[WARN] 保存配置失败: {e}", file=sys.stderr)


# ---------- worker exe 定位 ----------

def _find_extract_exe() -> str | None:
    """按 pipeline.resolve_worker_exe 的规则找 extract_frames.exe：
    exe 同目录 → System32 → PATH。找不到返回 None。"""
    try:
        return pipeline.resolve_worker_exe("extract_frames")
    except Exception:
        return None


# ---------- 视频扫描 ----------

def _list_videos(root: Path) -> list[Path]:
    """递归列出根目录下所有支持的视频文件。"""
    out: list[Path] = []
    if not root.is_dir():
        return out
    for ext in VIDEO_EXTS:
        try:
            out.extend(root.rglob(f"*{ext}"))
        except Exception:
            pass
    return sorted(out)


# ---------- 主 GUI ----------

class ExtractGUI:
    REFRESH_MS = 1000

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{APP_TITLE}  {APP_VERSION}")
        self._ui_scale = float(getattr(self.root, "__ui_scale__", 1.0))

        # 提前加载配置（geometry 需要）
        self._cfg = _load_config()
        saved_geo = self._cfg.get("window_geometry")
        if saved_geo:
            saved_geo = _pg._sanitize_saved_geometry(self.root, saved_geo)
        # extract_gui 主页内容较多，额外要求高度 >= 560, 否则回退默认
        # 避免历史保存的过矮窗口导致底部"开始抽帧"按钮被挤出可视区
        if saved_geo:
            parsed = _pg._parse_geometry_str(saved_geo)
            if parsed and parsed[1] < int(560 * self._ui_scale):
                saved_geo = None
        if saved_geo:
            self.root.geometry(saved_geo)
        else:
            self.root.geometry(_pg._compute_default_geometry(
                self.root, self._ui_scale,
                base_w=760, base_h=620, min_w=680, min_h=560))
        self.root.minsize(int(680 * self._ui_scale), int(560 * self._ui_scale))
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        try:
            _pg._apply_window_icon(self.root)
        except Exception:
            pass

        # 表单变量
        self._src_var = tk.StringVar(value=self._cfg.get("src", ""))
        self._out_var = tk.StringVar(value=self._cfg.get("out_root", ""))
        self._fps_var = tk.DoubleVar(value=float(self._cfg.get("fps", 1.0)))
        self._extract_jobs_var = tk.IntVar(value=int(self._cfg.get("extract_jobs", 1)))
        self._lock_ttl_var = tk.IntVar(value=int(self._cfg.get("lock_ttl", 900)))
        self._markers_root_var = tk.StringVar(value=self._cfg.get("markers_root", ""))
        self._force_reextract_var = tk.BooleanVar(
            value=bool(self._cfg.get("force_reextract", False)))
        self._minimize_to_tray_var = tk.BooleanVar(
            value=bool(self._cfg.get("minimize_to_tray", True)))
        self._hotkey_var = tk.StringVar(
            value=self._cfg.get("hotkey", HOTKEY_DEFAULT))

        # 子目录复选框：[(name, BooleanVar), ...]
        self._sub_vars: list[tuple[str, tk.BooleanVar]] = []

        # 运行时状态
        self._tray_icon = None
        self._tray_thread = None
        self._hotkey_registered = False
        self._worker_thread: threading.Thread | None = None
        self._worker_stop_flag = threading.Event()
        self._log_queue: queue.Queue[str] = queue.Queue()
        self._progress_var = tk.StringVar(value="就绪")
        self._total_videos = 0
        self._done_videos = 0

        # 日志文件（本次启动新建一份，不覆盖历史）
        self._log_path = _new_log_path()
        try:
            self._log_file = self._log_path.open("a", encoding="utf-8")
        except Exception:
            self._log_file = None
        self._log_file_lock = threading.Lock()

        # 智能自动滚动：手动往上翻则暂停跟随，滚回底部自动恢复
        self._auto_scroll_var = tk.BooleanVar(value=True)

        self._build_ui()
        self.root.after(200, self._drain_log_queue)

        # 环境预检
        self.root.after(300, self._check_environment)

    # ---------- UI ----------

    def _build_ui(self):
        _pg.apply_tab_style(self.root)

        # 底部按钮条：必须先 pack(side="bottom") 占位，
        # 否则窗口太矮时会被上方 expand=True 的 Notebook 挤到窗口外看不见。
        bar = ttk.Frame(self.root)
        bar.pack(side="bottom", fill="x", padx=8, pady=(0, 8))
        self._run_btn = ttk.Button(bar, text="▶ 开始抽帧", command=self._on_run)
        self._run_btn.pack(side="left")
        self._stop_btn = ttk.Button(bar, text="■ 停止", command=self._on_stop,
                                    state="disabled")
        self._stop_btn.pack(side="left", padx=6)
        ttk.Button(bar, text="最小化到托盘",
                   command=self.hide_to_tray).pack(side="left", padx=6)
        ttk.Button(bar, text="退出", command=self.quit_all).pack(side="right")

        # Notebook 放在按钮条上方，吃掉剩余空间
        nb = ttk.Notebook(self.root)
        nb.pack(side="top", fill="both", expand=True, padx=8, pady=6)

        # 主页 tab
        page = ttk.Frame(nb)
        nb.add(page, text="抽帧")
        self._build_main_tab(page)

        # 关于 tab
        about = ttk.Frame(nb)
        nb.add(about, text="关于")
        self._build_about_tab(about)

    def _build_main_tab(self, page: ttk.Frame):
        pad = {"padx": 6, "pady": 4}

        # 源目录
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="视频源目录：", width=12).pack(side="left")
        ttk.Entry(row, textvariable=self._src_var, width=60).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览...", command=self._browse_src).pack(
            side="left", padx=4)

        # 输出根
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="切帧输出根：", width=12).pack(side="left")
        ttk.Entry(row, textvariable=self._out_var, width=60).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览...", command=self._browse_out).pack(
            side="left", padx=4)

        # Marker 根
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="Marker 根：", width=12).pack(side="left")
        ttk.Entry(row, textvariable=self._markers_root_var, width=60).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览...", command=self._browse_markers_root).pack(
            side="left", padx=4)
        ttk.Label(page,
                  text="  抽帧锁/完成标记集中放到这里，按视频层级建镜像；"
                       "多机共享盘时所有机器指向同一位置",
                  foreground="#666").pack(anchor="w", padx=18)

        # fps
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="抽帧率(fps)：", width=12).pack(side="left")
        ttk.Spinbox(row, from_=0.1, to=60.0, increment=0.5, width=8,
                    textvariable=self._fps_var).pack(side="left")
        ttk.Label(row, text="  默认 1 = 每秒 1 帧",
                  foreground="#666").pack(side="left", padx=8)

        # 抽帧并发数
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="并发数：", width=12).pack(side="left")
        ttk.Spinbox(row, from_=1, to=32, increment=1, width=8,
                    textvariable=self._extract_jobs_var).pack(side="left")
        ttk.Label(row,
                  text="  一台机器同时抽多少个视频，默认 1，多机共享盘并发也安全",
                  foreground="#666").pack(side="left", padx=8)

        # 视频锁 TTL
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="视频锁 TTL(s)：", width=12).pack(side="left")
        ttk.Spinbox(row, from_=30, to=86400, increment=60, width=10,
                    textvariable=self._lock_ttl_var).pack(side="left")
        ttk.Label(row,
                  text="  多机共享盘的抢占锁，默认 900（15 分钟）",
                  foreground="#666").pack(side="left", padx=8)

        # 强制重切（默认不勾）：忽略已有 _done.marker，全部重抽
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Checkbutton(row, text="强制重切（忽略已完成标记，全部重抽）",
                        variable=self._force_reextract_var).pack(side="left")
        ttk.Label(row,
                  text="  极端场景使用；会覆盖已完成的视频，默认不勾选",
                  foreground="#a00").pack(side="left", padx=8)

        # 子目录多选
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="子目录多选（勾选要处理的一级子目录）：").pack(side="left")
        ttk.Button(row, text="扫描/刷新",
                   command=self._rescan_subs).pack(side="right")
        ttk.Button(row, text="全不选",
                   command=lambda: self._toggle_all(False)).pack(
            side="right", padx=4)
        ttk.Button(row, text="全选",
                   command=lambda: self._toggle_all(True)).pack(
            side="right", padx=4)

        self._sub_frame = ttk.LabelFrame(page, text="一级子目录")
        self._sub_frame.pack(fill="both", expand=True, padx=6, pady=4)

        # 进度 + 日志
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="进度：").pack(side="left")
        ttk.Label(row, textvariable=self._progress_var,
                  foreground="#0066cc").pack(side="left")
        # 日志控制条（右侧）：自动滚 + 打开日志文件夹
        ttk.Button(row, text="打开日志文件夹",
                   command=self._open_log_dir).pack(side="right", padx=4)
        ttk.Checkbutton(row, text="自动滚到底",
                        variable=self._auto_scroll_var).pack(side="right", padx=4)
        ttk.Label(row, text=f"  日志：{self._log_path.name}",
                  foreground="#888").pack(side="right", padx=4)

        # 日志区：Text + 纵/横滚动条（放在同一个 Frame 里）
        log_wrap = ttk.Frame(page)
        log_wrap.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        self._log_text = tk.Text(log_wrap, height=16,
                                 font=("Consolas", 9), wrap="none")
        self._log_vsb = ttk.Scrollbar(log_wrap, orient="vertical",
                                      command=self._on_log_scrollbar)
        self._log_hsb = ttk.Scrollbar(log_wrap, orient="horizontal",
                                      command=self._log_text.xview)
        self._log_text.configure(yscrollcommand=self._on_log_yview,
                                 xscrollcommand=self._log_hsb.set)
        self._log_text.grid(row=0, column=0, sticky="nsew")
        self._log_vsb.grid(row=0, column=1, sticky="ns")
        self._log_hsb.grid(row=1, column=0, sticky="ew")
        log_wrap.rowconfigure(0, weight=1)
        log_wrap.columnconfigure(0, weight=1)
        # 鼠标滚轮 / 键盘翻页触发时判定是否在底部（暂停自动滚）
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>",
                    "<Prior>", "<Next>", "<Up>", "<Down>",
                    "<Key-Home>", "<Key-End>"):
            self._log_text.bind(seq, self._on_log_user_scroll, add="+")
        self._log_text.config(state="disabled")

        # 载入上次日志末尾 200 行，方便接着看
        self._preload_prev_log_tail()

        # 托盘 / 快捷键
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Checkbutton(row, text="关闭时最小化到托盘",
                        variable=self._minimize_to_tray_var).pack(side="left")
        ttk.Label(row, text="   快捷键：").pack(side="left", padx=(12, 2))
        ttk.Entry(row, textvariable=self._hotkey_var, width=16).pack(side="left")
        ttk.Button(row, text="注册",
                   command=self._register_hotkey).pack(side="left", padx=4)

    def _build_about_tab(self, page: ttk.Frame):
        pad = {"padx": 12, "pady": 6}
        ttk.Label(page, text=APP_TITLE,
                  font=("Microsoft YaHei", 16, "bold")).pack(pady=(20, 4))
        ttk.Label(page, text=f"版本  {APP_VERSION}",
                  foreground="#555").pack(pady=2)
        ttk.Label(page, text=APP_COMPANY,
                  foreground="#c0392b",
                  font=("Microsoft YaHei", 10, "bold")).pack(pady=(4, 20))

        # 授权信息（占位，由 _refresh_about_license 填充）
        self._about_lic_frame = ttk.LabelFrame(page, text="授权信息")
        self._about_lic_frame.pack(fill="x", padx=12, pady=8)
        ttk.Label(self._about_lic_frame, text="加载中…",
                  foreground="#888").pack(padx=10, pady=8, anchor="w")

        # extract_frames.exe 检测
        exe = _find_extract_exe()
        if exe:
            ttk.Label(page, text=f"[√] extract_frames.exe → {exe}",
                      foreground="#3a7d3a").pack(anchor="w", **pad)
        else:
            ttk.Label(page,
                      text="[×] 未找到 extract_frames.exe（同目录 / System32 / PATH）",
                      foreground="#c0392b").pack(anchor="w", **pad)

    # ---------- 目录选择 ----------


    def _refresh_about_license(self):
        """由 main() 在拿到 license_info 后调用，把授权信息填进关于 Tab。"""
        info = getattr(self, "_license_info", None)
        frame = getattr(self, "_about_lic_frame", None)
        if frame is None:
            return
        try:
            _pg.render_license_info(self.root, frame, info)
        except Exception as e:
            for w in frame.winfo_children():
                w.destroy()
            ttk.Label(frame, text=f"（渲染失败：{e}）",
                      foreground="#c0392b").pack(padx=10, pady=8, anchor="w")

    def _browse_src(self):
        init = self._src_var.get() or os.path.expanduser("~")
        p = filedialog.askdirectory(initialdir=init, title="选择视频源目录")
        if p:
            self._src_var.set(p)
            self._rescan_subs()

    def _browse_out(self):
        init = self._out_var.get() or os.path.expanduser("~")
        p = filedialog.askdirectory(initialdir=init, title="选择切帧输出根")
        if p:
            self._out_var.set(p)

    def _browse_markers_root(self):
        init = self._markers_root_var.get() or os.path.expanduser("~")
        p = filedialog.askdirectory(initialdir=init, title="选择 Marker 根")
        if p:
            self._markers_root_var.set(p)

    def _rescan_subs(self):
        for w in self._sub_frame.winfo_children():
            w.destroy()
        self._sub_vars.clear()
        src = self._src_var.get().strip()
        if not src or not Path(src).is_dir():
            ttk.Label(self._sub_frame, text="  （请先选择有效的源目录）",
                      foreground="#999").pack(anchor="w")
            return
        try:
            subs = sorted([p.name for p in Path(src).iterdir() if p.is_dir()])
        except Exception as e:
            ttk.Label(self._sub_frame, text=f"  扫描失败：{e}",
                      foreground="#c0392b").pack(anchor="w")
            return
        if not subs:
            # 允许直接抽根目录里的视频
            ttk.Label(
                self._sub_frame,
                text="  源目录没有子目录，将直接抽帧根目录下的视频文件",
                foreground="#666").pack(anchor="w")
            return
        selected_prev = set(self._cfg.get("selected_subs", []) or [])
        for name in subs:
            v = tk.BooleanVar(value=(name in selected_prev) if selected_prev else True)
            ttk.Checkbutton(self._sub_frame, text=name, variable=v).pack(
                anchor="w", padx=4)
            self._sub_vars.append((name, v))

    def _toggle_all(self, state: bool):
        for _, v in self._sub_vars:
            v.set(state)

    # ---------- 环境检查 ----------

    def _check_environment(self):
        exe = _find_extract_exe()
        if not exe:
            messagebox.showwarning(
                "环境缺失",
                "未找到 extract_frames.exe。\n\n"
                "请把它放到本 GUI 同目录 或 C:\\Windows\\System32\\ 下。"
            )

    # ---------- 运行 ----------

    def _on_run(self):
        if self._worker_thread and self._worker_thread.is_alive():
            messagebox.showinfo("正在运行", "已经有任务在跑，请先停止或等待完成。")
            return
        src = self._src_var.get().strip()
        out = self._out_var.get().strip()
        if not src or not Path(src).is_dir():
            messagebox.showerror("参数错误", f"源目录无效：{src}"); return
        if not out:
            messagebox.showerror("参数错误", "输出根不能为空"); return

        mr = self._markers_root_var.get().strip()
        if not mr:
            messagebox.showerror("参数错误",
                                 "Marker 根不能为空。多机共享盘时所有机器应指向同一位置。")
            return

        exe = _find_extract_exe()
        if not exe:
            messagebox.showerror(
                "环境缺失",
                "未找到 extract_frames.exe，请放到本 GUI 同目录或 System32 后重试。")
            return

        # 决定要处理的目录列表：勾选的子目录 → 每个子目录一次调用；
        # 若没有子目录，直接对整个 src 跑一次
        src_p = Path(src)
        markers_root_p = Path(mr)
        if self._sub_vars:
            selected = [name for name, v in self._sub_vars if v.get()]
            if not selected:
                messagebox.showerror("参数错误", "至少勾选一个子目录"); return
            sub_pairs = [
                (src_p / name,
                 Path(out) / src_p.name / name,
                 markers_root_p / src_p.name / name)
                for name in selected
            ]
        else:
            sub_pairs = [(src_p,
                          Path(out) / src_p.name,
                          markers_root_p / src_p.name)]

        # 保存配置
        _save_config(self._dump_cfg())

        # 汇总总视频数（用于进度百分比）
        self._total_videos = 0
        for sub_src, _dst, _mr in sub_pairs:
            self._total_videos += len(_list_videos(sub_src))
        self._done_videos = 0
        if self._total_videos == 0:
            if not messagebox.askyesno(
                    "提示",
                    "扫描不到任何 .h265 / .mp4 文件，仍要启动吗？"):
                return

        # 启动后台线程
        self._worker_stop_flag.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_run, args=(exe, sub_pairs), daemon=True)
        self._worker_thread.start()
        self._run_btn.config(state="disabled")
        self._stop_btn.config(state="normal")
        self._log(f"[启动] 共 {len(sub_pairs)} 个子目录，"
                  f"预计 {self._total_videos} 个视频")

    def _on_stop(self):
        if not messagebox.askyesno("确认", "确定要停止当前抽帧任务吗？"):
            return
        self._worker_stop_flag.set()
        self._log("[停止] 已请求停止，等当前视频抽完就退出")

    def _worker_run(self, exe: str, sub_pairs: list[tuple[Path, Path, Path]]):
        """在后台线程里按 sub_pairs 顺序调 extract_frames.exe。
        stdout 实时通过 _log_queue 回传给 GUI。"""
        try:
            for sub_src, sub_dst, sub_mr in sub_pairs:
                if self._worker_stop_flag.is_set():
                    self._log("[中止] 用户请求停止，跳过剩余目录")
                    break
                sub_dst.mkdir(parents=True, exist_ok=True)
                sub_mr.mkdir(parents=True, exist_ok=True)
                self._log(f"[抽帧] {sub_src} → {sub_dst} (markers={sub_mr})")

                cmd = [exe, str(sub_src), str(sub_dst),
                       "--fps", str(float(self._fps_var.get())),
                       "--ext", ",".join(VIDEO_EXTS),
                       "--jobs", str(int(self._extract_jobs_var.get())),
                       "--lock-ttl", str(int(self._lock_ttl_var.get())),
                       "--markers-root", str(sub_mr)]
                if bool(self._force_reextract_var.get()):
                    cmd.append("--no-skip-existing")
                    self._log("[警告] 已开启强制重切：所有视频将忽略已完成标记，覆盖重抽")
                self._log(f"[命令] {' '.join(cmd)}")

                # Windows 下 CREATE_NO_WINDOW 让子进程不弹黑窗
                creationflags = 0
                if os.name == "nt":
                    creationflags = 0x08000000  # CREATE_NO_WINDOW

                try:
                    proc = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        creationflags=creationflags)
                except FileNotFoundError:
                    self._log(f"[错误] 找不到 exe：{exe}")
                    continue

                for line in proc.stdout:  # type: ignore[union-attr]
                    line = line.rstrip()
                    if line:
                        self._log(line)
                        # 检测 "抽完一个视频" 的 marker，更新进度
                        if "抽完" in line or "done.marker" in line or line.endswith("_done.marker"):
                            self._done_videos += 1
                            self._push_progress()
                    if self._worker_stop_flag.is_set():
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                        break

                rc = proc.wait()
                self._log(f"[完成] {sub_src.name}  退出码={rc}")

            if not self._worker_stop_flag.is_set():
                self._log(f"[全部完成] 共处理 {len(sub_pairs)} 个目录")
        except Exception as e:
            self._log(f"[异常] {type(e).__name__}: {e}")
        finally:
            self.root.after(0, self._on_worker_finished)

    def _on_worker_finished(self):
        self._run_btn.config(state="normal")
        self._stop_btn.config(state="disabled")
        self._progress_var.set(
            f"完成 {self._done_videos}/{self._total_videos}"
            if self._total_videos else "完成")

    def _push_progress(self):
        if self._total_videos <= 0:
            return
        pct = int(self._done_videos * 100 / self._total_videos)
        self._progress_var.set(
            f"{self._done_videos}/{self._total_videos}  ({pct}%)")

    # ---------- 日志 ----------

    def _log(self, msg: str):
        """线程安全：把日志放进队列 + 同步 append 到当前日志文件。"""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}\n"
        # 落盘（跨线程写文件加锁）
        if self._log_file is not None:
            try:
                with self._log_file_lock:
                    self._log_file.write(line)
                    self._log_file.flush()
            except Exception:
                pass
        # 主线程刷 UI
        self._log_queue.put(line)

    def _drain_log_queue(self):
        try:
            appended = False
            while True:
                line = self._log_queue.get_nowait()
                self._log_text.config(state="normal")
                self._log_text.insert("end", line)
                self._log_text.config(state="disabled")
                appended = True
        except queue.Empty:
            pass
        # 只有勾选『自动滚到底』才追到最新
        if appended and self._auto_scroll_var.get():
            self._log_text.see("end")
        self.root.after(200, self._drain_log_queue)

    # ---- 智能滚动 & 日志文件辅助 ----

    def _on_log_yview(self, first: str, last: str) -> None:
        """Text 的 yscrollcommand：同步滚动条位置 + 到底部时恢复自动滚。"""
        # 同步滚动条 UI
        try:
            self._log_vsb.set(first, last)
        except Exception:
            pass
        # 若滚回底部，自动恢复跟随
        try:
            lnum = float(last)
        except Exception:
            return
        if lnum >= 0.9999 and not self._auto_scroll_var.get():
            self._auto_scroll_var.set(True)

    def _on_log_scrollbar(self, *args) -> None:
        """用户拖动滚动条：正常滚动 + 判定是否离开底部。"""
        self._log_text.yview(*args)
        try:
            _, last = self._log_text.yview()
            if last < 0.9999:
                self._auto_scroll_var.set(False)
        except Exception:
            pass

    def _on_log_user_scroll(self, _event=None) -> None:
        """鼠标滚轮 / 键盘导致的滚动：稍后判定是否已离开底部。"""
        self.root.after(1, self._maybe_pause_autoscroll)

    def _maybe_pause_autoscroll(self) -> None:
        try:
            _, last = self._log_text.yview()
            if last < 0.9999:
                self._auto_scroll_var.set(False)
            else:
                self._auto_scroll_var.set(True)
        except Exception:
            pass

    def _preload_prev_log_tail(self) -> None:
        """启动时把上次日志末尾若干行填进 Text，方便接着看。"""
        prev = _latest_prev_log(exclude=self._log_path)
        if prev is None:
            return
        lines = _tail_lines(prev, 200)
        if not lines:
            return
        self._log_text.config(state="normal")
        self._log_text.insert("end",
            f"===== 上次日志 tail 200 行：{prev.name} =====\n")
        for line in lines:
            self._log_text.insert("end", line)
        self._log_text.insert("end",
            f"===== 上次日志结束 · 当前日志：{self._log_path.name} =====\n")
        self._log_text.see("end")
        self._log_text.config(state="disabled")

    def _open_log_dir(self) -> None:
        d = _log_dir()
        d.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(str(d))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(d)])
            else:
                subprocess.Popen(["xdg-open", str(d)])
        except Exception as e:
            self._log(f"[错误] 打开日志目录失败：{e}")

    # ---------- 配置 ----------

    def _dump_cfg(self) -> dict:
        cfg = {
            "src": self._src_var.get(),
            "out_root": self._out_var.get(),
            "fps": float(self._fps_var.get()),
            "extract_jobs": int(self._extract_jobs_var.get()),
            "lock_ttl": int(self._lock_ttl_var.get()),
            "markers_root": self._markers_root_var.get(),
            "force_reextract": bool(self._force_reextract_var.get()),
            "minimize_to_tray": bool(self._minimize_to_tray_var.get()),
            "hotkey": self._hotkey_var.get(),
            "selected_subs": [name for name, v in self._sub_vars if v.get()],
        }
        try:
            geo = self.root.winfo_geometry()
            if geo:
                cfg["window_geometry"] = geo
        except Exception:
            pass
        old = self._cfg or {}
        if "hide_close_hint" in old:
            cfg["hide_close_hint"] = old["hide_close_hint"]
        return cfg

    # ---------- 关闭 / 托盘 ----------

    def on_close(self):
        try:
            _save_config(self._dump_cfg())
        except Exception:
            pass
        if self._minimize_to_tray_var.get():
            self.hide_to_tray()
            self._maybe_show_close_hint()
        else:
            self.quit_all()

    def _maybe_show_close_hint(self):
        cfg = self._cfg or {}
        if cfg.get("hide_close_hint"):
            return
        try:
            top = tk.Toplevel(self.root)
        except Exception:
            return
        top.title(f"{APP_TITLE} 已最小化到托盘")
        top.transient(self.root)
        top.attributes("-topmost", True)
        scale = self._ui_scale
        top.geometry(_pg._scale_geometry(520, 260, scale))
        top.resizable(False, False)

        tk.Label(top, text="ⓘ  程序已最小化到系统托盘",
                 font=("Microsoft YaHei", 14, "bold"),
                 foreground="#0066cc").pack(pady=(18, 6))
        tk.Label(top, text=(
            "点右上角 × 只是把窗口收起来了，程序仍在后台运行。\n\n"
            "• 通知区域（时间旁边）有本程序图标，右键 → 退出\n"
            f"• 快捷键 {self._hotkey_var.get()} 可呼出主窗口\n\n"
            "不想最小化，请取消勾选『关闭时最小化到托盘』。"),
            font=("Microsoft YaHei", 10), justify="left",
            wraplength=int(480 * scale), foreground="#333").pack(
            padx=18, pady=(0, 8), anchor="w")

        hide_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="以后不再提示",
                        variable=hide_var).pack(anchor="w", padx=18, pady=(0, 6))

        def _confirm():
            if hide_var.get():
                self._cfg["hide_close_hint"] = True
                try:
                    _save_config(self._dump_cfg())
                except Exception:
                    pass
            top.destroy()

        ttk.Button(top, text="知道了", command=_confirm, width=12).pack(
            pady=(4, 14))
        top.protocol("WM_DELETE_WINDOW", _confirm)

    def hide_to_tray(self):
        self._start_tray_if_needed()
        self.root.withdraw()

    def show_main(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

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
        self._worker_stop_flag.set()
        self.root.destroy()
        try:
            threading.Timer(0.4, lambda: os._exit(0)).start()
        except Exception:
            os._exit(0)

    def _start_tray_if_needed(self):
        if self._tray_icon is not None:
            return
        try:
            import pystray
            from PIL import Image
        except Exception as e:
            messagebox.showwarning("托盘不可用", f"缺少 pystray/Pillow：{e}")
            return
        try:
            icon_path = _pg._resource_path("icon.png")
            img = Image.open(icon_path)
        except Exception:
            # 兜底：随手画一个
            from PIL import Image, ImageDraw
            img = Image.new("RGBA", (64, 64), (0, 102, 204, 255))
            d = ImageDraw.Draw(img)
            d.text((16, 20), "EX", fill="white")
        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口",
                             lambda: self.root.after(0, self.show_main)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出",
                             lambda: self.root.after(0, self.quit_all)),
        )
        self._tray_icon = pystray.Icon(
            "pic-clear-extract", img, APP_TITLE, menu)

        def run_tray():
            try:
                self._tray_icon.run()  # type: ignore[union-attr]
            except Exception:
                pass

        self._tray_thread = threading.Thread(target=run_tray, daemon=True)
        self._tray_thread.start()

    def _register_hotkey(self):
        try:
            import keyboard
        except Exception as e:
            messagebox.showwarning("快捷键不可用",
                                   f"缺少 keyboard 库：{e}")
            return
        try:
            if self._hotkey_registered:
                keyboard.unhook_all_hotkeys()
            hk = self._hotkey_var.get().strip() or HOTKEY_DEFAULT
            keyboard.add_hotkey(hk, lambda: self.root.after(0, self.show_main))
            self._hotkey_registered = True
            messagebox.showinfo("已注册", f"快捷键 {hk} 已注册")
        except Exception as e:
            messagebox.showerror("注册失败", f"{type(e).__name__}: {e}")


# ---------- 入口 ----------

def main() -> int:
    _pg._enable_hidpi_awareness()

    if "--diag-dpi" in sys.argv[1:]:
        return _pg._run_diag_dpi()
    if "--fingerprint" in sys.argv[1:]:
        info = _pg.probe_license_status()
        print(f"[授权] {info.get('msg', '')}")
        print(f"[授权] 本机指纹: {info.get('fingerprint', '?')}")
        return 0 if info.get("ok") else 3

    # 授权检查
    license_info = _pg.probe_license_status()
    if not license_info.get("ok"):
        if os.environ.get("PIPELINE_SKIP_LICENSE") == "1":
            print("[授权] PIPELINE_SKIP_LICENSE=1，跳过授权", flush=True)
        else:
            try:
                _pg.show_license_error_dialog(license_info)
            except Exception:
                print(f"[授权] {license_info.get('msg')}", file=sys.stderr)
            sys.exit(3)

    # ---- 动态口令（TOTP，v0.3.0 新增），未通过 sys.exit(4) ----
    _pg.require_otp_or_die()

    root = tk.Tk()
    # 先隐藏窗口，等 UI 全部构造完再一次性 deiconify，避免"小窗口闪现→变大"
    root.withdraw()
    try:
        scale = _pg._apply_dpi_scaling(root)
        root.__ui_scale__ = scale
        app = ExtractGUI(root)
        app._license_info = license_info
        try:
            app._refresh_about_license()
        except Exception:
            pass
        # 扫描默认子目录
        if app._src_var.get():
            app._rescan_subs()
        root.update_idletasks()
        root.deiconify()
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
