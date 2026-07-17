# -*- coding: utf-8 -*-
"""
license_db.py —— license 签发历史的数据访问层（MySQL）

设计原则：
- 依赖 PyMySQL（纯 Python，零编译），失败时上层调用要能容忍（DB 挂了也不能
  卡住 license 生成/下载）。
- 全部参数化查询，防 SQL 注入。
- 用短连接：签发是稀疏事件，不上连接池。

环境变量：
    PIC_CLEAR_DB_HOST     默认 127.0.0.1
    PIC_CLEAR_DB_PORT     默认 3306
    PIC_CLEAR_DB_USER     默认 root
    PIC_CLEAR_DB_PASSWORD 默认 空
    PIC_CLEAR_DB_NAME     默认 pic_clear
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any, Optional


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v is not None and v != "" else default


def db_config() -> dict:
    return {
        "host": _env("PIC_CLEAR_DB_HOST", "127.0.0.1"),
        "port": int(_env("PIC_CLEAR_DB_PORT", "3306")),
        "user": _env("PIC_CLEAR_DB_USER", "root"),
        "password": _env("PIC_CLEAR_DB_PASSWORD", ""),
        "database": _env("PIC_CLEAR_DB_NAME", "pic_clear"),
        "charset": "utf8mb4",
    }


class DBUnavailable(Exception):
    """DB 连不上 / 未装驱动。上层用来决定"降级但继续"。"""


def _connect():
    try:
        import pymysql  # type: ignore
    except ImportError as e:
        raise DBUnavailable("未安装 PyMySQL，请: pip install pymysql") from e
    cfg = db_config()
    try:
        return pymysql.connect(autocommit=True, **cfg)
    except Exception as e:
        raise DBUnavailable(f"连接 MySQL 失败: {e}") from e


def ping() -> tuple[bool, str]:
    """给页面 / 健康检查用：返回 (ok, msg)。永不抛异常。"""
    try:
        conn = _connect()
    except DBUnavailable as e:
        return False, str(e)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True, "ok"
    except Exception as e:
        return False, f"查询失败: {e}"
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------- 写 ----------

def insert_issue(
    *,
    fingerprint: str,
    issued_to: str,
    expire_date: str,
    note: str,
    issued_at: str,
    source: str = "web",
    operator: str = "",
    client_ip: str = "",
    payload_b64: str,
    signature_b64: str,
) -> int:
    """写一条签发记录，返回自增 id。抛 DBUnavailable / pymysql 原生异常。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO license_issues
                    (fingerprint, issued_to, expire_date, note, issued_at,
                     source, operator, client_ip,
                     payload_b64, signature_b64)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (fingerprint, issued_to, expire_date, note, issued_at,
                 source, operator, client_ip,
                 payload_b64, signature_b64),
            )
            return int(cur.lastrowid)
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------- 读 ----------

def _row_to_summary(row: tuple) -> dict:
    """列表用摘要：不含 payload / signature（避免响应体过大）。"""
    (id_, fp, issued_to, expire_date, note, issued_at, created_at,
     source, operator, client_ip) = row
    return {
        "id": id_,
        "fingerprint": fp,
        "issued_to": issued_to,
        "expire_date": expire_date,
        "note": note,
        "issued_at": _fmt(issued_at),
        "created_at": _fmt(created_at),
        "source": source,
        "operator": operator,
        "client_ip": client_ip,
    }


def _fmt(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, (datetime,)):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, (date,)):
        return v.strftime("%Y-%m-%d")
    return str(v)


def list_issues(limit: int = 100, keyword: str = "") -> list[dict]:
    """按 created_at 倒序返回摘要。keyword 会模糊匹配 fingerprint / issued_to / note。"""
    limit = max(1, min(int(limit), 500))
    conn = _connect()
    try:
        with conn.cursor() as cur:
            base_cols = (
                "id, fingerprint, issued_to, expire_date, note, "
                "issued_at, created_at, source, operator, client_ip"
            )
            if keyword:
                like = f"%{keyword}%"
                cur.execute(
                    f"SELECT {base_cols} FROM license_issues "
                    f"WHERE fingerprint LIKE %s OR issued_to LIKE %s OR note LIKE %s "
                    f"ORDER BY id DESC LIMIT %s",
                    (like, like, like, limit),
                )
            else:
                cur.execute(
                    f"SELECT {base_cols} FROM license_issues "
                    f"ORDER BY id DESC LIMIT %s",
                    (limit,),
                )
            return [_row_to_summary(r) for r in cur.fetchall()]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_issue(issue_id: int) -> Optional[dict]:
    """按 id 取一条完整记录（含 payload_b64 / signature_b64）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, fingerprint, issued_to, expire_date, note,
                       issued_at, created_at, source, operator, client_ip,
                       payload_b64, signature_b64
                FROM license_issues WHERE id = %s
                """,
                (int(issue_id),),
            )
            row = cur.fetchone()
            if not row:
                return None
            (id_, fp, issued_to, expire_date, note, issued_at, created_at,
             source, operator, client_ip, payload_b64, signature_b64) = row
            return {
                "id": id_,
                "fingerprint": fp,
                "issued_to": issued_to,
                "expire_date": expire_date,
                "note": note,
                "issued_at": _fmt(issued_at),
                "created_at": _fmt(created_at),
                "source": source,
                "operator": operator,
                "client_ip": client_ip,
                "payload_b64": payload_b64,
                "signature_b64": signature_b64,
            }
    finally:
        try:
            conn.close()
        except Exception:
            pass


def build_lic_text(record: dict) -> str:
    """按 license.lic 格式拼两行 base64（尾部 \n）。"""
    return f"{record['payload_b64']}\n{record['signature_b64']}\n"
