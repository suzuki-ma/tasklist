@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "GOOGLE_SYNC_ENABLED=0"
if not defined TASKLIST_DATA_DIR set "TASKLIST_DATA_DIR=H:\マイドライブ\tasklist\shared-data"
if not defined TASKLIST_DEVICE_ID set "TASKLIST_DEVICE_ID=windows-%COMPUTERNAME%"

if not exist "%TASKLIST_DATA_DIR%\.tasklist-shared.json" (
    echo Google Drive shared data is not ready:
    echo   %TASKLIST_DATA_DIR%
    echo Wait for Google Drive to finish syncing, then try again.
    pause
    exit /b 1
)
if not exist "%TASKLIST_DATA_DIR%\tasks.csv" (
    echo tasks.csv is missing. Startup was cancelled to protect your data.
    pause
    exit /b 1
)
if not exist "%TASKLIST_DATA_DIR%\tags.csv" (
    echo tags.csv is missing. Startup was cancelled to protect your data.
    pause
    exit /b 1
)

if defined TASKLIST_PYTHON_EXE (
    set "PYTHON_EXE=%TASKLIST_PYTHON_EXE%"
) else if exist "%USERPROFILE%\anaconda3\envs\taskenv\python.exe" (
    set "PYTHON_EXE=%USERPROFILE%\anaconda3\envs\taskenv\python.exe"
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
    py -m venv .venv
    if errorlevel 1 exit /b 1
)

"%PYTHON_EXE%" -c "import flask, matplotlib, japanize_matplotlib" >nul 2>&1
if errorlevel 1 (
    "%PYTHON_EXE%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Dependency installation failed.
        pause
        exit /b 1
    )
)

if /I "%~1"=="--check" (
    "%PYTHON_EXE%" -c "import app; app.ensure_files(); assert app.SHARED_DATA_MODE; print(app.DATA_DIR)"
    exit /b %errorlevel%
)

echo Tasklist is using Google Drive shared data.
echo Do not start Tasklist on another PC at the same time.
echo To switch PCs, press Ctrl+C here and wait for Google Drive sync to finish.
"%PYTHON_EXE%" app.py

pause
