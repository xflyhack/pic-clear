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
    echo [append_stats] ERROR: 缺少目录参数
    endlocal & exit /b 1
)
if not exist "%TARGET_DIR%\" (
    echo [append_stats] ERROR: 目录不存在: %TARGET_DIR%
    endlocal & exit /b 2
)

set "REPORT=%TARGET_DIR%\dedupe_report.csv"
if not exist "%REPORT%" (
    echo [append_stats] WARN: 没有 dedupe_report.csv,跳过统计: %TARGET_DIR%
    endlocal & exit /b 2
)

REM ---- 统计输出路径 ----
REM STATS_ROOT 优先取环境变量, 让 watcher/run_all 传进来, 兼容多盘符.
if not defined STATS_ROOT set "STATS_ROOT=Z:\data_source"
REM 关键:%DATE% 在中文 Windows 上可能包含"周一/星期日"这样的前缀,
REM tokens 拆分会把星期名当成 YYYY,最终生成"周一0714"这种错目录.
REM 用 PowerShell 拿日期,绕开 %DATE% 陷阱,输出永远是 8 位 yyyyMMdd.
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "TODAY=%%d"
if not defined TODAY (
    echo [append_stats] ERROR: 无法获取当前日期(PowerShell 可用吗?)
    endlocal ^& exit /b 3
)
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

REM ---- 数图片总数(不递归)----
set "TOTAL=0"
for %%X in ("%TARGET_DIR%\*.jpg" "%TARGET_DIR%\*.jpeg" "%TARGET_DIR%\*.png") do (
    if exist "%%~X" set /a TOTAL+=1
)

REM ---- 数 DELETE 行数 ----
set "DELETED=0"
for /f "usebackq delims=" %%L in (`findstr /R /C:",DELETE," "%REPORT%" 2^>nul`) do (
    set /a DELETED+=1
)

REM 兜底:如果 CSV 表头不是标准的(第 2 列 action),上面 findstr 找不到就用另一个模式
if "%DELETED%"=="0" (
    for /f "usebackq delims=" %%L in (`findstr /R /C:"^[0-9][0-9]*,DELETE," "%REPORT%" 2^>nul`) do (
        set /a DELETED+=1
    )
)

set /a REMAIN=TOTAL - DELETED
if %REMAIN% LSS 0 set "REMAIN=0"

REM 文件夹名 = TARGET_DIR 的最后一段
for %%N in ("%TARGET_DIR%") do set "FOLDER_NAME=%%~nxN"

REM 时间戳 (YYYY-MM-DD HH:MM:SS)
REM 上面把 %DATE% 的拆分提取删了,这里改用 PowerShell 拿完整时间戳.
REM Get-Date -Format s 输出 ISO8601 :2026-07-14T10:23:45,无空格最亲 for /f
for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format s"') do set "TS_RAW=%%t"
set "TS=!TS_RAW:T= !"

REM ---- 追加一行 ----
REM abs_path 里可能有逗号或空格,用双引号包起来
>> "%STATS_CSV%" echo %FOLDER_NAME%,%TOTAL%,%DELETED%,%REMAIN%,"%TARGET_DIR%",%TS%
if not exist "%STATS_CSV%" (
    >&2 echo [append_stats] ERROR: 写不进统计 CSV: %STATS_CSV%
    echo -1
    endlocal ^& exit /b 5
)

REM ---- 计算当日累计 remain ----
REM 跳过表头行;第 4 列是 remain
set "CUM_REMAIN=0"
for /f "usebackq skip=1 tokens=4 delims=," %%R in ("%STATS_CSV%") do (
    set /a CUM_REMAIN+=%%R 2>nul
)

REM stdout 只打这一个数字(watcher 靠它判断 8w 阈值)
echo %CUM_REMAIN%
endlocal & exit /b 0
