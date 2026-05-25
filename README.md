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

The server exposes `http://localhost:8081`. Verify it's up:
```bash
curl http://localhost:8081/health
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
    --hunyuan-url http://localhost:8081
```

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
