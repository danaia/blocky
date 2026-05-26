#!/usr/bin/env bash
# run_pipeline.sh — Start ComfyUI + Hunyuan3D, run the batch, then shut down.
#
# Usage:
#   bash scripts/run_pipeline.sh [manifest_file]
#
# Defaults to manifest/test_3.json if no argument is given.

set -euo pipefail

BLOCKY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMFYUI_DIR="/home/dana/workspace/ComfyUI"
HUNYUAN_DIR="/home/dana/workspace/hunyuan3d-2.1"
MANIFEST="${1:-manifest/test_3.json}"

COMFYUI_PORT=8188
HUNYUAN_PORT=8081
READY_TIMEOUT=180   # seconds to wait for each service

COMFYUI_PID=""
HUNYUAN_PID=""

# Full python paths — avoids sourcing two venvs in the same shell
HUNYUAN_PYTHON="${HUNYUAN_DIR}/.venv/bin/python"
COMFYUI_PYTHON="${COMFYUI_DIR}/.venv/bin/python"
BLOCKY_PYTHON="${BLOCKY_DIR}/.venv/bin/python"

# ---------------------------------------------------------------------------
# Cleanup: kill only services WE started
# ---------------------------------------------------------------------------
cleanup() {
    echo ""
    echo "Shutting down services..."
    if [[ -n "$HUNYUAN_PID" ]] && kill -0 "$HUNYUAN_PID" 2>/dev/null; then
        kill "$HUNYUAN_PID" 2>/dev/null
        echo "  Hunyuan3D stopped (PID $HUNYUAN_PID)"
    fi
    if [[ -n "$COMFYUI_PID" ]] && kill -0 "$COMFYUI_PID" 2>/dev/null; then
        kill "$COMFYUI_PID" 2>/dev/null
        echo "  ComfyUI stopped (PID $COMFYUI_PID)"
    fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Helper: wait until a port accepts connections, with PID liveness check
# ---------------------------------------------------------------------------
wait_for_port() {
    local name="$1" port="$2" pid="${3:-}"
    echo -n "  Waiting for $name on port $port"
    local elapsed=0
    while ! (echo > /dev/tcp/localhost/"$port") 2>/dev/null; do
        # Fail fast if the process we launched has already died
        if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
            echo " DIED"
            echo ""
            echo "ERROR: $name (PID $pid) exited before becoming ready." >&2
            echo "       Check the log for details." >&2
            exit 1
        fi
        sleep 2
        elapsed=$((elapsed + 2))
        echo -n "."
        if [[ $elapsed -ge $READY_TIMEOUT ]]; then
            echo " TIMEOUT"
            echo "ERROR: $name did not become ready within ${READY_TIMEOUT}s." >&2
            exit 1
        fi
    done
    echo " ready"
}

# ---------------------------------------------------------------------------
# 1. Start Hunyuan3D API (if not already running)
# ---------------------------------------------------------------------------
if (echo > /dev/tcp/localhost/$HUNYUAN_PORT) 2>/dev/null; then
    echo "Hunyuan3D already running on port $HUNYUAN_PORT, skipping start."
else
    echo "Starting Hunyuan3D API server..."
    fuser -k "${HUNYUAN_PORT}/tcp" 2>/dev/null || true

    # Must cd into the src dir so local hy3dshape package takes precedence
    nohup bash -c "
        cd '${HUNYUAN_DIR}/hunyuan3d-2.1-src'
        export CUDA_HOME=/usr/local/cuda-12.8
        export PATH=\"\${CUDA_HOME}/bin:\${PATH}\"
        export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
        export CUBLAS_WORKSPACE_CONFIG=:4096:8
        exec '${HUNYUAN_PYTHON}' api_server.py \
            --model_path tencent/Hunyuan3D-2.1 \
            --subfolder hunyuan3d-dit-v2-1 \
            --low_vram_mode \
            --port '${HUNYUAN_PORT}'
    " > "${BLOCKY_DIR}/outputs/logs/hunyuan_api.log" 2>&1 &
    HUNYUAN_PID=$!
    echo "  PID $HUNYUAN_PID (log: outputs/logs/hunyuan_api.log)"
fi

# ---------------------------------------------------------------------------
# 2. Start ComfyUI (if not already running)
# ---------------------------------------------------------------------------
if (echo > /dev/tcp/localhost/$COMFYUI_PORT) 2>/dev/null; then
    echo "ComfyUI already running on port $COMFYUI_PORT, skipping start."
else
    echo "Starting ComfyUI..."
    fuser -k "${COMFYUI_PORT}/tcp" 2>/dev/null || true

    nohup "$COMFYUI_PYTHON" "${COMFYUI_DIR}/main.py" \
        --port "$COMFYUI_PORT" \
        > "${BLOCKY_DIR}/outputs/logs/comfyui.log" 2>&1 &
    COMFYUI_PID=$!
    echo "  PID $COMFYUI_PID (log: outputs/logs/comfyui.log)"
fi

# ---------------------------------------------------------------------------
# 3. Wait for both services to be ready
# ---------------------------------------------------------------------------
wait_for_port "Hunyuan3D" "$HUNYUAN_PORT" "$HUNYUAN_PID"
wait_for_port "ComfyUI"   "$COMFYUI_PORT" "$COMFYUI_PID"

# ---------------------------------------------------------------------------
# 4. Run the batch pipeline
# ---------------------------------------------------------------------------
echo ""
echo "Running batch: $MANIFEST"
cd "${BLOCKY_DIR}"
"$BLOCKY_PYTHON" scripts/run_batch.py "$MANIFEST"

echo ""
echo "Pipeline complete."
