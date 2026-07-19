# -*- coding: utf-8 -*-
"""stats_viewer_gui.py — pic-clear 统计查看器 (5 Tab)

功能:
  - 扫描目录 (默认 ~/.pic-clear) 下所有 stats_*.db, 聚合读多机数据
  - 5 个 Tab: 抽帧 / 去重 / 分类 / 每日趋势(图表) / 日志
  - 每个数据 Tab 支持: 时间范围过滤 + 机器过滤 + 表格 + 汇总柱状图
  - 图表用 matplotlib, 缺失则给出安装提示 (不 crash)

打包:
  可以直接 py 跑, 或 PyInstaller 打成 stats_viewer_gui.exe
"""

from __future__ import annotations

import json
import os
import sys
import threading
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, ttk

try:
    import stats_db as _stats_db  # type: ignore
except Exception as e:  # pragma: no cover
    print(f"[FATAL] 无法导入 stats_db: {e}", file=sys.stderr)
    raise


# ------------------------------------------------ matplotlib 软依赖

_MPL_OK = True
_MPL_ERR = ""
try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import (
        FigureCanvasTkAgg,
        NavigationToolbar2Tk,
    )
    # 中文字体兜底 (Windows 用微软雅黑, mac 用 PingFang)
    for _f in ("Microsoft YaHei", "SimHei", "PingFang SC",
               "Heiti SC", "Arial Unicode MS"):
        try:
            matplotlib.rcParams["font.sans-serif"] = [_f]
            break
        except Exception:
            pass
    matplotlib.rcParams["axes.unicode_minus"] = False
except Exception as _e:
    _MPL_OK = False
    _MPL_ERR = f"{type(_e).__name__}: {_e}"


# ------------------------------------------------ 版本

def _read_version() -> str:
    try:
        from _version import VERSION
        return VERSION
    except Exception:
        return "dev"


APP_TITLE = f"pic-clear 统计查看器  {_read_version()}"


# ------------------------------------------------ 主界面

class StatsViewerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1200x780")

        # 数据缓存
        self._extract_rows: list[dict] = []
        self._dedupe_rows: list[dict] = []
        self._classify_rows: list[dict] = []
        self._db_files: list[Path] = []

        # scan_dir
        default_dir = Path.home() / ".pic-clear"
        self.scan_dir_var = tk.StringVar(value=str(default_dir))
        self.range_var = tk.StringVar(value="最近 7 天")
        self.host_var = tk.StringVar(value="全部")

        self._build_topbar()
        self._build_tabs()
        self._build_footer()

        self._log(f"就绪. 版本 {_read_version()}")

        # v0.4.74: 启动即打印环境画像到日志
        try:
            from env_probe import probe_and_log
            self.root.after(100, lambda: probe_and_log(self._log))
        except Exception as _e:
            try: self._log(f"[ENV] probe_and_log 失败: {type(_e).__name__}: {_e}")
            except Exception: pass
        if not _MPL_OK:
            self._log(f"[提示] 未装 matplotlib, 图表 Tab 不可用. "
                      f"pip install matplotlib 后重启. err={_MPL_ERR}")
        self.refresh_async()

    # ---------- UI 骨架

    def _build_topbar(self) -> None:
        bar = ttk.Frame(self.root)
        bar.pack(side="top", fill="x", padx=8, pady=6)

        ttk.Label(bar, text="扫描目录:").pack(side="left")
        ent = ttk.Entry(bar, textvariable=self.scan_dir_var, width=48)
        ent.pack(side="left", padx=4)
        ttk.Button(bar, text="浏览...",
                   command=self._on_pick_dir).pack(side="left")

        ttk.Label(bar, text="  时间:").pack(side="left")
        ttk.Combobox(
            bar, textvariable=self.range_var, width=10, state="readonly",
            values=("最近 1 天", "最近 7 天", "最近 30 天", "全部"),
        ).pack(side="left")

        ttk.Label(bar, text="  机器:").pack(side="left")
        self._host_cb = ttk.Combobox(
            bar, textvariable=self.host_var, width=16, state="readonly",
            values=("全部",),
        )
        self._host_cb.pack(side="left")

        ttk.Button(bar, text="刷新",
                   command=self.refresh_async).pack(side="left", padx=8)

    def _build_tabs(self) -> None:
        nb = ttk.Notebook(self.root)
        nb.pack(side="top", fill="both", expand=True, padx=8, pady=4)

        self.tab_extract = ttk.Frame(nb)
        self.tab_dedupe = ttk.Frame(nb)
        self.tab_classify = ttk.Frame(nb)
        self.tab_trend = ttk.Frame(nb)
        self.tab_log = ttk.Frame(nb)

        nb.add(self.tab_extract, text="抽帧")
        nb.add(self.tab_dedupe, text="去重")
        nb.add(self.tab_classify, text="分类")
        nb.add(self.tab_trend, text="每日趋势")
        nb.add(self.tab_log, text="日志")

        self._build_extract_tab()
        self._build_dedupe_tab()
        self._build_classify_tab()
        self._build_trend_tab()
        self._build_log_tab()

    def _build_footer(self) -> None:
        self.footer_var = tk.StringVar(value="就绪")
        bar = ttk.Frame(self.root, relief="sunken")
        bar.pack(side="bottom", fill="x")
        ttk.Label(bar, textvariable=self.footer_var,
                  anchor="w").pack(side="left", fill="x", expand=True,
                                    padx=8, pady=2)

    # ---------- Tab: 抽帧

    def _build_extract_tab(self) -> None:
        pw = ttk.PanedWindow(self.tab_extract, orient="vertical")
        pw.pack(fill="both", expand=True)

        # 上: 汇总柱状图
        top = ttk.Frame(pw)
        pw.add(top, weight=2)
        self._extract_chart_frame = ttk.Frame(top)
        self._extract_chart_frame.pack(fill="both", expand=True)

        # 下: 明细表
        bot = ttk.Frame(pw)
        pw.add(bot, weight=3)
        cols = ("ts", "host", "video_path", "frames", "stage",
                "elapsed_sec", "fps")
        tv = _make_table(bot, cols, headers=(
            "时间", "机器", "视频", "帧数", "结果", "耗时(s)", "fps",
        ), on_double_click=lambda row:
           self._show_row_detail("抽帧记录", row, "extract"))
        self._extract_tv = tv

    # ---------- Tab: 去重

    def _build_dedupe_tab(self) -> None:
        pw = ttk.PanedWindow(self.tab_dedupe, orient="vertical")
        pw.pack(fill="both", expand=True)

        top = ttk.Frame(pw)
        pw.add(top, weight=2)
        self._dedupe_chart_frame = ttk.Frame(top)
        self._dedupe_chart_frame.pack(fill="both", expand=True)

        bot = ttk.Frame(pw)
        pw.add(bot, weight=3)
        cols = ("ts", "host", "dir_path", "total", "deleted", "remain",
                "freed_mb", "elapsed_sec")
        tv = _make_table(bot, cols, headers=(
            "时间", "机器", "目录", "总数", "删除", "剩余",
            "释放(MB)", "耗时(s)",
        ), on_double_click=lambda row:
           self._show_row_detail("去重记录", row, "dedupe"))
        self._dedupe_tv = tv

    # ---------- Tab: 分类

    def _build_classify_tab(self) -> None:
        pw = ttk.PanedWindow(self.tab_classify, orient="vertical")
        pw.pack(fill="both", expand=True)

        top = ttk.Frame(pw)
        pw.add(top, weight=2)
        self._classify_chart_frame = ttk.Frame(top)
        self._classify_chart_frame.pack(fill="both", expand=True)

        bot = ttk.Frame(pw)
        pw.add(bot, weight=3)
        cols = ("ts", "host", "camera_dir", "scanned", "copied_bucket",
                "buckets", "elapsed_sec")
        tv = _make_table(bot, cols, headers=(
            "时间", "机器", "camera", "扫描", "复制到桶",
            "桶分布", "耗时(s)",
        ), on_double_click=lambda row:
           self._show_row_detail("分类记录", row, "classify"))
        self._classify_tv = tv

    # ---------- Tab: 每日趋势

    def _build_trend_tab(self) -> None:
        self._trend_chart_frame = ttk.Frame(self.tab_trend)
        self._trend_chart_frame.pack(fill="both", expand=True)

    # ---------- Tab: 日志

    def _build_log_tab(self) -> None:
        frame = ttk.Frame(self.tab_log)
        frame.pack(fill="both", expand=True)
        self._log_text = tk.Text(frame, wrap="none")
        sb_y = ttk.Scrollbar(frame, orient="vertical",
                             command=self._log_text.yview)
        sb_x = ttk.Scrollbar(frame, orient="horizontal",
                             command=self._log_text.xview)
        self._log_text.configure(yscrollcommand=sb_y.set,
                                 xscrollcommand=sb_x.set)
        self._log_text.grid(row=0, column=0, sticky="nsew")
        sb_y.grid(row=0, column=1, sticky="ns")
        sb_x.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

    # ---------- 公共

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        try:
            self._log_text.insert("end", line)
            self._log_text.see("end")
        except Exception:
            print(line, end="")

    def _on_pick_dir(self) -> None:
        d = filedialog.askdirectory(
            initialdir=self.scan_dir_var.get() or str(Path.home()),
            title="选择 stats_*.db 所在目录",
        )
        if d:
            self.scan_dir_var.set(d)
            self.refresh_async()

    # ---------- 数据加载

    def refresh_async(self) -> None:
        self.footer_var.set("读取数据中...")
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self) -> None:
        try:
            scan_dir = self.scan_dir_var.get().strip() or str(
                Path.home() / ".pic-clear"
            )
            self._db_files = _stats_db.iter_db_files(scan_dir)
            self._log(f"扫到 {len(self._db_files)} 个 db: {scan_dir}")

            where, params = self._build_time_filter()
            self._extract_rows = _stats_db.query_all(
                self._db_files, "extract_stats",
                where_sql=where, params=params,
                order_by="ts DESC", limit=5000,
            )
            self._dedupe_rows = _stats_db.query_all(
                self._db_files, "dedupe_stats",
                where_sql=where, params=params,
                order_by="ts DESC", limit=5000,
            )
            self._classify_rows = _stats_db.query_all(
                self._db_files, "classify_stats",
                where_sql=where, params=params,
                order_by="ts DESC", limit=5000,
            )

            # host 下拉
            hosts = sorted({
                r.get("host", "")
                for r in (self._extract_rows + self._dedupe_rows
                          + self._classify_rows)
                if r.get("host")
            })
            self.root.after(0, self._on_data_ready, hosts)
        except Exception as e:
            self._log(f"[错误] refresh 失败: {type(e).__name__}: {e}")
            self.root.after(0,
                            lambda: self.footer_var.set(f"读取失败: {e}"))

    def _on_data_ready(self, hosts: list[str]) -> None:
        self._host_cb["values"] = ["全部"] + hosts
        self._apply_host_filter_and_render()

    def _apply_host_filter_and_render(self) -> None:
        h = self.host_var.get().strip()
        ext = self._extract_rows
        ded = self._dedupe_rows
        clf = self._classify_rows
        if h and h != "全部":
            ext = [r for r in ext if r.get("host") == h]
            ded = [r for r in ded if r.get("host") == h]
            clf = [r for r in clf if r.get("host") == h]
        self._render_extract(ext)
        self._render_dedupe(ded)
        self._render_classify(clf)
        self._render_trend(ext, ded, clf)
        self.footer_var.set(
            f"抽帧 {len(ext)} 条 / 去重 {len(ded)} 条 / 分类 {len(clf)} 条"
        )

    def _build_time_filter(self) -> tuple[str, tuple]:
        r = self.range_var.get()
        if r == "全部":
            return "", ()
        days = {"最近 1 天": 1, "最近 7 天": 7, "最近 30 天": 30}.get(r, 7)
        since = (datetime.now() - timedelta(days=days)
                 ).strftime("%Y-%m-%d %H:%M:%S")
        return "ts >= ?", (since,)

    # ---------- 详情弹层 (三个 Tab 双击共用)

    def _show_row_detail(self, title: str, row: dict,
                         task_type: str) -> None:
        """弹一个 modal 展示单行完整数据 + 关联的 task_run 配置快照."""
        try:
            _open_row_detail_dialog(
                parent=self.root, title=title,
                row=row,
                task_run=_stats_db.query_task_run(
                    self._db_files, row.get("task_id") or "",
                ),
                task_type=task_type,
            )
        except Exception as e:
            self._log(f"[错误] 弹层失败: {type(e).__name__}: {e}")

    # ---------- 渲染各 Tab

    def _render_extract(self, rows: list[dict]) -> None:
        # 表格
        _fill_table(self._extract_tv, rows, [
            ("ts", "ts"),
            ("host", "host"),
            ("video_path", "video_path"),
            ("frames", "frames"),
            ("stage", "stage"),
            ("elapsed_sec", lambda r: f"{(r.get('elapsed_sec') or 0):.1f}"),
            ("fps", "fps"),
        ])
        # 柱状图: 按机器汇总"帧数/耗时"
        _plot_bar(
            self._extract_chart_frame,
            title="抽帧汇总 (按机器)",
            data=_group_bar(rows, "host",
                            frames=lambda r: r.get("frames") or 0,
                            elapsed=lambda r: r.get("elapsed_sec") or 0),
            series=("frames", "elapsed"),
            series_label=("总帧数", "总耗时(s)"),
        )

    def _render_dedupe(self, rows: list[dict]) -> None:
        _fill_table(self._dedupe_tv, rows, [
            ("ts", "ts"),
            ("host", "host"),
            ("dir_path", "dir_path"),
            ("total", "total"),
            ("deleted", "deleted"),
            ("remain", "remain"),
            ("freed_mb",
             lambda r: f"{(r.get('freed_bytes') or 0) / 1024 / 1024:.1f}"),
            ("elapsed_sec",
             lambda r: f"{(r.get('elapsed_sec') or 0):.1f}"),
        ])
        _plot_bar(
            self._dedupe_chart_frame,
            title="去重汇总 (按机器)",
            data=_group_bar(rows, "host",
                            deleted=lambda r: r.get("deleted") or 0,
                            freed_mb=lambda r:
                                (r.get("freed_bytes") or 0) / 1024 / 1024),
            series=("deleted", "freed_mb"),
            series_label=("删除张数", "释放(MB)"),
        )

    def _render_classify(self, rows: list[dict]) -> None:
        def _fmt_buckets(r: dict) -> str:
            raw = r.get("bucket_json") or "{}"
            try:
                d = json.loads(raw)
                return " ".join(f"{k}={v}" for k, v in d.items() if v)
            except Exception:
                return raw

        _fill_table(self._classify_tv, rows, [
            ("ts", "ts"),
            ("host", "host"),
            ("camera_dir", "camera_dir"),
            ("scanned", "scanned"),
            ("copied_bucket", "copied_bucket"),
            ("buckets", _fmt_buckets),
            ("elapsed_sec",
             lambda r: f"{(r.get('elapsed_sec') or 0):.1f}"),
        ])

        # 桶柱状图: 6 个桶的总数
        bucket_totals: dict[str, int] = {}
        for r in rows:
            try:
                d = json.loads(r.get("bucket_json") or "{}")
                for k, v in d.items():
                    bucket_totals[k] = bucket_totals.get(k, 0) + int(v or 0)
            except Exception:
                pass
        # 组装成 _plot_bar 期望格式: {桶名: {count: N}}
        data = {k: {"count": v} for k, v in bucket_totals.items()}
        _plot_bar(
            self._classify_chart_frame,
            title="分类汇总 (按桶)",
            data=data,
            series=("count",),
            series_label=("命中张数",),
        )

    def _render_trend(self, ext: list[dict], ded: list[dict],
                      clf: list[dict]) -> None:
        # 按天汇总 3 条线 (抽帧张数 / 去重删除 / 分类扫描)
        def _by_day(rows: list[dict], value_fn) -> dict[str, float]:
            out: dict[str, float] = {}
            for r in rows:
                ts = (r.get("ts") or "")[:10]  # YYYY-MM-DD
                if not ts:
                    continue
                out[ts] = out.get(ts, 0) + float(value_fn(r) or 0)
            return out

        ext_day = _by_day(ext, lambda r: r.get("frames") or 0)
        ded_day = _by_day(ded, lambda r: r.get("deleted") or 0)
        clf_day = _by_day(clf, lambda r: r.get("scanned") or 0)

        days = sorted(set(ext_day) | set(ded_day) | set(clf_day))
        series = [
            ("抽帧张数", [ext_day.get(d, 0) for d in days]),
            ("去重删除", [ded_day.get(d, 0) for d in days]),
            ("分类扫描", [clf_day.get(d, 0) for d in days]),
        ]
        _plot_line(self._trend_chart_frame,
                   title="每日趋势",
                   x_labels=days,
                   series=series)


# ------------------------------------------------ 表格 helper

def _make_table(parent, cols: tuple[str, ...],
                headers: tuple[str, ...],
                on_double_click=None) -> ttk.Treeview:
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True)
    tv = ttk.Treeview(frame, columns=cols, show="headings", height=20)
    for c, h in zip(cols, headers):
        tv.heading(c, text=h)
        w = 90
        if c in ("video_path", "dir_path", "camera_dir"):
            w = 380
        elif c in ("buckets",):
            w = 240
        tv.column(c, width=w, anchor="w", stretch=True)
    sb_y = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
    sb_x = ttk.Scrollbar(frame, orient="horizontal", command=tv.xview)
    tv.configure(yscrollcommand=sb_y.set, xscrollcommand=sb_x.set)
    tv.grid(row=0, column=0, sticky="nsew")
    sb_y.grid(row=0, column=1, sticky="ns")
    sb_x.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    # iid -> 原始 row dict, 供双击弹层用
    tv._row_data_map = {}  # type: ignore[attr-defined]
    if on_double_click is not None:
        def _handle(_evt):
            iid = tv.focus() or (tv.selection()[0] if tv.selection() else "")
            if not iid:
                return
            row = getattr(tv, "_row_data_map", {}).get(iid)
            if row is not None:
                on_double_click(row)
        tv.bind("<Double-1>", _handle)
        tv.bind("<Return>", _handle)
    return tv


def _fill_table(tv: ttk.Treeview, rows: list[dict],
                col_map: list[tuple[str, object]]) -> None:
    for iid in tv.get_children():
        tv.delete(iid)
    tv._row_data_map = {}  # type: ignore[attr-defined]
    for r in rows:
        vals = []
        for _, key in col_map:
            if callable(key):
                v = key(r)
            else:
                v = r.get(key, "")
            vals.append("" if v is None else v)
        iid = tv.insert("", "end", values=vals)
        tv._row_data_map[iid] = r  # type: ignore[attr-defined]


# ------------------------------------------------ 图表 helper

def _group_bar(rows: list[dict], group_key: str,
               **value_fns) -> dict[str, dict[str, float]]:
    """按 group_key 聚合. value_fns 是 {系列名: fn(row)->number}."""
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        k = r.get(group_key) or "-"
        bucket = out.setdefault(k, {})
        for series_name, fn in value_fns.items():
            bucket[series_name] = bucket.get(series_name, 0) + float(
                fn(r) or 0
            )
    return out


def _clear_frame(frame: tk.Widget) -> None:
    for w in frame.winfo_children():
        try:
            w.destroy()
        except Exception:
            pass


def _mpl_placeholder(frame: tk.Widget) -> None:
    _clear_frame(frame)
    txt = (
        "未装 matplotlib, 图表不可用.\n"
        f"pip install matplotlib\n\n错误: {_MPL_ERR}"
    )
    tk.Label(frame, text=txt, justify="left", fg="#c00",
             font=("", 11)).pack(padx=20, pady=20, anchor="w")


def _plot_bar(frame: tk.Widget, title: str,
              data: dict[str, dict[str, float]],
              series: tuple[str, ...],
              series_label: tuple[str, ...]) -> None:
    if not _MPL_OK:
        _mpl_placeholder(frame)
        return
    _clear_frame(frame)
    fig = Figure(figsize=(9, 3.6), dpi=100)
    ax = fig.add_subplot(111)
    if not data:
        # 空数据: 保留 title, 只清掉刻度; 别用 axis('off'), 那样 title 也会被吃掉
        ax.text(0.5, 0.5, "暂无数据", ha="center", va="center",
                transform=ax.transAxes, fontsize=14, color="#888")
        ax.set_xticks([])
        ax.set_yticks([])
    else:
        keys = list(data.keys())
        n = len(series)
        import numpy as _np
        x = _np.arange(len(keys))
        width = 0.8 / max(1, n)
        for i, s in enumerate(series):
            ys = [data[k].get(s, 0) for k in keys]
            ax.bar(x + (i - (n - 1) / 2) * width, ys, width,
                   label=series_label[i])
        ax.set_xticks(x)
        ax.set_xticklabels(keys, rotation=15, ha="right")
        ax.legend(loc="best")
        ax.grid(axis="y", linestyle=":", alpha=0.5)
    ax.set_title(title)
    fig.tight_layout()
    canvas = FigureCanvasTkAgg(fig, master=frame)
    canvas.draw()
    canvas.get_tk_widget().pack(fill="both", expand=True)
    tb = NavigationToolbar2Tk(canvas, frame, pack_toolbar=False)
    tb.update()
    tb.pack(side="bottom", fill="x")


def _plot_line(frame: tk.Widget, title: str,
               x_labels: list[str],
               series: list[tuple[str, list[float]]]) -> None:
    if not _MPL_OK:
        _mpl_placeholder(frame)
        return
    _clear_frame(frame)
    fig = Figure(figsize=(9, 4.4), dpi=100)
    ax = fig.add_subplot(111)
    if not x_labels:
        ax.text(0.5, 0.5, "暂无数据", ha="center", va="center",
                transform=ax.transAxes, fontsize=14, color="#888")
        ax.set_xticks([])
        ax.set_yticks([])
    else:
        for name, ys in series:
            ax.plot(x_labels, ys, marker="o", label=name)
        ax.legend(loc="best")
        ax.grid(True, linestyle=":", alpha=0.5)
        ax.tick_params(axis="x", rotation=30)
    ax.set_title(title)
    fig.tight_layout()
    canvas = FigureCanvasTkAgg(fig, master=frame)
    canvas.draw()
    canvas.get_tk_widget().pack(fill="both", expand=True)
    tb = NavigationToolbar2Tk(canvas, frame, pack_toolbar=False)
    tb.update()
    tb.pack(side="bottom", fill="x")


# ------------------------------------------------ 入口

_ROW_FIELD_LABELS: dict[str, str] = {
    "id": "记录 ID",
    "ts": "时间",
    "host": "机器名",
    "task_id": "任务 ID",
    "version": "程序版本",
    "src_root": "视频源根 (抽帧)",
    "dst_root": "抽帧输出根",
    "video_path": "视频文件",
    "output_dir": "抽帧输出目录",
    "rel_path": "相对路径",
    "frames": "抽出帧数",
    "duration_sec": "视频时长(s)",
    "fps": "抽帧 fps",
    "quality": "JPEG 质量",
    "naming_style": "命名规则",
    "seq_digits": "序号位数",
    "elapsed_sec": "耗时(s)",
    "stage": "结果",
    "exit_code": "退出码",
    "msg": "备注",
    "source_root": "图片源根 (去重)",
    "dir_path": "去重目录",
    "total": "总数",
    "deleted": "删除数",
    "remain": "剩余数",
    "freed_bytes": "释放字节",
    "threshold": "相似阈值",
    "protect_count": "目标保护数",
    "scene_count": "场景保护数",
    "apply": "执行删除",
    "report_csv": "报告 CSV",
    "in_root": "分类输入根",
    "out_root": "分类输出根",
    "camera_dir": "camera 目录",
    "scanned": "扫描图片数",
    "copied_bucket": "复制到桶数",
    "bucket_json": "各桶分布 (JSON)",
    "errors": "出错数",
    "_db_file": "所在 db 文件",
}


def _open_row_detail_dialog(parent, title, row, task_run, task_type):
    """双击一行弹这个 modal 对话框.

    上半: 本行所有字段 (label + value 只读文本 + 复制按钮)
    下半: 关联 task_run 的配置快照 (JSON pretty), 若无 task_id 显示提示
    右下: 复制全部按钮
    """
    top = tk.Toplevel(parent)
    top.title(title)
    top.geometry("900x680")
    top.transient(parent)
    try:
        top.grab_set()
    except Exception:
        pass

    pw = ttk.PanedWindow(top, orient="vertical")
    pw.pack(fill="both", expand=True, padx=6, pady=6)

    # ---- 上: 明细字段 ----
    upper = ttk.LabelFrame(pw, text="记录字段")
    pw.add(upper, weight=3)

    canvas = tk.Canvas(upper, highlightthickness=0)
    sb = ttk.Scrollbar(upper, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=sb.set)
    canvas.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    inner = ttk.Frame(canvas)
    canvas_win = canvas.create_window((0, 0), window=inner, anchor="nw")

    def _on_inner_config(_e):
        canvas.configure(scrollregion=canvas.bbox("all"))
    inner.bind("<Configure>", _on_inner_config)

    def _on_canvas_config(e):
        canvas.itemconfigure(canvas_win, width=e.width)
    canvas.bind("<Configure>", _on_canvas_config)

    def _on_wheel(e):
        try:
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        except Exception:
            pass
    canvas.bind_all("<MouseWheel>", _on_wheel)
    top.protocol("WM_DELETE_WINDOW",
                 lambda: (canvas.unbind_all("<MouseWheel>"), top.destroy()))

    def _copy_to_clipboard(text):
        try:
            top.clipboard_clear()
            top.clipboard_append(text)
            top.update()
        except Exception:
            pass

    inner.columnconfigure(1, weight=1)
    r = 0
    for key, val in row.items():
        label = _ROW_FIELD_LABELS.get(key, key)
        display = "" if val is None else str(val)
        ttk.Label(inner, text=f"{label}:", anchor="e",
                  width=18).grid(row=r, column=0, sticky="ne",
                                 padx=(4, 6), pady=2)
        if len(display) > 120 or "\n" in display:
            txt = tk.Text(inner, height=min(6, display.count("\n") + 3),
                          wrap="word")
            txt.insert("1.0", display)
            txt.configure(state="disabled")
            txt.grid(row=r, column=1, sticky="ew", pady=2)
        else:
            var = tk.StringVar(value=display)
            ent = ttk.Entry(inner, textvariable=var, state="readonly")
            ent.grid(row=r, column=1, sticky="ew", pady=2)
        ttk.Button(inner, text="复制", width=6,
                   command=lambda v=display:
                   _copy_to_clipboard(v)).grid(
                       row=r, column=2, padx=(4, 4), pady=2)
        r += 1

    # ---- 下: task_runs 配置快照 ----
    lower = ttk.LabelFrame(pw, text="GUI 启动配置快照 (task_runs)")
    pw.add(lower, weight=2)

    if task_run is None:
        tk.Label(lower, fg="#888",
                 text=(
                     "未找到关联的 task_run 记录.\n"
                     "可能的原因:\n"
                     "  · 这条记录是 v0.4.53 之前的老数据, 那时 GUI 还没往\n"
                     "    task_runs 表写快照\n"
                     "  · 或者 task_id 为空 (直接跑 exe 没经过 GUI)"
                 ),
                 justify="left").pack(padx=10, pady=10, anchor="w")
    else:
        cfg_str = task_run.get("config_json") or "{}"
        try:
            cfg_pretty = json.dumps(
                json.loads(cfg_str), ensure_ascii=False, indent=2,
            )
        except Exception:
            cfg_pretty = cfg_str

        header_bar = ttk.Frame(lower)
        header_bar.pack(fill="x", padx=6, pady=(6, 0))
        ttk.Label(
            header_bar,
            text=(f"task_id={task_run.get('task_id')}  "
                  f"type={task_run.get('task_type')}  "
                  f"host={task_run.get('host')}  "
                  f"ts={task_run.get('ts')}  "
                  f"version={task_run.get('version') or '-'}"),
        ).pack(side="left")
        ttk.Button(
            header_bar, text="复制配置 JSON",
            command=lambda: _copy_to_clipboard(cfg_pretty),
        ).pack(side="right")

        txt_frame = ttk.Frame(lower)
        txt_frame.pack(fill="both", expand=True, padx=6, pady=6)
        text = tk.Text(txt_frame, wrap="none")
        sb_y = ttk.Scrollbar(txt_frame, orient="vertical",
                             command=text.yview)
        sb_x = ttk.Scrollbar(txt_frame, orient="horizontal",
                             command=text.xview)
        text.configure(yscrollcommand=sb_y.set, xscrollcommand=sb_x.set)
        text.insert("1.0", cfg_pretty)
        text.configure(state="disabled")
        text.grid(row=0, column=0, sticky="nsew")
        sb_y.grid(row=0, column=1, sticky="ns")
        sb_x.grid(row=1, column=0, sticky="ew")
        txt_frame.rowconfigure(0, weight=1)
        txt_frame.columnconfigure(0, weight=1)

    # ---- 底部按钮条 ----
    btm = ttk.Frame(top)
    btm.pack(side="bottom", fill="x", padx=8, pady=(0, 8))

    def _copy_all():
        lines = [f"[{title}]"]
        for k, v in row.items():
            lines.append(f"{_ROW_FIELD_LABELS.get(k, k)}: {v}")
        if task_run is not None:
            lines.append("")
            lines.append("[GUI 启动配置快照]")
            cfg_str = task_run.get("config_json") or "{}"
            try:
                cfg_pretty = json.dumps(
                    json.loads(cfg_str), ensure_ascii=False, indent=2,
                )
            except Exception:
                cfg_pretty = cfg_str
            lines.append(cfg_pretty)
        _copy_to_clipboard("\n".join(lines))

    ttk.Button(btm, text="复制全部",
               command=_copy_all).pack(side="right", padx=4)
    ttk.Button(btm, text="关闭",
               command=top.destroy).pack(side="right", padx=4)


def main() -> int:
    root = tk.Tk()
    app = StatsViewerApp(root)

    def _on_range_or_host_change(*_a):
        app.refresh_async()

    app.range_var.trace_add("write", _on_range_or_host_change)
    app.host_var.trace_add("write",
                           lambda *_: app._apply_host_filter_and_render())

    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
