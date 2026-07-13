# 自动化 bat 脚本

> **给开发/AI 助手**：新写 bat 之前先看 [`../docs/bat_conventions.md`](../docs/bat_conventions.md)，
> 里面记录了 UTF-8 BOM + CRLF + 双 `@echo off` 等踩坑约定。


给堡垒机 Windows 用的一键脚本。把两个 exe 放到系统 PATH（推荐 `C:\Windows\System32`），bat 自动定位 `Z:\sjbz_*` 源目录，让你**挑选要处理的子目录**，把结果写到 `Z:\切帧结果\` 下、层级完全对齐。

> 想要**后台运行 + 关窗口不中断 + 集中日志**？请看 `docs/pipeline_exe.md`（`pipeline.exe`）。
> 本目录下的 bat 保留，用于**前台交互 + 逐个 Y/N 确认删除**的场景。

> 想要**抽帧和去重同时跑、磁盘不攒垃圾**？跑 `run_all.bat` 时选"启动去重监听窗口"，
> 会自动 `start` 一个 `dedupe_watcher.bat` 后台窗口，看到 `_done.marker` 就去重。

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

## 只想看运行状态？

双击 `check_status.bat`：
- 每 5 秒刷新一次，展示 `pipeline.exe` / `extract_frames.exe` / `dedupe_pic.exe` 的进程状态（PID、内存）
- 顺带展示 `Z:\切帧结果\.pipeline\jobs\` 下**最新一个任务**的进度（job_id、state、done/total、last_message）
- 只读，**不做任何操作**；`Ctrl+C` 退出
- 若 `OUT_ROOT` 不是默认 `Z:\切帧结果`，请打开 bat 改顶部的 `set "OUT_ROOT=..."`

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

## 边抽边删（dedupe_watcher）

**背景**：默认 `run_all.bat` 会先把一个子目录**所有视频**都抽完，再统一去重。
子目录很大时，中间态会占很多磁盘。

**推荐做法**：让**抽帧**和**去重**并行跑，抽完一个视频立即清理：

1. 双击 `run_all.bat`，正常选源目录、子目录
2. 在 "现在启动一个去重监听后台窗口 (Y/N)" 时选 **Y**
3. 会自动弹出一个新的 cmd 窗口，标题是 `dedupe_watcher`
4. `run_all.bat` 只负责抽帧；`dedupe_watcher` 后台循环扫描，看到 `_done.marker` 就去重

**监听窗口原理**：
- 每次 `extract_frames.exe` 抽完一个视频，会在输出目录写一个 `_done.marker`
- `dedupe_watcher.bat` 每 5 秒扫一次输出根目录，找有 `_done.marker` 但没 `_dedup_done.marker` 的目录
- 对每个这样的目录跑 `dedupe_pic.exe`，成功后写 `_dedup_done.marker`
- 抽帧全部完成后，`run_all.bat` 结束；监听窗口继续扫，把剩下的清理完，然后进入空转等待
- 你觉得没事干了，就在监听窗口按 **Ctrl+C** 退出

**默认是 dry-run**（只出 CSV 报告，不真删）。检查报告后想真删：
- 关掉当前监听窗口
- 手工再开一个：`dedupe_watcher.bat "Z:\切帧结果\sjbz_20260708" /apply`
- 也可以加 `/threshold 3`（相似阈值）、`/interval 3`（扫描间隔秒）、`/once`（扫一遍就退）

**手工单独跑 watcher**（不通过 run_all）：
```
dedupe_watcher.bat                              # 监听默认 Z:\切帧结果，dry-run
dedupe_watcher.bat "Z:\切帧结果\sjbz_xxx"      # 只监听指定目录
dedupe_watcher.bat "Z:\切帧结果" /apply         # 真删模式
dedupe_watcher.bat "Z:\切帧结果" /once          # 扫一轮就退，不循环
```

## 排查：dedupe_pic 报 "DLL load failed" / "初始化检测器失败"

**现象**：`dedupe_pic.exe` 一启动就打印：
```
[FATAL] 初始化检测器失败: DLL load failed while importing onnxruntime_pybind11_state: 动态链接库(DLL)初始化例程失败。
```

**原因**：`onnxruntime` 需要 **Microsoft Visual C++ Redistributable 2019+**（`vc_redist.x64.exe`）。堡垒机 Windows Server 上通常没装。

**解决方案（三选一）**：

1. **装 VC++ Runtime**（推荐，一劳永逸）
   - 下载：https://aka.ms/vs/17/release/vc_redist.x64.exe
   - 上传到堡垒机双击装完，重启 cmd 即可

2. **降级到纯 dHash 模式**（快速跑通，但**没有人/车保护**）
   ```
   dedupe_pic.exe <目录> --threshold 3 --allow-no-detector --apply --trash-dir ...
   ```
   或改用 `--no-protect`（一样是纯 dHash）
   ⚠ **风险**：含有人/车/电车的图片可能被误删。仅在能人工复核报告时使用。

3. **让 watcher 跳过失败目录**（自动降级失败重试）
   `dedupe_watcher.bat` v2 遇到 rc≠0 时会写 `_dedup_failed.marker`，
   下次运行**不再重试此目录**。排查完手工删掉该 marker 就恢复。
