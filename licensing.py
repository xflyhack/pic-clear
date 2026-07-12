# -*- coding: utf-8 -*-
"""
授权校验模块：机器指纹 + RSA-PSS-SHA256 签名。

- Windows 环境下从 wmic/powershell 抓主板序列号、磁盘序列号、hostname
- 拼接后 SHA256 取 16 位 → XXXX-XXXX-XXXX-XXXX 展示
- license.lic 格式：
    LINE 1: BASE64(JSON payload)
    LINE 2: BASE64(RSA-PSS-SHA256 signature)
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import subprocess
import sys
from datetime import date
from pathlib import Path


# 打包进 exe 的公钥（用 openssl 生成，私钥在 ~/.dedupe_pic_keys/private.pem，不进 repo）
_PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAz4Sdep4sXaVeAjy3yBgO
QQcR4ZW2PKwsuDuOsyJTjjyliVnWGLY4novwZhR1Qy8Ut/FUDNF9DwvWxMCzuybw
SoBdgQNjZz0qPw0uHOID29aD35laqsQn83BONH/roH4jTWtJNS8Wk4hXWbXRHkdf
9TIPhU7dkNIjeczRsMqdvwXw74yBXH4csY0Fl4yBkTy3574pvlUqf/HJR/LgC4eN
wfKWe2sD+8p22wmULflkyGzSp1teYyhONiqtvyZAOB1nghJLVu8qnmTmtl7GVEA+
YpZE7tLEWobtmRRkIPW70bx431CdhRzirC3iaHnRWx8GR9mRb1/KJ3O9c7DMCxiJ
rQIDAQAB
-----END PUBLIC KEY-----
"""


# --------------------------- 机器指纹 -------------------------------------

def _run(cmd: list[str], timeout: int = 5) -> str:
    """跑一条命令，屏蔽 stderr，超时返回空串。"""
    try:
        out = subprocess.check_output(
            cmd,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            creationflags=(
                0x08000000 if sys.platform == "win32" else 0
            ),  # CREATE_NO_WINDOW，避免弹黑框
        )
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _pick_last_nonempty_line(output: str) -> str:
    """wmic 的输出是标题行 + 数据行；取最后一行非空。"""
    lines = [l.strip() for l in output.splitlines() if l.strip()]
    if len(lines) >= 2:
        # 跳过第 1 行标题
        return lines[-1]
    return ""


def _motherboard_serial() -> str:
    """Win: wmic baseboard get serialnumber → 降级 powershell → UNKNOWN"""
    if sys.platform != "win32":
        # 非 Windows 环境，本地测试用
        return _run(["uname", "-n"]) or "NON-WIN"

    out = _run(["wmic", "baseboard", "get", "serialnumber"])
    val = _pick_last_nonempty_line(out)
    if val and val.lower() != "serialnumber":
        return val

    # 降级：PowerShell（Win11 新版本 wmic 可能被移除）
    out = _run([
        "powershell", "-NoProfile", "-Command",
        "(Get-CimInstance Win32_BaseBoard).SerialNumber",
    ])
    return out.strip() or "UNKNOWN-BOARD"


def _disk_serial() -> str:
    """Win: wmic diskdrive get serialnumber → 降级 powershell → UNKNOWN"""
    if sys.platform != "win32":
        return "NON-WIN"

    out = _run(["wmic", "diskdrive", "get", "serialnumber"])
    val = _pick_last_nonempty_line(out)
    if val and val.lower() != "serialnumber":
        return val

    out = _run([
        "powershell", "-NoProfile", "-Command",
        "(Get-CimInstance Win32_DiskDrive | Select-Object -First 1).SerialNumber",
    ])
    return out.strip() or "UNKNOWN-DISK"


def get_fingerprint() -> str:
    """返回本机指纹，形如 A1B2-C3D4-E5F6-7890。"""
    board = _motherboard_serial()
    disk = _disk_serial()
    host = socket.gethostname() or "UNKNOWN-HOST"
    raw = f"{board}|{disk}|{host}"
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16].upper()
    return "-".join(h[i:i + 4] for i in range(0, 16, 4))


def get_fingerprint_debug() -> dict:
    """调试用：返回指纹和原始三段（不含 SHA256 前的完整原文，只保护公钥）。"""
    board = _motherboard_serial()
    disk = _disk_serial()
    host = socket.gethostname() or "UNKNOWN-HOST"
    raw = f"{board}|{disk}|{host}"
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16].upper()
    return {
        "board_serial": board,
        "disk_serial": disk,
        "hostname": host,
        "fingerprint": "-".join(h[i:i + 4] for i in range(0, 16, 4)),
    }


# --------------------------- License 验签 ---------------------------------

class LicenseError(Exception):
    """license 相关错误"""


def _load_payload_and_sig(license_path: Path) -> tuple[bytes, bytes]:
    if not license_path.is_file():
        raise LicenseError("license.lic 不存在")
    try:
        lines = [
            l.strip() for l in license_path.read_text().splitlines() if l.strip()
        ]
        if len(lines) < 2:
            raise LicenseError("license.lic 内容不完整（需要 2 行）")
        payload = base64.b64decode(lines[0], validate=True)
        sig = base64.b64decode(lines[1], validate=True)
        return payload, sig
    except LicenseError:
        raise
    except Exception as e:
        raise LicenseError(f"license.lic 格式错误: {e}") from e


def verify_license(license_path: Path) -> tuple[bool, str]:
    """返回 (是否有效, 说明文字)"""
    try:
        payload, sig = _load_payload_and_sig(license_path)
    except LicenseError as e:
        return False, str(e)

    # RSA-PSS 验签
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        return False, (
            "缺少 cryptography 库。这通常意味着 exe 打包异常，请重新下载。"
        )

    try:
        pub = serialization.load_pem_public_key(_PUBLIC_KEY_PEM)
        pub.verify(
            sig,
            payload,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
    except Exception:
        return False, "签名校验失败（license 被篡改，或不是官方签发）"

    try:
        data = json.loads(payload.decode("utf-8"))
    except Exception as e:
        return False, f"license payload 不是合法 JSON: {e}"

    expected_fp = data.get("fingerprint", "")
    actual_fp = get_fingerprint()
    if expected_fp != actual_fp:
        return False, (
            f"授权与本机不匹配。\n"
            f"          本机指纹: {actual_fp}\n"
            f"          授权指纹: {expected_fp}"
        )

    # 到期检查（永久授权时 expire_date == "never"）
    expire = str(data.get("expire_date", "never")).strip().lower()
    if expire and expire != "never":
        if date.today().isoformat() > expire:
            return False, f"授权已于 {expire} 到期"

    issued_to = data.get("issued_to", "unknown")
    note = data.get("note", "")
    msg = f"授权有效（发放给: {issued_to}"
    if note:
        msg += f"; {note}"
    msg += ")"
    return True, msg


# --------------------------- CLI（供 debug）------------------------------

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="License / fingerprint 工具")
    ap.add_argument("--verify", type=Path, help="校验一个 license.lic")
    ap.add_argument("--debug", action="store_true", help="打印指纹的构成明细")
    args = ap.parse_args()

    if args.debug:
        info = get_fingerprint_debug()
        for k, v in info.items():
            print(f"{k}: {v}")
    else:
        print(f"fingerprint: {get_fingerprint()}")

    if args.verify:
        ok, msg = verify_license(args.verify)
        print(f"verify: {'PASS' if ok else 'FAIL'} - {msg}")
        sys.exit(0 if ok else 1)
