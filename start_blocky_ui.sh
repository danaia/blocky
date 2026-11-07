#!/usr/bin/env bash
# Start the local Blocky browser control panel.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${BLOCKY_UI_PORT:-8765}"
HOST="${BLOCKY_UI_HOST:-127.0.0.1}"
URL="http://${HOST}:${PORT}/index.html"
COMFYUI_HOST="${COMFYUI_HOST:-127.0.0.1}"
COMFYUI_PORT="${COMFYUI_PORT:-8188}"
BLOCKY_OPEN_BROWSER="${BLOCKY_OPEN_BROWSER:-1}"
HUNYUAN_PORT="${HUNYUAN_PORT:-8082}"
export COMFYUI_HOST COMFYUI_PORT HUNYUAN_PORT

if [[ -z "${COMFYUI_DIR:-}" ]]; then
    if [[ -d "/home/dana/comfy/ComfyUI" ]]; then
        COMFYUI_DIR="/home/dana/comfy/ComfyUI"
    elif [[ -d "${ROOT_DIR}/../ComfyUI" ]]; then
        COMFYUI_DIR="${ROOT_DIR}/../ComfyUI"
    else
        COMFYUI_DIR="/home/dana/workspace/ComfyUI"
    fi
fi

cd "$ROOT_DIR"
mkdir -p outputs/logs

if [[ ! -x ".venv/bin/python" ]]; then
    echo "ERROR: .venv/bin/python not found. Create the Blocky venv first." >&2
    exit 1
fi

require_free_port() {
    .venv/bin/python - "$1" "$2" <<'PYPORT'
import socket, sys
with socket.socket() as sock:
    if sock.connect_ex((sys.argv[1], int(sys.argv[2]))) == 0:
        sys.exit(f"ERROR: Port {sys.argv[2]} is occupied by another service; leaving it running.")
PYPORT
}

start_comfyui() {
    local comfy_url="http://${COMFYUI_HOST}:${COMFYUI_PORT}"
    if check_comfyui; then
        echo "ComfyUI already appears to be running at ${comfy_url}"
        return 0
    fi

    if [[ ! -f "${COMFYUI_DIR}/main.py" ]]; then
        echo "WARNING: ComfyUI not found at ${COMFYUI_DIR}; image generation will stay unavailable." >&2
        echo "         Set COMFYUI_DIR=/path/to/ComfyUI before running this script." >&2
        return 0
    fi
    if [[ ! -x "${COMFYUI_DIR}/.venv/bin/python" ]]; then
        echo "WARNING: ComfyUI Python not found at ${COMFYUI_DIR}/.venv/bin/python" >&2
        return 0
    fi

    require_free_port "$COMFYUI_HOST" "$COMFYUI_PORT"
    echo "Starting ComfyUI at ${comfy_url}"
    if command -v setsid >/dev/null 2>&1; then
        setsid -f "${COMFYUI_DIR}/.venv/bin/python" "${COMFYUI_DIR}/main.py" \
            --listen "$COMFYUI_HOST" --port "${COMFYUI_PORT}" \
            > "${ROOT_DIR}/outputs/logs/comfyui.log" 2>&1
        sleep 0.25
        pgrep -f "${COMFYUI_DIR}/main.py.*--port ${COMFYUI_PORT}" | tail -1 > "${ROOT_DIR}/outputs/logs/comfyui.pid" || true
    else
        nohup "${COMFYUI_DIR}/.venv/bin/python" "${COMFYUI_DIR}/main.py" \
            --listen "$COMFYUI_HOST" --port "${COMFYUI_PORT}" \
            > "${ROOT_DIR}/outputs/logs/comfyui.log" 2>&1 &
        echo "$!" > "${ROOT_DIR}/outputs/logs/comfyui.pid"
    fi

    for _ in {1..120}; do
        if check_comfyui; then
            echo "ComfyUI ready at ${comfy_url}"
            return 0
        fi
        if ! kill -0 "$(cat "${ROOT_DIR}/outputs/logs/comfyui.pid")" 2>/dev/null; then
            echo "WARNING: ComfyUI exited before becoming ready. See outputs/logs/comfyui.log" >&2
            return 0
        fi
        sleep 1
    done
    echo "WARNING: ComfyUI did not become ready within 120s. See outputs/logs/comfyui.log" >&2
}

check_comfyui() {
    python3 - "$COMFYUI_HOST" "$COMFYUI_PORT" <<'PY' >/dev/null 2>&1
import sys
import urllib.request

host, port = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(f"http://{host}:{port}/system_stats", timeout=2) as response:
    raise SystemExit(0 if 200 <= response.status < 300 else 1)
PY
}

start_comfyui

if .venv/bin/python - "$HOST" "$PORT" <<'PYUI' >/dev/null 2>&1
import json, sys, urllib.request
with urllib.request.urlopen(f"http://{sys.argv[1]}:{sys.argv[2]}/api/state", timeout=5) as r:
    data = json.load(r)
    assert "manifests" in data and "services" in data
PYUI
then
    echo "Blocky UI already appears to be running at ${URL}"
else
    require_free_port "$HOST" "$PORT"
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

if [[ "$BLOCKY_OPEN_BROWSER" != "0" ]]; then
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$URL" >/dev/null 2>&1 || true
    elif command -v open >/dev/null 2>&1; then
        open "$URL" >/dev/null 2>&1 || true
    fi
fi

sleep 1
if ! check_comfyui; then
    echo "WARNING: ComfyUI is not reachable after startup. See outputs/logs/comfyui.log" >&2
fi

echo "Open ${URL}"
echo "Server log: outputs/logs/blocky_ui.log"
echo "ComfyUI log: outputs/logs/comfyui.log"

.venv/bin/python - "$HOST" "$PORT" <<'PYUI'
import json, sys, urllib.request
with urllib.request.urlopen(f"http://{sys.argv[1]}:{sys.argv[2]}/api/state", timeout=10) as r:
    data = json.load(r)
    assert "manifests" in data and "services" in data
print("Blocky UI API ready")
PYUI
