# AGENTS.md（本仓库给 AI 助手的说明）

## 沟通语言
- 默认中文回复
- Git 提交注释用中文，格式：`类型(范围): 中文描述`（示例：`修复(bat): xxx`、`新增(pipe_gui): xxx`）

## 写 bat 脚本时必读

**任何情况下写新的 `.bat`，先看 `docs/bat_conventions.md`。**

**必须遵守的**（重复踩过的坑）：

1. **UTF-8 + BOM + CRLF**
2. **头部必须写两次 `@echo off`**（BOM 会让第一次失效）：
   ```bat
   @echo off
   setlocal EnableExtensions EnableDelayedExpansion
   >nul chcp 65001
   @echo off              ← 这一行是关键，不能省
   title 你的脚本名
   ```
3. `for` 循环内用延迟展开 `!var!`，必须 `EnableDelayedExpansion`
4. 用 `REM` 而不是 `::` 写注释

## 目录结构关键说明

- `pipeline.py` → 编排层，打包成 `pipeline.exe`（`--console`）
- `pipe_gui.py` → GUI 前端，打包成 `pipe_gui.exe`（`--windowed`）
- `extract_frames.py` / `dedupe_pic.py` → 独立打包的干活 exe
- `licensing.py` → 授权模块（RSA-PSS 签名 + 机器指纹）
- `scripts_bat/*.bat` → 4-5 个批处理脚本
- `docs/*.md` → 各功能使用说明

## 打包相关

- 三个 exe 各有独立 workflow：`.github/workflows/build-*.yml`
- 用 PyArmor 加密字节码 + PyInstaller onefile
- **私钥 `secrets/private.pem` 已提交（仓库私有）**

## 交互风格约束

- **先说思路再动手**：涉及超过一个文件或非平凡改动时，先描述方案让用户拍板
- **不要瞎改**：用户明说"不要直接改"、"先看看代码"时，只诊断/展示，不 `apply_patch`
- **提交注释中文**、简短、格式化

## 私钥位置
- `~/.dedupe_pic_keys/private.pem`（作者 Mac）
- `secrets/private.pem`（仓库内，仅供 CI）
- 签发命令：`python gen_license.py <fingerprint> --issued-to <name>`

## 已签发的机器指纹（信任列表）

| 指纹 | 归属 |
|---|---|
| `CCFE-FF07-E560-9D30` | 用户 Mac 开发机 |
| `F260-BD4F-30EA-C616` | 堡垒机 Windows |
| `A0A0-6D01-06EF-C18E` | 本地 Win 测试机 |
| `E915-F232-792C-5B41` | 追加签发 |
