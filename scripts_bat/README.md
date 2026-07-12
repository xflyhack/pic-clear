# 自动化 bat 脚本

给堡垒机 Windows 用的一键脚本。把两个 exe 放到系统 PATH（推荐 `C:\Windows\System32`），bat 自动定位 `Z:\sjbz_*` 源目录，让你**挑选要处理的子目录**，把结果写到 `Z:\切帧结果\` 下、层级完全对齐。

## 前置准备（一次性）

1. 把 `extract_frames.exe`、`dedupe_pic.exe`、`license.lic` 复制到 `C:\Windows\System32\`
   - 或者放到别的目录，然后把该目录加到 `PATH`
   - **`license.lic` 必须和 exe 同目录**
2. 把三个 bat 复制到桌面：
   - `run_all.bat` —— 一键跑完抽帧+去重
   - `step1_extract.bat` —— 只抽帧
   - `step2_dedupe.bat` —— 只去重
3. 建议永久关闭 cmd 的"快速编辑模式"：
   右键 cmd 窗口标题栏 → 属性 → 编辑选项 → 取消勾选 **快速编辑模式**。

## 使用流程

双击任一 bat：
1. 自动定位 `sjbz_*` 根目录（多个则列表选一个）
2. 列出根目录下一级子目录，你选：`1,2` / `1-3` / `all`
3. 依次处理已选子目录，输出目录 = `%OUT_ROOT%\<sjbz>\<子目录>`

## 进度提示

- 每次调 exe 之前，bat 会打一行 `[HH:mm:ss] START extract_frames.exe ...`，方便看进度
- **`extract_frames.exe`** 的输出粒度：
  - 启动：参数打印
  - `[扫描] 找到 N 个待处理视频`
  - 每个视频：`[i/N] xxx.h265 (已用 xxs, 剩余 ~yys)` + `...抽帧中，请稍候`
  - **单个视频抽帧过程中 ffmpeg 是静默的**（几秒到几十秒），别以为卡住
- **`dedupe_pic.exe`** 的输出粒度：
  - `[扫描+检测] 50/599 (8.3%) 速率 3.0/s 已用 17s 剩余 ~3.1m 受保护 31`
  - 每处理若干张打一次，逐张更新
  - **首次加载 YOLO 模型有 0.2-2s 静默**，正常
- **默认无实时日志文件**（cmd 里 tee 不好写）。想留档：cmd 窗口"属性"里勾"编辑 → 启用行换行选择"，跑完手动全选复制。

## 断点续跑 / 增量覆盖

**`extract_frames.exe`（v3+）已支持断点**：
- 每个视频抽完成功后，会在输出目录里写一个空的 `_done.marker`
- **下次再跑**：目录里有 `_done.marker` → 直接跳过（打印 `跳过（已完成）`）
- **半成品**（目录里有 `frame_*.jpg` 但没 `_done.marker`）→ 自动清空重抽
- 想强制全部重抽：加 `--no-skip-existing`

结论：
- **中途关掉窗口 / 网络掉线 / 电脑重启，再跑同一条命令就是断点续跑**
- **目标目录已经有旧图**也没关系，会智能处理（完整的跳过，半成品清空重抽）

**`dedupe_pic.exe`**：
- 天生可重跑：每次都扫、每次都出报告
- 上次软删的图片已经在 `_trash_` 里，下次扫不到，不会重复处理
- **随便重跑几次都没关系**（dry-run 更是想跑就跑）

## 需要改盘符 / 目录前缀 / 输出根？

改 bat 开头三行：
```bat
set "DATA_DRIVE=Z:"
set "OUT_ROOT=%DATA_DRIVE%\切帧结果"
set "DATA_PREFIX=sjbz_"
```

## 常见问题

- **窗口卡住不动**：cmd 快速编辑模式，按一下 Enter / Esc 恢复；或永久关闭（见前置准备）
- **提示"找不到 exe"**：exe 不在 PATH。放 `C:\Windows\System32` 或改 bat 里为绝对路径
- **license.lic 报错**：license 必须和 exe 同目录
- **改抽帧参数**：改 bat 里 `extract_frames.exe ... --fps 1 --ext .h265`
- **改相似阈值**：改 `--threshold 3`，值越小越严格
