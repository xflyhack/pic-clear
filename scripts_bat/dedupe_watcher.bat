@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
title dedupe_watcher

REM ============================================================
REM  dedupe_watcher.bat
REM  监听 OUT_ROOT 下的 _done.marker，看到就跑 dedupe_pic.exe
REM  抽帧完一个视频，这里就立刻去重，磁盘不会攒垃圾。
REM
REM  用法：
REM    dedupe_watcher.bat                        （默认监听 Z:\切帧结果）
REM    dedupe_watcher.bat "Z:\切帧结果\sjbz_20260708"  （只监听指定目录）
REM
REM  参数：
REM    第 1 个位置参数 = 监听根目录（可选）
REM    /apply           = 直接真删（默认 dry-run）
REM    /threshold N     = 相似阈值，默认 3
REM    /interval N      = 空转扫描间隔秒，默认 5
REM    /once            = 扫一遍就退出（否则死循环）
REM ============================================================

REM ---- 默认参数 ----
set "WATCH_ROOT="
set "APPLY=0"
set "THRESHOLD=3"
set "INTERVAL=5"
set "RUN_ONCE=0"

REM ---- 简单解析 ----
:PARSE_ARGS
if "%~1"=="" goto :PARSED
if /I "%~1"=="/apply"     ( set "APPLY=1"    & shift & goto :PARSE_ARGS )
if /I "%~1"=="--apply"    ( set "APPLY=1"    & shift & goto :PARSE_ARGS )
if /I "%~1"=="/once"      ( set "RUN_ONCE=1" & shift & goto :PARSE_ARGS )
if /I "%~1"=="--once"     ( set "RUN_ONCE=1" & shift & goto :PARSE_ARGS )
if /I "%~1"=="/threshold" ( set "THRESHOLD=%~2" & shift & shift & goto :PARSE_ARGS )
if /I "%~1"=="/interval"  ( set "INTERVAL=%~2"  & shift & shift & goto :PARSE_ARGS )
if not defined WATCH_ROOT set "WATCH_ROOT=%~1"
shift
goto :PARSE_ARGS

:PARSED
if not defined WATCH_ROOT set "WATCH_ROOT=Z:\切帧结果"

if not exist "%WATCH_ROOT%\" (
    echo [错误] 监听根目录不存在：%WATCH_ROOT%
    echo        请传入正确的路径，比如 dedupe_watcher.bat "Z:\切帧结果"
    pause & exit /b 2
)

where dedupe_pic.exe >nul 2>nul || (
    echo [错误] 找不到 dedupe_pic.exe，请放到 PATH（如 C:\Windows\System32）。
    pause & exit /b 3
)

echo ============================================================
echo   dedupe_watcher
echo   监听根目录 ： %WATCH_ROOT%
echo   模式       ： %APPLY% (0=dry-run / 1=真删)
echo   阈值       ： %THRESHOLD%
echo   扫描间隔   ： %INTERVAL% 秒
echo   一次即退   ： %RUN_ONCE%
echo ============================================================
echo   工作原理：
echo   - 递归扫描目录，找有 _done.marker 但没 _dedup_done.marker 的视频输出目录
echo   - 对每个这样的目录跑 dedupe_pic.exe
echo   - 成功后写 _dedup_done.marker，下次不再重复
echo   按 Ctrl+C 可随时退出
echo ============================================================
echo.

set "TOTAL_PROCESSED=0"

:LOOP
set "FOUND_THIS_ROUND=0"

REM 找所有 _done.marker
for /f "delims=" %%M in ('dir /s /b /a-d "%WATCH_ROOT%\_done.marker" 2^>nul') do (
    set "MARKER=%%M"
    call :PROCESS_ONE
)

if "!FOUND_THIS_ROUND!"=="0" (
    if "%RUN_ONCE%"=="1" (
        echo [完成] 本轮无待处理视频。已处理累计：!TOTAL_PROCESSED! 个。退出。
        endlocal & exit /b 0
    )
    for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format HH:mm:ss"') do echo [%%t] 本轮 0 个待处理，%INTERVAL%s 后重试... （累计已处理 !TOTAL_PROCESSED!）
    timeout /t %INTERVAL% >nul
) else (
    if "%RUN_ONCE%"=="1" (
        echo [完成] 本轮共处理 !FOUND_THIS_ROUND! 个，累计 !TOTAL_PROCESSED!。退出。
        endlocal & exit /b 0
    )
    REM 有活干就不睡，继续下一轮
)
goto :LOOP


REM ====================================================================
REM  :PROCESS_ONE
REM  in:  MARKER = <某目录>\_done.marker 的完整路径
REM ====================================================================
:PROCESS_ONE
setlocal EnableDelayedExpansion

REM 取 marker 所在目录 = 视频输出目录
for %%F in ("!MARKER!") do set "TARGET_DIR=%%~dpF"
REM 去掉末尾反斜杠
if "!TARGET_DIR:~-1!"=="\" set "TARGET_DIR=!TARGET_DIR:~0,-1!"

REM 已经去过重了就跳过
if exist "!TARGET_DIR!\_dedup_done.marker" (
    endlocal & goto :EOF
)

REM 处理中标记，防止 watcher 起了两份互相抢
if exist "!TARGET_DIR!\_dedup_running.marker" (
    endlocal & goto :EOF
)
echo running > "!TARGET_DIR!\_dedup_running.marker"

for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format HH:mm:ss"') do echo.
for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format HH:mm:ss"') do echo [%%t] 发现待处理：!TARGET_DIR!

set "REPORT_CSV=!TARGET_DIR!\dedupe_report.csv"

if "%APPLY%"=="1" (
    set "TRASH_DIR=!TARGET_DIR!\_trash"
    dedupe_pic.exe "!TARGET_DIR!" --threshold %THRESHOLD% --apply --trash-dir "!TRASH_DIR!" --report "!REPORT_CSV!"
) else (
    dedupe_pic.exe "!TARGET_DIR!" --threshold %THRESHOLD% --report "!REPORT_CSV!"
)
set "RC=!ERRORLEVEL!"

del "!TARGET_DIR!\_dedup_running.marker" 2>nul

if "!RC!"=="0" (
    echo done > "!TARGET_DIR!\_dedup_done.marker"
    for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format HH:mm:ss"') do echo [%%t] [OK] !TARGET_DIR!
) else (
    echo [错误] dedupe rc=!RC!  目录：!TARGET_DIR!
)

endlocal & set /a FOUND_THIS_ROUND+=1 & set /a TOTAL_PROCESSED+=1
goto :EOF
