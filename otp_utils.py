# -*- coding: utf-8 -*-
"""
otp_utils.py —— 手写 TOTP（RFC 6238）实现，零第三方依赖。

用途：
- 生成 base32 密钥（每台机器一份，长期保管）
- 由密钥 + 当前时间戳算 6 位动态口令
- 校验用户输入的 6 位码（默认允许 ±3 个 30 秒窗口 = ±90 秒时钟漂移）
- 生成 otpauth:// URI，用于二维码 / Authenticator 导入

不引 pyotp / cryptography，只用 Python 标准库：secrets / hmac / hashlib / base64 /
time / struct。PyInstaller 打包后体积不增。

与 Google Authenticator / 微软 Authenticator / 1Password / Bitwarden 完全兼容。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote


# ---------- 常量 ----------

DEFAULT_DIGITS = 6         # 6 位数字（Google Authenticator 兼容）
DEFAULT_PERIOD = 30        # 每 30 秒变一次（RFC 6238 推荐）
DEFAULT_ALGO = "SHA1"      # HMAC-SHA1（RFC 6238 默认）
DEFAULT_ISSUER = "pic-clear"


# ---------- 密钥 ----------

def generate_secret(nbytes: int = 20) -> str:
    """生成一个新的 TOTP base32 密钥。

    参数：
        nbytes: 原始随机字节数，20 是 RFC 4226 推荐值（160 bit，与 SHA1 匹配）

    返回：
        base32 字符串（不带 =），可直接给 Authenticator 扫码或手工输入。
        长度 = ceil(20 * 8 / 5) = 32 个字符
    """
    raw = secrets.token_bytes(nbytes)
    b32 = base64.b32encode(raw).decode("ascii").rstrip("=")
    return b32


def normalize_secret(secret: str) -> bytes:
    """把用户输入的 base32（可能带空格、小写、缺 = 填充）转成原始字节。"""
    s = "".join(secret.split()).upper().replace("-", "")
    # base32 需要 8 字符对齐，补 =
    pad = (-len(s)) % 8
    s = s + ("=" * pad)
    return base64.b32decode(s, casefold=True)


# ---------- 生成 6 位码 ----------

def totp_at(secret: str, at_ts: float | None = None,
            digits: int = DEFAULT_DIGITS,
            period: int = DEFAULT_PERIOD,
            algo: str = DEFAULT_ALGO) -> str:
    """算出某个时刻的 TOTP 码。

    参数：
        secret: base32 字符串
        at_ts:  Unix 时间戳（秒）。None = 当前时间
        digits: 位数（一般 6）
        period: 步长秒（一般 30）
        algo:   SHA1 / SHA256 / SHA512
    返回：
        n 位数字字符串（前导 0 保留）
    """
    if at_ts is None:
        at_ts = time.time()
    counter = int(at_ts) // period
    return _hotp(normalize_secret(secret), counter, digits=digits, algo=algo)


def now_code(secret: str) -> str:
    """便捷函数：当前时刻的 6 位码。"""
    return totp_at(secret)


def seconds_to_next(period: int = DEFAULT_PERIOD,
                    at_ts: float | None = None) -> int:
    """当前码还剩多少秒过期。"""
    if at_ts is None:
        at_ts = time.time()
    return period - int(at_ts) % period


# ---------- 校验 ----------

def verify(secret: str, code: str,
           at_ts: float | None = None,
           window: int = 3,
           digits: int = DEFAULT_DIGITS,
           period: int = DEFAULT_PERIOD,
           algo: str = DEFAULT_ALGO) -> bool:
    """校验用户输入的 code 是否有效。

    参数：
        window: 允许的时钟漂移窗口数（左右各取几个 period 都算通过）
                默认 3 ≈ ±90 秒，堡垒机 / 手机时钟差得挺离谱也能过
    """
    if not code:
        return False
    code = code.strip().replace(" ", "").replace("-", "")
    if not code.isdigit() or len(code) != digits:
        return False
    if at_ts is None:
        at_ts = time.time()
    key = normalize_secret(secret)
    base_counter = int(at_ts) // period
    for offset in range(-window, window + 1):
        cand = _hotp(key, base_counter + offset, digits=digits, algo=algo)
        # 常数时间比较，防侧信道
        if hmac.compare_digest(cand, code):
            return True
    return False


# ---------- otpauth:// URI（给二维码用）----------

def build_otpauth_uri(secret: str, label: str,
                      issuer: str = DEFAULT_ISSUER,
                      digits: int = DEFAULT_DIGITS,
                      period: int = DEFAULT_PERIOD,
                      algo: str = DEFAULT_ALGO) -> str:
    """构造 otpauth://totp/... URI，Google/微软 Authenticator 扫码即可导入。

    label 是二维码里显示的账户名，一般用机器指纹或者用户名。
    """
    label_enc = quote(f"{issuer}:{label}", safe="")
    params = (
        f"secret={secret}"
        f"&issuer={quote(issuer)}"
        f"&algorithm={algo}"
        f"&digits={digits}"
        f"&period={period}"
    )
    return f"otpauth://totp/{label_enc}?{params}"


# ---------- 内部：HOTP（RFC 4226）----------

def _hotp(key: bytes, counter: int, digits: int, algo: str) -> str:
    """HOTP 算法本身，TOTP = HOTP(secret, time // period)。"""
    algo_up = algo.upper()
    if algo_up == "SHA1":
        h_func = hashlib.sha1
    elif algo_up == "SHA256":
        h_func = hashlib.sha256
    elif algo_up == "SHA512":
        h_func = hashlib.sha512
    else:
        raise ValueError(f"unsupported algo: {algo}")

    msg = struct.pack(">Q", counter)  # 8 字节大端计数器
    digest = hmac.new(key, msg, h_func).digest()

    # 动态截断（RFC 4226 §5.3）
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF

    mod = 10 ** digits
    code = truncated % mod
    return str(code).zfill(digits)


# ---------- 简单自测 ----------

if __name__ == "__main__":
    # RFC 6238 附录 B 参考向量（SHA1）
    # 密钥 "12345678901234567890" -> base32 "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    sec = base64.b32encode(b"12345678901234567890").decode()
    # T = 59  → counter = 1 → 94287082
    assert totp_at(sec, at_ts=59, digits=8) == "94287082", "RFC6238 vector T=59 fail"
    # T = 1111111109 → counter = 37037036 → 07081804
    assert totp_at(sec, at_ts=1111111109, digits=8) == "07081804"
    # T = 1234567890 → counter = 41152263 → 89005924
    assert totp_at(sec, at_ts=1234567890, digits=8) == "89005924"
    print("[OK] RFC 6238 SHA1 vectors passed.")

    # verify 双向
    s = generate_secret()
    c = now_code(s)
    assert verify(s, c)
    assert not verify(s, "000000")
    print(f"[OK] generated secret: {s}")
    print(f"[OK] current 6-digit: {c}")
    print(f"[OK] seconds to next: {seconds_to_next()}s")
    print(f"[OK] otpauth URI: {build_otpauth_uri(s, 'E062-9731-46AC-1C0D')}")
