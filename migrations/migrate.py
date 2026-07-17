#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
migrations/migrate.py —— pic-clear 数据库迁移器

用法：
    python migrations/migrate.py                # 跑所有未执行的迁移
    python migrations/migrate.py --status       # 只看状态，不执行
    python migrations/migrate.py --db pic_clear # 覆盖库名（默认从 env / pic_clear）

连接参数按优先级：命令行 > 环境变量 > 默认值
    --host / PIC_CLEAR_DB_HOST      默认 127.0.0.1
    --port / PIC_CLEAR_DB_PORT      默认 3306
    --user / PIC_CLEAR_DB_USER      默认 root
    --password / PIC_CLEAR_DB_PASSWORD  默认 空
    --db   / PIC_CLEAR_DB_NAME      默认 pic_clear

驱动：优先用 PyMySQL；没装就 fallback 到系统 `mysql` CLI。

行为：
    1. 建库（IF NOT EXISTS）
    2. 建 schema_migrations 表（IF NOT EXISTS）
    3. 扫 migrations/*.sql，按文件名字典序依次执行
       - 已在 schema_migrations 里 → 跳过
       - 未执行 → 逐条 statement 执行，成功后写入 schema_migrations
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent


def env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v is not None and v != "" else default


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="pic-clear MySQL 迁移器")
    ap.add_argument("--host", default=env("PIC_CLEAR_DB_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int,
                    default=int(env("PIC_CLEAR_DB_PORT", "3306")))
    ap.add_argument("--user", default=env("PIC_CLEAR_DB_USER", "root"))
    ap.add_argument("--password", default=env("PIC_CLEAR_DB_PASSWORD", ""))
    ap.add_argument("--db", default=env("PIC_CLEAR_DB_NAME", "pic_clear"))
    ap.add_argument("--status", action="store_true",
                    help="只打印状态，不执行")
    return ap.parse_args()


# ---------- SQL 拆分：按 ; 分句，但忽略字符串/注释中的 ; ----------

_SPLIT_RE = re.compile(
    r"""
    '(?:\\.|[^'\\])*'      # 单引号字符串
  | "(?:\\.|[^"\\])*"      # 双引号字符串
  | `(?:[^`])*`            # 反引号标识符
  | /\*.*?\*/              # 块注释
  | --[^\n]*               # 行注释 --
  | \#[^\n]*               # 行注释 #
  | ;                      # 分号
    """,
    re.VERBOSE | re.DOTALL,
)


def split_statements(sql: str) -> list[str]:
    """把一段 SQL 拆成多条 statement，忽略字符串/注释里的分号。"""
    out, last = [], 0
    for m in _SPLIT_RE.finditer(sql):
        if m.group(0) == ";":
            stmt = sql[last:m.start()].strip()
            if stmt:
                out.append(stmt)
            last = m.end()
    tail = sql[last:].strip()
    if tail:
        out.append(tail)
    return out


# ---------- 驱动层：PyMySQL 或 CLI ----------

class DB:
    def execute(self, sql: str, params: tuple = ()) -> None: ...
    def fetchall(self, sql: str, params: tuple = ()) -> list[tuple]: ...
    def close(self) -> None: ...


class PyMySQLDB(DB):
    def __init__(self, host, port, user, password, database=None):
        import pymysql  # type: ignore
        self.pymysql = pymysql
        self.conn = pymysql.connect(
            host=host, port=port, user=user, password=password,
            database=database, charset="utf8mb4",
            autocommit=True,
        )

    def execute(self, sql, params=()):
        with self.conn.cursor() as cur:
            cur.execute(sql, params)

    def fetchall(self, sql, params=()):
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


class MysqlCliDB(DB):
    """fallback：调 mysql 命令行。只用来跑 DDL，够用。"""

    def __init__(self, host, port, user, password, database=None):
        self.host, self.port = host, port
        self.user, self.password = user, password
        self.database = database

    def _base_cmd(self, batch=False):
        cmd = ["mysql", "-h", self.host, "-P", str(self.port),
               "-u", self.user, "--default-character-set=utf8mb4"]
        if self.password:
            cmd.append(f"-p{self.password}")
        if self.database:
            cmd.append(self.database)
        if batch:
            cmd.extend(["-N", "-B"])  # 无表头 tab 分隔
        return cmd

    def execute(self, sql, params=()):
        if params:
            raise NotImplementedError("MysqlCliDB 不支持参数化，请装 PyMySQL")
        subprocess.run(self._base_cmd(), input=sql.encode("utf-8"),
                       check=True)

    def fetchall(self, sql, params=()):
        if params:
            raise NotImplementedError("MysqlCliDB 不支持参数化")
        r = subprocess.run(self._base_cmd(batch=True), input=sql.encode("utf-8"),
                           capture_output=True, check=True)
        rows = []
        for line in r.stdout.decode("utf-8").splitlines():
            if not line:
                continue
            rows.append(tuple(line.split("\t")))
        return rows

    def close(self):
        pass


def open_db(host, port, user, password, database=None) -> DB:
    try:
        return PyMySQLDB(host, port, user, password, database)
    except ImportError:
        print("[warn] 没装 PyMySQL，fallback 到 mysql CLI（只能跑 DDL）",
              file=sys.stderr)
        return MysqlCliDB(host, port, user, password, database)


# ---------- 主流程 ----------

def ensure_database(args) -> None:
    """先连不带 database，把库建出来。"""
    db = open_db(args.host, args.port, args.user, args.password, None)
    try:
        db.execute(
            f"CREATE DATABASE IF NOT EXISTS `{args.db}` "
            f"DEFAULT CHARSET utf8mb4 DEFAULT COLLATE utf8mb4_unicode_ci"
        )
    finally:
        db.close()


SCHEMA_MIG_DDL = """
CREATE TABLE IF NOT EXISTS `schema_migrations` (
  `filename`   VARCHAR(255) NOT NULL,
  `applied_at` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`filename`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
""".strip()


def main() -> int:
    args = parse_args()

    ensure_database(args)

    db = open_db(args.host, args.port, args.user, args.password, args.db)
    try:
        db.execute(SCHEMA_MIG_DDL)

        rows = db.fetchall("SELECT filename FROM schema_migrations")
        applied = {r[0] for r in rows}

        files = sorted(p for p in MIGRATIONS_DIR.glob("*.sql"))
        if not files:
            print("[warn] migrations/ 目录下没有 .sql")
            return 0

        print(f"数据库    : {args.user}@{args.host}:{args.port}/{args.db}")
        print(f"已执行    : {len(applied)} / 总 {len(files)}")

        pending = [f for f in files if f.name not in applied]
        if args.status:
            for f in files:
                mark = "✓" if f.name in applied else " "
                print(f"  [{mark}] {f.name}")
            return 0

        if not pending:
            print("[ok] 无待执行迁移")
            return 0

        for f in pending:
            print(f"[run] {f.name}")
            sql = f.read_text(encoding="utf-8")
            # 简单处理：直接批量丢给驱动执行
            for stmt in split_statements(sql):
                db.execute(stmt)
            db.execute(
                "INSERT INTO schema_migrations(filename) VALUES(%s)"
                if isinstance(db, PyMySQLDB)
                else f"INSERT INTO schema_migrations(filename) VALUES('{f.name}')",
                (f.name,) if isinstance(db, PyMySQLDB) else (),
            )
            print(f"[ok]  {f.name}")
        print("[done] 所有迁移已执行")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
