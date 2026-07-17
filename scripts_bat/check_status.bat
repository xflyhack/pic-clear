@echo off
setlocal EnableExtensions EnableDelayedExpansion
>nul chcp 65001
title check_status - pic-clear


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
REM  check_status.bat
REM  只做检测,不做任何操作.循环刷新(默认 5 秒一次).
REM  Ctrl+C 退出.
REM  依赖:tasklist,powershell(Win 都自带).
REM ============================================================

REM 刷新间隔(秒),可改
set "INTERVAL=5"

REM 输出根,用于查找最新任务的 status.json;跟 pipeline 的 OUT_ROOT 保持一致
set "OUT_ROOT=Z:\切帧结果"

:LOOP
cls
echo ============================================================
echo   pic-clear 运行状态检测       %DATE% %TIME:~0,8%
echo ============================================================
echo.

REM ---- 检测三个 exe ----
call :CHECK_PROC "pipeline.exe"
call :CHECK_PROC "extract_frames.exe"
call :CHECK_PROC "dedupe_pic.exe"

echo ------------------------------------------------------------

REM ---- 读取最新 job 的 status.json ----
call :SHOW_LATEST_JOB

echo ============================================================
echo   %INTERVAL% 秒后自动刷新   ^|   Ctrl+C 退出
echo ============================================================

timeout /t %INTERVAL% /nobreak >nul
goto :LOOP

REM ====================================================================
REM :CHECK_PROC   %1=exe 名(含引号)
REM ====================================================================
:CHECK_PROC
set "PROC=%~1"
set "COUNT=0"
set "PIDS="
set "TOTAL_MEM=0"
for /f "tokens=1,2,5 delims=," %%A in ('tasklist /FI "IMAGENAME eq %PROC%" /FO CSV /NH 2^>nul') do (
    set "N=%%~A"
    if /I "!N!"=="%PROC%" (
        set /a COUNT+=1
        set "PID_=%%~B"
        set "MEM=%%~C"
        REM 去掉逗号和 K
        set "MEM=!MEM: K=!"
        set "MEM=!MEM:,=!"
        set /a TOTAL_MEM+=MEM 2>nul
        if defined PIDS ( set "PIDS=!PIDS!,!PID_!" ) else ( set "PIDS=!PID_!" )
    )
)
if !COUNT! GTR 0 (
    call :FORMAT_MEM !TOTAL_MEM! MEM_FMT
    call :PAD "%PROC%" 22 NAME_PAD
    if !COUNT! GTR 1 (
        echo   [ OK  ] !NAME_PAD! 运行中   进程数: !COUNT!   PID: !PIDS!   总内存: !MEM_FMT!
    ) else (
        echo   [ OK  ] !NAME_PAD! 运行中   PID: !PIDS!   内存: !MEM_FMT!
    )
) else (
    call :PAD "%PROC%" 22 NAME_PAD
    echo   [ --  ] !NAME_PAD! 未运行
)
goto :EOF

REM ====================================================================
REM :FORMAT_MEM   %1=KB 数字, %2=输出变量名  ->  "12,345 K" 或 "12.3 MB"
REM ====================================================================
:FORMAT_MEM
set /a "KB=%~1"
if !KB! GEQ 1024 (
    set /a "MB_INT=KB/1024"
    set /a "MB_DEC=(KB %% 1024)*10/1024"
    set "%~2=!MB_INT!.!MB_DEC! MB"
) else (
    set "%~2=!KB! K"
)
goto :EOF

REM ====================================================================
REM :PAD   %1=字符串, %2=目标宽度, %3=输出变量名(右侧补空格到目标宽度)
REM ====================================================================
:PAD
set "S=%~1"
set "W=%~2"
set "PADDED=%S%                                                  "
call set "PADDED=%%PADDED:~0,!W!%%"
set "%~3=!PADDED!"
goto :EOF

REM ====================================================================
REM :SHOW_LATEST_JOB   在 %OUT_ROOT%\.pipeline\jobs 下找最新 job 展示
REM ====================================================================
:SHOW_LATEST_JOB
set "JOBS_DIR=%OUT_ROOT%\.pipeline\jobs"
if not exist "%JOBS_DIR%" (
    echo   [任务] 未找到 jobs 目录:%JOBS_DIR%
    goto :EOF
)

REM 找最新一个子目录(按修改时间倒序,取第一个)
set "LATEST="
for /f "delims=" %%D in ('dir /b /ad /o-d "%JOBS_DIR%" 2^>nul') do (
    if not defined LATEST set "LATEST=%%D"
)
if not defined LATEST (
    echo   [任务] jobs 目录下没有任何任务
    goto :EOF
)

set "STATUS_FILE=%JOBS_DIR%\%LATEST%\status.json"
if not exist "%STATUS_FILE%" (
    echo   [任务] 最新任务无 status.json:%LATEST%
    goto :EOF
)

REM 用 PowerShell 解析 JSON,格式化输出
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$s = Get-Content -Raw -Encoding UTF8 -Path '%STATUS_FILE%' ^| ConvertFrom-Json; ^
     $done = ($s.subs ^| Where-Object { $_.stage -eq 'done' }).Count; ^
     $failed = ($s.subs ^| Where-Object { $_.stage -eq 'failed' }).Count; ^
     Write-Host ('  最新任务  : ' + $s.job_id); ^
     Write-Host ('  状态      : ' + $s.state + '    worker PID: ' + $s.pid); ^
     Write-Host ('  创建于    : ' + $s.created_at); ^
     Write-Host ('  进度      : ' + $done + ' / ' + $s.total_subs + '   (失败 ' + $failed + ')'); ^
     Write-Host ('  最后消息  : ' + $s.last_message)"
goto :EOF

