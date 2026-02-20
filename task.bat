@echo off

REM ====== 設定 ======
set CONDA_ENV=taskenv
set SCRIPT_PATH=C:\Users\canmi\Documents\GitHub\tasklist\tasklist\app.py
REM ===================

REM conda 初期化
call "%USERPROFILE%\anaconda3\Scripts\activate.bat"

REM 環境を有効化
call conda activate %CONDA_ENV%

REM スクリプト実行
python "%SCRIPT_PATH%"

pause