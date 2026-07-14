@echo off
>nul chcp 65001
setlocal EnableExtensions EnableDelayedExpansion
title summary_stats

REM ========================================================
REM  summary_stats.bat  --  aggregate machine_id_*.csv
REM  Menus intentionally kept ASCII to survive cmd encoding.
REM  Summary output (Chinese) is emitted by PowerShell where
REM  the encoding path is clean.
REM ========================================================

set "PS1_HELPER=%~dp0summary_stats_helper.ps1"
if not exist "%PS1_HELPER%" goto :ERR_NO_HELPER

echo ============================================================
echo   pic-clear stats summary
echo ============================================================
echo.

REM =============================================
REM  Step 0  choose drive
REM =============================================
:MENU_DRIVE
echo [Step 1/3] choose drive
set "DRIVE_LIST_FILE=%TEMP%\summary_stats_drives_%RANDOM%.txt"
del /q "%DRIVE_LIST_FILE%" 2>nul

set "IDX=0"
for %%D in (A B C D E F G H I J K L M N O P Q R S T U V W X Y Z) do (
    if exist "%%D:\" (
        set /a IDX+=1
        echo   [!IDX!] %%D:\
        >>"%DRIVE_LIST_FILE%" echo %%D:\
    )
)

if "!IDX!"=="0" (
    echo   [ERR] no drive found
    del /q "%DRIVE_LIST_FILE%" 2>nul
    pause
    exit /b 2
)

echo.
set "DRIVE_CHOICE="
set /p DRIVE_CHOICE="drive # (default 1): "
if not defined DRIVE_CHOICE set "DRIVE_CHOICE=1"

set "CUR_DIR="
set "N=0"
for /f "usebackq delims=" %%L in ("%DRIVE_LIST_FILE%") do (
    set /a N+=1
    if "!N!"=="!DRIVE_CHOICE!" set "CUR_DIR=%%L"
)
del /q "%DRIVE_LIST_FILE%" 2>nul

if not defined CUR_DIR (
    echo   [ERR] invalid: !DRIVE_CHOICE!
    echo.
    goto :MENU_DRIVE
)

if "!CUR_DIR:~-1!"=="\" set "CUR_DIR=!CUR_DIR:~0,-1!"
echo.
echo [drive] !CUR_DIR!\
echo.

REM =============================================
REM  Step 1  browse / drill
REM =============================================
:BROWSE_LOOP
echo ============================================================
echo   current: !CUR_DIR!\
echo ============================================================

set "SUB_LIST_FILE=%TEMP%\summary_stats_subs_%RANDOM%.txt"
del /q "%SUB_LIST_FILE%" 2>nul

set "SUB_IDX=0"
for /f "delims=" %%S in ('dir /ad /b "!CUR_DIR!\" 2^>nul') do (
    set /a SUB_IDX+=1
    echo   [!SUB_IDX!] %%S
    >>"%SUB_LIST_FILE%" echo %%S
)

if "!SUB_IDX!"=="0" (
    echo   [empty, no subfolders]
)

echo.
echo   [1-N]  drill into that subfolder
echo   [0]    use CURRENT dir as stats root
echo   [U]    go UP one level
echo   [D]    switch DRIVE
echo   [Q]    quit
echo.

set "BROWSE_CHOICE="
set /p BROWSE_CHOICE="choice (default 0): "
if not defined BROWSE_CHOICE set "BROWSE_CHOICE=0"

if /I "!BROWSE_CHOICE!"=="Q" (
    del /q "%SUB_LIST_FILE%" 2>nul
    exit /b 0
)
if /I "!BROWSE_CHOICE!"=="D" (
    del /q "%SUB_LIST_FILE%" 2>nul
    goto :MENU_DRIVE
)
if /I "!BROWSE_CHOICE!"=="U" (
    del /q "%SUB_LIST_FILE%" 2>nul
    goto :BROWSE_UP
)
if "!BROWSE_CHOICE!"=="0" (
    del /q "%SUB_LIST_FILE%" 2>nul
    set "STATS_ROOT=!CUR_DIR!"
    goto :AFTER_ROOT
)

set "SUB_NAME="
set "N=0"
for /f "usebackq delims=" %%L in ("%SUB_LIST_FILE%") do (
    set /a N+=1
    if "!N!"=="!BROWSE_CHOICE!" set "SUB_NAME=%%L"
)
del /q "%SUB_LIST_FILE%" 2>nul

if not defined SUB_NAME (
    echo   [ERR] invalid: !BROWSE_CHOICE!
    echo.
    goto :BROWSE_LOOP
)

set "CUR_DIR=!CUR_DIR!\!SUB_NAME!"
echo.
goto :BROWSE_LOOP

:BROWSE_UP
for %%P in ("!CUR_DIR!") do set "PARENT=%%~dpP"
if "!PARENT:~-1!"=="\" set "PARENT=!PARENT:~0,-1!"
if not defined PARENT goto :MENU_DRIVE
if "!PARENT!"=="!CUR_DIR!" goto :MENU_DRIVE
set "CUR_DIR=!PARENT!"
echo.
goto :BROWSE_LOOP

:AFTER_ROOT
echo.
echo [stats root] !STATS_ROOT!
echo.

REM =============================================
REM  Step 2  granularity
REM =============================================
:MENU_GRAN
echo [Step 2/3] granularity
echo   [1] all  (recurse the stats root)  (default)
echo   [2] exact  (match abs_path exactly)
echo   [3] prefix (abs_path + descendants)
echo.
set "GRAN_CHOICE="
set /p GRAN_CHOICE="choice [1/2/3] (default 1): "
if not defined GRAN_CHOICE set "GRAN_CHOICE=1"

set "PATH_FILTER="
set "PATH_MODE="

if "!GRAN_CHOICE!"=="1" goto :GRAN_ALL
if "!GRAN_CHOICE!"=="2" goto :GRAN_EXACT
if "!GRAN_CHOICE!"=="3" goto :GRAN_PREFIX

echo   [ERR] invalid: !GRAN_CHOICE!
echo.
goto :MENU_GRAN

:GRAN_ALL
set "PATH_MODE=all"
goto :AFTER_GRAN

:GRAN_EXACT
set "PATH_FILTER="
set /p PATH_FILTER="input full path (drag-drop ok): "
if not defined PATH_FILTER (
    echo   [ERR] no path
    echo.
    goto :MENU_GRAN
)
set "PATH_MODE=exact"
goto :AFTER_GRAN

:GRAN_PREFIX
set "PATH_FILTER="
set /p PATH_FILTER="input full path (drag-drop ok): "
if not defined PATH_FILTER (
    echo   [ERR] no path
    echo.
    goto :MENU_GRAN
)
set "PATH_MODE=prefix"
goto :AFTER_GRAN

:AFTER_GRAN
if defined PATH_FILTER set "PATH_FILTER=!PATH_FILTER:"=!"

echo.
if "!PATH_MODE!"=="all" (
    echo [scope] all machines + all folders
) else if "!PATH_MODE!"=="exact" (
    echo [scope] exact: !PATH_FILTER!
) else (
    echo [scope] prefix: !PATH_FILTER!\...
)

echo.
echo ============================================================
echo   [Step 3/3] aggregating (PowerShell) ...
echo ============================================================

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1_HELPER%" -StatsRoot "!STATS_ROOT!" -PathMode "!PATH_MODE!" -PathFilter "!PATH_FILTER!" -ExportCsv "0"

if errorlevel 1 (
    echo.
    echo   [ERR] PowerShell aggregation failed, errorlevel=!errorlevel!
    pause
    exit /b 3
)

echo.
set "EXPORT_CHOICE="
set /p EXPORT_CHOICE="export summary to CSV? [Y/N] (default N): "
if /I "!EXPORT_CHOICE!"=="Y" (
    echo.
    powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1_HELPER%" -StatsRoot "!STATS_ROOT!" -PathMode "!PATH_MODE!" -PathFilter "!PATH_FILTER!" -ExportCsv "1"
)

echo.
echo ============================================================
echo   done.
echo ============================================================
pause
exit /b 0

:ERR_NO_HELPER
echo [ERR] helper not found: %PS1_HELPER%
echo       Place summary_stats_helper.ps1 next to summary_stats.bat
pause
exit /b 2
