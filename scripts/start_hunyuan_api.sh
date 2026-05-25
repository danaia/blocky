#!/usr/bin/env bash
# Start the Hunyuan3D API server on port 8081.
# Run this in a separate terminal BEFORE running run_batch.py.
#
# Usage:
#   bash scripts/start_hunyuan_api.sh
set -e

HUNYUAN_DIR="/home/dana/workspace/hunyuan3d-2.1"
PORT=8081

# Kill any existing process on the port
fuser -k ${PORT}/tcp 2>/dev/null || true
sleep 1

source "${HUNYUAN_DIR}/.venv/bin/activate"
export CUDA_HOME=/usr/local/cuda-12.8
export PATH="${CUDA_HOME}/bin:${PATH}"
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export CUBLAS_WORKSPACE_CONFIG=:4096:8

cd "${HUNYUAN_DIR}/hunyuan3d-2.1-src"
echo "Starting Hunyuan3D API server on port ${PORT}..."
python api_server.py \
    --model_path tencent/Hunyuan3D-2.1 \
    --subfolder hunyuan3d-dit-v2-1 \
    --low_vram_mode \
    --port ${PORT}
