@echo off
setlocal EnableExtensions EnableDelayedExpansion
>nul chcp 65001
@echo off
title %~n0

REM ---- verify chcp 65001 actually took effect ----
set "CHCP_OK=0"
for /f "tokens=* delims=" %%A in ('chcp') do set "CHCP_LINE=%%A"
echo(!CHCP_LINE! | findstr /C:"65001" >nul && set "CHCP_OK=1"
if not "!CHCP_OK!"=="1" (
    echo [FATAL] chcp 65001 did not take effect on this machine.
    echo         current: !CHCP_LINE!
    pause
    exit /b 4
)

REM ============================================================
REM  make_deep_dir.bat
REM  一键在指定盘符下创建深层测试目录:
REM
REM    <盘符>\切帧结果\sjbz_20260715_07_15REM        chengluyang_30_nv_qingtian_3100lux_CNCWREM        202684448\clip_2026_07_14_18_44_48_0001\camera
REM
REM  用法:
REM    make_deep_dir.bat                默认盘 = 脚本所在盘
REM    make_deep_dir.bat D:             指定盘符
REM    make_deep_dir.bat D: -img        额外在末端目录放 2 张占位 jpg
REM ============================================================

set "DRIVE=%~1"
if not defined DRIVE set "DRIVE=%~d0"

set "OPT=%~2"

set "SUB=\切帧结果\sjbz_20260715_07_15\chengluyang_30_nv_qingtian_3100lux_CNCW684448\clip_2026_07_14_18_44_48_0001\camera"
set "TARGET=%DRIVE%%SUB%"

echo ============================================================
echo   make_deep_dir  生成深层测试目录
echo ============================================================
echo   盘符   : %DRIVE%
echo   目标   : %TARGET%
echo   额外   : %OPT%
echo ============================================================
echo.

if not exist "%DRIVE%" (
    echo [ERR] 盘符不存在: %DRIVE%
    pause
    exit /b 2
)

mkdir "%TARGET%" 2>nul
if exist "%TARGET%" (
    echo [ OK ] 目录已就绪: %TARGET%
) else (
    echo [ERR] 创建失败: %TARGET%
    pause
    exit /b 3
)

if /i "%OPT%"=="-img" (
    echo.
    echo [INFO] 生成 2 张占位 jpg 到末端目录...
    REM  1x1 jpg 的最小合法字节 (base64 解码写入)
    call :WRITE_JPG "%TARGET%\placeholder_0001.jpg"
    call :WRITE_JPG "%TARGET%\placeholder_0002.jpg"
    dir /b "%TARGET%\*.jpg"
)

echo.
echo ============================================================
echo   完成
echo ============================================================
pause
exit /b 0


REM ============================================================
REM  :WRITE_JPG  <目标文件>
REM   用 certutil -decode 把一张最小 1x1 jpg 写出来
REM ============================================================
:WRITE_JPG
set "OUT=%~1"
set "TMP_B64=%TEMP%\_placeholder_%RANDOM%.b64"
>"%TMP_B64%" echo /9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a
>>"%TMP_B64%" echo HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIy
>>"%TMP_B64%" echo MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIAAhEB
>>"%TMP_B64%" echo AxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9
>>"%TMP_B64%" echo AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6
>>"%TMP_B64%" echo Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ip
>>"%TMP_B64%" echo qrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/
>>"%TMP_B64%" echo APwA/9k=
certutil -decode "%TMP_B64%" "%OUT%" >nul 2>&1
if errorlevel 1 (
    echo   [WARN] 写入失败: %OUT%
) else (
    echo   [ OK ] %OUT%
)
del "%TMP_B64%" >nul 2>&1
goto :eof
