# -*- coding: utf-8 -*-
"""pic-clear 统计入库工具（SQLite 单机本地）.

设计要点（详见 docs/stats_db.md）:
  - 每台机器写自己本地 ~/.pic-clear/stats_<hostname>.db，不共享 Z 盘
  - 三张表:
      extract_stats    抽帧, 一条 = 一个视频
      dedupe_stats     去重, 一条 = 一个目录
      classify_stats   分类, 一条 = 一个 camera 目录
  - 并发四层防御:
      1) PRAGMA busy_timeout=5000
      2) PRAGMA journal_mode=WAL
      3) 短连接: 每次 open -> insert -> close
      4) 外层 retry 5 次, 只对 "database is locked" 重试
  - 静默失败: 主流程 (抽帧/去重/分类) 绝不能因为落库失败中断
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1

_HOST_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def _safe_hostname() -> str:
    try:
        host = socket.gethostname() or "unknown"
    except Exception:
        host = "unknown"
    return _HOST_SAFE_RE.sub("_", host)[:64] or "unknown"


def default_db_path() -> Path:
    """默认 db 路径: ~/.pic-clear/stats_<hostname>.db"""
    root = Path.home() / ".pic-clear"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return root / f"stats_{_safe_hostname()}.db"


# ---------------------------------------------------------------- 建表

_DDL_EXTRACT = """
CREATE TABLE IF NOT EXISTS extract_stats (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,        -- 记录时间 YYYY-MM-DD HH:MM:SS
    host          TEXT    NOT NULL,        -- 机器名
    task_id       TEXT,                    -- 一次 GUI 任务的 uuid, 便于聚合
    version       TEXT,                    -- 程序版本 (extract_frames --version)
    src_root      TEXT,                    -- 视频源根
    dst_root      TEXT,                    -- 抽帧输出根
    video_path    TEXT    NOT NULL,        -- 视频绝对路径
    output_dir    TEXT,                    -- 抽帧输出目录 (视频同名文件夹)
    rel_path      TEXT,                    -- 相对 src_root 的路径, 打印用
    frames        INTEGER DEFAULT 0,       -- 抽出的帧数
    duration_sec  REAL,                    -- 视频时长 (秒, 未知则 NULL)
    fps           REAL,                    -- 抽帧 fps
    quality       INTEGER,                 -- JPEG 质量
    naming_style  TEXT,                    -- 命名规则 legacy/parent/custom
    seq_digits    INTEGER,                 -- 序号补零位数
    elapsed_sec   REAL,                    -- 本视频耗时 (秒)
    stage         TEXT,                    -- ok / empty / failed / locked
    exit_code     INTEGER,                 -- 兼容字段, 0 表 ok, 非 0 失败
    msg           TEXT                     -- 备注 (ffmpeg 报错等)
);
CREATE INDEX IF NOT EXISTS idx_extract_ts       ON extract_stats(ts);
CREATE INDEX IF NOT EXISTS idx_extract_task     ON extract_stats(task_id);
CREATE INDEX IF NOT EXISTS idx_extract_video    ON extract_stats(video_path);
"""

_DDL_DEDUPE = """
CREATE TABLE IF NOT EXISTS dedupe_stats (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT    NOT NULL,       -- 记录时间
    host           TEXT    NOT NULL,       -- 机器名
    task_id        TEXT,                   -- 任务 uuid
    version        TEXT,                   -- 程序版本
    source_root    TEXT,                   -- 图片源根 (GUI 传入)
    dir_path       TEXT    NOT NULL,       -- 本次去重的目录
    total          INTEGER DEFAULT 0,      -- 目录内图片总数
    deleted        INTEGER DEFAULT 0,      -- 实际删除张数
    remain         INTEGER DEFAULT 0,      -- 保留张数 (total - deleted)
    freed_bytes    INTEGER DEFAULT 0,      -- 释放空间字节
    threshold      INTEGER,                -- 相似阈值 (Hamming)
    protect_count  INTEGER DEFAULT 0,      -- 目标检测保护数
    scene_count    INTEGER DEFAULT 0,      -- 场景保护数
    apply          INTEGER DEFAULT 0,      -- 1=真删, 0=dry-run
    report_csv     TEXT,                   -- dedupe_report.csv 路径
    elapsed_sec    REAL,                   -- 本目录耗时
    exit_code      INTEGER,                -- 0=ok
    msg            TEXT
);
CREATE INDEX IF NOT EXISTS idx_dedupe_ts    ON dedupe_stats(ts);
CREATE INDEX IF NOT EXISTS idx_dedupe_task  ON dedupe_stats(task_id);
CREATE INDEX IF NOT EXISTS idx_dedupe_dir   ON dedupe_stats(dir_path);
"""

_DDL_CLASSIFY = """
CREATE TABLE IF NOT EXISTS classify_stats (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,       -- 记录时间
    host          TEXT    NOT NULL,       -- 机器名
    task_id       TEXT,                   -- 任务 uuid
    version       TEXT,                   -- 程序版本
    in_root       TEXT,                   -- 分类输入根
    out_root      TEXT,                   -- 分类输出根
    camera_dir    TEXT    NOT NULL,       -- 本次处理的 camera 目录
    scanned       INTEGER DEFAULT 0,      -- 扫描图片数
    copied_bucket INTEGER DEFAULT 0,      -- 复制到桶的图片计数 (可能 > scanned, 一图多桶)
    bucket_json   TEXT,                   -- 各桶命中数 JSON, 例:
                                          --   {"活体":10,"关节":3,"前备箱":2,"前机盖":0,"手势":5,"遮挡":0}
    errors        INTEGER DEFAULT 0,      -- 出错张数
    elapsed_sec   REAL,                   -- 本 camera 耗时
    exit_code     INTEGER,                -- 0=ok
    msg           TEXT
);
CREATE INDEX IF NOT EXISTS idx_classify_ts     ON classify_stats(ts);
CREATE INDEX IF NOT EXISTS idx_classify_task   ON classify_stats(task_id);
CREATE INDEX IF NOT EXISTS idx_classify_camera ON classify_stats(camera_dir);
"""

_DDL_TASK_RUNS = """
CREATE TABLE IF NOT EXISTS task_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,       -- 记录时间
    host         TEXT    NOT NULL,       -- 机器名
    task_id      TEXT    NOT NULL,       -- 任务 uuid (16 位十六进制)
    task_type    TEXT    NOT NULL,       -- extract / dedupe / classify
    version      TEXT,                   -- GUI 版本号
    cmdline      TEXT,                   -- 完整命令行 (若有)
    config_json  TEXT                    -- GUI 侧收集的完整配置 JSON
);
CREATE INDEX IF NOT EXISTS idx_task_runs_ts       ON task_runs(ts);
CREATE INDEX IF NOT EXISTS idx_task_runs_task_id  ON task_runs(task_id);
CREATE INDEX IF NOT EXISTS idx_task_runs_type     ON task_runs(task_type);
"""

_DDL_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    ts      TEXT NOT NULL
);
"""


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL_SCHEMA)
    conn.executescript(_DDL_EXTRACT)
    conn.executescript(_DDL_DEDUPE)
    conn.executescript(_DDL_CLASSIFY)
    conn.executescript(_DDL_TASK_RUNS)
    cur = conn.execute("SELECT COUNT(1) FROM schema_version")
    if cur.fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO schema_version(version, ts) VALUES (?, ?)",
            (SCHEMA_VERSION, _now_str()),
        )


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _open(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    # 关键 PRAGMA: WAL + busy_timeout, 允许并发读写
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    return conn


def _insert_with_retry(
    db_path: Path, sql: str, params: tuple,
) -> None:
    """带重试的短连接 INSERT. 只对 'database is locked' 重试, 最多 5 次."""
    last_exc: Exception | None = None
    for i in range(5):
        try:
            conn = _open(db_path)
            try:
                _init_db(conn)
                conn.execute(sql, params)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            return
        except sqlite3.OperationalError as e:
            last_exc = e
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                time.sleep(0.1 * (i + 1))
                continue
            raise
    if last_exc is not None:
        raise last_exc


# ---------------------------------------------------------------- API

def open_stats_db(path: str | os.PathLike | None = None) -> Path:
    """确保 db 已建表, 返回其绝对路径. 出错静默返回默认路径."""
    db_path = Path(path) if path else default_db_path()
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _open(db_path)
        try:
            _init_db(conn)
        finally:
            conn.close()
    except Exception as e:
        print(f"[stats_db] WARN open 失败: {type(e).__name__}: {e}",
              file=sys.stderr)
    return db_path


def _log_silent(prefix: str, e: Exception) -> None:
    print(f"[stats_db] WARN {prefix} 失败: {type(e).__name__}: {e}",
          file=sys.stderr)


def record_extract(
    *,
    video_path: str,
    output_dir: str | None = None,
    frames: int = 0,
    duration_sec: float | None = None,
    fps: float | None = None,
    quality: int | None = None,
    naming_style: str | None = None,
    seq_digits: int | None = None,
    elapsed_sec: float | None = None,
    stage: str = "ok",
    exit_code: int = 0,
    msg: str = "",
    src_root: str | None = None,
    dst_root: str | None = None,
    rel_path: str | None = None,
    task_id: str | None = None,
    version: str | None = None,
    db_path: str | os.PathLike | None = None,
) -> None:
    """记录一次抽帧结果 (一个视频一条). 静默失败."""
    try:
        target = Path(db_path) if db_path else default_db_path()
        _insert_with_retry(
            target,
            """
            INSERT INTO extract_stats(
                ts, host, task_id, version, src_root, dst_root,
                video_path, output_dir, rel_path, frames,
                duration_sec, fps, quality, naming_style, seq_digits,
                elapsed_sec, stage, exit_code, msg
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now_str(), _safe_hostname(), task_id, version,
                src_root, dst_root, video_path, output_dir, rel_path,
                int(frames or 0),
                duration_sec, fps, quality, naming_style, seq_digits,
                elapsed_sec, stage, int(exit_code or 0), msg,
            ),
        )
    except Exception as e:
        _log_silent("record_extract", e)


def record_dedupe(
    *,
    dir_path: str,
    total: int = 0,
    deleted: int = 0,
    remain: int | None = None,
    freed_bytes: int = 0,
    threshold: int | None = None,
    protect_count: int = 0,
    scene_count: int = 0,
    apply: bool = False,
    report_csv: str | None = None,
    elapsed_sec: float | None = None,
    exit_code: int = 0,
    msg: str = "",
    source_root: str | None = None,
    task_id: str | None = None,
    version: str | None = None,
    db_path: str | os.PathLike | None = None,
) -> None:
    """记录一次去重结果 (一个目录一条). 静默失败."""
    try:
        if remain is None:
            remain = max(0, int(total or 0) - int(deleted or 0))
        target = Path(db_path) if db_path else default_db_path()
        _insert_with_retry(
            target,
            """
            INSERT INTO dedupe_stats(
                ts, host, task_id, version, source_root,
                dir_path, total, deleted, remain, freed_bytes,
                threshold, protect_count, scene_count, apply,
                report_csv, elapsed_sec, exit_code, msg
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now_str(), _safe_hostname(), task_id, version, source_root,
                dir_path, int(total or 0), int(deleted or 0), int(remain or 0),
                int(freed_bytes or 0),
                threshold, int(protect_count or 0), int(scene_count or 0),
                1 if apply else 0,
                report_csv, elapsed_sec, int(exit_code or 0), msg,
            ),
        )
    except Exception as e:
        _log_silent("record_dedupe", e)


def record_classify(
    *,
    camera_dir: str,
    scanned: int = 0,
    copied_bucket: int = 0,
    bucket_counts: dict[str, int] | None = None,
    errors: int = 0,
    elapsed_sec: float | None = None,
    exit_code: int = 0,
    msg: str = "",
    in_root: str | None = None,
    out_root: str | None = None,
    task_id: str | None = None,
    version: str | None = None,
    db_path: str | os.PathLike | None = None,
) -> None:
    """记录一次分类结果 (一个 camera 目录一条). 静默失败."""
    try:
        bucket_json = json.dumps(bucket_counts or {}, ensure_ascii=False)
        target = Path(db_path) if db_path else default_db_path()
        _insert_with_retry(
            target,
            """
            INSERT INTO classify_stats(
                ts, host, task_id, version, in_root, out_root,
                camera_dir, scanned, copied_bucket, bucket_json,
                errors, elapsed_sec, exit_code, msg
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now_str(), _safe_hostname(), task_id, version,
                in_root, out_root, camera_dir,
                int(scanned or 0), int(copied_bucket or 0), bucket_json,
                int(errors or 0), elapsed_sec, int(exit_code or 0), msg,
            ),
        )
    except Exception as e:
        _log_silent("record_classify", e)


def record_task_run(
    *,
    task_id: str,
    task_type: str,
    config: dict | None = None,
    cmdline: str | None = None,
    version: str | None = None,
    db_path: str | os.PathLike | None = None,
) -> None:
    """记录一次 GUI 任务的完整启动配置 (每次点开始 = 一条). 静默失败.

    task_type: 'extract' / 'dedupe' / 'classify'
    config:    GUI 收集的完整配置 dict, 会 json.dumps 存起来
    cmdline:   完整命令行 (可选, 有则存)
    """
    try:
        cfg_json = json.dumps(config or {}, ensure_ascii=False, default=str)
        target = Path(db_path) if db_path else default_db_path()
        _insert_with_retry(
            target,
            """
            INSERT INTO task_runs(
                ts, host, task_id, task_type, version, cmdline, config_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now_str(), _safe_hostname(), task_id, task_type,
                version, cmdline, cfg_json,
            ),
        )
    except Exception as e:
        _log_silent("record_task_run", e)


# ---------------------------------------------------------------- 查询辅助

def iter_db_files(scan_dir: str | os.PathLike | None = None) -> list[Path]:
    """列出目录下所有 stats_*.db (给聚合查看器用). 默认扫 ~/.pic-clear/"""
    root = Path(scan_dir) if scan_dir else (Path.home() / ".pic-clear")
    if not root.is_dir():
        return []
    return sorted(root.glob("stats_*.db"))


def query_all(
    db_files: list[Path],
    table: str,
    where_sql: str = "",
    params: tuple = (),
    order_by: str = "ts DESC",
    limit: int | None = None,
) -> list[dict]:
    """聚合读多个 db 的同一张表. 返回 dict 列表, 每条附带 _db_file 字段."""
    if table not in {"extract_stats", "dedupe_stats", "classify_stats"}:
        raise ValueError(f"invalid table: {table}")
    rows: list[dict] = []
    sql = f"SELECT * FROM {table}"
    if where_sql:
        sql += " WHERE " + where_sql
    if order_by:
        sql += " ORDER BY " + order_by
    if limit:
        sql += f" LIMIT {int(limit)}"
    for f in db_files:
        try:
            conn = _open(f)
            try:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(sql, params)
                for r in cur.fetchall():
                    d = dict(r)
                    d["_db_file"] = str(f)
                    rows.append(d)
            finally:
                conn.close()
        except Exception as e:
            print(f"[stats_db] WARN query {f}: {e}", file=sys.stderr)
    return rows


def query_task_run(
    db_files: list[Path], task_id: str,
) -> dict | None:
    """按 task_id 找一条 task_runs (聚合多机 db), 找不到返回 None."""
    if not task_id:
        return None
    for f in db_files:
        try:
            conn = _open(f)
            try:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    "SELECT * FROM task_runs WHERE task_id = ? "
                    "ORDER BY ts DESC LIMIT 1", (task_id,),
                )
                r = cur.fetchone()
                if r:
                    d = dict(r)
                    d["_db_file"] = str(f)
                    return d
            finally:
                conn.close()
        except Exception as e:
            print(f"[stats_db] WARN query_task_run {f}: {e}",
                  file=sys.stderr)
    return None


if __name__ == "__main__":
    # 简单自测
    p = open_stats_db()
    print(f"db = {p}")
    record_extract(video_path="/tmp/x.mp4", frames=10, stage="ok",
                   elapsed_sec=1.2, fps=1.0)
    record_dedupe(dir_path="/tmp/pics", total=100, deleted=10,
                  freed_bytes=1024*1024, apply=True)
    record_classify(camera_dir="/tmp/cam", scanned=50,
                    bucket_counts={"活体": 5, "关节": 1}, copied_bucket=6)
    print("ok")
