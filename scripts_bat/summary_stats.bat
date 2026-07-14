@echo off
setlocal EnableExtensions EnableDelayedExpansion
>nul chcp 65001
@echo off
title summary_stats - pic-clear

REM ============================================================
REM  summary_stats.bat
REM  Aggregate <StatsRoot>\<YYYYMMDD>\machine_id_*.csv
REM  Requires: powershell.exe + summary_stats_helper.ps1 (same dir)
REM  Usage:
REM    double-click summary_stats.bat        --  interactive menu
REM    summary_stats.bat 20260714             --  fixed date
REM    summary_stats.bat 20260714 D:\my_stats --  fixed date + fixed root
REM ============================================================

set "PS1_HELPER=%~dp0summary_stats_helper.ps1"

if not exist "%PS1_HELPER%" goto :ERR_NO_HELPER

REM ---- arg 1 = date (YYYYMMDD), arg 2 = stats root ----
set "DATE_MODE="
set "DATE_VALUE="
set "STATS_ROOT="
if not "%~1"=="" (
    set "DATE_VALUE=%~1"
    set "DATE_MODE=one"
)
if not "%~2"=="" (
    set "STATS_ROOT=%~2"
)

echo ============================================================
echo   pic-clear stats summary
echo ============================================================
echo.

REM ============================================================
REM  Step 0 : choose stats root
REM ============================================================
if defined STATS_ROOT goto :CHECK_STATS

:MENU_ROOT
echo [Step 0] choose stats root directory
echo   scanning drives for \data_source ...
echo.

set "IDX=0"
set "ROOT_LIST_FILE=%TEMP%\summary_stats_roots_%RANDOM%.txt"
del /q "%ROOT_LIST_FILE%" 2>nul

REM Enumerate FileSystem drives; for each check <drive>\data_source
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "Get-PSDrive -PSProvider FileSystem ^| ForEach-Object { $_.Root }"`) do (
    if exist "%%~D" (
        set "CAND=%%~Ddata_source"
        if exist "!CAND!\" (
            set /a IDX+=1
            echo   [!IDX!] !CAND!    ^(has data_source^)
            >>"%ROOT_LIST_FILE%" echo !CAND!
        )
    )
)

if "!IDX!"=="0" (
    echo   ^(no drive has \data_source subfolder^)
    echo.
)

echo   [M] input a custom path manually
echo   [Q] quit
echo.
set "ROOT_CHOICE="
if "!IDX!"=="0" goto :ROOT_ASK_NONE
if "!IDX!"=="1" goto :ROOT_ASK_ONE
goto :ROOT_ASK_MANY

:ROOT_ASK_NONE
set /p ROOT_CHOICE="choose [M/Q] default=M: "
if not defined ROOT_CHOICE set "ROOT_CHOICE=M"
goto :ROOT_HANDLE

:ROOT_ASK_ONE
set /p ROOT_CHOICE="choose [1/M/Q] default=1: "
if not defined ROOT_CHOICE set "ROOT_CHOICE=1"
goto :ROOT_HANDLE

:ROOT_ASK_MANY
set /p ROOT_CHOICE="choose [1-!IDX!/M/Q] default=1: "
if not defined ROOT_CHOICE set "ROOT_CHOICE=1"
goto :ROOT_HANDLE

:ROOT_HANDLE

if /I "!ROOT_CHOICE!"=="Q" (
    del /q "%ROOT_LIST_FILE%" 2>nul
    exit /b 0
)
if /I "!ROOT_CHOICE!"=="M" goto :ROOT_MANUAL

REM numeric: read that line from list file
set "N=0"
for /f "usebackq delims=" %%L in ("%ROOT_LIST_FILE%") do (
    set /a N+=1
    if "!N!"=="!ROOT_CHOICE!" set "STATS_ROOT=%%L"
)

if not defined STATS_ROOT (
    echo [ERROR] invalid choice: !ROOT_CHOICE!
    echo.
    goto :MENU_ROOT
)

del /q "%ROOT_LIST_FILE%" 2>nul
goto :CHECK_STATS

:ROOT_MANUAL
del /q "%ROOT_LIST_FILE%" 2>nul
set "STATS_ROOT="
set /p STATS_ROOT="input stats root full path (e.g. Z:\data_source): "
if not defined STATS_ROOT (
    echo [ERROR] no path given
    echo.
    goto :MENU_ROOT
)
REM strip surrounding quotes if drag-drop
set "STATS_ROOT=!STATS_ROOT:"=!"
REM strip trailing backslash
if "!STATS_ROOT:~-1!"=="\" set "STATS_ROOT=!STATS_ROOT:~0,-1!"

:CHECK_STATS
if not exist "!STATS_ROOT!\" (
    echo [ERROR] stats root not exist: !STATS_ROOT!
    echo.
    if defined DATE_MODE (
        echo         you passed it via command line, please double-check.
        pause
        exit /b 2
    )
    set "STATS_ROOT="
    goto :MENU_ROOT
)

echo.
echo [root] !STATS_ROOT!
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

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1_HELPER%" -StatsRoot "!STATS_ROOT!" -DateMode "!DATE_MODE!" -DateValue "!DATE_VALUE!" -PathMode "!PATH_MODE!" -PathFilter "!PATH_FILTER!" -ExportCsv "0"

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
    powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1_HELPER%" -StatsRoot "!STATS_ROOT!" -DateMode "!DATE_MODE!" -DateValue "!DATE_VALUE!" -PathMode "!PATH_MODE!" -PathFilter "!PATH_FILTER!" -ExportCsv "1"
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
