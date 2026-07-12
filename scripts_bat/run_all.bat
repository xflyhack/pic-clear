@echo off
setlocal EnableExtensions EnableDelayedExpansion
>nul chcp 65001
@echo off
title %~n0

REM ============================================================
REM  run_all.bat
REM  一站式：先抽帧，(可选) 由 dedupe_watcher 后台并行去重，否则串行去重
REM  依赖：extract_frames.exe / dedupe_pic.exe 在 PATH，同目录下有 license.lic
REM ============================================================

set "DATA_DRIVE=Z:"
set "OUT_ROOT=%DATA_DRIVE%\切帧结果"
set "DATA_PREFIX=sjbz_"

echo ============================================================
echo   run_all  一站式抽帧 + 去重
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
call :LOG_INFO "已选 !SELECTED_COUNT! 个子目录，将依次处理:"
for /l %%i in (1,1,!SELECTED_COUNT!) do (
    call echo         - %%SELECTED[%%i]%%
)
echo.
call :LOG_INFO "提示: 若窗口看似卡住无输出，按一下 Enter 或 Esc 恢复"
call :LOG_INFO "      建议永久关闭快速编辑模式: 右键窗口标题 -^> 属性 -^> 编辑选项"
echo.

where extract_frames.exe >nul 2>nul
if errorlevel 1 (
    call :LOG_ERR "找不到 extract_frames.exe"
    pause & exit /b 3
)
where dedupe_pic.exe >nul 2>nul
if errorlevel 1 (
    call :LOG_ERR "找不到 dedupe_pic.exe"
    pause & exit /b 3
)

REM 时间戳（cmd 内置变量拼装，不启动 powershell）
set "TS_D=%DATE:~0,4%%DATE:~5,2%%DATE:~8,2%"
set "TS_T=%TIME:~0,2%%TIME:~3,2%%TIME:~6,2%"
set "TS_T=%TS_T: =0%"
set "TS=%TS_D%_%TS_T%"

REM ---- 是否启动 dedupe_watcher 后台窗口 ----
echo.
call :LOG_INFO "并行模式: 抽帧的同时后台窗口自动去重，磁盘不攒垃圾"
call :LOG_INFO "  - watcher 会持续扫描 %OUT_ROOT%\%SRC_ROOT_NAME%，看到 _done.marker 就去重"
call :LOG_INFO "  - 抽帧下一个视频时，上一个已在被清理"
echo.
choice /C YN /M "现在启动一个去重监听后台窗口 (Y=启动真删 / N=不启动，改走串行去重)"
set "USE_WATCHER=0"
if errorlevel 2 goto :SKIP_WATCHER
set "USE_WATCHER=1"

set "WATCH_TARGET=%OUT_ROOT%\%SRC_ROOT_NAME%"
if not exist "!WATCH_TARGET!" mkdir "!WATCH_TARGET!" 2>nul

set "WATCHER_BAT=%~dp0dedupe_watcher.bat"
if not exist "!WATCHER_BAT!" (
    for %%W in (dedupe_watcher.bat) do set "WATCHER_BAT=%%~$PATH:W"
)
if not defined WATCHER_BAT (
    call :LOG_WARN "找不到 dedupe_watcher.bat，跳过监听窗口"
    set "USE_WATCHER=0"
    goto :SKIP_WATCHER
)
if not exist "!WATCHER_BAT!" (
    call :LOG_WARN "dedupe_watcher.bat 不存在: !WATCHER_BAT!"
    set "USE_WATCHER=0"
    goto :SKIP_WATCHER
)

call :LOG_INFO "启动后台窗口 : !WATCHER_BAT!"
call :LOG_INFO "监听目录     : !WATCH_TARGET!"
start "dedupe_watcher" cmd /k ""!WATCHER_BAT!" "!WATCH_TARGET!" /apply"
call :LOG_INFO "监听窗口已弹出 (真删模式，重复图会被移到 _trash 目录)"
call :LOG_INFO "如需 dry-run 观察，可另开: dedupe_watcher.bat \"!WATCH_TARGET!\""
echo.

:SKIP_WATCHER

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
    call :LOG_OK "全部子目录处理完成"
) else (
    call :LOG_ERR "处理完成，但至少 1 个子目录出错，请翻窗口日志"
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
call :LOG_STEP "(!IDX!/!SELECTED_COUNT!) 子目录: !SUBNAME!"
call :LOG_INFO "  源   : !SRC_DIR!"
call :LOG_INFO "  目标 : !DST_DIR!"

set "T=%TIME:~0,8%"
call :LOG_INFO "  %T%  抽帧开始"
extract_frames.exe "!SRC_DIR!" "!DST_DIR!" --fps 1 --ext .h265
if errorlevel 1 (
    set "T=%TIME:~0,8%"
    call :LOG_ERR "  %T%  抽帧失败: !SUBNAME!"
    endlocal & exit /b 1
)
set "T=%TIME:~0,8%"
call :LOG_OK "  %T%  抽帧完成: !SUBNAME!"

if "%USE_WATCHER%"=="1" (
    call :LOG_INFO "  后续去重已交给 dedupe_watcher 后台窗口，跳过本进程去重"
    endlocal & exit /b 0
)

set "REPORT_CSV=!DST_DIR!\dedupe_report_%TS%.csv"
set "TRASH_DIR=!DST_DIR!\_trash_%TS%"

set "T=%TIME:~0,8%"
call :LOG_INFO "  %T%  去重 dry-run 开始"
dedupe_pic.exe "!DST_DIR!" --threshold 3 --report "!REPORT_CSV!"
if errorlevel 1 (
    call :LOG_ERR "  dry-run 失败: !SUBNAME!"
    endlocal & exit /b 1
)

echo.
choice /C YNA /M "子目录 !SUBNAME! dry-run 完毕，Y=真删 / N=跳过此目录 / A=对剩余全部真删"
set "CHOICE_RC=!ERRORLEVEL!"
if !CHOICE_RC! EQU 2 (
    call :LOG_INFO "  跳过真删: !SUBNAME!"
    endlocal & exit /b 0
)

set "T=%TIME:~0,8%"
call :LOG_INFO "  %T%  真删开始 (软删到 !TRASH_DIR!)"
dedupe_pic.exe "!DST_DIR!" --threshold 3 --apply --trash-dir "!TRASH_DIR!" --report "!REPORT_CSV!"
if errorlevel 1 (
    set "T=%TIME:~0,8%"
    call :LOG_ERR "  %T%  真删失败: !SUBNAME!"
    endlocal & exit /b 1
)
set "T=%TIME:~0,8%"
call :LOG_OK "  %T%  子目录完成: !SUBNAME!"
endlocal & exit /b 0


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
