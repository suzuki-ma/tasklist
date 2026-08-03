@echo off
setlocal
cd /d "%~dp0"

set "GOOGLE_SYNC_ENABLED=0"
if defined TASKLIST_PYTHON_EXE (
    set "PYTHON_EXE=%TASKLIST_PYTHON_EXE%"
) else (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
)

if not exist "%PYTHON_EXE%" (
    where py >nul 2>&1
    if errorlevel 1 (
        echo Python was not found. Install Python 3 and enable the py launcher.
        pause
        exit /b 1
    )

    echo Creating a local Python environment...
    py -m venv .venv
    if errorlevel 1 (
        echo Failed to create the Python environment.
        pause
        exit /b 1
    )
)

"%PYTHON_EXE%" -c "import flask, matplotlib, japanize_matplotlib" >nul 2>&1
if errorlevel 1 (
    echo Installing the required packages...
    "%PYTHON_EXE%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Failed to install the required packages.
        pause
        exit /b 1
    )
)

if /I "%~1"=="--check" (
    "%PYTHON_EXE%" -c "import app; assert not app.GOOGLE_SYNC_ENABLED; print('Google-free startup check passed.')"
    exit /b %errorlevel%
)

echo Starting Tasklist without Google sync...
"%PYTHON_EXE%" app.py

pause
