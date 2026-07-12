# pipeline.exe 使用说明

`pipeline.exe` 是**编排层**，替代 `scripts_bat/*.bat`。
把它和 `extract_frames.exe` / `dedupe_pic.exe` / `license.lic` 一起放到 PATH（推荐 `C:\Windows\System32`）即可。

## 目录布局

```
C:\Windows\System32\
├── extract_frames.exe   （不变）
├── dedupe_pic.exe       （不变）
├── pipeline.exe         （新增，约 10-20 MB）
└── license.lic
```

每次提交任务后，pipeline 会在 `Z:\切帧结果\.pipeline\jobs\<job_id>\` 下建目录，
存 `manifest.json`（提交参数）、`status.json`（实时状态）、`pipeline.log`（编排日志）、
`worker.log`（子进程 stdout+stderr）、`reports\<子目录>_report.csv`。

## 常用命令

### 提交任务

**交互模式**（自动列 sjbz_* 和子目录，让你选）：
```
pipeline.exe submit
```

**无交互模式**（写脚本调用时用）：
```
pipeline.exe submit --auto ^
    --src Z:\sjbz_20260708 ^
    --subs 1,2 ^
    --threshold 3
```

`--subs` 支持：
- 序号列表：`1,2`
- 区间：    `1-3`
- 全部：    `all`
- 具体名字：`1,3,VLM`（跳过内部索引解析）

**默认行为**：只做 dry-run，产出 CSV 报告，不真删。
想真删加 `--apply`。

提交后 pipeline 会 **立刻返回**（后台 detach 运行），关掉 cmd 窗口任务也在跑。

### 查看进度

```
pipeline.exe list                 # 列所有历史/运行中任务
pipeline.exe status               # 看最近一个任务的详情
pipeline.exe status <job_id>      # 看指定任务
```

`status` 会打印每个子目录的 stage：
- `pending`      未开始
- `extracting`   抽帧中
- `done`         抽帧完成（去重可能还在后台跑）
- `failed`       失败（看 `note` 字段）
- `skipped`      跳过

以及**视频级的实时计数**：
- `抽帧=N`   已抽完的视频数（有 `_done.marker`）
- `去重=M`   已去重的视频数（有 `_dedup_done.marker`）

**并行工作原理**（v2 版本起）：
- 主线程串行调用 `extract_frames.exe`，它内部把每个视频抽完后写 `_done.marker`
- Watcher 线程循环扫描（每 3 秒），发现 `_done.marker` 但没 `_dedup_done.marker` 的目录 → 立刻跑 `dedupe_pic.exe`，成功后写 `_dedup_done.marker`
- 抽帧和去重天然并行，视频粒度即抽即删，磁盘占用最小
- 断点：`_done.marker` / `_dedup_done.marker` 就是断点，再跑一次自动跳过已完成的

### 看日志

```
pipeline.exe logs                   # 打印最近一个任务的 worker.log
pipeline.exe logs <job_id>          # 指定任务
pipeline.exe logs <job_id> -f       # tail -f 模式，实时跟踪
pipeline.exe logs <job_id> --which pipeline    # 只看编排层日志（不含子进程输出）
```

### 停止任务

```
pipeline.exe stop <job_id>          # 优雅停止 worker
```

### 查指纹 / 申请 license

```
pipeline.exe --fingerprint
```

打印 `XXXX-XXXX-XXXX-XXXX`，把这串发给作者要 license.lic。

## 常见问题

- **`pipeline.exe list` 报 "无有效授权"**：把 license.lic 放到 `pipeline.exe` 同目录。
- **worker 突然挂了**：`pipeline.exe status` 看 `alive`；`pipeline.exe logs -f` 看 worker.log。
- **想改盘符**：submit 时加 `--data-drive Y:` `--out-root Y:\切帧结果`。
- **同时跑多个任务**：pipeline 不限制并发，你可以连续 `submit` 多次，每次一个独立 job_id。
  但同时跑多个会挤网络盘带宽，建议按需。

## 与 scripts_bat 的关系

- `scripts_bat/*.bat`：保留，用于**前台交互**场景（Y/N/A 逐个确认真删）
- `pipeline.exe`：**后台运行 + 集中日志**场景

两者可以共存，看你哪种更顺手。
