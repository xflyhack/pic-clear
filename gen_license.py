#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成 license.lic 的开发端工具。仅在作者本机（Mac）运行。
不进入 exe 分发。

用法：
    python gen_license.py A1B2-C3D4-E5F6-7890 --issued-to xflyhack
    python gen_license.py A1B2-C3D4-E5F6-7890 --issued-to lisi \
        --expire 2027-12-31 --note "内测许可" --output lisi.lic
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import date
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(
        description="签发 license.lic（本地开发工具，不打包进 exe）"
    )
    ap.add_argument(
        "fingerprint",
        help="用户机器指纹，形如 XXXX-XXXX-XXXX-XXXX",
    )
    ap.add_argument(
        "--issued-to",
        default="user",
        help="授权对象姓名/工号，会写进 license，方便日后追溯。默认 user",
    )
    ap.add_argument(
        "--expire",
        default="never",
        help="到期日期 YYYY-MM-DD 或 never。默认 never（永久）",
    )
    ap.add_argument("--note", default="", help="任意备注")
    ap.add_argument(
        "--private-key",
        default=str(Path.home() / ".dedupe_pic_keys" / "private.pem"),
        help="RSA 私钥路径，默认 ~/.dedupe_pic_keys/private.pem",
    )
    ap.add_argument(
        "--output",
        default="license.lic",
        help="输出的 license 文件名，默认 license.lic",
    )
    args = ap.parse_args()

    # 校验私钥存在
    priv_path = Path(args.private_key)
    if not priv_path.is_file():
        print(f"[FATAL] 私钥不存在: {priv_path}", file=sys.stderr)
        print(
            "先在本机生成：\n"
            "  mkdir -p ~/.dedupe_pic_keys && cd ~/.dedupe_pic_keys\n"
            "  openssl genrsa -out private.pem 2048\n"
            "  openssl rsa -in private.pem -pubout -out public.pem\n"
            "  chmod 600 private.pem",
            file=sys.stderr,
        )
        return 2

    # 规范化指纹
    fp = args.fingerprint.upper().replace(" ", "")
    if fp.count("-") != 3 or len(fp.replace("-", "")) != 16:
        print(
            f"[FATAL] 指纹格式错误: {args.fingerprint}\n"
            "应形如 A1B2-C3D4-E5F6-7890",
            file=sys.stderr,
        )
        return 2

    # 校验 expire 格式
    if args.expire != "never":
        try:
            date.fromisoformat(args.expire)
        except Exception:
            print(
                f"[FATAL] --expire 格式错误: {args.expire}"
                "（应为 YYYY-MM-DD 或 never）",
                file=sys.stderr,
            )
            return 2

    # 构造 payload
    payload_dict = {
        "fingerprint": fp,
        "issued_to": args.issued_to,
        "issued_at": date.today().isoformat(),
        "expire_date": args.expire,
        "note": args.note,
    }
    payload = json.dumps(
        payload_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")

    # 签名
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        print(
            "[FATAL] 未安装 cryptography。请: pip install cryptography",
            file=sys.stderr,
        )
        return 2

    priv = serialization.load_pem_private_key(priv_path.read_bytes(), password=None)
    sig = priv.sign(
        payload,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )

    out_path = Path(args.output)
    out_path.write_text(
        base64.b64encode(payload).decode()
        + "\n"
        + base64.b64encode(sig).decode()
        + "\n",
        encoding="utf-8",
    )

    print(f"[OK] 已生成 {out_path.resolve()}")
    print(f"     指纹     : {fp}")
    print(f"     发放给   : {args.issued_to}")
    print(f"     到期     : {args.expire}")
    if args.note:
        print(f"     备注     : {args.note}")
    print()
    print("把这个 license.lic 文件发给用户，放到 dedupe_pic.exe 同目录即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
