#!/usr/bin/env bash
# Start the Hunyuan3D API server on port 8082.
# Run this in a separate terminal BEFORE running run_batch.py.
#
# Usage:
#   bash scripts/start_hunyuan_api.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BLOCKY_DIR="$(dirname "${SCRIPT_DIR}")"

if [[ -z "${HUNYUAN_DIR:-}" ]]; then
    if [[ -d "${BLOCKY_DIR}/../hunyuan3d-2.1" ]]; then
        HUNYUAN_DIR="${BLOCKY_DIR}/../hunyuan3d-2.1"
    else
        HUNYUAN_DIR="/home/dana/workspace/hunyuan3d-2.1"
    fi
fi
PORT="${HUNYUAN_PORT:-${PORT:-8082}}"

if [[ ! -x "${HUNYUAN_DIR}/.venv/bin/python" ]]; then
    echo "ERROR: Hunyuan3D venv not found at ${HUNYUAN_DIR}/.venv/bin/python" >&2
    echo "Set HUNYUAN_DIR to the Hunyuan3D install directory and try again." >&2
    exit 1
fi

if [[ ! -f "${HUNYUAN_DIR}/hunyuan3d-2.1-src/api_server.py" ]]; then
    echo "ERROR: Hunyuan3D API server not found at ${HUNYUAN_DIR}/hunyuan3d-2.1-src/api_server.py" >&2
    echo "Install or restore the Hunyuan3D source tree, or set HUNYUAN_DIR to its location." >&2
    exit 1
fi

# Refuse conflicts without terminating the existing service.
"${HUNYUAN_DIR}/.venv/bin/python" - "$PORT" <<'PYPORT'
import socket, sys
with socket.socket() as sock:
    if sock.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0:
        sys.exit(f"ERROR: Port {sys.argv[1]} is occupied; leaving its service running.")
PYPORT

source "${HUNYUAN_DIR}/.venv/bin/activate"
export CUDA_HOME=/usr/local/cuda-12.8
export PATH="${CUDA_HOME}/bin:${PATH}"
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export CUBLAS_WORKSPACE_CONFIG=:4096:8

cd "${HUNYUAN_DIR}/hunyuan3d-2.1-src"
mkdir -p gradio_cache
echo "Starting Hunyuan3D API server on port ${PORT}..."
exec python api_server.py \
    --model_path tencent/Hunyuan3D-2.1 \
    --subfolder hunyuan3d-dit-v2-1 \
    --low_vram_mode \
    --port ${PORT}
