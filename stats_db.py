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


SCHEMA_VERSION = 2

_HOST_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def _safe_hostname() -> str:
    try:
        host = socket.gethostname() or "unknown"
    except Exception:
        host = "unknown"
    return _HOST_SAFE_RE.sub("_", host)[:64] or "unknown"


# --------------------------- 机器指纹 / 本地 IP (带缓存) ------------------
# 目的: 多机同一份 db 时快速分辨到底哪台机器. 一定要两个字段, 防止 IP 侧翻车
# (换网卡 / VPN / DHCP), 硬件指纹兜底.

_MACHINE_FP_CACHE: str | None = None
_LOCAL_IP_CACHE: tuple[float, str] | None = None
_LOCAL_IP_TTL_SEC = 30.0


def _machine_fp() -> str:
    """licensing.get_fingerprint() 结果, 形如 'F260-BD4F-30EA-C616'.

    进程内永久缓存 (硬件指纹在一个进程生命周期内不会变).
    licensing 缺失或指纹计算异常 -> 返回 ''; 主流程绝不受影响.
    """
    global _MACHINE_FP_CACHE
    if _MACHINE_FP_CACHE is not None:
        return _MACHINE_FP_CACHE
    fp = ""
    try:
        import licensing  # type: ignore
        fp = licensing.get_fingerprint() or ""
    except Exception:
        # subprocess 挂 / licensing 未打包 / 权限异常 -> 静默
        fp = ""
    _MACHINE_FP_CACHE = fp
    return fp


def _local_ip() -> str:
    """本机默认路由出口 IP (IPv4). 30 秒 TTL.

    - UDP connect 到 8.8.8.8:80 拿 getsockname 里的本地地址, 不发实际数据包
    - 断网 / 无路由: 落到 gethostbyname(hostname); 还挂就返回 ''
    """
    global _LOCAL_IP_CACHE
    now = time.time()
    if _LOCAL_IP_CACHE is not None:
        _t, _ip = _LOCAL_IP_CACHE
        if now - _t < _LOCAL_IP_TTL_SEC:
            return _ip
    ip = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(0.5)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0] or ""
        finally:
            try: s.close()
            except Exception: pass
    except Exception:
        try:
            ip = socket.gethostbyname(socket.gethostname()) or ""
        except Exception:
            ip = ""
    _LOCAL_IP_CACHE = (now, ip)
    return ip


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
    host          TEXT    NOT NULL,        -- 机器名 (socket.gethostname)
    machine_fp    TEXT,                    -- 机器指纹 (licensing get_fingerprint)
    local_ip      TEXT,                    -- 本地 IP (默认路由出口)
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
    machine_fp     TEXT,                   -- 机器指纹
    local_ip       TEXT,                   -- 本地 IP
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
    machine_fp    TEXT,                   -- 机器指纹
    local_ip      TEXT,                   -- 本地 IP
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


# ---------------- 最终表 (每目标一行, UPSERT). 与流水表一一对应, 用于跨天/跨机
# 汇总时避免同一视频/目录/camera 被重复计数. 流水表 (extract_stats /
# dedupe_stats / classify_stats) 全量保留, 供审计与回放.

_DDL_EXTRACT_FINAL = """
CREATE TABLE IF NOT EXISTS extract_final (
    video_md5     TEXT PRIMARY KEY,          -- 视频快速指纹 (size + head/mid/tail)
    file_size     INTEGER,                   -- 视频字节数
    video_path    TEXT NOT NULL,             -- 最新一次抽帧时的绝对路径
    src_root      TEXT,
    dst_root      TEXT,
    output_dir    TEXT,
    rel_path      TEXT,
    frames        INTEGER NOT NULL DEFAULT 0,-- 最新一次抽出的帧数
    duration_sec  REAL,
    fps           REAL,
    quality       INTEGER,
    naming_style  TEXT,
    seq_digits    INTEGER,
    last_ts       TEXT NOT NULL,             -- 最后一次落库时间
    last_host     TEXT NOT NULL,             -- 最后一次是哪台机器 (hostname)
    last_machine_fp TEXT,                    -- 最后一次机器指纹
    last_local_ip TEXT,                      -- 最后一次本地 IP
    last_task_id  TEXT,                      -- 最后一次的任务 uuid
    last_version  TEXT,                      -- 最后一次的程序版本 tag
    last_stage    TEXT,                      -- ok / empty / failed / locked
    last_elapsed_sec REAL,                   -- 最后一次耗时
    last_msg      TEXT,
    first_ts      TEXT NOT NULL,             -- 首次落库时间
    run_count     INTEGER NOT NULL DEFAULT 1 -- 累计抽帧次数 (>1 = 被重复切过)
);
CREATE INDEX IF NOT EXISTS idx_ex_final_last_ts  ON extract_final(last_ts);
CREATE INDEX IF NOT EXISTS idx_ex_final_video    ON extract_final(video_path);
CREATE INDEX IF NOT EXISTS idx_ex_final_host     ON extract_final(last_host);
"""

_DDL_DEDUPE_FINAL = """
CREATE TABLE IF NOT EXISTS dedupe_final (
    dir_path       TEXT NOT NULL,            -- 去重目录 (与 host 组合唯一)
    host           TEXT NOT NULL,            -- 机器名 (PK 一部分)
    last_machine_fp TEXT,                    -- 最后一次机器指纹
    last_local_ip  TEXT,                     -- 最后一次本地 IP
    source_root    TEXT,
    total          INTEGER NOT NULL DEFAULT 0,
    deleted        INTEGER NOT NULL DEFAULT 0,
    remain         INTEGER NOT NULL DEFAULT 0,
    freed_bytes    INTEGER NOT NULL DEFAULT 0,
    threshold      INTEGER,
    protect_count  INTEGER DEFAULT 0,
    scene_count    INTEGER DEFAULT 0,
    apply          INTEGER DEFAULT 0,
    report_csv     TEXT,
    last_ts        TEXT NOT NULL,
    last_task_id   TEXT,
    last_version   TEXT,                     -- 最后一次的程序版本 tag
    last_elapsed_sec REAL,
    last_exit_code INTEGER,
    last_msg       TEXT,
    first_ts       TEXT NOT NULL,
    run_count      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (dir_path, host)
);
CREATE INDEX IF NOT EXISTS idx_de_final_last_ts ON dedupe_final(last_ts);
CREATE INDEX IF NOT EXISTS idx_de_final_dir     ON dedupe_final(dir_path);
"""

_DDL_CLASSIFY_FINAL = """
CREATE TABLE IF NOT EXISTS classify_final (
    camera_dir    TEXT NOT NULL,             -- camera 目录 (与 host 组合唯一)
    host          TEXT NOT NULL,             -- 机器名 (PK 一部分)
    last_machine_fp TEXT,                    -- 最后一次机器指纹
    last_local_ip TEXT,                      -- 最后一次本地 IP
    in_root       TEXT,
    out_root      TEXT,
    scanned       INTEGER NOT NULL DEFAULT 0,
    copied_bucket INTEGER NOT NULL DEFAULT 0,
    bucket_json   TEXT,
    errors        INTEGER NOT NULL DEFAULT 0,
    last_ts       TEXT NOT NULL,
    last_task_id  TEXT,
    last_version  TEXT,                      -- 最后一次的程序版本 tag
    last_elapsed_sec REAL,
    last_exit_code INTEGER,
    last_msg      TEXT,
    first_ts      TEXT NOT NULL,
    run_count     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (camera_dir, host)
);
CREATE INDEX IF NOT EXISTS idx_cl_final_last_ts ON classify_final(last_ts);
CREATE INDEX IF NOT EXISTS idx_cl_final_camera  ON classify_final(camera_dir);
"""

_DDL_TASK_RUNS = """
CREATE TABLE IF NOT EXISTS task_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,       -- 记录时间
    host         TEXT    NOT NULL,       -- 机器名
    machine_fp   TEXT,                   -- 机器指纹
    local_ip     TEXT,                   -- 本地 IP
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


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """给已存在的老 db 补齐新增列, 幂等. SQLite 的 ALTER TABLE ADD COLUMN
    不支持 IF NOT EXISTS, 所以先 PRAGMA table_info 探测再加."""
    plan = {
        "extract_stats":   [("machine_fp", "TEXT"), ("local_ip", "TEXT")],
        "dedupe_stats":    [("machine_fp", "TEXT"), ("local_ip", "TEXT")],
        "classify_stats":  [("machine_fp", "TEXT"), ("local_ip", "TEXT")],
        "extract_final":   [("last_machine_fp", "TEXT"),
                            ("last_local_ip", "TEXT")],
        "dedupe_final":    [("last_machine_fp", "TEXT"),
                            ("last_local_ip", "TEXT")],
        "classify_final":  [("last_machine_fp", "TEXT"),
                            ("last_local_ip", "TEXT")],
        "task_runs":       [("machine_fp", "TEXT"), ("local_ip", "TEXT")],
    }
    for tbl, cols in plan.items():
        try:
            cur = conn.execute(f"PRAGMA table_info({tbl})")
            existing = {row[1] for row in cur.fetchall()}
            if not existing:
                continue  # 表不存在, 由 CREATE IF NOT EXISTS 建
            for col, typ in cols:
                if col not in existing:
                    try:
                        conn.execute(
                            f"ALTER TABLE {tbl} ADD COLUMN {col} {typ}")
                    except sqlite3.OperationalError as e:
                        # 并发迁移下另一进程已加 -> "duplicate column"
                        if "duplicate column" not in str(e).lower():
                            print(f"[stats_db] WARN ALTER {tbl}.{col}: {e}",
                                  file=sys.stderr)
        except Exception as e:
            print(f"[stats_db] WARN ensure_columns {tbl}: {e}",
                  file=sys.stderr)


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL_SCHEMA)
    conn.executescript(_DDL_EXTRACT)
    conn.executescript(_DDL_DEDUPE)
    conn.executescript(_DDL_CLASSIFY)
    conn.executescript(_DDL_EXTRACT_FINAL)
    conn.executescript(_DDL_DEDUPE_FINAL)
    conn.executescript(_DDL_CLASSIFY_FINAL)
    conn.executescript(_DDL_TASK_RUNS)
    # 老 db 补列 (ALTER TABLE ADD COLUMN, 幂等)
    _ensure_columns(conn)
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


def _upsert_with_retry(
    db_path: Path, sql: str, params: tuple,
) -> None:
    """与 _insert_with_retry 语义一致, 供 UPSERT (INSERT ... ON CONFLICT) 复用."""
    _insert_with_retry(db_path, sql, params)


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
    video_md5: str | None = None,
    file_size: int | None = None,
    db_path: str | os.PathLike | None = None,
) -> None:
    """记录一次抽帧结果.

    - 写流水: extract_stats 一次一条, 全量保留.
    - 写最终: extract_final 以 video_md5 为主键 UPSERT; 无 md5 时跳过 final.
      重复切帧时: 流水累加, 最终表覆盖 (run_count+1).

    静默失败, 不影响主流程.
    """
    try:
        target = Path(db_path) if db_path else default_db_path()
        ts_now = _now_str()
        host = _safe_hostname()
        m_fp = _machine_fp()
        l_ip = _local_ip()
        _insert_with_retry(
            target,
            """
            INSERT INTO extract_stats(
                ts, host, machine_fp, local_ip, task_id, version,
                src_root, dst_root, video_path, output_dir, rel_path,
                frames, duration_sec, fps, quality, naming_style,
                seq_digits, elapsed_sec, stage, exit_code, msg
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts_now, host, m_fp, l_ip, task_id, version,
                src_root, dst_root, video_path, output_dir, rel_path,
                int(frames or 0),
                duration_sec, fps, quality, naming_style, seq_digits,
                elapsed_sec, stage, int(exit_code or 0), msg,
            ),
        )
        # UPSERT 到 extract_final; 无 md5 时不写 (无法唯一标识视频)
        if video_md5:
            _upsert_with_retry(
                target,
                """
                INSERT INTO extract_final(
                    video_md5, file_size, video_path, src_root, dst_root,
                    output_dir, rel_path, frames, duration_sec, fps,
                    quality, naming_style, seq_digits,
                    last_ts, last_host, last_machine_fp, last_local_ip,
                    last_task_id, last_version,
                    last_stage, last_elapsed_sec, last_msg,
                    first_ts, run_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(video_md5) DO UPDATE SET
                    file_size    = excluded.file_size,
                    video_path   = excluded.video_path,
                    src_root     = excluded.src_root,
                    dst_root     = excluded.dst_root,
                    output_dir   = excluded.output_dir,
                    rel_path     = excluded.rel_path,
                    frames       = excluded.frames,
                    duration_sec = excluded.duration_sec,
                    fps          = excluded.fps,
                    quality      = excluded.quality,
                    naming_style = excluded.naming_style,
                    seq_digits   = excluded.seq_digits,
                    last_ts      = excluded.last_ts,
                    last_host    = excluded.last_host,
                    last_machine_fp = excluded.last_machine_fp,
                    last_local_ip   = excluded.last_local_ip,
                    last_task_id = excluded.last_task_id,
                    last_version = excluded.last_version,
                    last_stage   = excluded.last_stage,
                    last_elapsed_sec = excluded.last_elapsed_sec,
                    last_msg     = excluded.last_msg,
                    run_count    = extract_final.run_count + 1
                """,
                (
                    video_md5, file_size, video_path, src_root, dst_root,
                    output_dir, rel_path, int(frames or 0), duration_sec, fps,
                    quality, naming_style, seq_digits,
                    ts_now, host, m_fp, l_ip, task_id, version,
                    stage, elapsed_sec, msg,
                    ts_now,
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
    """记录一次去重结果.

    - 写流水: dedupe_stats 一次一条.
    - 写最终: dedupe_final 以 (dir_path, host) 为主键 UPSERT.
    静默失败.
    """
    try:
        if remain is None:
            remain = max(0, int(total or 0) - int(deleted or 0))
        target = Path(db_path) if db_path else default_db_path()
        ts_now = _now_str()
        host = _safe_hostname()
        m_fp = _machine_fp()
        l_ip = _local_ip()
        _insert_with_retry(
            target,
            """
            INSERT INTO dedupe_stats(
                ts, host, machine_fp, local_ip, task_id, version, source_root,
                dir_path, total, deleted, remain, freed_bytes,
                threshold, protect_count, scene_count, apply,
                report_csv, elapsed_sec, exit_code, msg
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts_now, host, m_fp, l_ip, task_id, version, source_root,
                dir_path, int(total or 0), int(deleted or 0), int(remain or 0),
                int(freed_bytes or 0),
                threshold, int(protect_count or 0), int(scene_count or 0),
                1 if apply else 0,
                report_csv, elapsed_sec, int(exit_code or 0), msg,
            ),
        )
        _upsert_with_retry(
            target,
            """
            INSERT INTO dedupe_final(
                dir_path, host, last_machine_fp, last_local_ip,
                source_root, total, deleted, remain,
                freed_bytes, threshold, protect_count, scene_count,
                apply, report_csv, last_ts, last_task_id, last_version,
                last_elapsed_sec, last_exit_code, last_msg,
                first_ts, run_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, 1)
            ON CONFLICT(dir_path, host) DO UPDATE SET
                last_machine_fp = excluded.last_machine_fp,
                last_local_ip   = excluded.last_local_ip,
                source_root   = excluded.source_root,
                total         = excluded.total,
                deleted       = excluded.deleted,
                remain        = excluded.remain,
                freed_bytes   = excluded.freed_bytes,
                threshold     = excluded.threshold,
                protect_count = excluded.protect_count,
                scene_count   = excluded.scene_count,
                apply         = excluded.apply,
                report_csv    = excluded.report_csv,
                last_ts       = excluded.last_ts,
                last_task_id  = excluded.last_task_id,
                last_version  = excluded.last_version,
                last_elapsed_sec = excluded.last_elapsed_sec,
                last_exit_code   = excluded.last_exit_code,
                last_msg      = excluded.last_msg,
                run_count     = dedupe_final.run_count + 1
            """,
            (
                dir_path, host, m_fp, l_ip,
                source_root, int(total or 0),
                int(deleted or 0), int(remain or 0), int(freed_bytes or 0),
                threshold, int(protect_count or 0), int(scene_count or 0),
                1 if apply else 0, report_csv,
                ts_now, task_id, version,
                elapsed_sec, int(exit_code or 0), msg,
                ts_now,
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
    """记录一次分类结果.

    - 写流水: classify_stats 一次一条.
    - 写最终: classify_final 以 (camera_dir, host) 为主键 UPSERT.
    静默失败.
    """
    try:
        bucket_json = json.dumps(bucket_counts or {}, ensure_ascii=False)
        target = Path(db_path) if db_path else default_db_path()
        ts_now = _now_str()
        host = _safe_hostname()
        m_fp = _machine_fp()
        l_ip = _local_ip()
        _insert_with_retry(
            target,
            """
            INSERT INTO classify_stats(
                ts, host, machine_fp, local_ip, task_id, version,
                in_root, out_root,
                camera_dir, scanned, copied_bucket, bucket_json,
                errors, elapsed_sec, exit_code, msg
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts_now, host, m_fp, l_ip, task_id, version,
                in_root, out_root, camera_dir,
                int(scanned or 0), int(copied_bucket or 0), bucket_json,
                int(errors or 0), elapsed_sec, int(exit_code or 0), msg,
            ),
        )
        _upsert_with_retry(
            target,
            """
            INSERT INTO classify_final(
                camera_dir, host, last_machine_fp, last_local_ip,
                in_root, out_root,
                scanned, copied_bucket, bucket_json, errors,
                last_ts, last_task_id, last_version,
                last_elapsed_sec, last_exit_code, last_msg,
                first_ts, run_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(camera_dir, host) DO UPDATE SET
                last_machine_fp = excluded.last_machine_fp,
                last_local_ip   = excluded.last_local_ip,
                in_root       = excluded.in_root,
                out_root      = excluded.out_root,
                scanned       = excluded.scanned,
                copied_bucket = excluded.copied_bucket,
                bucket_json   = excluded.bucket_json,
                errors        = excluded.errors,
                last_ts       = excluded.last_ts,
                last_task_id  = excluded.last_task_id,
                last_version  = excluded.last_version,
                last_elapsed_sec = excluded.last_elapsed_sec,
                last_exit_code   = excluded.last_exit_code,
                last_msg      = excluded.last_msg,
                run_count     = classify_final.run_count + 1
            """,
            (
                camera_dir, host, m_fp, l_ip,
                in_root, out_root,
                int(scanned or 0), int(copied_bucket or 0), bucket_json,
                int(errors or 0),
                ts_now, task_id, version,
                elapsed_sec, int(exit_code or 0), msg,
                ts_now,
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
                ts, host, machine_fp, local_ip,
                task_id, task_type, version, cmdline, config_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now_str(), _safe_hostname(), _machine_fp(), _local_ip(),
                task_id, task_type,
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
