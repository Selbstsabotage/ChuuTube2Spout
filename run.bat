@echo off
rem Launch ChuuTubePoi using the virtual environment created by setup.ps1.
rem
rem Settings live in settings.txt next to this file - not here. That way
rem doctor.bat and check.bat see them too, and updates cannot overwrite them.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo No virtual environment found.
    echo Run this first:  powershell -ExecutionPolicy Bypass -File setup.ps1
    pause
    exit /b 1
)

".venv\Scripts\python.exe" run.py %*
if errorlevel 1 pause
endlocal
