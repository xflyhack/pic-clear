@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
title %~n0

REM ===========================================================
REM  依赖：
REM   - extract_frames.exe / dedupe_pic.exe 已放入 PATH
REM     推荐放到 C:\Windows\System32
REM   - 每个 exe 同目录下都要有 license.lic
REM ===========================================================

REM ---- 数据盘盘符（默认 Z:）----
set "DATA_DRIVE=Z:"

REM ---- 输出根目录（数据盘上，就近落盘）----
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

REM ---- Step A: 选 sjbz_* 顶层目录 ----
set "SRC_ROOT="
set "SRC_ROOT_NAME="
set "MATCH_COUNT=0"
for /d %%D in ("%DATA_DRIVE%\%DATA_PREFIX%*") do (
    set /a MATCH_COUNT+=1
    set "SRC_ROOT=%%~fD"
    set "SRC_ROOT_NAME=%%~nxD"
)

if "%MATCH_COUNT%"=="0" (
    echo [提示] 在 %DATA_DRIVE%\ 下没有找到 %DATA_PREFIX%* 目录。
    echo        请把源目录路径拖到本窗口，或手工输入，然后回车：
    set /p "SRC_ROOT=源目录: "
    if not defined SRC_ROOT ( echo 未提供，退出。 & pause & exit /b 2 )
    for %%X in ("!SRC_ROOT!") do set "SRC_ROOT_NAME=%%~nxX"
) else if "%MATCH_COUNT%"=="1" (
    echo [自动] 唯一 sjbz 目录：!SRC_ROOT!
) else (
    echo [选择] 找到多个 %DATA_PREFIX%* 目录：
    set "IDX=0"
    for /d %%D in ("%DATA_DRIVE%\%DATA_PREFIX%*") do (
        set /a IDX+=1
        set "ROOT_!IDX!=%%~fD"
        echo    [!IDX!] %%~nxD
    )
    echo.
    set /p "PICK=请输入编号: "
    call set "SRC_ROOT=%%ROOT_!PICK!%%"
    if not defined SRC_ROOT ( echo 无效选择，退出。 & pause & exit /b 2 )
    for %%X in ("!SRC_ROOT!") do set "SRC_ROOT_NAME=%%~nxX"
)

echo.
echo ------------------------------------------------------------
echo   sjbz 根目录 ： %SRC_ROOT%
echo ------------------------------------------------------------
echo.

REM ---- Step B: 列出 sjbz_root 下的一级子目录，让用户选 ----
set "SUB_COUNT=0"
for /d %%D in ("%SRC_ROOT%\*") do (
    set /a SUB_COUNT+=1
    set "SUB_!SUB_COUNT!=%%~nxD"
)

if "%SUB_COUNT%"=="0" (
    echo [提示] %SRC_ROOT% 下没有一级子目录，将直接对整个目录处理。
    set "SELECTED[1]=."
    set "SELECTED_COUNT=1"
    goto :SUB_DONE
)

echo [子目录] %SRC_ROOT% 下的一级子目录（共 %SUB_COUNT% 个）：
for /l %%i in (1,1,%SUB_COUNT%) do (
    echo    [%%i] !SUB_%%i!
)
echo.
echo 输入方式（可组合）：
echo    - 序号列表（逗号分隔）：  1,2
echo    - 序号区间：              1-3
echo    - 全部：                  all
echo.
set /p "SEL=请输入你要处理的子目录: "
if not defined SEL ( echo 未输入，退出。 & pause & exit /b 2 )

REM ---- 解析选择，写入 SELECTED[1..N] ----
set "SELECTED_COUNT=0"

REM 全部
if /I "!SEL!"=="all" (
    for /l %%i in (1,1,%SUB_COUNT%) do (
        set /a SELECTED_COUNT+=1
        set "SELECTED[!SELECTED_COUNT!]=!SUB_%%i!"
    )
    goto :SUB_DONE
)

REM 逗号分隔 -> 空格分隔（for %%t 遍历）
set "TOKENS=!SEL:,= !"
for %%T in (!TOKENS!) do (
    set "TOK=%%T"
    REM 判断是否是区间 a-b
    echo !TOK! | findstr /r "^[0-9][0-9]*-[0-9][0-9]*$" >nul && (
        for /f "tokens=1,2 delims=-" %%a in ("!TOK!") do (
            set "A=%%a"
            set "B=%%b"
            for /l %%k in (!A!,1,!B!) do (
                if %%k GEQ 1 if %%k LEQ %SUB_COUNT% (
                    set /a SELECTED_COUNT+=1
                    call set "NAME=%%SUB_%%k%%"
                    call set "SELECTED[!SELECTED_COUNT!]=%%NAME%%"
                )
            )
        )
    ) || (
        REM 单个序号
        echo !TOK! | findstr /r "^[0-9][0-9]*$" >nul && (
            set "K=!TOK!"
            if !K! GEQ 1 if !K! LEQ %SUB_COUNT% (
                set /a SELECTED_COUNT+=1
                call set "NAME=%%SUB_!K!%%"
                call set "SELECTED[!SELECTED_COUNT!]=!NAME!"
            )
        )
    )
)

if "!SELECTED_COUNT!"=="0" (
    echo [错误] 输入 "!SEL!" 无法解析出有效子目录。
    pause & exit /b 2
)

:SUB_DONE
echo.
echo [已选] 共 !SELECTED_COUNT! 个子目录，将依次处理：
for /l %%i in (1,1,!SELECTED_COUNT!) do (
    call echo    - %%SELECTED[%%i]%%
)
echo.

REM 建议关闭 QuickEdit
echo [提示] 如果窗口卡住无输出，按一下 Enter 或 Esc 恢复。
echo        建议永久关闭"快速编辑模式"：右键窗口标题 -^> 属性 -^> 编辑选项。
echo.

where dedupe_pic.exe >nul 2>nul || (
    echo [错误] 找不到 dedupe_pic.exe。 & pause & exit /b 3
)

for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TS=%%t"

set "OVERALL_RC=0"
for /l %%i in (1,1,!SELECTED_COUNT!) do (
    call set "SUBNAME=%%SELECTED[%%i]%%"
    call :DO_DEDUPE %%i
    if errorlevel 1 set "OVERALL_RC=1"
)

echo.
echo ============================================================
echo   去重完毕，总退出码：%OVERALL_RC%
echo ============================================================
pause
endlocal
exit /b %OVERALL_RC%


:DO_DEDUPE
setlocal EnableDelayedExpansion
if "!SUBNAME!"=="." (
    set "DST_DIR=%OUT_ROOT%\%SRC_ROOT_NAME%"
    set "SRC_DIR=%SRC_ROOT%"
) else (
    set "DST_DIR=%OUT_ROOT%\%SRC_ROOT_NAME%\!SUBNAME!"
    set "SRC_DIR=%SRC_ROOT%\!SUBNAME!"
)

REM 优先对抽帧结果目录去重；不存在则回退到源目录
set "TARGET_DIR=!DST_DIR!"
if not exist "!TARGET_DIR!\" (
    echo [提示] 抽帧输出目录不存在，改用源目录 !SRC_DIR!
    set "TARGET_DIR=!SRC_DIR!"
)

echo.
echo ############################################################
echo   [%1/!SELECTED_COUNT!] 去重：!SUBNAME!
echo   目标：!TARGET_DIR!
echo ############################################################

set "REPORT_CSV=!DST_DIR!\dedupe_report_%TS%.csv"
set "TRASH_DIR=!DST_DIR!\_trash_%TS%"

echo --- dry-run ---
dedupe_pic.exe "!TARGET_DIR!" --threshold 3 --report "!REPORT_CSV!"
if errorlevel 1 ( endlocal & exit /b 1 )

echo.
choice /C YN /M "子目录 !SUBNAME! 确认真删？"
if errorlevel 2 (
    echo [跳过] !SUBNAME!
    endlocal & exit /b 0
)

dedupe_pic.exe "!TARGET_DIR!" --threshold 3 --apply --trash-dir "!TRASH_DIR!" --report "!REPORT_CSV!"
set "RC=!ERRORLEVEL!"
endlocal & exit /b %RC%
