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
from pathlib import Path
import tkinter as tk
from tkinter import (
    Tk, StringVar, IntVar, DoubleVar, END, DISABLED, NORMAL,
    filedialog, messagebox,
)
from tkinter import ttk

import classify_pic
from classify_pic import (
    ClassifyConfig, DEFAULT_FRONT_KEYWORDS, DEFAULT_IMAGE_EXT, run,
    BUCKET_LIVENESS, BUCKET_KEYPOINT, BUCKET_FRUNK, BUCKET_HOOD, BUCKET_OCCLUSION,
)


APP_TITLE = "pic-clear 二次分类工具"
APP_VERSION = "v0.4.24"
APP_COMPANY = "山东数旗信息科技有限公司"
CONFIG_NAME = "classify_gui.json"

# GUI 里给用户显示的桶列表（跟 classify_pic 里保持一致）
BUCKET_ORDER = [
    BUCKET_LIVENESS, BUCKET_KEYPOINT, BUCKET_FRUNK, BUCKET_HOOD, BUCKET_OCCLUSION,
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
        root.geometry("980x760")

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_flag = threading.Event()

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
        # 每桶阈值
        self.bucket_thres_vars: dict[str, DoubleVar] = {
            b: DoubleVar(value=0.75) for b in BUCKET_ORDER
        }
        # 样例统计标签
        self.sample_count_labels: dict[str, StringVar] = {
            b: StringVar(value="—") for b in BUCKET_ORDER
        }

        self._build_ui()
        self._apply_config(_load_config())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(150, self._drain_log)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        outer = ttk.Frame(nb)
        nb.add(outer, text="  分类  ")
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

        # 中部左：阈值面板
        mid = ttk.Frame(outer)
        mid.pack(fill="x", pady=(6, 4))

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

        # 按钮
        btn = ttk.Frame(outer)
        btn.pack(fill="x", pady=(4, 4))
        self.start_btn = ttk.Button(btn, text="开始", command=self._start)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(btn, text="停止", command=self._stop, state=DISABLED)
        self.stop_btn.pack(side="left", padx=4)
        ttk.Button(btn, text="清空日志", command=self._clear_log).pack(side="left", padx=4)

        # 分类页底部：一段小提示，正式日志在"日志" tab
        hint = ttk.Label(
            outer,
            text="完整日志请切到上方\"日志\" tab 查看（支持上下 / 左右滚动条）",
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
        if self.worker and self.worker.is_alive():
            return
        try:
            cfg = self._build_config()
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"参数错误: {e}")
            return

        self.cancel_flag.clear()
        self._persist_config()   # 每次点开始都存一次，防意外崩溃丢配置
        self.start_btn.configure(state=DISABLED)
        self.stop_btn.configure(state=NORMAL)
        self._log(f"[启动] 输入={cfg.in_root}  输出={cfg.out_root}")

        def _target():
            try:
                run(cfg, log=self._enqueue_log, cancel=self.cancel_flag.is_set)
            except Exception as e:
                self._enqueue_log(f"[FATAL] {e}")
            finally:
                self._enqueue_log("__DONE__")

        self.worker = threading.Thread(target=_target, daemon=True)
        self.worker.start()

    def _stop(self) -> None:
        if not (self.worker and self.worker.is_alive()):
            return
        self.cancel_flag.set()
        self._log("[停止] 请求已发送，等当前图片处理完…")

    def _build_config(self) -> ClassifyConfig:
        in_root = self.in_var.get().strip()
        out_root = self.out_var.get().strip()
        if not in_root or not out_root:
            raise ValueError("输入 / 输出目录不能为空")
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
        )

    # ---------------------------------------------------------- logging
    def _enqueue_log(self, msg: str) -> None:
        self.log_queue.put(msg)

    def _drain_log(self) -> None:
        try:
            while True:
                msg = self.log_queue.get_nowait()
                if msg == "__DONE__":
                    self.start_btn.configure(state=NORMAL)
                    self.stop_btn.configure(state=DISABLED)
                    continue
                self._log(msg)
        except queue.Empty:
            pass
        self.root.after(150, self._drain_log)

    def _log(self, msg: str) -> None:
        self.log.configure(state="normal")
        self.log.insert(END, msg + "\n")
        self.log.see(END)
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", END)
        self.log.configure(state="disabled")

    # -------------------------------------------------- 日志 tab
    def _build_log_tab(self, page: ttk.Frame) -> None:
        toolbar = ttk.Frame(page)
        toolbar.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Button(toolbar, text="清空日志", command=self._clear_log).pack(
            side="left", padx=4
        )
        ttk.Button(toolbar, text="跳到底部",
                   command=lambda: self.log.see(END)).pack(side="left", padx=4)

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

    # -------------------------------------------------- 关于 tab
    def _build_about_tab(self, page: ttk.Frame) -> None:
        pad = {"padx": 12, "pady": 6}
        ttk.Label(page, text=APP_TITLE,
                  font=("Microsoft YaHei", 16, "bold")).pack(pady=(24, 4))
        ttk.Label(page, text=f"版本  {APP_VERSION}",
                  foreground="#0a7").pack(pady=2)
        ttk.Label(page, text=APP_COMPANY,
                  foreground="#c0392b",
                  font=("Microsoft YaHei", 10, "bold")).pack(pady=(4, 12))
        ttk.Label(
            page,
            text=("对已去重图片二次分类：\n"
                  "  · 舱外活体检测（骑行/滑板/三轮）\n"
                  "  · 人体关键点（步行/站立）\n"
                  "  · 前备箱防夹检测\n"
                  "  · 前机盖开关检测（少样本 embedding）\n"
                  "  · 遮挡（少样本 embedding）"),
            foreground="#555", justify="left",
        ).pack(anchor="w", **pad)
        ttk.Label(
            page,
            text=("配置文件：%USERPROFILE%\\.pic-clear\\classify_gui.json\n"
                  "license.lic / otp.secret 与其他 exe 共用"),
            foreground="#888", justify="left", font=("Consolas", 9),
        ).pack(anchor="w", **pad)

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
        }

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
        self._persist_config()
        self.root.destroy()


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
