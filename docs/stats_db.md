# 统计数据库设计文档（stats.db）

**状态**：设计中，未实现（v0.4.49 起接入）
**目的**：把 extract / dedupe 长任务的产出量落库，方便跨天/跨机汇总、导出、可视化。

---

## §1 存储位置

**每台机器一个本地文件**（不共享 SQLite 到 Z 盘）：

- 默认路径：`~/.pic-clear/stats_<hostname>.db`
  - Windows：`%USERPROFILE%\.pic-clear\stats_<hostname>.db`
  - hostname 从 `socket.gethostname()` 取，特殊字符做 `[A-Za-z0-9_-]` 过滤
- GUI 可覆盖：新加 `stats_db_path` 配置项，写在 `~/.pic-clear/dedupe_gui.json` / `extract_gui.json`

**为什么不放共享盘**（血泪教训）：
- Z 盘是 SMB 共享，SQLite 的文件锁在 SMB 上不可靠
- 多机并发写会 `database is locked`，极端情况下坏库
- 每机本地写自己文件 → **无锁竞争**，`WAL` 模式 + 短事务，本地磁盘并发安全

**GUI 查看端聚合**：
- `stats_viewer_gui.py` 支持"扫描目录"（默认 `~/.pic-clear/`）
- 自动读该目录下所有 `stats_*.db`，用 SQL `ATTACH DATABASE` 或 Python 层合并
- 想跨机看，把各机器的 `stats_<hostname>.db` 手动拷到一个目录再扫

---

## §2 表结构

**建库时开启 WAL**：
```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;  -- WAL 下够安全, 快
PRAGMA foreign_keys=ON;
```

### 2.1 `extract_stats`（抽帧记录）

```sql
CREATE TABLE IF NOT EXISTS extract_stats (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,        -- ISO8601 本地时间 '2026-07-18 14:23:45'
    ts_date       TEXT    NOT NULL,        -- '2026-07-18' 冗余, 便于按天 group
    host          TEXT    NOT NULL,        -- 机器名 (来源 socket.gethostname)
    task_id       TEXT,                    -- 一次抽帧任务的 uuid, 便于把同一批归组
    source_root   TEXT,                    -- 用户点开始时填的 "视频源目录"
    video_path    TEXT    NOT NULL,        -- 具体这条视频的绝对路径
    output_dir    TEXT    NOT NULL,        -- 帧输出目录
    frames        INTEGER NOT NULL,        -- 本次抽出多少张
    duration_sec  REAL,                    -- 视频时长 (秒), NULL=没探到
    fps           REAL,                    -- 抽帧频率 (每秒 x 张)
    naming_style  TEXT,                    -- 'parent' 或 'legacy'
    seq_digits    INTEGER,                 -- 序号补零位数
    elapsed_sec   REAL,                    -- 本视频抽帧耗时
    exit_code     INTEGER,                 -- 0 成功, 非 0 失败
    error_msg     TEXT                     -- 失败时记原因
);
CREATE INDEX idx_ex_date ON extract_stats(ts_date);
CREATE INDEX idx_ex_task ON extract_stats(task_id);
CREATE INDEX idx_ex_source_root ON extract_stats(source_root);
```

### 2.2 `dedupe_stats`（去重记录）

```sql
CREATE TABLE IF NOT EXISTS dedupe_stats (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT    NOT NULL,       -- ISO8601 完成时间
    ts_date        TEXT    NOT NULL,       -- '2026-07-18'
    host           TEXT    NOT NULL,
    task_id        TEXT,                   -- 一轮去重的 uuid
    target_root    TEXT,                   -- 用户点开始时填的 "去重目标"
    dir_path       TEXT    NOT NULL,       -- 具体这个 camera 目录
    total          INTEGER NOT NULL,       -- 目录里图片总数 (扫描完的有效数)
    deleted        INTEGER NOT NULL,       -- 本次删了多少
    remain         INTEGER NOT NULL,       -- total - deleted
    freed_bytes    INTEGER,                -- 释放字节数
    threshold      INTEGER,                -- 相似度阈值
    protect_count  INTEGER,                -- 受保护多少张
    scene_count    INTEGER,                -- 场景保护多少张
    apply          INTEGER,                -- 0=仅报告  1=真删
    report_csv     TEXT,                   -- 对应的 dedupe_report.csv 绝对路径
    elapsed_sec    REAL,
    exit_code      INTEGER,
    error_msg      TEXT
);
CREATE INDEX idx_de_date ON dedupe_stats(ts_date);
CREATE INDEX idx_de_task ON dedupe_stats(task_id);
CREATE INDEX idx_de_target_root ON dedupe_stats(target_root);
```

### 2.3 `schema_version`（迁移用）

```sql
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version(version) VALUES (1);
```

未来加列走 `ALTER TABLE ... ADD COLUMN`（SQLite 支持），schema_version bump。

---

## §3 写入时机与并发策略

### 3.1 extract_frames.py

- 每抽完一个视频写一条 → 单条 INSERT + COMMIT
- 用 `with` 短事务，10~50ms 一次，不阻塞主流程
- 失败也要写（`exit_code != 0` + `error_msg`），跨天恢复才好追

**任务级归组**：
- `main()` 启动时生成一个 `task_id = uuid.uuid4().hex[:12]`
- 传给每次 INSERT，用户想看"这批任务干了多少"直接 `WHERE task_id=?`

### 3.2 dedupe_pic.py

- 每处理完一个 camera 目录写一条（删完 or dry-run 完 or 失败）
- 位置：`_run_dedupe` 末尾，紧挨着 `_dedup_done.marker` 写完之后
- 从 `dedupe_report.csv` 头再读一次 total/deleted 兜底（万一变量丢了）

### 3.3 并发安全

**本机内**（多线程 GUI 起多个子进程）：
- SQLite WAL 模式 + 每次 `connect()` 就用就关，避免长连接卡锁
- 单表 INSERT，冲突几率极低
- 兜底：`BEGIN IMMEDIATE` + retry 3 次，间隔 100ms

**跨机**：不涉及（每机自己文件）

---

## §4 API 模块：`stats_db.py`

**统一封装**，两个 exe 都调这个模块：

```python
# stats_db.py 大致 API

def open_stats_db(path: Path | None = None) -> Path:
    """打开或初始化. path 默认 ~/.pic-clear/stats_<host>.db.
    首次调用建表 + 开 WAL. 返回最终路径."""

def record_extract(
    *, video_path: str, output_dir: str, frames: int,
    duration_sec: float | None = None, fps: float | None = None,
    source_root: str | None = None, task_id: str | None = None,
    naming_style: str | None = None, seq_digits: int | None = None,
    elapsed_sec: float | None = None,
    exit_code: int = 0, error_msg: str | None = None,
    db_path: Path | None = None,
) -> None: ...

def record_dedupe(
    *, dir_path: str, total: int, deleted: int, remain: int,
    target_root: str | None = None, task_id: str | None = None,
    freed_bytes: int | None = None, threshold: int | None = None,
    protect_count: int | None = None, scene_count: int | None = None,
    apply: bool = True, report_csv: str | None = None,
    elapsed_sec: float | None = None,
    exit_code: int = 0, error_msg: str | None = None,
    db_path: Path | None = None,
) -> None: ...

def query_extract(...) -> list[dict]: ...
def query_dedupe(...) -> list[dict]: ...
def sum_by_day(kind: str, ...) -> list[tuple[str, int]]: ...  # (date, count)
```

**特点**：
- 所有 record_* 内部 try/except **静默失败**（打 stderr 但不抛），DB 挂了不影响主流程
- `db_path=None` 走默认，多机部署零改动

---

## §5 GUI 查看工具：`stats_viewer_gui.py`

**打包成独立 exe** `stats_viewer_gui.exe`，跟 dedupe_gui 一样的样式。

### 5.1 界面结构

```
┌─────────────────────────────────────────────────┐
│ [Tab] 抽帧统计 | 去重统计 | 每日趋势 | 关于     │
├─────────────────────────────────────────────────┤
│ 扫描目录: [~/.pic-clear/         ] [浏览] [刷新] │
│ 已加载: stats_HOST1.db (12345 条) stats_HOST2.db (6789 条) │
│                                                 │
│ 日期: [2026-07-01] 到 [2026-07-18]              │
│ 主机: [全部 ▼]    关键字: [_______]  [过滤]    │
├─────────────────────────────────────────────────┤
│ 合计: 抽帧 12345 视频, 234567 张 / 总耗时 6.5h  │
├─────────────────────────────────────────────────┤
│  ts        host   source_root   video   frames  │
│  2026-...  H1    Z:\sjbz_...   1.mp4    179     │
│  ...                                            │
├─────────────────────────────────────────────────┤
│ [导出 CSV] [清理选中记录] [关闭]                │
└─────────────────────────────────────────────────┘
```

### 5.2 4 个 Tab

1. **抽帧统计**：表格 + 顶部合计（"今天 xxx 张 / 本周 xxx 张 / 全部 xxx 张"）
2. **去重统计**：同上，字段换成 total/deleted/remain
3. **每日趋势**：折线图 — 横轴日期，纵轴每天张数
   - 抽帧曲线 + 去重曲线两条线
   - 用 `matplotlib` embed 到 tkinter（PyInstaller 打包能过）
4. **关于**：跟 dedupe_gui 同款风格，显示 `stats_viewer_gui` 自己版本

### 5.3 交互

- 「扫描目录」→ 用 `glob(dir + '/stats_*.db')` 找所有库文件 → ATTACH 后聚合查询
- 「刷新」→ 重扫 + 重跑当前过滤 SQL
- 「导出 CSV」→ 把当前表格里的数据存 CSV（不是导整个库）
- 「清理选中记录」→ 慎用，让用户勾了后二次确认再 DELETE

---

## §6 向下兼容：老 CSV 怎么办

**并存**方案（v0.4.49 → 未来某版下掉 CSV）：

- 老逻辑 `scripts_bat/append_stats.bat` 继续写 `~/.pic-clear/.stats/<date>/machine_id_HOST.csv`
- 新 `stats_db.py` 独立写 SQLite
- 两边不互相干扰，历史数据用旧 GUI（如果有）或直接看 CSV 文件

**迁移工具**（可选，未来做）：`stats_migrate_csv.py` 一次性把老 CSV 导进 SQLite。

---

## §7 打包与分发

### 7.1 依赖

- SQLite：Python 标准库 `sqlite3`，不需额外依赖
- matplotlib：新增 CI 依赖，`stats_viewer_gui` 的 workflow 里加 `pip install matplotlib`
- **注意**：dedupe_gui / extract_gui 只写库不看图，**不引入 matplotlib**

### 7.2 workflow

- 新 workflow `.github/workflows/build-stats-viewer-gui-exe.yml`
- 新增 `stats_db.py` 要 copy 到所有涉及的 build 文件夹：
  - `build-dedupe-gui-exe.yml`
  - `build-extract-gui-exe.yml`（GUI 层要读 task_id 传给子进程，可能也要）
  - `build-extract-frames-exe.yml`（extract_frames.exe 直接写库）
  - `build-dedupe-pic-exe.yml`（dedupe_pic.exe 直接写库）
- pyarmor gen 一起加密

---

## §8 实施步骤（分阶段，避免大爆炸）

### Phase 1：底层写库（v0.4.49）
1. 新建 `stats_db.py` — API + 建表 + 兼容层
2. `extract_frames.py` 每抽完一个视频调 `record_extract`
3. `dedupe_pic.py` 每处理完一个目录调 `record_dedupe`
4. 两个 workflow 加 copy + pyarmor
5. **验证**：跑一次抽帧 / 一次去重 → 用 `sqlite3` 命令行看库有没有数据

### Phase 2：GUI 传 task_id 与源根（v0.4.50）
1. `extract_gui` / `dedupe_gui` 启动子进程时生成 task_id + 通过命令行传 `--task-id XXX --source-root YYY`
2. 让 SQL 能按任务归组查询

### Phase 3：查看器（v0.4.51）
1. 新建 `stats_viewer_gui.py` — 表格 Tab
2. 新 workflow 打包
3. **验证**：能看到 Phase 1 写进去的数据

### Phase 4：图表（v0.4.52）
1. matplotlib embed 每日趋势 Tab
2. 导出 CSV

### Phase 5（可选）：老 CSV 一键导入 SQLite

---

## §9 注意事项 / 已知陷阱

1. **hostname 里的中文/特殊字符**：过滤成 `[A-Za-z0-9_-]`，别让文件名炸掉
2. **DB 打不开时**：所有 record_* 静默 fail + stderr 打日志，不能因为落库失败搞崩主流程（这是抽帧/去重工具，落库是**副产物**）
3. **DB 文件误删/迁移**：`open_stats_db` 每次都 `CREATE TABLE IF NOT EXISTS`，删了自动重建，只是丢历史
4. **PyInstaller onefile 打包 sqlite3**：标准库自带，通常不用 `--hidden-import`，但保险起见加 `--hidden-import sqlite3`
5. **matplotlib 首启很慢**（~2s）：查看器 GUI 里图表 Tab 首次点开再加载，别启动时预加载
6. **时间字段用文本**：不用 UNIX epoch，直接存 `datetime.now().strftime('%Y-%m-%d %H:%M:%S')`，SQLite 里 lexicographic 顺序等于时间顺序，比较友好
7. **write 失败重试**：`sqlite3.OperationalError: database is locked` retry 3 次，每次 sleep 100ms 递增；仍失败就吞掉，别抛

---

## §10 数据留存策略

- 无自动清理（磁盘占用不大，10w 条 ~ 20MB）
- GUI 提供「清理选中记录」按钮，用户自己删
- 未来可加"保留 90 天，超期归档" 但不着急

---

**审阅确认**：这份文档 OK 就开工 Phase 1。有想改的告诉我。
