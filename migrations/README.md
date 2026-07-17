# migrations

pic-clear MySQL 迁移目录。

## 用法

```bash
# 需要装 PyMySQL（推荐）
pip install pymysql

# 直接执行所有未跑的迁移
python migrations/migrate.py

# 只看状态
python migrations/migrate.py --status

# 换库名 / 换机器
python migrations/migrate.py \
    --host 127.0.0.1 --port 3306 \
    --user root --password '' \
    --db pic_clear
```

也支持环境变量：`PIC_CLEAR_DB_HOST` / `PORT` / `USER` / `PASSWORD` / `NAME`。

## 约定

- 每个迁移文件名 `NNNN_描述.sql`，按字典序执行。
- 已执行的文件名记录在 `pic_clear.schema_migrations`，重复运行只跑未执行的。
- 每个文件都要**自身幂等**（`CREATE ... IF NOT EXISTS`、`ALTER ... IF NOT EXISTS`
  等），避免手工修表后再跑挂掉。
- 已执行过的迁移**不要再改**，需要修正就新建一个 `NNNN_fix_xxx.sql`。

## 当前迁移

| 文件 | 说明 |
|---|---|
| `0001_init_license_issues.sql` | 建 `pic_clear` 库和 `license_issues` 表（网页签发历史） |
