# Tasklist

## Googleを使わないPCで起動する

1. このリポジトリをZIPでダウンロードして展開します。
2. Python 3をインストールします。その際、Python Launcher（`py`）を有効にします。
3. `start_without_google.bat` をダブルクリックします。
4. EdgeやFirefoxで `http://127.0.0.1:5000/` を開きます。

初回だけ、専用の `.venv` 環境と必要な基本パッケージを自動作成します。Google関連パッケージやGoogleアカウントは不要です。

Google同期は初期状態では無効です。利用するPCだけ、Google関連パッケージと認証ファイルを準備したうえで、起動前に環境変数 `GOOGLE_SYNC_ENABLED=1` を設定してください。

タスクデータは `data/` にローカル保存され、GitHubには含まれません。別PCへ移す場合は、アプリを停止してから `data` フォルダをUSBメモリなどでコピーしてください。
