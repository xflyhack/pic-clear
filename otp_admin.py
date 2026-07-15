# -*- coding: utf-8 -*-
"""
otp_admin.py —— 作者本人在 Mac 上跑的 TOTP 密钥管理工具。

流程：
  1) 用户发指纹给你（比如 E062-9731-46AC-1C0D）
  2) 你跑 `python3 otp_admin.py generate <指纹> --issued-to <名字>`
     - 生成一个 base32 密钥
     - 保存到 ~/.pic-clear-otp/<指纹>.json
     - 打印 otpauth:// URI（可以复制到 Authenticator 或转 QR 分享）
  3) 生成 otp.secret 文件发给用户，让他放到 exe 同目录
  4) 用户之后要 6 位码时，扫二维码进 Authenticator，或者你跑
     `python3 otp_admin.py current <指纹>` 应急告诉他

数据存放：
  ~/.pic-clear-otp/
  ├── E062-9731-46AC-1C0D.json      ← {secret, issued_to, created_at, issuer}
  ├── A0A0-6D01-06EF-C18E.json
  └── ...

secret 文件（发给用户放到 exe 同目录）：
  otp.secret ← 单行 base32 字符串，纯文本
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import otp_utils


# 密钥库位置：优先环境变量 PIC_CLEAR_OTP_VAULT（Docker 挂载 volume 用），
# 否则默认 ~/.pic-clear-otp。
_env_vault = os.environ.get("PIC_CLEAR_OTP_VAULT")
VAULT_DIR = (Path(_env_vault).expanduser() if _env_vault
             else Path(os.path.expanduser("~")) / ".pic-clear-otp")


def _vault_dir() -> Path:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    return VAULT_DIR


def _rec_path(fingerprint: str) -> Path:
    fp = fingerprint.strip().upper()
    if not fp:
        raise SystemExit("[错误] 指纹不能为空")
    return _vault_dir() / f"{fp}.json"


def _load(fingerprint: str) -> dict:
    p = _rec_path(fingerprint)
    if not p.is_file():
        raise SystemExit(f"[错误] 未找到 {fingerprint} 的密钥记录：{p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _save(fingerprint: str, rec: dict) -> Path:
    p = _rec_path(fingerprint)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    return p


def cmd_generate(args: argparse.Namespace) -> int:
    fp = args.fingerprint.strip().upper()
    if _rec_path(fp).is_file() and not args.force:
        raise SystemExit(
            f"[错误] {fp} 已存在密钥，加 --force 才允许覆盖\n"
            f"       文件位置：{_rec_path(fp)}"
        )
    secret = otp_utils.generate_secret()
    rec = {
        "fingerprint": fp,
        "issued_to": args.issued_to,
        "issuer": args.issuer or otp_utils.DEFAULT_ISSUER,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "secret": secret,
        "algo": otp_utils.DEFAULT_ALGO,
        "digits": otp_utils.DEFAULT_DIGITS,
        "period": otp_utils.DEFAULT_PERIOD,
    }
    p = _save(fp, rec)
    uri = otp_utils.build_otpauth_uri(secret, fp, issuer=rec["issuer"])

    print("=" * 60)
    print(f"  [已签发] 机器指纹 {fp}")
    print("=" * 60)
    print(f"  颁发给      : {args.issued_to or '(未填)'}")
    print(f"  签发时间    : {rec['created_at']}")
    print(f"  密钥文件    : {p}")
    print()
    print(f"  base32 密钥 : {secret}")
    print(f"  当前 6 位码 : {otp_utils.now_code(secret)}")
    print()
    print("  otpauth URI（复制到 Authenticator 或转成二维码分享给用户）：")
    print(f"    {uri}")
    print()
    print("  接下来：把下面这一行内容存成 otp.secret，跟 license.lic 一起发给用户，")
    print("  放到 exe 同目录：")
    print()
    print(f"    {secret}")
    print()

    if args.write_secret_to:
        out = Path(args.write_secret_to).expanduser().resolve()
        out.write_text(secret + "\n", encoding="utf-8")
        print(f"  [√] otp.secret 已写入 {out}")

    return 0


def cmd_current(args: argparse.Namespace) -> int:
    rec = _load(args.fingerprint)
    code = otp_utils.totp_at(rec["secret"])
    remain = otp_utils.seconds_to_next()
    print(f"  {rec['fingerprint']}  →  {code}   （还剩 {remain} 秒过期）")
    if args.watch:
        try:
            while True:
                time.sleep(1)
                code2 = otp_utils.totp_at(rec["secret"])
                remain2 = otp_utils.seconds_to_next()
                sys.stdout.write(
                    f"\r  {rec['fingerprint']}  →  {code2}   "
                    f"（还剩 {remain2:>2d} 秒）   "
                )
                sys.stdout.flush()
        except KeyboardInterrupt:
            print()
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    files = sorted(_vault_dir().glob("*.json"))
    if not files:
        print("  （空）尚未签发任何机器")
        return 0
    print(f"  {'指纹':<22}  {'颁发给':<12}  {'签发时间':<20}  {'当前码':<8}")
    print("  " + "-" * 74)
    for f in files:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        code = otp_utils.totp_at(rec["secret"])
        print(f"  {rec['fingerprint']:<22}  "
              f"{(rec.get('issued_to') or '-'):<12}  "
              f"{rec.get('created_at', '-'):<20}  "
              f"{code:<8}")
    return 0


def cmd_rotate(args: argparse.Namespace) -> int:
    rec = _load(args.fingerprint)
    old_secret = rec["secret"]
    new_secret = otp_utils.generate_secret()
    rec["secret"] = new_secret
    rec["rotated_at"] = datetime.now().isoformat(timespec="seconds")
    rec.setdefault("history", []).append(
        {"secret": old_secret, "retired_at": rec["rotated_at"]})
    _save(rec["fingerprint"], rec)
    print(f"  [已轮换] {rec['fingerprint']}")
    print(f"    旧密钥: {old_secret}")
    print(f"    新密钥: {new_secret}")
    print(f"    当前码: {otp_utils.totp_at(new_secret)}")
    return 0


def cmd_show_uri(args: argparse.Namespace) -> int:
    rec = _load(args.fingerprint)
    uri = otp_utils.build_otpauth_uri(
        rec["secret"], rec["fingerprint"], issuer=rec.get("issuer") or "pic-clear")
    print(uri)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="pic-clear TOTP 密钥管理（作者用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd")

    sp = sub.add_parser("generate", help="给一台机器签发新密钥")
    sp.add_argument("fingerprint", help="机器指纹，例如 E062-9731-46AC-1C0D")
    sp.add_argument("--issued-to", default="", help="颁发给谁（可选）")
    sp.add_argument("--issuer", default=None, help="issuer 名，默认 pic-clear")
    sp.add_argument("--force", action="store_true", help="已存在则覆盖")
    sp.add_argument("--write-secret-to", default=None,
                    help="顺便把纯 base32 写到此文件（就是要发给用户的 otp.secret）")
    sp.set_defaults(func=cmd_generate)

    sp = sub.add_parser("current", help="打印某台机器当前 6 位码")
    sp.add_argument("fingerprint")
    sp.add_argument("-w", "--watch", action="store_true",
                    help="持续刷新显示，Ctrl+C 退出")
    sp.set_defaults(func=cmd_current)

    sp = sub.add_parser("list", help="列出所有已签发的机器")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("rotate", help="给某台机器换一个新密钥")
    sp.add_argument("fingerprint")
    sp.set_defaults(func=cmd_rotate)

    sp = sub.add_parser("uri", help="打印 otpauth:// URI（可转二维码）")
    sp.add_argument("fingerprint")
    sp.set_defaults(func=cmd_show_uri)

    args = ap.parse_args()
    if not getattr(args, "func", None):
        ap.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
