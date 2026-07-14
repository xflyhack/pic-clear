"""summary_stats_gui.py

pic-clear 统计汇总 GUI（tkinter）

功能:
  - 选磁盘 / 目录树钻取（任何节点右键"就用这个当统计根"）
  - 汇总 machine_id_*.csv：涉及文件夹、原图总数、删除数、剩余、按机器分
  - 导出汇总 CSV
  - 授权检查（复用 licensing.py）

用法:
  python summary_stats_gui.py                    # 本地开发
  summary_stats_gui.exe                          # 打包后
  summary_stats_gui.exe --fingerprint            # 只打印指纹（脚本调用）
"""
from __future__ import annotations

import csv
import os
import string
import sys
import time
from collections import defaultdict
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


# ============================================================
# 授权
# ============================================================

def _check_license_or_die() -> None:
    """跟 pipeline._check_license_or_die 一模一样的接口."""
    if os.environ.get("PIPELINE_SKIP_LICENSE") == "1":
        return
    try:
        from licensing import get_fingerprint, verify_license
    except ImportError as e:
        print(f"[FATAL] 无法加载 licensing 模块: {e}", file=sys.stderr)
        sys.exit(2)

    env_lic = os.environ.get("PIPELINE_LICENSE") or os.environ.get("DEDUPE_LICENSE")
    if env_lic:
        license_path = Path(env_lic).expanduser().resolve()
    elif getattr(sys, "frozen", False):
        license_path = Path(sys.executable).resolve().parent / "license.lic"
    else:
        license_path = Path.cwd() / "license.lic"

    ok, msg = verify_license(license_path)
    if ok:
        print(f"[授权] {msg}", flush=True)
        return

    fp = get_fingerprint()
    print("=" * 60)
    print("[授权] summary_stats_gui 未获得有效授权，无法运行。")
    print(f"[授权] 原因: {msg}")
    print(f"[授权] license 期望位置: {license_path}")
    print()
    print(f"[授权] 本机指纹: {fp}")
    print("[授权] 请把这行指纹发给作者，获取 license.lic 后放到 exe 同目录。")
    print("=" * 60)
    sys.exit(3)


# ============================================================
# 统计核心逻辑
# ============================================================

# CSV 字段（来自 append_stats.bat）：
#   folder_name,total,deleted,remain,abs_path,timestamp

def find_all_csv(root: Path) -> list[Path]:
    """递归找 machine_id_*.csv."""
    if not root.exists():
        return []
    try:
        return sorted(root.rglob("machine_id_*.csv"))
    except OSError:
        return []


def _to_int(x: str) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def aggregate(
    csv_files: list[Path],
    path_mode: str = "all",
    path_filter: str = "",
) -> dict:
    """核心汇总。

    Returns:
      {
        "csv_count": int,
        "row_count": int,
        "folder_count": int,
        "total_original": int,
        "total_deleted": int,
        "total_remain": int,
        "delete_ratio": float,
        "by_machine": { machine: {folders, original, deleted, remain} },
        "detail": [ {folder_name, abs_path, original, deleted, remain}, ... ]
      }
    """
    # 收集所有行：(row, machine, csv_path)
    all_rows: list[tuple[dict, str]] = []
    for csv_path in csv_files:
        machine = csv_path.stem.replace("machine_id_", "", 1)
        try:
            with open(csv_path, "r", encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    all_rows.append((row, machine))
        except OSError:
            continue

    # 路径过滤
    def _pass(row: dict) -> bool:
        ap = (row.get("abs_path") or "").rstrip("\\/")
        if path_mode == "exact":
            return ap == path_filter.rstrip("\\/")
        if path_mode == "prefix":
            base = path_filter.rstrip("\\/")
            return ap == base or ap.startswith(base + "\\") or ap.startswith(base + "/")
        return True

    filtered = [(r, m) for (r, m) in all_rows if _pass(r)]

    # 按 abs_path 分组
    groups: dict[str, list[tuple[dict, str]]] = defaultdict(list)
    for r, m in filtered:
        groups[(r.get("abs_path") or "").rstrip("\\/")].append((r, m))

    folder_count = len(groups)
    total_original = 0
    total_deleted = 0
    total_remain = 0

    by_machine: dict[str, dict[str, int]] = defaultdict(
        lambda: {"folders": 0, "original": 0, "deleted": 0, "remain": 0}
    )

    detail: list[dict] = []

    for abs_path, items in groups.items():
        # 按时间戳升序
        items_sorted = sorted(items, key=lambda pair: pair[0].get("timestamp") or "")
        first = items_sorted[0][0]
        last = items_sorted[-1][0]
        first_total = _to_int(first.get("total", "0"))
        last_remain = _to_int(last.get("remain", "0"))
        sum_deleted = sum(_to_int(r.get("deleted", "0")) for r, _ in items)

        total_original += first_total
        total_deleted += sum_deleted
        total_remain += last_remain

        machine = items_sorted[-1][1]
        m_stats = by_machine[machine]
        m_stats["folders"] += 1
        m_stats["original"] += first_total
        m_stats["deleted"] += sum_deleted
        m_stats["remain"] += last_remain

        detail.append({
            "folder_name": last.get("folder_name") or "",
            "abs_path": abs_path,
            "original": first_total,
            "deleted": sum_deleted,
            "remain": last_remain,
        })

    delete_ratio = (total_deleted / total_original * 100.0) if total_original else 0.0

    return {
        "csv_count": len(csv_files),
        "row_count": len(filtered),
        "folder_count": folder_count,
        "total_original": total_original,
        "total_deleted": total_deleted,
        "total_remain": total_remain,
        "delete_ratio": delete_ratio,
        "by_machine": dict(by_machine),
        "detail": sorted(detail, key=lambda d: d["abs_path"]),
    }


def export_csv(result: dict, stats_root: Path, out_path: Path) -> None:
    """把汇总结果导出到 CSV。UTF-8 with BOM，Excel 打开中文不乱码。"""
    with open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["section", "key", "value"])

        writer.writerow(["汇总", "统计根目录",     str(stats_root)])
        writer.writerow(["汇总", "涉及文件夹",     result["folder_count"]])
        writer.writerow(["汇总", "原始图片总数",   result["total_original"]])
        writer.writerow(["汇总", "累计删除数",     result["total_deleted"]])
        writer.writerow(["汇总", "当前剩余",       result["total_remain"]])
        writer.writerow(["汇总", "删除比例%",      f"{result['delete_ratio']:.2f}"])

        for machine, s in sorted(result["by_machine"].items()):
            writer.writerow([f"按机器_{machine}", "文件夹数", s["folders"]])
            writer.writerow([f"按机器_{machine}", "原图",     s["original"]])
            writer.writerow([f"按机器_{machine}", "删除",     s["deleted"]])
            writer.writerow([f"按机器_{machine}", "剩余",     s["remain"]])

        for d in result["detail"]:
            writer.writerow([
                "明细",
                f"{d['folder_name']} | {d['abs_path']}",
                f"原图={d['original']} 删除={d['deleted']} 剩余={d['remain']}",
            ])


# ============================================================
# GUI
# ============================================================

def _list_drives() -> list[str]:
    """列出所有存在的盘符（Windows）；非 Windows 返回 ['/']."""
    if os.name != "nt":
        return ["/"]
    drives = []
    for letter in string.ascii_uppercase:
        p = f"{letter}:\\"
        if os.path.exists(p):
            drives.append(p)
    return drives


class SummaryStatsGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("pic-clear 统计汇总")
        try:
            root.geometry("1080x720")
        except Exception:
            pass

        # 状态
        self.stats_root: Path | None = None
        self.last_result: dict | None = None

        self._build_top()
        self._build_tree()
        self._build_result()

    # ---------- 顶部：磁盘 + 统计根 + 粒度 ----------
    def _build_top(self) -> None:
        top = ttk.Frame(self.root, padding=8)
        top.pack(side="top", fill="x")

        ttk.Label(top, text="磁盘：").pack(side="left")
        self.drive_var = tk.StringVar()
        self.drive_combo = ttk.Combobox(
            top, textvariable=self.drive_var, values=_list_drives(),
            width=8, state="readonly",
        )
        self.drive_combo.pack(side="left")
        self.drive_combo.bind("<<ComboboxSelected>>", self._on_drive_change)
        drives = _list_drives()
        if drives:
            self.drive_var.set(drives[0])

        ttk.Button(top, text="刷新磁盘", command=self._refresh_drives).pack(side="left", padx=(4, 12))

        ttk.Label(top, text="统计根：").pack(side="left")
        self.root_var = tk.StringVar(value="[请在下方目录树选一个]")
        ttk.Entry(top, textvariable=self.root_var, width=60, state="readonly").pack(side="left", padx=4)

        ttk.Button(top, text="选当前节点为统计根", command=self._set_current_as_root).pack(side="left", padx=4)

        # 粒度
        gran = ttk.Frame(self.root, padding=(8, 0))
        gran.pack(side="top", fill="x")
        ttk.Label(gran, text="粒度：").pack(side="left")
        self.gran_var = tk.StringVar(value="all")
        for label, val in [("全部（递归所有 csv）", "all"),
                           ("精确（匹配 abs_path）", "exact"),
                           ("前缀（含子孙）", "prefix")]:
            ttk.Radiobutton(gran, text=label, variable=self.gran_var, value=val).pack(side="left")

        ttk.Label(gran, text="过滤路径：").pack(side="left", padx=(12, 2))
        self.filter_var = tk.StringVar()
        ttk.Entry(gran, textvariable=self.filter_var, width=50).pack(side="left")

        # 汇总按钮
        btns = ttk.Frame(self.root, padding=8)
        btns.pack(side="top", fill="x")
        self.run_btn = ttk.Button(btns, text="开始汇总", command=self._run_aggregate)
        self.run_btn.pack(side="left")
        ttk.Button(btns, text="导出汇总 CSV", command=self._export).pack(side="left", padx=8)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(btns, textvariable=self.status_var, foreground="#666").pack(side="left", padx=12)

    # ---------- 中间：目录树 ----------
    def _build_tree(self) -> None:
        wrap = ttk.LabelFrame(self.root, text="目录树（双击展开子目录；右键或按钮把当前选中项设为统计根）", padding=6)
        wrap.pack(side="top", fill="both", expand=False, padx=8, pady=4)

        self.tree = ttk.Treeview(wrap, columns=("path",), displaycolumns=(), height=12)
        self.tree.heading("#0", text="目录", anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)

        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)

        self.tree.bind("<<TreeviewOpen>>", self._on_tree_open)
        self.tree.bind("<Double-1>", self._on_tree_dblclick)
        self.tree.bind("<Button-3>", self._on_tree_right_click)  # 右键菜单

        self._populate_root()

    # ---------- 底部：结果 ----------
    def _build_result(self) -> None:
        wrap = ttk.LabelFrame(self.root, text="汇总结果", padding=6)
        wrap.pack(side="top", fill="both", expand=True, padx=8, pady=4)

        # 上：核心指标
        self.summary_text = tk.Text(wrap, height=8, wrap="none")
        self.summary_text.pack(side="top", fill="x")
        self.summary_text.configure(state="disabled")

        # 下：明细表
        cols = ("folder_name", "original", "deleted", "remain", "abs_path")
        self.detail = ttk.Treeview(wrap, columns=cols, show="headings", height=12)
        for c, w, txt in [
            ("folder_name", 180, "文件夹"),
            ("original",     90, "原图"),
            ("deleted",      90, "删除"),
            ("remain",       90, "剩余"),
            ("abs_path",    500, "绝对路径"),
        ]:
            self.detail.heading(c, text=txt)
            self.detail.column(c, width=w, anchor="w")
        self.detail.pack(side="top", fill="both", expand=True, pady=(6, 0))

    # ================================================================
    # 目录树
    # ================================================================

    def _populate_root(self) -> None:
        for iid in self.tree.get_children(""):
            self.tree.delete(iid)
        drv = self.drive_var.get() or (_list_drives()[0] if _list_drives() else "")
        if not drv:
            return
        # 根节点用盘符
        root_iid = self.tree.insert("", "end", text=drv, values=(drv,), open=False)
        # 占位子节点：这样才能显示可展开箭头
        self.tree.insert(root_iid, "end", text="...")

    def _iid_path(self, iid: str) -> Path:
        vals = self.tree.item(iid, "values")
        return Path(vals[0]) if vals else Path("")

    def _on_tree_open(self, _evt=None) -> None:
        iid = self.tree.focus()
        if not iid:
            return
        self._expand_node(iid)

    def _expand_node(self, iid: str) -> None:
        # 已展开过就不重复
        children = self.tree.get_children(iid)
        if children:
            # 若只有 "..." 占位则清空重列
            first = self.tree.item(children[0], "text")
            if first != "..." and len(children) > 0:
                return
            for c in children:
                self.tree.delete(c)

        path = self._iid_path(iid)
        try:
            subs = sorted([p for p in path.iterdir() if p.is_dir()], key=lambda p: p.name.lower())
        except (OSError, PermissionError):
            subs = []

        for p in subs:
            sub_iid = self.tree.insert(iid, "end", text=p.name, values=(str(p),))
            # 加占位子节点保持展开箭头（若该目录也有子目录）
            try:
                has_sub = any(x.is_dir() for x in p.iterdir())
            except (OSError, PermissionError):
                has_sub = False
            if has_sub:
                self.tree.insert(sub_iid, "end", text="...")

    def _on_tree_dblclick(self, _evt=None) -> None:
        iid = self.tree.focus()
        if not iid:
            return
        # 双击也当"设为统计根"
        self._set_current_as_root()

    def _on_tree_right_click(self, evt) -> None:
        iid = self.tree.identify_row(evt.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="就用这里作为统计根", command=self._set_current_as_root)
        menu.add_command(label="展开这里", command=lambda: self._expand_node(iid))
        try:
            menu.tk_popup(evt.x_root, evt.y_root)
        finally:
            menu.grab_release()

    def _set_current_as_root(self) -> None:
        iid = self.tree.focus()
        if not iid:
            messagebox.showwarning("提示", "请先在目录树里选中一个节点")
            return
        p = self._iid_path(iid)
        if not p.exists():
            messagebox.showerror("路径无效", f"{p} 不存在")
            return
        self.stats_root = p
        self.root_var.set(str(p))
        self.status_var.set(f"统计根已设为：{p}")

    # ================================================================
    # 磁盘
    # ================================================================

    def _refresh_drives(self) -> None:
        vals = _list_drives()
        self.drive_combo["values"] = vals
        if vals and self.drive_var.get() not in vals:
            self.drive_var.set(vals[0])
        self._populate_root()

    def _on_drive_change(self, _evt=None) -> None:
        self._populate_root()

    # ================================================================
    # 汇总
    # ================================================================

    def _run_aggregate(self) -> None:
        if not self.stats_root:
            messagebox.showwarning("提示", "请先选中一个目录并点『选当前节点为统计根』（或双击/右键）")
            return
        self.status_var.set("扫描 csv 中……")
        self.root.update_idletasks()

        t0 = time.time()
        csv_files = find_all_csv(self.stats_root)
        if not csv_files:
            self.status_var.set(f"未找到 csv：{self.stats_root}")
            self._show_summary(
                f"[结果] {self.stats_root} 下没找到 machine_id_*.csv\n"
                f"       确认 append_stats.bat 是否往这里写过数据。"
            )
            self._clear_detail()
            self.last_result = None
            return

        self.status_var.set(f"汇总中……找到 {len(csv_files)} 个 csv")
        self.root.update_idletasks()

        result = aggregate(
            csv_files,
            path_mode=self.gran_var.get(),
            path_filter=self.filter_var.get(),
        )
        self.last_result = result

        elapsed = time.time() - t0
        self._show_summary(self._format_summary(result))
        self._fill_detail(result["detail"])
        self.status_var.set(f"完成，用时 {elapsed:.2f}s")

    def _format_summary(self, r: dict) -> str:
        lines = [
            f"  统计根目录   : {self.stats_root}",
            f"  扫到 csv     : {r['csv_count']} 个",
            f"  数据条目     : {r['row_count']} 行",
            f"  涉及文件夹   : {r['folder_count']} 个",
            f"  参与的机器   : {len(r['by_machine'])} 台",
            "",
            "  [核心指标]",
            f"  原始图片总数 : {r['total_original']:,} 张   (每目录首次 total 求和)",
            f"  累计删除数   : {r['total_deleted']:,} 张   (所有 deleted 求和)",
            f"  当前剩余     : {r['total_remain']:,} 张   (每目录最后一次 remain 求和)",
            f"  删除比例     : {r['delete_ratio']:.2f}%",
        ]
        if r["by_machine"]:
            lines.append("")
            lines.append("  [按机器分]")
            for m in sorted(r["by_machine"].keys()):
                s = r["by_machine"][m]
                lines.append(
                    f"    {m:<24}  文件夹 {s['folders']:>4}"
                    f"  原图 {s['original']:>10,}"
                    f"  删除 {s['deleted']:>10,}"
                    f"  剩余 {s['remain']:>10,}"
                )
        return "\n".join(lines)

    def _show_summary(self, text: str) -> None:
        self.summary_text.configure(state="normal")
        self.summary_text.delete("1.0", "end")
        self.summary_text.insert("1.0", text)
        self.summary_text.configure(state="disabled")

    def _clear_detail(self) -> None:
        for iid in self.detail.get_children(""):
            self.detail.delete(iid)

    def _fill_detail(self, detail: list[dict]) -> None:
        self._clear_detail()
        for d in detail:
            self.detail.insert("", "end", values=(
                d["folder_name"],
                f"{d['original']:,}",
                f"{d['deleted']:,}",
                f"{d['remain']:,}",
                d["abs_path"],
            ))

    # ================================================================
    # 导出
    # ================================================================

    def _export(self) -> None:
        if not self.last_result or not self.stats_root:
            messagebox.showwarning("提示", "还没有汇总结果，先点『开始汇总』")
            return
        ts = time.strftime("%Y%m%d_%H%M%S")
        default_name = f"summary_{ts}.csv"
        out = filedialog.asksaveasfilename(
            title="导出汇总 CSV",
            defaultextension=".csv",
            initialfile=default_name,
            filetypes=[("CSV files", "*.csv"), ("All", "*.*")],
        )
        if not out:
            return
        try:
            export_csv(self.last_result, self.stats_root, Path(out))
            messagebox.showinfo("导出成功", f"已导出：\n{out}")
            self.status_var.set(f"已导出：{out}")
        except Exception as e:
            messagebox.showerror("导出失败", f"{type(e).__name__}: {e}")


# ============================================================
# 入口
# ============================================================

def main() -> int:
    # 仅打印指纹（供签发脚本调用）
    if "--fingerprint" in sys.argv[1:]:
        try:
            from licensing import get_fingerprint
            print(get_fingerprint())
            return 0
        except Exception as e:
            print(f"[ERR] {e}", file=sys.stderr)
            return 1

    _check_license_or_die()

    root = tk.Tk()
    try:
        if os.name == "nt":
            try:
                from ctypes import windll
                windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
        SummaryStatsGUI(root)
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
