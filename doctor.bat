@echo off
rem Check the environment without launching the UI.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run setup.ps1 first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" run.py --doctor
pause
endlocal
