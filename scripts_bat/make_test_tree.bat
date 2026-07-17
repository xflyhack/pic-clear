@echo off
setlocal EnableExtensions EnableDelayedExpansion
>nul chcp 65001
title %~n0

REM ============================================================
REM  make_test_tree.bat
REM  生成一批"深层目录"用于测试递归遍历软件
REM
REM  目录结构 (示例):
REM    <盘符>\切帧结果\sjbz_20260707_1ces\2\
REM        <日期> <地名>\<站点>-<编号>-NV\<HHMMSS>\clip_2026_07_07_HH_MM_SS_XXXX\
REM            camera\
REM                GEN6_<4位>_260707_HHMMSS_camera01
REM                GEN6_<4位>_260707_HHMMSS_camera02
REM                ...
REM
REM  用法:
REM    make_test_tree.bat                       使用默认参数
REM    make_test_tree.bat <盘符>                指定盘符, 如 D:
REM    make_test_tree.bat <盘符> <CLIP数>       clip 目录个数
REM    make_test_tree.bat <盘符> <CLIP数> <CAM数>  每个 clip 下 camera 子目录个数
REM
REM  默认:
REM    盘符   = 当前脚本所在盘
REM    CLIP数 = 3
REM    CAM数  = 10   (会生成 camera01..camera10)
REM ============================================================

REM ---- 参数 ----
set "DRIVE=%~1"
if not defined DRIVE set "DRIVE=%~d0"
set "CLIP_COUNT=%~2"
if not defined CLIP_COUNT set "CLIP_COUNT=3"
set "CAM_COUNT=%~3"
if not defined CAM_COUNT set "CAM_COUNT=10"

set "ROOT=%DRIVE%\切帧结果\sjbz_20260707_1ces\2"

echo ============================================================
echo   make_test_tree  生成测试目录
echo ============================================================
echo   盘符     : %DRIVE%
echo   根目录   : %ROOT%
echo   clip 数  : %CLIP_COUNT%
echo   camera数 : %CAM_COUNT%   (camera01..camera%CAM_COUNT%)
echo ============================================================
echo.

REM ---- 随机池 ----
set "PLACES=QINGTIAN BEIYUAN NANSHAN XILING DONGGUAN HUANGSHAN LUOYANG CHANGAN"
set "SITES=MAHUILING LIULINGHE SANYUANQIAO YUEXIULU HUACHENG DALIANWAN QILINGANG"

REM ---- 创建根 ----
if not exist "%ROOT%\" (
    mkdir "%ROOT%" 2>nul
    if errorlevel 1 (
        echo [ERR] 无法创建根目录: %ROOT%
        pause & exit /b 2
    )
)

set /a TOTAL_LEAF=0

for /l %%C in (1,1,%CLIP_COUNT%) do (
    call :MAKE_ONE_CLIP %%C
)

echo.
echo ============================================================
echo   完成. 共创建 !TOTAL_LEAF! 个叶子 camera 目录
echo   根路径: %ROOT%
echo ============================================================
pause
exit /b 0


REM ============================================================
REM  :MAKE_ONE_CLIP <序号>
REM ============================================================
:MAKE_ONE_CLIP
setlocal EnableDelayedExpansion

REM 从 PLACES / SITES 随机挑一个
call :PICK_ONE "%PLACES%" PLACE
call :PICK_ONE "%SITES%"  SITE

REM 随机站点编号 10..99
set /a SITE_NUM=10 + !RANDOM! %% 90

REM 随机时刻 HHMMSS (小时 08..18)
set /a HH=8 + !RANDOM! %% 11
set /a MM=!RANDOM! %% 60
set /a SS=!RANDOM! %% 60
if !HH! LSS 10 set "HH=0!HH!"
if !MM! LSS 10 set "MM=0!MM!"
if !SS! LSS 10 set "SS=0!SS!"
set "HMS=!HH!!MM!!SS!"

REM clip 后 4 位随机序号
set /a CLIP_SEQ=!RANDOM! %% 10000
set "CLIP_SEQ_STR=0000!CLIP_SEQ!"
set "CLIP_SEQ_STR=!CLIP_SEQ_STR:~-4!"

REM GEN6 前 4 位随机
set /a GEN_NUM=!RANDOM! %% 10000
set "GEN_NUM_STR=0000!GEN_NUM!"
set "GEN_NUM_STR=!GEN_NUM_STR:~-4!"

set "L1=0707 !PLACE!"
set "L2=!SITE!-!SITE_NUM!-NV"
set "L3=!HMS!"
set "L4=clip_2026_07_07_!HH!_!MM!_!SS!_!CLIP_SEQ_STR!"
set "CAM_DIR=%ROOT%\!L1!\!L2!\!L3!\!L4!\camera"

echo [%~1/%CLIP_COUNT%] 创建: !CAM_DIR!
mkdir "!CAM_DIR!" 2>nul

set /a LOCAL_LEAF=0
for /l %%N in (1,1,%CAM_COUNT%) do (
    set "IDX=0%%N"
    set "IDX=!IDX:~-2!"
    set "LEAF=!CAM_DIR!\GEN6_!GEN_NUM_STR!_260707_!HMS!_camera!IDX!"
    mkdir "!LEAF!" 2>nul
    if not errorlevel 1 set /a LOCAL_LEAF+=1
)
echo         叶子目录: !LOCAL_LEAF! 个

endlocal & set /a TOTAL_LEAF+=%CAM_COUNT%
goto :eof


REM ============================================================
REM  :PICK_ONE  "<空格分隔字符串>"  <输出变量名>
REM ============================================================
:PICK_ONE
setlocal EnableDelayedExpansion
set "LIST=%~1"
set /a N=0
for %%X in (!LIST!) do set /a N+=1
set /a PICK=!RANDOM! %% N
set /a I=0
for %%X in (!LIST!) do (
    if !I! EQU !PICK! (
        endlocal & set "%~2=%%X"
        goto :eof
    )
    set /a I+=1
)
endlocal
goto :eof
