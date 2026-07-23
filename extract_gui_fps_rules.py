# -*- coding: utf-8 -*-
"""extract_gui_fps_rules.py — extract_gui 里"差异化 fps 规则"UI 组件.

设计要点:
- 主体是 FpsRulesFrame: 挂到 extract_gui 基础 tab 上的 LabelFrame
  * 一个"启用"复选框
  * camera_regex 输入框
  * Treeview 展示规则列表
  * 底部按钮条: 新增/删除/上移/下移/导入/导出/测试匹配
- 规则编辑用 Toplevel 对话框 (RuleEditDialog)
- 测试匹配用 Toplevel 对话框 (TestMatchDialog)
- 内部状态: self.state = {"enabled": bool, "camera_regex": str, "rules": [dict]}
  rules 里每条: {"keyword": str, "ids": [str,...], "speed": float}

拆出来的原因: extract_gui.py 已经 1000+ 行, 这块 UI 独立打包便于维护 + 复用.
"""
from __future__ import annotations

import json
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable

try:
    import fps_rules as _fr
except Exception:
    _fr = None  # type: ignore


DEFAULT_CAMERA_REGEX = r"camera(\d+)"


def _default_state() -> dict:
    return {
        "enabled": False,
        "camera_regex": DEFAULT_CAMERA_REGEX,
        "rules": [],
    }


class FpsRulesFrame(ttk.LabelFrame):
    """挂在抽帧 tab 上的 LabelFrame. 内含一切规则相关控件.

    使用:
        rules_frame = FpsRulesFrame(parent, initial_state=cfg.get("fps_rules") or {})
        rules_frame.pack(fill="x", padx=8, pady=4)
        # 保存时:
        cfg["fps_rules_enabled"] = rules_frame.get_enabled()
        cfg["fps_rules"] = rules_frame.get_state()
        # 传给 CLI:
        rules_frame.dump_cli_json(dst_path)   # 生成 --fps-rules 需要的 JSON
    """

    COLS = ("enabled", "keyword", "ids", "speed")
    COL_TITLES = {
        "enabled": "启用",
        "keyword": "关键字",
        "ids":     "编号规则",
        "speed":   "抽帧率",
    }
    COL_WIDTHS = {"enabled": 50, "keyword": 120, "ids": 260, "speed": 80}

    def __init__(self, parent: tk.Misc, *, initial_state: dict | None = None) -> None:
        super().__init__(parent, text="差异化抽帧规则 (可选)")
        self.state: dict = _default_state()
        if isinstance(initial_state, dict):
            self.state["enabled"] = bool(initial_state.get("enabled", False))
            self.state["camera_regex"] = str(
                initial_state.get("camera_regex") or DEFAULT_CAMERA_REGEX)
            _rules = initial_state.get("rules") or []
            if isinstance(_rules, list):
                self.state["rules"] = [self._normalize_rule(r) for r in _rules
                                       if isinstance(r, dict)]

        self._enabled_var = tk.BooleanVar(value=bool(self.state["enabled"]))
        self._regex_var = tk.StringVar(value=self.state["camera_regex"])
        self._enabled_var.trace_add("write", lambda *_a: self._sync_enabled())

        self._build_ui()
        self._reload_tree()
        self._sync_enabled()

    # ---------------- 内部数据规整 ----------------

    @staticmethod
    def _normalize_rule(r: dict) -> dict:
        return {
            "enabled": bool(r.get("enabled", True)),
            "keyword": str(r.get("keyword", "")).strip(),
            "ids": [str(x).strip() for x in (r.get("ids") or [])
                    if str(x).strip()],
            "speed": float(r.get("speed", 1.0)),
        }

    # ---------------- UI 搭建 ----------------

    def _build_ui(self) -> None:
        pad = {"padx": 6, "pady": 3}

        # 启用勾 + 说明
        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Checkbutton(
            top, text="启用规则", variable=self._enabled_var,
        ).pack(side="left")
        ttk.Label(
            top, text="  勾选后, 命中规则的视频会用规则里的 fps 替换默认 fps",
            foreground="#666",
        ).pack(side="left")

        # camera_regex 行
        row = ttk.Frame(self); row.pack(fill="x", **pad)
        ttk.Label(row, text="编号提取正则:", width=14).pack(side="left")
        self._regex_entry = ttk.Entry(row, textvariable=self._regex_var, width=32)
        self._regex_entry.pack(side="left")
        ttk.Button(
            row, text="恢复默认",
            command=lambda: self._regex_var.set(DEFAULT_CAMERA_REGEX),
        ).pack(side="left", padx=4)
        ttk.Label(
            row, text="  从文件名里取 编号(int) 用. 留空 = 不看编号只看关键字.",
            foreground="#666",
        ).pack(side="left")

        # Treeview
        tree_wrap = ttk.Frame(self); tree_wrap.pack(fill="x", padx=6, pady=4)
        self._tree = ttk.Treeview(
            tree_wrap, columns=self.COLS, show="headings", height=6,
        )
        for c in self.COLS:
            self._tree.heading(c, text=self.COL_TITLES[c])
            self._tree.column(c, width=self.COL_WIDTHS[c],
                              anchor="center" if c in ("enabled", "speed") else "w")
        self._tree.pack(side="left", fill="x", expand=True)
        sb = ttk.Scrollbar(tree_wrap, orient="vertical",
                           command=self._tree.yview)
        sb.pack(side="left", fill="y")
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.bind("<Double-1>", lambda _e: self._on_edit_current())

        # 按钮条
        btnbar = ttk.Frame(self); btnbar.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(btnbar, text="+ 新增", command=self._on_add).pack(side="left")
        ttk.Button(btnbar, text="✎ 编辑", command=self._on_edit_current).pack(side="left", padx=(4, 0))
        ttk.Button(btnbar, text="🗑 删除", command=self._on_delete).pack(side="left", padx=(4, 0))
        ttk.Button(btnbar, text="↑ 上移", command=lambda: self._on_move(-1)).pack(side="left", padx=(8, 0))
        ttk.Button(btnbar, text="↓ 下移", command=lambda: self._on_move(+1)).pack(side="left", padx=(4, 0))
        ttk.Button(btnbar, text="导入JSON", command=self._on_import).pack(side="left", padx=(12, 0))
        ttk.Button(btnbar, text="导出JSON", command=self._on_export).pack(side="left", padx=(4, 0))
        ttk.Button(btnbar, text="测试匹配…", command=self._on_test_match).pack(side="left", padx=(12, 0))
        ttk.Button(btnbar, text="清空", command=self._on_clear).pack(side="right")

    def _sync_enabled(self) -> None:
        """启用勾变化: 灰掉/激活内部控件. 空表格时也灰."""
        st = "normal" if self._enabled_var.get() else "disabled"
        try:
            self._regex_entry.configure(state=st)
        except Exception:
            pass
        # Treeview 灰不了, 但按钮条可以. 我们简单粗暴 disable 全部按钮.
        for child in self.winfo_children():
            for sub in getattr(child, "winfo_children", lambda: [])():
                if isinstance(sub, ttk.Button):
                    # 保留 "恢复默认" 只在 regex_entry 那行
                    try:
                        sub.configure(state=st)
                    except Exception:
                        pass

    def _reload_tree(self) -> None:
        for iid in self._tree.get_children():
            self._tree.delete(iid)
        for i, r in enumerate(self.state["rules"]):
            self._tree.insert(
                "", "end", iid=str(i),
                values=(
                    "☑" if r.get("enabled", True) else "☐",
                    r.get("keyword", ""),
                    ", ".join(r.get("ids") or []),
                    r.get("speed", 1.0),
                ),
            )

    # ---------------- 按钮回调 ----------------

    def _current_index(self) -> int | None:
        sel = self._tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except Exception:
            return None

    def _on_add(self) -> None:
        dlg = RuleEditDialog(self, rule=None)
        self.wait_window(dlg)
        if dlg.result is not None:
            self.state["rules"].append(dlg.result)
            self._reload_tree()

    def _on_edit_current(self) -> None:
        idx = self._current_index()
        if idx is None:
            messagebox.showinfo("提示", "请先选中一行", parent=self)
            return
        dlg = RuleEditDialog(self, rule=dict(self.state["rules"][idx]))
        self.wait_window(dlg)
        if dlg.result is not None:
            self.state["rules"][idx] = dlg.result
            self._reload_tree()
            self._tree.selection_set(str(idx))

    def _on_delete(self) -> None:
        idx = self._current_index()
        if idx is None:
            messagebox.showinfo("提示", "请先选中一行", parent=self)
            return
        r = self.state["rules"][idx]
        if not messagebox.askyesno(
                "确认删除", f"删除规则 keyword={r.get('keyword')} ?",
                parent=self):
            return
        del self.state["rules"][idx]
        self._reload_tree()

    def _on_move(self, delta: int) -> None:
        idx = self._current_index()
        if idx is None:
            return
        new_idx = idx + delta
        if new_idx < 0 or new_idx >= len(self.state["rules"]):
            return
        rules = self.state["rules"]
        rules[idx], rules[new_idx] = rules[new_idx], rules[idx]
        self._reload_tree()
        self._tree.selection_set(str(new_idx))

    def _on_clear(self) -> None:
        if not self.state["rules"]:
            return
        if not messagebox.askyesno("确认清空", "清空所有规则?", parent=self):
            return
        self.state["rules"] = []
        self._reload_tree()

    def _on_import(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="选择规则 JSON",
            filetypes=[("JSON", "*.json"), ("All", "*")],
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            if _fr is None:
                raise RuntimeError("fps_rules 模块未加载")
            rs = _fr.parse_fps_rules(data)
        except Exception as e:
            messagebox.showerror(
                "导入失败", f"{type(e).__name__}: {e}", parent=self)
            return
        self.state["camera_regex"] = rs.camera_regex or DEFAULT_CAMERA_REGEX
        self._regex_var.set(self.state["camera_regex"])
        self.state["rules"] = [
            {"enabled": True, "keyword": r.keyword,
             "ids": list(r.ids), "speed": float(r.speed)}
            for r in rs.rules
        ]
        self._reload_tree()
        messagebox.showinfo(
            "导入成功", f"已加载 {len(rs.rules)} 条规则", parent=self)

    def _on_export(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self, title="导出规则 JSON",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All", "*")],
        )
        if not path:
            return
        # 按 speed 聚合成 groups 格式, 更贴近用户手写风格
        try:
            groups: list[dict] = []
            by_speed: dict[float, dict] = {}
            for r in self.state["rules"]:
                if not r.get("enabled", True):
                    continue
                sp = float(r.get("speed", 1.0))
                g = by_speed.get(sp)
                if g is None:
                    g = {"speed": sp}
                    by_speed[sp] = g
                    groups.append(g)
                g[r["keyword"]] = list(r.get("ids") or [])
            data = {
                "camera_regex": self._regex_var.get() or DEFAULT_CAMERA_REGEX,
                "groups": groups,
            }
            with open(path, "w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
        except Exception as e:
            messagebox.showerror(
                "导出失败", f"{type(e).__name__}: {e}", parent=self)
            return
        messagebox.showinfo("导出成功", f"已写入 {path}", parent=self)

    def _on_test_match(self) -> None:
        if _fr is None:
            messagebox.showerror("测试匹配", "fps_rules 模块未加载", parent=self)
            return
        # 用当前 UI 状态构造一个临时 ruleset (只包含 enabled=True 的规则)
        try:
            data = self._to_cli_dict(default_fps_placeholder=1.0)
            rs = _fr.parse_fps_rules(data)
        except Exception as e:
            messagebox.showerror("测试匹配", f"规则构造失败: {e}", parent=self)
            return
        TestMatchDialog(self, ruleset=rs)

    # ---------------- 对外接口 ----------------

    def get_enabled(self) -> bool:
        return bool(self._enabled_var.get())

    def get_state(self) -> dict:
        return {
            "enabled": self.get_enabled(),
            "camera_regex": self._regex_var.get() or DEFAULT_CAMERA_REGEX,
            "rules": [dict(r) for r in self.state["rules"]],
        }

    def has_any_active_rule(self) -> bool:
        return any(r.get("enabled", True) for r in self.state["rules"])

    def _to_cli_dict(self, *, default_fps_placeholder: float) -> dict:
        """转成 CLI 认识的 flat schema."""
        rules_out = []
        for r in self.state["rules"]:
            if not r.get("enabled", True):
                continue
            kw = (r.get("keyword") or "").strip()
            if not kw:
                continue
            rules_out.append({
                "keyword": kw,
                "ids": list(r.get("ids") or []),
                "speed": float(r.get("speed", 1.0)),
            })
        return {
            "default_fps": float(default_fps_placeholder),
            "camera_regex": self._regex_var.get() or DEFAULT_CAMERA_REGEX,
            "rules": rules_out,
        }

    def dump_cli_json(self, path: str, *, default_fps: float) -> bool:
        """把当前状态写成 CLI 用的 JSON. 无启用规则 -> 返回 False, 不写文件."""
        data = self._to_cli_dict(default_fps_placeholder=default_fps)
        if not data["rules"]:
            return False
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        return True


# ==========================================================================
#                             编辑 / 测试对话框
# ==========================================================================

class RuleEditDialog(tk.Toplevel):
    """新增 / 编辑一条 fps 规则."""

    def __init__(self, parent: tk.Misc, *, rule: dict | None) -> None:
        super().__init__(parent)
        self.title("编辑规则" if rule else "新增规则")
        self.transient(parent)  # type: ignore[arg-type]
        self.grab_set()
        self.result: dict | None = None

        rule = rule or {"enabled": True, "keyword": "",
                        "ids": [], "speed": 30.0}
        self._enabled_var = tk.BooleanVar(value=bool(rule.get("enabled", True)))
        self._keyword_var = tk.StringVar(value=str(rule.get("keyword", "")))
        self._ids_var = tk.StringVar(
            value=", ".join(rule.get("ids") or []))
        self._speed_var = tk.DoubleVar(value=float(rule.get("speed", 30.0)))

        pad = {"padx": 8, "pady": 4}

        r0 = ttk.Frame(self); r0.pack(fill="x", **pad)
        ttk.Checkbutton(r0, text="启用本规则",
                        variable=self._enabled_var).pack(side="left")

        r1 = ttk.Frame(self); r1.pack(fill="x", **pad)
        ttk.Label(r1, text="关键字:", width=10).pack(side="left")
        ttk.Entry(r1, textvariable=self._keyword_var, width=24).pack(side="left")
        ttk.Label(r1, text=" (大小写不敏感, 例: dtss)",
                  foreground="#666").pack(side="left")

        r2 = ttk.Frame(self); r2.pack(fill="x", **pad)
        ttk.Label(r2, text="编号规则:", width=10).pack(side="left")
        ttk.Entry(r2, textvariable=self._ids_var, width=40).pack(side="left")

        r2b = ttk.Frame(self); r2b.pack(fill="x", padx=8)
        ttk.Label(
            r2b,
            text=("  逗号分隔; 支持单值和区间 (~), 例: '02, 16, 14~17' 或 "
                  "'camera02~camera16'; 留空=只按关键字命中"),
            foreground="#666", wraplength=520, justify="left",
        ).pack(anchor="w")
        self._err_lbl = ttk.Label(r2b, text="", foreground="#c00")
        self._err_lbl.pack(anchor="w", padx=2)

        r3 = ttk.Frame(self); r3.pack(fill="x", **pad)
        ttk.Label(r3, text="抽帧率:", width=10).pack(side="left")
        ttk.Spinbox(r3, from_=0.1, to=60.0, increment=0.5, width=8,
                    textvariable=self._speed_var).pack(side="left")
        ttk.Label(r3, text="  fps, 例: 30",
                  foreground="#666").pack(side="left")

        btns = ttk.Frame(self); btns.pack(fill="x", pady=8, padx=8)
        self._ok_btn = ttk.Button(btns, text="确定", command=self._on_ok)
        self._ok_btn.pack(side="right", padx=4)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right")

        # 实时校验
        self._ids_var.trace_add("write", lambda *_a: self._revalidate())
        self._revalidate()

        self.bind("<Return>", lambda _e: self._on_ok())
        self.bind("<Escape>", lambda _e: self.destroy())

    def _revalidate(self) -> None:
        if _fr is None:
            return
        ok, err = _fr.validate_ids_spec(self._ids_var.get())
        self._err_lbl.configure(text="" if ok else err)
        try:
            self._ok_btn.configure(state="normal" if ok else "disabled")
        except Exception:
            pass

    def _on_ok(self) -> None:
        kw = self._keyword_var.get().strip()
        if not kw:
            messagebox.showerror("错误", "关键字不能为空", parent=self)
            return
        try:
            speed = float(self._speed_var.get())
        except Exception:
            messagebox.showerror("错误", "抽帧率必须是数字", parent=self)
            return
        if speed <= 0:
            messagebox.showerror("错误", "抽帧率必须 > 0", parent=self)
            return
        if _fr is not None:
            ids = _fr.parse_ids_spec(self._ids_var.get())
        else:
            ids = [x.strip() for x in self._ids_var.get().split(",") if x.strip()]
        self.result = {
            "enabled": bool(self._enabled_var.get()),
            "keyword": kw,
            "ids": ids,
            "speed": speed,
        }
        self.destroy()


class TestMatchDialog(tk.Toplevel):
    """测试匹配对话框: 粘一批文件名, 展示每个命中哪条规则、用什么 fps."""

    def __init__(self, parent: tk.Misc, *, ruleset) -> None:
        super().__init__(parent)
        self.title("测试匹配")
        self.transient(parent)  # type: ignore[arg-type]
        self.grab_set()
        self._ruleset = ruleset

        pad = {"padx": 8, "pady": 4}

        ttk.Label(
            self,
            text="在下方粘贴/编辑视频文件名 (每行一个), 点 跑一遍 查看结果:",
        ).pack(anchor="w", **pad)

        wrap = ttk.Frame(self); wrap.pack(fill="both", expand=True, **pad)
        self._input = tk.Text(wrap, height=8, width=80)
        self._input.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self._input.yview)
        sb.pack(side="left", fill="y")
        self._input.configure(yscrollcommand=sb.set)

        self._input.insert("1.0", (
            "cyc_24_nan_qt_2000lx_DTSS_260722_144229_camera01\n"
            "cyc_24_nan_qt_2000lx_DTSS_260722_144229_camera03\n"
            "cyc_24_nan_qt_2000lx_DTSS_260722_144229_camera15\n"
            "cyc_24_nan_qt_2000lx_CnDTSS_260722_144229_camera16\n"
            "cyc_24_nan_qt_2000lx_CWDTSS_260722_144229_camera08\n"
        ))

        btn = ttk.Frame(self); btn.pack(fill="x", **pad)
        ttk.Button(btn, text="跑一遍", command=self._run).pack(side="left")
        ttk.Button(btn, text="关闭", command=self.destroy).pack(side="right")

        self._result = tk.Text(self, height=12, width=100, bg="#f6f6f6")
        self._result.pack(fill="both", expand=True, **pad)

        self.bind("<Escape>", lambda _e: self.destroy())

    def _run(self) -> None:
        if _fr is None:
            self._result.delete("1.0", "end")
            self._result.insert("1.0", "fps_rules 模块未加载")
            return
        names = [ln.strip() for ln in self._input.get("1.0", "end").splitlines()
                 if ln.strip()]
        self._result.delete("1.0", "end")
        if not names:
            self._result.insert("1.0", "(输入为空)")
            return
        lines = []
        max_len = min(80, max(len(n) for n in names))
        for name in names:
            fps, hit = _fr.resolve_fps_for_video(name, self._ruleset)
            hit_txt = f"命中={hit}" if hit else "命中=(无)"
            lines.append(f"{name.ljust(max_len)}  fps={fps:<6}  {hit_txt}")
        self._result.insert("1.0", "\n".join(lines))
