@echo off
cd /d "%~dp0"
if "%~1"=="" (
    start "" ".venv\Scripts\pythonw.exe" main.py
) else (
    ".venv\Scripts\python.exe" main.py %*
    if errorlevel 1 pause
)
