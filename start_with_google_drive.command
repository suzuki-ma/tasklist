#!/bin/zsh
set -eu

SCRIPT_DIR="${0:A:h}"

if [[ -z "${TASKLIST_DATA_DIR:-}" ]]; then
  candidates=(
    "$HOME"/Library/CloudStorage/GoogleDrive-*/My\ Drive/tasklist/shared-data(N)
    "$HOME"/Library/CloudStorage/GoogleDrive-*/マイドライブ/tasklist/shared-data(N)
  )
  if (( ${#candidates[@]} != 1 )); then
    echo "Google Drive shared-data folder could not be selected automatically."
    echo "Drag the folder from Finder into Terminal and set TASKLIST_DATA_DIR to that path."
    exit 1
  fi
  export TASKLIST_DATA_DIR="${candidates[1]}"
fi

export GOOGLE_SYNC_ENABLED=0
export TASKLIST_DEVICE_ID="mac-${HOST%%.*}"

if [[ ! -f "$TASKLIST_DATA_DIR/.tasklist-shared.json" \
   || ! -f "$TASKLIST_DATA_DIR/tasks.csv" \
   || ! -f "$TASKLIST_DATA_DIR/tags.csv" ]]; then
  echo "Google Drive is not fully synced: $TASKLIST_DATA_DIR"
  exit 1
fi

cd "$SCRIPT_DIR"
PYTHON_EXE="${TASKLIST_PYTHON_EXE:-$SCRIPT_DIR/.venv/bin/python3}"
if [[ ! -x "$PYTHON_EXE" ]]; then
  python3 -m venv "$SCRIPT_DIR/.venv"
fi

if ! "$PYTHON_EXE" -c 'import flask, matplotlib, japanize_matplotlib' >/dev/null 2>&1; then
  "$PYTHON_EXE" -m pip install -r requirements.txt
fi

echo "Tasklist is using Google Drive shared data."
echo "Do not start it on another computer at the same time."
echo "Press Control+C before switching computers, then wait for Drive sync."
exec "$PYTHON_EXE" app.py
