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
from subproc_group import SubprocGroup
import sys
import threading
import re
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
# 版本号: CI 会在打包前覆盖 _version.py 里的 VERSION 成 tag 名 (如 v0.4.30);
# 本地跑 py 时 fallback 到 'dev', 找不到 _version.py 也能启动.
try:
    from _version import VERSION as _V
except Exception:
    _V = 'dev'
APP_VERSION = _V
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

def _normalize_windows_path(path_str) -> str:
    r"""把 tkinter/filedialog 返回值 (混合斜杠 + 悄悄加的 //?/ 前缀) 归一化.

    v0.4.36: dedupe_gui 的 GUI 变量 (target / markers_root) 从
    filedialog / 手工输入 / json 反读进来时形式不定, 可能是:
      - Z:\...        (标准反斜杠)
      - Z:/...         (正斜杠, 用户或 tkinter 转的)
      - //?/Z:/...     (tkinter filedialog 对长路径悄悄加前缀 + 正斜杠)
      - \\?\Z:\... (已带前缀)
    如果不统一, Path("Z:/a/b").relative_to(Path("Z:\a")) 会抛 ValueError,
    导致 marker_dir 只取 rel=d.name (叶子名), 不同祖先重名互相覆盖,
    断线续跑失效, 重复清理.
    """
    s = str(path_str)
    if os.name != "nt":
        return s
    if "/" in s:
        s = s.replace("/", "\\")
    while s.startswith("\\\\?\\\\\\?\\"):
        s = s[4:]
    return s


def _to_long_path(path_str: str) -> str:
    r"""Windows 上 >= 180 字符的绝对路径转成 \\?\ 前缀, 绕开 MAX_PATH=260.

    v0.4.36: 入口先走 _normalize_windows_path 修 tkinter //?/ 混斜杠坑.
    """
    if os.name != "nt":
        return str(path_str)
    path_str = _normalize_windows_path(path_str)
    if path_str.startswith("\\\\?\\") or path_str.startswith("\\?\\"):
        return path_str
    if len(path_str) < 180:
        return path_str
    if path_str.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path_str.lstrip("\\")
    # 已经是绝对路径的直接用, 避免 abspath 在某些实现下多拼一次 cwd
    if os.path.isabs(path_str):
        abs_s = path_str
    else:
        try:
            abs_s = os.path.abspath(path_str)
        except Exception:
            abs_s = path_str
    if abs_s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + abs_s.lstrip("\\")
    return "\\\\?\\" + abs_s


def _is_regular_file(path_str: str) -> bool:
    r"""os.path.isfile 的长路径安全版本. 优先直接 isfile,
    失败时套 \\?\ 前缀再试 (Windows 长路径场景)."""
    try:
        if os.path.isfile(path_str):
            return True
    except Exception:
        pass
    long_p = _to_long_path(path_str)
    if long_p == path_str:
        return False
    try:
        return os.path.isfile(long_p)
    except Exception:
        return False


def _rel_from_drive_root(p: Path) -> Path:
    """把绝对路径的 anchor (盘符 / UNC 头) 去掉, 返回相对盘根的整段路径.

    - Windows: ``Z:\\a\\b\\c`` -> ``a\\b\\c``
    - UNC    : ``\\\\srv\\share\\x\\y`` -> ``x\\y``  (share 也一起去掉)
    - POSIX  : ``/a/b/c`` -> ``a/b/c``
    - 盘根本身 (``Z:\\``) -> ``Path('.')``

    与 extract/dedupe 的 "marker 位置从盘根往下完整镜像" 规则配套.
    """
    try:
        parts = p.parts
    except Exception:
        return Path(p.name)
    if not parts:
        return Path(".")
    anchor_str = p.anchor  # ``Z:\\`` 或 ``\\\\srv\\share\\`` 或 ``/``
    if anchor_str:
        rest = parts[1:]
    else:
        rest = parts
    if not rest:
        return Path(".")
    return Path(*rest)


def _find_dedupe_targets(root: Path, mode: str,
                         logger=None) -> list[Path]:
    r"""按模式返回要跑 dedupe_pic 的目录列表.

    - single: 只跑 root 本身
    - subdirs: root 下每个一级子目录跑一次
    - recursive: 递归找所有'含 jpg/jpeg/png 的最深层目录',
                 用 os.walk 替代 rglob, 对 Windows 长路径 (>=200 chr)
                 用 \\?\ 前缀重试, 避免长路径下 is_file 静默失败.

    logger: 可选的日志回调 (msg -> None), 用于把扫描过程输出到 GUI 进度栏.
    """
    def _log(msg: str):
        if logger is not None:
            try:
                logger(msg)
            except Exception:
                pass

    if not root.is_dir():
        _log(f"[扫描] target 不是目录: {root}")
        return []
    if mode == "single":
        _log(f"[扫描] single 模式, 直接用 target: {root}")
        return [root]
    if mode == "subdirs":
        subs = sorted([p for p in root.iterdir() if p.is_dir()])
        _log(f"[扫描] subdirs 模式, 一级子目录 {len(subs)} 个")
        return subs

    # recursive: os.walk + 长路径兼容
    exts = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
    out: set[str] = set()
    total_images = 0
    long_path_hits = 0
    root_str = str(root)
    walk_root = _to_long_path(root_str)

    for dirpath, dirnames, filenames in os.walk(walk_root):
        # 剥掉 \?\ 前缀, 结果里存正常路径 (dedupe_pic.exe 会自己处理长路径)
        normal_dirpath = dirpath
        if normal_dirpath.startswith("\\\\?\\UNC\\"):
            normal_dirpath = "\\" + normal_dirpath[len("\\\\?\\UNC\\"):]
        elif normal_dirpath.startswith("\\\\?\\"):
            normal_dirpath = normal_dirpath[len("\\\\?\\"):]

        has_image = False
        for name in filenames:
            lower = name.lower()
            for ext in exts:
                if lower.endswith(ext):
                    total_images += 1
                    if len(os.path.join(normal_dirpath, name)) >= 200:
                        long_path_hits += 1
                    has_image = True
                    break
        if has_image:
            out.add(normal_dirpath)

    _log(f"[扫描] recursive: 遍历完成, 共 {total_images} 张图片 "
         f"(其中 {long_path_hits} 张 >=200 字符长路径)")
    _log(f"[扫描] 归纳出 {len(out)} 个候选目录")
    return sorted(Path(s) for s in out)


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
                base_w=820, base_h=760, min_w=760, min_h=620))
        self.root.minsize(int(760 * self._ui_scale), int(620 * self._ui_scale))
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        try:
            _pg._apply_window_icon(self.root)
        except Exception:
            pass

        # 表单变量
        self._target_var = tk.StringVar(value=self._cfg.get("target", ""))
        # v0.4.37 新增: 图片源根 (跟 extract_gui 的 输出根 对齐), 空=不做镜像 (回退到 v0.4.36 行为)
        self._src_root_var = tk.StringVar(value=self._cfg.get("src_root", ""))
        self._mode_var = tk.StringVar(
            value=self._cfg.get("mode", "recursive"))
        self._threshold_var = tk.IntVar(
            value=int(self._cfg.get("threshold", 3)))
        self._motion_var = tk.DoubleVar(
            value=float(self._cfg.get("motion", 0.12)))
        self._apply_var = tk.BooleanVar(
            value=bool(self._cfg.get("apply", True)))
        # v0.4.45: GUI 不再暴露"永久删除"开关 (没有 trash-dir 时它就是死开关)
        # 保留变量, 恒定 True, 保证 --hard-delete 一直传给 dedupe_pic.exe
        self._hard_delete_var = tk.BooleanVar(value=True)
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
        # 智能自动滚动：手动往上翻则暂停跟随，滚回底部自动恢复
        self._auto_scroll_var = tk.BooleanVar(value=True)

        # v0.4.42 常驻模式：主循环线程 + 统计 + 状态栏
        self._loop_thread: threading.Thread | None = None
        self._loop_stop_event = threading.Event()
        # v0.4.46 子进程组: Windows 用 Job Object 保证 GUI 死后子进程一起死
        self._subproc_group = SubprocGroup()
        # 累计统计（GUI 存活期间累计, 关闭后重置）
        self._stat_done_dirs = 0
        self._stat_deleted_images = 0
        self._stat_lock = threading.Lock()
        # 状态栏文案
        self._status_var = tk.StringVar(value="未启动")
        # 扫描间隔（秒），空转时用 Event.wait 睡这么久
        self._scan_interval_var = tk.IntVar(
            value=int(self._cfg.get("scan_interval", 10)))

        self._build_ui()
        self.root.after(200, self._drain_log_queue)
        self.root.after(300, self._check_environment)

    # ---------- UI ----------

    def _build_ui(self):
        _pg.apply_tab_style(self.root)

        # v0.4.43: 底部按钮条必须先 pack(side="bottom") 占位，
        # 否则窗口太矮时会被上方 expand=True 的 Notebook 挤到窗口外看不见
        bar = ttk.Frame(self.root)
        bar.pack(side="bottom", fill="x", padx=8, pady=(0, 8))
        self._run_btn = ttk.Button(bar, text="▶ 开始去重（持续运行，不点停止不会退出）",
                                    command=self._on_run)
        self._run_btn.pack(side="left")
        self._stop_btn = ttk.Button(bar, text="■ 停止（当前目录跑完再退）",
                                    command=self._on_stop,
                                    state="disabled")
        self._stop_btn.pack(side="left", padx=6)
        ttk.Button(bar, text="最小化到托盘",
                   command=self.hide_to_tray).pack(side="left", padx=6)
        ttk.Button(bar, text="退出", command=self.quit_all).pack(side="right")

        # v0.4.44 永久 footer 状态栏: 跟随 root, 切 tab 也不会消失
        # 分割线 + 状态文本, 都用 side="bottom" 后 pack 就会在 bar 之上、Notebook 之下
        footer = ttk.Frame(self.root)
        footer.pack(side="bottom", fill="x", padx=0, pady=0)
        ttk.Separator(footer, orient="horizontal").pack(fill="x")
        inner = ttk.Frame(footer)
        inner.pack(fill="x", padx=8, pady=4)
        ttk.Label(inner, text="状态：", foreground="#666").pack(side="left")
        ttk.Label(inner, textvariable=self._status_var,
                  foreground="#0066cc").pack(side="left")

        # Notebook 放在按钮条上方，吃掉剩余空间
        nb = ttk.Notebook(self.root)
        nb.pack(side="top", fill="both", expand=True, padx=8, pady=6)

        page = ttk.Frame(nb)
        nb.add(page, text="去重")
        self._build_main_tab(page)

        protect_page = ttk.Frame(nb)
        nb.add(protect_page, text="保护类别")
        self._build_protect_tab(protect_page)

        about = ttk.Frame(nb)
        nb.add(about, text="关于")
        self._build_about_tab(about)

        # 日志 tab（独立出来，避免主页按钮/控件被挤没）
        log_tab = ttk.Frame(nb)
        nb.add(log_tab, text="日志")
        self._build_log_tab(log_tab)

    def _build_main_tab(self, page: ttk.Frame):
        pad = {"padx": 6, "pady": 4}

        # 目标目录
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="去重目标：", width=14).pack(side="left")
        ttk.Entry(row, textvariable=self._target_var, width=60).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览...", command=self._browse_target).pack(
            side="left", padx=4)

        # 图片源根 (对齐 extract_gui 的"输出根", 用来算相对路径, marker 就镜像到这里之下)
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="图片源根：", width=14).pack(side="left")
        ttk.Entry(row, textvariable=self._src_root_var, width=60).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览...", command=self._browse_src_root).pack(
            side="left", padx=4)
        ttk.Label(page,
                  text="  切帧的输出根 (如 Z:\\切帧结果), 用来算 Marker 相对路径; "
                       "空=只用目标叶子名作镜像 (老行为)",
                  foreground="#666").pack(anchor="w", padx=20)

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

        # v0.4.42: 空转扫描间隔（常驻模式）
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="扫描间隔(s)：", width=14).pack(side="left")
        ttk.Spinbox(row, from_=2, to=3600, increment=5, width=8,
                    textvariable=self._scan_interval_var).pack(side="left")
        ttk.Label(row, text="  无新任务时每隔多久重新扫一次目标目录；默认 10 秒",
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
        ttk.Checkbutton(row, text="执行删除（不勾只生成 dedupe_report.csv 报告，不删图）",
                        variable=self._apply_var).pack(side="left", padx=4)

        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Label(row, text="", width=14).pack(side="left")
        ttk.Checkbutton(row, text="场景保护（保留纯色/异常帧）",
                        variable=self._scene_protect_var).pack(
            side="left", padx=4)
        ttk.Checkbutton(row,
                        text=f"强制重跑（忽略 {DEDUP_DONE_MARKER}）",
                        variable=self._force_rerun_var).pack(
            side="left", padx=4)

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

    def _build_log_tab(self, page: ttk.Frame):
        pad = {"padx": 6, "pady": 4}
        # 顶部控制条
        row = ttk.Frame(page); row.pack(fill="x", **pad)
        ttk.Checkbutton(row, text="自动滚到底",
                        variable=self._auto_scroll_var).pack(side="left", padx=4)
        ttk.Label(row, text="（手动上滚会自动暂停跟随，滚回底部恢复）",
                  foreground="#888").pack(side="left", padx=8)

        # 日志区：Text + 纵/横滚动条
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
        # 鼠标滚轮 / 键盘翻页触发时判定是否在底部（暂停自动滚）
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>",
                    "<Prior>", "<Next>", "<Up>", "<Down>",
                    "<Key-Home>", "<Key-End>"):
            self._log_text.bind(seq, self._on_log_user_scroll, add="+")
        self._log_text.config(state="disabled")

    def _on_log_yview(self, first: str, last: str) -> None:
        """Text 的 yscrollcommand：同步滚动条位置 + 到底部时恢复自动滚。"""
        self._log_vsb.set(first, last)
        try:
            lnum = float(last)
        except ValueError:
            return
        if lnum >= 0.9999 and not self._auto_scroll_var.get():
            self._auto_scroll_var.set(True)

    def _on_log_scrollbar(self, *args) -> None:
        """滚动条被拖动时同步 Text，并判断是否离开底部（暂停自动滚）。"""
        self._log_text.yview(*args)
        try:
            _, last = self._log_text.yview()
            if last < 0.9999:
                self._auto_scroll_var.set(False)
        except Exception:
            pass

    def _on_log_user_scroll(self, event=None):
        """鼠标滚轮 / PgUp/Down / Home/End 触发。"""
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

    def _browse_src_root(self):
        init = self._src_root_var.get() or self._target_var.get() or os.path.expanduser("~")
        p = filedialog.askdirectory(initialdir=init, title="选择图片源根 (切帧输出根)")
        if p:
            self._src_root_var.set(p)

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
        # v0.4.42 常驻模式: 点开始 -> 主循环线程 -> 不停扫描 + 处理 + 空转 sleep
        if self._loop_thread and self._loop_thread.is_alive():
            messagebox.showinfo("正在运行", "已经在跑了，请先点停止。")
            return

        # 参数校验
        target = self._target_var.get().strip()
        target = _normalize_windows_path(target)
        if not target or not Path(target).is_dir():
            messagebox.showerror("参数错误", f"目标目录无效：{target}"); return
        exe = _find_dedupe_exe()
        if not exe:
            messagebox.showerror(
                "环境缺失",
                "未找到 dedupe_pic.exe，请放到本 GUI 同目录或 System32 后重试。")
            return
        mr = self._markers_root_var.get().strip()
        if not mr:
            messagebox.showerror("配置缺失",
                                 "请先设置『Marker 根』目录。多机共享盘时所有机器应指向同一位置。")
            return
        mr = _normalize_windows_path(mr)
        markers_root = Path(mr)
        try:
            markers_root.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror("创建失败", f"无法创建 Marker 根目录：{e}")
            return
        sr_raw = self._src_root_var.get().strip()
        if sr_raw:
            sr_norm = _normalize_windows_path(sr_raw)
            if not Path(sr_norm).is_dir():
                messagebox.showerror(
                    "参数错误",
                    f"图片源根不是有效目录：{sr_norm}\n留空=沿用老规则; 填了就必须存在.")
                return

        _save_config(self._dump_cfg())

        # 重置累计统计
        with self._stat_lock:
            self._stat_done_dirs = 0
            self._stat_deleted_images = 0
        self._update_status("启动中…")
        self._log("[启动] 常驻模式：不点停止会一直循环扫描 + 处理")

        # 启动主循环线程
        self._loop_stop_event.clear()
        self._worker_stop_flag.clear()
        self._loop_thread = threading.Thread(
            target=self._loop_run, args=(exe, Path(target), markers_root),
            daemon=True, name="dedupe-loop")
        self._loop_thread.start()

        self._run_btn.config(state="disabled")
        self._stop_btn.config(state="normal")

    def _on_stop(self):
        if not messagebox.askyesno("确认", "确定要停止吗？当前正在处理的目录会跑完再退。"):
            return
        self._loop_stop_event.set()
        self._worker_stop_flag.set()
        self._log("[停止] 已请求停止，等当前目录跑完就退出")
        self._update_status("停止中，等当前目录跑完…")

    # ---------- v0.4.42 常驻主循环 ----------

    def _build_pairs_once(self, target_p: Path,
                          markers_root: Path) -> list[tuple[Path, Path]]:
        """扫一次目标目录, 算 pairs, 过滤已 done 的.
        每一轮循环都会重新调, 用来接住生产端新产出的目录."""
        mode = self._mode_var.get()
        try:
            dirs = _find_dedupe_targets(target_p, mode, logger=self._log)
        except Exception as e:
            self._log(f"[扫描异常] {type(e).__name__}: {e}")
            return []
        if not dirs:
            return []

        sr_raw = self._src_root_var.get().strip()
        src_root: Path | None = None
        if sr_raw:
            src_root = Path(_normalize_windows_path(sr_raw))

        pairs: list[tuple[Path, Path]] = []
        for d in dirs:
            if src_root is not None:
                try:
                    rel = d.relative_to(src_root)
                except Exception:
                    rel = Path(d.name)
                marker_dir = markers_root if str(rel) == "." else markers_root / rel
            else:
                try:
                    rel = d.relative_to(target_p)
                except Exception:
                    rel = Path(d.name)
                marker_dir = markers_root / rel if str(rel) != "." else markers_root
            pairs.append((d, marker_dir))

        # 过滤已完成 marker (除非强制重跑)
        if not self._force_rerun_var.get():
            skipped_n = sum(1 for _, md in pairs
                            if _is_regular_file(str(md / DEDUP_DONE_MARKER)))
            pairs = [(d, md) for d, md in pairs
                     if not _is_regular_file(str(md / DEDUP_DONE_MARKER))]
            if skipped_n:
                self._log(f"[跳过] {skipped_n} 个目录已有 {DEDUP_DONE_MARKER}")
        return pairs

    def _loop_run(self, exe: str, target_p: Path,
                  markers_root: Path) -> None:
        """常驻主循环: 有活儿就干, 没活儿睡 scan_interval 秒.

        - 停止旗触发时立刻退出 (Event.wait 支持提前唤醒)
        - 任何异常 log + sleep, 不让循环崩掉
        """
        round_no = 0
        while not self._loop_stop_event.is_set():
            round_no += 1
            try:
                self._update_status(f"第 {round_no} 轮：扫描中…")
                pairs = self._build_pairs_once(target_p, markers_root)
            except Exception as e:
                self._log(f"[扫描异常] {type(e).__name__}: {e}")
                pairs = []

            if pairs:
                self._log(f"[启动] 本轮 {len(pairs)} 个目录待去重，"
                          f"并发={int(self._dedupe_jobs_var.get())}")
                self._total_dirs = len(pairs)
                self._done_dirs = 0
                self._push_progress()
                try:
                    self._worker_run(exe, pairs)
                except Exception as e:
                    self._log(f"[执行异常] {type(e).__name__}: {e}")
                if self._loop_stop_event.is_set():
                    break
                # 处理完立刻回头再扫, 不睡
                continue

            # 没活儿: 空转睡, 每秒刷新一次状态栏倒计时
            interval = max(2, int(self._scan_interval_var.get()))
            self._log(f"[空转] 未发现待处理目录，{interval}s 后重试")
            for i in range(interval, 0, -1):
                if self._loop_stop_event.is_set():
                    break
                self._update_status(f"空转中，{i}s 后重新扫描")
                if self._loop_stop_event.wait(1.0):
                    break

        self._log("[结束] 主循环已退出")
        self._update_status("已停止")
        self.root.after(0, self._on_worker_finished)

    def _update_status(self, phase: str) -> None:
        """在状态栏上拼: 阶段 + 累计已处理目录 + 累计已删图片."""
        with self._stat_lock:
            d = self._stat_done_dirs
            img = self._stat_deleted_images
        self._status_var.set(
            f"{phase}   |   已处理 {d} 个目录   |   已删除 {img} 张图片")

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

            try:
                # v0.4.46 走 SubprocGroup: 自动绑 Job Object,
                # GUI 死 (正常/强杀/os._exit) 子进程一起死
                proc = self._subproc_group.popen(
                    cmd,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace")
            except FileNotFoundError:
                self._log(f"[{tag}] [错误] 找不到 exe：{exe}")
                return d, 127

            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.rstrip()
                if line:
                    self._log(f"[{tag}] {line}")
                    # v0.4.42 抠 "[删除完成] 成功 N 个" 累加到状态栏
                    m = _DELETED_LINE_RE.search(line)
                    if m:
                        try:
                            n = int(m.group(1))
                        except ValueError:
                            n = 0
                        if n > 0:
                            with self._stat_lock:
                                self._stat_deleted_images += n
                            self._update_status(f"正在处理：{tag}")
                if self._worker_stop_flag.is_set() or self._loop_stop_event.is_set():
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    break
            rc = proc.wait()
            self._log(f"[{tag}] 完成 rc={rc}")
            with done_lock:
                self._done_dirs += 1
            with self._stat_lock:
                self._stat_done_dirs += 1
            self._push_progress()
            self._update_status(f"完成一个目录：{tag}")
            return d, rc

        try:
            with ThreadPoolExecutor(max_workers=jobs,
                                    thread_name_prefix="dedupe") as ex:
                futures = [ex.submit(_run_one, p) for p in pairs]
                for fut in futures:
                    try:
                        fut.result()
                    except Exception as e:
                        self._log(f"[异常] {type(e).__name__}: {e}")
                    if self._worker_stop_flag.is_set() or self._loop_stop_event.is_set():
                        for f in futures:
                            f.cancel()
            if not (self._worker_stop_flag.is_set() or self._loop_stop_event.is_set()):
                self._log(f"[本轮完成] 共处理 {len(pairs)} 个目录，回头继续扫描")
        except Exception as e:
            self._log(f"[异常] {type(e).__name__}: {e}")
        # v0.4.42: 常驻模式下由 _loop_run 控制按钮状态, 这里不再触发 _on_worker_finished

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
        appended = False
        try:
            while True:
                line = self._log_queue.get_nowait()
                self._log_text.config(state="normal")
                self._log_text.insert("end", line)
                self._log_text.config(state="disabled")
                appended = True
        except queue.Empty:
            pass
        if appended and self._auto_scroll_var.get():
            try:
                self._log_text.see("end")
            except Exception:
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
            "scan_interval": int(self._scan_interval_var.get()),
            "lock_ttl": int(self._lock_ttl_var.get()),
            "src_root": self._src_root_var.get(),
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
        self._loop_stop_event.set()
        # v0.4.46 主动杀所有子进程 (dedupe_pic.exe), 避免变孤儿
        try:
            self._subproc_group.terminate_all(wait_timeout=2.0)
        except Exception:
            pass
        try:
            # Windows: close Job handle 触发 KILL_ON_JOB_CLOSE, 兜底再杀一次
            self._subproc_group.close()
        except Exception:
            pass
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
