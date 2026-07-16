# -*- coding: utf-8 -*-
"""
dedupe_gui.py —— pic-clear 的去重独立 GUI

- 只做一件事：选一个图片目录，后台调 dedupe_pic.exe 对该目录（可选递归对
  一级子目录逐个跑）做近似去重。支持 threshold / motion-threshold /
  apply / hard-delete / scene-protect / 强制重跑忽略 marker。
- 不做抽帧：想抽帧请打开 extract_gui.exe。
- 后台线程调 subprocess，实时把 stdout 显示到日志区，进度按子目录计数。
- 托盘 + 快捷键：托盘常驻，Ctrl+Alt+D 呼出主窗口。
- DPI 自适应、图标、授权、配置文件复用 pipe_gui 的现成实现。

编译成独立的 dedupe_gui.exe，与现有 pipe_gui.exe / extract_gui.exe /
dedupe_pic.exe 完全独立。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except Exception as e:
    print(f"[FATAL] 缺少 tkinter：{e}", file=sys.stderr)
    sys.exit(1)

import pipe_gui as _pg  # noqa: E402
import pipeline  # noqa: E402


APP_TITLE = "pic-clear 去重工具"
APP_VERSION = "v0.3.0"
APP_COMPANY = "山东数旗信息科技有限公司"
CONFIG_NAME = "dedupe_gui.json"
HOTKEY_DEFAULT = "ctrl+alt+d"
DEDUP_DONE_MARKER = "_dedup_done.marker"


# ---------- 配置文件（独立于 pipe_gui.json） ----------

def _config_path() -> Path:
    return Path(os.path.expanduser("~")) / ".pic-clear" / CONFIG_NAME


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

def _find_dedupe_exe() -> str | None:
    try:
        return pipeline.resolve_worker_exe("dedupe_pic")
    except Exception:
        return None


# ---------- 图片目录扫描 ----------

def _find_dedupe_targets(root: Path, mode: str) -> list[Path]:
    """按模式返回要跑 dedupe_pic 的目录列表：
      - single: 只跑 root 本身
      - subdirs: root 下每个一级子目录跑一次
      - recursive: 递归找所有"含 jpg/jpeg/png 的最深层目录"
    """
    if not root.is_dir():
        return []
    if mode == "single":
        return [root]
    if mode == "subdirs":
        return sorted([p for p in root.iterdir() if p.is_dir()])
    # recursive
    out: set[Path] = set()
    exts = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
    for ext in exts:
        for p in root.rglob(f"*{ext}"):
            if p.is_file():
                out.add(p.parent)
    return sorted(out)


# ---------- 主 GUI ----------

class DedupeGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{APP_TITLE}  {APP_VERSION}")
        self._ui_scale = float(getattr(self.root, "__ui_scale__", 1.0))

        self._cfg = _load_config()
        saved_geo = self._cfg.get("window_geometry")
        if saved_geo:
            saved_geo = _pg._sanitize_saved_geometry(self.root, saved_geo)
        if saved_geo:
            self.root.geometry(saved_geo)
        else:
            self.root.geometry(_pg._compute_default_geometry(
                self.root, self._ui_scale,
                base_w=780, base_h=640, min_w=700, min_h=520))
        self.root.minsize(int(700 * self._ui_scale), int(520 * self._ui_scale))
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        try:
            _pg._apply_window_icon(self.root)
        except Exception:
            pass

        # 表单变量
        self._target_var = tk.StringVar(value=self._cfg.get("target", ""))
        self._mode_var = tk.StringVar(
            value=self._cfg.get("mode", "recursive"))
        self._threshold_var = tk.IntVar(
            value=int(self._cfg.get("threshold", 3)))
        self._motion_var = tk.DoubleVar(
            value=float(self._cfg.get("motion", 0.12)))
        self._apply_var = tk.BooleanVar(
            value=bool(self._cfg.get("apply", True)))
        self._hard_delete_var = tk.BooleanVar(
            value=bool(self._cfg.get("hard_delete", True)))
        self._scene_protect_var = tk.BooleanVar(
            value=bool(self._cfg.get("scene_protect", False)))
        self._force_rerun_var = tk.BooleanVar(
            value=bool(self._cfg.get("force_rerun", False)))
        self._minimize_to_tray_var = tk.BooleanVar(
            value=bool(self._cfg.get("minimize_to_tray", True)))
        self._hotkey_var = tk.StringVar(
            value=self._cfg.get("hotkey", HOTKEY_DEFAULT))
        # 并发 + 锁 TTL + Marker 根
        self._dedupe_jobs_var = tk.IntVar(
            value=int(self._cfg.get("dedupe_jobs", 1)))
        self._lock_ttl_var = tk.IntVar(
            value=int(self._cfg.get("lock_ttl", 900)))
        self._markers_root_var = tk.StringVar(
            value=self._cfg.get("markers_root", ""))

        # 保护类别（COCO 80）
        saved_pc = self._cfg.get("protect_classes")
        if isinstance(saved_pc, list):
            checked_set = set(saved_pc)
            checked_set.add("person")  # 硬保护，永远勾
        else:
            checked_set = set(_pg.COCO_DEFAULT_PROTECT)
        self._protect_class_vars: dict[str, tk.BooleanVar] = {
            name: tk.BooleanVar(value=(name in checked_set))
            for name in _pg.COCO_ZH.keys()
        }

        # 运行时状态
        self._tray_icon = None
        self._tray_thread = None
        self._hotkey_registered = False
        self._worker_thread: threading.Thread | None = None
        self._worker_stop_flag = threading.Event()
        self._log_queue: queue.Queue[str] = queue.Queue()
        self._progress_var = tk.StringVar(value="就绪")
        self._done_dirs = 0
        self._total_dirs = 0

        self._build_ui()
        self.root.after(200, self._drain_log_queue)
        self.root.after(300, self._check_environment)

    # ---------- UI ----------

    def _build_ui(self):
        _pg.apply_tab_style(self.root)
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=6)

        page = ttk.Frame(nb)
        nb.add(page, text="去重")
        self._build_main_tab(page)

        protect_page = ttk.Frame(nb)
        nb.add(protect_page, text="保护类别")
        self._build_protect_tab(protect_page)

        about = ttk.Frame(nb)
        nb.add(about, text="关于")
        self._build_about_tab(about)

        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=8, pady=(0, 8))
        self._run_btn = ttk.Button(bar, text="▶ 开始去重", command=self._on_run)
        self._run_btn.pack(side="left")
        self._stop_btn = ttk.Button(bar, text="■ 停止", command=self._on_stop,
                                    state="disabled")
        self._stop_btn.pack(side="left", padx=6)
        ttk.Button(bar, text="最小化到托盘",
                   command=self.hide_to_tray).pack(side="left", padx=6)
        ttk.Button(bar, text="退出", command=self.quit_all).pack(side="right")

    def _build_main_tab(self, page: ttk.Frame):
        pad = {"padx": 6, "pady": 4}

        # 目标目录
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="去重目标：", width=14).pack(side="left")
        ttk.Entry(row, textvariable=self._target_var, width=60).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览...", command=self._browse_target).pack(
            side="left", padx=4)

        # Marker 根
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="Marker 根：", width=14).pack(side="left")
        ttk.Entry(row, textvariable=self._markers_root_var, width=60).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览...", command=self._browse_markers_root).pack(
            side="left", padx=4)
        ttk.Label(page,
                  text="  去重锁/完成标记集中放到这里，按图片目录层级建镜像；"
                       "多机共享盘时所有机器指向同一位置",
                  foreground="#666").pack(anchor="w", padx=20)

        # 并发数 + 锁 TTL
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="并发数：", width=14).pack(side="left")
        ttk.Spinbox(row, from_=1, to=16, increment=1, width=8,
                    textvariable=self._dedupe_jobs_var).pack(side="left")
        ttk.Label(row, text="  同时跑多少个 dedupe_pic.exe，默认 1；"
                            "多机共享盘并发也安全",
                  foreground="#666").pack(side="left", padx=8)

        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="去重锁 TTL(s)：", width=14).pack(side="left")
        ttk.Spinbox(row, from_=30, to=86400, increment=60, width=10,
                    textvariable=self._lock_ttl_var).pack(side="left")
        ttk.Label(row, text="  多机共享盘时锁过期自动抢占，默认 900（15 分钟）",
                  foreground="#666").pack(side="left", padx=8)

        # 处理范围
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="处理范围：", width=14).pack(side="left")
        for label, val in [
            ("只当前目录", "single"),
            ("一级子目录", "subdirs"),
            ("递归所有图片子目录（推荐）", "recursive"),
        ]:
            ttk.Radiobutton(row, text=label, value=val,
                            variable=self._mode_var).pack(side="left", padx=4)

        # threshold
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="相似度阈值：", width=14).pack(side="left")
        ttk.Spinbox(row, from_=0, to=64, increment=1, width=8,
                    textvariable=self._threshold_var).pack(side="left")
        ttk.Label(row, text="  Hamming 距离，0=完全相同，越大越激进，"
                            "推荐 3~5",
                  foreground="#666").pack(side="left", padx=8)

        # motion
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="车辆运动阈值：", width=14).pack(side="left")
        ttk.Spinbox(row, from_=0.0, to=1.0, increment=0.01, width=8,
                    textvariable=self._motion_var,
                    format="%.2f").pack(side="left")
        ttk.Label(row, text="  相邻帧车辆位移超过该比例才判为运动，"
                            "值越小保护越激进",
                  foreground="#666").pack(side="left", padx=8)

        # 选项
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="选项：", width=14).pack(side="left")
        ttk.Checkbutton(row, text="真删除（否则只生成报告）",
                        variable=self._apply_var).pack(side="left", padx=4)
        ttk.Checkbutton(row, text="永久删除（不进回收站）",
                        variable=self._hard_delete_var).pack(side="left", padx=4)

        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="", width=14).pack(side="left")
        ttk.Checkbutton(row, text="场景保护（保留纯色/异常帧）",
                        variable=self._scene_protect_var).pack(
            side="left", padx=4)
        ttk.Checkbutton(row,
                        text=f"强制重跑（忽略 {DEDUP_DONE_MARKER}）",
                        variable=self._force_rerun_var).pack(
            side="left", padx=4)

        # 进度 + 日志
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="进度：").pack(side="left")
        ttk.Label(row, textvariable=self._progress_var,
                  foreground="#0066cc").pack(side="left")

        self._log_text = tk.Text(page, height=14, font=("Consolas", 9))
        self._log_text.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        self._log_text.config(state="disabled")

        # 托盘 / 快捷键
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Checkbutton(row, text="关闭时最小化到托盘",
                        variable=self._minimize_to_tray_var).pack(side="left")
        ttk.Label(row, text="   快捷键：").pack(side="left", padx=(12, 2))
        ttk.Entry(row, textvariable=self._hotkey_var, width=16).pack(side="left")
        ttk.Button(row, text="注册",
                   command=self._register_hotkey).pack(side="left", padx=4)

    def _build_protect_tab(self, page: ttk.Frame):
        """保护类别 Tab：COCO 80 类中文复选框。
        规则：人=硬保护；车类=运动才保护；其它=软保护但静物永远不动等同永久保留。"""
        pad = {"padx": 6, "pady": 4}

        head = ttk.Frame(page); head.pack(fill="x", **pad)
        ttk.Label(head,
                  text="保护类别（YOLO 命中即保留；人=硬保护，车=运动才保护）",
                  font=("Microsoft YaHei", 11, "bold"),
                  foreground="#0066cc").pack(anchor="w")
        ttk.Label(head, text=(
            "• 人：命中即保留，永远不删\n"
            "• 车（自行车/汽车/摩托车/公交车/火车/卡车）：相邻帧发生位移才保留，"
            "静止不动可参与相似度去重\n"
            "• 其它类别：也走『软保护』逻辑，静物永远不动即等同永久保留"),
            font=("Microsoft YaHei", 9),
            foreground="#555", justify="left").pack(anchor="w", pady=(2, 4))

        btnbar = ttk.Frame(page); btnbar.pack(fill="x", **pad)
        ttk.Button(btnbar, text="全选",
                   command=lambda: self._protect_bulk("all")).pack(
            side="left", padx=2)
        ttk.Button(btnbar, text="全不选",
                   command=lambda: self._protect_bulk("none")).pack(
            side="left", padx=2)
        ttk.Button(btnbar, text="恢复默认（7 类）",
                   command=lambda: self._protect_bulk("default")).pack(
            side="left", padx=2)
        ttk.Button(btnbar, text="仅保留人和车",
                   command=lambda: self._protect_bulk("person_and_vehicle")).pack(
            side="left", padx=2)

        # 复选框滚动区
        canvas_frame = ttk.Frame(page)
        canvas_frame.pack(fill="both", expand=True, **pad)
        canvas = tk.Canvas(canvas_frame, borderwidth=0, highlightthickness=0)
        vbar = ttk.Scrollbar(canvas_frame, orient="vertical",
                             command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_inner_config(_e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
        inner.bind("<Configure>", _on_inner_config)

        def _on_canvas_config(e):
            canvas.itemconfig(inner_id, width=e.width)
        canvas.bind("<Configure>", _on_canvas_config)

        def _on_mousewheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel, add="+")

        for group_title, class_list in _pg.COCO_GROUPS:
            gf = ttk.LabelFrame(inner, text=group_title)
            gf.pack(fill="x", padx=6, pady=4)
            cols = 4
            for idx, cname in enumerate(class_list):
                r, c = divmod(idx, cols)
                zh = _pg.COCO_ZH.get(cname, cname)
                text = f"{zh}  ({cname})"
                cb = ttk.Checkbutton(gf, text=text,
                                     variable=self._protect_class_vars[cname])
                cb.grid(row=r, column=c, sticky="w", padx=6, pady=2)
                if cname == "person":
                    self._protect_class_vars[cname].set(True)
                    cb.configure(state="disabled")

    def _protect_bulk(self, mode: str) -> None:
        """『全选/全不选/恢复默认/仅人和车』快捷按钮实现。person 永远保持勾选。"""
        if mode == "all":
            for _, v in self._protect_class_vars.items():
                v.set(True)
        elif mode == "none":
            for name, v in self._protect_class_vars.items():
                v.set(name == "person")
        elif mode == "default":
            for name, v in self._protect_class_vars.items():
                v.set(name in _pg.COCO_DEFAULT_PROTECT)
        elif mode == "person_and_vehicle":
            for name, v in self._protect_class_vars.items():
                v.set(name == "person" or name in _pg.COCO_VEHICLE_SET)

    def _get_selected_protect_arg(self) -> str:
        names = [n for n, v in self._protect_class_vars.items() if v.get()]
        if "person" not in names:
            names.insert(0, "person")
        return ",".join(names)

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

        exe = _find_dedupe_exe()
        if exe:
            ttk.Label(page, text=f"[√] dedupe_pic.exe → {exe}",
                      foreground="#3a7d3a").pack(anchor="w", **pad)
        else:
            ttk.Label(page,
                      text="[×] 未找到 dedupe_pic.exe（同目录 / System32 / PATH）",
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

    def _browse_target(self):
        init = self._target_var.get() or os.path.expanduser("~")
        p = filedialog.askdirectory(initialdir=init, title="选择去重目标目录")
        if p:
            self._target_var.set(p)

    def _browse_markers_root(self):
        init = self._markers_root_var.get() or os.path.expanduser("~")
        p = filedialog.askdirectory(initialdir=init, title="选择 Marker 根")
        if p:
            self._markers_root_var.set(p)

    # ---------- 环境检查 ----------

    def _check_environment(self):
        exe = _find_dedupe_exe()
        if not exe:
            messagebox.showwarning(
                "环境缺失",
                "未找到 dedupe_pic.exe。\n\n"
                "请把它放到本 GUI 同目录 或 C:\\Windows\\System32\\ 下。")

    # ---------- 运行 ----------

    def _on_run(self):
        if self._worker_thread and self._worker_thread.is_alive():
            messagebox.showinfo("正在运行", "已经有任务在跑，请先停止或等待完成。")
            return
        target = self._target_var.get().strip()
        if not target or not Path(target).is_dir():
            messagebox.showerror("参数错误", f"目标目录无效：{target}"); return

        exe = _find_dedupe_exe()
        if not exe:
            messagebox.showerror(
                "环境缺失",
                "未找到 dedupe_pic.exe，请放到本 GUI 同目录或 System32 后重试。")
            return

        target_p = Path(target)
        mode = self._mode_var.get()
        dirs = _find_dedupe_targets(target_p, mode)
        if not dirs:
            messagebox.showwarning("提示",
                                   "扫描不到需要去重的目录（含图片文件）。")
            return

        # markers_root 必填
        mr = self._markers_root_var.get().strip()
        if not mr:
            messagebox.showerror("配置缺失",
                                 "请先设置『Marker 根』目录。多机共享盘时所有机器应指向同一位置。")
            return
        markers_root = Path(mr)
        try:
            markers_root.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror("创建失败", f"无法创建 Marker 根目录：{e}")
            return

        # 计算 (target, marker_dir) 对：marker_dir = markers_root / rel(target - target_p)
        pairs: list[tuple[Path, Path]] = []
        for d in dirs:
            try:
                rel = d.relative_to(target_p)
            except Exception:
                rel = Path(d.name)
            marker_dir = markers_root / rel if str(rel) != "." else markers_root
            pairs.append((d, marker_dir))

        # 过滤已完成 marker（除非强制重跑）
        if not self._force_rerun_var.get():
            skipped = [(d, md) for d, md in pairs if (md / DEDUP_DONE_MARKER).exists()]
            pairs = [(d, md) for d, md in pairs if not (md / DEDUP_DONE_MARKER).exists()]
            if skipped:
                self._log(f"[跳过] {len(skipped)} 个目录已有 {DEDUP_DONE_MARKER}，"
                          "如需强制重跑请勾选左下选项")
            if not pairs:
                messagebox.showinfo(
                    "无需处理",
                    f"所有目录在 markers 里都已有 {DEDUP_DONE_MARKER}。\n"
                    "如果想强制重跑，请勾选『强制重跑』后再点开始。")
                return

        # 保存配置
        _save_config(self._dump_cfg())

        self._total_dirs = len(pairs)
        self._done_dirs = 0
        self._push_progress()

        # 启动后台线程
        self._worker_stop_flag.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_run, args=(exe, pairs), daemon=True)
        self._worker_thread.start()
        self._run_btn.config(state="disabled")
        self._stop_btn.config(state="normal")
        jobs = int(self._dedupe_jobs_var.get())
        self._log(f"[启动] 共 {len(pairs)} 个目录待去重，并发={jobs}")

    def _on_stop(self):
        if not messagebox.askyesno("确认", "确定要停止当前去重任务吗？"):
            return
        self._worker_stop_flag.set()
        self._log("[停止] 已请求停止，等当前目录跑完就退出")

    def _worker_run(self, exe: str, pairs: list[tuple[Path, Path]]):
        """并发跑 dedupe_pic.exe，每个目录一个子进程。

        pairs: [(target_dir, marker_dir), ...]
        marker_dir 由外层算好（markers_root 下对应的镜像位置），
        dedupe_pic.exe 靠 --marker-dir 抢 _dedup.lock、写 _dedup_done.marker。
        """
        from concurrent.futures import ThreadPoolExecutor
        jobs = max(1, int(self._dedupe_jobs_var.get()))
        lock_ttl = int(self._lock_ttl_var.get())
        force = bool(self._force_rerun_var.get())
        protect_arg = self._get_selected_protect_arg()
        # 完成一个记一个的锁（tk 主线程 after 也够，用 python threading.Lock）
        done_lock = threading.Lock()

        def _run_one(pair: tuple[Path, Path]) -> tuple[Path, int]:
            d, marker_dir = pair
            if self._worker_stop_flag.is_set():
                return d, -1
            tag = d.name
            self._log(f"[{tag}] 开始（marker={marker_dir}）")
            cmd = [exe, str(d),
                   "--threshold", str(int(self._threshold_var.get())),
                   "--motion-threshold", str(float(self._motion_var.get())),
                   "--marker-dir", str(marker_dir),
                   "--lock-ttl", str(lock_ttl)]
            if self._apply_var.get():
                cmd.append("--apply")
            if self._hard_delete_var.get():
                cmd.append("--hard-delete")
            if self._scene_protect_var.get():
                cmd.append("--scene-protect")
            if force:
                cmd.append("--force")
            if protect_arg:
                cmd.extend(["--protect", protect_arg])

            creationflags = 0
            if os.name == "nt":
                creationflags = 0x08000000  # CREATE_NO_WINDOW
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=creationflags)
            except FileNotFoundError:
                self._log(f"[{tag}] [错误] 找不到 exe：{exe}")
                return d, 127

            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.rstrip()
                if line:
                    self._log(f"[{tag}] {line}")
                if self._worker_stop_flag.is_set():
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    break
            rc = proc.wait()
            self._log(f"[{tag}] 完成 rc={rc}")
            with done_lock:
                self._done_dirs += 1
            self._push_progress()
            return d, rc

        try:
            with ThreadPoolExecutor(max_workers=jobs,
                                    thread_name_prefix="dedupe") as ex:
                futures = [ex.submit(_run_one, p) for p in pairs]
                # 若停止旗触发，取消未开始的
                for fut in futures:
                    try:
                        fut.result()
                    except Exception as e:
                        self._log(f"[异常] {type(e).__name__}: {e}")
                    if self._worker_stop_flag.is_set():
                        for f in futures:
                            f.cancel()
            if not self._worker_stop_flag.is_set():
                self._log(f"[全部完成] 共处理 {len(pairs)} 个目录")
        except Exception as e:
            self._log(f"[异常] {type(e).__name__}: {e}")
        finally:
            self.root.after(0, self._on_worker_finished)

    def _on_worker_finished(self):
        self._run_btn.config(state="normal")
        self._stop_btn.config(state="disabled")

    def _push_progress(self):
        if self._total_dirs <= 0:
            self._progress_var.set("就绪")
            return
        pct = int(self._done_dirs * 100 / self._total_dirs)
        self._progress_var.set(
            f"{self._done_dirs}/{self._total_dirs}  ({pct}%)")

    # ---------- 日志 ----------

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_queue.put(f"[{ts}] {msg}\n")

    def _drain_log_queue(self):
        try:
            while True:
                line = self._log_queue.get_nowait()
                self._log_text.config(state="normal")
                self._log_text.insert("end", line)
                self._log_text.see("end")
                self._log_text.config(state="disabled")
        except queue.Empty:
            pass
        self.root.after(200, self._drain_log_queue)

    # ---------- 配置 ----------

    def _dump_cfg(self) -> dict:
        cfg = {
            "target": self._target_var.get(),
            "mode": self._mode_var.get(),
            "threshold": int(self._threshold_var.get()),
            "motion": float(self._motion_var.get()),
            "apply": bool(self._apply_var.get()),
            "hard_delete": bool(self._hard_delete_var.get()),
            "scene_protect": bool(self._scene_protect_var.get()),
            "force_rerun": bool(self._force_rerun_var.get()),
            "dedupe_jobs": int(self._dedupe_jobs_var.get()),
            "lock_ttl": int(self._lock_ttl_var.get()),
            "markers_root": self._markers_root_var.get(),
            "minimize_to_tray": bool(self._minimize_to_tray_var.get()),
            "hotkey": self._hotkey_var.get(),
            "protect_classes": [n for n, v in
                                self._protect_class_vars.items() if v.get()],
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
            from PIL import Image, ImageDraw
            img = Image.new("RGBA", (64, 64), (192, 57, 43, 255))
            d = ImageDraw.Draw(img)
            d.text((16, 20), "DE", fill="white")
        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口",
                             lambda: self.root.after(0, self.show_main)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出",
                             lambda: self.root.after(0, self.quit_all)),
        )
        self._tray_icon = pystray.Icon(
            "pic-clear-dedupe", img, APP_TITLE, menu)

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
        app = DedupeGUI(root)
        app._license_info = license_info
        try:
            app._refresh_about_license()
        except Exception:
            pass
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
