@echo off
setlocal EnableExtensions EnableDelayedExpansion
>nul chcp 65001
@echo off


REM ---- verify chcp 65001 actually took effect ----
REM  Some old Windows / VM environments silently ignore 'chcp 65001'.
REM  If it fails, non-ASCII bytes below are parsed as GBK and the
REM  whole bat blows up. Detect and abort with an ASCII-only message.
set "CHCP_OK=0"
for /f "tokens=* delims=" %%A in ('chcp') do set "CHCP_LINE=%%A"
echo(!CHCP_LINE! | findstr /C:"65001" >nul && set "CHCP_OK=1"
if not "!CHCP_OK!"=="1" (
    echo [FATAL] chcp 65001 did not take effect on this machine.
    echo         current: !CHCP_LINE!
    echo.
    echo   How to fix:
    echo     1^) run 'chcp 65001' in this cmd window and try again, or
    echo     2^) use extract_gui.exe / dedupe_gui.exe instead, or
    echo     3^) ask ops to enable UTF-8 in Region ^> Administrative
    echo        ^> "Beta: Use Unicode UTF-8 for worldwide language support".
    pause
    exit /b 4
)
REM ============================================================
REM  append_stats.bat  <TARGET_DIR>
REM  被 dedupe_watcher.bat 每处理完一个目录后 call 一次
REM
REM  作用:
REM   - 从 <TARGET_DIR>\dedupe_report.csv 数 DELETE 行数
REM   - 从 <TARGET_DIR> 数图片总数(.jpg/.jpeg/.png,不递归)
REM   - 追加一行到 Z:\data_source\YYYYMMDD\machine_id_%COMPUTERNAME%.csv
REM   - stdout 打印一个数字:当日累计剩余总和(供 watcher 判 8w 停止)
REM     没有可用结果时打印 -1
REM
REM  返回码:
REM   0 = 成功
REM   1 = 参数错误
REM   2 = 目录/CSV 不存在
REM ============================================================

set "TARGET_DIR=%~1"
if "%TARGET_DIR%"=="" (
    >&2 echo [append_stats] ERROR: 缺少目录参数
    echo -1
    endlocal ^& exit /b 1
)
if not exist "%TARGET_DIR%\" (
    >&2 echo [append_stats] ERROR: 目录不存在: %TARGET_DIR%
    echo -1
    endlocal ^& exit /b 2
)

set "REPORT=%TARGET_DIR%\dedupe_report.csv"
set "HAS_REPORT=1"
if not exist "%REPORT%" (
    >&2 echo [append_stats] WARN: 没有 dedupe_report.csv, 按 total=0/deleted=0 记账: %TARGET_DIR%
    set "HAS_REPORT=0"
)

REM ---- 统计输出路径 ----
REM STATS_ROOT 优先取环境变量, 让 watcher/run_all 传进来, 兼容多盘符.
if not defined STATS_ROOT set "STATS_ROOT=Z:\data_source"

REM ---- 取当天日期 yyyyMMdd, 三级兜底, 全部用 label goto, 不放在括号块里 ----
REM 括号块 if () (...) 内含 for /f + 中文 + 转义符 + ^& exit /b N,
REM 在部分堡垒机 cmd 上会被解析器误 tokenize, 报
REM   '3)' is not recognized as an internal or external command
REM   '?' is not recognized ... (中文首字节被当命令名)
REM 改成 label 顺序流, 每级 fallback 走 goto, cmd 只按顶层单行解析.
REM 本地/虚拟机上 PowerShell 一级即成功, 后面分支不会执行, 语义不变.
REM ---- 第1级: PowerShell (yyyyMMdd 直出) ----
set "TODAY="
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd" 2^>nul') do set "TODAY=%%d"
if not defined TODAY goto :TRY_DATE
echo(!TODAY!| findstr /R "^[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]$" >nul
if not errorlevel 1 goto :TODAY_OK
set "TODAY="

:TRY_DATE
REM ---- 第2级: %%DATE%% 切前 4/6/8 位 (中文 Win 通常是 2026-07-17 或 2026/07/17) ----
>&2 echo [append_stats] WARN: PowerShell 拿日期失败, fallback 到 %%DATE%%
set "D_RAW=%DATE%"
set "TODAY=!D_RAW:~0,4!!D_RAW:~5,2!!D_RAW:~8,2!"
if not defined TODAY goto :TRY_WMIC
echo(!TODAY!| findstr /R "^[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]$" >nul
if not errorlevel 1 goto :TODAY_OK
set "TODAY="

:TRY_WMIC
REM ---- 第3级: wmic os get localdatetime (Win7+ 都有) ----
>&2 echo [append_stats] WARN: %%DATE%% 也拿不到, fallback 到 wmic
for /f "tokens=2 delims==" %%d in ('wmic os get localdatetime /value 2^>nul ^| findstr "="') do set "TODAY=%%d"
if defined TODAY set "TODAY=!TODAY:~0,8!"
if not defined TODAY goto :TODAY_FAIL
echo(!TODAY!| findstr /R "^[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]$" >nul
if not errorlevel 1 goto :TODAY_OK
set "TODAY="

:TODAY_FAIL
>&2 echo [append_stats] ERROR: 三种方式都拿不到日期, 无法按天分目录
echo -1
endlocal ^& exit /b 3

:TODAY_OK
>&2 echo [append_stats] today=!TODAY! stats_root=%STATS_ROOT%
set "STATS_DIR=%STATS_ROOT%\%TODAY%"
set "STATS_CSV=%STATS_DIR%\machine_id_%COMPUTERNAME%.csv"

if not exist "%STATS_ROOT%\" mkdir "%STATS_ROOT%" 2>nul
if not exist "%STATS_ROOT%\" (
    >&2 echo [append_stats] ERROR: 统计根目录不可写, 请设置环境变量 STATS_ROOT 到可写位置, 当前: %STATS_ROOT%
    echo -1
    endlocal ^& exit /b 4
)
if not exist "%STATS_DIR%\"  mkdir "%STATS_DIR%"  2>nul
if not exist "%STATS_DIR%\" (
    >&2 echo [append_stats] ERROR: 无法创建当日目录 %STATS_DIR%
    echo -1
    endlocal ^& exit /b 4
)

REM 第一次写入时写表头
if not exist "%STATS_CSV%" (
    > "%STATS_CSV%" echo folder_name,total,deleted,remain,abs_path,timestamp
)

REM 文件夹名 = TARGET_DIR 的最后一段, 后面统计诊断日志要用
for %%N in ("%TARGET_DIR%") do set "FOLDER_NAME=%%~nxN"

REM ---- 数图片总数(不递归)----
set "TOTAL=0"
REM 用 dir /b /a-d 数图片文件, 每种扩展名逐条读实际文件名, 才是真实数量
for %%E in (jpg jpeg png) do (
    for /f "delims=" %%F in ('dir /b /a-d "%TARGET_DIR%\*.%%E" 2^>nul') do (
        set /a TOTAL+=1
    )
)

REM ---- 数 DELETE 行数 ----
REM 走 PowerShell (cmd for/f 遇 BOM/中文/空字段不稳); 没 CSV 时跳过.
REM 用 label 而不是 if () 括号块, 避免在堡垒机 cmd 上被误 tokenize.
set "DELETED=0"
if not "!HAS_REPORT!"=="1" goto :DELETED_DONE
for /f %%D in ('powershell -NoProfile -Command "try { (Import-Csv '%REPORT%' | Where-Object { $_.action -eq 'DELETE' }).Count } catch { 0 }" 2^>nul') do set "DELETED=%%D"
if not "!DELETED!"=="0" goto :DELETED_DONE
REM 兜底: PowerShell 没数出来, 退化到 cmd for/f
for /f "usebackq skip=1 tokens=2 delims=," %%A in ("%REPORT%") do (
    if /I "%%A"=="DELETE" set /a DELETED+=1
)
:DELETED_DONE
REM 调试: stderr 打一行, 便于排查; 累计还是 0 时先看这一行是不是有值
>&2 echo [append_stats] %FOLDER_NAME%: total=!TOTAL! deleted=!DELETED! report="%REPORT%"

set /a REMAIN=TOTAL - DELETED
if %REMAIN% LSS 0 set "REMAIN=0"

REM 时间戳 (YYYY-MM-DD HH:MM:SS)
REM 上面把 %DATE% 的拆分提取删了,这里改用 PowerShell 拿完整时间戳.
REM Get-Date -Format s 输出 ISO8601 :2026-07-14T10:23:45,无空格最亲 for /f
for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format s" 2^>nul') do set "TS_RAW=%%t"
if not defined TS_RAW (
    >&2 echo [append_stats] WARN: PowerShell 拿时间戳失败, fallback 到 %%TIME%%
    set "TS_RAW=!TODAY:~0,4!-!TODAY:~4,2!-!TODAY:~6,2!T%TIME:~0,8%"
)
set "TS=!TS_RAW:T= !"

REM ---- 追加一行 ----
REM abs_path 里可能有逗号或空格,用双引号包起来
>> "%STATS_CSV%" echo %FOLDER_NAME%,%TOTAL%,%DELETED%,%REMAIN%,"%TARGET_DIR%",%TS%
if not exist "%STATS_CSV%" (
    >&2 echo [append_stats] ERROR: 写不进统计 CSV: %STATS_CSV%
    echo -1
    endlocal ^& exit /b 5
)

REM ---- 计算当日累计已删 (第 3 列 deleted 汇总) ----
REM watcher 用它跟 DAILY_DELETE_LIMIT 比: 累计已删 >= 上限 -> 停止再取下一个目录
REM 之前累加的是第 4 列 remain, 语义相反, 已修正
set "CUM_DELETED=0"
for /f "usebackq skip=1 tokens=3 delims=," %%D in ("%STATS_CSV%") do (
    set /a CUM_DELETED+=%%D 2>nul
)

REM stdout 只打这一个数字(watcher 靠它判断删除上限)
echo %CUM_DELETED%
endlocal & exit /b 0
