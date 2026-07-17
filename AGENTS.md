# AGENTS.md（本仓库给 AI 助手的说明）

## 沟通语言
- 默认中文回复
- Git 提交注释用中文，格式：`类型(范围): 中文描述`（示例：`修复(bat): xxx`、`新增(pipe_gui): xxx`）

## Windows 文件 IO 必读（新工具开发者硬规则）

**任何要在 Windows 上做文件 IO（读图、扫盘、删除、写 marker）的新工具, 先读
`docs/windows_long_path.md`. 覆盖从 v0.4.28 到 v0.4.35 的完整踩坑史,
5 个 tag 的血泪总结.**

**必抄的 4 件事**（细节和模板代码在文档里）:

1. **入口归一化**: `_normalize_windows_path` 一定要有, 修 tkinter 返回 `//?/Z:/...` 的坑
2. **`\\?\` 前缀**: `_to_long_path` 只加 `\\?\`, **别做 UNC 展开** (新 Windows 上反而打不开)
3. **所有 pathlib IO 全套 `_safe_*` helper**: `_safe_stat` / `_safe_unlink` / `_safe_exists` /
   `_safe_is_file` / `_safe_move` —— 只保护 PIL 是不够的
4. **`except Exception: pass` 全删**: 涉及 marker / lock / 状态持久化的静默吞异常一律
   改成 `[ERROR]` stderr 打日志, 断线续跑失效再也无法无声无息

**诊断利器**: 堡垒机上遇到"某路径打不开"先跑 `diag_pic.exe`, 6 种打开方式一次摸清,
比出 exe 迭代猜快 O(10min).

---

## 写 bat 脚本时必读

**任何情况下写新的 `.bat`，先看 `docs/bat_conventions.md`。**

**必须遵守的**（重复踩过的坑）：

1. **UTF-8 无 BOM + CRLF**（2026-07-14 新约定，见 `docs/bat_conventions.md`）
2. **头部三行必须纯 ASCII**，`chcp 65001` 之前不能出现任何非 ASCII 字节：
   ```bat
   @echo off
   setlocal EnableExtensions EnableDelayedExpansion
   >nul chcp 65001
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

---

## 版本号硬规则（禁止硬编码，2026-07-18 起）

**所有需要打包成 exe 的入口（GUI / CLI / pipeline）必须走同一套机制。**

### py 侧写法（新增文件照抄）

```python
# GUI:
try:
    from _version import VERSION as _V
except Exception:
    _V = "dev"
APP_VERSION = _V

# CLI (argparse):
def _read_version() -> str:
    try:
        from _version import VERSION
        return VERSION
    except Exception:
        return "dev"

parser.add_argument("--version", action="version",
                    version=f"my_tool {_read_version()}")
```

### 禁止事项

- ❌ **不许** `APP_VERSION = "v0.3.0"` 硬编码
- ❌ **不许** `__version__ = "1.2.3"` 硬编码
- ❌ **不许** 从 `pyproject.toml` / `setup.py` 读版本
- ❌ **不许** 把 `_version.py` 走 pyarmor gen（明文常量，加密没意义）

### CI 侧强制（新 workflow 必须包含以下 4 步）

1. **`Write _version.py from git ref` 步骤**（在 `Install runtime deps` 之后、
   `Prepare build folder` 之前）：
   ```yaml
   - name: Write _version.py from git ref
     run: |
       if "%GITHUB_REF_TYPE%"=="tag" (
         echo VERSION = "%GITHUB_REF_NAME%" > _version.py
       ) else (
         echo VERSION = "%GITHUB_REF_NAME%-%GITHUB_SHA:~0,7%" > _version.py
       )
       type _version.py
     shell: cmd
   ```
2. 如果有 `Prepare build folder`，加一行 `copy _version.py build_XXX\`
3. pyinstaller 命令加 `--hidden-import _version ^`
4. **不要** 让 `_version.py` 进 `pyarmor gen` 的文件列表

### 效果验证

- **本地跑 py**：`exe --version` 显示 `dev`（因为项目根 `_version.py` 是 `VERSION = "dev"`）
- **CI tag push (`v0.4.30`)**：显示 `v0.4.30`（跟 git tag 一致）
- **CI branch push (`main`)**：显示 `main-ef22322`（分支名 + 7 位 SHA）
- GUI 的"关于"页版本号 = `--version` 输出 = git tag

### 为什么这么严

历史教训：5 个 GUI 各自硬写版本号，
`classify_gui=v0.4.27` / `dedupe_gui=v0.3.0` / `extract_gui=v0.3.1` / `pipe_gui=v0.3.0` / `gen_license_gui=v0.1.10`，
git tag 打到 `v0.4.29` 时用户在堡垒机上截图 dedupe_gui 显示 `v0.3.0`，
**根本分不清是老 exe 还是没升级成功**。以后不允许再犯。

### 新增打包工具时的自检清单

- [ ] py 文件里没有 `= "v0.x.y"` 硬编码
- [ ] py 文件走 `from _version import VERSION` fallback `dev`
- [ ] 新 workflow 有 `Write _version.py from git ref` 步骤
- [ ] 新 workflow 的 pyinstaller 命令带 `--hidden-import _version`
- [ ] 如果用 `Prepare build folder`，有 `copy _version.py build_XXX\`
- [ ] 打 tag 后跑一遍 `exe --version` 验证输出跟 tag 一致

---

## 最近踩过的坑（血泪教训，2026-07-18 起持续更新）

> **每次开工前先扫一眼本节**。这些是最近两周在堡垒机上翻车、
> 修完又翻车、最后才定位到的问题。写下来防止再犯。

### 1. bat 括号块内禁塞 `for /f` + 中文 + 转义符 + `exit /b`
- 现象：堡垒机上报 `'3)' is not recognized as an internal or external command`
- 根因：`if () (... for /f ... ^& exit /b N)` 组合让 cmd 解析阶段误 tokenize
- 修法：改成顶层 label goto（`:TRY_XXX` / `:FAIL` / `:OK`），把 fallback 拆平
- 本地虚拟机不暴露，因为 PowerShell 一级就返回，不进后续块

### 2. bat 里 stdout / stderr 严格分开
- 现象：watcher 里 `[INFO ] 当日累计已删 [append_stats] WARN: ...` 撞散一行
- 根因：被 call 的 bat 用 `echo` 输出诊断到 stdout；调用方 `> tmp` 把 stdout 重定向了
- 铁律：
  - 诊断/警告/错误 **一律** `>&2 echo ...`
  - stdout **只能** 输出协议约定的值（数字 / `-1` / 空）
  - 错误分支 `exit /b N` **之前必须** 先 `echo <协议值>` 到 stdout

### 3. Python PIL `Image.open()` 长路径静默失败
- 现象：`[扫描完成] 有效图片 0, 失败/跳过 N`，整个目录被跳过，不生成 CSV
- 根因：Windows MAX_PATH=260，PIL 走 C 层 `fopen`，**≥200 字符就开始翻车**
- 修法：绝对路径 ≥200 字符转成 `\\?\` NT namespace 前缀（`_to_long_path` helper）
- 影响面：`dedupe_pic.py` / `detector.py` / `embed_detector.py` / `pose_detector.py`
- 本地/Mac 零回归：`os.name != 'nt'` 原样返回

### 13. Windows 映射盘 (Z:) 长路径 + `\\?\` 前缀真相 (v0.4.32 修订)
- **背景**: 堡垒机 Windows Server 2022 (build 20348) 上 Z: 挂 `\\filestor01...\kj-e68-datamark-100`,
          `Z:\...` 路径 >200 字符时 PIL 全部打不开, 156 张图 `[扫描完成] 有效图片 0`.
- **走过的弯路 (v0.4.31)**: 以为必须展开成 `\\?\UNC\server\share\...`, 结果堡垒机实测
          `\\?\UNC\...` **反而打不开** (`Could not find a part of the path`), 而 `\\?\Z:\...`
          却 **能打开**. 微软老文档说的 "映射盘必须展开 UNC" 在新版 Windows 上不成立.
- **正确修法 (v0.4.32)**:
    1. `_to_long_path` 不再走 UNC 展开分支, 保持 `\\?\Z:\...` 形式
    2. 长度阈值 200 → 180 更保守
    3. `_pil_open` 加 **BytesIO 兜底**: `Image.open(long_path)` 失败时改用
       `Image.open(io.BytesIO(open(long_path,"rb").read()))`, 绕开 PIL 走 CRT `fopen` 对
       `\\?\` 挑食的坑
    4. 前 3 次失败在 stderr 打完整诊断 (原路径 / 长路径 / 异常类型 / WNetGetConnection 结果),
       方便下次真机排查
- **验证方法**: 堡垒机上 `.NET File.OpenRead('\\?\Z:\...')` OK size=180283 (可开),
              `.NET File.OpenRead('\\?\UNC\filestor01...')` ERR (不可开) —— 就是这条结论.
- **影响面**: `dedupe_pic.py` / `detector.py`
- **兼容性**: 非映射盘 / 短路径 / Mac / Linux **零回归**; BytesIO 兜底路径只在 `Image.open` 失败才走.
- **v0.4.34 反转再反转**: 用 diag_pic.exe 实测发现, tkinter `filedialog.askopenfilename()` 在
  长路径下返回值形如 `//?/Z:/切帧结果/...`, **正斜杠 + 已带 `\?\` 前缀**. 我们代码里
  `s.startswith("\\?\\")` 用反斜杠判断根本不匹配, 又叠一层 `\?\` → `\?\//?/Z:/...`
  双重前缀 → FileNotFoundError. **真根因是路径归一化 bug, 跟映射盘/UNC/PIL 全无关**.
- **v0.4.34 正确修法**: 新增 `_normalize_windows_path()` helper, 入口先做:
    1) `/` 全换成 `\`
    2) 折叠误加的 `\\?\\\?\` → `\\?\`
  三处入口全套: `dedupe_pic._to_long_path` / `detector._to_long_path` /
  `diag_pic._long_path_prefix` + `run_diagnostics`
- **教训**: `\?\` 相关 helper 必须**先归一化再判断前缀**, 否则 startsWith 会被斜杠方向骗过.

- **诊断日志样例**:
    ```
    [PIL诊断] path=Z:\...frame_000006.jpg len=204
    [PIL诊断]   long_path=\\?\Z:\...frame_000006.jpg len=208
    [PIL诊断]   Image.open ERR UnidentifiedImageError: ...
    [PIL诊断]   BytesIO Image.open ERR ...  file bytes read OK, size=180283
    ```

### 14. `\\?\` 前缀只保护了 PIL, 却漏了 `Path.stat` / `Path.unlink`
- **现象 (v0.4.34 遗留)**: 图片能 diag_pic 打开, 但 dedupe_pic 报 156 张 `[打开失败-stat]`
- **根因**: `_pil_open` 内部走 `_to_long_path` OK, 但 `build_index` 里 `p.stat()` 是 `pathlib` 直调,
  Windows 上 CRT 层 MAX_PATH=260 卡住, 返回 `WinError 3 系统找不到指定的路径`.
  同理 `p.unlink()` (删除阶段) / `p.exists()` / `p.is_file()` (marker 判断) 全部踩坑.
- **修法 (v0.4.35)**: 新增 5 个 helper: `_safe_stat` / `_safe_unlink` / `_safe_exists` /
  `_safe_is_file` / `_safe_move`. 每个都先试原路径, 挂了自动走 `_to_long_path` 兜底.
  改造 4 个热点调用: `build_index` stat / 删除 unlink+move / `args.root.exists` /
  `done_marker.is_file`.
- **顺带修**: `done_marker.write_text` 原本 `except Exception: pass` 静默吞异常,
  改成 stderr 打 `[ERROR] 写 done marker 失败`, 断线续跑失效不再无声无息.
- **教训**: 上长路径修复不能只盯 PIL, 所有 `pathlib.Path` 上的 IO 方法都要过一遍;
  静默 `except: pass` 是 marker/lock/断点续跑类功能的大杀器, 一律改成明确日志.

### 4. PyArmor trial 版对单次 `pyarmor gen` 有配额
- 现象：CI 报 `ERROR out of license`，`dist_obf` 空目录，后续 copy 全失败
- 根因：一次给 3+ 个文件，最近脚本变大后爆额度
- 修法：**每次 `pyarmor gen` 只传 1 个文件**，PyArmor 天然追加到同一 output 目录
- 8 个 workflow 全部拆分过（`.github/workflows/build-*.yml`）

### 5. 抢跑改代码是最大的罪
- 用户说 "先看看"、"不要改"、"先分析下" —— 就**只**诊断/展示，**不**动 `apply_patch`
- 用户明确说 "改" 或 review 过方案再动
- 违反过一次就会导致用户不知道回滚到哪里（惨痛教训）

### 6. 兼容 > 替换（堡垒机 vs 本地虚拟机）
- 本地虚拟机能跑的逻辑**不允许删除**，只能重构成"兼容堡垒机 + 保留原路径"
- 反例：曾把 wmic 那级 fallback 删了，用户立刻要求恢复
- 正确做法：三级 fallback 完整保留，只是把嵌套 `if ()` 改成 label goto

### 7. Python 改动 = 必须重新出 exe
- 堡垒机跑的是打包好的 `.exe`，py 改动不 push + 打 tag 就永远看不到
- 单纯 push 到 main 会构建 unversioned exe；**打 `v0.4.NN` tag 才能出带版本号的 release exe**
- 用户经常问 "是不是没打 tag" —— 记住：**代码改完 → commit → push → 打 tag → push tag**

### 8. `.git/` 是只读挂载
- `git add / commit / push / tag / reset` 都需要 `require_escalated`
- 已批准的前缀：`["git", "add"]` / `["git", "commit"]` / `["git", "push"]` / `["git", "tag"]` / `["git", "reset", "--hard"]` / `["git", "checkout", "--"]`
- 不要在沙箱里默默失败，遇到 `Operation not permitted` 直接升权

### 9. 不要 `git add -A` / 不要碰用户脏文件
- 用户长期保留的脏文件：`AGENTS.md` 自身（他有本地未提交改动）/ `Dockerfile.otp_web` / `docker-compose.otp_web.yml` / `gen_license.py` / `licensing.py` / `otp_web.py` / `requirements.txt` / `secrets/README.md`
- 还有 untracked：`dist_mac/` / `license_db.py` / `migrations/`
- **只 `git add <明确文件列表>`**，不要 `-A`

### 10. bat 硬约定自检清单（每次改 bat 后跑一遍）
```python
data = open("scripts_bat/xxx.bat", "rb").read()
assert data[:3] != b"\xef\xbb\xbf", "有 BOM"
assert sum(1 for i,b in enumerate(data) if b==0x0A and (i==0 or data[i-1]!=0x0D)) == 0, "有 bare LF"
idx = data.find(b"chcp 65001")
assert all(b < 0x80 for b in data[:idx]), "chcp 之前有非 ASCII"
for ch in "：，（）；！？、":
    assert ch.encode("utf-8") not in data, f"含全角标点: {ch}"
```

### 11. Commit 分片纪律
- 一个 commit 只做**一件事**，题材不同拆开：
  - `修复(bat): xxx` / `修复(长路径): xxx` / `修复(ci): xxx` / `新增(命名): xxx`
- 中文格式 `类型(范围): 中文描述`
- 便于回滚（用户经常需要精确回退某一次）

### 12. 版本号硬编码是最容易被遗忘的坑
- 现象：dedupe_gui 关于页显示 `v0.3.0`，但 git tag 已到 `v0.4.29`
- 根因：5 个 GUI 各自硬写 `APP_VERSION = "vX.Y.Z"`，每次改一个漏一个
- 修法：**全部改成 `from _version import VERSION`，CI 从 git tag 注入**
- **硬规则详见本文件"版本号硬规则"章节**，新增任何打包工具前先读一遍
- 影响：用户分不清是老 exe 还是没升级成功；出问题回滚不知道回到哪个 tag
