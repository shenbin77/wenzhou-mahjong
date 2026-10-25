@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if %ERRORLEVEL% EQU 0 (
  py -3 launch_multi.py --all
) else (
  python launch_multi.py --all
)
echo.
echo 此程序只启动每个已在线雷电设备的辅助器窗口；不会启动游戏或自动点击。
pause
