@echo off
rem YouTube changes its delivery regularly; yt-dlp keeps up. Run this when a
rem URL that used to work stops loading.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run setup.ps1 first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade yt-dlp
pause
endlocal
