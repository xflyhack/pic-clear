@echo off
setlocal EnableExtensions EnableDelayedExpansion
>nul chcp 65001
@echo off
title dedupe_watcher

REM ---- verify chcp 65001 actually took effect ----
REM  This bat is often spawned via 'start' from run_all.bat, so it runs
REM  in a fresh cmd window that does NOT inherit the parent's chcp.
REM  Detect and abort early with an ASCII-only message if UTF-8 fails.
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
REM  dedupe_watcher.bat
REM  Watch WATCH_ROOT for _done.marker files, run dedupe_pic.exe
REM  when found. Cleans up right after each video is extracted.
REM
REM  Usage:
REM    dedupe_watcher.bat
REM    dedupe_watcher.bat "Z:\out\job1"
REM
REM  Args:
REM    <positional>     watch root (default: OUT_ROOT env or Z:\out)
REM    /apply           real delete (default dry-run)
REM    /threshold N     similarity threshold, default 3
REM    /interval N      scan interval seconds, default 5
REM    /motion N        adjacent-frame car motion threshold
REM    /scene           enable --scene-protect
REM    /once            run one pass and exit (default: loop forever)
REM ============================================================

set "WATCH_ROOT="
set "APPLY=0"
set "THRESHOLD=3"
set "INTERVAL=5"
set "MOTION="
set "SCENE_ARG="
set "RUN_ONCE=0"
REM LOCK_TTL: stale-lock TTL in seconds (multi-machine dedupe).
REM   Must match run_all / dedupe_gui defaults or the two won't coordinate.
set "LOCK_TTL=900"
REM Stop watcher after this many cumulative remaining images.
REM Set to 0 to disable.
set "DAILY_REMAIN_LIMIT=8000000000"
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
if /I "%~1"=="/scene"     ( set "SCENE_ARG=--scene-protect" & shift & goto :PARSE_ARGS )
if /I "%~1"=="--scene"    ( set "SCENE_ARG=--scene-protect" & shift & goto :PARSE_ARGS )
if /I "%~1"=="--scene-protect" ( set "SCENE_ARG=--scene-protect" & shift & goto :PARSE_ARGS )
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
    call :LOG_ERR "找不到 dedupe_pic.exe,请放到 PATH (推荐 C:\Windows\System32)"
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
if defined SCENE_ARG goto :SCENE_ON
call :LOG_INFO "场景保护   : 关闭"
goto :SCENE_LOGGED
:SCENE_ON
call :LOG_INFO "场景保护   : 开启 (--scene-protect)"
:SCENE_LOGGED
echo ------------------------------------------------------------
call :LOG_INFO "工作原理: 扫描有 _done.marker 但没 _dedup_done.marker 的目录"
call :LOG_INFO "          对每个这样的目录调用 dedupe_pic.exe"
call :LOG_INFO "          成功写 _dedup_done.marker,失败写 _dedup_failed.marker"
call :LOG_INFO "按 Ctrl+C 可随时退出"
echo ============================================================
echo.

set "TOTAL_PROCESSED=0"

:LOOP
if "%DAILY_LIMIT_HIT%"=="1" (
    call :LOG_OK "已达当日剩余阈值 %DAILY_REMAIN_LIMIT%,watcher 停止(不影响正在处理的目录)"
    endlocal & exit /b 0
)
set "FOUND_THIS_ROUND=0"

for /f "delims=" %%M in ('dir /s /b /a-d "%WATCH_ROOT%\_done.marker" 2^>nul') do (
    set "MARKER=%%M"
    call :PROCESS_ONE
)

if "!FOUND_THIS_ROUND!"=="0" (
    if "%RUN_ONCE%"=="1" (
        call :LOG_OK "本轮无待处理目录,累计已处理 !TOTAL_PROCESSED! 个,退出"
        endlocal & exit /b 0
    )
    set "T=%TIME:~0,8%"
    call :LOG_INFO "!T!  本轮 0 个待处理,%INTERVAL%s 后重试 (累计 !TOTAL_PROCESSED!)"
    timeout /t %INTERVAL% >nul
) else (
    if "%RUN_ONCE%"=="1" (
        call :LOG_OK "本轮共处理 !FOUND_THIS_ROUND! 个,累计 !TOTAL_PROCESSED!,退出"
        endlocal & exit /b 0
    )
)
goto :LOOP


REM ====================================================================
REM  :PROCESS_ONE  in: MARKER = full path to <dir>\_done.marker
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
    REM hard-delete, skip _trash so no second cleanup pass is needed
    if defined MOTION (
        dedupe_pic.exe "!TARGET_DIR!" --threshold %THRESHOLD% --marker-dir "!TARGET_DIR!" --lock-ttl %LOCK_TTL% !SCENE_ARG! --motion-threshold %MOTION% --apply --hard-delete --report "!REPORT_CSV!"
    ) else (
        dedupe_pic.exe "!TARGET_DIR!" --threshold %THRESHOLD% --marker-dir "!TARGET_DIR!" --lock-ttl %LOCK_TTL% !SCENE_ARG! --apply --hard-delete --report "!REPORT_CSV!"
    )
) else (
    if defined MOTION (
        dedupe_pic.exe "!TARGET_DIR!" --threshold %THRESHOLD% --marker-dir "!TARGET_DIR!" --lock-ttl %LOCK_TTL% !SCENE_ARG! --motion-threshold %MOTION% --report "!REPORT_CSV!"
    ) else (
        dedupe_pic.exe "!TARGET_DIR!" --threshold %THRESHOLD% --marker-dir "!TARGET_DIR!" --lock-ttl %LOCK_TTL% !SCENE_ARG! --report "!REPORT_CSV!"
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
    call :LOG_ERR "        已写入 _dedup_failed.marker,本轮不再重试此目录"
    call :LOG_ERR "        排查后手工删除 _dedup_failed.marker 即可让下轮继续"
)

REM propagate DAILY_LIMIT_HIT out of the setlocal scope
set "_HIT=!DAILY_LIMIT_HIT!"
endlocal & set /a FOUND_THIS_ROUND+=1 & set /a TOTAL_PROCESSED+=1 & set "DAILY_LIMIT_HIT=%_HIT%"
goto :EOF


REM ====================================================================
REM  :DO_STATS  <TARGET_DIR>
REM  Call append_stats.bat to get cumulative-remaining count; if it
REM  exceeds DAILY_REMAIN_LIMIT, set DAILY_LIMIT_HIT=1.
REM  Route stdout through a tempfile to avoid nested for/if + delayed
REM  expansion pitfalls.
REM ====================================================================
:DO_STATS
REM No setlocal here: DAILY_LIMIT_HIT is intentionally modified in the caller scope
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
    call :LOG_WARN "append_stats.bat 无输出,跳过阈值判断"
    goto :EOF
)
call :LOG_INFO "        当日累计剩余 !CUM_REMAIN! / 阈值 %DAILY_REMAIN_LIMIT%"
if %DAILY_REMAIN_LIMIT% LEQ 0 goto :EOF
if !CUM_REMAIN! GEQ %DAILY_REMAIN_LIMIT% set "DAILY_LIMIT_HIT=1"
goto :EOF


REM ====================================================================
REM  Log helpers: uniform tag width, greppable
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
