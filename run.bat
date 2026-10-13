@echo off
rem Double-click to open the AION 2 Market Assistant window (no console window stays open).
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo The Python environment is missing: .venv\Scripts\pythonw.exe was not found.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m app.main
