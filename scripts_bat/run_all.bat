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

REM 检查两个 exe
where extract_frames.exe >nul 2>nul || (
    echo [错误] 找不到 extract_frames.exe。 & pause & exit /b 3
)
where dedupe_pic.exe >nul 2>nul || (
    echo [错误] 找不到 dedupe_pic.exe。 & pause & exit /b 3
)

REM 时间戳（整轮共用）
for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TS=%%t"

set "OVERALL_RC=0"
set "IDX=0"

for /l %%i in (1,1,!SELECTED_COUNT!) do (
    set /a IDX+=1
    call set "SUBNAME=%%SELECTED[%%i]%%"
    call :RUN_ONE
    if errorlevel 1 set "OVERALL_RC=1"
)

echo.
echo ============================================================
if "%OVERALL_RC%"=="0" (
    echo   全部子目录处理完成
) else (
    echo   处理完成，但至少 1 个子目录出错，请翻窗口日志
)
echo ============================================================
pause
endlocal
exit /b %OVERALL_RC%


REM ================== 单个子目录处理函数 ==================
:RUN_ONE
setlocal EnableDelayedExpansion
if "!SUBNAME!"=="." (
    set "SRC_DIR=%SRC_ROOT%"
    set "DST_DIR=%OUT_ROOT%\%SRC_ROOT_NAME%"
) else (
    set "SRC_DIR=%SRC_ROOT%\!SUBNAME!"
    set "DST_DIR=%OUT_ROOT%\%SRC_ROOT_NAME%\!SUBNAME!"
)
if not exist "!DST_DIR!" mkdir "!DST_DIR!" 2>nul

echo.
echo ############################################################
echo   [!IDX!/!SELECTED_COUNT!] 处理子目录：!SUBNAME!
echo   源  ： !SRC_DIR!
echo   目标： !DST_DIR!
echo ############################################################

REM --- 1) 抽帧 ---
echo.
echo --- 抽帧 ---
extract_frames.exe "!SRC_DIR!" "!DST_DIR!" --fps 1 --ext .h265
if errorlevel 1 (
    echo [错误] 抽帧失败：!SUBNAME!
    endlocal & exit /b 1
)

REM --- 2) dry-run ---
echo.
echo --- 去重 dry-run ---
set "REPORT_CSV=!DST_DIR!\dedupe_report_%TS%.csv"
set "TRASH_DIR=!DST_DIR!\_trash_%TS%"
dedupe_pic.exe "!DST_DIR!" --threshold 3 --report "!REPORT_CSV!"
if errorlevel 1 (
    echo [错误] dry-run 失败：!SUBNAME!
    endlocal & exit /b 1
)

REM --- 3) 确认真删 ---
echo.
choice /C YNA /M "子目录 !SUBNAME! dry-run 完毕，Y=真删 / N=跳过此目录 / A=对剩余全部真删"
set "CHOICE_RC=!ERRORLEVEL!"
REM CHOICE_RC: 1=Y 2=N 3=A
if !CHOICE_RC! EQU 2 (
    echo [跳过] 未删除 !SUBNAME!
    endlocal & exit /b 0
)
if !CHOICE_RC! EQU 3 set "AUTO_APPLY_ALL=1"

echo.
echo --- 真删除 (软删到 !TRASH_DIR!) ---
dedupe_pic.exe "!DST_DIR!" --threshold 3 --apply --trash-dir "!TRASH_DIR!" --report "!REPORT_CSV!"
if errorlevel 1 (
    echo [错误] 真删失败：!SUBNAME!
    endlocal & exit /b 1
)
echo [OK] !SUBNAME! 完成
endlocal & exit /b 0
