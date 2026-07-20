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

from gui_log_util import GuiLogController  # noqa: E402
from tray_util import TrayController, TOOLTIP_STATS_VIEWER  # noqa: E402

HOTKEY_DEFAULT = "ctrl+alt+s"  # v0.4.88: stats 统计工具全局呼出主窗口


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
        # v0.4.94: 自定义时间段. range_var == "自定义…" 时启用.
        # 值格式 (start_str, end_str), YYYY-MM-DD HH:MM:SS 字符串, sqlite 直接比较.
        self._custom_time_range: tuple[str, str] | None = None
        # 只读 Entry 上显示的摘要, 方便用户看清当前生效的自定义区间
        self._custom_time_display_var = tk.StringVar(value="")
        self.host_var = tk.StringVar(value="全部")
        # v0.4.89: "最终 / 流水" 视图切换.
        #   最终 (默认) = 读 *_final 表, 一目标一行 UPSERT, 重复切帧不重复计数.
        #   流水       = 读 *_stats 老表, 每次任务一条, 用于审计/回放.
        self.view_mode_var = tk.StringVar(value="最终")

        # v0.4.87: 日志改公共模块 GuiLogController (落盘 + preload + queue).
        self._log_ctl = GuiLogController(app_name="stats_viewer_gui")
        self._auto_scroll_var = tk.BooleanVar(value=True)

        # v0.4.88: 托盘 + 全局快捷键 + 关闭最小化.
        # stats_viewer 之前没有配置持久化, 托盘偏好只在内存里 (下次启动重置默认).
        # 图标 fallback: 绿底 "ST".
        self._minimize_to_tray_var = tk.BooleanVar(value=True)
        self._hotkey_var = tk.StringVar(value=HOTKEY_DEFAULT)
        self._hide_close_hint_var = tk.BooleanVar(value=False)
        self._tray = TrayController(
            root=self.root,
            app_id="pic-clear-stats-viewer",
            tooltip=TOOLTIP_STATS_VIEWER,
            fallback_glyph=((39, 174, 96, 255), "ST"),
            hotkey_default=HOTKEY_DEFAULT,
            app_title="pic-clear 统计工具",
            ui_scale=float(getattr(root, "__ui_scale__", 1.0)),
        )
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_topbar()
        self._build_tabs()
        self._build_footer()

        # v0.4.87: 起 pump 循环, 把 controller queue 里的行灌进 Text.
        self.root.after(200, self._drain_log_queue)

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
        # v0.4.94: 预设扩容 + 自定义时间段.
        # - 最近 5/30 分钟 / 1/6 小时 更细粒度
        # - 今天 / 昨天 从 0 点起算 (跟 last N 分钟不同)
        # - 自定义… 弹对话框, 选完写回 _custom_time_range
        ttk.Combobox(
            bar, textvariable=self.range_var, width=12, state="readonly",
            values=(
                "最近 5 分钟", "最近 30 分钟",
                "最近 1 小时", "最近 6 小时",
                "今天", "昨天",
                "最近 1 天", "最近 7 天", "最近 30 天",
                "全部", "自定义…",
            ),
        ).pack(side="left")
        # 只读小 Entry 展示自定义区间摘要 (自定义模式下才有内容)
        ttk.Entry(
            bar, textvariable=self._custom_time_display_var,
            width=32, state="readonly",
        ).pack(side="left", padx=(4, 0))

        ttk.Label(bar, text="  机器:").pack(side="left")
        self._host_cb = ttk.Combobox(
            bar, textvariable=self.host_var, width=16, state="readonly",
            values=("全部",),
        )
        self._host_cb.pack(side="left")

        # v0.4.89: 视图切换. 最终=去重复计数 (推荐), 流水=每次任务一条.
        ttk.Label(bar, text="  视图:").pack(side="left")
        ttk.Combobox(
            bar, textvariable=self.view_mode_var, width=8, state="readonly",
            values=("最终", "流水"),
        ).pack(side="left")

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
        cols = ("ts", "host", "machine_fp", "local_ip",
                "video_path", "frames", "stage",
                "run_count", "version", "elapsed_sec", "fps")
        tv = _make_table(bot, cols, headers=(
            "时间", "机器", "指纹", "IP", "视频", "帧数", "结果",
            "重复次数", "版本", "耗时(s)", "fps",
        ), on_double_click=lambda row:
           self._show_row_detail("抽帧记录", row, "extract"))
        self._extract_tv = tv

        # v0.4.93: footer 显示当前视图下的汇总: 涉及视频数 / 帧数 / 抽帧总耗时 / 视频总时长.
        # 数字取决于视图 (最终 vs 流水), _render_extract 里刷新.
        self._extract_footer_var = tk.StringVar(value="")
        ttk.Label(bot, textvariable=self._extract_footer_var,
                  anchor="w", padding=(6, 4)).pack(side="bottom", fill="x")

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
        cols = ("ts", "host", "machine_fp", "local_ip",
                "dir_path", "total", "deleted", "remain",
                "freed_mb", "run_count", "version", "elapsed_sec")
        tv = _make_table(bot, cols, headers=(
            "时间", "机器", "指纹", "IP", "目录", "总数", "删除", "剩余",
            "释放(MB)", "重复次数", "版本", "耗时(s)",
        ), on_double_click=lambda row:
           self._show_row_detail("去重记录", row, "dedupe"))
        self._dedupe_tv = tv

        # v0.4.97: 去重 footer 汇总, 跟抽帧 footer 一致的位置和样式.
        self._dedupe_footer_var = tk.StringVar(value="")
        ttk.Label(bot, textvariable=self._dedupe_footer_var,
                  anchor="w", padding=(6, 4)).pack(side="bottom", fill="x")

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
        cols = ("ts", "host", "machine_fp", "local_ip",
                "camera_dir", "scanned", "copied_bucket",
                "buckets", "run_count", "version", "elapsed_sec")
        tv = _make_table(bot, cols, headers=(
            "时间", "机器", "指纹", "IP", "camera", "扫描", "复制到桶",
            "桶分布", "重复次数", "版本", "耗时(s)",
        ), on_double_click=lambda row:
           self._show_row_detail("分类记录", row, "classify"))
        self._classify_tv = tv

    # ---------- Tab: 每日趋势

    def _build_trend_tab(self) -> None:
        self._trend_chart_frame = ttk.Frame(self.tab_trend)
        self._trend_chart_frame.pack(fill="both", expand=True)

    # ---------- Tab: 日志

    def _build_log_tab(self) -> None:
        # v0.4.87: 工具条走 GuiLogController.build_toolbar
        # (清空日志(Tab) / 打开日志文件夹 / 当前日志文件名).
        def _extra(tb):
            ttk.Checkbutton(tb, text="自动滚到底",
                            variable=self._auto_scroll_var
                            ).pack(side="left", padx=4)
            # v0.4.88: 托盘 & 快捷键 (stats_viewer 没独立"关于" tab, 挤到日志工具条)
            ttk.Separator(tb, orient="vertical").pack(
                side="left", fill="y", padx=6)
            ttk.Checkbutton(tb, text="关闭最小化到托盘",
                            variable=self._minimize_to_tray_var
                            ).pack(side="left", padx=4)
            ttk.Label(tb, text="  快捷键：").pack(side="left")
            ttk.Entry(tb, textvariable=self._hotkey_var, width=14
                      ).pack(side="left")
            ttk.Button(tb, text="注册", command=self._register_hotkey
                       ).pack(side="left", padx=4)
        self._log_ctl.build_toolbar(self.tab_log, extra_toolbar=_extra)

        frame = ttk.Frame(self.tab_log)
        frame.pack(fill="both", expand=True)
        self._log_text = tk.Text(frame, wrap="none", font=("Consolas", 9))
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

        # v0.4.87: Text 绑给控制器 + preload 上次 tail 200 行
        self._log_ctl.attach_text(self._log_text)
        self._log_ctl.preload_prev_tail_to_text()

    # ---------- v0.4.88 托盘 / 关闭 -----------

    def _on_close(self) -> None:
        # 点 × 默认最小化到托盘, 取消勾选才真正退出.
        if self._minimize_to_tray_var.get():
            self._tray.hide_to_tray()
            self._tray.maybe_show_close_hint(
                cfg_get=lambda: bool(self._hide_close_hint_var.get()),
                # stats_viewer 没有配置持久化, cfg_set 只更新内存变量.
                # 下次启动"以后不再提示"会重置 (未来若加配置持久化再改).
                cfg_set=lambda v: self._hide_close_hint_var.set(bool(v)),
                hotkey_display=self._hotkey_var.get(),
            )
        else:
            self.quit_all()

    def hide_to_tray(self) -> None:
        self._tray.hide_to_tray()

    def show_main(self) -> None:
        self._tray.show_main()

    def quit_all(self) -> None:
        self._tray.quit_all()

    def _register_hotkey(self) -> None:
        hk = self._hotkey_var.get().strip() or HOTKEY_DEFAULT
        ok, msg = self._tray.register_hotkey(hk)
        if ok:
            from tkinter import messagebox
            messagebox.showinfo("已注册", msg)
        else:
            from tkinter import messagebox
            messagebox.showerror("注册失败", msg)

    # ---------- 公共

    def _log(self, msg: str) -> None:
        # v0.4.87: 转发到 GuiLogController (加时间戳 + 落盘 + 塞 controller queue).
        # 下一次 _drain_log_queue 里 pump() 就会把该行灌进 Text.
        self._log_ctl.log(msg)

    def _drain_log_queue(self) -> None:
        if self._log_ctl.pump() and self._auto_scroll_var.get():
            try:
                self._log_text.see("end")
            except Exception:
                pass
        self.root.after(200, self._drain_log_queue)

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
            mode = self.view_mode_var.get() or "最终"
            self._log(f"扫到 {len(self._db_files)} 个 db: {scan_dir} [视图={mode}]")

            if mode == "流水":
                # 老流水: *_stats 表, 时间字段就是 ts
                where, params = self._build_time_filter(ts_col="ts")
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
            else:
                # 最终: *_final 表, 字段名不同 (last_ts / last_version / last_stage ...)
                # 查询后统一映射成流水字段, 下游渲染代码不用感知差异.
                where, params = self._build_time_filter(ts_col="last_ts")
                self._extract_rows = _map_final_rows(
                    _query_final(self._db_files, "extract_final",
                                 where, params),
                    "extract",
                )
                self._dedupe_rows = _map_final_rows(
                    _query_final(self._db_files, "dedupe_final",
                                 where, params),
                    "dedupe",
                )
                self._classify_rows = _map_final_rows(
                    _query_final(self._db_files, "classify_final",
                                 where, params),
                    "classify",
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

    # ---------- v0.4.94: 时间过滤 (预设 + 自定义时间段)

    def _on_range_var_change(self, *_a) -> None:
        """range_var 改变时的分发. 拦截 "自定义…" 弹对话框.

        - 选 "自定义…": 弹层, 用户确定后写 _custom_time_range + refresh;
          取消则回退到上一次生效的 range (保存在 self._prev_range).
        - 其他预设: 清空 _custom_time_range 并 refresh.
        """
        r = self.range_var.get()
        if r == "自定义…":
            ok = self._open_custom_time_dialog()
            if not ok:
                # 用户取消, 回退. 用 after 避免 trace 递归 (set 会再触发一次 trace,
                # 但届时值等于 _prev_range, 不是"自定义…", 走 else 分支正常 refresh)
                prev = getattr(self, "_prev_range", None) or "最近 7 天"
                self.root.after(0, lambda: self.range_var.set(prev))
                return
            # 确定后 _custom_time_range 已就绪, 记录并刷新
            self._prev_range = r
            self.refresh_async()
        else:
            self._custom_time_range = None
            self._custom_time_display_var.set("")
            self._prev_range = r
            self.refresh_async()

    def _open_custom_time_dialog(self) -> bool:
        """弹自定义时间段对话框. 用户点"确定"返回 True, "取消" / 关闭返回 False.

        对话框里:
        - 起始 / 结束 各一个 Entry (YYYY-MM-DD HH:MM:SS)
        - 一排快捷: 此刻 / 今天0点 / 昨天0点 / 1小时前 / 1天前
        - 确定时校验格式 + 起 <= 止
        """
        top = tk.Toplevel(self.root)
        top.title("自定义时间段")
        top.transient(self.root)
        try:
            top.grab_set()
        except Exception:
            pass
        top.resizable(False, False)

        # 默认值: 起始 = 上次自定义 or 1 天前; 结束 = 此刻
        now = datetime.now()
        if self._custom_time_range is not None:
            s_default, e_default = self._custom_time_range
        else:
            s_default = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
            e_default = now.strftime("%Y-%m-%d %H:%M:%S")

        start_var = tk.StringVar(value=s_default)
        end_var = tk.StringVar(value=e_default)

        frm = ttk.Frame(top, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="起始:").grid(row=0, column=0, sticky="e",
                                          padx=(0, 6), pady=4)
        ttk.Entry(frm, textvariable=start_var, width=22).grid(
            row=0, column=1, sticky="w", pady=4)
        ttk.Label(frm, text="结束:").grid(row=1, column=0, sticky="e",
                                          padx=(0, 6), pady=4)
        ttk.Entry(frm, textvariable=end_var, width=22).grid(
            row=1, column=1, sticky="w", pady=4)

        ttk.Label(frm, text="格式: YYYY-MM-DD HH:MM:SS, 例 2026-07-20 14:30:00",
                  foreground="#888").grid(
                      row=2, column=0, columnspan=2, sticky="w",
                      padx=(0, 0), pady=(0, 8))

        # 快捷按钮
        quick_frm = ttk.LabelFrame(frm, text="快捷填入")
        quick_frm.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        def _fmt(dt: datetime) -> str:
            return dt.strftime("%Y-%m-%d %H:%M:%S")

        def _set_range(s: datetime, e: datetime) -> None:
            start_var.set(_fmt(s))
            end_var.set(_fmt(e))

        today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yest0 = today0 - timedelta(days=1)

        quick_specs = [
            ("最近 1 小时", now - timedelta(hours=1), now),
            ("最近 6 小时", now - timedelta(hours=6), now),
            ("今天",       today0, now),
            ("昨天",       yest0, today0),
            ("最近 1 天",  now - timedelta(days=1), now),
            ("最近 7 天",  now - timedelta(days=7), now),
        ]
        for i, (label, s, e) in enumerate(quick_specs):
            ttk.Button(
                quick_frm, text=label, width=10,
                command=lambda s=s, e=e: _set_range(s, e),
            ).grid(row=i // 3, column=i % 3, padx=4, pady=3)

        # 确定 / 取消
        result = {"ok": False}
        err_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=err_var, foreground="#c0392b").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(0, 4))

        def _try_parse(s: str) -> datetime | None:
            s = (s or "").strip()
            # 允许省略秒 / 时分秒, 分别兜底
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    return datetime.strptime(s, fmt)
                except ValueError:
                    continue
            return None

        def _on_ok() -> None:
            s_dt = _try_parse(start_var.get())
            e_dt = _try_parse(end_var.get())
            if s_dt is None:
                err_var.set("起始时间格式非法")
                return
            if e_dt is None:
                err_var.set("结束时间格式非法")
                return
            if s_dt > e_dt:
                err_var.set("起始时间必须 <= 结束时间")
                return
            self._custom_time_range = (_fmt(s_dt), _fmt(e_dt))
            self._custom_time_display_var.set(
                f"{_fmt(s_dt)}  →  {_fmt(e_dt)}"
            )
            result["ok"] = True
            top.destroy()

        def _on_cancel() -> None:
            top.destroy()

        btn_frm = ttk.Frame(frm)
        btn_frm.grid(row=5, column=0, columnspan=2, sticky="e", pady=(4, 0))
        ttk.Button(btn_frm, text="取消", width=8,
                   command=_on_cancel).pack(side="right", padx=(6, 0))
        ttk.Button(btn_frm, text="确定", width=8,
                   command=_on_ok).pack(side="right")

        top.protocol("WM_DELETE_WINDOW", _on_cancel)
        # 阻塞等对话框关闭
        self.root.wait_window(top)
        return result["ok"]

    def _build_time_filter(self, ts_col: str = "ts") -> tuple[str, tuple]:
        """根据 range_var / _custom_time_range 生成 WHERE 片段 + 参数.

        v0.4.94: 支持更多预设 + 自定义 BETWEEN.
        - 全部: 空条件
        - 自定义…: BETWEEN start AND end (含首尾秒)
        - 分钟/小时/天: ts >= now - delta
        - 今天 / 昨天: 按 0 点分界
        """
        r = self.range_var.get()
        if r == "全部":
            return "", ()
        if r == "自定义…":
            if self._custom_time_range is None:
                # 用户点了"自定义…"但还没确认, 兜底不加条件
                return "", ()
            s, e = self._custom_time_range
            return f"{ts_col} BETWEEN ? AND ?", (s, e)

        now = datetime.now()
        # 分钟级
        minutes = {
            "最近 5 分钟": 5, "最近 30 分钟": 30,
        }.get(r)
        if minutes is not None:
            since = (now - timedelta(minutes=minutes)
                     ).strftime("%Y-%m-%d %H:%M:%S")
            return f"{ts_col} >= ?", (since,)
        # 小时级
        hours = {
            "最近 1 小时": 1, "最近 6 小时": 6,
        }.get(r)
        if hours is not None:
            since = (now - timedelta(hours=hours)
                     ).strftime("%Y-%m-%d %H:%M:%S")
            return f"{ts_col} >= ?", (since,)
        # 今天 / 昨天 (按自然日切)
        if r == "今天":
            since = now.replace(hour=0, minute=0, second=0, microsecond=0)
            return f"{ts_col} >= ?", (since.strftime("%Y-%m-%d %H:%M:%S"),)
        if r == "昨天":
            today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
            yest0 = today0 - timedelta(days=1)
            return (
                f"{ts_col} >= ? AND {ts_col} < ?",
                (yest0.strftime("%Y-%m-%d %H:%M:%S"),
                 today0.strftime("%Y-%m-%d %H:%M:%S")),
            )
        # 天级 (兼容老选项)
        days = {"最近 1 天": 1, "最近 7 天": 7, "最近 30 天": 30}.get(r, 7)
        since = (now - timedelta(days=days)
                 ).strftime("%Y-%m-%d %H:%M:%S")
        return f"{ts_col} >= ?", (since,)

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
            ("machine_fp", lambda r: r.get("machine_fp") or ""),
            ("local_ip",   lambda r: r.get("local_ip") or ""),
            ("video_path", "video_path"),
            ("frames", "frames"),
            ("stage", "stage"),
            ("run_count", lambda r: r.get("run_count") or ""),
            ("version", lambda r: r.get("version") or ""),
            ("elapsed_sec", lambda r: f"{(r.get('elapsed_sec') or 0):.1f}"),
            ("fps", "fps"),
        ])
        # v0.4.93: footer 汇总. '涉及视频数' 按 video_md5 去重, 无 md5
        # 兜底用 video_path (老数据/长路径 fingerprint 失败等场景).
        # 最终视图: extract_final 本身按 video_md5 去重, len(rows) 就是视频数.
        # 流水视图: 一个视频可能有多条记录, 需要去重.
        _mode = self.view_mode_var.get() or "最终"
        _seen: set = set()
        _videos = 0
        _frames = 0
        _elapsed = 0.0
        _duration = 0.0
        for r in rows:
            _k = r.get("video_md5") or r.get("video_path") or ""
            if _k and _k not in _seen:
                _seen.add(_k)
                _videos += 1
                # 视频总时长只按'每个视频计一次'算, 别把重复抽的算多次
                _duration += float(r.get("duration_sec") or 0)
            _frames += int(r.get("frames") or 0)
            _elapsed += float(r.get("elapsed_sec") or 0)
        self._extract_footer_var.set(
            f"[{_mode}] 统计数据: 一共涉及 {_videos} 个视频, "
            f"切出帧数 {_frames} 张, "
            f"总耗时: {_fmt_duration_human(_elapsed)}, "
            f"视频总时长: {_fmt_duration_human(_duration)}"
        )
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
            ("machine_fp", lambda r: r.get("machine_fp") or ""),
            ("local_ip",   lambda r: r.get("local_ip") or ""),
            ("dir_path", "dir_path"),
            ("total", "total"),
            ("deleted", "deleted"),
            ("remain", "remain"),
            ("freed_mb",
             lambda r: f"{(r.get('freed_bytes') or 0) / 1024 / 1024:.1f}"),
            ("run_count", lambda r: r.get("run_count") or ""),
            ("version", lambda r: r.get("version") or ""),
            ("elapsed_sec",
             lambda r: f"{(r.get('elapsed_sec') or 0):.1f}"),
        ])
        # v0.4.97: footer 汇总. dedupe_final PK=(dir_path, host), 天然按目录去重,
        # len(rows) 就是涉及目录数. 流水视图一个目录可能多条, 但 total/deleted/remain
        # 累加口径跟抽帧一致 (rows 直接累加, 用户看视图切换的实际数字).
        _mode = self.view_mode_var.get() or "最终"
        _dirs = len(rows)
        _total = 0
        _deleted = 0
        _remain = 0
        _freed = 0
        _elapsed = 0.0
        for r in rows:
            _total += int(r.get("total") or 0)
            _deleted += int(r.get("deleted") or 0)
            _remain += int(r.get("remain") or 0)
            _freed += int(r.get("freed_bytes") or 0)
            _elapsed += float(r.get("elapsed_sec") or 0)
        _freed_mb = _freed / 1024 / 1024
        self._dedupe_footer_var.set(
            f"[{_mode}] 统计数据: 涉及 {_dirs} 个目录, "
            f"总张数: {_total} 张, "
            f"总删除: {_deleted} 张, "
            f"总剩余: {_remain} 张, "
            f"总释放: {_freed_mb:.1f}MB, "
            f"总耗时: {_fmt_duration_human(_elapsed)}"
        )
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
            ("machine_fp", lambda r: r.get("machine_fp") or ""),
            ("local_ip",   lambda r: r.get("local_ip") or ""),
            ("camera_dir", "camera_dir"),
            ("scanned", "scanned"),
            ("copied_bucket", "copied_bucket"),
            ("buckets", _fmt_buckets),
            ("run_count", lambda r: r.get("run_count") or ""),
            ("version", lambda r: r.get("version") or ""),
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
        elif c in ("version",):
            w = 80
        elif c in ("run_count",):
            w = 60
        elif c in ("machine_fp",):
            w = 150
        elif c in ("local_ip",):
            w = 110
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

def _fmt_duration_human(sec: float | None) -> str:
    """秒 -> 'X小时Y分钟Z秒' 人类可读. v0.4.93 footer 用."""
    if sec is None or sec <= 0:
        return "0秒"
    total = int(sec)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    parts: list[str] = []
    if h:
        parts.append(f"{h}小时")
    if m:
        parts.append(f"{m}分钟")
    parts.append(f"{s}秒")
    return "".join(parts)


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
    # v0.4.91: 必须持有 StringVar 引用, 否则 for 循环退出后 var 被 GC,
    # tkinter 侧 tk variable 被 unset, readonly Entry 就显示成空.
    # (video_path/output_dir/rel_path 走 tk.Text 分支不受影响, 所以之前
    #  看起来"只有 3 个长字段能显示, 其他全空", 就是这个坑.)
    top._detail_stringvars = []  # type: ignore[attr-defined]
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
            top._detail_stringvars.append(var)  # type: ignore[attr-defined]
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




# ---------------------------------------- final 表查询与字段映射 (v0.4.89)

def _query_final(db_files: list, table: str,
                 where_sql: str, params: tuple) -> list[dict]:
    """查 *_final 表. 不能直接复用 stats_db.query_all 因为它硬编码了 3 张流水表."""
    import sqlite3
    allowed = {"extract_final", "dedupe_final", "classify_final"}
    if table not in allowed:
        raise ValueError(f"invalid final table: {table}")
    rows: list[dict] = []
    sql = f"SELECT * FROM {table}"
    if where_sql:
        sql += " WHERE " + where_sql
    sql += " ORDER BY last_ts DESC LIMIT 5000"
    for f in db_files:
        try:
            conn = sqlite3.connect(str(f), timeout=5.0)
            try:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(sql, params)
                for r in cur.fetchall():
                    d = dict(r)
                    d["_db_file"] = str(f)
                    rows.append(d)
            finally:
                conn.close()
        except sqlite3.OperationalError as e:
            # final 表在老 db 上不存在, 首次升级时会命中, 静默跳过
            if "no such table" not in str(e).lower():
                print(f"[stats_viewer] WARN query final {f}: {e}",
                      file=__import__("sys").stderr)
        except Exception as e:
            print(f"[stats_viewer] WARN query final {f}: {e}",
                  file=__import__("sys").stderr)
    # 多机 db 聚合: 抽帧按 video_md5 合并 (取 last_ts 最新的那条),
    # 去重/分类的 PK 已经含 host, 无需二次合并.
    if table == "extract_final":
        merged: dict[str, dict] = {}
        for r in rows:
            k = r.get("video_md5") or ""
            if not k:
                # 极少情况: 老库或写库时缺 md5, 用 id + db_file 兜底成唯一
                k = f"__no_md5__:{r.get('_db_file')}:{r.get('rowid') or id(r)}"
            prev = merged.get(k)
            if prev is None or (r.get("last_ts") or "") > (
                    prev.get("last_ts") or ""):
                merged[k] = r
        rows = list(merged.values())
        rows.sort(key=lambda r: r.get("last_ts") or "", reverse=True)
    return rows


def _map_final_rows(rows: list[dict], kind: str) -> list[dict]:
    """把 *_final 表的 last_* 字段映射成流水字段名, 下游渲染代码零改动.

    映射规则 (三张 final 表共用):
      last_ts       -> ts
      last_host     -> host (只 extract_final 有; 其他表 host 本来就有)
      last_task_id  -> task_id
      last_version  -> version
      last_elapsed_sec -> elapsed_sec
      last_stage    -> stage      (extract 专属)
      last_exit_code-> exit_code  (dedupe/classify 专属)
      last_msg      -> msg
      run_count / first_ts 保留原名 (新字段).
    """
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        # 通用
        if "last_ts" in d:      d.setdefault("ts", d.get("last_ts"))
        if "last_host" in d:    d.setdefault("host", d.get("last_host"))
        if "last_task_id" in d: d.setdefault("task_id", d.get("last_task_id"))
        if "last_version" in d: d.setdefault("version", d.get("last_version"))
        if "last_machine_fp" in d:
            d.setdefault("machine_fp", d.get("last_machine_fp"))
        if "last_local_ip" in d:
            d.setdefault("local_ip", d.get("last_local_ip"))
        if "last_elapsed_sec" in d:
            d.setdefault("elapsed_sec", d.get("last_elapsed_sec"))
        if "last_msg" in d:     d.setdefault("msg", d.get("last_msg"))
        # extract 专属
        if kind == "extract" and "last_stage" in d:
            d.setdefault("stage", d.get("last_stage"))
        # dedupe / classify 专属
        if "last_exit_code" in d:
            d.setdefault("exit_code", d.get("last_exit_code"))
        out.append(d)
    return out


def main() -> int:
    root = tk.Tk()
    app = StatsViewerApp(root)

    # v0.4.94: range 变更走 App 方法 (要拦截 "自定义…" 弹对话框)
    app.range_var.trace_add("write", app._on_range_var_change)
    app.host_var.trace_add("write",
                           lambda *_: app._apply_host_filter_and_render())
    # v0.4.89: 切换 "最终/流水" 视图直接触发重新查表
    app.view_mode_var.trace_add("write", lambda *_: app.refresh_async())

    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
