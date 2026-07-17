@echo off
setlocal EnableExtensions EnableDelayedExpansion
>nul chcp 65001
@echo off
title hide_markers.bat


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
REM  hide_markers.bat
REM  递归隐藏当前目录(bat 所在目录)下面的 marker + csv:
REM    - _done.marker
REM    - _dedup_done.marker
REM    - _dedup_running.marker
REM    - _dedup_failed.marker
REM    - dedupe_report.csv
REM  用法:把 bat 拷到要处理的根目录里,双击运行即可.
REM ============================================================

REM 处理根目录 = bat 所在目录
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

echo ============================================================
echo   hide_markers.bat   (隐藏)
echo   处理根目录: %ROOT%
echo ============================================================
echo.

if not exist "%ROOT%" (
    echo [ERROR] 目录不存在: %ROOT%
    pause
    exit /b 2
)

set "TOTAL=0"

echo [扫描] _done.marker ...
set "N=0"
for /r "%ROOT%" %%F in (_done.marker) do (
    if exist "%%F" (
        attrib +H "%%F" >nul 2>&1
        set /a N+=1
        set /a TOTAL+=1
    )
)
echo   隐藏 !N! 个 _done.marker

echo [扫描] _dedup_done.marker ...
set "N=0"
for /r "%ROOT%" %%F in (_dedup_done.marker) do (
    if exist "%%F" (
        attrib +H "%%F" >nul 2>&1
        set /a N+=1
        set /a TOTAL+=1
    )
)
echo   隐藏 !N! 个 _dedup_done.marker

echo [扫描] _dedup_running.marker ...
set "N=0"
for /r "%ROOT%" %%F in (_dedup_running.marker) do (
    if exist "%%F" (
        attrib +H "%%F" >nul 2>&1
        set /a N+=1
        set /a TOTAL+=1
    )
)
echo   隐藏 !N! 个 _dedup_running.marker

echo [扫描] _dedup_failed.marker ...
set "N=0"
for /r "%ROOT%" %%F in (_dedup_failed.marker) do (
    if exist "%%F" (
        attrib +H "%%F" >nul 2>&1
        set /a N+=1
        set /a TOTAL+=1
    )
)
echo   隐藏 !N! 个 _dedup_failed.marker

echo [扫描] dedupe_report.csv ...
set "N=0"
for /r "%ROOT%" %%F in (dedupe_report.csv) do (
    if exist "%%F" (
        attrib +H "%%F" >nul 2>&1
        set /a N+=1
        set /a TOTAL+=1
    )
)
echo   隐藏 !N! 个 dedupe_report.csv

echo.
echo ============================================================
echo   完成,共隐藏 !TOTAL! 个文件.
echo ============================================================
echo.
pause
exit /b 0
