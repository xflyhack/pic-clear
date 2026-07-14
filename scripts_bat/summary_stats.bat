@echo off
setlocal EnableExtensions EnableDelayedExpansion
>nul chcp 65001
title summary_stats - pic-clear

REM ============================================================
REM  summary_stats.bat   pic-clear 每日 CSV 汇总
REM
REM  流程：
REM    Step 0  选盘符
REM    Step 1  在盘上浏览/钻取，最终选定 stats root
REM            stats root 期望结构： <root>\<YYYYMMDD>\machine_id_*.csv
REM    Step 2  选日期(今天/指定 YYYYMMDD/全部)
REM    Step 3  选粒度(全部/单目录精确/单目录及子孙)
REM    Step 4  屏幕汇总 + 询问是否导出 CSV
REM
REM  依赖：
REM    powershell.exe(自带)
REM    同目录下 summary_stats_helper.ps1
REM ============================================================

set "PS1_HELPER=%~dp0summary_stats_helper.ps1"
if not exist "%PS1_HELPER%" goto :ERR_NO_HELPER

echo ============================================================
echo   pic-clear 每日统计汇总
echo ============================================================
echo.

REM ============================================================
REM  Step 0  选盘符
REM ============================================================
:MENU_DRIVE
echo [第一步] 选择数据盘
set "DRIVE_LIST_FILE=%TEMP%\summary_stats_drives_%RANDOM%.txt"
del /q "%DRIVE_LIST_FILE%" 2>nul

set "IDX=0"
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "Get-PSDrive -PSProvider FileSystem ^| ForEach-Object { $_.Root }"`) do (
    if exist "%%~D" (
        set /a IDX+=1
        echo   [!IDX!] %%~D
        >>"%DRIVE_LIST_FILE%" echo %%~D
    )
)

if "!IDX!"=="0" (
    echo   [错误] 没有可用盘符
    del /q "%DRIVE_LIST_FILE%" 2>nul
    pause
    exit /b 2
)

echo.
set "DRIVE_CHOICE="
set /p DRIVE_CHOICE="请输入盘符编号(默认 1): "
if not defined DRIVE_CHOICE set "DRIVE_CHOICE=1"

set "CUR_DIR="
set "N=0"
for /f "usebackq delims=" %%L in ("%DRIVE_LIST_FILE%") do (
    set /a N+=1
    if "!N!"=="!DRIVE_CHOICE!" set "CUR_DIR=%%L"
)
del /q "%DRIVE_LIST_FILE%" 2>nul

if not defined CUR_DIR (
    echo   [错误] 无效编号：!DRIVE_CHOICE!
    echo.
    goto :MENU_DRIVE
)

REM 去掉末尾反斜杠，方便后面拼接
if "!CUR_DIR:~-1!"=="\" set "CUR_DIR=!CUR_DIR:~0,-1!"
echo.
echo [已选盘] !CUR_DIR!\
echo.

REM ============================================================
REM  Step 1  浏览 / 钻取，选定 stats root
REM ============================================================
:BROWSE_LOOP
echo ============================================================
echo   当前目录: !CUR_DIR!\
echo ============================================================

set "SUB_LIST_FILE=%TEMP%\summary_stats_subs_%RANDOM%.txt"
del /q "%SUB_LIST_FILE%" 2>nul

set "SUB_IDX=0"
for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "Get-ChildItem -LiteralPath '!CUR_DIR!\' -Directory -ErrorAction SilentlyContinue ^| Sort-Object Name ^| ForEach-Object { $_.Name }"`) do (
    set /a SUB_IDX+=1
    echo   [!SUB_IDX!] %%S
    >>"%SUB_LIST_FILE%" echo %%S
)

if "!SUB_IDX!"=="0" (
    echo   [空目录，没有子目录]
)

echo.
echo   [数字]  钻取到对应编号的子目录
echo   [0]  就用当前目录作为 stats root
echo   [U]  返回上一级
echo   [D]  换盘符
echo   [Q]  退出
echo.

set "BROWSE_CHOICE="
set /p BROWSE_CHOICE="请选择(默认 0): "
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

REM 数字：钻到对应子目录
set "SUB_NAME="
set "N=0"
for /f "usebackq delims=" %%L in ("%SUB_LIST_FILE%") do (
    set /a N+=1
    if "!N!"=="!BROWSE_CHOICE!" set "SUB_NAME=%%L"
)
del /q "%SUB_LIST_FILE%" 2>nul

if not defined SUB_NAME (
    echo   [错误] 无效编号：!BROWSE_CHOICE!
    echo.
    goto :BROWSE_LOOP
)

set "CUR_DIR=!CUR_DIR!\!SUB_NAME!"
echo.
goto :BROWSE_LOOP

:BROWSE_UP
REM 返回上一级
for %%P in ("!CUR_DIR!") do set "PARENT=%%~dpP"
REM %~dp 会给出带末尾反斜杠的父路径，去掉尾斜杠
if "!PARENT:~-1!"=="\" set "PARENT=!PARENT:~0,-1!"
REM 已经在盘根就退回选盘符
if not defined PARENT goto :MENU_DRIVE
if "!PARENT!"=="!CUR_DIR!" goto :MENU_DRIVE
REM 如果 PARENT 只剩 X: 这种(长度 <= 2)，也允许
set "CUR_DIR=!PARENT!"
echo.
goto :BROWSE_LOOP

:AFTER_ROOT
echo.
echo [已选 stats root] !STATS_ROOT!
echo.

REM ============================================================
REM  Step 2  选日期
REM ============================================================
:MENU_DATE
echo [第二步] 统计日期范围
echo   [1] 今天  (默认)
echo   [2] 指定某天  (输入 YYYYMMDD)
echo   [3] 全部日期  (所有历史)
echo.
set "DATE_CHOICE="
set /p DATE_CHOICE="请选择 [1/2/3](默认 1): "
if not defined DATE_CHOICE set "DATE_CHOICE=1"

if "!DATE_CHOICE!"=="1" goto :DATE_TODAY
if "!DATE_CHOICE!"=="2" goto :DATE_INPUT
if "!DATE_CHOICE!"=="3" goto :DATE_ALL

echo   [错误] 无效选项：!DATE_CHOICE!
echo.
goto :MENU_DATE

:DATE_TODAY
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "DATE_VALUE=%%d"
set "DATE_MODE=one"
goto :AFTER_DATE

:DATE_INPUT
set "DATE_VALUE="
set /p DATE_VALUE="请输入 YYYYMMDD (例 20260714): "
set "DATE_MODE=one"
goto :AFTER_DATE

:DATE_ALL
set "DATE_MODE=all"
goto :AFTER_DATE

:AFTER_DATE
if not "!DATE_MODE!"=="one" goto :SHOW_DATE
echo !DATE_VALUE!| findstr /R /C:"^[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]$" >nul
if errorlevel 1 (
    echo   [错误] 日期格式非法：!DATE_VALUE!，必须是 8 位数字 YYYYMMDD
    echo.
    goto :MENU_DATE
)

:SHOW_DATE
echo.
if "!DATE_MODE!"=="one" (
    echo [日期] 单日汇总: !DATE_VALUE!
) else (
    echo [日期] 全部历史
)

REM ============================================================
REM  Step 3  选粒度
REM ============================================================
:MENU_GRAN
echo.
echo [第三步] 统计粒度
echo   [1] 全部  (所有机器所有目录)  (默认)
echo   [2] 单个目录  (精确匹配 abs_path)
echo   [3] 目录及子孙  (前缀匹配 abs_path\...)
echo.
set "GRAN_CHOICE="
set /p GRAN_CHOICE="请选择 [1/2/3](默认 1): "
if not defined GRAN_CHOICE set "GRAN_CHOICE=1"

set "PATH_FILTER="
set "PATH_MODE="

if "!GRAN_CHOICE!"=="1" goto :GRAN_ALL
if "!GRAN_CHOICE!"=="2" goto :GRAN_EXACT
if "!GRAN_CHOICE!"=="3" goto :GRAN_PREFIX

echo   [错误] 无效选项：!GRAN_CHOICE!
echo.
goto :MENU_GRAN

:GRAN_ALL
set "PATH_MODE=all"
goto :AFTER_GRAN

:GRAN_EXACT
set "PATH_FILTER="
set /p PATH_FILTER="请输入目录完整路径(可拖拽窗口): "
if not defined PATH_FILTER (
    echo   [错误] 未输入路径
    echo.
    goto :MENU_GRAN
)
set "PATH_MODE=exact"
goto :AFTER_GRAN

:GRAN_PREFIX
set "PATH_FILTER="
set /p PATH_FILTER="请输入目录完整路径(可拖拽窗口): "
if not defined PATH_FILTER (
    echo   [错误] 未输入路径
    echo.
    goto :MENU_GRAN
)
set "PATH_MODE=prefix"
goto :AFTER_GRAN

:AFTER_GRAN
if defined PATH_FILTER set "PATH_FILTER=!PATH_FILTER:"=!"

echo.
if "!PATH_MODE!"=="all" (
    echo [粒度] 全部机器 + 全部目录
) else if "!PATH_MODE!"=="exact" (
    echo [粒度] 精确: !PATH_FILTER!
) else (
    echo [粒度] 前缀: !PATH_FILTER!\...
)

echo.
echo ============================================================
echo   汇总中，请稍候(PowerShell 处理中)...
echo ============================================================

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1_HELPER%" -StatsRoot "!STATS_ROOT!" -DateMode "!DATE_MODE!" -DateValue "!DATE_VALUE!" -PathMode "!PATH_MODE!" -PathFilter "!PATH_FILTER!" -ExportCsv "0"

if errorlevel 1 (
    echo.
    echo   [错误] PowerShell 汇总失败，errorlevel=!errorlevel!
    pause
    exit /b 3
)

echo.
set "EXPORT_CHOICE="
set /p EXPORT_CHOICE="是否把上面的汇总导出成 CSV? [Y/N](默认 N): "
if /I "!EXPORT_CHOICE!"=="Y" (
    echo.
    powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1_HELPER%" -StatsRoot "!STATS_ROOT!" -DateMode "!DATE_MODE!" -DateValue "!DATE_VALUE!" -PathMode "!PATH_MODE!" -PathFilter "!PATH_FILTER!" -ExportCsv "1"
)

echo.
echo ============================================================
echo   完成
echo ============================================================
pause
exit /b 0

:ERR_NO_HELPER
echo [错误] 找不到帮助脚本: %PS1_HELPER%
echo        请把 summary_stats_helper.ps1 放到 summary_stats.bat 同目录。
pause
exit /b 2
