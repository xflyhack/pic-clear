# 本仓库 bat 脚本编写约定

## 为什么有这份文档

本仓库的 bat 脚本要在**中文 Windows Server 2022**（堡垒机）和本地测试机里运行，
既要显示中文不乱码，又要能被普通用户双击执行。**过程中踩了几个坑，
以后写新的 bat 之前先看这里，避免重复入坑。**

---

## 文件格式：**UTF-8 无 BOM + CRLF**（新约定，2026-07-14 更新）

- **UTF-8 无 BOM**（不要 `0xEF 0xBB 0xBF`）
- **CRLF** 换行（`\r\n`）
- 头部第一行是 `@echo off`，第二行 `setlocal ...`，第三行 `>nul chcp 65001`

### 为什么改这条

旧约定要求 **UTF-8 + BOM + 双 @echo off**（BOM 让第一个 `@echo off` 失效，
必须在 chcp 65001 后再关一次）。这个方案在 Windows Server 2022 部分环境下
能跑，但在**另一些 cmd 环境**（例如某些 Explorer 双击 + 中文 Windows 组合）下会崩：

- BOM 三字节 `0xEF 0xBB 0xBF` 在系统默认代码页（936=GBK）里被读成 `∩╗┐` 或 `锘`，
  被 cmd 当成"命令名"去查，报错 `'??' is not recognized as an internal or external command`
- 加了双 `@echo off` 也不一定能压住 —— 因为在极端情况下，BOM 之前的字节导致 cmd
  连**行边界都解析错**，会把 REM 行也当命令执行，进而触发全角括号 `（Windows）` 里的
  `（` 报错、`Maximum setlocal recursion level reached` 等一连串灾难

**结论：直接不放 BOM，问题根除。** UTF-8 内容配合 `>nul chcp 65001` 依然能正确显示中文
（chcp 生效后 cmd 会用 UTF-8 解析后续字节）。

---

## 头部标准写法（**照抄**）

```bat
@echo off
setlocal EnableExtensions EnableDelayedExpansion
>nul chcp 65001
title 你的脚本名
```

**不再需要**在 chcp 之后再写一次 `@echo off`（那个坑因为去掉 BOM 已经消失了）。
如果你从旧脚本复制过来，看到两次 `@echo off`，保留无害，但新脚本别加了。

---

## 验证方式

```bash
# 应该输出 '@echo off'（前 9 字节），不是 'efbb bf'
head -c 9 xxx.bat | xxd

# 换行必须全部 CRLF
python3 -c "d=open('xxx.bat','rb').read(); \
  print('CRLF:', d.count(b'\r\n'), 'bare LF (should be 0):', d.count(b'\n')-d.count(b'\r\n'))"
```

---

## 中文能不能出现在 bat 里

**可以**，但看情况：

- **echo/set /p 提示语中的中文** —— 大多数环境 OK。个别环境下依然乱码
  （比如通过 telnet/SSH 转发窗口，或者 cmd 的 raster 字体不支持 UTF-8）
- **REM 注释里的中文** —— **一律 OK**，chcp 65001 生效后 REM 是被跳过的，
  内容不参与命令解析
- **全角标点 `（）「」——`** —— **强烈不建议**在 echo 里用！
  一旦编码/代码页出问题，会连锁触发一堆命令解析错乱。用半角 `()` `[]` `--` 更稳

### 如果必须绝对稳（对乱码零容忍）

- 把 echo 提示语全部改成 **ASCII 英文 + 拼音**，脚本正文中 REM 用中文没关系
- 参考 `scripts_bat/summary_stats.bat`

---

## 避免嵌套 `if / else if` 括号块 + `set /p` / `for /f`（重要）

**反例**（会崩）：

```bat
if "!X!"=="1" (
    set /p Y="input Y (e.g. 20260714): "
    set "MODE=one"
) else if "!X!"=="2" (
    for /f %%d in ('powershell -Command "..."') do set "Y=%%d"
    set "MODE=one"
) else (
    ...
)
```

上面这段在括号块内的 `^(` `^)` 转义解析不稳，加上 `set /p` 提示语里的 `(`，
可能把外层 `if ( ... )` 提前闭合，把后续 echo 内容全都当命令执行。

**正例**：用 `goto :LABEL` 平铺流程

```bat
if "!X!"=="1" goto :ACT_ONE
if "!X!"=="2" goto :ACT_TWO
goto :ACT_ELSE

:ACT_ONE
set /p Y="input Y (e.g. 20260714): "
set "MODE=one"
goto :AFTER

:ACT_TWO
for /f %%d in ('powershell -Command "..."') do set "Y=%%d"
set "MODE=one"
goto :AFTER

:ACT_ELSE
...
goto :AFTER

:AFTER
...
```

参考 `scripts_bat/summary_stats.bat`。

---

## 其他小约定

- **`setlocal EnableDelayedExpansion`**：只要用到 `!var!`（`for` 循环内展开）就必须写
- **中文注释**用 `REM` 而不是 `::`（`::` 在 `if`/`for` 块里可能出错）
- **`pause`** 放在脚本末尾，普通用户双击运行完能看到输出再关闭
- **错误处理**用 `exit /b <非零>` 明确告知 exit code，别 `exit 1`（那样会关掉整个 cmd）
- **中文路径**要处理时用双引号包裹，如 `"%ROOT%\%SUB%"`
- **子例程里 `setlocal` 必须配对 `endlocal`**：否则每次调用累积一层，
  32 次后触发 `Maximum setlocal recursion level reached`

---

## 历史坑速查

| 症状 | 根因 | 修法 |
|------|------|------|
| `'∩╗┐@echo' 不是内部或外部命令` | BOM + 系统默认代码页非 UTF-8 | 去掉 BOM |
| `'??' is not recognized as an internal or external command` | 同上（乱码显示 BOM） | 去掉 BOM |
| `'ell（Windows'` `'同目录下'` 循环报错 | REM 里全角括号 + 括号块内 set /p 转义崩 | 去掉 BOM，去掉 REM 全角括号，或整个 bat 改 ASCII |
| `Maximum setlocal recursion level reached` | 一次崩溃触发 cmd 反复重放；或子例程 setlocal 未配对 endlocal | 修根因；子例程加 `endlocal` |
| 中文变 `?` 或方块 | chcp 未切到 65001，或 cmd 用了 raster 字体 | `>nul chcp 65001` + 换 Consolas/lucida console 字体 |
| 生成的日期目录变 "周一0714" | 中文 Windows `%DATE%` 输出带星期名前缀 | 用 `for /f %%d in ('powershell -Command "Get-Date -Format yyyyMMdd"')` |
