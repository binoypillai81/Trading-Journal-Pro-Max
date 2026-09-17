#!/usr/bin/env bash
# Start Trade Time Machine at http://127.0.0.1:8765
# Journal database: data/journal.sqlite3 (override with TTM_DB_PATH=/path/to/file.sqlite3)
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Creating virtual environment…"
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8765}"
