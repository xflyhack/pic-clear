@echo off
setlocal EnableExtensions EnableDelayedExpansion
>nul chcp 65001
@echo off
title %~n0

REM ---- verify chcp 65001 actually took effect ----
REM  Some old Windows / VM environments silently ignore 'chcp 65001'.
REM  If it fails, non-ASCII bytes below are parsed as GBK and the whole
REM  bat blows up. Detect and abort early with an ASCII-only message.
set "CHCP_OK=0"
for /f "tokens=* delims=" %%A in ('chcp') do set "CHCP_LINE=%%A"
echo(!CHCP_LINE! | findstr /C:"65001" >nul && set "CHCP_OK=1"
if not "!CHCP_OK!"=="1" (
    echo [FATAL] chcp 65001 did not take effect on this machine.
    echo         current: !CHCP_LINE!
    echo.
    echo   How to fix:
    echo     1^) run 'chcp 65001' in this cmd window and try again, or
    echo     2^) use extract_gui.exe instead ^(no code-page issue^), or
    echo     3^) ask ops to enable UTF-8 in Region ^> Administrative
    echo        ^> "Beta: Use Unicode UTF-8 for worldwide language support".
    pause
    exit /b 4
)

REM ============================================================
REM  step1_extract.bat
REM  extract only: SRC/sjbz_*/subdir -> OUT_ROOT/sjbz_*/subdir
REM  Requires:
REM   - extract_frames.exe in PATH (e.g. C:\Windows\System32)
REM   - license.lic next to each exe
REM ============================================================

REM ---- config (edit here if your paths differ) ----
set "OUT_ROOT=Z:\切帧结果"
REM MARKERS_ROOT: dir holding _extract.lock / _done.marker per video.
REM All machines on the shared drive MUST point to the same location.
set "MARKERS_ROOT=Z:\切帧标记"
REM VIDEO_EXTS: comma-separated video extensions to extract.
REM Passed as --ext to extract_frames.exe; keep in sync with extract_gui defaults.
set "VIDEO_EXTS=h265,mp4"
REM LOCK_TTL: stale-lock TTL in seconds (multi-machine coordination).
REM   Must match run_all / extract_gui defaults.
set "LOCK_TTL=900"

echo ============================================================
echo   step1_extract  只做抽帧
echo ============================================================
echo.

REM ---- Step A: input source directory ----
REM  Drop the auto-detect for Z:\sjbz_*  - just ask the user to
REM  drag a folder into this window (or paste an absolute path).
set "SRC_ROOT="
set "SRC_ROOT_NAME="
call :LOG_INFO "把源视频目录拖到本窗口,或手工粘贴绝对路径,然后回车:"
set /p "SRC_ROOT=源目录: "
if not defined SRC_ROOT ( call :LOG_ERR "未提供源目录,退出" & pause & exit /b 2 )
REM strip surrounding quotes if the user dragged a path with spaces
set "SRC_ROOT=!SRC_ROOT:"=!"
if not exist "!SRC_ROOT!\" (
    call :LOG_ERR "源目录不存在: !SRC_ROOT!"
    pause & exit /b 2
)
for %%X in ("!SRC_ROOT!") do set "SRC_ROOT_NAME=%%~nxX"

echo.
call :LOG_INFO "源目录  : !SRC_ROOT!"
echo.

REM ---- Step A2: choose / override OUT_ROOT ----
REM  Empty input = keep the default at the top of this bat.
REM  MARKERS_ROOT follows OUT_ROOT: if user overrides OUT_ROOT,
REM  markers go to '<new OUT_ROOT>\.markers' automatically so both
REM  machines on a shared drive stay in sync without a second prompt.
call :LOG_INFO "默认输出根: %OUT_ROOT%"
call :LOG_INFO "  - 直接回车 = 用默认;  或输入新的绝对路径,例如 D:\pic-clear\frames"
set "OUT_INPUT="
set /p "OUT_INPUT=输出根 (回车=默认): "
if not defined OUT_INPUT goto :OUT_ROOT_READY
REM strip quotes if user dragged a folder with spaces
set "OUT_INPUT=!OUT_INPUT:"=!"
set "OUT_ROOT=!OUT_INPUT!"
REM 用户覆盖了 OUT_ROOT, markers 跟着走,避免二次输入
set "MARKERS_ROOT=!OUT_INPUT!\.markers"
:OUT_ROOT_READY
if not exist "!OUT_ROOT!" mkdir "!OUT_ROOT!" 2>nul
call :LOG_INFO "实际输出根: !OUT_ROOT!"
echo.

REM ---- Step A3: choose / override MARKERS_ROOT ----
REM  Marker root holds _extract.lock and _done.marker files.
REM  When multiple machines share the disk (Z:), they MUST all
REM  point to the same MARKERS_ROOT so they can coordinate:
REM  - _extract.lock prevents two machines from grabbing the same video
REM  - _done.marker lets a restarted job resume from the last video
REM  Empty input = keep current value (shown as '当前').
call :LOG_INFO "当前标记根: !MARKERS_ROOT!"
call :LOG_INFO "  - 直接回车 = 保持当前"
call :LOG_INFO "  - 或输入新的绝对路径,多机并发时所有机器要指向同一个位置"
call :LOG_INFO "    例如 Z:\pic-clear-markers"
set "MK_INPUT="
set /p "MK_INPUT=标记根 (回车=保持): "
if not defined MK_INPUT goto :MARKERS_ROOT_READY
REM strip quotes if user dragged a folder with spaces
set "MK_INPUT=!MK_INPUT:"=!"
set "MARKERS_ROOT=!MK_INPUT!"
:MARKERS_ROOT_READY
if not exist "!MARKERS_ROOT!" mkdir "!MARKERS_ROOT!" 2>nul
call :LOG_INFO "实际标记根: !MARKERS_ROOT!"
echo.

REM ---- Step B: 选一级子目录 ----
set "SUB_COUNT=0"
for /d %%D in ("%SRC_ROOT%\*") do (
    set /a SUB_COUNT+=1
    set "SUB_!SUB_COUNT!=%%~nxD"
)

if "%SUB_COUNT%"=="0" (
    call :LOG_WARN "%SRC_ROOT% 下没有一级子目录,将直接对整个目录处理"
    set "SELECTED[1]=."
    set "SELECTED_COUNT=1"
    goto :SUB_DONE
)

call :LOG_INFO "%SRC_ROOT% 下一级子目录 (共 %SUB_COUNT% 个):"
for /l %%i in (1,1,%SUB_COUNT%) do (
    call echo         [%%i] %%SUB_%%i%%
)
echo.
call :LOG_INFO "输入方式(可组合):"
call :LOG_INFO "  - 序号列表 (逗号分隔) : 1,2"
call :LOG_INFO "  - 序号区间             : 1-3"
call :LOG_INFO "  - 全部                 : all"
echo.
set /p "SEL=请输入你要处理的子目录: "
if not defined SEL ( call :LOG_ERR "未输入,退出" & pause & exit /b 2 )
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
call :LOG_INFO "已选 !SELECTED_COUNT! 个子目录,将依次抽帧:"
for /l %%i in (1,1,!SELECTED_COUNT!) do (
    call echo         - %%SELECTED[%%i]%%
)
echo.
call :LOG_INFO "提示: 若窗口看似卡住无输出,按一下 Enter 或 Esc 恢复"
call :LOG_INFO "      建议永久关闭快速编辑模式: 右键窗口标题 -^> 属性 -^> 编辑选项"
echo.

where extract_frames.exe >nul 2>nul
if errorlevel 1 (
    call :LOG_ERR "找不到 extract_frames.exe"
    pause & exit /b 3
)

REM ---- image name style: new or old, bat only supports these two ----
echo.
call :LOG_INFO "图片命名规则:"
call :LOG_INFO "  N = 新版  video1 - 副本_0001.jpg  (parent + 4 位补零,推荐)"
call :LOG_INFO "  O = 老版  frame_000001.jpg        (legacy + 6 位补零,兼容历史)"
choice /C NO /M "请选择命名规则 (N=新版 / O=老版)"
set "NAME_ARGS=--name-style parent --name-digits 4"
set "NAME_LABEL=新版 parent + 4 位"
if errorlevel 2 (
    set "NAME_ARGS=--name-style legacy --name-digits 6"
    set "NAME_LABEL=老版 legacy + 6 位"
)
call :LOG_INFO "命名规则   : !NAME_LABEL!"
call :LOG_INFO "命名参数   : !NAME_ARGS!"
call :LOG_INFO "视频扩展名 : %VIDEO_EXTS%"
echo.

set "OVERALL_RC=0"
for /l %%i in (1,1,!SELECTED_COUNT!) do (
    call set "SUBNAME=%%SELECTED[%%i]%%"
    call :DO_EXTRACT %%i
    if errorlevel 1 set "OVERALL_RC=1"
)

echo.
echo ============================================================
if "%OVERALL_RC%"=="0" (
    call :LOG_OK "全部子目录抽帧完成"
) else (
    call :LOG_ERR "抽帧完成,但至少 1 个子目录出错,请翻窗口日志"
)
echo ============================================================
pause
endlocal
exit /b %OVERALL_RC%


:DO_EXTRACT
setlocal EnableDelayedExpansion
if "!SUBNAME!"=="." (
    set "SRC_DIR=%SRC_ROOT%"
    set "DST_DIR=%OUT_ROOT%\%SRC_ROOT_NAME%"
    set "MK_DIR=%MARKERS_ROOT%\%SRC_ROOT_NAME%"
) else (
    set "SRC_DIR=%SRC_ROOT%\!SUBNAME!"
    set "DST_DIR=%OUT_ROOT%\%SRC_ROOT_NAME%\!SUBNAME!"
    set "MK_DIR=%MARKERS_ROOT%\%SRC_ROOT_NAME%\!SUBNAME!"
)
if not exist "!DST_DIR!" mkdir "!DST_DIR!" 2>nul
if not exist "!MK_DIR!" mkdir "!MK_DIR!" 2>nul

echo.
call :LOG_STEP "(%1/!SELECTED_COUNT!) 抽帧: !SUBNAME!"
call :LOG_INFO "  源   : !SRC_DIR!"
call :LOG_INFO "  目标 : !DST_DIR!"
call :LOG_INFO "  标记 : !MK_DIR!"
set "T=%TIME:~0,8%"
call :LOG_INFO "  %T%  开始"

extract_frames.exe "!SRC_DIR!" "!DST_DIR!" --fps 1 --ext %VIDEO_EXTS% --lock-ttl %LOCK_TTL% --markers-root "!MK_DIR!" %NAME_ARGS%
set "RC=!ERRORLEVEL!"

set "T=%TIME:~0,8%"
if "!RC!"=="0" (
    call :LOG_OK "  %T%  (%1/!SELECTED_COUNT!) 抽帧完成: !SUBNAME!"
) else (
    call :LOG_ERR "  %T%  (%1/!SELECTED_COUNT!) 抽帧失败: !SUBNAME!  rc=!RC!"
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
