# 本仓库 bat 脚本编写约定

## 为什么有这份文档

本仓库的 bat 脚本要在**中文 Windows Server 2022**（堡垒机）里运行，
既要显示中文不乱码，又要能被普通用户双击执行。**过程中踩了几个坑，
以后写新的 bat 之前先看这里，避免重复入坑。**

---

## 文件格式：**必须** UTF-8 + BOM + CRLF

- **UTF-8**：源文件用 UTF-8 编码
- **BOM**（`0xEF 0xBB 0xBF`）：文件开头必须有 BOM，配合 `chcp 65001` 才能让 cmd 正确
  显示中文（否则中文变乱码或 `?`）
- **CRLF** 换行（`\r\n`）：Windows cmd 对 LF-only 换行的解析不稳定，
  中文路径 + 复杂 `for /r` 时容易出诡异错误。用 CRLF 保底。

验证方式：

```bash
head -c 3 xxx.bat | xxd
# 应输出：efbb bf

python3 -c "d=open('xxx.bat','rb').read(); print('CRLF:', d.count(b'\r\n'), 'bare LF (should be 0):', d.count(b'\n')-d.count(b'\r\n'))"
# 应输出：CRLF: N   bare LF (should be 0): 0
```

---

## 头部必须写两次 `@echo off`（**核心坑**）

**正确写法**（照抄）：

```bat
@echo off
setlocal EnableExtensions EnableDelayedExpansion
>nul chcp 65001
@echo off
title 你的脚本名
```

### 为什么要两次

BOM (`0xEF 0xBB 0xBF`) 出现在**第一个 `@echo off` 之前**，cmd.exe 会把 BOM 当命令字节，
第一行"实际上"执行的是一个乱码命令并报错：

```
'∩╗┐@echo' 不是内部或外部命令
```

这一行失败后，**`@echo off` 没生效**，后续每一行命令都会被回显到屏幕，脚本看着一团糟：

```
D:\path\to\dir>title hide_markers.bat

D:\path\to\dir>REM ============================================================

D:\path\to\dir>REM  hide_markers.bat
（每一行都被回显 …）
```

修复方式就是在 `chcp 65001` **之后**（此时 UTF-8 已生效，BOM 那一行的错误已经翻篇了）
再来一次 `@echo off`，确保 echo 状态可靠关闭。

### 为什么 `chcp 65001` 之前不能只写一次

`chcp 65001` 之前，如果代码页是默认的 936（GBK），
cmd 读 BOM 那三个字节 = `∩╗┐`（在 GBK 里的显示），当命令找。
你不管加多少个 `@echo off`，**只要它们在 `chcp 65001` 之前**都可能受 BOM 影响。

所以顺序必须是：**先 chcp 切 UTF-8，再重新 `@echo off`。**

---

## 参考模板

现有 `scripts_bat/run_all.bat`、`scripts_bat/hide_markers.bat` 都遵守这个约定，写新脚本可以复制它们头部。

## 其他小约定

- **`setlocal EnableDelayedExpansion`**：只要用到 `!var!`（`for` 循环内展开）就必须写
- **中文注释**用 `REM` 而不是 `::`（`::` 在 `if`/`for` 块里可能出错）
- **`pause`** 放在脚本末尾，普通用户双击运行完能看到输出再关闭
- **错误处理**用 `exit /b <非零>` 明确告知 exit code，别 `exit 1`（那样会关掉整个 cmd）
- **中文路径**要处理时用双引号包裹，如 `"%ROOT%\%SUB%"`
