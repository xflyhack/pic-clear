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
        ))
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
        ))
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
        ))
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
                headers: tuple[str, ...]) -> ttk.Treeview:
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
    return tv


def _fill_table(tv: ttk.Treeview, rows: list[dict],
                col_map: list[tuple[str, object]]) -> None:
    for iid in tv.get_children():
        tv.delete(iid)
    for r in rows:
        vals = []
        for _, key in col_map:
            if callable(key):
                v = key(r)
            else:
                v = r.get(key, "")
            vals.append("" if v is None else v)
        tv.insert("", "end", values=vals)


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
        ax.text(0.5, 0.5, "暂无数据", ha="center", va="center",
                transform=ax.transAxes, fontsize=14, color="#888")
        ax.axis("off")
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
        ax.axis("off")
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
