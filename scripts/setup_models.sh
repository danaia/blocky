#!/usr/bin/env bash
# Download all model weights needed for the Blocky pipeline.
# Reads HUGGING FACE token from .env in the project root.
#
# Usage (from blocky/ project root):
#   bash scripts/setup_models.sh              # normal download (full speed)
#   GENTLE=1     bash scripts/setup_models.sh  # idle-only I/O priority — won't kill your network
#   SKIP_FLUX=1  bash scripts/setup_models.sh  # skip the ~34 GB Flux models entirely
#
# What this does:
#   1. Downloads missing Hunyuan3D-2.1 shape model (~8 GB)
#   2. Clones ComfyUI into ../ComfyUI
#   3. Sets up ComfyUI venv + dependencies
#   4. Downloads Flux schnell model files (~34 GB total)
#
# NOTE: Flux schnell requires accepting terms at:
#   https://huggingface.co/black-forest-labs/FLUX.1-schnell
# Your HF account must have accepted those terms before the download will work.
set -e

# Wrapper function that applies ionice (I/O scheduling) for lower priority downloads.
# When GENTLE=1, uses ionice class 3 (idle-only) so downloads only run when
# the system has no other I/O demands — won't starve your network.
hf_download() {
    local model="$1"
    shift
    if [[ "${GENTLE:-0}" == "1" ]]; then
        ionice -c 3 "$HF_BIN" download "$model" "$@" &
        local pid=$!
        wait $pid
    else
        "$HF_BIN" download "$model" "$@"
    fi
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
HUNYUAN_DIR="/home/dana/workspace/hunyuan3d-2.1"
COMFYUI_DIR="${PROJECT_DIR}/../ComfyUI"
HF_BIN="${HUNYUAN_DIR}/.venv/bin/hf"

# ---------------------------------------------------------------------------
# Read HF token from .env  (key has a space: "HUGGING FACE=...")
# ---------------------------------------------------------------------------
ENV_FILE="${PROJECT_DIR}/.env"
if [[ ! -f "${ENV_FILE}" ]]; then
    echo "✗ .env not found at ${ENV_FILE}"
    exit 1
fi

HF_TOKEN=$(python3 -c "
with open('${ENV_FILE}') as f:
    for line in f:
        if '=' in line and 'FACE' in line.upper():
            print(line.split('=', 1)[1].strip())
            break
")

if [[ -z "${HF_TOKEN}" ]]; then
    echo "✗ Could not parse HuggingFace token from .env"
    exit 1
fi

export HUGGING_FACE_HUB_TOKEN="${HF_TOKEN}"
export HF_TOKEN="${HF_TOKEN}"
echo "✓ HuggingFace token loaded"

# ---------------------------------------------------------------------------
# 1. Hunyuan3D — download missing shape model (hunyuan3d-dit-v2-1)
# ---------------------------------------------------------------------------
echo ""
echo "=== Hunyuan3D shape model (hunyuan3d-dit-v2-1) ==="

DIT_CACHE="${HOME}/.cache/huggingface/hub/models--tencent--Hunyuan3D-2.1"
DIT_MODEL_PATH=$(python3 -c "
import os, glob
base = os.path.expanduser('~/.cache/huggingface/hub/models--tencent--Hunyuan3D-2.1/snapshots')
snapshots = glob.glob(os.path.join(base, '*', 'hunyuan3d-dit-v2-1'))
print(snapshots[0] if snapshots else '')
")

if [[ -n "${DIT_MODEL_PATH}" ]]; then
    echo "✓ hunyuan3d-dit-v2-1 already downloaded at ${DIT_MODEL_PATH}"
else
    echo "  Downloading hunyuan3d-dit-v2-1 (~8 GB)..."
    hf_download tencent/Hunyuan3D-2.1 \
        --include "hunyuan3d-dit-v2-1/**" \
        --token "${HF_TOKEN}"
    echo "  ✓ hunyuan3d-dit-v2-1 downloaded"
fi

# Skip remaining large downloads if SKIP_FLUX=1 (useful for partial setup)
if [[ "${SKIP_FLUX:-0}" == "1" ]]; then
    echo ""
    echo "=== Skipping Flux models (SKIP_FLUX=1) ==="
    echo "Hunyuan3D model is ready. Install ComfyUI and download Flux models later with:"
    echo "  bash scripts/setup_models.sh"
    exit 0
fi

# ---------------------------------------------------------------------------
# 2. ComfyUI — clone and set up
# ---------------------------------------------------------------------------
echo ""
echo "=== ComfyUI ==="

if [[ -d "${COMFYUI_DIR}" ]]; then
    echo "✓ ComfyUI already exists at ${COMFYUI_DIR}"
else
    echo "  Cloning ComfyUI..."
    git clone https://github.com/comfy-org/ComfyUI.git "${COMFYUI_DIR}"
    echo "  ✓ ComfyUI cloned"
fi

if [[ ! -d "${COMFYUI_DIR}/.venv" ]]; then
    echo "  Setting up ComfyUI venv..."
    python3 -m venv "${COMFYUI_DIR}/.venv"
    source "${COMFYUI_DIR}/.venv/bin/activate"
    pip install -q -r "${COMFYUI_DIR}/requirements.txt"
    deactivate
    echo "  ✓ ComfyUI venv ready"
else
    echo "✓ ComfyUI venv already exists"
fi

# Create model directories
mkdir -p "${COMFYUI_DIR}/models/unet"
mkdir -p "${COMFYUI_DIR}/models/clip"
mkdir -p "${COMFYUI_DIR}/models/vae"

# ---------------------------------------------------------------------------
# 3. Flux schnell — main model
#    Requires accepting terms at:
#    https://huggingface.co/black-forest-labs/FLUX.1-schnell
# ---------------------------------------------------------------------------
echo ""
echo "=== Flux schnell model files ==="

FLUX_UNET="${PROJECT_DIR}/models/unet/flux1-schnell.safetensors"
if [[ -f "${FLUX_UNET}" ]]; then
    echo "✓ flux1-schnell.safetensors already present at ${FLUX_UNET}"
else
    echo "  Downloading flux1-schnell.safetensors (~24 GB) from black-forest-labs/FLUX.1-schnell..."
    [[ "${GENTLE:-0}" == "1" ]] && echo "  ⚡ Running with idle-only I/O priority (won't kill your network)"
    hf_download black-forest-labs/FLUX.1-schnell \
        flux1-schnell.safetensors \
        --local-dir "${COMFYUI_DIR}/models/unet" \
        --token "${HF_TOKEN}"
    echo "  ✓ flux1-schnell.safetensors downloaded"
fi

# ---------------------------------------------------------------------------
# 4. Flux text encoders (T5-XXL + CLIP-L) — from community repo, no gating
# ---------------------------------------------------------------------------
CLIP_T5="${COMFYUI_DIR}/models/clip/t5xxl_fp16.safetensors"
CLIP_L="${COMFYUI_DIR}/models/clip/clip_l.safetensors"

if [[ -f "${CLIP_T5}" ]]; then
    echo "✓ t5xxl_fp16.safetensors already present"
else
    echo "  Downloading t5xxl_fp16.safetensors (~9.6 GB)..."
    hf_download comfyanonymous/flux_text_encoders \
        t5xxl_fp16.safetensors \
        --local-dir "${COMFYUI_DIR}/models/clip" \
        --token "${HF_TOKEN}"
    echo "  ✓ t5xxl_fp16.safetensors downloaded"
fi

if [[ -f "${CLIP_L}" ]]; then
    echo "✓ clip_l.safetensors already present"
else
    echo "  Downloading clip_l.safetensors (~246 MB)..."
    hf_download comfyanonymous/flux_text_encoders \
        clip_l.safetensors \
        --local-dir "${COMFYUI_DIR}/models/clip" \
        --token "${HF_TOKEN}"
    echo "  ✓ clip_l.safetensors downloaded"
fi

# ---------------------------------------------------------------------------
# 5. Flux VAE (ae.safetensors) — also from the schnell repo
# ---------------------------------------------------------------------------
FLUX_VAE="${COMFYUI_DIR}/models/vae/ae.safetensors"
if [[ -f "${FLUX_VAE}" ]]; then
    echo "✓ ae.safetensors already present"
else
    echo "  Downloading ae.safetensors (~335 MB)..."
    hf_download black-forest-labs/FLUX.1-schnell \
        ae.safetensors \
        --local-dir "${COMFYUI_DIR}/models/vae" \
        --token "${HF_TOKEN}"
    echo "  ✓ ae.safetensors downloaded"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "========================================"
echo "SETUP COMPLETE"
echo ""
echo "Model locations:"
echo "  Hunyuan3D:  ~/.cache/huggingface/hub/models--tencent--Hunyuan3D-2.1"
echo "  Flux unet:  ${COMFYUI_DIR}/models/unet/flux1-schnell.safetensors"
echo "  Flux clip:  ${COMFYUI_DIR}/models/clip/"
echo "  Flux vae:   ${COMFYUI_DIR}/models/vae/ae.safetensors"
echo ""
echo "Next steps:"
[[ "${GENTLE:-0}" == "1" ]] && echo "  (Used idle-only I/O priority. Run without GENTLE=1 for full speed.)"
echo "  Terminal 1:  bash scripts/start_hunyuan_api.sh"
echo "  Terminal 2:  cd ${COMFYUI_DIR} && source .venv/bin/activate && python main.py --listen 0.0.0.0 --port 8188"
echo "  Terminal 3:  python scripts/run_batch.py manifest/city_objects.json"
echo "========================================"
