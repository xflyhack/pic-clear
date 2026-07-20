# 统计数据库设计文档（stats.db）

**状态**：Phase 1 已实现（v0.4.49），Phase 3 查看器 GUI 已实现（v0.4.49 一并合并）
**目的**：把 extract / dedupe / classify 长任务的产出量落库，方便跨天/跨机汇总、导出、可视化。

---

## §1 存储位置

**每台机器一个本地文件**（不共享 SQLite 到 Z 盘）：

- 默认路径：`~/.pic-clear/stats_<hostname>.db`
  - Windows：`%USERPROFILE%\.pic-clear\stats_<hostname>.db`
  - hostname 从 `socket.gethostname()` 取，特殊字符过滤为 `[A-Za-z0-9_-]`
- GUI 可覆盖：新加 `stats_db_path` 配置项，写在 `~/.pic-clear/dedupe_gui.json` / `extract_gui.json` / `classify_gui.json`

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
PRAGMA synchronous=NORMAL;   -- WAL 下够安全, 更快
PRAGMA busy_timeout=5000;    -- 单机并发的关键: 遇锁自动重试 5 秒
PRAGMA foreign_keys=ON;
```

### 2.1 `extract_stats`（抽帧记录）

**含义**：extract_frames.exe 每抽完一个视频写一条。

```sql
CREATE TABLE IF NOT EXISTS extract_stats (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    ts_date       TEXT    NOT NULL,
    host          TEXT    NOT NULL,
    task_id       TEXT,
    source_root   TEXT,
    video_path    TEXT    NOT NULL,
    output_dir    TEXT    NOT NULL,
    frames        INTEGER NOT NULL,
    duration_sec  REAL,
    fps           REAL,
    naming_style  TEXT,
    seq_digits    INTEGER,
    elapsed_sec   REAL,
    exit_code     INTEGER,
    error_msg     TEXT
);
CREATE INDEX idx_ex_date ON extract_stats(ts_date);
CREATE INDEX idx_ex_task ON extract_stats(task_id);
CREATE INDEX idx_ex_source_root ON extract_stats(source_root);
```

**字段中文注释**：

| 字段 | 类型 | 中文含义 | 示例 |
|---|---|---|---|
| `id` | INTEGER PK | 自增主键 | 1, 2, ... |
| `ts` | TEXT | 抽帧完成时间（本地时区，ISO8601） | `2026-07-18 14:23:45` |
| `ts_date` | TEXT | 完成日期（冗余，方便按天分组） | `2026-07-18` |
| `host` | TEXT | 机器名（`socket.gethostname()`） | `DESKTOP-M7ET7A6` |
| `task_id` | TEXT | 一次抽帧任务的短 uuid（同一批视频共用） | `a3f1b2c9d4e5` |
| `source_root` | TEXT | 用户点开始时填的"视频源目录" | `Z:\sjbz_20260715` |
| `video_path` | TEXT | 具体这条视频的绝对路径 | `Z:\sjbz_...\1.mp4` |
| `output_dir` | TEXT | 帧输出目录 | `D:\切帧结果\...\camera01` |
| `frames` | INTEGER | 本视频抽出多少张图 | `179` |
| `duration_sec` | REAL | 视频时长（秒），未探到=NULL | `18.5` |
| `fps` | REAL | 抽帧频率（每秒 x 张） | `1.0` |
| `naming_style` | TEXT | 图片命名规则 | `parent` / `legacy` |
| `seq_digits` | INTEGER | 序号补零位数 | `4` / `6` |
| `elapsed_sec` | REAL | 本视频抽帧耗时（秒） | `2.3` |
| `exit_code` | INTEGER | 子进程退出码，0=成功 | `0` |
| `error_msg` | TEXT | 失败时的错误说明 | `ffmpeg: no such file` |

---

### 2.2 `dedupe_stats`（去重记录）

**含义**：dedupe_pic.exe 每处理完一个 camera 目录写一条。

```sql
CREATE TABLE IF NOT EXISTS dedupe_stats (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT    NOT NULL,
    ts_date        TEXT    NOT NULL,
    host           TEXT    NOT NULL,
    task_id        TEXT,
    target_root    TEXT,
    dir_path       TEXT    NOT NULL,
    total          INTEGER NOT NULL,
    deleted        INTEGER NOT NULL,
    remain         INTEGER NOT NULL,
    freed_bytes    INTEGER,
    threshold      INTEGER,
    protect_count  INTEGER,
    scene_count    INTEGER,
    apply          INTEGER,
    report_csv     TEXT,
    elapsed_sec    REAL,
    exit_code      INTEGER,
    error_msg      TEXT
);
CREATE INDEX idx_de_date ON dedupe_stats(ts_date);
CREATE INDEX idx_de_task ON dedupe_stats(task_id);
CREATE INDEX idx_de_target_root ON dedupe_stats(target_root);
```

**字段中文注释**：

| 字段 | 类型 | 中文含义 | 示例 |
|---|---|---|---|
| `id` | INTEGER PK | 自增主键 | 1, 2, ... |
| `ts` | TEXT | 完成时间（本地时区，ISO8601） | `2026-07-18 14:23:45` |
| `ts_date` | TEXT | 完成日期（按天分组用） | `2026-07-18` |
| `host` | TEXT | 机器名 | `DESKTOP-M7ET7A6` |
| `task_id` | TEXT | 一轮去重的短 uuid | `b8e2c7a1f9d0` |
| `target_root` | TEXT | 用户点开始时填的"去重目标" | `Z:\切帧结果` |
| `dir_path` | TEXT | 具体这个 camera 目录绝对路径 | `Z:\切帧结果\...\camera07` |
| `total` | INTEGER | 目录里图片总数（有效可读的） | `156` |
| `deleted` | INTEGER | 本次实际删掉的张数 | `42` |
| `remain` | INTEGER | 剩余 = total - deleted | `114` |
| `freed_bytes` | INTEGER | 释放的字节数 | `72817356` |
| `threshold` | INTEGER | 相似度阈值（Hamming 距离） | `3` |
| `protect_count` | INTEGER | 受保护未删的张数（有 person/车辆等） | `52` |
| `scene_count` | INTEGER | 场景保护数（纯色/异常帧） | `0` |
| `apply` | INTEGER | 是否真删，0=仅报告 1=删了 | `1` |
| `report_csv` | TEXT | 对应 dedupe_report.csv 的绝对路径 | `Z:\...\dedupe_report.csv` |
| `elapsed_sec` | REAL | 本目录总耗时（秒） | `78.2` |
| `exit_code` | INTEGER | 子进程退出码，0=成功 | `0` |
| `error_msg` | TEXT | 失败时的错误说明 | `keyboard interrupt` |

---

### 2.3 `classify_stats`（二次分类记录）

**含义**：classify_gui / classify_pic 每处理完一个 camera 目录写一条。因为 classify 是"读一个目录 → 按规则复制/移动到不同 bucket 目录"，粒度和 dedupe 一致（一目录一条记录）。

```sql
CREATE TABLE IF NOT EXISTS classify_stats (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    ts_date       TEXT    NOT NULL,
    host          TEXT    NOT NULL,
    task_id       TEXT,
    in_root       TEXT,
    out_root      TEXT,
    dir_path      TEXT    NOT NULL,
    total         INTEGER NOT NULL,
    classified    INTEGER NOT NULL,
    skipped       INTEGER,
    failed        INTEGER,
    bucket_json   TEXT,
    copy_mode     TEXT,
    rules_dir     TEXT,
    elapsed_sec   REAL,
    exit_code     INTEGER,
    error_msg     TEXT
);
CREATE INDEX idx_cl_date ON classify_stats(ts_date);
CREATE INDEX idx_cl_task ON classify_stats(task_id);
CREATE INDEX idx_cl_in_root ON classify_stats(in_root);
```

**字段中文注释**：

| 字段 | 类型 | 中文含义 | 示例 |
|---|---|---|---|
| `id` | INTEGER PK | 自增主键 | 1, 2, ... |
| `ts` | TEXT | 完成时间 | `2026-07-18 15:10:22` |
| `ts_date` | TEXT | 完成日期 | `2026-07-18` |
| `host` | TEXT | 机器名 | `DESKTOP-M7ET7A6` |
| `task_id` | TEXT | 一轮分类的短 uuid | `c1d9e2f3a4b5` |
| `in_root` | TEXT | 输入根（GUI 里"输入根"框） | `D:\切帧结果` |
| `out_root` | TEXT | 输出根（GUI 里"输出根"框） | `D:\分类结果` |
| `dir_path` | TEXT | 具体这个 camera 目录 | `D:\切帧结果\...\camera07` |
| `total` | INTEGER | 目录里图片总数 | `114` |
| `classified` | INTEGER | 成功分类到某桶的张数 | `98` |
| `skipped` | INTEGER | 未命中任何规则、跳过的张数 | `10` |
| `failed` | INTEGER | 处理失败的张数（读取错/copy 错） | `6` |
| `bucket_json` | TEXT | 各桶命中数量的 JSON（未来加桶不用 ALTER TABLE） | `{"骑行":20,"步行":15,"前备箱":8,...}` |
| `copy_mode` | TEXT | 输出方式 | `copy` / `move` / `symlink` |
| `rules_dir` | TEXT | 用了哪套规则目录 | `D:\pic-clear\rules_v3` |
| `elapsed_sec` | REAL | 本目录耗时（秒） | `45.1` |
| `exit_code` | INTEGER | 0=成功 | `0` |
| `error_msg` | TEXT | 失败原因 | `rules dir not found` |

**为什么 bucket_json 不拆列**：
- 当前 6 大桶（骑行/步行/前备箱/前机盖/手势/遮挡）后期可能加
- JSON 一列存字典，查询用 `json_extract(bucket_json, '$.骑行')` 语法（SQLite 3.38+ 支持）
- 表格 GUI 里显示时解析成小柱状图

---

### 2.4 `schema_version`（迁移用）

```sql
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version(version) VALUES (1);
```

未来加列走 `ALTER TABLE ... ADD COLUMN`（SQLite 支持），schema_version bump。
`open_stats_db()` 里检测版本 → 缺什么列就补什么列。

---

## §3 单机并发写入策略（重点）

**背景**：一台机器上 GUI 会同时启动多个子进程（extract_gui/dedupe_gui/classify_gui 各自并发跑），所有子进程都往**同一个** `stats_<host>.db` 写。SQLite WAL 模式允许多 readers + 单 writer，两个 writer 撞上就 `SQLITE_BUSY`。

**四层防御**：

### 3.1 `PRAGMA busy_timeout=5000`（第一层，SQLite 自己重试）

打开连接立刻设，SQLite 遇到 BUSY 会自旋重试 5 秒。
对我们几毫秒级的 INSERT 来说，5 秒 = 天文数字，99% 场景够。

### 3.2 短连接（第二层，别霸占锁）

**每次调用 record_* 都 open → insert → commit → close**，绝不长连接。
理由：长连接持有 shared lock 期间，另一个 writer 拿不到 exclusive lock → 立刻 BUSY，busy_timeout 也救不了（因为 shared 一直没释放）。

```python
def record_extract(**kwargs):
    with sqlite3.connect(_db_path()) as conn:   # 用完就关
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("INSERT ...", (...))
        # with 块结束自动 commit + close
```

### 3.3 Python 层 retry（第三层，兜底 5 秒还不够的极端情况）

```python
for i in range(5):
    try:
        _do_insert()
        return
    except sqlite3.OperationalError as e:
        if "locked" not in str(e).lower():
            raise
        time.sleep(0.1 * (i + 1))   # 100ms, 200ms, ..., 500ms
# 仍失败 -> 打 stderr 日志, 静默吞掉, 不能让统计失败搞崩主流程
```

### 3.4 静默失败（第四层，落库是副产物）

- 记录函数最外层 try/except，任何异常打 stderr 就返回
- 抽帧/去重/分类主流程**绝不因为落库失败中断**

**为什么这套够用**：
- 单次 INSERT 是 SQLite 里最快的操作（<1ms）
- 就算 10 个 writer 同时排队，5 秒内轻松走完
- 遇到 SMB 磁盘诡异问题（不应该，因为库在本地），有 retry 兜底
- 真炸也是"少一条统计"，不影响业务

**关键约束**：
- 不要用 executemany 做超大批量 INSERT（会长时间持锁），如果 dedupe 一次要写多条，用循环单条 INSERT
- 不要在事务里做 IO（open 文件、subprocess 之类），事务只包 SQL

---

## §4 API 模块：`stats_db.py`

**统一封装**，四个入口（extract_frames / dedupe_pic / classify_pic / stats_viewer_gui）都调这个模块。

```python
# stats_db.py 大致 API

def open_stats_db(path: Path | None = None) -> Path:
    """打开或初始化. path 默认 ~/.pic-clear/stats_<host>.db.
    首次调用建表 + 开 WAL + 设 busy_timeout. 返回最终路径.
    静默失败: 拿不到路径就打 stderr, 返回 None."""

def record_extract(
    *, video_path: str, output_dir: str, frames: int,
    duration_sec: float | None = None, fps: float | None = None,
    source_root: str | None = None, task_id: str | None = None,
    naming_style: str | None = None, seq_digits: int | None = None,
    elapsed_sec: float | None = None,
    exit_code: int = 0, error_msg: str | None = None,
    db_path: Path | None = None,
) -> None:
    """抽帧成功/失败都调. 内部走 retry + 静默 fail."""

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

def record_classify(
    *, dir_path: str, total: int, classified: int,
    in_root: str | None = None, out_root: str | None = None,
    task_id: str | None = None,
    skipped: int | None = None, failed: int | None = None,
    bucket_counts: dict[str, int] | None = None,   # 自动 json.dumps
    copy_mode: str = "copy",
    rules_dir: str | None = None,
    elapsed_sec: float | None = None,
    exit_code: int = 0, error_msg: str | None = None,
    db_path: Path | None = None,
) -> None: ...

# 查询 API (viewer 用)
def query_extract(...) -> list[dict]: ...
def query_dedupe(...) -> list[dict]: ...
def query_classify(...) -> list[dict]: ...
def sum_by_day(kind: str, ...) -> list[tuple[str, int]]: ...  # (date, count)
```

**特点**：
- 所有 record_* 内部 try/except **静默失败**（打 stderr 但不抛），DB 挂了不影响主流程
- `db_path=None` 走默认，多机部署零改动
- record_* **不长连接**，每次自行 open/close

---

## §5 GUI 查看工具：`stats_viewer_gui.py`

**打包成独立 exe** `stats_viewer_gui.exe`，跟 dedupe_gui 一样的样式（关于 tab、footer 状态栏、日志 tab 等）。

### 5.1 界面结构

```
┌──────────────────────────────────────────────────────┐
│ [Tab] 抽帧 | 去重 | 分类 | 每日趋势 | 日志 | 关于     │
├──────────────────────────────────────────────────────┤
│ 扫描目录: [~/.pic-clear/         ] [浏览] [刷新]      │
│ 已加载: stats_HOST1.db (12345 条) stats_HOST2.db (6789 条) │
│                                                       │
│ 日期: [2026-07-01] 到 [2026-07-18]                    │
│ 主机: [全部 ▼]    关键字: [_______]  [过滤]          │
├──────────────────────────────────────────────────────┤
│ 合计: 抽帧 12345 视频, 234567 张 / 总耗时 6.5h        │
├──────────────────────────────────────────────────────┤
│  ts        host   source_root   video   frames       │
│  2026-...  H1    Z:\sjbz_...   1.mp4    179          │
│  ...                                                  │
├──────────────────────────────────────────────────────┤
│ [导出 CSV] [清理选中记录] [关闭]                     │
└──────────────────────────────────────────────────────┘
```

### 5.2 五个业务 Tab

1. **抽帧统计**：表格 + 顶部合计（"今天 xxx 张 / 本周 xxx 张 / 全部 xxx 张"）
2. **去重统计**：同上，字段换成 total/deleted/remain
3. **分类统计**：同上，字段 total/classified/skipped/failed + bucket 分布小柱状图
4. **每日趋势**：折线图 —— 横轴日期，纵轴每天张数
   - **3 条曲线**：抽帧张数（蓝）/ 去重删除数（红）/ 分类张数（绿）
   - 用 `matplotlib` embed 到 tkinter（PyInstaller 打包能过）
5. **日志**：查看器自己的 log tab（复用 render_log_tab helper）

### 5.3 关于 Tab

- 复用 v0.4.48 的样式：标题 + 授权信息 + 内嵌模块版本
- 显示 `stats_viewer_gui` 自己版本 + 各扫到的 db 文件路径

### 5.4 交互

- 「扫描目录」→ 用 `glob(dir + '/stats_*.db')` 找所有库 → ATTACH 后聚合查询
- 「刷新」→ 重扫 + 重跑当前过滤 SQL
- 「导出 CSV」→ 把当前表格里的数据存 CSV
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
- matplotlib：新增 CI 依赖，**只在 `stats_viewer_gui` 的 workflow 里** `pip install matplotlib`
- **注意**：dedupe_pic / extract_frames / classify_pic 只写库不看图，**不引入 matplotlib**

### 7.2 workflow

- 新 workflow `.github/workflows/build-stats-viewer-gui-exe.yml`
- 新增 `stats_db.py` 要 copy 到所有涉及的 build 文件夹：
  - `build-dedupe-pic-exe.yml`（dedupe_pic.exe 直接写库）
  - `build-extract-frames-exe.yml`（extract_frames.exe 直接写库）
  - `build-classify-gui-exe.yml`（classify_pic 是 import 进 GUI 的，跟 GUI 一起打）
- pyarmor gen 一起加密

---

## §8 实施步骤（分阶段，避免大爆炸）

### Phase 1：底层写库（v0.4.49）
1. 新建 `stats_db.py` — API + 建表 + 并发防御四层 + 兼容层
2. `extract_frames.py` 每抽完一个视频调 `record_extract`
3. `dedupe_pic.py` 每处理完一个目录调 `record_dedupe`
4. `classify_pic.py` 每处理完一个 camera 目录调 `record_classify`
5. 三个 workflow 加 copy + pyarmor
6. **验证**：分别跑一次抽帧 / 去重 / 分类 → 用 `sqlite3` 命令行看库有没有数据

### Phase 2：GUI 传 task_id 与源根（v0.4.50）
1. `extract_gui` / `dedupe_gui` / `classify_gui` 启动子进程/子任务时生成 task_id + 通过命令行或函数参数传下去
2. 让 SQL 能按任务归组查询

### Phase 3：查看器（v0.4.51）
1. ✅ `stats_viewer_gui.py` — 5 Tab（抽帧/去重/分类/每日趋势/日志）
   - 每个数据 Tab 都有: 汇总柱状图 + 明细表格
   - 每日趋势 Tab 是 3 条折线（抽帧/去重/分类）
2. ✅ `.github/workflows/build-stats-viewer-gui-exe.yml`
3. 使用方法：
   - 本地：`python stats_viewer_gui.py`
   - Windows exe：从 Release 下载 `stats_viewer_gui.exe`
   - 默认扫 `~/.pic-clear/stats_*.db`，UI 里也能改扫描目录（多机同步汇总用）
4. matplotlib 是**软依赖**：装了则显示图表；没装则图表 Tab 显示提示文字，其余功能仍可用

### Phase 4：图表（v0.4.52）
1. matplotlib embed 每日趋势 Tab（3 条曲线）
2. 分类 Tab 里的 bucket 分布小柱状图
3. 导出 CSV

### Phase 5（可选）：老 CSV 一键导入 SQLite

---

## §9 注意事项 / 已知陷阱

1. **hostname 里的中文/特殊字符**：过滤成 `[A-Za-z0-9_-]`，别让文件名炸掉
2. **DB 打不开时**：所有 record_* 静默 fail + stderr 打日志，不能因为落库失败搞崩主流程（这是抽帧/去重/分类工具，落库是**副产物**）
3. **DB 文件误删/迁移**：`open_stats_db` 每次都 `CREATE TABLE IF NOT EXISTS`，删了自动重建，只是丢历史
4. **PyInstaller onefile 打包 sqlite3**：标准库自带，通常不用 `--hidden-import`，但保险起见加 `--hidden-import sqlite3`
5. **matplotlib 首启很慢**（~2s）：查看器 GUI 里图表 Tab **首次点开再加载**，别启动时预加载
6. **时间字段用文本**：不用 UNIX epoch，直接存 `datetime.now().strftime('%Y-%m-%d %H:%M:%S')`，SQLite 里 lexicographic 顺序等于时间顺序，比较友好
7. **write 失败重试**：`sqlite3.OperationalError: database is locked` retry 5 次，每次 sleep 100~500ms 递增；仍失败就吞掉，别抛
8. **不要长连接**：每次 record 都 open → insert → commit → close，用 `with sqlite3.connect(...)` 一步搞定
9. **不要事务里做 IO**：事务只包 SQL；文件读、subprocess、网络等一律在事务外
10. **classify 的 `bucket_json` 字段查询**：SQLite 3.38+ 支持 `json_extract(bucket_json, '$.骑行')`，PyInstaller 打包出的 sqlite3 通常够新；不够就在 Python 层解析

---

## §10 数据留存策略

- 无自动清理（磁盘占用不大，10w 条 ~ 20MB）
- GUI 提供「清理选中记录」按钮，用户自己删
- 未来可加"保留 90 天，超期归档" 但不着急

---

## §11 与 Marker 机制的关系

- **Marker（`_done.marker` / `_dedup_done.marker` / `_classify_done.marker`）** = "干过了没有"的**布尔位**，粒度到目录，用于断点续跑
- **stats.db** = "干了多少"的**明细记录**，粒度到目录/视频，用于统计汇总
- 两者互不干扰：Marker 写在 markers_root，stats 写在 `~/.pic-clear/`
- 断点续跑时（Marker 已在）**不会再写 stats**（因为不会重跑），这是正确的语义

---

**审阅确认**：这份文档 OK 就开工 Phase 1。有想改的告诉我。

---

## §12 最终表 vs 流水表（v0.4.89 新增）

### 背景

测试阶段经常对**同一批视频/目录反复切帧/去重/分类**（调参、验证修复、回归）。
如果统计工具直接按 `extract_stats` 汇总，同一视频会被算 N 次，
"总帧数 / 总删除数" 全部虚高。

### 方案：一份流水 + 一份最终

每种任务同时维护两张表：

| 用途 | 流水表 | 最终表 |
|---|---|---|
| 抽帧 | `extract_stats` | `extract_final` |
| 去重 | `dedupe_stats`  | `dedupe_final`  |
| 分类 | `classify_stats`| `classify_final`|

- **流水表**：每次任务一条 `INSERT`，不去重。审计 / 回放 / 追问「上周三那次跑了啥」用
- **最终表**：一目标一行 `INSERT ... ON CONFLICT DO UPDATE`，最新一次覆盖旧值。统计汇总用

### 主键 & 唯一性

- `extract_final.video_md5` —— **视频内容快速指纹**，见下方
- `dedupe_final(dir_path, host)` —— 同一目录在不同机器上算不同记录，因为多台机器可能各自处理不同批次
- `classify_final(camera_dir, host)` —— 同上

### 抽帧快速指纹 (`extract_frames._video_fingerprint`)

- `sha1( str(size) + head(1MB) + mid(1MB) + tail(1MB) )`
  - 文件 <1MB：只 hash size + 全文件
  - 文件 <3MB：hash size + head + tail
  - 文件 ≥3MB：hash size + head + mid + tail
- 目的：几毫秒完成，避免大视频算全量 md5
- 权衡：理论上有极小概率误判两不同视频为同一 md5，实测场景（不同录制片段）冲突可忽略
- 失败静默返回 `(None, None)` → 该视频只写流水表、不进最终表，主流程不受影响

### 版本字段

**流水表和最终表都有 `version` 列**（最终表叫 `last_version`），
CI 会在打 tag 时把 git tag 写进 `_version.py`，再由三个工具透传到 record_*。

- 流水表：每条都有 version → 排查"哪个 tag 出的问题"
- 最终表：`last_version` = 最近一次跑那个 tag，`first_ts` / `last_ts` 可以看时间跨度

### UPSERT 会更新哪些字段？

以 `extract_final` 为例，重复切同一视频时：

- **覆盖**：`video_path` / `frames` / `fps` / `quality` / `last_ts` / `last_host` /
  `last_task_id` / `last_version` / `last_stage` / `last_elapsed_sec` / `last_msg`
- **累加**：`run_count = run_count + 1`
- **保留**：`first_ts`（首次落库时间，永不变）

去重 / 分类 final 表规则对称。

### 查询端 (`stats_viewer_gui`) 行为

- 顶部工具条新增「视图」下拉，`最终 / 流水` 二选一，默认「最终」
- 「最终」模式：
  - 抽帧/去重/分类三个 Tab 直接查 `*_final`
  - 表格新增两列「重复次数」`run_count` 和「版本」`version`
  - 多机聚合：抽帧按 `video_md5` 合并（取 `last_ts` 最新的那条），
    去重/分类主键含 `host` 天然分开
- 「流水」模式：回退到查 `*_stats`，跟老行为一致
- 老 db（升级前的）没有 `*_final` 表 → 静默跳过、不报错

### 兼容性

- **老流水表**（`extract_stats` / `dedupe_stats` / `classify_stats`）**结构不动**，历史数据继续可用
- **新增字段全在新表**，不改老 schema，`SCHEMA_VERSION` 从 1 升到 2
- 新表首次访问时自动 `CREATE TABLE IF NOT EXISTS`，无需迁移脚本
- 主流程（抽帧/去重/分类）静默失败原则保留：final 表写失败不影响流水表，两者都失败不影响任务本身

### 血泪教训归档

以前 `stats_viewer_gui` 表格里"总帧数"会随着重复切帧线性膨胀，
测试阶段甚至看到一个 100 帧的视频显示 5000 帧（切了 50 次），
用户在群里问"这机器怎么这么牛"—— 从此有了最终表。

---

## §13 机器识别字段：`machine_fp` + `local_ip`（v0.4.89 追加）

### 背景

多台机器共同处理任务（本地 Mac / 堡垒机 Win / 用户 Win 测试机 / 追签机器），
只靠 `host = socket.gethostname()` 很难分辨：

- hostname 可能重名（`DESKTOP-XXXX` 太随机、有时全一样）
- 机器换网卡 / VPN / 迁盘时无法追溯
- 授权侧已有指纹，但流水表里没落库 → 出问题查授权 vs 查任务对不上

### 落库字段（6 张表 + task_runs 都加）

| 字段 | 类型 | 含义 | 示例 |
|---|---|---|---|
| `machine_fp` | TEXT | licensing 指纹（`板号 + 盘号 + hostname` 的 sha256 前 16 位） | `F260-BD4F-30EA-C616` |
| `local_ip`   | TEXT | 本机默认路由出口 IPv4 | `192.168.1.23` |

在 final 表里叫 `last_machine_fp` / `last_local_ip`（跟 `last_ts` / `last_version` 对称）。

### 为什么两个都要留

- 指纹是**硬件级**唯一标识（板号 + 盘号），换网络不变
- IP 反映**当次运行的网络位置**，方便群里同事快速指认："那台 192.168.1.23 的机器"
- 有时候硬件指纹在虚拟机 / 云主机上会变（板号不稳），IP 兜底
- 反过来，指纹稳定但没 IP 不好口头交流，两个互补

### 取值逻辑（`stats_db._machine_fp` / `_local_ip`）

**指纹**：进程内**永久缓存**（硬件指纹在一个进程生命周期内不会变）：

- 调 `licensing.get_fingerprint()`
- 首次 ~18ms（走 `wmic` / `system_profiler` subprocess），之后走缓存 <1μs
- licensing 未打包 / subprocess 挂掉 → 返回 `""`，主流程不受影响

**IP**：**30 秒 TTL** 缓存（防止 wifi 切换 / 拔插网线时长时间跑不更新）：

- 经典 UDP `connect("8.8.8.8", 80)` + `getsockname()` 拿默认路由出口 IP
- **不发实际数据包**（UDP connect 只走内核路由决策）
- 断网 fallback 到 `gethostbyname(hostname)`
- 都挂 → 返回 `""`

### 老库自动升级（`_ensure_columns`）

SQLite 的 `ALTER TABLE ADD COLUMN` **不支持** `IF NOT EXISTS`，
所以先 `PRAGMA table_info(<table>)` 拿现有列名，缺哪列补哪列，幂等安全。

覆盖表：`extract_stats` / `dedupe_stats` / `classify_stats` /
`extract_final` / `dedupe_final` / `classify_final` / `task_runs`。

**老数据不回填**：v0.4.89 之前写的行，`machine_fp` / `local_ip` 就是 `NULL`（历史无法补）。

### 查看器展示

`stats_viewer_gui` 三个 Tab 表格都新增两列：

- 「指纹」：显示 `machine_fp`（16 字符带连字符），列宽 150
- 「IP」：显示 `local_ip`（IPv4），列宽 110

流水视图直接读 `machine_fp` / `local_ip`；
最终视图从 `last_machine_fp` / `last_local_ip` 映射（下游 render 代码零改动）。

### 已知陷阱

1. **licensing 未随打包一起 hidden-import** → `_machine_fp()` 静默返回空。三个 exe workflow 已经带 licensing，理论上不会踩，但记住这条 fallback
2. **虚拟机 / Docker** 板号可能全一样 → 指纹碰撞。这时候 IP 是唯一区分手段（IP 一般不会撞）
3. **VPN / 代理**：`local_ip` 可能是 VPN 网段的 IP（10.x / 100.x），不是物理 LAN IP。语义上仍是"这个进程出口 IP"，符合预期
4. **`_local_ip` 首次 500ms 超时**：设了 `s.settimeout(0.5)`，极端情况（防火墙拦截 UDP）也不会卡住主流程
