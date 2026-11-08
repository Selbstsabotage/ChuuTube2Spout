@echo off
rem Delete downloaded clips and the record of what was fetched.
rem Safe at any time - anything you load again is simply fetched again.
rem
rem Deliberately goes through run.py rather than "rmdir cache": the cache
rem location is configurable (CHUUTUBE2SPOUT_CACHE / settings.txt), and a script that
rem hardcodes "cache" would say "Done." while leaving a relocated cache - and
rem the viewing history in it - exactly where it was.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run setup.ps1 first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" run.py --clear-cache
pause
endlocal
