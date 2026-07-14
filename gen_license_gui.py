# -*- coding: utf-8 -*-
"""
gen_license_gui.py —— license.lic 签发工具（GUI 版）

⚠️ 此工具**内置签发私钥**（PyInstaller 打包时通过 --add-data 把 secrets/private.pem
   一起打包进 exe）。**仅限作者/内部使用，不要外发**。
   拿到此 exe 的人具备签发任意 license 的能力。

功能：
- tkinter 表单：指纹 / 发放对象 / 到期日 / 备注 / 输出路径
- 一键生成 license.lic
- 支持切换『内置私钥』（默认）和『外部私钥文件』两种模式
"""

from __future__ import annotations

# ---- 版本 / 版权 ----
APP_VERSION = "v0.1.1"
COPYRIGHT_TEXT = "本工具版权归山东数旗信息科技有限公司所有"

import base64
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

# --- tkinter ---
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except Exception as e:
    print(f"[FATAL] 缺少 tkinter: {e}", file=sys.stderr)
    sys.exit(1)

# --- cryptography ---
try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
except Exception as e:
    print(f"[FATAL] 缺少 cryptography 库: {e}", file=sys.stderr)
    sys.exit(1)


# =========================================================================
# 内置私钥定位
# =========================================================================

def _bundled_private_key_path() -> Path | None:
    """PyInstaller onefile 打包后，--add-data 的资源在 sys._MEIPASS 下。

    我们把 secrets/private.pem 一起打包，运行时从 _MEIPASS 里读。
    开发模式下（frozen=False）从项目目录读。
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parent
    candidates = [
        base / "secrets" / "private.pem",   # 主要位置
        base / "private.pem",               # 备选：直接放根
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


# =========================================================================
# 签发核心（跟 gen_license.py 里的逻辑一致）
# =========================================================================

def sign_license(
    fingerprint: str,
    issued_to: str,
    expire_date: str,
    note: str,
    private_key_bytes: bytes,
) -> bytes:
    """按 pic-clear 授权协议签发 license，返回 license.lic 的完整文本内容（bytes）。

    格式：
        base64(payload_json)\\n
        base64(signature)\\n
    """
    payload_dict = {
        "fingerprint": fingerprint,
        "issued_to": issued_to,
        "issued_at": date.today().isoformat(),
        "expire_date": expire_date,
        "note": note,
    }
    payload = json.dumps(
        payload_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")

    priv = serialization.load_pem_private_key(private_key_bytes, password=None)
    sig = priv.sign(
        payload,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    text = (
        base64.b64encode(payload).decode()
        + "\n"
        + base64.b64encode(sig).decode()
        + "\n"
    )
    return text.encode("utf-8")


# =========================================================================
# GUI
# =========================================================================

class GenLicenseGUI:
    APP_TITLE = f"license.lic 签发工具（内置私钥版，仅限内部）  {APP_VERSION}"

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(self.APP_TITLE)
        self.root.geometry("620x520")

        # 状态变量
        self._fp_var = tk.StringVar()
        self._issued_to_var = tk.StringVar(value="user")
        self._expire_mode_var = tk.StringVar(value="never")  # "never" | "date"
        self._expire_date_var = tk.StringVar(value=self._default_expire_date())
        self._note_var = tk.StringVar()
        self._out_var = tk.StringVar()

        # 私钥来源：built_in / external
        self._key_source_var = tk.StringVar(value="built_in")
        self._external_key_var = tk.StringVar()

        self._built_in_key = _bundled_private_key_path()

        self._build_ui()

    @staticmethod
    def _default_expire_date() -> str:
        today = date.today()
        return f"{today.year + 1}-{today.month:02d}-{today.day:02d}"

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # 顶部警告
        warn = tk.Label(
            self.root,
            text="⚠ 此工具内置签发私钥，仅限内部使用，请勿外发。",
            fg="red", font=("Microsoft YaHei", 10, "bold"),
            anchor="w",
        )
        warn.pack(fill="x", padx=8, pady=(8, 0))

        # 版权行（红色）
        copyright_label = tk.Label(
            self.root,
            text=COPYRIGHT_TEXT,
            fg="red", font=("Microsoft YaHei", 10, "bold"),
            anchor="w",
        )
        copyright_label.pack(fill="x", padx=8, pady=(0, 0))

        # 内置私钥状态
        key_hint_frame = tk.Frame(self.root)
        key_hint_frame.pack(fill="x", padx=8, pady=(0, 4))
        if self._built_in_key:
            tk.Label(
                key_hint_frame,
                text=f"内置私钥: {self._built_in_key}",
                fg="green", anchor="w",
            ).pack(fill="x")
        else:
            tk.Label(
                key_hint_frame,
                text="⚠ 未检测到内置私钥（开发模式下正常；打包后应有）",
                fg="orange", anchor="w",
            ).pack(fill="x")

        # ---- 指纹 ----
        f_fp = ttk.LabelFrame(self.root, text="▶ 机器指纹")
        f_fp.pack(fill="x", **pad)
        row = ttk.Frame(f_fp); row.pack(fill="x", padx=6, pady=6)
        ttk.Label(row, text="指纹:", width=8).pack(side="left")
        entry_fp = ttk.Entry(row, textvariable=self._fp_var, width=40, font=("Consolas", 11))
        entry_fp.pack(side="left", fill="x", expand=True)
        entry_fp.focus_set()
        ttk.Label(f_fp, text="  形如 XXXX-XXXX-XXXX-XXXX，会自动大写", foreground="gray").pack(anchor="w", padx=6)

        # ---- 授权信息 ----
        f_info = ttk.LabelFrame(self.root, text="▶ 授权信息")
        f_info.pack(fill="x", **pad)

        row = ttk.Frame(f_info); row.pack(fill="x", padx=6, pady=3)
        ttk.Label(row, text="发放给:", width=8).pack(side="left")
        ttk.Entry(row, textvariable=self._issued_to_var, width=30).pack(side="left")

        row = ttk.Frame(f_info); row.pack(fill="x", padx=6, pady=3)
        ttk.Label(row, text="到期:", width=8).pack(side="left")
        ttk.Radiobutton(row, text="永久 (never)", variable=self._expire_mode_var,
                        value="never").pack(side="left")
        ttk.Radiobutton(row, text="到期日", variable=self._expire_mode_var,
                        value="date").pack(side="left", padx=(12, 4))
        ttk.Entry(row, textvariable=self._expire_date_var, width=14).pack(side="left")
        ttk.Label(row, text="  (YYYY-MM-DD)", foreground="gray").pack(side="left")

        row = ttk.Frame(f_info); row.pack(fill="x", padx=6, pady=3)
        ttk.Label(row, text="备注:", width=8).pack(side="left")
        ttk.Entry(row, textvariable=self._note_var, width=48).pack(side="left", fill="x", expand=True)

        # ---- 私钥来源 ----
        f_key = ttk.LabelFrame(self.root, text="▶ 私钥来源")
        f_key.pack(fill="x", **pad)
        row = ttk.Frame(f_key); row.pack(fill="x", padx=6, pady=3)
        ttk.Radiobutton(row, text="使用内置私钥", variable=self._key_source_var,
                        value="built_in").pack(side="left")
        ttk.Radiobutton(row, text="外部文件:", variable=self._key_source_var,
                        value="external").pack(side="left", padx=(12, 4))
        ttk.Entry(row, textvariable=self._external_key_var, width=32).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览...", command=self._browse_key).pack(side="left", padx=4)

        # ---- 输出 ----
        f_out = ttk.LabelFrame(self.root, text="▶ 输出")
        f_out.pack(fill="x", **pad)
        row = ttk.Frame(f_out); row.pack(fill="x", padx=6, pady=6)
        ttk.Label(row, text="保存到:", width=8).pack(side="left")
        ttk.Entry(row, textvariable=self._out_var, width=48).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览...", command=self._browse_out).pack(side="left", padx=4)

        # 输出路径默认值：当前工作目录/license.lic
        self._out_var.set(str(Path.cwd() / "license.lic"))

        # ---- 底部按钮 ----
        f_btn = ttk.Frame(self.root); f_btn.pack(fill="x", pady=8)
        ttk.Button(f_btn, text="生成 license.lic", command=self._on_generate,
                   width=20).pack(side="left", padx=10)
        ttk.Button(f_btn, text="填示例", command=self._fill_example,
                   width=10).pack(side="left")
        ttk.Button(f_btn, text="退出", command=self.root.destroy,
                   width=10).pack(side="right", padx=10)

        # ---- 底部 status bar：版权 + 版本 ----
        # 注意：必须先 pack 到 bottom，再 pack 结果 Text，否则 Text 的 expand=True
        # 会把 status_bar 挤到看不见的地方。
        status_bar = tk.Label(
            self.root,
            text=f"{COPYRIGHT_TEXT}    |    {APP_VERSION}",
            bd=1, relief="sunken", anchor="e",
            font=("Microsoft YaHei", 9),
            fg="gray",
        )
        status_bar.pack(side="bottom", fill="x")

        # ---- 结果显示 ----
        self._result = tk.Text(self.root, height=6, font=("Consolas", 9))
        self._result.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._result.insert("end", "填好上面的信息后，点『生成 license.lic』。\n")
        self._result.config(state="disabled")

    # ---------- 事件 ----------

    def _fill_example(self):
        self._fp_var.set("E915-F232-792C-5B41")
        self._issued_to_var.set("xflyhack")
        self._expire_mode_var.set("never")
        self._note_var.set("示例")

    def _browse_key(self):
        p = filedialog.askopenfilename(
            title="选择 private.pem 私钥",
            filetypes=[("PEM 私钥", "*.pem *.key"), ("所有文件", "*.*")],
        )
        if p:
            self._external_key_var.set(p)
            self._key_source_var.set("external")

    def _browse_out(self):
        init = self._out_var.get() or str(Path.cwd())
        init_dir = str(Path(init).parent) if init else str(Path.cwd())
        p = filedialog.asksaveasfilename(
            title="保存 license.lic 到...",
            initialdir=init_dir,
            initialfile="license.lic",
            defaultextension=".lic",
            filetypes=[("License 文件", "*.lic"), ("所有文件", "*.*")],
        )
        if p:
            self._out_var.set(p)

    def _validate_fingerprint(self, fp: str) -> tuple[bool, str]:
        fp = fp.upper().replace(" ", "")
        if fp.count("-") != 3 or len(fp.replace("-", "")) != 16:
            return False, fp
        return True, fp

    def _log(self, msg: str, level: str = "info"):
        self._result.config(state="normal")
        prefix = {"info": "[INFO] ", "ok": "[ OK ] ", "err": "[ERR ] "}.get(level, "")
        self._result.insert("end", prefix + msg + "\n")
        self._result.see("end")
        self._result.config(state="disabled")

    def _clear_log(self):
        self._result.config(state="normal")
        self._result.delete("1.0", "end")
        self._result.config(state="disabled")

    def _on_generate(self):
        self._clear_log()

        # 1. 校验指纹
        ok, fp = self._validate_fingerprint(self._fp_var.get())
        if not ok:
            self._log(f"指纹格式错误: {self._fp_var.get()!r}\n应形如 XXXX-XXXX-XXXX-XXXX", "err")
            messagebox.showerror("指纹格式错误", "应形如 XXXX-XXXX-XXXX-XXXX")
            return
        # 回填规范化后的指纹
        self._fp_var.set(fp)

        # 2. issued_to
        issued_to = self._issued_to_var.get().strip() or "user"

        # 3. expire
        if self._expire_mode_var.get() == "never":
            expire = "never"
        else:
            expire = self._expire_date_var.get().strip()
            try:
                date.fromisoformat(expire)
            except Exception:
                self._log(f"到期日格式错误: {expire}（应为 YYYY-MM-DD）", "err")
                messagebox.showerror("到期日格式错误", "应为 YYYY-MM-DD")
                return

        note = self._note_var.get().strip()

        # 4. 私钥
        if self._key_source_var.get() == "built_in":
            if not self._built_in_key:
                self._log("找不到内置私钥（可能是开发模式）。请切换到『外部文件』", "err")
                messagebox.showerror("私钥缺失", "找不到内置私钥。请切换到『外部文件』")
                return
            try:
                key_bytes = self._built_in_key.read_bytes()
            except Exception as e:
                self._log(f"读取内置私钥失败: {e}", "err")
                return
            key_src = f"内置 ({self._built_in_key})"
        else:
            ext = self._external_key_var.get().strip()
            if not ext or not Path(ext).is_file():
                self._log(f"外部私钥无效: {ext!r}", "err")
                messagebox.showerror("私钥无效", f"外部私钥文件不存在: {ext}")
                return
            try:
                key_bytes = Path(ext).read_bytes()
            except Exception as e:
                self._log(f"读取外部私钥失败: {e}", "err")
                return
            key_src = f"外部 ({ext})"

        # 5. 输出路径
        out = self._out_var.get().strip()
        if not out:
            self._log("请填输出路径", "err")
            return
        out_path = Path(out)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self._log(f"创建输出目录失败: {e}", "err")
            return

        # 6. 签发
        try:
            lic_bytes = sign_license(fp, issued_to, expire, note, key_bytes)
        except Exception as e:
            self._log(f"签发失败: {type(e).__name__}: {e}", "err")
            messagebox.showerror("签发失败", f"{type(e).__name__}: {e}")
            return

        try:
            out_path.write_bytes(lic_bytes)
        except Exception as e:
            self._log(f"写文件失败: {e}", "err")
            messagebox.showerror("写文件失败", str(e))
            return

        # 成功
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._log(f"生成成功 ({ts})", "ok")
        self._log(f"  文件     : {out_path}")
        self._log(f"  指纹     : {fp}")
        self._log(f"  发放给   : {issued_to}")
        self._log(f"  到期     : {expire}")
        if note:
            self._log(f"  备注     : {note}")
        self._log(f"  私钥来源 : {key_src}")
        messagebox.showinfo(
            "签发完成",
            f"已生成: {out_path}\n\n"
            f"指纹: {fp}\n"
            f"发放给: {issued_to}\n"
            f"到期: {expire}\n\n"
            "把这个 .lic 文件发给用户，放到 exe 同目录即可。"
        )


# =========================================================================
# 入口
# =========================================================================

def main() -> int:
    root = tk.Tk()
    try:
        if os.name == "nt":
            try:
                from ctypes import windll
                windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
        GenLicenseGUI(root)
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
