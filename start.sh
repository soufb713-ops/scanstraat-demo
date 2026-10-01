#!/usr/bin/env bash
# Start the app.  bash start.sh       -> only this computer (http://localhost:5000)
#                 bash start.sh lan   -> also phones/pc's on the office network
cd "$(dirname "$0")"
[ -d .venv ] || { echo "Eerst: bash setup.sh"; exit 1; }
host=127.0.0.1; [ "${1:-}" = "lan" ] && host=0.0.0.0
echo "Scanstraat (privacy-versie) draait op http://localhost:5000  (stoppen: Ctrl+C)"
exec .venv/bin/gunicorn -w 1 --threads 8 -b "$host:5000" --timeout 600 app:app
