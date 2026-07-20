# -*- coding: utf-8 -*-
"""
classify_gui.py — classify_pic.py 的 tkinter GUI 前端。

主要功能：
  - 选输入 / 输出 / 样例(rules) 目录
  - 配置过滤关键字、前视关键字
  - 显示已加载样例统计（点"刷新样例"按钮增量更新）
  - 每桶单独 embedding 相似度阈值
  - 后台线程跑 classify_pic.run()，日志 tail 到窗口
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import (
    Tk, StringVar, IntVar, DoubleVar, END, DISABLED, NORMAL,
    BooleanVar, filedialog, messagebox,
)
from tkinter import ttk

import classify_pic
import pipe_gui as _pg  # noqa: E402
from gui_log_util import GuiLogController  # noqa: E402
from tray_util import TrayController, TOOLTIP_CLASSIFY  # noqa: E402

# stats_db 可选; 主流程不能因为落库失败中断
try:
    import stats_db as _stats_db  # type: ignore
except Exception:  # pragma: no cover
    _stats_db = None  # type: ignore
from classify_pic import (
    ClassifyConfig, DEFAULT_FRONT_KEYWORDS, DEFAULT_IMAGE_EXT, run,
    BUCKET_LIVENESS, BUCKET_KEYPOINT, BUCKET_FRUNK, BUCKET_HOOD,
    BUCKET_GESTURE, BUCKET_OCCLUSION,
)


APP_TITLE = "数旗_图片分类工具"
HOTKEY_DEFAULT = "ctrl+alt+f"  # v0.4.88: filter 二次过滤工具, 全局呼出主窗口
# 版本号: CI 会在打包前覆盖 _version.py 里的 VERSION 成 tag 名 (如 v0.4.30);
# 本地跑 py 时 fallback 到 'dev', 找不到 _version.py 也能启动.
try:
    from _version import VERSION as _V
except Exception:
    _V = 'dev'
APP_VERSION = _V
APP_COMPANY = "山东数旗信息科技有限公司"
CONFIG_NAME = "classify_gui.json"

# GUI 里给用户显示的 6 大类桶列表（跟 classify_pic 里保持一致）
BUCKET_ORDER = [
    BUCKET_LIVENESS, BUCKET_KEYPOINT, BUCKET_FRUNK, BUCKET_HOOD,
    BUCKET_GESTURE, BUCKET_OCCLUSION,
]


# ---------- 配置持久化：~/.pic-clear/classify_gui.json ----------

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
        print(f"[配置] 保存失败: {e}", flush=True)


class ClassifyApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        root.title(f"{APP_TITLE}  {APP_VERSION}")
        root.geometry("1000x880")

        # log_queue: 外部 worker (classify_pic) 通过这个 queue 把日志送回 GUI 主线程.
        # 这一层保留, 因为 classify_pic 的 API 期望的就是"外部 put 一个 queue".
        # v0.4.87: 新增 GuiLogController 负责"落盘 + preload + Tab 展示".
        # 数据流: worker -> self.log_queue -> _drain_log -> self._log_ctl.log()
        #        -> ctl 内部 queue -> ctl.pump() -> Text
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self._log_ctl = GuiLogController(app_name="classify_gui")
        self._auto_scroll_var = tk.BooleanVar(value=True)
        self.worker: threading.Thread | None = None
        self.cancel_flag = threading.Event()

        # v0.4.88: 托盘 + 全局快捷键 + 关闭最小化. 老版 classify_gui 没有这套,
        # 现在跟 dedupe/extract 拉齐: 点 X 默认最小化到托盘, 右下角图标
        # 鼠标 hover 显示 "二次过滤工具". 图标 fallback: 紫底 "CL".
        # DPI scale 从 root 拿 (extract_gui / dedupe_gui 走 pipe_gui 注入).
        self._ui_scale = float(getattr(root, "__ui_scale__", 1.0))
        self._minimize_to_tray_var = tk.BooleanVar(value=True)
        self._hotkey_var = tk.StringVar(value=HOTKEY_DEFAULT)
        self._hide_close_hint_var = tk.BooleanVar(value=False)
        self._tray = TrayController(
            root=self.root,
            app_id="pic-clear-classify",
            tooltip=TOOLTIP_CLASSIFY,
            fallback_glyph=((155, 89, 182, 255), "CL"),
            hotkey_default=HOTKEY_DEFAULT,
            app_title=APP_TITLE,
            ui_scale=self._ui_scale,
        )

        # ---- 变量 ----
        self.in_var = StringVar()
        self.out_var = StringVar()
        self.rules_var = StringVar()
        self.filter_var = StringVar(value="")
        self.front_var = StringVar(value=",".join(DEFAULT_FRONT_KEYWORDS))
        self.ext_var = StringVar(value=",".join(DEFAULT_IMAGE_EXT))
        self.camera_var = StringVar(value="camera")
        self.model_var = StringVar(value="")
        self.pose_var = StringVar(value="")
        self.embed_model_var = StringVar(value="")
        self.person_conf_var = DoubleVar(value=0.3)
        self.person_area_var = DoubleVar(value=0.005)
        self.kp_min_var = IntVar(value=8)
        self.limit_var = IntVar(value=0)
        self.embed_default_var = DoubleVar(value=0.75)
        # 多线程 + marker（v0.4.26）
        self.markers_root_var = StringVar(value="")
        self.jobs_var = IntVar(value=1)
        self.lock_ttl_var = IntVar(value=900)
        self.force_rerun_var = BooleanVar(value=False)
        # v0.4.56 常驻循环 & 空转间隔
        self.scan_interval_var = IntVar(value=10)
        self._loop_thread: "threading.Thread | None" = None
        self._loop_stop_event = threading.Event()
        # v0.4.57 全局 footer 状态栏 (切 tab 也能看)
        self._stat_done_cameras = 0
        self._stat_classified_images = 0
        self._stat_lock = threading.Lock()
        self._status_var = StringVar(value="未启动")
        # 每桶阈值
        self.bucket_thres_vars: dict[str, DoubleVar] = {
            b: DoubleVar(value=0.75) for b in BUCKET_ORDER
        }
        # 样例统计标签
        self.sample_count_labels: dict[str, StringVar] = {
            b: StringVar(value="—") for b in BUCKET_ORDER
        }

        self._build_ui()

        # v0.4.73: 启动即打印**多行**环境画像 (含常用路径可达性)
        try:
            from env_probe import probe_and_log
            def _run_env_probe():
                pp = []
                for v in (self.in_var, self.out_var):
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
        self._apply_config(_load_config())
        # v0.4.88: 注入托盘退出流程要跑的业务收尾动作
        self._install_shutdown_hooks()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(150, self._drain_log)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}
        # v0.4.57 永久 footer 状态栏: 跟随 root, 切 tab 也不会消失
        footer = ttk.Frame(self.root)
        footer.pack(side="bottom", fill="x", padx=0, pady=0)
        ttk.Separator(footer, orient="horizontal").pack(fill="x")
        _inner = ttk.Frame(footer)
        _inner.pack(fill="x", padx=8, pady=4)
        ttk.Label(_inner, text="状态：", foreground="#666").pack(side="left")
        ttk.Label(_inner, textvariable=self._status_var,
                  foreground="#0066cc").pack(side="left")

        nb = ttk.Notebook(self.root)
        nb.pack(side="top", fill="both", expand=True, padx=8, pady=8)
        outer = ttk.Frame(nb)
        nb.add(outer, text="  分类  ")
        config_page = ttk.Frame(nb)
        nb.add(config_page, text="  配置  ")
        log_page = ttk.Frame(nb)
        nb.add(log_page, text="  日志  ")
        about = ttk.Frame(nb)
        nb.add(about, text="  关于  ")
        self._nb = nb
        self._log_page = log_page
        self._build_about_tab(about)
        self._build_log_tab(log_page)

        # 顶部：路径 + 参数
        top = ttk.LabelFrame(outer, text="路径与基本参数")
        top.pack(fill="x")

        row = 0

        def add_path_row(parent, label, var, is_dir=True):
            nonlocal row
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="e", **pad)
            ttk.Entry(parent, textvariable=var, width=68).grid(
                row=row, column=1, sticky="we", **pad
            )
            ttk.Button(
                parent, text="浏览…",
                command=lambda: self._pick(var, is_dir),
            ).grid(row=row, column=2, **pad)
            row += 1

        add_path_row(top, "输入根目录", self.in_var, True)
        add_path_row(top, "输出根目录", self.out_var, True)
        add_path_row(top, "样例目录 (rules)", self.rules_var, True)
        add_path_row(top, "Marker 根目录", self.markers_root_var, True)

        def add_text_row(parent, label, var, tip=""):
            nonlocal row
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="e", **pad)
            ttk.Entry(parent, textvariable=var, width=68).grid(
                row=row, column=1, sticky="we", **pad
            )
            if tip:
                ttk.Label(parent, text=tip, foreground="#888").grid(
                    row=row, column=2, sticky="w", **pad
                )
            row += 1

        add_text_row(top, "过滤关键字",     self.filter_var, "逗号分隔，子目录名包含即跳过")
        add_text_row(top, "前视关键字",     self.front_var,  "规则 4，逗号分隔")
        add_text_row(top, "图片后缀",       self.ext_var,    "逗号分隔")
        add_text_row(top, "分水岭目录名",   self.camera_var, "精确匹配的目录名（默认 camera）")

        add_path_row(top, "yolov8n.onnx",       self.model_var, False)
        add_path_row(top, "yolov8n-pose.onnx",  self.pose_var, False)
        add_path_row(top, "mobilenetv3_embed.onnx", self.embed_model_var, False)

        top.columnconfigure(1, weight=1)

        # 阈值面板（放在"配置" tab）
        mid = ttk.Frame(config_page)
        mid.pack(fill="both", expand=True, padx=8, pady=8)

        thr = ttk.LabelFrame(mid, text="YOLO 阈值")
        thr.pack(side="left", fill="both", expand=True, padx=(0, 4))

        def add_thr(row_idx, label, var, w=6):
            ttk.Label(thr, text=label).grid(row=row_idx, column=0, sticky="e", **pad)
            ttk.Entry(thr, textvariable=var, width=w).grid(
                row=row_idx, column=1, sticky="w", **pad
            )

        add_thr(0, "person 置信度",   self.person_conf_var)
        add_thr(1, "person 面积占比", self.person_area_var)
        add_thr(2, "关键点可见下限",  self.kp_min_var, w=4)
        add_thr(3, "limit (0=不限)",  self.limit_var, w=6)

        # 中部右：embedding 阈值（per-bucket）
        emb = ttk.LabelFrame(mid, text="Embedding 相似度阈值 & 样例数")
        emb.pack(side="left", fill="both", expand=True)

        ttk.Label(emb, text="默认阈值").grid(row=0, column=0, sticky="e", **pad)
        ttk.Entry(emb, textvariable=self.embed_default_var, width=6).grid(
            row=0, column=1, sticky="w", **pad
        )
        ttk.Button(emb, text="刷新样例", command=self._refresh_samples).grid(
            row=0, column=2, columnspan=2, sticky="we", **pad
        )

        for i, bucket in enumerate(BUCKET_ORDER, start=1):
            ttk.Label(emb, text=bucket).grid(row=i, column=0, sticky="e", **pad)
            ttk.Entry(emb, textvariable=self.bucket_thres_vars[bucket], width=6).grid(
                row=i, column=1, sticky="w", **pad
            )
            ttk.Label(emb, text="样例:").grid(row=i, column=2, sticky="e", **pad)
            ttk.Label(
                emb, textvariable=self.sample_count_labels[bucket],
                foreground="#0a7",
            ).grid(row=i, column=3, sticky="w", **pad)

        emb.columnconfigure(3, weight=1)

        # 多机 / 并发 / marker
        conc = ttk.LabelFrame(outer, text="多机并发 & 完成标记")
        conc.pack(fill="x", pady=(4, 4))

        ttk.Label(conc, text="并发数").grid(row=0, column=0, sticky="e", **pad)
        ttk.Spinbox(
            conc, from_=1, to=16, increment=1, width=6,
            textvariable=self.jobs_var,
        ).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(
            conc,
            text="camera 目录粒度并发，共享盘多机并发安全",
            foreground="#888",
        ).grid(row=0, column=2, columnspan=3, sticky="w", **pad)

        ttk.Label(conc, text="锁 TTL(s)").grid(row=1, column=0, sticky="e", **pad)
        ttk.Spinbox(
            conc, from_=30, to=86400, increment=60, width=8,
            textvariable=self.lock_ttl_var,
        ).grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(
            conc,
            text="_classify.lock 超时后可被别机抢占（默认 900s = 15 分钟）",
            foreground="#888",
        ).grid(row=1, column=2, columnspan=3, sticky="w", **pad)

        ttk.Label(conc, text="扫描间隔(s)").grid(row=2, column=0, sticky="e", **pad)
        ttk.Spinbox(
            conc, from_=2, to=3600, increment=1, width=8,
            textvariable=self.scan_interval_var,
        ).grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(
            conc,
            text="常驻模式：本轮无待处理目录时，间隔多久重新扫描一次",
            foreground="#888",
        ).grid(row=2, column=2, columnspan=3, sticky="w", **pad)

        ttk.Checkbutton(
            conc,
            text="强制重跑（忽略 _classify_done.marker）",
            variable=self.force_rerun_var,
        ).grid(row=3, column=0, columnspan=4, sticky="w", **pad)

        # 按钮
        btn = ttk.Frame(outer)
        btn.pack(fill="x", pady=(4, 4))
        self.start_btn = ttk.Button(btn, text="开始", command=self._start)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(btn, text="停止", command=self._stop, state=DISABLED)
        self.stop_btn.pack(side="left", padx=4)
        ttk.Button(btn, text="清空日志", command=self._log_ctl.clear_tab).pack(side="left", padx=4)

        # 分类页底部：一段小提示
        hint = ttk.Label(
            outer,
            text="YOLO / Embedding 阈值请到\"配置\" tab 调整；完整日志请切到\"日志\" tab 查看",
            foreground="#888",
        )
        hint.pack(anchor="w", pady=(4, 0))

    # ---------------------------------------------------------- handlers
    def _pick(self, var: StringVar, is_dir: bool) -> None:
        if is_dir:
            p = filedialog.askdirectory(initialdir=var.get() or os.getcwd())
        else:
            p = filedialog.askopenfilename(
                initialdir=var.get() or os.getcwd(),
                filetypes=[("ONNX", "*.onnx"), ("All", "*")],
            )
        if p:
            var.set(p)

    def _refresh_samples(self) -> None:
        """点按钮：加载 rules/ 下的样例（增量），把每桶数量显示到界面。"""
        rules = self.rules_var.get().strip()
        if not rules:
            messagebox.showinfo(APP_TITLE, "请先选择样例目录")
            return
        rules_path = Path(rules).expanduser().resolve()
        if not rules_path.is_dir():
            messagebox.showerror(APP_TITLE, f"样例目录不存在: {rules_path}")
            return

        # 在后台线程里做，避免卡 UI
        for b in BUCKET_ORDER:
            self.sample_count_labels[b].set("加载中…")

        def _target():
            try:
                from embed_detector import EmbedMatcher, resolve_embed_model_path
                emb_path = resolve_embed_model_path(
                    self.embed_model_var.get().strip() or None
                )
                if emb_path is None:
                    self._enqueue_log("[样例] 找不到 mobilenetv3_embed.onnx")
                    for b in BUCKET_ORDER:
                        self.sample_count_labels[b].set("—")
                    return
                m = EmbedMatcher(emb_path, rules_path)
                m.load_prototypes(log=self._enqueue_log)
                # 更新每桶数量
                for b in BUCKET_ORDER:
                    n = m.bucket_sample_count(b)
                    self.sample_count_labels[b].set(str(n) if n else "0")
                # 用户可能在 rules/ 下建了别的桶名（不在 BUCKET_ORDER）
                extra = [
                    b for b in m.loaded_buckets() if b not in BUCKET_ORDER
                ]
                if extra:
                    self._enqueue_log(
                        f"[样例] 检测到未知桶名（会被跳过）: {', '.join(extra)}"
                    )
            except Exception as e:
                self._enqueue_log(f"[样例] 刷新失败: {e}")

        threading.Thread(target=_target, daemon=True).start()

    def _start(self) -> None:
        # v0.4.56 常驻循环: 点开始 -> 后台线程无限循环 (扫描 + 处理 + 空转 sleep)
        if self._loop_thread and self._loop_thread.is_alive():
            messagebox.showinfo(APP_TITLE, "已经在跑了，请先点停止。")
            return
        try:
            cfg = self._build_config()
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"参数错误: {e}")
            return
        # markers_root 强制必填 (_build_config 已校验, 这里再兜一次防未来变更)
        if not str(cfg.markers_root or "").strip():
            messagebox.showerror(APP_TITLE, "Marker 根目录必选")
            return

        self.cancel_flag.clear()
        self._loop_stop_event.clear()
        self._persist_config()   # 每次点开始都存一次，防意外崩溃丢配置
        self.start_btn.configure(state=DISABLED)
        self.stop_btn.configure(state=NORMAL)
        # v0.4.57 重置累计计数 + 状态栏
        with self._stat_lock:
            self._stat_done_cameras = 0
            self._stat_classified_images = 0
        self._update_status("启动中…")
        self._log("[启动] 常驻模式：不点停止会一直循环扫描 + 处理")
        self._log(f"[启动] 输入={cfg.in_root}  输出={cfg.out_root}")

        # 生成本次任务 task_id, 落 task_runs 快照; classify_pic 用 env 读取
        task_id = uuid.uuid4().hex[:16]
        self._current_task_id = task_id
        os.environ["PICCLEAR_TASK_ID"] = task_id
        self._record_run_snapshot(task_id, cfg)

        def _target():
            round_no = 0
            try:
                while not self._loop_stop_event.is_set():
                    round_no += 1
                    self._enqueue_log(f"[扫描] 第 {round_no} 轮开始")
                    self._update_status(f"第 {round_no} 轮：扫描中…")
                    try:
                        stats = run(
                            cfg,
                            log=self._enqueue_log,
                            cancel=lambda: self.cancel_flag.is_set() or self._loop_stop_event.is_set(),
                        )
                        scanned = int(getattr(stats, "scanned", 0) or 0)
                    except Exception as e:
                        self._enqueue_log(f"[异常] {type(e).__name__}: {e}")
                        scanned = 0

                    if scanned > 0:
                        with self._stat_lock:
                            self._stat_classified_images += scanned

                    if self._loop_stop_event.is_set():
                        break

                    if scanned > 0:
                        # 本轮真的干活了, 立刻回头继续扫
                        self._enqueue_log(f"[本轮完成] 处理 {scanned} 张，回头继续扫描")
                        self._update_status(f"本轮完成，处理 {scanned} 张，继续扫描")
                        continue

                    # 空转
                    interval = max(2, int(self.scan_interval_var.get() or 10))
                    self._enqueue_log(f"[空转] 未发现待处理目录，{interval}s 后重试")
                    # 倒计时状态栏
                    for _i in range(interval, 0, -1):
                        if self._loop_stop_event.is_set():
                            break
                        self._update_status(f"空转中，{_i}s 后重新扫描")
                        if self._loop_stop_event.wait(1.0):
                            break
            finally:
                self._enqueue_log("[结束] 主循环已退出")
                self._enqueue_log("__DONE__")

        self._loop_thread = threading.Thread(
            target=_target, daemon=True, name="classify-loop"
        )
        self.worker = self._loop_thread  # 兼容 _drain_log 里的语义
        self._loop_thread.start()

    def _stop(self) -> None:
        if not (self._loop_thread and self._loop_thread.is_alive()):
            return
        if not messagebox.askyesno(APP_TITLE,
                                   "确定要停止吗？当前正在处理的图片会跑完再退出。"):
            return
        self._loop_stop_event.set()
        self.cancel_flag.set()
        self._log("[停止] 已请求停止，等当前批次跑完就退出")
        self._update_status("停止中，等当前批次跑完…")

    def _update_status(self, phase: str) -> None:
        """状态栏: 阶段 + 累计已处理 camera 目录 + 累计已分类图片."""
        with self._stat_lock:
            d = self._stat_done_cameras
            n = self._stat_classified_images
        try:
            self._status_var.set(
                f"{phase}   |   已处理 {d} 个目录   |   已分类 {n} 张图片"
            )
        except Exception:
            pass

    def _build_config(self) -> ClassifyConfig:
        in_root = self.in_var.get().strip()
        out_root = self.out_var.get().strip()
        if not in_root or not out_root:
            raise ValueError("输入 / 输出目录不能为空")
        markers_str = self.markers_root_var.get().strip()
        if not markers_str:
            raise ValueError(
                "Marker 根目录必填。请指定一个（多机共享盘时所有机器应指向同一位置）"
            )
        markers_path = Path(os.path.expanduser(markers_str)).resolve()
        rules_str = self.rules_var.get().strip()
        rules_path = Path(rules_str).expanduser().resolve() if rules_str else None

        per_bucket: dict[str, float] = {}
        default_thres = float(self.embed_default_var.get())
        for b, var in self.bucket_thres_vars.items():
            v = float(var.get())
            if abs(v - default_thres) > 1e-9:
                per_bucket[b] = v

        return ClassifyConfig(
            in_root=Path(os.path.expanduser(in_root)),
            out_root=Path(os.path.expanduser(out_root)),
            filter_keywords=tuple(
                s.strip() for s in self.filter_var.get().split(",") if s.strip()
            ),
            front_keywords=tuple(
                s.strip() for s in self.front_var.get().split(",") if s.strip()
            ),
            image_extensions=tuple(
                s.strip().lower().lstrip(".") for s in self.ext_var.get().split(",") if s.strip()
            ),
            yolo_model=self.model_var.get().strip() or None,
            pose_model=self.pose_var.get().strip() or None,
            embed_model=self.embed_model_var.get().strip() or None,
            rules_dir=rules_path,
            person_conf=float(self.person_conf_var.get()),
            person_area_ratio=float(self.person_area_var.get()),
            kp_visible_min=int(self.kp_min_var.get()),
            embed_sim_default=default_thres,
            embed_sim_per_bucket=per_bucket,
            limit=int(self.limit_var.get()),
            camera_dir_name=self.camera_var.get().strip() or "camera",
            markers_root=markers_path,
            jobs=max(1, int(self.jobs_var.get())),
            lock_ttl=max(30, int(self.lock_ttl_var.get())),
            force_rerun=bool(self.force_rerun_var.get()),
        )

    # ---------------------------------------------------------- logging
    def _enqueue_log(self, msg: str) -> None:
        self.log_queue.put(msg)

    def _drain_log(self) -> None:
        # v0.4.87: 先从 worker queue 抽消息给控制器 (落盘 + 时间戳),
        # 再让控制器 pump 把新行灌进 Text.
        try:
            while True:
                msg = self.log_queue.get_nowait()
                if msg == "__DONE__":
                    self.start_btn.configure(state=NORMAL)
                    self.stop_btn.configure(state=DISABLED)
                    self._update_status("已停止")
                    continue
                # 计数一个 camera 目录完成: classify_pic 里每个 camera 开跑时打 "[camera] " 前缀
                if isinstance(msg, str) and msg.startswith("[camera] "):
                    with self._stat_lock:
                        self._stat_done_cameras += 1
                    self._update_status(f"正在处理：{msg[9:].split(' ')[0][-40:]}")
                self._log(msg)
        except queue.Empty:
            pass
        if self._log_ctl.pump() and self._auto_scroll_var.get():
            try:
                self.log.see(END)
            except Exception:
                pass
        self.root.after(150, self._drain_log)

    def _log(self, msg: str) -> None:
        # v0.4.87: 转发到 GuiLogController (加时间戳 + 落盘 + 塞 controller queue).
        # 下一次 _drain_log 里 pump() 就会把该行灌进 Text.
        self._log_ctl.log(msg)

    # -------------------------------------------------- 日志 tab
    def _build_log_tab(self, page: ttk.Frame) -> None:
        # v0.4.87: 工具条走 GuiLogController.build_toolbar
        # (清空日志(Tab) / 打开日志文件夹 / 当前日志文件名).
        # "跳到底部" + "自动滚到底" 通过 extra_toolbar 追加, 保留老功能.
        def _extra(tb):
            ttk.Checkbutton(tb, text="自动滚到底",
                            variable=self._auto_scroll_var
                            ).pack(side="left", padx=4)
            ttk.Button(tb, text="跳到底部",
                       command=lambda: self.log.see(END)
                       ).pack(side="left", padx=4)
        self._log_ctl.build_toolbar(page, extra_toolbar=_extra)

        wrap = ttk.Frame(page)
        wrap.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        self.log = tk.Text(
            wrap, height=28, wrap="none",
            font=("Consolas", 10),
        )
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.log.yview)
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.log.xview)
        self.log.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.log.configure(state="disabled")

        # v0.4.87: Text 绑给控制器 + preload 上次 tail 200 行
        self._log_ctl.attach_text(self.log)
        self._log_ctl.preload_prev_tail_to_text()

    # -------------------------------------------------- 关于 tab
    def _build_about_tab(self, page: ttk.Frame) -> None:
        # v0.4.55 关于页面布局改造: 与 dedupe_gui 风格对齐
        #   外层 wrap 收窄内容, 标题块 -> LabelFrame(应用说明) -> LabelFrame(配置文件)
        #   -> LabelFrame(内嵌模块版本)
        wrap = ttk.Frame(page)
        wrap.pack(fill="both", expand=True, padx=12, pady=8)

        # 标题块
        ttk.Label(wrap, text=APP_TITLE,
                  font=("Microsoft YaHei", 16, "bold")).pack(pady=(20, 4))
        ttk.Label(wrap, text=f"版本  {APP_VERSION}",
                  foreground="#555").pack(pady=2)
        ttk.Label(wrap, text=APP_COMPANY,
                  foreground="#c0392b",
                  font=("Microsoft YaHei", 10, "bold")).pack(pady=(4, 16))

        # 应用说明
        desc_frame = ttk.LabelFrame(wrap, text="应用说明")
        desc_frame.pack(fill="x", padx=4, pady=6)
        ttk.Label(
            desc_frame,
            text=("对已去重图片二次分类：\n"
                  "  · 舱外活体检测（骑行/滑板/三轮）\n"
                  "  · 人体关键点（步行/站立）\n"
                  "  · 前备箱防夹检测\n"
                  "  · 前机盖开关检测（少样本 embedding）\n"
                  "  · 动态手势（少样本 embedding）\n"
                  "  · 遮挡（少样本 embedding）\n\n"
                  "支持多机并发：camera 目录级 _classify.lock + _classify_done.marker\n"
                  "多机共享盘时所有机器请指向同一 Marker 根目录"),
            foreground="#555", justify="left",
        ).pack(anchor="w", padx=10, pady=8)

        # 配置文件位置
        cfg_frame = ttk.LabelFrame(wrap, text="配置文件")
        cfg_frame.pack(fill="x", padx=4, pady=6)
        ttk.Label(
            cfg_frame,
            text=("配置文件：%USERPROFILE%\\.pic-clear\\classify_gui.json\n"
                  "license.lic / otp.secret 与其他 exe 共用"),
            foreground="#888", justify="left", font=("Consolas", 9),
        ).pack(anchor="w", padx=10, pady=8)

        # 内嵌模块版本 (classify_pic 是 import 进来的, 跟 GUI 同版本, 走静态渲染)
        core_frame = ttk.LabelFrame(wrap, text="内嵌模块 (classify_pic)")
        core_frame.pack(fill="x", padx=4, pady=6)
        _pg.render_static_version_frame(
            core_frame,
            label_text="模块版本",
            version_text=f"classify_pic {APP_VERSION}",
            extra_note="classify_pic 与本 GUI 同版本一起打包, 无独立 exe",
        )

        # v0.4.88: 托盘 & 快捷键 LabelFrame (跟 dedupe / extract 一致的关于页布局)
        tray_frame = ttk.LabelFrame(wrap, text="托盘 & 快捷键")
        tray_frame.pack(fill="x", padx=4, pady=6)
        row = ttk.Frame(tray_frame); row.pack(fill="x", padx=10, pady=(8, 2))
        ttk.Checkbutton(row, text="关闭时最小化到托盘",
                        variable=self._minimize_to_tray_var
                        ).pack(side="left")
        row = ttk.Frame(tray_frame); row.pack(fill="x", padx=10, pady=(2, 8))
        ttk.Label(row, text="全局快捷键：").pack(side="left")
        ttk.Entry(row, textvariable=self._hotkey_var, width=16
                  ).pack(side="left", padx=(2, 6))
        ttk.Button(row, text="注册", command=self._register_hotkey
                   ).pack(side="left")
        ttk.Label(row, text="  (默认 ctrl+alt+f, 全局呼出主窗口)",
                  foreground="#888").pack(side="left", padx=8)

    # -------------------------------------------------- 配置持久化
    def _dump_config(self) -> dict:
        return {
            "in_root": self.in_var.get(),
            "out_root": self.out_var.get(),
            "rules_dir": self.rules_var.get(),
            "filter_keywords": self.filter_var.get(),
            "front_keywords": self.front_var.get(),
            "image_extensions": self.ext_var.get(),
            "yolo_model": self.model_var.get(),
            "pose_model": self.pose_var.get(),
            "embed_model": self.embed_model_var.get(),
            "person_conf": float(self.person_conf_var.get()),
            "person_area": float(self.person_area_var.get()),
            "kp_visible_min": int(self.kp_min_var.get()),
            "limit": int(self.limit_var.get()),
            "embed_default": float(self.embed_default_var.get()),
            "camera_dir_name": self.camera_var.get(),
            "bucket_thres": {
                b: float(v.get()) for b, v in self.bucket_thres_vars.items()
            },
            "markers_root": self.markers_root_var.get(),
            "jobs": int(self.jobs_var.get()),
            "lock_ttl": int(self.lock_ttl_var.get()),
            "force_rerun": bool(self.force_rerun_var.get()),
            "scan_interval": int(self.scan_interval_var.get()),
            # v0.4.88 托盘相关
            "minimize_to_tray": bool(self._minimize_to_tray_var.get()),
            "hotkey": self._hotkey_var.get(),
            "hide_close_hint": bool(self._hide_close_hint_var.get()),
        }

    def _record_run_snapshot(self, task_id: str, cfg) -> None:
        """把本次分类任务的完整 GUI 配置落 task_runs 表. 静默失败."""
        if _stats_db is None:
            return
        try:
            snap = self._dump_config()
            snap["_task_id"] = task_id
            snap["_in_root_norm"] = str(cfg.in_root)
            snap["_out_root_norm"] = str(cfg.out_root)
            try:
                snap["_markers_root_norm"] = (
                    str(cfg.markers_root) if cfg.markers_root else ""
                )
            except Exception:
                pass
            _stats_db.record_task_run(
                task_id=task_id,
                task_type="classify",
                config=snap,
                cmdline=None,
                version=APP_VERSION,
            )
            self._log(f"[stats] task_id={task_id}")
        except Exception as e:
            print(f"[record_run_snapshot] {type(e).__name__}: {e}",
                  file=sys.stderr)

    def _apply_config(self, cfg: dict) -> None:
        if not cfg:
            return
        def _set(var, key, cast=str):
            if key in cfg and cfg[key] is not None:
                try:
                    var.set(cast(cfg[key]))
                except Exception:
                    pass
        _set(self.in_var, "in_root")
        _set(self.out_var, "out_root")
        _set(self.rules_var, "rules_dir")
        _set(self.filter_var, "filter_keywords")
        _set(self.front_var, "front_keywords")
        _set(self.ext_var, "image_extensions")
        _set(self.model_var, "yolo_model")
        _set(self.pose_var, "pose_model")
        _set(self.embed_model_var, "embed_model")
        _set(self.person_conf_var, "person_conf", float)
        _set(self.person_area_var, "person_area", float)
        _set(self.kp_min_var, "kp_visible_min", int)
        _set(self.limit_var, "limit", int)
        _set(self.camera_var, "camera_dir_name")
        _set(self.embed_default_var, "embed_default", float)
        _set(self.markers_root_var, "markers_root")
        _set(self.jobs_var, "jobs", int)
        _set(self.lock_ttl_var, "lock_ttl", int)
        _set(self.force_rerun_var, "force_rerun", bool)
        _set(self.scan_interval_var, "scan_interval", int)
        # v0.4.88 托盘相关
        _set(self._minimize_to_tray_var, "minimize_to_tray", bool)
        _set(self._hotkey_var, "hotkey")
        _set(self._hide_close_hint_var, "hide_close_hint", bool)
        thres = cfg.get("bucket_thres") or {}
        for b, v in self.bucket_thres_vars.items():
            if b in thres:
                try:
                    v.set(float(thres[b]))
                except Exception:
                    pass

    def _persist_config(self) -> None:
        try:
            _save_config(self._dump_config())
        except Exception as e:
            self._log(f"[配置] 保存失败: {e}")

    def _on_close(self) -> None:
        # v0.4.88: 点 × 默认最小化到托盘, 取消勾选才真正退出.
        self._persist_config()
        if self._minimize_to_tray_var.get():
            self._tray.hide_to_tray()
            self._tray.maybe_show_close_hint(
                cfg_get=lambda: bool(self._hide_close_hint_var.get()),
                cfg_set=self._persist_hide_close_hint,
                hotkey_display=self._hotkey_var.get(),
            )
        else:
            self.quit_all()

    def _persist_hide_close_hint(self, val: bool) -> None:
        self._hide_close_hint_var.set(bool(val))
        try:
            self._persist_config()
        except Exception:
            pass

    # v0.4.88: 托盘转发 API, 供 GUI 内部 / 关于 tab 按钮调用
    def hide_to_tray(self) -> None:
        self._tray.hide_to_tray()

    def show_main(self) -> None:
        self._tray.show_main()

    def quit_all(self) -> None:
        self._tray.quit_all()

    def _install_shutdown_hooks(self) -> None:
        """业务收尾: 停 worker 循环 + cancel flag, 注入托盘退出流程."""
        self._tray.add_shutdown_hook(self.cancel_flag.set)
        self._tray.add_shutdown_hook(self._loop_stop_event.set)

    def _register_hotkey(self) -> None:
        hk = self._hotkey_var.get().strip() or HOTKEY_DEFAULT
        ok, msg = self._tray.register_hotkey(hk)
        if ok:
            messagebox.showinfo("已注册", msg)
        else:
            messagebox.showerror("注册失败", msg)


def _check_license_or_die_gui() -> None:
    try:
        from licensing import get_fingerprint, verify_license
    except ImportError as e:
        messagebox.showerror(APP_TITLE, f"无法加载 licensing 模块: {e}")
        sys.exit(2)

    env_lic = os.environ.get("DEDUPE_LICENSE")
    if env_lic:
        license_path = Path(env_lic).expanduser().resolve()
    elif getattr(sys, "frozen", False):
        license_path = Path(sys.executable).resolve().parent / "license.lic"
    else:
        license_path = Path.cwd() / "license.lic"

    ok, msg = verify_license(license_path)
    if ok:
        return
    fp = get_fingerprint()
    messagebox.showerror(
        APP_TITLE,
        f"授权失败：{msg}\n\n本机指纹：{fp}\n\n请将指纹发给作者获取 license.lic。",
    )
    sys.exit(3)


def main() -> int:
    # 支持 --skip-license 调试
    if "--skip-license" not in sys.argv:
        _check_license_or_die_gui()
    root = Tk()
    ClassifyApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
