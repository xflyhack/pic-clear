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
import re
from winpath_util import to_long_path as _to_long_path
import subprocess
from subproc_group import SubprocGroup
import sys
import threading
import time
import uuid
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
from gui_log_util import GuiLogController  # noqa: E402
from tray_util import TrayController, TOOLTIP_EXTRACT  # noqa: E402

# stats_db 可选; 主流程不能因为落库失败中断
try:
    import stats_db as _stats_db  # type: ignore
except Exception:  # pragma: no cover
    _stats_db = None  # type: ignore


APP_TITLE = "pic-clear 抽帧工具"
# 版本号: CI 会在打包前覆盖 _version.py 里的 VERSION 成 tag 名 (如 v0.4.30);
# 本地跑 py 时 fallback 到 'dev', 找不到 _version.py 也能启动.
try:
    from _version import VERSION as _V
except Exception:
    _V = 'dev'
APP_VERSION = _V
APP_COMPANY = "山东数旗信息科技有限公司"
CONFIG_NAME = "extract_gui.json"
HOTKEY_DEFAULT = "ctrl+alt+e"
VIDEO_EXTS = (".h265", ".mp4")


# ---------- 配置文件（独立于 pipe_gui.json） ----------

def _config_path() -> Path:
    return Path(os.path.expanduser("~")) / ".pic-clear" / CONFIG_NAME


# v0.4.87: 日志落盘 / preload / 打开目录 逻辑全部搬到 gui_log_util.py 公共模块.
# 老路径 ~/.pic-clear/extract_gui_logs/ 里的历史 log 通过 GuiLogController
# 的 legacy_dir 兜底继续能被 preload 读到.


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

# extract_frames 处理完一个视频的日志正则.
# 匹配以下几种格式:
#   多线程 v0.4.78+ (中文标签): "[第 3 个/共 61 个] ✓ path  已抽帧数=179 ..."
#   多线程老格式 (向后兼容):     "[3/61] ✓ path  帧=179 耗时=..."
#   单线程独立结束行:            "    ✓ OK，帧数 179"             或 ⊘ / ✗
# 备注: 同一视频只有一行会命中 (多线程只有 idx 那行, 单线程只有 ✓/⊘/✗ 那行)
_EXTRACT_DONE_RE = re.compile(
    r"(?:^\[第\s*\d+\s*个/共\s*\d+\s*个\][ \t]+[\u2713\u2298\u25c7\u2717])"  # [第 N 个/共 M 个] ✓⊘◇✗
    r"|(?:^\[\d+/\d+\][ \t]+[\u2713\u2298\u25c7\u2717])"                     # [N/M] ✓⊘◇✗ (老格式)
    r"|(?:^[ \t]+[\u2713\u2298\u2717][ \t])"                                 # 缩进 ✓⊘✗ 独立行
)


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


def _count_done_markers(markers_root: Path) -> int:
    r"""数一下 markers_root 下已存在的 _done.marker 数量.

    extract_frames 里每个视频对应一个 <markers_root>/.../<视频stem>/_done.marker,
    无论内容是 'done' 还是 'empty' 都表示这个视频跑过了, 都算已完成.

    v0.4.65: 深路径 (UNC + 12 层) Windows scandir 会 silently skip 部分子目录,
    导致 rglob 数出的结果每次不同 (堡垒机实测同一目录连着数 113 / 65).
    改成先给根目录套 \\?\ 前缀再 rglob, 让 scandir 走 NT namespace, 稳定得多.
    """
    if not markers_root:
        return 0
    root_str = str(markers_root)
    if not Path(root_str).is_dir():
        # 短路径可能因长度 fail, 试一下长路径 is_dir
        long_root = _to_long_path(root_str)
        if long_root == root_str or not Path(long_root).is_dir():
            return 0
        walk_root = Path(long_root)
    else:
        # 无论短路径通不通, 递归时统一走长路径, 避免 scandir 走深了才 fail
        long_root = _to_long_path(root_str)
        walk_root = Path(long_root) if long_root != root_str else Path(root_str)
    try:
        return sum(1 for _ in walk_root.rglob("_done.marker"))
    except Exception:
        return 0


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
                base_w=800, base_h=740, min_w=720, min_h=620))
        self.root.minsize(int(720 * self._ui_scale), int(620 * self._ui_scale))
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
        # 命名规则相关（新版：video1_0009.jpg / 老版：frame_000002.jpg）
        # name_style: legacy / parent / custom
        self._name_style_var = tk.StringVar(
            value=str(self._cfg.get("name_style", "parent")))
        self._name_template_var = tk.StringVar(
            value=str(self._cfg.get("name_template", "{parent}_{seq}")))
        self._name_digits_var = tk.IntVar(
            value=int(self._cfg.get("name_digits", 4)))
        self._name_preview_var = tk.StringVar(value="")
        self._minimize_to_tray_var = tk.BooleanVar(
            value=bool(self._cfg.get("minimize_to_tray", True)))
        self._hotkey_var = tk.StringVar(
            value=self._cfg.get("hotkey", HOTKEY_DEFAULT))

        # 子目录复选框：[(name, BooleanVar), ...]
        self._sub_vars: list[tuple[str, tk.BooleanVar]] = []

        # 运行时状态
        # v0.4.88: 托盘 + 全局快捷键 + 关闭最小化 全部走 TrayController
        # (跟 dedupe/classify/stats_viewer 共享 tray_util 公共模块).
        # 图标 fallback: 蓝底 + "EX" (跟老版本一致, 避免打包过渡期视觉突变).
        self._tray = TrayController(
            root=self.root,
            app_id="pic-clear-extract",
            tooltip=TOOLTIP_EXTRACT,
            fallback_glyph=((0, 102, 204, 255), "EX"),
            hotkey_default=HOTKEY_DEFAULT,
            app_title=APP_TITLE,
            ui_scale=self._ui_scale,
        )
        self._worker_thread: threading.Thread | None = None
        self._worker_stop_flag = threading.Event()
        # v0.4.46 子进程组: Windows 用 Job Object 保证 GUI 死后子进程一起死
        self._subproc_group = SubprocGroup()
        self._progress_var = tk.StringVar(value="就绪")
        self._total_videos = 0
        self._done_videos = 0

        # v0.4.87: 日志改公共模块 GuiLogController.
        # legacy_dir 兜底: 迁移前的 ~/.pic-clear/extract_gui_logs/ 里的老 log
        # 首次跑新版时仍能被 preload 读到, 不至于丢历史.
        self._log_ctl = GuiLogController(
            app_name="extract_gui",
            legacy_dir=(Path(os.path.expanduser("~"))
                        / ".pic-clear" / "extract_gui_logs"),
            legacy_pattern="extract_gui_*.log",
        )
        # 兼容旧字段名, 外部 (env_probe 等) 可能通过 self._log_path 拿路径.
        self._log_path = self._log_ctl.log_path

        # 智能自动滚动：手动往上翻则暂停跟随，滚回底部自动恢复
        self._auto_scroll_var = tk.BooleanVar(value=True)

        self._build_ui()
        # v0.4.88: 注入托盘退出流程要跑的业务收尾动作
        self._install_shutdown_hooks()
        self.root.after(200, self._drain_log_queue)

        # v0.4.73: 启动即打印**多行**环境画像 (含常用路径可达性)
        try:
            from env_probe import probe_and_log
            def _run_env_probe():
                pp = []
                for v in (self._src_var, self._out_var, self._markers_root_var):
                    try:
                        s = v.get().strip()
                        if s:
                            pp.append(s)
                    except Exception:
                        pass
                probe_and_log(self._log, probe_paths=pp)
            self.root.after(100, _run_env_probe)
        except Exception as _e:
            self._log(f"[ENV] probe_and_log 失败: {type(_e).__name__}: {_e}")

        # v0.4.76: 启动即打印**子 exe 版本**, 避免 "GUI 是新版但 extract_frames.exe
        # 还是老版, marker/长路径修复没跟上" 这种坑 (v0.4.75 用户遭遇过).
        try:
            from child_exe_ver import probe_and_log as _core_probe
            def _run_core_probe():
                _core_probe(
                    self._log,
                    exe_finder=_find_extract_exe,
                    exe_name="extract_frames.exe",
                    gui_version=APP_VERSION,
                )
            self.root.after(200, _run_core_probe)
        except Exception as _e:
            self._log(f"[CORE] 探测失败: {type(_e).__name__}: {_e}")

        # 环境预检
        self.root.after(300, self._check_environment)
        # 启动后按当前配置 (视频源目录 + markers 根) 自动预扫一次进度,
        # 让用户不用点"开始"就能看到 "完成 X/Y"
        self.root.after(500, self._preview_progress_from_config)

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

        # 日志 tab（独立出来，避免主页按钮/控件被挤没）
        log_tab = ttk.Frame(nb)
        nb.add(log_tab, text="日志")
        self._build_log_tab(log_tab)

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

        # 命名规则：新版 / 老版 / 自定义 + 位数 + 预览
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="命名规则：", width=12).pack(side="left")
        self._name_style_combo = ttk.Combobox(
            row, width=32, state="readonly",
            values=[
                "新版 ({parent}_{seq})",
                "老版 (frame_{seq})",
                "自定义（下方模板生效）",
            ],
        )
        # 内部值与显示值映射
        self._name_style_display_map = {
            "parent": "新版 ({parent}_{seq})",
            "legacy": "老版 (frame_{seq})",
            "custom": "自定义（下方模板生效）",
        }
        self._name_style_reverse_map = {
            v: k for k, v in self._name_style_display_map.items()
        }
        cur_style = self._name_style_var.get() or "parent"
        self._name_style_combo.set(
            self._name_style_display_map.get(cur_style,
                                             self._name_style_display_map["parent"]))
        self._name_style_combo.pack(side="left")
        self._name_style_combo.bind(
            "<<ComboboxSelected>>", lambda _e: self._on_name_style_changed())
        ttk.Label(row, text="  序号位数：",
                  foreground="#666").pack(side="left", padx=(8, 0))
        ttk.Spinbox(row, from_=1, to=8, increment=1, width=4,
                    textvariable=self._name_digits_var,
                    command=self._refresh_name_preview).pack(side="left")

        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="模板：", width=12).pack(side="left")
        self._name_template_entry = ttk.Entry(
            row, textvariable=self._name_template_var, width=44)
        self._name_template_entry.pack(side="left")
        ttk.Label(row, text="  占位符 {parent}=视频同名文件夹  {seq}=序号",
                  foreground="#666").pack(side="left", padx=6)
        # 用户手动改模板时刷新预览
        self._name_template_var.trace_add(
            "write", lambda *_: self._refresh_name_preview())
        self._name_digits_var.trace_add(
            "write", lambda *_: self._refresh_name_preview())

        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="预览：", width=12).pack(side="left")
        ttk.Label(row, textvariable=self._name_preview_var,
                  foreground="#0066cc").pack(side="left")
        # 初始状态同步一次
        self._on_name_style_changed(persist=False)

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

        # 进度（日志区已挪到独立 Tab）
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="进度：").pack(side="left")
        ttk.Label(row, textvariable=self._progress_var,
                  foreground="#0066cc").pack(side="left")
        ttk.Label(row, text="（详细日志见「日志」Tab）",
                  foreground="#888").pack(side="left", padx=8)

        # 托盘 / 快捷键
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Checkbutton(row, text="关闭时最小化到托盘",
                        variable=self._minimize_to_tray_var).pack(side="left")
        ttk.Label(row, text="   快捷键：").pack(side="left", padx=(12, 2))
        ttk.Entry(row, textvariable=self._hotkey_var, width=16).pack(side="left")
        ttk.Button(row, text="注册",
                   command=self._register_hotkey).pack(side="left", padx=4)

    def _build_log_tab(self, page: ttk.Frame):
        # v0.4.87: 工具条走 GuiLogController.build_toolbar, "自动滚到底"
        # 复选框通过 extra_toolbar 回调追加, 保留原智能滚动行为.
        def _extra(tb):
            ttk.Checkbutton(tb, text="自动滚到底",
                            variable=self._auto_scroll_var
                            ).pack(side="left", padx=8)
        self._log_ctl.build_toolbar(page, extra_toolbar=_extra)

        # 日志区: Text + 纵/横滚动条 (GUI 自建, 保留智能滚动 hook)
        log_wrap = ttk.Frame(page)
        log_wrap.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        self._log_text = tk.Text(log_wrap, height=24,
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
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>",
                    "<Prior>", "<Next>", "<Up>", "<Down>",
                    "<Key-Home>", "<Key-End>"):
            self._log_text.bind(seq, self._on_log_user_scroll, add="+")
        self._log_text.config(state="disabled")

        # v0.4.87: 把 Text 绑给控制器 + preload 上次 tail 200 行
        self._log_ctl.attach_text(self._log_text)
        self._log_ctl.preload_prev_tail_to_text()

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

        # v0.4.48 内核版本 (走共享 helper, 与 dedupe_gui / classify_gui 风格一致)
        self._about_core_frame = ttk.LabelFrame(page, text="内核版本 (extract_frames.exe)")
        self._about_core_frame.pack(fill="x", padx=12, pady=8)
        self.root.after(400, self._refresh_core_version)

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

    def _refresh_core_version(self) -> None:
        _pg.render_core_version_frame(
            self.root, self._about_core_frame,
            label_text="内核版本",
            exe_finder=_find_extract_exe,
            missing_hint="请把 extract_frames.exe 放到本 GUI 同目录 / System32 / PATH 后重启",
        )

    def _browse_src(self):
        init = self._src_var.get() or os.path.expanduser("~")
        p = filedialog.askdirectory(initialdir=init, title="选择视频源目录")
        if p:
            self._src_var.set(p)
            self._rescan_subs()
            self._preview_progress_from_config()

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
            self._preview_progress_from_config()

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
            v.trace_add(
                "write",
                lambda *_a: self._preview_progress_from_config(),
            )
            ttk.Checkbutton(self._sub_frame, text=name, variable=v).pack(
                anchor="w", padx=4)
            self._sub_vars.append((name, v))
        # 子目录列表变了后刷一次进度预览
        self._preview_progress_from_config()

    def _toggle_all(self, state: bool):
        for _, v in self._sub_vars:
            v.set(state)

    # ---------- 命名规则 ----------

    def _on_name_style_changed(self, persist: bool = True) -> None:
        """Combobox 切换：更新内部 style 变量、切换 Entry 使能、刷新预览。"""
        display = self._name_style_combo.get()
        style = self._name_style_reverse_map.get(display, "parent")
        self._name_style_var.set(style)
        # 老版 / 新版：模板固定，Entry 只读展示；自定义时才可编辑
        preset_map = {
            "parent": "{parent}_{seq}",
            "legacy": "frame_{seq}",
        }
        if style in preset_map:
            self._name_template_var.set(preset_map[style])
            try:
                self._name_template_entry.configure(state="readonly")
            except Exception:
                pass
        else:
            try:
                self._name_template_entry.configure(state="normal")
            except Exception:
                pass
        self._refresh_name_preview()

    def _refresh_name_preview(self) -> None:
        """按当前 style/template/digits 算一个示例文件名放到预览 Label。"""
        try:
            style = self._name_style_var.get() or "parent"
            template = self._name_template_var.get().strip()
            digits = int(self._name_digits_var.get() or 4)
        except Exception:
            self._name_preview_var.set("(参数不合法)")
            return
        digits = max(1, min(8, digits))
        if style == "custom":
            tmpl = template or "{parent}_{seq}"
        else:
            tmpl = {"parent": "{parent}_{seq}",
                    "legacy": "frame_{seq}"}.get(style, "{parent}_{seq}")
        try:
            example = (tmpl.replace("{parent}", "video1")
                           .replace("{seq}", f"{9:0{digits}d}") + ".jpg")
        except Exception as e:
            example = f"(模板非法：{e})"
        self._name_preview_var.set(example)

    def _current_name_params(self) -> tuple[str, str, int]:
        """返回 (style, template, digits)。用于组装 CLI 参数。"""
        style = self._name_style_var.get() or "parent"
        template = self._name_template_var.get().strip()
        try:
            digits = int(self._name_digits_var.get() or 4)
        except Exception:
            digits = 4
        digits = max(1, min(8, digits))
        # 非 custom 时，模板保持与 preset 一致，避免用户误改后不生效
        if style == "parent":
            template = "{parent}_{seq}"
        elif style == "legacy":
            template = "frame_{seq}"
        return style, template, digits

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

        # 命名规则参数校验（模板必须含 {seq}，不能含路径/非法字符）
        name_style, name_template, name_digits = self._current_name_params()
        if "{seq}" not in name_template:
            messagebox.showerror(
                "参数错误",
                f"命名模板必须包含 {{seq}} 占位符：{name_template}")
            return
        bad_chars = set("/\\%\n\r\t\0:*?\"<>|")
        for ch in name_template:
            if ch in bad_chars:
                messagebox.showerror(
                    "参数错误",
                    f"命名模板中不允许的字符：{ch!r}\n模板={name_template}")
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
        # 从 markers 预扫已完成数, 支持"关软件重开也能看到进度"
        self._done_videos = 0
        for _s, _d, sub_mr in sub_pairs:
            self._done_videos += _count_done_markers(sub_mr)
        if self._done_videos:
            self._log(
                f"[恢复] 预扫 markers: 已完成 {self._done_videos}/"
                f"{self._total_videos}"
            )
        self._push_progress()
        if self._total_videos == 0:
            if not messagebox.askyesno(
                    "提示",
                    "扫描不到任何 .h265 / .mp4 文件，仍要启动吗？"):
                return

        # 启动后台线程
        self._worker_stop_flag.clear()
        # 生成本次任务 task_id, 落一条 task_runs 快照 (完整 GUI 配置),
        # 再通过环境变量传给 extract_frames.exe, 让每条 extract_stats 都能关联
        task_id = uuid.uuid4().hex[:16]
        self._current_task_id = task_id
        os.environ["PICCLEAR_TASK_ID"] = task_id
        self._record_run_snapshot(task_id, sub_pairs, exe, name_style,
                                  name_template, name_digits)
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
                # 命名规则参数（GUI 全局配置，对所有子目录任务生效）
                name_style, name_template, name_digits = self._current_name_params()
                cmd += ["--name-style", name_style,
                        "--name-digits", str(name_digits)]
                if name_style == "custom" and name_template:
                    cmd += ["--name-template", name_template]
                if bool(self._force_reextract_var.get()):
                    cmd.append("--no-skip-existing")
                    self._log("[警告] 已开启强制重切：所有视频将忽略已完成标记，覆盖重抽")
                self._log(f"[命令] {' '.join(cmd)}")

                # Windows 下 CREATE_NO_WINDOW 让子进程不弹黑窗
                try:
                    # v0.4.46 走 SubprocGroup: 自动绑 Job Object,
                    # GUI 死 (正常/强杀/os._exit) 子进程 (extract_frames.exe/ffmpeg) 一起死
                    proc = self._subproc_group.popen(
                        cmd,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace")
                except FileNotFoundError:
                    self._log(f"[错误] 找不到 exe：{exe}")
                    continue

                for line in proc.stdout:  # type: ignore[union-attr]
                    line = line.rstrip()
                    if line:
                        self._log(line)
                        # 检测 "一个视频处理完了" 更新进度.
                        # extract_frames 实际的完成日志格式:
                        #   多线程: "[3/61] ✓ path/to/x.mp4  帧=179 耗时=39.0s ..."
                        #   多线程 (empty/locked/failed): 同上但标签是 ⊘ / ◇ / ✗
                        #   单线程: 独立一行 "    ✓ OK，帧数 179"
                        #           或 "    ⊘ 跳过（无帧）..." / "    ✗ 失败: ..."
                        # 只要看到 stage 标签就算一个视频处理过, 计数 +1.
                        if _EXTRACT_DONE_RE.search(line):
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

    def _preview_progress_from_config(self) -> None:
        """启动时/切目录时按当前配置预扫一次, 恢复 "完成 X/Y" 显示.

        不启动子进程, 只做本地文件扫描. 如果 markers 里没数据, 也会把
        Y=总视频数 显示出来, 让用户知道待抽帧总量.
        """
        try:
            src = self._src_var.get().strip()
            mr = self._markers_root_var.get().strip()
            if not src or not mr:
                return
            src_p = Path(src)
            mr_p = Path(mr)
            if not src_p.is_dir():
                return
            # 生成 sub_pairs 的方式跟 _on_run 完全一致
            if self._sub_vars:
                selected = [name for name, v in self._sub_vars if v.get()]
            else:
                selected = []
            if selected:
                pairs = [(src_p / name, mr_p / src_p.name / name)
                         for name in selected]
            else:
                pairs = [(src_p, mr_p / src_p.name)]
            total = 0
            done = 0
            for sub_src, sub_mr in pairs:
                total += len(_list_videos(sub_src))
                done += _count_done_markers(sub_mr)
            self._total_videos = total
            self._done_videos = min(done, total) if total else 0
            if total > 0:
                pct = int(self._done_videos * 100 / total)
                self._progress_var.set(
                    f"{self._done_videos}/{total}  ({pct}%)"
                )
            else:
                self._progress_var.set("就绪")
        except Exception as e:
            # 预扫失败不影响主流程, 静默
            print(f"[preview_progress] {type(e).__name__}: {e}",
                  file=sys.stderr)

    # ---------- 日志 ----------

    def _log(self, msg: str):
        """线程安全: 转发到 GuiLogController (加时间戳 + 落盘 + 塞 queue)."""
        self._log_ctl.log(msg)

    def _drain_log_queue(self):
        # v0.4.87: pump 由公共控制器做, 智能滚由 GUI 自己判断 (跟原行为一致).
        if self._log_ctl.pump() and self._auto_scroll_var.get():
            try:
                self._log_text.see("end")
            except Exception:
                pass
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

    # v0.4.87: _preload_prev_log_tail / _open_log_dir 已搬到 gui_log_util 公共模块.

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
            # 命名规则
            "name_style": self._name_style_var.get() or "parent",
            "name_template": self._name_template_var.get(),
            "name_digits": int(self._name_digits_var.get() or 4),
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

    def _record_run_snapshot(
        self, task_id: str,
        sub_pairs: list[tuple[Path, Path, Path]],
        exe: str,
        name_style: str, name_template: str, name_digits: int,
    ) -> None:
        """把本次抽帧任务的完整 GUI 配置落 task_runs 表. 静默失败."""
        if _stats_db is None:
            return
        try:
            cfg = self._dump_cfg()
            # 额外补几个关键字段, 方便日后排查
            cfg["_task_id"] = task_id
            cfg["_exe"] = exe
            cfg["_sub_pairs"] = [
                {"sub_src": str(s), "sub_dst": str(d), "sub_markers": str(m)}
                for s, d, m in sub_pairs
            ]
            cfg["_effective_name_style"] = name_style
            cfg["_effective_name_template"] = name_template
            cfg["_effective_name_digits"] = name_digits
            _stats_db.record_task_run(
                task_id=task_id,
                task_type="extract",
                config=cfg,
                cmdline=None,
                version=APP_VERSION,
            )
            self._log(f"[stats] task_id={task_id}")
        except Exception as e:
            print(f"[record_run_snapshot] {type(e).__name__}: {e}",
                  file=sys.stderr)

    # ---------- 关闭 / 托盘 ----------

    def on_close(self):
        try:
            _save_config(self._dump_cfg())
        except Exception:
            pass
        if self._minimize_to_tray_var.get():
            self._tray.hide_to_tray()
            self._tray.maybe_show_close_hint(
                cfg_get=lambda: bool((self._cfg or {}).get("hide_close_hint")),
                cfg_set=self._persist_hide_close_hint,
                hotkey_display=self._hotkey_var.get(),
            )
        else:
            self.quit_all()

    def _persist_hide_close_hint(self, val: bool) -> None:
        """把"以后不再提示"写回 self._cfg + 落盘."""
        try:
            self._cfg["hide_close_hint"] = bool(val)
            _save_config(self._dump_cfg())
        except Exception:
            pass

    # v0.4.88: hide_to_tray / show_main / quit_all / _start_tray_if_needed /
    # _maybe_show_close_hint / _register_hotkey 全部搬到 tray_util.TrayController.
    # 这里保留 super-thin 转发方法, 因为 GUI 里其它地方 (关于 tab 按钮等)
    # 已经引用 self.hide_to_tray / self.quit_all / self.show_main.

    def hide_to_tray(self):
        self._tray.hide_to_tray()

    def show_main(self):
        self._tray.show_main()

    def quit_all(self):
        # 业务侧收尾 (worker stop + 子进程组) 通过 TrayController.add_shutdown_hook
        # 注入到公共退出流程, 见 _install_shutdown_hooks.
        self._tray.quit_all()

    def _install_shutdown_hooks(self) -> None:
        """把业务收尾动作注入托盘的退出流程. __init__ 尾部调一次."""
        self._tray.add_shutdown_hook(self._worker_stop_flag.set)

        def _kill_subproc():
            try:
                self._subproc_group.terminate_all(wait_timeout=2.0)
            except Exception:
                pass
            try:
                self._subproc_group.close()
            except Exception:
                pass
        self._tray.add_shutdown_hook(_kill_subproc)

    def _register_hotkey(self):
        hk = self._hotkey_var.get().strip() or HOTKEY_DEFAULT
        ok, msg = self._tray.register_hotkey(hk)
        if ok:
            messagebox.showinfo("已注册", msg)
        else:
            messagebox.showerror("注册失败", msg)


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
