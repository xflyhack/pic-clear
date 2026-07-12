@echo off
setlocal EnableExtensions EnableDelayedExpansion
>nul chcp 65001
@echo off
title %~n0

REM ============================================================
REM  step2_dedupe.bat
REM  只做去重：对 Z:\切帧结果\sjbz_YYYYMMDD\子目录 逐个 dry-run + 二次确认真删
REM  依赖：dedupe_pic.exe 在 PATH (推荐 C:\Windows\System32)
REM ============================================================

set "DATA_DRIVE=Z:"
set "OUT_ROOT=%DATA_DRIVE%\切帧结果"
set "DATA_PREFIX=sjbz_"

echo ============================================================
echo   step2_dedupe  只做去重
echo ============================================================
call :LOG_INFO "数据盘   : %DATA_DRIVE%"
call :LOG_INFO "输出根   : %OUT_ROOT%"
call :LOG_INFO "目录前缀 : %DATA_PREFIX%*"
echo ============================================================
echo.

if not exist "%DATA_DRIVE%\" (
    call :LOG_ERR "数据盘 %DATA_DRIVE% 不存在，请先挂载或检查 net use"
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
    call :LOG_WARN "在 %DATA_DRIVE%\ 下没有找到 %DATA_PREFIX%* 目录"
    call :LOG_INFO "请把源目录路径拖到本窗口，或手工输入，然后回车："
    set /p "SRC_ROOT=源目录: "
    if not defined SRC_ROOT ( call :LOG_ERR "未提供，退出" & pause & exit /b 2 )
    for %%X in ("!SRC_ROOT!") do set "SRC_ROOT_NAME=%%~nxX"
) else if "%MATCH_COUNT%"=="1" (
    call :LOG_INFO "唯一 sjbz 目录: !SRC_ROOT!"
) else (
    call :LOG_INFO "找到多个 %DATA_PREFIX%* 目录，请选择："
    set "IDX=0"
    for /d %%D in ("%DATA_DRIVE%\%DATA_PREFIX%*") do (
        set /a IDX+=1
        set "ROOT_!IDX!=%%~fD"
        echo         [!IDX!] %%~nxD
    )
    echo.
    set /p "PICK=请输入编号: "
    call set "SRC_ROOT=%%ROOT_!PICK!%%"
    if not defined SRC_ROOT ( call :LOG_ERR "无效选择，退出" & pause & exit /b 2 )
    for %%X in ("!SRC_ROOT!") do set "SRC_ROOT_NAME=%%~nxX"
)

echo.
call :LOG_INFO "sjbz 根目录: %SRC_ROOT%"
echo.

REM ---- Step B: 选一级子目录 ----
set "SUB_COUNT=0"
for /d %%D in ("%SRC_ROOT%\*") do (
    set /a SUB_COUNT+=1
    set "SUB_!SUB_COUNT!=%%~nxD"
)

if "%SUB_COUNT%"=="0" (
    call :LOG_WARN "%SRC_ROOT% 下没有一级子目录，将直接对整个目录处理"
    set "SELECTED[1]=."
    set "SELECTED_COUNT=1"
    goto :SUB_DONE
)

call :LOG_INFO "%SRC_ROOT% 下一级子目录 (共 %SUB_COUNT% 个):"
for /l %%i in (1,1,%SUB_COUNT%) do (
    call echo         [%%i] %%SUB_%%i%%
)
echo.
call :LOG_INFO "输入方式（可组合）："
call :LOG_INFO "  - 序号列表 (逗号分隔) : 1,2"
call :LOG_INFO "  - 序号区间             : 1-3"
call :LOG_INFO "  - 全部                 : all"
echo.
set /p "SEL=请输入你要处理的子目录: "
if not defined SEL ( call :LOG_ERR "未输入，退出" & pause & exit /b 2 )
set "SELECTED_COUNT=0"

if /I "!SEL!"=="all" (
    for /l %%i in (1,1,%SUB_COUNT%) do (
        set /a SELECTED_COUNT+=1
        call set "NAME=%%SUB_%%i%%"
        call set "SELECTED[!SELECTED_COUNT!]=%%NAME%%"
    )
    goto :SUB_DONE
)

set "TOKENS=!SEL:,= !"
for %%T in (!TOKENS!) do call :ADD_SEL "%%T"

if "!SELECTED_COUNT!"=="0" (
    call :LOG_ERR "输入 \"!SEL!\" 无法解析出有效子目录"
    pause & exit /b 2
)

goto :SUB_DONE

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

:ADD_ONE
set "K=%~1"
if %K% LSS 1 goto :EOF
if %K% GTR %SUB_COUNT% goto :EOF
set /a SELECTED_COUNT+=1
call set "NAME=%%SUB_%K%%%"
call set "SELECTED[%SELECTED_COUNT%]=%%NAME%%"
goto :EOF


:SUB_DONE
echo.
call :LOG_INFO "已选 !SELECTED_COUNT! 个子目录，将依次去重:"
for /l %%i in (1,1,!SELECTED_COUNT!) do (
    call echo         - %%SELECTED[%%i]%%
)
echo.
call :LOG_INFO "提示: 若窗口看似卡住无输出，按一下 Enter 或 Esc 恢复"
call :LOG_INFO "      建议永久关闭快速编辑模式: 右键窗口标题 -^> 属性 -^> 编辑选项"
echo.

where dedupe_pic.exe >nul 2>nul
if errorlevel 1 (
    call :LOG_ERR "找不到 dedupe_pic.exe"
    pause & exit /b 3
)

REM 用 cmd 内置变量拼时间戳，不再 spawn powershell
set "TS_D=%DATE:~0,4%%DATE:~5,2%%DATE:~8,2%"
set "TS_T=%TIME:~0,2%%TIME:~3,2%%TIME:~6,2%"
set "TS_T=%TS_T: =0%"
set "TS=%TS_D%_%TS_T%"

set "OVERALL_RC=0"
for /l %%i in (1,1,!SELECTED_COUNT!) do (
    call set "SUBNAME=%%SELECTED[%%i]%%"
    call :DO_DEDUPE %%i
    if errorlevel 1 set "OVERALL_RC=1"
)

echo.
echo ============================================================
if "%OVERALL_RC%"=="0" (
    call :LOG_OK "全部子目录去重完成"
) else (
    call :LOG_ERR "去重完成，但至少 1 个子目录出错，请翻窗口日志"
)
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

set "TARGET_DIR=!DST_DIR!"
if not exist "!TARGET_DIR!\" (
    call :LOG_WARN "抽帧输出目录不存在，改用源目录: !SRC_DIR!"
    set "TARGET_DIR=!SRC_DIR!"
)

echo.
call :LOG_STEP "(%1/!SELECTED_COUNT!) 去重: !SUBNAME!"
call :LOG_INFO "  目标 : !TARGET_DIR!"

set "REPORT_CSV=!DST_DIR!\dedupe_report_%TS%.csv"
set "TRASH_DIR=!DST_DIR!\_trash_%TS%"

set "T=%TIME:~0,8%"
call :LOG_INFO "  %T%  开始 dry-run"
dedupe_pic.exe "!TARGET_DIR!" --threshold 3 --report "!REPORT_CSV!"
if errorlevel 1 (
    call :LOG_ERR "  dry-run 失败: !SUBNAME!"
    endlocal & exit /b 1
)

echo.
choice /C YN /M "子目录 !SUBNAME! 确认真删"
if errorlevel 2 (
    call :LOG_INFO "  跳过: !SUBNAME!"
    endlocal & exit /b 0
)

set "T=%TIME:~0,8%"
call :LOG_INFO "  %T%  开始真删 (软删到 !TRASH_DIR!)"
dedupe_pic.exe "!TARGET_DIR!" --threshold 3 --apply --trash-dir "!TRASH_DIR!" --report "!REPORT_CSV!"
set "RC=!ERRORLEVEL!"

set "T=%TIME:~0,8%"
if "!RC!"=="0" (
    call :LOG_OK "  %T%  (%1/!SELECTED_COUNT!) 完成: !SUBNAME!"
) else (
    call :LOG_ERR "  %T%  (%1/!SELECTED_COUNT!) 真删失败: !SUBNAME!  rc=!RC!"
)
endlocal & exit /b %RC%


REM ====================================================================
REM  日志子过程
REM ====================================================================
:LOG_INFO
echo [INFO ] %~1
goto :EOF
:LOG_OK
echo [ OK  ] %~1
goto :EOF
:LOG_WARN
echo [WARN ] %~1
goto :EOF
:LOG_ERR
echo [ERROR] %~1
goto :EOF
:LOG_STEP
echo [STEP ] %~1
goto :EOF
