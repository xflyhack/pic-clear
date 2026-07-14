@echo off
setlocal EnableExtensions EnableDelayedExpansion
>nul chcp 65001
@echo off
title summary_stats - pic-clear

REM ============================================================
REM  summary_stats.bat
REM  Aggregate Z:\data_source\<YYYYMMDD>\machine_id_*.csv
REM  Requires: powershell.exe + summary_stats_helper.ps1 (same dir)
REM  Usage:
REM    double-click summary_stats.bat        --  interactive menu
REM    summary_stats.bat 20260714             --  fixed date, skip date menu
REM ============================================================

set "STATS_ROOT=Z:\data_source"
set "PS1_HELPER=%~dp0summary_stats_helper.ps1"

if not exist "%PS1_HELPER%" goto :ERR_NO_HELPER
if not exist "%STATS_ROOT%\" goto :ERR_NO_STATS

set "DATE_MODE="
set "DATE_VALUE="
if not "%~1"=="" (
    set "DATE_VALUE=%~1"
    set "DATE_MODE=one"
)

echo ============================================================
echo   pic-clear tong ji hui zong / daily stats summary
echo ============================================================
echo.

if defined DATE_MODE goto :AFTER_DATE

:MENU_DATE
echo [Step 1] date range
echo   [1] today  (default)
echo   [2] pick date  (input YYYYMMDD)
echo   [3] all history
echo.
set "DATE_CHOICE="
set /p DATE_CHOICE="choose [1/2/3] default=1: "
if not defined DATE_CHOICE set "DATE_CHOICE=1"

if "!DATE_CHOICE!"=="1" goto :DATE_TODAY
if "!DATE_CHOICE!"=="2" goto :DATE_INPUT
if "!DATE_CHOICE!"=="3" goto :DATE_ALL

echo [ERROR] invalid choice: !DATE_CHOICE!
echo.
goto :MENU_DATE

:DATE_TODAY
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "DATE_VALUE=%%d"
set "DATE_MODE=one"
goto :AFTER_DATE

:DATE_INPUT
set "DATE_VALUE="
set /p DATE_VALUE="input YYYYMMDD (e.g. 20260714): "
set "DATE_MODE=one"
goto :AFTER_DATE

:DATE_ALL
set "DATE_MODE=all"
goto :AFTER_DATE

:AFTER_DATE
if not "!DATE_MODE!"=="one" goto :SHOW_DATE
echo !DATE_VALUE!| findstr /R /C:"^[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]$" >nul
if errorlevel 1 (
    echo [ERROR] date must be 8-digit YYYYMMDD, got: !DATE_VALUE!
    pause
    exit /b 2
)

:SHOW_DATE
echo.
if "!DATE_MODE!"=="one" (
    echo [date] single day: !DATE_VALUE!
) else (
    echo [date] all history
)

:MENU_GRAN
echo.
echo [Step 2] granularity
echo   [1] all machines / all folders  (default)
echo   [2] one folder     (exact match abs_path)
echo   [3] one folder + descendants  (prefix match)
echo.
set "GRAN_CHOICE="
set /p GRAN_CHOICE="choose [1/2/3] default=1: "
if not defined GRAN_CHOICE set "GRAN_CHOICE=1"

set "PATH_FILTER="
set "PATH_MODE="

if "!GRAN_CHOICE!"=="1" goto :GRAN_ALL
if "!GRAN_CHOICE!"=="2" goto :GRAN_EXACT
if "!GRAN_CHOICE!"=="3" goto :GRAN_PREFIX

echo [ERROR] invalid choice: !GRAN_CHOICE!
echo.
goto :MENU_GRAN

:GRAN_ALL
set "PATH_MODE=all"
goto :AFTER_GRAN

:GRAN_EXACT
set "PATH_FILTER="
set /p PATH_FILTER="input full path (drag-drop supported): "
if not defined PATH_FILTER (
    echo [ERROR] no path given
    pause
    exit /b 2
)
set "PATH_MODE=exact"
goto :AFTER_GRAN

:GRAN_PREFIX
set "PATH_FILTER="
set /p PATH_FILTER="input full path (drag-drop supported): "
if not defined PATH_FILTER (
    echo [ERROR] no path given
    pause
    exit /b 2
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
echo   working, please wait  (PowerShell)...
echo ============================================================

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1_HELPER%" -StatsRoot "%STATS_ROOT%" -DateMode "!DATE_MODE!" -DateValue "!DATE_VALUE!" -PathMode "!PATH_MODE!" -PathFilter "!PATH_FILTER!" -ExportCsv "0"

if errorlevel 1 (
    echo.
    echo [ERROR] PowerShell aggregation failed, errorlevel=!errorlevel!
    pause
    exit /b 3
)

echo.
set "EXPORT_CHOICE="
set /p EXPORT_CHOICE="export the summary above to CSV? [Y/N] default=N: "
if /I "!EXPORT_CHOICE!"=="Y" (
    echo.
    powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1_HELPER%" -StatsRoot "%STATS_ROOT%" -DateMode "!DATE_MODE!" -DateValue "!DATE_VALUE!" -PathMode "!PATH_MODE!" -PathFilter "!PATH_FILTER!" -ExportCsv "1"
)

echo.
echo ============================================================
echo   done.
echo ============================================================
pause
exit /b 0

:ERR_NO_HELPER
echo [ERROR] helper not found: %PS1_HELPER%
echo         Please place summary_stats_helper.ps1 next to summary_stats.bat
pause
exit /b 2

:ERR_NO_STATS
echo [ERROR] stats root not found: %STATS_ROOT%
echo         Z: not mapped? Or no dedupe_watcher/append_stats has ever run?
pause
exit /b 2
