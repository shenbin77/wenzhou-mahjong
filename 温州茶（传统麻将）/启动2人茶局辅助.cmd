@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if errorlevel 1 (
  echo 未找到 pythonw，请先确认本机 Python 安装。
  pause
  exit /b 1
)
start "" pythonw "%~dp0bot\launcher_window.py"
