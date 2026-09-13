@echo off
cd /d "%~dp0"
where pythonw.exe >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw.exe "%~dp0fa26_app.py"
) else (
    python "%~dp0fa26_app.py"
    if errorlevel 1 pause
)
