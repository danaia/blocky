> **Recommended GPU: NVIDIA GeForce RTX 5090 (32 GB VRAM) or better for best results.** Blocky's local image-to-3D workflow is designed for this class of GPU. Mesh generation needs at least 20,000 MiB of free GPU memory; unload other GPU models before starting a 3D job.

# BLOCKY

Automated pipeline: manifest → Flux images → Hunyuan3D GLB meshes.

One command produces PNGs, GLBs, and per-asset metadata from a JSON manifest.

---

## Prerequisites

### 1. ComfyUI with Flux schnell

Install ComfyUI as a sibling directory (outside this repo):

```bash
git clone https://github.com/comfy-org/ComfyUI.git ../ComfyUI
cd ../ComfyUI && python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Download model checkpoints into `../ComfyUI/models/`:
- `unet/flux1-schnell.safetensors`
- `clip/t5xxl_fp16.safetensors`
- `clip/clip_l.safetensors`
- `vae/ae.safetensors`

Update `workflows/flux_image.json` nodes 1, 2, 3 with your actual filenames if they differ.

Start ComfyUI (keep running in a separate terminal):
```bash
cd ../ComfyUI && source .venv/bin/activate
python main.py --listen 0.0.0.0 --port 8188
```

### 2. Hunyuan3D API server

Already installed at `/home/dana/workspace/hunyuan3d-2.1`.

Start the API server (keep running in a separate terminal):
```bash
bash scripts/start_hunyuan_api.sh
```

The server exposes `http://localhost:8082`. Verify it's up:
```bash
curl http://localhost:8082/health
```

---

## Setup

```bash
cd /home/dana/workspace/blocky
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

---

## Usage

**Validate the manifest:**
```bash
python scripts/validate_manifest.py manifest/city_objects.json
```

**Dry run (see what will be generated):**
```bash
python scripts/run_batch.py manifest/city_objects.json --dry-run
```

**Run the full pipeline:**
```bash
python scripts/run_batch.py manifest/city_objects.json
```

**Resume after failure:** Re-running the same command skips already-completed assets (`mesh_complete` in `outputs/index.jsonl`).

**Custom service URLs:**
```bash
python scripts/run_batch.py manifest/city_objects.json \
    --comfyui-url http://localhost:8188 \
    --hunyuan-url http://localhost:8082
```

**Optional post-process: decimate GLBs for in-game assets**
```bash
blender --background --python scripts/decimate_glbs.py -- \
  outputs/meshes/street_furniture/*.glb
```

This step requires Blender because `scripts/decimate_glbs.py` uses Blender's `bpy` API.
LM Studio is not used by the decimation script.

---

## Output Structure

```
outputs/
├── images/
│   ├── street_furniture/
│   │   └── streetlamp_001_001.png
│   └── storefront/
│       └── cash_register_001_001.png
├── meshes/
│   ├── street_furniture/
│   │   ├── streetlamp_001_001.glb
│   │   └── streetlamp_001_001.json   ← per-asset metadata
│   └── storefront/
│       ├── cash_register_001_001.glb
│       └── cash_register_001_001.json
├── logs/
│   ├── run_20260525_120000.log
│   └── failures.json                 ← only written if failures occur
└── index.jsonl                       ← asset state machine (one line per asset)
```

### Asset state machine (`index.jsonl`)

Each line tracks one asset through its lifecycle:

| `status` | Meaning |
|---|---|
| `pending` | Not yet started |
| `image_complete` | PNG generated successfully |
| `mesh_complete` | GLB + metadata written, sanity checks passed |
| `failed` | Error occurred (see `error` field) |

### Per-asset metadata JSON

Written beside each `.glb` file:

```json
{
  "asset_id": "streetlamp_001_001",
  "source_prompt": "...",
  "seed": 123456789,
  "image_size": 1024,
  "mesh_quality": "draft",
  "generator": { "image": "flux-schnell", "mesh": "hunyuan3d-2.1" },
  "sanity": { "file_size_bytes": 245760, "vertex_count": 12483, "loads_ok": true, "has_textures": false }
}
```

---

## Workflow Node IDs

`workflows/flux_image.json` uses these fixed node IDs referenced by `run_batch.py`:

| Node | ID | Purpose |
|---|---|---|
| `CLIPTextEncode` | `4` | Positive prompt text |
| `EmptySD3LatentImage` | `5` | Width / height |
| `RandomNoise` | `8` | Seed |
| `SaveImage` | `13` | Output PNG |

Do not renumber these nodes when customising the workflow.

---

## Mesh Quality Settings

| `mesh_quality` | `texture` | `octree_resolution` | Use case |
|---|---|---|---|
| `draft` | false | 256 | Fast iteration, shape only |
| `standard` | true | 256 | Full PBR textures |

---

## Phase 1 Success Criteria

- `python scripts/run_batch.py manifest/city_objects.json` runs without manual intervention
- Output: 40 PNGs, 40 GLBs, 40 metadata JSONs
- Meshes open in a 3D viewer with visible geometry
- Re-running skips already-completed assets
- Failed assets logged to `outputs/logs/failures.json`

## Browser control panel

After **Generate 3D**, a global footer follows the job across manifests and the
image review dialog. It shows the active stage, completed model count, estimated
overall progress, elapsed time, and approximate time remaining. ETA uses recent
mesh metadata (a 90-second per-model fallback) plus startup and export allowances.
Progress reaches 100% only after Blender export succeeds. Failed or cancelled
jobs remain visible with a link to the job log; finished jobs can be dismissed.

AI Entries defaults to **Codex · ChatGPT sign-in**, using the local Codex CLI
and your eligible ChatGPT/Codex subscription usage. Install the official Codex
CLI and run `codex login` in a terminal as the same user who starts Blocky.
Use `codex login status` or **Check connection** in Blocky to verify sign-in.
If the CLI is outside the server's PATH, set `CODEX_BIN=/absolute/path/to/codex`
before launching Blocky. Login remains in Codex; Blocky does not read or copy tokens.

Generation runs `codex exec` with a JSON schema in a temporary directory,
with a read-only sandbox, shell tools and web search disabled, and a 180-second
timeout. It ignores user Codex configuration and requires ChatGPT authentication;
API key environment variables are removed from the child process. Subscription
usage limits still apply. Generated entries appear in the editor for review.

The **OpenAI API · separate billing** option retains the API generator and uses
`OPENAI_API_KEY` (or legacy `OPENAI_API`) from `.env`. Neither AI provider is
needed for local Flux image generation or Hunyuan3D conversion once a manifest exists.

Run `./start_blocky_ui.sh` and open http://127.0.0.1:8765/.
The launcher starts ComfyUI on port 8188 and the UI on port 8765.
Hunyuan3D starts on demand for mesh jobs on port 8082.
Existing services are preserved; conflicting ports produce an error.
Override `HUNYUAN_PORT`, `COMFYUI_PORT`, or `BLOCKY_UI_PORT` in the environment
before launching to use different ports. The batch runner also honors
`HUNYUAN_PORT` and `COMFYUI_PORT` (explicit URL arguments take precedence).
Mesh jobs need at least 20,000 MiB of free GPU memory. Unload other GPU models
before generating meshes when memory is insufficient.
