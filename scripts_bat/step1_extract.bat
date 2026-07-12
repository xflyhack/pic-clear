@echo off
chcp 65001 >nul
@echo off
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

REM ---- Step B: 列出 sjbz_root 下一级子目录，让用户选 ----
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
    call echo    [%%i] %%SUB_%%i%%
)
echo.
echo 输入方式（可组合）：
echo    - 序号列表（逗号分隔）：  1,2
echo    - 序号区间：              1-3
echo    - 全部：                  all
echo.
set /p "SEL=请输入你要处理的子目录: "
if not defined SEL ( echo 未输入，退出。 & pause & exit /b 2 )
set "SELECTED_COUNT=0"

REM all 快捷路径
if /I "!SEL!"=="all" (
    for /l %%i in (1,1,%SUB_COUNT%) do (
        set /a SELECTED_COUNT+=1
        call set "NAME=%%SUB_%%i%%"
        call set "SELECTED[!SELECTED_COUNT!]=%%NAME%%"
    )
    goto :SUB_DONE
)

REM 逗号分隔 -> 空格分隔，逐个 token 走子过程
set "TOKENS=!SEL:,= !"
for %%T in (!TOKENS!) do call :ADD_SEL "%%T"

if "!SELECTED_COUNT!"=="0" (
    echo [错误] 输入 "!SEL!" 无法解析出有效子目录。
    pause & exit /b 2
)

goto :SUB_DONE


REM ============ :ADD_SEL：处理一个 token（'2' 或 '1-3'）====================
:ADD_SEL
set "TOK=%~1"
echo(!TOK!| findstr /r "^[0-9][0-9]*-[0-9][0-9]*$" >nul
if not errorlevel 1 (
    for /f "tokens=1,2 delims=-" %%a in ("!TOK!") do (
        for /l %%k in (%%a,1,%%b) do call :ADD_ONE %%k
    )
    goto :EOF
)
echo(!TOK!| findstr /r "^[0-9][0-9]*$" >nul
if not errorlevel 1 (
    call :ADD_ONE !TOK!
)
goto :EOF


REM ============ :ADD_ONE：把一个序号追加到 SELECTED[] =====================
:ADD_ONE
set "K=%~1"
if %K% LSS 1 goto :EOF
if %K% GTR %SUB_COUNT% goto :EOF
set /a SELECTED_COUNT+=1
REM 用 call 二次解析，让 %SUB_1% 这种间接变量能拿到值
call set "NAME=%%SUB_%K%%%"
call set "SELECTED[%SELECTED_COUNT%]=%%NAME%%"
goto :EOF


:SUB_DONE
echo.
echo [已选] 共 !SELECTED_COUNT! 个子目录，将依次处理：
for /l %%i in (1,1,!SELECTED_COUNT!) do (
    call echo    - %%SELECTED[%%i]%%
)
echo.
echo [提示] 如果窗口卡住看起来无输出，按一下 Enter 或 Esc 恢复。
echo        建议永久关闭"快速编辑模式"：右键窗口标题 -^> 属性 -^> 编辑选项。
echo.

where extract_frames.exe >nul 2>nul || (
    echo [错误] 找不到 extract_frames.exe。 & pause & exit /b 3
)

set "OVERALL_RC=0"
for /l %%i in (1,1,!SELECTED_COUNT!) do (
    call set "SUBNAME=%%SELECTED[%%i]%%"
    call :DO_EXTRACT %%i
    if errorlevel 1 set "OVERALL_RC=1"
)

echo.
echo ============================================================
echo   抽帧完毕，总退出码：%OVERALL_RC%
echo ============================================================
pause
endlocal
exit /b %OVERALL_RC%


:DO_EXTRACT
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
echo   [%1/!SELECTED_COUNT!] 抽帧：!SUBNAME!
echo   源  ： !SRC_DIR!
echo   目标： !DST_DIR!
echo ############################################################
for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format HH:mm:ss"') do echo [%%t] START extract_frames.exe
extract_frames.exe "!SRC_DIR!" "!DST_DIR!" --fps 1 --ext .h265
set "RC=!ERRORLEVEL!"
endlocal & exit /b %RC%
