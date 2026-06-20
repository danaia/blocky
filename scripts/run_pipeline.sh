#!/usr/bin/env bash
# run_pipeline.sh — Three-phase pipeline: images, meshes, then Blender decimation.
# ComfyUI is fully stopped and VRAM confirmed free before Hunyuan3D starts.
#
# Usage:
#   bash scripts/run_pipeline.sh [manifest_file]
#
# Defaults to manifest/test_3.json if no argument is given.

set -euo pipefail

BLOCKY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "${COMFYUI_DIR:-}" ]]; then
    if [[ -d "/home/dana/comfy/ComfyUI" ]]; then
        COMFYUI_DIR="/home/dana/comfy/ComfyUI"
    elif [[ -d "${BLOCKY_DIR}/../ComfyUI" ]]; then
        COMFYUI_DIR="${BLOCKY_DIR}/../ComfyUI"
    else
        COMFYUI_DIR="/home/dana/workspace/ComfyUI"
    fi
fi
if [[ -z "${HUNYUAN_DIR:-}" ]]; then
    if [[ -d "${BLOCKY_DIR}/../hunyuan3d-2.1" ]]; then
        HUNYUAN_DIR="${BLOCKY_DIR}/../hunyuan3d-2.1"
    else
        HUNYUAN_DIR="/home/dana/workspace/hunyuan3d-2.1"
    fi
fi
MANIFEST="${1:-manifest/test_3.json}"
BLENDER_BIN="${BLENDER_BIN:-/usr/local/bin/blender}"
DECIMATE_PRESET="${DECIMATE_PRESET:-game_ready}"
DECIMATE_OUTPUT_ROOT="${DECIMATE_OUTPUT_ROOT:-in_game_assets}"

COMFYUI_PORT=8188
HUNYUAN_PORT=8081
READY_TIMEOUT=180   # seconds to wait for each service

COMFYUI_PID=""
HUNYUAN_PID=""

# Full python paths — avoids sourcing two venvs in the same shell
HUNYUAN_PYTHON="${HUNYUAN_DIR}/.venv/bin/python"
COMFYUI_PYTHON="${COMFYUI_DIR}/.venv/bin/python"
BLOCKY_PYTHON="${BLOCKY_DIR}/.venv/bin/python"

# VRAM threshold: don't start Hunyuan3D until this many MiB are free (RTX 5090 = 32768 MiB)
VRAM_FREE_THRESHOLD=20000

require_file() {
    local path="$1" description="$2"
    if [[ ! -e "$path" ]]; then
        echo "ERROR: Missing ${description}: ${path}" >&2
        exit 1
    fi
}

require_executable() {
    local path="$1" description="$2"
    if [[ ! -x "$path" ]]; then
        echo "ERROR: Missing executable ${description}: ${path}" >&2
        exit 1
    fi
}

preflight_paths() {
    require_executable "$BLOCKY_PYTHON" "Blocky Python"
    require_executable "$COMFYUI_PYTHON" "ComfyUI Python"
    require_file "${COMFYUI_DIR}/main.py" "ComfyUI main.py"
    require_executable "$HUNYUAN_PYTHON" "Hunyuan3D Python"
    require_file "${HUNYUAN_DIR}/hunyuan3d-2.1-src/api_server.py" "Hunyuan3D API server"
    require_executable "$BLENDER_BIN" "Blender"
}

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
# Kill ALL lingering pipeline processes from previous/crashed runs
# ---------------------------------------------------------------------------
kill_stale_processes() {
    echo "Clearing stale processes and ports..."
    pkill -f "api_server.py"       2>/dev/null && echo "  Killed stale Hunyuan3D" || true
    pkill -f "ComfyUI/main.py"     2>/dev/null && echo "  Killed stale ComfyUI"   || true
    fuser -k "${HUNYUAN_PORT}/tcp" 2>/dev/null || true
    fuser -k "${COMFYUI_PORT}/tcp" 2>/dev/null || true
    sleep 3
}

# ---------------------------------------------------------------------------
# Wait until a port accepts connections (with PID liveness check)
# ---------------------------------------------------------------------------
wait_for_port() {
    local name="$1" port="$2" pid="${3:-}"
    echo -n "  Waiting for $name on port $port"
    local elapsed=0
    while ! (echo > /dev/tcp/localhost/"$port") 2>/dev/null; do
        if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
            echo " DIED"
            echo "ERROR: $name (PID $pid) exited before becoming ready." >&2
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
# Stop a service and wait for its port to close
# ---------------------------------------------------------------------------
stop_service() {
    local name="$1" pid="$2" port="$3"
    if kill -0 "$pid" 2>/dev/null; then
        echo "  Stopping $name (PID $pid)..."
        kill "$pid" 2>/dev/null
        local elapsed=0
        while (echo > /dev/tcp/localhost/"$port") 2>/dev/null; do
            sleep 1
            elapsed=$((elapsed + 1))
            [[ $elapsed -ge 30 ]] && break
        done
        echo "  $name stopped."
    fi
}

# ---------------------------------------------------------------------------
# Poll nvidia-smi until free VRAM exceeds threshold
# ---------------------------------------------------------------------------
wait_for_vram() {
    local threshold="$1"
    echo -n "  Waiting for VRAM >${threshold} MiB free"
    local elapsed=0
    while true; do
        local free_mib
        free_mib=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null \
                   | head -1 | tr -d ' ')
        if [[ -n "$free_mib" ]] && [[ "$free_mib" -ge "$threshold" ]]; then
            echo " — ${free_mib} MiB free"
            return 0
        fi
        echo -n " (${free_mib:-?} MiB)"
        sleep 3
        elapsed=$((elapsed + 3))
        if [[ $elapsed -ge 90 ]]; then
            echo " — timeout, proceeding anyway"
            return 0
        fi
    done
}

run_blender_decimation() {
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "PHASE 3: Blender decimation"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    if [[ ! -x "$BLENDER_BIN" ]]; then
        echo "ERROR: Blender not found at $BLENDER_BIN" >&2
        exit 1
    fi

    local mesh_dir
    local ran_any=0
    shopt -s nullglob
    for mesh_dir in "$BLOCKY_DIR"/outputs/meshes/*; do
        [[ -d "$mesh_dir" ]] || continue
        local category
        category="$(basename "$mesh_dir")"
        local glbs=("$mesh_dir"/*.glb)
        [[ ${#glbs[@]} -gt 0 ]] || continue

        ran_any=1
        echo "Decimating category: $category"
        "$BLENDER_BIN" --background --python "$BLOCKY_DIR/scripts/decimate_glbs.py" -- \
            --manifest "$MANIFEST" \
            --preset "$DECIMATE_PRESET" \
            --output-dir "$BLOCKY_DIR/$DECIMATE_OUTPUT_ROOT/$category" \
            "${glbs[@]}"
    done
    shopt -u nullglob

    if [[ "$ran_any" -eq 0 ]]; then
        echo "No mesh GLBs found to decimate."
    fi
}

# ===========================================================================
# PRE-FLIGHT: kill stale processes, show GPU state
# ===========================================================================
cd "${BLOCKY_DIR}"
mkdir -p outputs/logs

preflight_paths
kill_stale_processes

echo ""
nvidia-smi --query-gpu=name,memory.total,memory.free,memory.used \
           --format=csv,noheader 2>/dev/null \
  | awk -F',' '{printf "  GPU: %s | total=%s | free=%s | used=%s\n", $1,$2,$3,$4}' \
  || true

echo ""
echo "Running batch: $MANIFEST"

# ===========================================================================
# PHASE 1 — Image generation (ComfyUI only; Hunyuan3D NOT running)
# ===========================================================================
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "PHASE 1: Image generation (ComfyUI)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

echo "Starting ComfyUI..."
nohup "$COMFYUI_PYTHON" "${COMFYUI_DIR}/main.py" \
    --port "$COMFYUI_PORT" \
    > "${BLOCKY_DIR}/outputs/logs/comfyui.log" 2>&1 &
COMFYUI_PID=$!
echo "  PID $COMFYUI_PID (log: outputs/logs/comfyui.log)"

wait_for_port "ComfyUI" "$COMFYUI_PORT" "$COMFYUI_PID"

"$BLOCKY_PYTHON" scripts/run_batch.py "$MANIFEST" --phase images

# Fully stop ComfyUI and confirm VRAM is free before Phase 2
stop_service "ComfyUI" "$COMFYUI_PID" "$COMFYUI_PORT"
COMFYUI_PID=""

wait_for_vram "$VRAM_FREE_THRESHOLD"

# ===========================================================================
# PHASE 2 — Mesh generation (Hunyuan3D only; ComfyUI NOT running)
#   Retries up to MAX_MESH_RETRIES times, restarting Hunyuan3D between each
#   attempt, so a single CUDA failure doesn't block the remaining assets.
# ===========================================================================
MAX_MESH_RETRIES=10
MESH_ATTEMPT=0

count_remaining() {
    "$BLOCKY_PYTHON" - <<'PYEOF'
import json, sys
from pathlib import Path
entries = [json.loads(l) for l in Path("outputs/index.jsonl").read_text().splitlines() if l.strip()]
pending = sum(1 for e in entries if e["status"] in ("image_complete", "failed"))
print(pending)
PYEOF
}

start_hunyuan() {
    echo "Starting Hunyuan3D API server..."
    nohup bash -c "
        cd '${HUNYUAN_DIR}/hunyuan3d-2.1-src'
        mkdir -p gradio_cache
        export CUDA_HOME=/usr/local/cuda-12.8
        export PATH=\"\${CUDA_HOME}/bin:\${PATH}\"
        export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
        export CUBLAS_WORKSPACE_CONFIG=:4096:8
        exec '${HUNYUAN_PYTHON}' api_server.py \
            --model_path tencent/Hunyuan3D-2.1 \
            --subfolder hunyuan3d-dit-v2-1 \
            --low_vram_mode \
            --port '${HUNYUAN_PORT}'
    " >> "${BLOCKY_DIR}/outputs/logs/hunyuan_api.log" 2>&1 &
    HUNYUAN_PID=$!
    echo "  PID $HUNYUAN_PID (log: outputs/logs/hunyuan_api.log)"
    wait_for_port "Hunyuan3D" "$HUNYUAN_PORT" "$HUNYUAN_PID"
}

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "PHASE 2: Mesh generation (Hunyuan3D)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

start_hunyuan

while true; do
    MESH_ATTEMPT=$((MESH_ATTEMPT + 1))
    echo ""
    echo "  Mesh attempt ${MESH_ATTEMPT}/${MAX_MESH_RETRIES}..."

    set +e
    "$BLOCKY_PYTHON" scripts/run_batch.py "$MANIFEST" --phase meshes \
        --hunyuan-url "http://localhost:${HUNYUAN_PORT}"
    MESH_EXIT=$?
    set -e

    REMAINING=$(count_remaining)
    echo "  Remaining after attempt ${MESH_ATTEMPT}: ${REMAINING} assets"

    if [[ "$REMAINING" -eq 0 ]]; then
        echo "  All assets complete."
        break
    fi

    if [[ "$MESH_ATTEMPT" -ge "$MAX_MESH_RETRIES" ]]; then
        echo "  Max retries (${MAX_MESH_RETRIES}) reached. ${REMAINING} assets unfinished."
        break
    fi

    echo "  CUDA failure detected — restarting Hunyuan3D and retrying ${REMAINING} assets..."
    stop_service "Hunyuan3D" "$HUNYUAN_PID" "$HUNYUAN_PORT"
    HUNYUAN_PID=""
    wait_for_vram "$VRAM_FREE_THRESHOLD"
    start_hunyuan
done

echo ""
echo "Pipeline complete."

run_blender_decimation
