@echo off
setlocal EnableExtensions EnableDelayedExpansion
>nul chcp 65001
@echo off
title summary_stats - pic-clear

REM ============================================================
REM  summary_stats.bat
REM  汇总 Z:\data_source\<YYYYMMDD>\machine_id_*.csv，输出每日统计。
REM
REM  依赖：
REM    - PowerShell（Windows 自带）
REM    - 同目录下 summary_stats_helper.ps1
REM
REM  用法：
REM    双击 summary_stats.bat              → 交互菜单
REM    summary_stats.bat 20260714           → 指定日期，跳过日期菜单
REM ============================================================

set "STATS_ROOT=Z:\data_source"
set "PS1_HELPER=%~dp0summary_stats_helper.ps1"

if not exist "%PS1_HELPER%" (
    echo [ERROR] 找不到帮助脚本: %PS1_HELPER%
    echo         请确保 summary_stats_helper.ps1 与 summary_stats.bat 在同一目录。
    pause
    exit /b 2
)

if not exist "%STATS_ROOT%\" (
    echo [ERROR] 统计目录不存在: %STATS_ROOT%
    echo         这台机器可能没挂载 Z: 盘，或者还没跑过 dedupe_watcher/append_stats。
    pause
    exit /b 2
)

REM ---- 命令行参数：第一个参数当作 YYYYMMDD ----
set "DATE_MODE="
set "DATE_VALUE="
if not "%~1"=="" (
    set "DATE_VALUE=%~1"
    set "DATE_MODE=one"
)

echo ============================================================
echo   pic-clear 每日统计汇总
echo ============================================================
echo.

REM ============================================================
REM  第一步：选日期
REM ============================================================
if not defined DATE_MODE (
    echo [第一步] 选统计日期
    echo   [1] 今天  ^(推荐^)
    echo   [2] 指定某天  ^(输入 YYYYMMDD^)
    echo   [3] 全部日期  ^(所有历史^)
    echo.
    set /p DATE_CHOICE="请选择 [1/2/3]（默认 1）: "
    if not defined DATE_CHOICE set "DATE_CHOICE=1"

    if "!DATE_CHOICE!"=="1" (
        for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "DATE_VALUE=%%d"
        set "DATE_MODE=one"
    ) else if "!DATE_CHOICE!"=="2" (
        set /p DATE_VALUE="输入 YYYYMMDD ^(例 20260714^): "
        set "DATE_MODE=one"
    ) else if "!DATE_CHOICE!"=="3" (
        set "DATE_MODE=all"
    ) else (
        echo [ERROR] 无效选项
        pause
        exit /b 2
    )
)

REM 校验单日格式
if "!DATE_MODE!"=="one" (
    echo !DATE_VALUE! | findstr /R "^^[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]$" >nul
    if errorlevel 1 (
        echo [ERROR] 日期格式非法：!DATE_VALUE!，必须是 8 位数字 YYYYMMDD
        pause
        exit /b 2
    )
)

echo.
if "!DATE_MODE!"=="one" (
    echo [日期] 单日汇总: !DATE_VALUE!
) else (
    echo [日期] 全部历史
)

REM ============================================================
REM  第二步：选粒度
REM ============================================================
echo.
echo [第二步] 选统计粒度
echo   [1] 全部  ^(所有机器所有目录^)
echo   [2] 单个目录  ^(精确匹配 abs_path^)
echo   [3] 目录及子孙  ^(前缀匹配 abs_path\...^)
echo.
set /p GRAN_CHOICE="请选择 [1/2/3]（默认 1）: "
if not defined GRAN_CHOICE set "GRAN_CHOICE=1"

set "PATH_FILTER="
set "PATH_MODE="

if "!GRAN_CHOICE!"=="1" (
    set "PATH_MODE=all"
) else if "!GRAN_CHOICE!"=="2" (
    set /p PATH_FILTER="输入目录完整路径（可直接拖拽窗口）: "
    if not defined PATH_FILTER (
        echo [ERROR] 未输入路径
        pause
        exit /b 2
    )
    set "PATH_MODE=exact"
) else if "!GRAN_CHOICE!"=="3" (
    set /p PATH_FILTER="输入目录完整路径（可直接拖拽窗口）: "
    if not defined PATH_FILTER (
        echo [ERROR] 未输入路径
        pause
        exit /b 2
    )
    set "PATH_MODE=prefix"
) else (
    echo [ERROR] 无效选项
    pause
    exit /b 2
)

REM 去掉拖拽路径两端可能的引号
if defined PATH_FILTER set "PATH_FILTER=!PATH_FILTER:"=!"

echo.
if "!PATH_MODE!"=="all" (
    echo [粒度] 全部机器 + 全部目录
) else if "!PATH_MODE!"=="exact" (
    echo [粒度] 精确匹配: !PATH_FILTER!
) else (
    echo [粒度] 前缀匹配: !PATH_FILTER!\...
)

echo.
echo ============================================================
echo   汇总中，请稍候（PowerShell 处理中）...
echo ============================================================

REM ============================================================
REM  第三步：调 PowerShell 帮助脚本（不导出）
REM ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1_HELPER%" -StatsRoot "%STATS_ROOT%" -DateMode "!DATE_MODE!" -DateValue "!DATE_VALUE!" -PathMode "!PATH_MODE!" -PathFilter "!PATH_FILTER!" -ExportCsv "0"

if errorlevel 1 (
    echo.
    echo [ERROR] PowerShell 汇总失败（errorlevel=%errorlevel%）
    pause
    exit /b 3
)

echo.
set /p EXPORT_CHOICE="是否把上面的汇总导出成 CSV？[Y/N]（默认 N）: "
if /I "!EXPORT_CHOICE!"=="Y" (
    echo.
    powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1_HELPER%" -StatsRoot "%STATS_ROOT%" -DateMode "!DATE_MODE!" -DateValue "!DATE_VALUE!" -PathMode "!PATH_MODE!" -PathFilter "!PATH_FILTER!" -ExportCsv "1"
)

echo.
echo ============================================================
echo   完成
echo ============================================================
pause
exit /b 0
