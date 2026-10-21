@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Create environment: py -m venv .venv
    echo Then install: .venv\Scripts\python.exe -m pip install -e .
    pause
    exit /b 1
)
.venv\Scripts\python.exe -m airframe
