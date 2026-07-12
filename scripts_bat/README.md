# 自动化 bat 脚本

给堡垒机 Windows 用的一键脚本。用途：把两个 exe 放到系统 PATH（推荐 `C:\Windows\System32`），bat 就自动找 `Z:\sjbz_*` 源目录，把结果写到 `Z:\切帧结果\`。

## 前置准备（一次性）

1. 把 `extract_frames.exe`、`dedupe_pic.exe`、`license.lic` 三个文件复制到 `C:\Windows\System32\`
   - 也可以放到别的目录，然后把该目录加到 `PATH`
   - **`license.lic` 必须和 exe 同目录**
2. 把这三个 bat 复制到桌面（或任何顺手的地方）：
   - `run_all.bat` —— 一键跑完抽帧+去重
   - `step1_extract.bat` —— 只抽帧
   - `step2_dedupe.bat` —— 只去重（对抽帧结果目录）
3. 建议永久关闭 cmd 的"快速编辑模式"（避免鼠标点击导致卡住）：
   - 打开任意 cmd 窗口 → 右键窗口标题栏 → 属性 → 编辑选项 → 取消勾选 **快速编辑模式** → 确定并保存

## 使用

**首选**：双击 `run_all.bat`
- 自动切到 `Z:`，找 `sjbz_*` 目录（唯一就用，多个让你选）
- 输出目录：`Z:\切帧结果\<sjbz_目录名>\`
- 抽帧完自动进入去重的 dry-run
- dry-run 出报告后按 `Y` 才真删，按 `N` 只保留报告

## 需要改盘符 / 目录前缀？

打开 bat，改开头这三行就行：

```bat
set "DATA_DRIVE=Z:"
set "OUT_ROOT=%DATA_DRIVE%\切帧结果"
set "DATA_PREFIX=sjbz_"
```

## 常见问题

- **窗口卡住不动**：cmd 快速编辑模式，按一下 Enter 或 Esc 恢复；或按上文永久关闭。
- **提示"找不到 exe"**：exe 不在 PATH。要么放 `C:\Windows\System32`，要么修改 bat 里的 exe 路径为绝对路径。
- **license.lic 报错**：license 需要和 exe 在同一目录。
- **想改抽帧参数（fps / 扩展名）**：修改 bat 里 `extract_frames.exe ...` 那一行。
- **想改相似阈值**：修改 bat 里 `--threshold 3`，越小越严格（要求越接近才算重复）。
