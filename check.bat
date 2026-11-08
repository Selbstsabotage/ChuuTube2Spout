@echo off
rem Work out why a URL will not play.
rem   check.bat "https://www.youtube.com/watch?v=..."
rem Quote the URL: cmd treats = and & as argument separators.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run setup.ps1 first.
    pause
    exit /b 1
)
rem Assign first, then test with "defined". `if "%~1"==""` unbalances the parse
rem if the argument itself contains a quote, and a troubleshooting command is
rem exactly the kind of thing people paste from a forum thread.
set "ARG=%~1"
if not defined ARG (
    echo usage: check.bat "https://www.youtube.com/watch?v=..."
    pause
    exit /b 1
)
".venv\Scripts\python.exe" run.py --check %*
pause
endlocal
