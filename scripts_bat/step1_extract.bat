@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
title %~n0

REM ===========================================================
REM  依赖：
REM   - extract_frames.exe / dedupe_pic.exe 已放入 PATH
REM     推荐放到 C:\Windows\System32（系统 PATH 自带）
REM   - 每个 exe 同目录下都要有 license.lic（否则 System32 里也行）
REM ===========================================================

REM ---- 数据盘盘符（默认 Z:）----
set "DATA_DRIVE=Z:"

REM ---- 输出根目录（在数据盘上，就近落盘）----
set "OUT_ROOT=%DATA_DRIVE%\切帧结果"

REM ---- 源目录前缀（自动匹配，比如 sjbz_20260708）----
set "DATA_PREFIX=sjbz_"

echo ============================================================
echo   自动化脚本
echo   数据盘   ： %DATA_DRIVE%
echo   输出根   ： %OUT_ROOT%
echo   目录前缀 ： %DATA_PREFIX%*
echo ============================================================
echo.

if not exist "%DATA_DRIVE%\" (
    echo [错误] 数据盘 %DATA_DRIVE% 不存在，请先挂载或检查 net use。
    pause & exit /b 2
)

REM ---- 自动找 sjbz_* 顶层目录 ----
set "SRC_DIR="
set "SRC_NAME="
set "MATCH_COUNT=0"
for /d %%D in ("%DATA_DRIVE%\%DATA_PREFIX%*") do (
    set /a MATCH_COUNT+=1
    set "SRC_DIR=%%~fD"
    set "SRC_NAME=%%~nxD"
)

if "%MATCH_COUNT%"=="0" (
    echo [提示] 在 %DATA_DRIVE%\ 下没有找到 %DATA_PREFIX%* 目录。
    echo        请把源目录路径拖到本窗口，或手工输入，然后回车：
    set /p "SRC_DIR=源目录: "
    if not defined SRC_DIR ( echo 未提供源目录，退出。 & pause & exit /b 2 )
    for %%X in ("!SRC_DIR!") do set "SRC_NAME=%%~nxX"
) else if "%MATCH_COUNT%"=="1" (
    echo [自动] 唯一匹配：!SRC_DIR!
) else (
    echo [选择] 找到多个源目录：
    set "IDX=0"
    for /d %%D in ("%DATA_DRIVE%\%DATA_PREFIX%*") do (
        set /a IDX+=1
        set "OPT_!IDX!=%%~fD"
        echo    [!IDX!] %%~nxD
    )
    echo.
    set /p "PICK=请输入编号: "
    call set "SRC_DIR=%%OPT_!PICK!%%"
    if not defined SRC_DIR ( echo 无效选择，退出。 & pause & exit /b 2 )
    for %%X in ("!SRC_DIR!") do set "SRC_NAME=%%~nxX"
)

set "DST_DIR=%OUT_ROOT%\%SRC_NAME%"
if not exist "%DST_DIR%" mkdir "%DST_DIR%" 2>nul

echo.
echo ------------------------------------------------------------
echo   源目录   ： %SRC_DIR%
echo   输出目录 ： %DST_DIR%
echo ------------------------------------------------------------
echo.
echo [提示] 如果窗口卡住看起来无输出，按一下 Enter 或 Esc 恢复。
echo        建议永久关闭"快速编辑模式"：右键窗口标题 -^> 属性 -^> 编辑选项。
echo.

echo ============================================================
echo   仅执行：抽帧
echo ============================================================
where extract_frames.exe >nul 2>nul || (
    echo [错误] 找不到 extract_frames.exe。
    pause & exit /b 3
)

extract_frames.exe "%SRC_DIR%" "%DST_DIR%" --fps 1 --ext .h265
set "RC=%ERRORLEVEL%"
echo.
echo 退出码：%RC%
pause
endlocal
exit /b %RC%
