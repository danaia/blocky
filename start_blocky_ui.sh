#!/usr/bin/env bash
# Start the local Blocky browser control panel.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${BLOCKY_UI_PORT:-8765}"
HOST="${BLOCKY_UI_HOST:-127.0.0.1}"
URL="http://${HOST}:${PORT}/index.html"

cd "$ROOT_DIR"
mkdir -p outputs/logs

if [[ ! -x ".venv/bin/python" ]]; then
    echo "ERROR: .venv/bin/python not found. Create the Blocky venv first." >&2
    exit 1
fi

if (echo > "/dev/tcp/${HOST}/${PORT}") >/dev/null 2>&1; then
    echo "Blocky UI already appears to be running at ${URL}"
else
    echo "Starting Blocky UI at ${URL}"
    if command -v setsid >/dev/null 2>&1; then
        setsid -f .venv/bin/python scripts/blocky_ui_server.py --host "$HOST" --port "$PORT" \
            > outputs/logs/blocky_ui.log 2>&1
        sleep 0.25
        pgrep -f "scripts/blocky_ui_server.py --host ${HOST} --port ${PORT}" | tail -1 > outputs/logs/blocky_ui.pid || true
    else
        nohup .venv/bin/python scripts/blocky_ui_server.py --host "$HOST" --port "$PORT" \
            > outputs/logs/blocky_ui.log 2>&1 &
        echo "$!" > outputs/logs/blocky_ui.pid
    fi

    for _ in {1..40}; do
        if (echo > "/dev/tcp/${HOST}/${PORT}") >/dev/null 2>&1; then
            break
        fi
        sleep 0.25
    done
fi

if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 || true
elif command -v open >/dev/null 2>&1; then
    open "$URL" >/dev/null 2>&1 || true
fi

echo "Open ${URL}"
echo "Server log: outputs/logs/blocky_ui.log"
