@echo off
setlocal EnableExtensions EnableDelayedExpansion
>nul chcp 65001
@echo off
title dedupe_watcher

REM ============================================================
REM  dedupe_watcher.bat
REM  监听 WATCH_ROOT 下的 _done.marker，发现就调 dedupe_pic.exe 去重
REM  抽帧完一个视频，这里立刻清理，磁盘不攒垃圾
REM
REM  用法：
REM    dedupe_watcher.bat
REM    dedupe_watcher.bat "Z:\切帧结果\sjbz_20260708"
REM
REM  参数：
REM    第 1 个位置参数 = 监听根目录（可选，默认 Z:\切帧结果）
REM    /apply           = 真删（默认 dry-run）
REM    /threshold N     = 相似阈值，默认 3
REM    /interval N      = 扫描间隔秒，默认 5
REM    /motion N        = 相邻帧车运动阈值（越大越严格，默认由 exe 决定 0.05）
REM    /once            = 扫一遍就退出（否则死循环）
REM ============================================================

set "WATCH_ROOT="
set "APPLY=0"
set "THRESHOLD=3"
set "INTERVAL=5"
set "MOTION="
set "RUN_ONCE=0"
REM 当日累计剩余图片数超过该值就停止 watcher（不影响正在处理的目录）
REM 改为 0 或不设置来禁用
set "DAILY_REMAIN_LIMIT=80000"
set "DAILY_LIMIT_HIT=0"

:PARSE_ARGS
if "%~1"=="" goto :PARSED
if /I "%~1"=="/apply"     ( set "APPLY=1"    & shift & goto :PARSE_ARGS )
if /I "%~1"=="--apply"    ( set "APPLY=1"    & shift & goto :PARSE_ARGS )
if /I "%~1"=="/once"      ( set "RUN_ONCE=1" & shift & goto :PARSE_ARGS )
if /I "%~1"=="--once"     ( set "RUN_ONCE=1" & shift & goto :PARSE_ARGS )
if /I "%~1"=="/threshold" ( set "THRESHOLD=%~2" & shift & shift & goto :PARSE_ARGS )
if /I "%~1"=="/interval"  ( set "INTERVAL=%~2"  & shift & shift & goto :PARSE_ARGS )
if /I "%~1"=="/motion"    ( set "MOTION=%~2"    & shift & shift & goto :PARSE_ARGS )
if not defined WATCH_ROOT set "WATCH_ROOT=%~1"
shift
goto :PARSE_ARGS

:PARSED
if not defined WATCH_ROOT set "WATCH_ROOT=Z:\切帧结果"

if not exist "%WATCH_ROOT%\" (
    call :LOG_ERR "监听根目录不存在: %WATCH_ROOT%"
    call :LOG_ERR "示例: dedupe_watcher.bat \"Z:\切帧结果\""
    pause & exit /b 2
)

where dedupe_pic.exe >nul 2>nul
if errorlevel 1 (
    call :LOG_ERR "找不到 dedupe_pic.exe，请放到 PATH (推荐 C:\Windows\System32)"
    pause & exit /b 3
)

set "APPLY_TXT=dry-run"
if "%APPLY%"=="1" set "APPLY_TXT=真删"

echo ============================================================
echo   dedupe_watcher
echo ============================================================
call :LOG_INFO "监听根目录 : %WATCH_ROOT%"
call :LOG_INFO "模式       : %APPLY_TXT%"
call :LOG_INFO "阈值       : %THRESHOLD%"
call :LOG_INFO "扫描间隔   : %INTERVAL% 秒"
call :LOG_INFO "一次即退   : %RUN_ONCE%"
echo ------------------------------------------------------------
call :LOG_INFO "工作原理: 扫描有 _done.marker 但没 _dedup_done.marker 的目录"
call :LOG_INFO "          对每个这样的目录调用 dedupe_pic.exe"
call :LOG_INFO "          成功写 _dedup_done.marker，失败写 _dedup_failed.marker"
call :LOG_INFO "按 Ctrl+C 可随时退出"
echo ============================================================
echo.

set "TOTAL_PROCESSED=0"

:LOOP
if "%DAILY_LIMIT_HIT%"=="1" (
    call :LOG_OK "已达当日剩余阈值 %DAILY_REMAIN_LIMIT%，watcher 停止（不影响正在处理的目录）"
    endlocal & exit /b 0
)
set "FOUND_THIS_ROUND=0"

for /f "delims=" %%M in ('dir /s /b /a-d "%WATCH_ROOT%\_done.marker" 2^>nul') do (
    set "MARKER=%%M"
    call :PROCESS_ONE
)

if "!FOUND_THIS_ROUND!"=="0" (
    if "%RUN_ONCE%"=="1" (
        call :LOG_OK "本轮无待处理目录，累计已处理 !TOTAL_PROCESSED! 个，退出"
        endlocal & exit /b 0
    )
    set "T=%TIME:~0,8%"
    call :LOG_INFO "!T!  本轮 0 个待处理，%INTERVAL%s 后重试 (累计 !TOTAL_PROCESSED!)"
    timeout /t %INTERVAL% >nul
) else (
    if "%RUN_ONCE%"=="1" (
        call :LOG_OK "本轮共处理 !FOUND_THIS_ROUND! 个，累计 !TOTAL_PROCESSED!，退出"
        endlocal & exit /b 0
    )
)
goto :LOOP


REM ====================================================================
REM  :PROCESS_ONE  in: MARKER = <某目录>\_done.marker 的完整路径
REM ====================================================================
:PROCESS_ONE
if "%DAILY_LIMIT_HIT%"=="1" goto :EOF
setlocal EnableDelayedExpansion

for %%F in ("!MARKER!") do set "TARGET_DIR=%%~dpF"
if "!TARGET_DIR:~-1!"=="\" set "TARGET_DIR=!TARGET_DIR:~0,-1!"

if exist "!TARGET_DIR!\_dedup_done.marker"    ( endlocal & goto :EOF )
if exist "!TARGET_DIR!\_dedup_failed.marker"  ( endlocal & goto :EOF )
if exist "!TARGET_DIR!\_dedup_running.marker" ( endlocal & goto :EOF )

> "!TARGET_DIR!\_dedup_running.marker" echo running

set "T=%TIME:~0,8%"
echo.
call :LOG_STEP "!T!  发现待处理: !TARGET_DIR!"

set "REPORT_CSV=!TARGET_DIR!\dedupe_report.csv"

if "%APPLY%"=="1" (
    REM 直接永久删除，不落 _trash（避免堡垒机上又要清理一遍）
    if defined MOTION (
        dedupe_pic.exe "!TARGET_DIR!" --threshold %THRESHOLD% --motion-threshold %MOTION% --apply --hard-delete --report "!REPORT_CSV!"
    ) else (
        dedupe_pic.exe "!TARGET_DIR!" --threshold %THRESHOLD% --apply --hard-delete --report "!REPORT_CSV!"
    )
) else (
    if defined MOTION (
        dedupe_pic.exe "!TARGET_DIR!" --threshold %THRESHOLD% --motion-threshold %MOTION% --report "!REPORT_CSV!"
    ) else (
        dedupe_pic.exe "!TARGET_DIR!" --threshold %THRESHOLD% --report "!REPORT_CSV!"
    )
)
set "RC=!ERRORLEVEL!"

del "!TARGET_DIR!\_dedup_running.marker" 2>nul

set "T2=%TIME:~0,8%"
if "!RC!"=="0" (
    > "!TARGET_DIR!\_dedup_done.marker" echo done
    call :LOG_OK "!T2!  完成: !TARGET_DIR!"
    call :DO_STATS "!TARGET_DIR!"
) else (
    > "!TARGET_DIR!\_dedup_failed.marker" echo rc=!RC!
    call :LOG_ERR "!T2!  dedupe 失败 rc=!RC!  目录: !TARGET_DIR!"
    call :LOG_ERR "        已写入 _dedup_failed.marker，本轮不再重试此目录"
    call :LOG_ERR "        排查后手工删除 _dedup_failed.marker 即可让下轮继续"
)

REM 把内部可能已置位的 DAILY_LIMIT_HIT 带出 setlocal 边界
set "_HIT=!DAILY_LIMIT_HIT!"
endlocal & set /a FOUND_THIS_ROUND+=1 & set /a TOTAL_PROCESSED+=1 & set "DAILY_LIMIT_HIT=%_HIT%"
goto :EOF


REM ====================================================================
REM  :DO_STATS  <TARGET_DIR>
REM  调 append_stats.bat 拿到"当日累计剩余"，超阈值就设 DAILY_LIMIT_HIT=1
REM  用 tempfile 中转 stdout，避免嵌套 for/if + delayed expansion 的坑
REM ====================================================================
:DO_STATS
REM 不再 setlocal，直接在调用者作用域修改 DAILY_LIMIT_HIT
set "TDIR=%~1"
set "STATS_BAT=%~dp0append_stats.bat"
if not exist "!STATS_BAT!" (
    call :LOG_WARN "append_stats.bat 不存在: !STATS_BAT!"
    goto :EOF
)
set "STATS_TMP=%TEMP%\dedupe_stats_%RANDOM%_%RANDOM%.txt"
call "!STATS_BAT!" "!TDIR!" > "!STATS_TMP!" 2>nul
set "CUM_REMAIN="
for /f "usebackq delims=" %%C in ("!STATS_TMP!") do set "CUM_REMAIN=%%C"
del "!STATS_TMP!" 2>nul
if not defined CUM_REMAIN (
    call :LOG_WARN "append_stats.bat 无输出，跳过阈值判断"
    goto :EOF
)
call :LOG_INFO "        当日累计剩余 !CUM_REMAIN! / 阈值 %DAILY_REMAIN_LIMIT%"
if %DAILY_REMAIN_LIMIT% LEQ 0 goto :EOF
if !CUM_REMAIN! GEQ %DAILY_REMAIN_LIMIT% set "DAILY_LIMIT_HIT=1"
goto :EOF


REM ====================================================================
REM  日志子过程：统一 tag 宽度，方便未来 grep
REM ====================================================================
:LOG_INFO
echo [INFO ] %~1
goto :EOF
:LOG_OK
echo [ OK  ] %~1
goto :EOF
:LOG_WARN
echo [WARN ] %~1
goto :EOF
:LOG_ERR
echo [ERROR] %~1
goto :EOF
:LOG_STEP
echo [STEP ] %~1
goto :EOF
