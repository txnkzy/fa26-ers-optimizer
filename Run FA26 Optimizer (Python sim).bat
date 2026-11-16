@echo off
rem The same program on the original Python lap integration, for comparing
rem against the compiled one. See core/sim26_native.py.
cd /d "%~dp0"
set FA26_SIM=python
where pythonw.exe >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw.exe "%~dp0fa26_app.py"
) else (
    python "%~dp0fa26_app.py"
    if errorlevel 1 pause
)
