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
#   1. Clones Hunyuan3D-2.1, creates its venv, and installs dependencies
#   2. Downloads missing Hunyuan3D-2.1 model folders
#   3. Clones ComfyUI into ../ComfyUI
#   4. Sets up ComfyUI venv + dependencies
#   5. Downloads Flux schnell model files (~34 GB total)
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
if [[ -z "${HUNYUAN_DIR:-}" ]]; then
    if [[ -d "/home/dana/workspace" ]]; then
        HUNYUAN_DIR="/home/dana/workspace/hunyuan3d-2.1"
    else
        HUNYUAN_DIR="${PROJECT_DIR}/../hunyuan3d-2.1"
    fi
fi
HUNYUAN_REPO_URL="${HUNYUAN_REPO_URL:-https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1.git}"
HUNYUAN_SRC_DIR="${HUNYUAN_DIR}/hunyuan3d-2.1-src"
if [[ -z "${COMFYUI_DIR:-}" ]]; then
    if [[ -d "/home/dana/comfy/ComfyUI" ]]; then
        COMFYUI_DIR="/home/dana/comfy/ComfyUI"
    else
        COMFYUI_DIR="${PROJECT_DIR}/../ComfyUI"
    fi
fi

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
    if command -v python3.10 >/dev/null 2>&1; then
        PYTHON_BIN="python3.10"
    elif command -v python3.11 >/dev/null 2>&1; then
        PYTHON_BIN="python3.11"
    else
        PYTHON_BIN="python3"
    fi
fi

if [[ -n "${HF_BIN:-}" ]]; then
    :
elif [[ -x "${HUNYUAN_DIR}/.venv/bin/hf" ]]; then
    HF_BIN="${HUNYUAN_DIR}/.venv/bin/hf"
elif [[ -x "${PROJECT_DIR}/.venv/bin/hf" ]]; then
    HF_BIN="${PROJECT_DIR}/.venv/bin/hf"
else
    HF_BIN="hf"
fi

# ---------------------------------------------------------------------------
# Read HF token from .env  (key has a space: "HUGGING FACE=...")
# ---------------------------------------------------------------------------
ENV_FILE="${PROJECT_DIR}/.env"
HF_TOKEN="${HF_TOKEN:-${HUGGING_FACE_HUB_TOKEN:-}}"
if [[ -z "${HF_TOKEN}" && -f "${ENV_FILE}" ]]; then
    HF_TOKEN=$(python3 -c "
with open('${ENV_FILE}') as f:
    for line in f:
        if '=' in line and 'FACE' in line.upper():
            print(line.split('=', 1)[1].strip())
            break
")
fi

if [[ -z "${HF_TOKEN}" ]]; then
    echo "✗ HuggingFace token not found. Set HF_TOKEN, HUGGING_FACE_HUB_TOKEN, or add it to ${ENV_FILE}"
    exit 1
fi

export HUGGING_FACE_HUB_TOKEN="${HF_TOKEN}"
export HF_TOKEN="${HF_TOKEN}"
echo "✓ HuggingFace token loaded"

if ! command -v "${HF_BIN}" >/dev/null 2>&1; then
    echo "✗ HuggingFace CLI not found at '${HF_BIN}'"
    echo "  Install it with: ${PROJECT_DIR}/.venv/bin/pip install huggingface_hub[cli]"
    exit 1
fi

# ---------------------------------------------------------------------------
# 1. Hunyuan3D — source, venv, dependencies, and model downloads
# ---------------------------------------------------------------------------
echo ""
echo "=== Hunyuan3D source + environment ==="

mkdir -p "${HUNYUAN_DIR}"
if [[ -d "${HUNYUAN_SRC_DIR}/.git" ]]; then
    echo "✓ Hunyuan3D source already exists at ${HUNYUAN_SRC_DIR}"
    git -C "${HUNYUAN_SRC_DIR}" pull --ff-only || echo "  ! Could not fast-forward Hunyuan3D source; leaving existing checkout in place"
elif [[ -e "${HUNYUAN_SRC_DIR}" ]]; then
    echo "✗ ${HUNYUAN_SRC_DIR} exists but is not a git checkout"
    echo "  Move it aside or set HUNYUAN_DIR to a clean install location."
    exit 1
else
    echo "  Cloning Hunyuan3D-2.1 into ${HUNYUAN_SRC_DIR}..."
    git clone "${HUNYUAN_REPO_URL}" "${HUNYUAN_SRC_DIR}"
    echo "  ✓ Hunyuan3D source cloned"
fi

if [[ ! -d "${HUNYUAN_DIR}/.venv" ]]; then
    echo "  Creating Hunyuan3D venv with ${PYTHON_BIN}..."
    "${PYTHON_BIN}" -m venv "${HUNYUAN_DIR}/.venv"
    echo "  ✓ Hunyuan3D venv ready"
else
    echo "✓ Hunyuan3D venv already exists"
fi

HUNYUAN_PYTHON="${HUNYUAN_DIR}/.venv/bin/python"
HUNYUAN_PIP="${HUNYUAN_DIR}/.venv/bin/pip"

echo "  Installing Hunyuan3D Python dependencies..."
"${HUNYUAN_PYTHON}" -m pip install -q --upgrade pip wheel setuptools
"${HUNYUAN_PIP}" install torch==2.11.0 torchvision==0.26.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/cu128
"${HUNYUAN_PIP}" install numpy==1.24.4 cython==3.0.12
# Hunyuan includes packages that try to bootstrap torch/numpy inside isolated
# build envs. Reuse the venv copies above so installs stay visible and resumable.
HUNYUAN_REQUIREMENTS="$(mktemp)"
grep -v -E '^[[:space:]]*bpy==4\.0([[:space:]]|$)' "${HUNYUAN_SRC_DIR}/requirements.txt" > "${HUNYUAN_REQUIREMENTS}"
"${HUNYUAN_PIP}" install --no-build-isolation -r "${HUNYUAN_REQUIREMENTS}"
rm -f "${HUNYUAN_REQUIREMENTS}"
"${HUNYUAN_PIP}" install bpy==4.2.0

if [[ -d "${HUNYUAN_SRC_DIR}/hy3dpaint/custom_rasterizer" ]]; then
    echo "  Installing Hunyuan3D custom rasterizer..."
    "${HUNYUAN_PIP}" install --no-build-isolation -e "${HUNYUAN_SRC_DIR}/hy3dpaint/custom_rasterizer"
fi

mkdir -p "${HUNYUAN_SRC_DIR}/gradio_cache"

if [[ -x "${HUNYUAN_SRC_DIR}/hy3dpaint/DifferentiableRenderer/compile_mesh_painter.sh" ]]; then
    echo "  Compiling Hunyuan3D differentiable renderer..."
    (
        PYBIND_INCLUDES="$("${HUNYUAN_PYTHON}" -m pybind11 --includes)"
        EXT_SUFFIX="$("${HUNYUAN_PYTHON}" -c 'import sysconfig; print(sysconfig.get_config_var("EXT_SUFFIX"))')"
        cd "${HUNYUAN_SRC_DIR}/hy3dpaint/DifferentiableRenderer"
        # Use the Hunyuan venv Python for includes/suffix; the upstream helper
        # assumes `python` and `python3-config` resolve to the same interpreter.
        c++ -O3 -Wall -shared -std=c++11 -fPIC ${PYBIND_INCLUDES} \
            mesh_inpaint_processor.cpp -o "mesh_inpaint_processor${EXT_SUFFIX}"
    )
fi

mkdir -p "${HUNYUAN_SRC_DIR}/hy3dpaint/ckpt"
REALESRGAN_CKPT="${HUNYUAN_SRC_DIR}/hy3dpaint/ckpt/RealESRGAN_x4plus.pth"
if [[ -f "${REALESRGAN_CKPT}" ]]; then
    echo "✓ RealESRGAN_x4plus.pth already present"
elif command -v wget >/dev/null 2>&1; then
    echo "  Downloading RealESRGAN_x4plus.pth..."
    wget -q https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth \
        -P "${HUNYUAN_SRC_DIR}/hy3dpaint/ckpt"
else
    echo "  Downloading RealESRGAN_x4plus.pth..."
    curl -fsSL https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth \
        -o "${REALESRGAN_CKPT}"
fi

echo ""
echo "=== Hunyuan3D model folders ==="

DIT_CACHE="${HOME}/.cache/huggingface/hub/models--tencent--Hunyuan3D-2.1"
DIT_MODEL_PATH=$(python3 -c "
import os, glob
base = os.path.expanduser('~/.cache/huggingface/hub/models--tencent--Hunyuan3D-2.1/snapshots')
snapshots = glob.glob(os.path.join(base, '*', 'hunyuan3d-dit-v2-1'))
print(snapshots[0] if snapshots else '')
")
PAINT_MODEL_PATH=$(python3 -c "
import os, glob
base = os.path.expanduser('~/.cache/huggingface/hub/models--tencent--Hunyuan3D-2.1/snapshots')
snapshots = glob.glob(os.path.join(base, '*', 'hunyuan3d-paintpbr-v2-1'))
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

if [[ -n "${PAINT_MODEL_PATH}" ]]; then
    echo "✓ hunyuan3d-paintpbr-v2-1 already downloaded at ${PAINT_MODEL_PATH}"
else
    echo "  Downloading hunyuan3d-paintpbr-v2-1..."
    hf_download tencent/Hunyuan3D-2.1 \
        --include "hunyuan3d-paintpbr-v2-1/**" \
        --token "${HF_TOKEN}"
    echo "  ✓ hunyuan3d-paintpbr-v2-1 downloaded"
fi

# Skip remaining large downloads if SKIP_FLUX=1 (useful for partial setup)
if [[ "${SKIP_FLUX:-0}" == "1" ]]; then
    echo ""
    echo "=== Skipping Flux models (SKIP_FLUX=1) ==="
    echo "Hunyuan3D is ready. Install ComfyUI and download Flux models later with:"
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

FLUX_UNET="${COMFYUI_DIR}/models/unet/flux1-schnell.safetensors"
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
echo "  Hunyuan3D app:     ${HUNYUAN_DIR}"
echo "  Hunyuan3D models:  ~/.cache/huggingface/hub/models--tencent--Hunyuan3D-2.1"
echo "  Flux unet:  ${COMFYUI_DIR}/models/unet/flux1-schnell.safetensors"
echo "  Flux clip:  ${COMFYUI_DIR}/models/clip/"
echo "  Flux vae:   ${COMFYUI_DIR}/models/vae/ae.safetensors"
echo ""
echo "Next steps:"
[[ "${GENTLE:-0}" == "1" ]] && echo "  (Used idle-only I/O priority. Run without GENTLE=1 for full speed.)"
echo "  Terminal 1:  HUNYUAN_DIR=${HUNYUAN_DIR} bash scripts/start_hunyuan_api.sh"
echo "  Terminal 2:  cd ${COMFYUI_DIR} && source .venv/bin/activate && python main.py --listen 0.0.0.0 --port 8188"
echo "  Terminal 3:  python scripts/run_batch.py manifest/city_objects.json"
echo "========================================"
