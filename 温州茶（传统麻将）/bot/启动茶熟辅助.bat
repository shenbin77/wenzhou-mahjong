@echo off
cd /d "%~dp0"
python "assist_window.py"
if errorlevel 1 pause
