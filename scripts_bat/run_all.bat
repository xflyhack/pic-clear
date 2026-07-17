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
REM  run_all.bat
REM  one-stop: extract frames, then optionally spawn dedupe_watcher
REM  or run dedupe serially.
REM  Requires: extract_frames.exe / dedupe_pic.exe in PATH,
REM            license.lic in the same dir as the exe.
REM ============================================================

REM ---- config (edit here if your paths differ) ----
set "OUT_ROOT=Z:\切帧结果"
REM MARKERS_ROOT: dir holding _extract.lock / _done.marker per video.
REM All machines on the shared drive MUST point to the same location.
set "MARKERS_ROOT=Z:\切帧标记"
REM VIDEO_EXTS: comma-separated video extensions to extract.
REM Passed as --ext to extract_frames.exe; keep in sync with extract_gui defaults.
set "VIDEO_EXTS=h265,mp4"
REM MOTION_THRESHOLD: car motion guard, larger = stricter (delete more).
REM   0.05 = exe default, even sub-pixel jitter counts as motion
REM   0.12 = bat default, must move >12%% of frame width/height
REM   0.20 = very strict, car must basically drive away
set "MOTION_THRESHOLD=0.12"
REM DEDUPE_THRESHOLD: hamming distance threshold, larger = looser.
REM   default 3 keeps behavior identical to previous bats.
set "DEDUPE_THRESHOLD=3"
REM LOCK_TTL: seconds a stale lock is considered dead (multi-machine).
REM   same default as extract_gui / dedupe_gui.
set "LOCK_TTL=900"

echo ============================================================
echo   run_all  一站式抽帧 + 去重
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
call :LOG_INFO "已选 !SELECTED_COUNT! 个子目录,将依次处理:"
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
where dedupe_pic.exe >nul 2>nul
if errorlevel 1 (
    call :LOG_ERR "找不到 dedupe_pic.exe"
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

REM 时间戳(cmd 内置变量拼装,不启动 powershell)
set "TS_D=%DATE:~0,4%%DATE:~5,2%%DATE:~8,2%"
set "TS_T=%TIME:~0,2%%TIME:~3,2%%TIME:~6,2%"
set "TS_T=%TS_T: =0%"
set "TS=%TS_D%_%TS_T%"

REM ---- 是否开启场景保护(纯色/渐变屏等异常帧不删) ----
echo.
call :LOG_INFO "场景保护: 把明显的纯色/渐变屏(传感器遮挡等)识别为异常帧,强制保留"
call :LOG_INFO "  Y = 开启(多保留一些图,严格不删纯色屏)"
call :LOG_INFO "  N = 关闭(与旧版一致,纯色屏也可能被删)"
choice /C YN /M "是否开启场景保护 (Y=开启 / N=不开启)"
set "SCENE_FLAG="
if errorlevel 2 goto :SCENE_DONE
set "SCENE_FLAG=--scene-protect"
call :LOG_INFO "场景保护已开启(会传 --scene-protect 给 dedupe_pic.exe)"
:SCENE_DONE
echo.
REM ---- 是否启动 dedupe_watcher 后台窗口 ----
echo.
call :LOG_INFO "并行模式: 抽帧的同时后台窗口自动去重,磁盘不攒垃圾"
call :LOG_INFO "  - watcher 会持续扫描 %OUT_ROOT%\%SRC_ROOT_NAME%,看到 _done.marker 就去重"
call :LOG_INFO "  - 抽帧下一个视频时,上一个已在被清理"
echo.
choice /C YN /M "现在启动一个去重监听后台窗口 (Y=启动真删 / N=不启动,改走串行去重)"
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
    call :LOG_WARN "找不到 dedupe_watcher.bat,跳过监听窗口"
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
set "WATCH_SCENE="
if defined SCENE_FLAG set "WATCH_SCENE=/scene"
start "dedupe_watcher" cmd /k ""!WATCHER_BAT!" "!WATCH_TARGET!" /apply /motion %MOTION_THRESHOLD% !WATCH_SCENE!"
call :LOG_INFO "监听窗口已弹出 (真删模式,重复图会被永久删除,不落回收站)"
call :LOG_INFO "如需 dry-run 观察,可另开: dedupe_watcher.bat \"!WATCH_TARGET!\""
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
    call :LOG_ERR "处理完成,但至少 1 个子目录出错,请翻窗口日志"
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
    set "MK_DIR=%MARKERS_ROOT%\%SRC_ROOT_NAME%"
) else (
    set "SRC_DIR=%SRC_ROOT%\!SUBNAME!"
    set "DST_DIR=%OUT_ROOT%\%SRC_ROOT_NAME%\!SUBNAME!"
    set "MK_DIR=%MARKERS_ROOT%\%SRC_ROOT_NAME%\!SUBNAME!"
)
if not exist "!DST_DIR!" mkdir "!DST_DIR!" 2>nul
if not exist "!MK_DIR!" mkdir "!MK_DIR!" 2>nul

echo.
call :LOG_STEP "(!IDX!/!SELECTED_COUNT!) 子目录: !SUBNAME!"
call :LOG_INFO "  源   : !SRC_DIR!"
call :LOG_INFO "  目标 : !DST_DIR!"
call :LOG_INFO "  标记 : !MK_DIR!"

set "T=%TIME:~0,8%"
call :LOG_INFO "  %T%  抽帧开始"
extract_frames.exe "!SRC_DIR!" "!DST_DIR!" --fps 1 --ext %VIDEO_EXTS% --lock-ttl %LOCK_TTL% --markers-root "!MK_DIR!" %NAME_ARGS%
if errorlevel 1 (
    set "T=%TIME:~0,8%"
    call :LOG_ERR "  %T%  抽帧失败: !SUBNAME!"
    endlocal & exit /b 1
)
set "T=%TIME:~0,8%"
call :LOG_OK "  %T%  抽帧完成: !SUBNAME!"

if "%USE_WATCHER%"=="1" (
    call :LOG_INFO "  后续去重已交给 dedupe_watcher 后台窗口,跳过本进程去重"
    endlocal & exit /b 0
)

set "REPORT_CSV=!DST_DIR!\dedupe_report_%TS%.csv"

set "T=%TIME:~0,8%"
call :LOG_INFO "  %T%  去重 dry-run 开始"
dedupe_pic.exe "!DST_DIR!" --threshold %DEDUPE_THRESHOLD% --motion-threshold %MOTION_THRESHOLD% --marker-dir "!MK_DIR!" --lock-ttl %LOCK_TTL% !SCENE_FLAG! --report "!REPORT_CSV!"
if errorlevel 1 (
    call :LOG_ERR "  dry-run 失败: !SUBNAME!"
    endlocal & exit /b 1
)

echo.
choice /C YNA /M "子目录 !SUBNAME! dry-run 完毕,Y=真删 / N=跳过此目录 / A=对剩余全部真删"
set "CHOICE_RC=!ERRORLEVEL!"
if !CHOICE_RC! EQU 2 (
    call :LOG_INFO "  跳过真删: !SUBNAME!"
    endlocal & exit /b 0
)

set "T=%TIME:~0,8%"
call :LOG_INFO "  %T%  真删开始 (永久删除,不落回收站)"
dedupe_pic.exe "!DST_DIR!" --threshold %DEDUPE_THRESHOLD% --motion-threshold %MOTION_THRESHOLD% --marker-dir "!MK_DIR!" --lock-ttl %LOCK_TTL% !SCENE_FLAG! --apply --hard-delete --report "!REPORT_CSV!"
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
