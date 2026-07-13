@echo off
setlocal EnableExtensions EnableDelayedExpansion
>nul chcp 65001
@echo off
title unhide_markers.bat

REM ============================================================
REM  unhide_markers.bat
REM  递归取消隐藏当前目录（bat 所在目录）下面的 marker + csv：
REM    - _done.marker
REM    - _dedup_done.marker
REM    - _dedup_running.marker
REM    - _dedup_failed.marker
REM    - dedupe_report.csv
REM  用法：把 bat 拷到要处理的根目录里，双击运行即可。
REM ============================================================

REM 处理根目录 = bat 所在目录
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

echo ============================================================
echo   unhide_markers.bat   (取消隐藏)
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
        attrib -H "%%F" >nul 2>&1
        set /a N+=1
        set /a TOTAL+=1
    )
)
echo   取消隐藏 !N! 个 _done.marker

echo [扫描] _dedup_done.marker ...
set "N=0"
for /r "%ROOT%" %%F in (_dedup_done.marker) do (
    if exist "%%F" (
        attrib -H "%%F" >nul 2>&1
        set /a N+=1
        set /a TOTAL+=1
    )
)
echo   取消隐藏 !N! 个 _dedup_done.marker

echo [扫描] _dedup_running.marker ...
set "N=0"
for /r "%ROOT%" %%F in (_dedup_running.marker) do (
    if exist "%%F" (
        attrib -H "%%F" >nul 2>&1
        set /a N+=1
        set /a TOTAL+=1
    )
)
echo   取消隐藏 !N! 个 _dedup_running.marker

echo [扫描] _dedup_failed.marker ...
set "N=0"
for /r "%ROOT%" %%F in (_dedup_failed.marker) do (
    if exist "%%F" (
        attrib -H "%%F" >nul 2>&1
        set /a N+=1
        set /a TOTAL+=1
    )
)
echo   取消隐藏 !N! 个 _dedup_failed.marker

echo [扫描] dedupe_report.csv ...
set "N=0"
for /r "%ROOT%" %%F in (dedupe_report.csv) do (
    if exist "%%F" (
        attrib -H "%%F" >nul 2>&1
        set /a N+=1
        set /a TOTAL+=1
    )
)
echo   取消隐藏 !N! 个 dedupe_report.csv

echo.
echo ============================================================
echo   完成，共取消隐藏 !TOTAL! 个文件。
echo ============================================================
echo.
pause
exit /b 0
