# Tasklist

## Google DriveでWindowsとMacのデータを共有する

共有するのは `data` だけです。コードは各PCにGitHubから置き、`.venv` と `unupload` はGoogle Driveへ入れません。

### Windowsの初回移行

1. タスクアプリを停止します。
2. Google Drive for desktopが `H:\マイドライブ` として利用可能か確認します。
3. `powershell -ExecutionPolicy Bypass -File scripts\setup_google_drive_data.ps1` を実行します。
4. `start_with_google_drive.bat` を起動します。

共有先は `H:\マイドライブ\tasklist\shared-data` です。既存の `H:\マイドライブ\tasklist\data` は古いデータとして残し、上書きしません。従来の `task.bat` も共有データを検出するとGoogle Drive版を起動します。

### Macの初回設定

1. Google Drive for desktopで `tasklist/shared-data` を「オフラインで使用可能」にします。
2. このGitHubリポジトリをMacのローカルディスクへクローンします。
3. 初回のみ `chmod +x start_with_google_drive.command` を実行します。
4. `./start_with_google_drive.command` で起動します。

Driveの場所を自動検出できない場合は、Finderから `shared-data` をTerminalへドラッグして実パスを確認し、`TASKLIST_DATA_DIR` に設定します。

### PCを切り替えるルール

1. 使用中PCのサーバーを `Ctrl+C` で終了します。ブラウザを閉じるだけでは不十分です。
2. Google Driveが「同期完了」になるまで待ちます。
3. 次のPCでも同期完了を確認してから起動します。

アプリは他PCの新しいリースを検出すると保存を拒否します。ただしDrive同期は即時ではないため、同時起動しない運用が必須です。保存前のデータは `shared-data/backups/YYYY-MM-DD` へ自動保存されます。

## Googleを使わないPCで起動する

1. このリポジトリをZIPでダウンロードして展開します。
2. Python 3をインストールします。その際、Python Launcher（`py`）を有効にします。
3. `start_without_google.bat` をダブルクリックします。
4. EdgeやFirefoxで `http://127.0.0.1:5000/` を開きます。

初回だけ、専用の `.venv` 環境と必要な基本パッケージを自動作成します。Google関連パッケージやGoogleアカウントは不要です。

Google同期は初期状態では無効です。利用するPCだけ、Google関連パッケージと認証ファイルを準備したうえで、起動前に環境変数 `GOOGLE_SYNC_ENABLED=1` を設定してください。

タスクデータは `data/` にローカル保存され、GitHubには含まれません。別PCへ移す場合は、アプリを停止してから `data` フォルダをUSBメモリなどでコピーしてください。
