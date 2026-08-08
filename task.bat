@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM 通常起動はGoogle Drive共有版に固定する。
REM Driveが未接続なら共有版の起動スクリプトが安全に中止する。
call "%~dp0start_with_google_drive.bat" %*
exit /b %errorlevel%
