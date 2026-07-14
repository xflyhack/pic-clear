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


# =========================================================================
# 配置持久化：记住上次的选择，下次打开自动填回来
# =========================================================================

import json


def _config_path() -> Path:
    r"""配置文件路径：%USERPROFILE%\.pic-clear\pipe_gui.json（跨用户/机器隔离）"""
    home = Path(os.path.expanduser("~"))
    return home / ".pic-clear" / "pipe_gui.json"


def _load_config() -> dict:
    """加载上次的配置。文件不存在 / 损坏 → 返回空 dict，走默认。"""
    p = _config_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config(cfg: dict) -> None:
    """保存配置。失败不抛异常（不能因为存配置失败影响主流程）。"""
    try:
        p = _config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as e:
        print(f"[WARN] 保存配置失败: {e}", file=sys.stderr)


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
# 一键杀死所有相关进程
# =========================================================================

# 会被清理的进程名（自己 pipe_gui.exe 只杀"其他实例"，当前实例排除）
KILL_TARGETS = ["pipeline.exe", "extract_frames.exe", "dedupe_pic.exe", "pipe_gui.exe"]


def kill_all_related(exclude_pids: list[int]) -> tuple[list[tuple[str, int, bool]], int]:
    """按名字杀掉所有 KILL_TARGETS，跳过 exclude_pids 里的 PID。

    返回 (results, killed_count)：
      results: [(name, pid, ok), ...] 每个尝试过的进程
      killed_count: 成功杀掉的进程数
    """
    results: list[tuple[str, int, bool]] = []
    killed = 0
    if os.name != "nt":
        # 非 Windows 场景：kill -9
        procs = query_processes()
        for name, info in procs.items():
            for pid in info["pids"]:
                if pid in exclude_pids:
                    continue
                try:
                    os.kill(pid, 9)
                    results.append((name, pid, True))
                    killed += 1
                except Exception:
                    results.append((name, pid, False))
        return results, killed

    # Windows：先查 PID 列表，逐个 taskkill /F /PID，跳过自己
    procs = query_processes()
    for name in KILL_TARGETS:
        info = procs.get(name)
        if not info or not info["running"]:
            continue
        for pid in info["pids"]:
            if pid in exclude_pids:
                continue
            try:
                rc = subprocess.call(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    creationflags=0x08000000,  # CREATE_NO_WINDOW
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                ok = (rc == 0)
                results.append((name, pid, ok))
                if ok:
                    killed += 1
            except Exception:
                results.append((name, pid, False))
    return results, killed


def mark_latest_job_stopped(out_root: Path) -> str | None:
    """把最新一个还在 running/pending 的 job 状态改成 stopped，返回 job_id。"""
    st = find_latest_job(out_root)
    if not st:
        return None
    if st.state not in ("pending", "running"):
        return None
    st.state = "stopped"
    st.ended_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st.last_message = "被『杀死所有进程』强制停止"
    try:
        # jobs 目录路径
        job_dir = out_root / ".pipeline" / "jobs" / st.job_id
        pipeline._save_status(job_dir, st)
        return st.job_id
    except Exception as e:
        print(f"[WARN] 更新 status.json 失败: {e}", file=sys.stderr)
        return None


# =========================================================================
# 主 GUI
# =========================================================================


# --- Tooltip 浮层 -----------------------------------------------------------

class _Tooltip:
    """轻量 tooltip：鼠标悬停 delay 毫秒后弹一个黄底 Toplevel，移开立即隐藏。
    不依赖三方库，Windows tkinter 原生够用。"""

    def __init__(self, widget: "tk.Widget", text: str, delay_ms: int = 350):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        widget.bind("<ButtonPress>", self._on_leave, add="+")

    def _on_enter(self, _event=None):
        self._cancel_pending()
        try:
            self._after_id = self.widget.after(self.delay_ms, self._show)
        except Exception:
            self._after_id = None

    def _on_leave(self, _event=None):
        self._cancel_pending()
        self._hide()

    def _cancel_pending(self):
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self):
        if self._tip is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 16
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        except Exception:
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)  # 无边框
        try:
            tip.wm_attributes("-topmost", True)
        except Exception:
            pass
        tip.wm_geometry(f"+{x}+{y}")
        # 黄底黑字，跟系统 tooltip 观感一致
        lbl = tk.Label(
            tip, text=self.text, justify="left",
            background="#ffffe0", foreground="#111",
            relief="solid", borderwidth=1,
            font=("Microsoft YaHei", 9),
            wraplength=360, padx=6, pady=3,
        )
        lbl.pack()
        self._tip = tip

    def _hide(self):
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


def _add_tip(widget: "tk.Widget", text: str) -> None:
    """给任意 widget 挂一个 tooltip（重复调用会重复挂，谨慎）。"""
    _Tooltip(widget, text)


def _tip_icon(parent: "tk.Widget", text: str) -> "tk.Label":
    """返回一个可以 pack/grid 的 ⓘ 图标 Label，鼠标悬停时展示 text。
    调用方负责把返回值 pack / grid 到合适位置。"""
    lbl = tk.Label(parent, text="ⓘ", foreground="#0066cc",
                   font=("Segoe UI", 10, "bold"), cursor="question_arrow")
    _Tooltip(lbl, text)
    return lbl


class PipeGUI:
    APP_TITLE = "pic-clear 图形界面"
    APP_VERSION = "v0.1.2"
    APP_COMPANY = "山东数旗信息科技有限公司"
    REFRESH_MS = 5000

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{self.APP_TITLE}  {self.APP_VERSION}")
        self.root.geometry("820x720")
        self.root.minsize(760, 560)
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
        # 新增：跟 run_all.bat 对齐的三个参数
        self._scene_protect_var = tk.BooleanVar(value=True)
        self._daily_remain_limit_var = tk.IntVar(value=80000)
        self._watch_interval_var = tk.DoubleVar(value=3.0)
        self._hotkey_var = tk.StringVar(value="ctrl+alt+p")

        self._sub_vars: list[tuple[str, tk.BooleanVar]] = []
        # 子目录进度 Label 引用：name -> Label
        self._sub_progress_labels: dict[str, "ttk.Label"] = {}
        # 汇总条 StringVar（顶部一句话概览）
        self._summary_var = tk.StringVar(value="空闲")
        # 日志窗
        self._log_toplevel: "tk.Toplevel | None" = None
        self._log_widgets: dict = {}

        # 加载上次的配置（如果有），下面填进各个 tk 变量
        self._loaded_config = _load_config()

        self._build_ui()
        self._refresh_env()

        # 有历史配置就用历史配置；否则走原自动逻辑（挑盘 + 猜 sjbz_*）
        if self._loaded_config:
            self._apply_loaded_config()
        else:
            self._auto_pick_drive()

        # 启动主按钮的定期状态刷新（run <-> stop）
        self._refresh_action_button()
        self.root.after(5000, self._schedule_action_refresh)

    # ---------- UI 布局 ----------

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # ------------------- Notebook（顶层 Tab） -------------------
        self._nb = ttk.Notebook(self.root)
        self._nb.pack(fill="both", expand=True, padx=8, pady=(6, 2))

        tab_home = ttk.Frame(self._nb)
        tab_dedup = ttk.Frame(self._nb)
        tab_extract = ttk.Frame(self._nb)
        tab_bg = ttk.Frame(self._nb)
        tab_about = ttk.Frame(self._nb)
        self._nb.add(tab_home, text="  主页  ")
        self._nb.add(tab_dedup, text="  去重参数  ")
        self._nb.add(tab_extract, text="  抽帧 & 编排  ")
        self._nb.add(tab_bg, text="  后台 & 快捷键  ")
        self._nb.add(tab_about, text="  关于  ")

        # ============= Tab 1：主页 =============
        self._build_tab_home(tab_home, pad)

        # ============= Tab 2：去重参数 =============
        self._build_tab_dedup(tab_dedup, pad)

        # ============= Tab 3：抽帧 & 编排 =============
        self._build_tab_extract(tab_extract, pad)

        # ============= Tab 4：后台 & 快捷键 =============
        self._build_tab_bg(tab_bg, pad)

        # ============= Tab 5：关于 =============
        self._build_tab_about(tab_about, pad)

        # 底部按钮（挂在 root 上，永远常驻，跟 Tab 无关）
        f_btn = ttk.Frame(self.root); f_btn.pack(fill="x", **pad)
        # 主动作按钮：空闲=运行，有任务=停止（run_or_stop 会根据最新 job 状态动态切）
        self._action_btn = ttk.Button(f_btn, text="▶ 运行", command=self._on_action_click, width=16)
        self._action_btn.pack(side="left", padx=4)
        self._action_state = "run"  # "run" | "stop"
        ttk.Button(f_btn, text="一键杀死所有", command=self._on_kill_all, width=14).pack(side="left", padx=4)
        ttk.Button(f_btn, text="查看状态", command=self.show_status_window, width=14).pack(side="left", padx=4)
        ttk.Button(f_btn, text="查看日志", command=self.show_log_window, width=14).pack(side="left", padx=4)
        ttk.Button(f_btn, text="最小化到托盘", command=self.hide_to_tray, width=14).pack(side="left", padx=4)
        ttk.Button(f_btn, text="退出", command=self.quit_all, width=10).pack(side="right", padx=4)
        ttk.Button(f_btn, text="重置配置", command=self._on_reset_config, width=10).pack(side="right", padx=4)

    # ------------------- 各 Tab 的内容 -------------------

    def _build_tab_home(self, tab: "ttk.Frame", pad: dict):
        """主页：环境检测 + 数据配置 + 子目录（含汇总条+进度列）"""
        # 环境检测
        f_env = ttk.LabelFrame(tab, text="▶ 环境检测")
        f_env.pack(fill="x", **pad)
        self._env_text = tk.Text(f_env, height=4, width=90, font=("Consolas", 9))
        self._env_text.pack(fill="x", padx=6, pady=4)
        self._env_text.config(state="disabled")

        # 数据配置
        f_data = ttk.LabelFrame(tab, text="▶ 数据配置")
        f_data.pack(fill="x", **pad)

        row = ttk.Frame(f_data); row.pack(fill="x", padx=6, pady=3)
        ttk.Label(row, text="数据盘：", width=10).pack(side="left")
        self._drive_combo = ttk.Combobox(row, textvariable=self._drive_var,
                                         values=list_drives(), width=8, state="readonly")
        self._drive_combo.pack(side="left")
        _tip_icon(row, "选择要处理数据所在的盘符。堡垒机常用 Z: 网络挂载盘；本地测试用 C:/D:").pack(side="left", padx=(6, 0))
        ttk.Button(row, text="刷新盘符", command=self._refresh_drives).pack(side="left", padx=6)
        self._drive_combo.bind("<<ComboboxSelected>>", lambda e: self._on_drive_change())

        row = ttk.Frame(f_data); row.pack(fill="x", padx=6, pady=3)
        ttk.Label(row, text="源目录：", width=10).pack(side="left")
        ttk.Entry(row, textvariable=self._src_var, width=60).pack(side="left", fill="x", expand=True)
        _tip_icon(row, "要处理的视频/图片根目录。可以是 Z:\\sjbz_20260708 这种一天数据的根").pack(side="left", padx=(6, 0))
        ttk.Button(row, text="浏览...", command=self._browse_src).pack(side="left", padx=6)

        row = ttk.Frame(f_data); row.pack(fill="x", padx=6, pady=3)
        ttk.Label(row, text="输出根：", width=10).pack(side="left")
        ttk.Entry(row, textvariable=self._out_var, width=60).pack(side="left", fill="x", expand=True)
        _tip_icon(row, "抽帧结果和 job 状态放这里，默认 <数据盘>\\切帧结果").pack(side="left", padx=(6, 0))
        ttk.Button(row, text="浏览...", command=self._browse_out).pack(side="left", padx=6)

        # 子目录选择（本页核心）
        f_subs = ttk.LabelFrame(tab, text="▶ 子目录（勾选要处理的，右侧显示实时进度）")
        f_subs.pack(fill="both", expand=True, **pad)
        # 汇总条：顶部一行，展示最新 job 的进度
        summary_row = ttk.Frame(f_subs)
        summary_row.pack(fill="x", padx=6, pady=(3, 0))
        ttk.Label(summary_row, text="当前任务：", foreground="#555").pack(side="left")
        ttk.Label(summary_row, textvariable=self._summary_var,
                  foreground="#0057b7").pack(side="left", padx=4)
        top = ttk.Frame(f_subs); top.pack(fill="x", padx=6, pady=3)
        ttk.Button(top, text="扫描/刷新", command=self._rescan_subs).pack(side="left")
        ttk.Button(top, text="全选", command=lambda: self._sub_toggle_all(True)).pack(side="left", padx=6)
        ttk.Button(top, text="全不选", command=lambda: self._sub_toggle_all(False)).pack(side="left")

        self._subs_canvas_frame = ttk.Frame(f_subs)
        self._subs_canvas_frame.pack(fill="both", expand=True, padx=6, pady=3)
        self._subs_canvas = tk.Canvas(self._subs_canvas_frame, height=180, highlightthickness=0)
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

    def _build_tab_dedup(self, tab: "ttk.Frame", pad: dict):
        """去重参数 tab：相似度、车运动、场景保护、真删除、日限"""
        f = ttk.LabelFrame(tab, text="▶ 相似度")
        f.pack(fill="x", **pad)
        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=6)
        _tip_icon(row, "两张图差异 ≤ 该值就当作近似重复。\n"
                       "0 = 只删完全一样\n"
                       "3 = 严格（几乎肉眼一样，推荐首次用）\n"
                       "5 = 默认，轻微压缩/裁边算相同\n"
                       "10 = 宽松，构图相似即合并，可能误伤").pack(side="left", padx=(0, 4))
        ttk.Label(row, text="相似度阈值 (-t)：", width=18).pack(side="left")
        ttk.Spinbox(row, from_=0, to=32, textvariable=self._threshold_var, width=6).pack(side="left")

        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=6)
        _tip_icon(row, "同一目录相邻帧比较，车中心位移 > 该值 × max(W,H) 就判定'在动'，保留。\n"
                       "越小越灵敏（车抖动就当动了），越大越钝。\n"
                       "默认 0.12；车辆序列推荐 0.02~0.05；大幅移动镜头用 0.10+").pack(side="left", padx=(0, 4))
        ttk.Label(row, text="车运动阈值 (-m)：", width=18).pack(side="left")
        ttk.Spinbox(row, from_=0.0, to=1.0, increment=0.01,
                    textvariable=self._motion_var, width=6, format="%.2f").pack(side="left")

        f = ttk.LabelFrame(tab, text="▶ 保护策略")
        f.pack(fill="x", **pad)
        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=4)
        _tip_icon(row, "开启后把'纯色屏 / 渐变屏'这类传感器遮挡的异常帧识别出来强制保留。\n"
                       "推荐开——异常帧留着做故障排查很有用").pack(side="left", padx=(0, 4))
        ttk.Checkbutton(row, text="场景保护 -S（推荐开）",
                        variable=self._scene_protect_var).pack(side="left")

        f = ttk.LabelFrame(tab, text="▶ 删除策略")
        f.pack(fill="x", **pad)
        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=4)
        _tip_icon(row, "不勾 = dry-run，只在每个子目录写 dedupe_report.csv 供检查，不动任何文件。\n"
                       "勾上 = 按 CSV 里的 DELETE 行真的删掉/移走").pack(side="left", padx=(0, 4))
        ttk.Checkbutton(row, text="真删除（-y，取消勾选则 dry-run）",
                        variable=self._apply_var).pack(side="left")
        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=4)
        _tip_icon(row, "只在'真删除'勾上时生效：\n"
                       "  不勾 = 移到目标目录下的 _trash/ 子文件夹，可回滚（占额外磁盘）\n"
                       "  勾上 = 直接 os.remove，不可恢复（磁盘干净）").pack(side="left", padx=(0, 4))
        ttk.Checkbutton(row, text="永久删除，不落 _trash（-H）",
                        variable=self._hard_delete_var).pack(side="left")

        f = ttk.LabelFrame(tab, text="▶ 安全阀")
        f.pack(fill="x", **pad)
        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=6)
        _tip_icon(row, "当日累计'剩余'张数达到该值，pipeline 自动停止，防止把有用图删过头。\n"
                       "0 = 禁用（不推荐）；默认 80000").pack(side="left", padx=(0, 4))
        ttk.Label(row, text="当日剩余上限 (-L)：", width=20).pack(side="left")
        ttk.Spinbox(row, from_=0, to=100000000, increment=1000,
                    textvariable=self._daily_remain_limit_var, width=12).pack(side="left")

    def _build_tab_extract(self, tab: "ttk.Frame", pad: dict):
        """抽帧 & 编排 tab：fps + watcher 扫描秒"""
        f = ttk.LabelFrame(tab, text="▶ 抽帧")
        f.pack(fill="x", **pad)
        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=6)
        _tip_icon(row, "视频每秒抽多少帧。默认 1.0 表示 1 秒 1 帧。\n"
                       "值越大图片越多、覆盖越全；也越占磁盘和 CPU。\n"
                       "堡垒机场景不建议 > 5").pack(side="left", padx=(0, 4))
        ttk.Label(row, text="抽帧 fps：", width=14).pack(side="left")
        ttk.Spinbox(row, from_=0.1, to=30.0, increment=0.5,
                    textvariable=self._fps_var, width=6, format="%.1f").pack(side="left")

        f = ttk.LabelFrame(tab, text="▶ 编排")
        f.pack(fill="x", **pad)
        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=6)
        _tip_icon(row, "去重 watcher 多久扫一次 _done.marker（抽帧完成标记）。\n"
                       "越小越及时（视频抽完立刻开始去重），但更耗 CPU。\n"
                       "默认 3.0 秒够用").pack(side="left", padx=(0, 4))
        ttk.Label(row, text="watcher 扫描秒：", width=18).pack(side="left")
        ttk.Spinbox(row, from_=0.5, to=60.0, increment=0.5,
                    textvariable=self._watch_interval_var, width=6, format="%.1f").pack(side="left")

        # 说明性小字
        note = ttk.Label(
            tab, foreground="#666", justify="left",
            text=("提示：抽帧和去重是并行的。主线程串行抽帧，watcher 在后台\n"
                  "每 N 秒扫一次，看到 _done.marker 就立刻起 dedupe，视频粒度\n"
                  "即抽即删，中间不会堆积占磁盘。"),
        )
        note.pack(fill="x", padx=14, pady=(4, 8), anchor="w")

    def _build_tab_bg(self, tab: "ttk.Frame", pad: dict):
        """后台 & 快捷键 tab"""
        f = ttk.LabelFrame(tab, text="▶ 窗口行为")
        f.pack(fill="x", **pad)
        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=4)
        _tip_icon(row, "关闭主窗口时最小化到系统托盘（右下角图标），进程不退出、任务继续跑。\n"
                       "想真正退出：托盘图标右键 → 退出，或主窗底部『退出』按钮").pack(side="left", padx=(0, 4))
        ttk.Checkbutton(row, text="点 × 时最小化到托盘（不退出）",
                        variable=self._minimize_to_tray_var).pack(side="left")
        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=4)
        _tip_icon(row, "pipeline 后台已内建 CREATE_NO_WINDOW，子进程（extract_frames/dedupe_pic）\n"
                       "不会弹黑窗口。此项只是提示，勾不勾都不影响").pack(side="left", padx=(0, 4))
        ttk.Checkbutton(row, text="隐藏所有子进程黑窗口（pipeline 已内建，此项仅提示）",
                        variable=self._hide_children_var).pack(side="left")

        f = ttk.LabelFrame(tab, text="▶ 全局快捷键")
        f.pack(fill="x", **pad)
        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=6)
        _tip_icon(row, "GUI 缩到托盘后按此快捷键把状态浮层拉出来。\n"
                       "格式 ctrl+alt+p / ctrl+shift+F1 之类。\n"
                       "部分堡垒机 RDP 会话会拦截全局钩子，用不了就用托盘图标呼出").pack(side="left", padx=(0, 4))
        ttk.Label(row, text="呼出快捷键：", width=14).pack(side="left")
        ttk.Entry(row, textvariable=self._hotkey_var, width=20).pack(side="left")
        ttk.Button(row, text="注册快捷键", command=self._register_hotkey).pack(side="left", padx=6)

        # 提示语
        note = ttk.Label(
            tab, foreground="#666", justify="left",
            text=("提示：所有 Tab 里的选项（包括源目录/输出根/勾选过的子目录）\n"
                  "下次启动会自动填回来。想清零就按底部『重置配置』按钮。"),
        )
        note.pack(fill="x", padx=14, pady=(4, 8), anchor="w")

    def _build_tab_about(self, tab: "ttk.Frame", pad: dict):
        """关于 tab：公司 + 版本 + 版权声明"""
        # 中央容器，垂直居中
        wrap = ttk.Frame(tab)
        wrap.place(relx=0.5, rely=0.5, anchor="center")

        ttk.Label(wrap, text="pic-clear 图形界面",
                  font=("Microsoft YaHei", 18, "bold"),
                  foreground="#222").pack(pady=(0, 6))

        ttk.Label(wrap, text=f"版本  {self.APP_VERSION}",
                  font=("Microsoft YaHei", 11),
                  foreground="#555").pack(pady=(0, 20))

        ttk.Separator(wrap, orient="horizontal").pack(fill="x", pady=(0, 16))

        ttk.Label(wrap, text=self.APP_COMPANY,
                  font=("Microsoft YaHei", 13, "bold"),
                  foreground="#c0392b").pack(pady=(0, 4))
        ttk.Label(wrap, text="版权所有  ·  All rights reserved.",
                  font=("Microsoft YaHei", 9),
                  foreground="#888").pack(pady=(0, 20))

        ttk.Label(wrap, foreground="#666", justify="center",
                  text=("图片近似去重  ·  YOLO 目标保护  ·  H265/MP4 抽帧  ·  编排后台\n"
                        "共 6 个独立 exe：extract_frames / dedupe_pic / pipeline\n"
                        "pipe_gui / summary_stats_gui / gen_license_gui")
                  ).pack(pady=(0, 6))

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

    def _apply_loaded_config(self):
        """把 _loaded_config 里的值填到各个 tk 变量。找不到的字段回落默认。"""
        cfg = self._loaded_config or {}

        # 数据盘：先看历史值是否还存在
        drives = list_drives()
        drv = cfg.get("data_drive")
        if drv and drv in drives:
            self._drive_var.set(drv)
        elif "Z:" in drives:
            self._drive_var.set("Z:")
        elif drives:
            self._drive_var.set(drives[0])

        # 源目录：历史值仍存在就填
        src = cfg.get("src", "")
        if src and Path(src).is_dir():
            self._src_var.set(src)

        # 输出根：历史值就填（可以不存在，用户可能想创建）
        out = cfg.get("out_root", "")
        if out:
            self._out_var.set(out)
        elif self._drive_var.get():
            self._out_var.set(str(pipeline.default_out_root(self._drive_var.get())))

        # 各种阈值
        if "threshold" in cfg:
            try: self._threshold_var.set(int(cfg["threshold"]))
            except Exception: pass
        if "motion" in cfg:
            try: self._motion_var.set(float(cfg["motion"]))
            except Exception: pass
        if "fps" in cfg:
            try: self._fps_var.set(float(cfg["fps"]))
            except Exception: pass

        # 复选框
        if "apply" in cfg:
            self._apply_var.set(bool(cfg["apply"]))
        if "hard_delete" in cfg:
            self._hard_delete_var.set(bool(cfg["hard_delete"]))
        if "minimize_to_tray" in cfg:
            self._minimize_to_tray_var.set(bool(cfg["minimize_to_tray"]))
        if "scene_protect" in cfg:
            self._scene_protect_var.set(bool(cfg["scene_protect"]))
        if "daily_remain_limit" in cfg:
            try: self._daily_remain_limit_var.set(int(cfg["daily_remain_limit"]))
            except Exception: pass
        if "watch_interval" in cfg:
            try: self._watch_interval_var.set(float(cfg["watch_interval"]))
            except Exception: pass

        # 快捷键
        if cfg.get("hotkey"):
            self._hotkey_var.set(cfg["hotkey"])

        # 源目录填好了就顺手扫一次子目录（并勾选上次选过的）
        if src and Path(src).is_dir():
            self._rescan_subs()
            last_selected = set(cfg.get("selected_subs", []))
            if last_selected:
                for name, var in self._sub_vars:
                    var.set(name in last_selected)

    def _dump_current_config(self) -> dict:
        """把当前表单状态收集成 dict，用于保存。"""
        return {
            "data_drive": self._drive_var.get(),
            "src": self._src_var.get(),
            "out_root": self._out_var.get(),
            "threshold": int(self._threshold_var.get()),
            "motion": float(self._motion_var.get()),
            "fps": float(self._fps_var.get()),
            "apply": bool(self._apply_var.get()),
            "hard_delete": bool(self._hard_delete_var.get()),
            "minimize_to_tray": bool(self._minimize_to_tray_var.get()),
            "hotkey": self._hotkey_var.get(),
            "scene_protect": bool(self._scene_protect_var.get()),
            "daily_remain_limit": int(self._daily_remain_limit_var.get()),
            "watch_interval": float(self._watch_interval_var.get()),
            "selected_subs": [name for name, v in self._sub_vars if v.get()],
        }

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
        self._sub_progress_labels.clear()
        src = self._src_var.get()
        if not src:
            return
        subs = list_subdirs(Path(src))
        if not subs:
            ttk.Label(self._subs_inner, text="（该目录下没有子目录，将直接处理整个目录）",
                      foreground="gray").pack(anchor="w")
            return
        # 单列布局：左边勾选 + 目录名，右边进度 Label
        for i, name in enumerate(subs):
            var = tk.BooleanVar(value=True)
            row = ttk.Frame(self._subs_inner)
            row.grid(row=i, column=0, sticky="ew", padx=4, pady=1)
            row.columnconfigure(0, weight=1)
            cb = ttk.Checkbutton(row, text=name, variable=var)
            cb.grid(row=0, column=0, sticky="w")
            prog = ttk.Label(row, text="—", foreground="#888",
                             font=("Consolas", 9), width=42, anchor="e")
            prog.grid(row=0, column=1, sticky="e", padx=(8, 4))
            self._sub_vars.append((name, var))
            self._sub_progress_labels[name] = prog
        # 让 subs_inner 撑满 canvas 宽度，进度列才能贴右
        self._subs_inner.columnconfigure(0, weight=1)

    def _sub_toggle_all(self, value: bool):
        for _, v in self._sub_vars:
            v.set(value)

    # ---------- 运行/停止 ----------

    def _on_action_click(self):
        """主按钮点击：根据当前状态分发到 _on_run 或 _on_stop。"""
        if self._action_state == "stop":
            self._on_stop()
        else:
            self._on_run()

    def _refresh_action_button(self):
        """按最新 job 状态切换主按钮的文案和绑定动作。

        - job 是 pending/running → 显示『■ 停止当前任务』
        - 其他（done/failed/stopped/无任务） → 显示『▶ 运行』
        """
        try:
            state = self._probe_running_state()
        except Exception:
            state = "run"
        if state == "stop":
            if self._action_state != "stop":
                self._action_btn.configure(text="■ 停止当前任务")
                self._action_state = "stop"
        else:
            if self._action_state != "run":
                self._action_btn.configure(text="▶ 运行")
                self._action_state = "run"

    def _probe_running_state(self) -> str:
        """判断是否有任务正在跑，返回 'stop' 或 'run'。"""
        out = self._out_var.get().strip()
        if not out:
            return "run"
        try:
            root = Path(out)
        except Exception:
            return "run"
        if not root.is_dir():
            return "run"
        st = find_latest_job(root)
        if not st:
            return "run"
        # 状态为 pending/running 且 worker 还活着才算"在跑"
        if st.state in ("pending", "running") and pipeline._process_alive(st.pid):
            return "stop"
        return "run"

    def _schedule_action_refresh(self):
        """每 5 秒轮询一次，自动切换按钮文案。"""
        try:
            self._refresh_action_button()
            self._refresh_progress_panel()
        finally:
            # 只要主窗口还在就继续轮询
            try:
                if self.root.winfo_exists():
                    self.root.after(5000, self._schedule_action_refresh)
            except Exception:
                pass

    def _refresh_progress_panel(self):
        """把最新 job 的进度铺到主窗：汇总条 + 每个子目录一行 Label。
        没任务或输出根找不到，就把进度列清空、汇总条显示空闲。"""
        # 默认状态
        summary = "空闲"
        sub_state: dict[str, tuple[str, int, int]] = {}

        try:
            out = self._out_var.get().strip()
            if out and Path(out).is_dir():
                st = find_latest_job(Path(out))
                if st:
                    done = sum(1 for s in st.subs if s.stage == "done")
                    failed = sum(1 for s in st.subs if s.stage == "failed")
                    total_ve = sum(int(getattr(s, "videos_extracted", 0) or 0) for s in st.subs)
                    total_vd = sum(int(getattr(s, "videos_deduped", 0) or 0) for s in st.subs)
                    alive = pipeline._process_alive(st.pid)
                    state_disp = st.state
                    if st.state in ("pending", "running") and not alive:
                        state_disp = f"{st.state}(worker已退出)"
                    summary = (
                        f"{st.job_id}  ·  {state_disp}  ·  "
                        f"完成 {done}/{st.total_subs}  失败 {failed}  ·  "
                        f"抽帧 {total_ve}  去重 {total_vd}"
                    )
                    if st.last_message:
                        # 消息太长截一下，避免撑爆汇总条
                        msg = st.last_message
                        if len(msg) > 60:
                            msg = msg[:57] + "..."
                        summary += f"  ·  {msg}"
                    for s in st.subs:
                        sub_state[s.name] = (
                            s.stage,
                            int(getattr(s, "videos_extracted", 0) or 0),
                            int(getattr(s, "videos_deduped", 0) or 0),
                        )
        except Exception as e:
            summary = f"（读取状态失败：{e}）"

        # 汇总条
        try:
            self._summary_var.set(summary)
        except Exception:
            pass

        # 每个子目录一行进度
        stage_map = {
            "pending": "待处理",
            "extracting": "抽帧中",
            "done": "抽帧完",
            "failed": "失败",
            "skipped": "跳过",
        }
        for name, lbl in self._sub_progress_labels.items():
            try:
                if name in sub_state:
                    stage, ve, vd = sub_state[name]
                    disp = stage_map.get(stage, stage)
                    fg = "#0057b7"
                    if stage == "done":
                        fg = "#0a7f2e"
                    elif stage == "failed":
                        fg = "#c0392b"
                    elif stage == "extracting":
                        fg = "#d17b00"
                    lbl.configure(
                        text=f"{disp}  抽帧={ve:<4} 去重={vd:<4}",
                        foreground=fg,
                    )
                else:
                    lbl.configure(text="—", foreground="#888")
            except Exception:
                pass
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

        # 先把当前选择存进配置，下次启动自动填回来
        _save_config(self._dump_current_config())

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
            daily_remain_limit=int(self._daily_remain_limit_var.get()),
            scene_protect=bool(self._scene_protect_var.get()),
            watch_interval=float(self._watch_interval_var.get()),
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
                self.root.after(0, self._refresh_action_button)
            except SystemExit as se:
                self.root.after(0, lambda: messagebox.showerror(
                    "提交失败", f"pipeline 退出码 {se.code}"))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror(
                    "提交失败", f"{type(e).__name__}: {e}"))

        threading.Thread(target=run_submit, daemon=True).start()

    def _on_reset_config(self):
        """删除配置文件并重置表单为默认。"""
        if not messagebox.askyesno("确认",
                                    "确定要清空『记住上次选择』的配置吗？\n"
                                    "会删除 %USERPROFILE%\\.pic-clear\\pipe_gui.json。"):
            return
        try:
            cp = _config_path()
            if cp.is_file():
                cp.unlink()
        except Exception as e:
            messagebox.showerror("失败", f"删除配置失败: {e}")
            return
        # 重置表单
        self._src_var.set("")
        self._out_var.set("")
        self._threshold_var.set(3)
        self._motion_var.set(0.12)
        self._fps_var.set(1.0)
        self._apply_var.set(True)
        self._hard_delete_var.set(True)
        self._minimize_to_tray_var.set(True)
        self._hotkey_var.set("ctrl+alt+p")
        self._scene_protect_var.set(True)
        self._daily_remain_limit_var.set(80000)
        self._watch_interval_var.set(3.0)
        # 清空子目录列表
        for w in self._subs_inner.winfo_children():
            w.destroy()
        self._sub_vars.clear()
        # 重跑自动挑盘逻辑
        self._auto_pick_drive()
        messagebox.showinfo("已重置", "配置已清空，可以重新选择。")

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
            self._refresh_action_button()
        except Exception as e:
            messagebox.showerror("停止失败", str(e))

    def _on_kill_all(self):
        """一键杀掉所有 pipeline/extract/dedupe/其他 pipe_gui 实例，跳过自己。"""
        # 二次确认
        target_list = "\n".join(f"    - {n}" for n in KILL_TARGETS)
        if not messagebox.askyesno(
            "确认一键清理",
            "将强制杀掉以下所有正在运行的进程（当前 GUI 自己除外）：\n\n"
            f"{target_list}\n\n"
            "适用场景：bat 脚本残留、多次误启动、状态卡死。\n"
            "任务会立刻中断，未处理完的图片保持原样，请确认？"):
            return

        my_pid = os.getpid()
        try:
            results, killed = kill_all_related(exclude_pids=[my_pid])
        except Exception as e:
            messagebox.showerror("失败", f"kill 失败: {e}")
            return

        # 顺手把最新 job 标记为 stopped，避免状态浮层里显示"僵尸 running"
        marked = None
        out = self._out_var.get().strip()
        if out and Path(out).is_dir():
            try:
                marked = mark_latest_job_stopped(Path(out))
            except Exception as e:
                print(f"[WARN] mark_latest_job_stopped: {e}", file=sys.stderr)

        # 汇报结果
        if not results:
            messagebox.showinfo("清理完成", "没有发现需要杀掉的相关进程。")
            self._refresh_action_button()
            return
        lines = [f"共尝试 {len(results)} 个，成功杀掉 {killed} 个：\n"]
        for name, pid, ok in results:
            mark = "√" if ok else "×"
            lines.append(f"  [{mark}] {name}  PID {pid}")
        if marked:
            lines.append(f"\n已把任务 {marked} 状态改为 stopped。")
        failed = len(results) - killed
        if failed > 0:
            lines.append(f"\n{failed} 个未能杀掉，可能需要管理员权限或已自行退出。")
        messagebox.showinfo("清理完成", "\n".join(lines))
        self._refresh_action_button()

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
            pystray.MenuItem("一键杀死所有", lambda: self.root.after(0, self._on_kill_all)),
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
        w.geometry("720x520")
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
                # 每个子目录的抽帧/去重进度（marker 驱动，视频粒度）
                if st.subs:
                    lines.append("")
                    lines.append("  子目录进度（抽帧=视频已抽完数，去重=视频已去重数）：")
                    total_extract = 0
                    total_dedup = 0
                    for idx, s in enumerate(st.subs, 1):
                        name = (s.name or f"sub{idx}")
                        # 名字过长截断，保证一行不溢
                        short = name if len(name) <= 28 else name[:25] + "..."
                        stage_disp = {
                            "pending": "待处理",
                            "extracting": "抽帧中",
                            "done": "抽帧完",
                            "failed": "失败",
                            "skipped": "跳过",
                        }.get(s.stage, s.stage)
                        ve = int(getattr(s, "videos_extracted", 0) or 0)
                        vd = int(getattr(s, "videos_deduped", 0) or 0)
                        total_extract += ve
                        total_dedup += vd
                        lines.append(
                            f"    [{idx:>2}] {short:<28}  {stage_disp:<6}  抽帧={ve:<4}  去重={vd:<4}"
                        )
                    lines.append(
                        f"  合计      : 抽帧={total_extract}  去重={total_dedup}"
                    )
            else:
                lines.append("  最新任务  : （无）")
        else:
            lines.append("  最新任务  : （输出目录未设置或不存在，无法查询）")

        info.config(state="normal")
        info.delete("1.0", "end")
        info.insert("end", "\n".join(lines))
        info.config(state="disabled")

    # ---------- 日志浮层 ----------

    LOG_TAIL_MAX_BYTES = 200_000  # 首次打开只加载末尾 200KB，防止大日志卡死
    LOG_TAIL_LINES = 500          # Text 里最多留多少行，超了裁前面
    LOG_REFRESH_MS = 2000         # 自动 tail 刷新间隔

    def show_log_window(self):
        if self._log_toplevel is not None and self._log_toplevel.winfo_exists():
            self._log_toplevel.deiconify()
            self._log_toplevel.lift()
            self._log_toplevel.focus_force()
            return
        w = tk.Toplevel(self.root)
        self._log_toplevel = w
        w.title("pic-clear 日志")
        w.geometry("900x560")
        w.protocol("WM_DELETE_WINDOW", lambda: (w.withdraw()))

        # 顶部：日志种类切换 + 当前 job 信息
        top = ttk.Frame(w); top.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(top, text="日志种类：").pack(side="left")
        which_var = tk.StringVar(value="worker")
        cb = ttk.Combobox(top, textvariable=which_var,
                          values=["worker", "pipeline"],
                          state="readonly", width=12)
        cb.pack(side="left", padx=4)
        job_info_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=job_info_var, foreground="#555").pack(side="left", padx=12)

        # 文本区
        mid = ttk.Frame(w); mid.pack(fill="both", expand=True, padx=8, pady=4)
        txt = tk.Text(mid, font=("Consolas", 9), wrap="none")
        yscroll = ttk.Scrollbar(mid, orient="vertical", command=txt.yview)
        xscroll = ttk.Scrollbar(mid, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        txt.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        mid.rowconfigure(0, weight=1)
        mid.columnconfigure(0, weight=1)
        txt.config(state="disabled")

        # 底部按钮
        auto_tail_var = tk.BooleanVar(value=True)
        btm = ttk.Frame(w); btm.pack(fill="x", padx=8, pady=(4, 8))
        ttk.Checkbutton(btm, text="自动 tail（2 秒刷）",
                        variable=auto_tail_var).pack(side="left")
        ttk.Button(btm, text="立即刷新",
                   command=lambda: self._reload_log_text(txt, which_var.get(), job_info_var, full_reload=True)
                   ).pack(side="left", padx=6)
        ttk.Button(btm, text="打开 job 目录",
                   command=self._open_current_job_dir).pack(side="left", padx=6)
        ttk.Button(btm, text="清屏",
                   command=lambda: self._clear_log_text(txt)).pack(side="left", padx=6)
        ttk.Button(btm, text="关闭", command=lambda: w.withdraw()).pack(side="right")

        # 状态存到实例，供 tail 循环用
        self._log_widgets = {
            "text": txt,
            "which": which_var,
            "auto_tail": auto_tail_var,
            "job_info": job_info_var,
            "last_size": {"worker": 0, "pipeline": 0},
            "last_job_id": None,
        }

        cb.bind("<<ComboboxSelected>>",
                lambda e: self._reload_log_text(txt, which_var.get(), job_info_var, full_reload=True))

        # 首次装载
        self._reload_log_text(txt, which_var.get(), job_info_var, full_reload=True)
        # 起 tail 循环
        self._schedule_log_refresh()

    def _resolve_log_path(self, which: str) -> "tuple[Path | None, str | None]":
        """定位最新 job 的日志文件。返回 (path, job_id)。"""
        out = self._out_var.get().strip()
        if not out or not Path(out).is_dir():
            return None, None
        st = find_latest_job(Path(out))
        if not st:
            return None, None
        job_dir = pipeline._job_dir(Path(out), st.job_id)
        fname = "worker.log" if which == "worker" else "pipeline.log"
        p = job_dir / fname
        return (p if p.is_file() else None), st.job_id

    def _reload_log_text(self, txt: tk.Text, which: str,
                         job_info_var: tk.StringVar, full_reload: bool = False):
        """重新加载日志。full_reload=True 时清空 Text 从末尾 200KB 重装。"""
        p, job_id = self._resolve_log_path(which)
        if p is None:
            self._set_log_content(txt, f"（暂无 {which}.log；请先跑一个任务）")
            job_info_var.set("（无任务）")
            self._log_widgets["last_size"][which] = 0
            self._log_widgets["last_job_id"] = None
            return
        # job 切换或强制重载 → 全量重载末尾一段
        need_full = full_reload or (self._log_widgets.get("last_job_id") != job_id)
        try:
            size = p.stat().st_size
        except OSError:
            return
        if need_full:
            try:
                with p.open("rb") as f:
                    if size > self.LOG_TAIL_MAX_BYTES:
                        f.seek(size - self.LOG_TAIL_MAX_BYTES)
                        # 跳过残行
                        f.readline()
                    content = f.read().decode("utf-8", errors="replace")
            except Exception as e:
                content = f"（读取失败：{e}）"
            self._set_log_content(txt, content)
            self._log_widgets["last_size"][which] = size
            self._log_widgets["last_job_id"] = job_id
            job_info_var.set(f"{job_id}  ·  {p.name}  ·  {size:,} 字节")
            self._scroll_log_to_end(txt)
            return
        # 增量 tail
        last_size = int(self._log_widgets["last_size"].get(which, 0))
        if size < last_size:
            # 文件被截断（不太可能），当作全量重载
            self._log_widgets["last_size"][which] = 0
            self._reload_log_text(txt, which, job_info_var, full_reload=True)
            return
        if size == last_size:
            return
        try:
            with p.open("rb") as f:
                f.seek(last_size)
                chunk = f.read(size - last_size).decode("utf-8", errors="replace")
        except Exception:
            return
        self._append_log_text(txt, chunk)
        self._log_widgets["last_size"][which] = size
        job_info_var.set(f"{job_id}  ·  {p.name}  ·  {size:,} 字节")

    def _set_log_content(self, txt: tk.Text, content: str) -> None:
        txt.config(state="normal")
        txt.delete("1.0", "end")
        txt.insert("end", content)
        self._trim_log_lines(txt)
        txt.config(state="disabled")

    def _append_log_text(self, txt: tk.Text, chunk: str) -> None:
        if not chunk:
            return
        # 记住滚动位置：如果用户在末尾，追加后自动滚到底；否则不动
        was_at_end = False
        try:
            yv = txt.yview()
            was_at_end = yv[1] >= 0.999
        except Exception:
            pass
        txt.config(state="normal")
        txt.insert("end", chunk)
        self._trim_log_lines(txt)
        txt.config(state="disabled")
        if was_at_end:
            self._scroll_log_to_end(txt)

    def _trim_log_lines(self, txt: tk.Text) -> None:
        try:
            total = int(txt.index("end-1c").split(".")[0])
        except Exception:
            return
        if total > self.LOG_TAIL_LINES:
            over = total - self.LOG_TAIL_LINES
            txt.delete("1.0", f"{over + 1}.0")

    def _scroll_log_to_end(self, txt: tk.Text) -> None:
        try:
            txt.see("end")
        except Exception:
            pass

    def _clear_log_text(self, txt: tk.Text) -> None:
        txt.config(state="normal")
        txt.delete("1.0", "end")
        txt.config(state="disabled")

    def _schedule_log_refresh(self):
        w = self._log_toplevel
        if w is None or not w.winfo_exists():
            return
        try:
            if w.winfo_viewable() and self._log_widgets.get("auto_tail") and \
                    self._log_widgets["auto_tail"].get():
                self._reload_log_text(
                    self._log_widgets["text"],
                    self._log_widgets["which"].get(),
                    self._log_widgets["job_info"],
                    full_reload=False,
                )
        except Exception:
            pass
        try:
            self.root.after(self.LOG_REFRESH_MS, self._schedule_log_refresh)
        except Exception:
            pass

    def _open_current_job_dir(self):
        out = self._out_var.get().strip()
        if not out or not Path(out).is_dir():
            messagebox.showinfo("提示", "输出根目录未设置或不存在"); return
        st = find_latest_job(Path(out))
        if not st:
            messagebox.showinfo("提示", "还没有任何任务"); return
        job_dir = pipeline._job_dir(Path(out), st.job_id)
        try:
            if os.name == "nt":
                os.startfile(str(job_dir))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(job_dir)])
            else:
                subprocess.Popen(["xdg-open", str(job_dir)])
        except Exception as e:
            messagebox.showerror("失败", f"打开目录失败：{e}")

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
