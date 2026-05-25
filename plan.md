# BLOCKY — Development Plan

## Phase 1: Asset Factory MVP

---

### Goal

Build a fully automated pipeline that takes a JSON manifest of city objects, generates images via Flux in ComfyUI, converts them to textured GLB meshes via Hunyuan3D-2.1 (direct Python), and outputs ready-to-use assets — all from a single command.

**The first win:** `python scripts/run_batch.py manifest/city_objects.json` produces 40 PNGs, 40 GLBs, and 40 metadata files with zero manual intervention.

---

### Architecture Overview

```
city_objects.json (manifest)
        │
        ▼
┌──────────────┐     ┌───────────────┐     ┌──────────────┐
│  Image Gen   │────▶│  Mesh Gen     │────▶│  Output      │
│  (Flux via   │     │  (Hunyuan3D   │     │  (GLB + PNG  │
│   ComfyUI)   │     │   Python CLI) │     │   + JSON)    │
└──────────────┘     └───────────────┘     └──────────────┘
        │                     │                    │
        ▼                     ▼                    ▼
  outputs/images/      outputs/meshes/       outputs/logs/
                              ↑
                        outputs/index.jsonl (state machine)
```

**Why ComfyUI for Flux only:** It's the only tool that natively supports Flux as a reusable JSON node graph, with a local REST API for headless automation. Your RTX 5090 (24 GB VRAM) handles shape generation (~10 GB) comfortably; texture synthesis (~21 GB) fits with careful memory management or gradient checkpointing.

**Why direct Python for Hunyuan3D in Phase 1:** Less brittle than building a custom Comfy workflow. Use the CLI/Python API directly until the pipeline works end-to-end.

---

### Project Structure

```
blocky/
├── plan                          ← This file
├── manifest/
│   └── city_objects.json         ← Object definitions (Phase 1 input)
├── workflows/
│   └── flux_image.json           ← ComfyUI workflow: prompt → Flux image
├── inputs/                       ← Staging area (optional, for manual uploads)
├── outputs/
│   ├── images/                   ← Generated PNGs
│   │   └── <category>/          ← Optional subdirs by category
│   ├── meshes/                   ← GLB + preview PNGs + metadata JSONs
│   │   └── <category>/          ← Mirrors image categories
│   ├── logs/                     ← Pipeline run logs
│   └── index.jsonl               ← Asset state machine (one line per asset)
├── scripts/
│   ├── run_batch.py              ← Main orchestrator (Phase 1)
│   └── validate_manifest.py      ← Schema validation for manifests
├── requirements.txt              ← Python deps
├── README.md                     ← Setup + usage docs
└── .gitignore                    ← Ignore venv, outputs/*, ComfyUI cache
```

---

### Phase 1: Detailed Steps

#### Step 1 — Project Scaffolding (Day 0)

**Tasks:**
- [ ] Create directory structure above
- [ ] Initialize git repo with `.gitignore` (exclude `venv/`, `outputs/*`, ComfyUI model downloads, `__pycache__/`)
- [ ] Write initial `README.md` with setup instructions and pipeline overview
- [ ] Create empty `requirements.txt`

**Deliverable:** Clean project skeleton ready for code.

---

#### Step 2 — Manifest Definition (Day 0)

**Tasks:**
- [ ] Create `manifest/city_objects.json` with the initial dataset:

```json
[
  {
    "id": "streetlamp_001",
    "category": "street_furniture",
    "prompt": "single modern city streetlamp, isolated object, neutral lighting, white background, game asset concept art",
    "negative_prompt": "text, watermark, people, background scene",
    "count": 4,
    "seed": null,
    "image_size": 1024,
    "mesh_quality": "draft"
  },
  {
    "id": "storefront_cash_register_001",
    "category": "storefront",
    "prompt": "cash register on counter, isolated object, white background, clean realistic product render",
    "negative_prompt": "text, watermark, people, cluttered background",
    "count": 4,
    "seed": null,
    "image_size": 1024,
    "mesh_quality": "draft"
  }
]
```

- [ ] Write `scripts/validate_manifest.py` that validates:
  - Each entry has all required fields: `id`, `category`, `prompt`, `negative_prompt`, `count`, `seed`, `image_size`, `mesh_quality`
  - `id` is unique across the manifest
  - `count` is a positive integer (1–20)
  - `prompt` and `negative_prompt` are non-empty and under 500 chars each
  - `image_size` is one of [512, 768, 1024]
  - `mesh_quality` is one of ["draft", "standard"]
- [ ] Run validator against the initial manifest to confirm it passes

**Deliverable:** Validated manifest + schema checker.

---

#### Step 3 — ComfyUI Installation & Configuration (Day 1–2)

**Tasks:**
- [ ] Clone ComfyUI: `git clone https://github.com/comfy-org/ComfyUI.git` into a sibling directory or configurable path (not inside blocky itself to keep the project clean)
- [ ] Create venv, install requirements
- [ ] Download Flux checkpoint (flux-dev or flux-schnell — prefer schnell for speed if quality is acceptable; document tradeoff)
- [ ] Verify ComfyUI starts and serves API on `localhost:8188`
- [ ] Test a manual image generation via the API to confirm it works

**Technical decisions:**
- **Flux model variant:** Start with `flux-schnell` (faster, lower VRAM) — if quality is insufficient for game assets, switch to `flux-dev`. Document both.
- **ComfyUI location:** Install as a sibling directory (`../ComfyUI`) or configurable path via env var `COMFYUI_PATH`. Do NOT embed ComfyUI inside the blocky repo.

**Deliverable:** Running ComfyUI instance with Flux capable of generating images via API.

---

#### Step 4 — Flux Image Generation Workflow (Day 2–3)

**Tasks:**
- [ ] Create `workflows/flux_image.json` — a ComfyUI workflow that:
  - Takes a text prompt as input
  - Runs Flux image generation with these settings:
    - Resolution: configurable via `image_size` field (default 1024×1024)
    - Steps: 4–8 (schnell is distilled; needs fewer steps)
    - CFG/guidance: per Flux spec
    - Sampler: euler or dpm++ (per Flux recommendations)
  - Applies a white/transparent background pass (either via workflow node or post-processing)
  - Outputs a PNG file
- [ ] Test the workflow manually via ComfyUI's API (`POST /prompt`)
- [ ] Verify output image quality — does it look like a clean game asset concept?

**Key considerations:**
- Flux is prompt-sensitive. The manifest prompts should be tuned for consistency: isolated subject, neutral lighting, white background, game-art style.
- Use `negative_prompt` field from the manifest in the workflow's negative conditioning node.

**Deliverable:** Working Flux workflow producing clean asset images via API.

---

#### Step 5 — Hunyuan3D Mesh Generation (Day 3–5)

**Tasks:**
- [ ] Install Hunyuan3D-2.1:
  - Clone the repo or install via pip if available
  - Verify it works with a test image
- [ ] Write direct Python/CLI integration in `run_batch.py` (NOT a Comfy workflow for Phase 1)
- [ ] Pipeline stages to implement:
  1. **Shape generation:** Input PNG → Hunyuan3D shape model → intermediate mesh/point cloud (~10 GB VRAM on 5090 ✓)
  2. **Texture/PBR synthesis:** Shape → texture model (~21 GB VRAM — may need gradient checkpointing or lower-res intermediates)
  3. **GLB export:** Final textured mesh exported as GLB
- [ ] Test end-to-end with a single image from Step 4

**Technical decisions:**
- **VRAM management:** At 29 GB total for shape + texture, the 5090's 24 GB is tight. Options:
  - Run shape and texture sequentially (not in parallel) — this works if we process one image at a time
  - Use `--low-vram` or equivalent flags on Hunyuan3D
  - Downsample input images to 512×512 before mesh generation (document the tradeoff)
- **Batching:** Process sequentially per image. Parallel processing will OOM.

**Deliverable:** Working pipeline: PNG → GLB + preview PNG + metadata JSON.

---

#### Step 6 — Asset State Machine & Batch Runner (`run_batch.py`) (Day 5–7)

**Tasks:**
- [ ] Write `scripts/run_batch.py` with the following logic:

```python
#!/usr/bin/env python3
"""Blocky Phase 1 batch runner.

Usage:
    python scripts/run_batch.py manifest/city_objects.json [--comfyui-url http://localhost:8188]

Asset State Machine (outputs/index.jsonl):
  status values: "pending" | "image_complete" | "mesh_complete" | "failed"
  
Each line is a JSON object tracking one asset's lifecycle.
"""

import json
import sys
import os
import time
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Optional


# --- Configuration ---
COMFYUI_URL = "http://localhost:8188"
OUTPUT_DIR = Path("outputs")
LOG_DIR = OUTPUT_DIR / "logs"
INDEX_PATH = OUTPUT_DIR / "index.jsonl"


def load_manifest(path: str) -> list[dict]:
    """Load and validate the manifest JSON."""
    with open(path) as f:
        data = json.load(f)
    
    required_fields = {"id", "category", "prompt", "negative_prompt", "count", "seed", "image_size", "mesh_quality"}
    
    for i, item in enumerate(data):
        missing = required_fields - set(item.keys())
        if missing:
            raise ValueError(f"Entry {i} ({item.get('id', 'UNKNOWN')}) missing fields: {missing}")
        
        if not isinstance(item["count"], int) or item["count"] < 1:
            raise ValueError(f"Entry '{item['id']}': count must be a positive integer")
        
        if len(item["prompt"]) > 500 or len(item["negative_prompt"]) > 500:
            raise ValueError(f"Entry '{item['id']}': prompt/negative_prompt must be under 500 chars")
    
    # Check for duplicate IDs
    ids = [item["id"] for item in data]
    duplicates = {x for x in ids if ids.count(x) > 1}
    if duplicates:
        raise ValueError(f"Duplicate IDs found: {duplicates}")
    
    return data


def load_index() -> dict[str, dict]:
    """Load the asset state machine from index.jsonl."""
    index = {}
    if INDEX_PATH.exists():
        with open(INDEX_PATH) as f:
            for line in f:
                entry = json.loads(line.strip())
                index[entry["asset_id"]] = entry
    return index


def save_index(index: dict[str, dict]):
    """Write the full state machine back to disk."""
    with open(INDEX_PATH, "w") as f:
        for asset_id in sorted(index.keys()):
            json.dump(index[asset_id], f)
            f.write("\n")


def update_asset_status(asset_id: str, status: str, **kwargs):
    """Update or create an entry in the state machine."""
    index = load_index()
    
    if asset_id not in index:
        index[asset_id] = {
            "asset_id": asset_id,
            "status": "pending",
            "image_path": None,
            "glb_path": None,
            "error": None,
            "vertex_count": None,
            "file_size_bytes": None,
        }
    
    index[asset_id]["status"] = status
    for key, value in kwargs.items():
        if value is not None:
            index[asset_id][key] = value
    
    save_index(index)


def wait_for_comfyui_job(prompt_id: str, comfyui_url: str, timeout: int = 600) -> bool:
    """Poll ComfyUI's /history endpoint until the job completes or times out.
    
    Returns True if the job completed successfully, False on failure/timeout.
    """
    import urllib.request
    
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            req = urllib.request.Request(f"{comfy_url}/history/{prompt_id}")
            with urllib.request.urlopen(req, timeout=10) as resp:
                history = json.loads(resp.read().decode())
            
            if prompt_id in history:
                node_outputs = history[prompt_id].get("outputs", {})
                # Check for image output nodes (typically named "SaveImage" or similar)
                has_image_output = any(
                    "images" in node_outputs.get(node_name, {})
                    for node_name in node_outputs
                )
                if has_image_output:
                    return True
                else:
                    # Job completed but no image output — likely failed silently
                    return False
        
        except Exception as e:
            # ComfyUI might be temporarily unavailable; keep polling
            time.sleep(2)
    
    # Timeout reached
    print(f"  ⚠ ComfyUI job {prompt_id} timed out after {timeout}s")
    return False


def generate_image(prompt: str, negative_prompt: str, output_path: Path, 
                   comfyui_url: str, image_size: int = 1024) -> bool:
    """Generate an image via ComfyUI API. Returns True on success."""
    import urllib.request
    
    # Load workflow template
    workflow_path = Path("workflows/flux_image.json")
    with open(workflow_path) as f:
        workflow = json.load(f)
    
    # Substitute prompt into the workflow (find the CLIPTextEncode node)
    for node_id, node in workflow.items():
        if "inputs" in node and "text" in node["inputs"]:
            if node["class_type"] == "CLIPTextEncode":
                # First text input = positive prompt, second = negative
                pass  # Adjust based on actual workflow structure
    
    # Queue the job
    payload = json.dumps({"prompt": workflow}).encode()
    req = urllib.request.Request(
        f"{comfyui_url}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode())
        prompt_id = result["prompt_id"]
    
    # Wait for completion
    success = wait_for_comfyui_job(prompt_id, comfyui_url)
    
    if not success:
        return False
    
    # Download the generated image from ComfyUI's output directory
    # (Implementation depends on how ComfyUI saves files — typically via SaveImage node)
    # For now, assume the workflow handles saving to a known path
    # TODO: Implement actual file retrieval based on your ComfyUI setup
    
    return True


def generate_mesh(image_path: Path, output_dir: Path, mesh_quality: str = "draft") -> dict:
    """Generate GLB mesh from image via Hunyuan3D Python API. Returns metadata dict."""
    # TODO: Implement direct Hunyuan3D call (not ComfyUI)
    # Example structure:
    #   from hunyuan3d import generate_mesh
    #   result = generate_mesh(str(image_path), quality=mesh_quality)
    #   glb_path = output_dir / f"{image_path.stem}.glb"
    #   result.save(glb_path)
    
    return {
        "vertex_count": 0,  # Placeholder — will be populated by real impl
        "texture_materials": [],
        "generation_time_seconds": 0,
    }


def sanity_check_mesh(glb_path: Path) -> dict:
    """Run basic guardrails on the generated GLB. Returns check results."""
    checks = {
        "exists": glb_path.exists(),
        "file_size_bytes": glb_path.stat().st_size if glb_path.exists() else 0,
        "size_ok": False,
        "loads_ok": False,
        "vertex_count": None,
        "has_textures": False,
    }
    
    if not checks["exists"]:
        return checks
    
    # Check file size > 100 KB
    checks["size_ok"] = checks["file_size_bytes"] > 102400
    
    # Try loading the mesh (requires a GLB loader like trimesh or pygltf)
    try:
        import trimesh
        mesh = trimesh.load(glb_path, process=False)
        checks["loads_ok"] = True
        checks["vertex_count"] = len(mesh.vertices) if hasattr(mesh, 'vertices') else None
        
        # Check for textures/materials
        if hasattr(mesh, 'visual') and mesh.visual is not None:
            checks["has_textures"] = True
    except Exception:
        checks["loads_ok"] = False
    
    return checks


def main():
    if len(sys.argv) < 2:
        print("Usage: python run_batch.py <manifest.json> [--comfyui-url URL]")
        sys.exit(1)
    
    manifest_path = sys.argv[1]
    comfyui_url = COMFYUI_URL
    
    # Parse optional --comfyui-url flag
    if "--comfyui-url" in sys.argv:
        idx = sys.argv.index("--comfyui-url")
        if idx + 1 < len(sys.argv):
            comfyui_url = sys.argv[idx + 1]
    
    # Load manifest
    items = load_manifest(manifest_path)
    print(f"Loaded manifest with {len(items)} object types")
    
    # Setup output dirs
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "images").mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "meshes").mkdir(parents=True, exist_ok=True)
    
    log_path = LOG_DIR / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    
    total_images = sum(item["count"] for item in items)
    total_meshes = total_images  # One mesh per image
    completed_images = 0
    completed_meshes = 0
    failures = []
    
    print(f"\n{'='*60}")
    print(f"BLOCKY Phase 1 — Asset Factory MVP")
    print(f"Total: {total_images} images → {total_meshes} meshes")
    print(f"ComfyUI: {comfyui_url}")
    print(f"{'='*60}\n")
    
    for item in items:
        obj_dir = OUTPUT_DIR / "images" / item["category"]
        obj_dir.mkdir(parents=True, exist_ok=True)
        
        mesh_dir = OUTPUT_DIR / "meshes" / item["category"]
        mesh_dir.mkdir(parents=True, exist_ok=True)
        
        print(f"\n[{item['id']}] Generating {item['count']} variations...")
        print(f"  Prompt: {item['prompt'][:80]}...")
        
        for v in range(1, item["count"] + 1):
            variation_id = f"{v:03d}"
            asset_id = f"{item['id']}_{variation_id}"
            image_path = obj_dir / f"{asset_id}.png"
            
            # Check state machine for skip/resume
            index = load_index()
            if asset_id in index and index[asset_id]["status"] == "mesh_complete":
                print(f"  ✓ {variation_id} — already complete, skipping")
                completed_images += 1
                completed_meshes += 1
                continue
            
            # Generate image (if not already done)
            if not image_path.exists() or index.get(asset_id, {}).get("status") != "image_complete":
                try:
                    print(f"  [{v}/{item['count']}] Generating image...")
                    success = generate_image(
                        item["prompt"],
                        item["negative_prompt"],
                        image_path,
                        comfyui_url,
                        item.get("image_size", 1024)
                    )
                    if not success:
                        raise Exception("Image generation failed")
                    
                    update_asset_status(asset_id, "image_complete", image_path=str(image_path))
                    completed_images += 1
                    print(f"    → {image_path}")
                except Exception as e:
                    failures.append({"id": item["id"], "variation": variation_id, "error": str(e)})
                    update_asset_status(asset_id, "failed", error=str(e))
                    print(f"    ✗ FAILED: {e}")
                    continue
            
            # Generate mesh (if not already done)
            glb_path = mesh_dir / f"{asset_id}.glb"
            if not glb_path.exists() or index.get(asset_id, {}).get("status") != "mesh_complete":
                try:
                    print(f"  [{v}/{item['count']}] Generating mesh...")
                    mesh_info = generate_mesh(image_path, mesh_dir, item.get("mesh_quality", "draft"))
                    
                    # Sanity checks
                    sanity = sanity_check_mesh(glb_path)
                    if not sanity["exists"] or not sanity["size_ok"]:
                        raise Exception(f"Mesh failed sanity check: {sanity}")
                    
                    update_asset_status(
                        asset_id, 
                        "mesh_complete", 
                        glb_path=str(glb_path),
                        vertex_count=sanity["vertex_count"],
                        file_size_bytes=sanity["file_size_bytes"]
                    )
                    completed_meshes += 1
                    print(f"    → {glb_path} ({sanity['vertex_count']} verts)")
                except Exception as e:
                    failures.append({"id": item["id"], "variation": variation_id, "error": str(e)})
                    update_asset_status(asset_id, "failed", error=str(e))
                    print(f"    ✗ MESH FAILED: {e}")
        
        time.sleep(1)  # Brief pause between object types
    
    # Summary
    print(f"\n{'='*60}")
    print(f"PIPELINE COMPLETE")
    print(f"  Images generated: {completed_images}/{total_images}")
    print(f"  Meshes generated: {completed_meshes}/{total_meshes}")
    print(f"  Failures:         {len(failures)}")
    
    if failures:
        print(f"\nFailures:")
        for f in failures:
            print(f"  - {f['id']} v{f['variation']}: {f['error']}")
    
    # Write failure log
    if failures:
        fail_log = LOG_DIR / "failures.json"
        with open(fail_log, "w") as f:
            json.dump(failures, f, indent=2)
        print(f"\nFailure details written to {fail_log}")
    
    # Write run summary log
    with open(log_path, "a") as f:
        f.write(f"Run completed at {datetime.utcnow().isoformat()}\n")
        f.write(f"Images: {completed_images}/{total_images}, Meshes: {completed_meshes}/{total_meshes}, Failures: {len(failures)}\n")
    
    print(f"\nLog written to {log_path}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
```

- [ ] Add `--dry-run` flag support (validate manifest and show what would be done without executing)
- [ ] Add progress bar (use `tqdm` if available, or simple text progress)

**Key behaviors:**
- **State machine skip/resume:** Check `outputs/index.jsonl` for asset status before regenerating
- **Per-object error isolation:** One failure doesn't kill the whole run
- **Structured logging:** Timestamped log files + failure JSON dump
- **Idempotent:** Safe to re-run; won't overwrite existing assets

**Deliverable:** Working batch runner that processes a manifest end-to-end.

---

#### Step 7 — Integration Testing & First Run (Day 7–8)

**Tasks:**
- [ ] Start ComfyUI with Flux loaded
- [ ] Run: `python scripts/run_batch.py manifest/city_objects.json`
- [ ] Verify outputs:
  - [ ] 4 PNG images in `outputs/images/street_furniture/`
  - [ ] 4 PNG images in `outputs/images/storefront/`
  - [ ] 4 GLB files in `outputs/meshes/street_furniture/`
  - [ ] 4 GLB files in `outputs/meshes/storefront/`
  - [ ] 8 metadata JSONs (one per asset, beside the mesh)
- [ ] Verify `outputs/index.jsonl` has correct status transitions for all assets
- [ ] Visually inspect images — are they clean, isolated game assets?
- [ ] Open meshes in a viewer (Blender or online GLB viewer) — is the geometry reasonable?
- [ ] Check metadata JSONs for completeness

**Success criteria:** All 8 assets generated without manual intervention. No crashes. Logs show clean run. State machine reflects accurate status.

---

#### Step 8 — Scale to Full Phase 1 Target (Day 8–9)

**Tasks:**
- [ ] Expand `manifest/city_objects.json` to **10 object types × 4 variations = 40 assets**:

```json
[
  {"id": "streetlamp_001", "category": "street_furniture", "prompt": "...", "negative_prompt": "...", "count": 4, "seed": null, "image_size": 1024, "mesh_quality": "draft"},
  {"id": "bench_001",       "category": "street_furniture", "prompt": "...", "negative_prompt": "...", "count": 4, "seed": null, "image_size": 1024, "mesh_quality": "draft"},
  {"id": "trash_can_001",   "category": "street_furniture", "prompt": "...", "negative_prompt": "...", "count": 4, "seed": null, "image_size": 1024, "mesh_quality": "draft"},
  {"id": "cash_register_001","category": "storefront",      "prompt": "...", "negative_prompt": "...", "count": 4, "seed": null, "image_size": 1024, "mesh_quality": "draft"},
  {"id": "display_shelf_001","category": "storefront",      "prompt": "...", "negative_prompt": "...", "count": 4, "seed": null, "image_size": 1024, "mesh_quality": "draft"},
  {"id": "traffic_light_001","category": "street_furniture", "prompt": "...", "negative_prompt": "...", "count": 4, "seed": null, "image_size": 1024, "mesh_quality": "draft"},
  {"id": "fire_hydrant_001", "category": "street_furniture", "prompt": "...", "negative_prompt": "...", "count": 4, "seed": null, "image_size": 1024, "mesh_quality": "draft"},
  {"id": "crosswalk_signal_001","category":"street_furniture", "prompt": "...", "negative_prompt": "...", "count": 4, "seed": null, "image_size": 1024, "mesh_quality": "draft"},
  {"id": "bus_stop_shelter_001","category":"street_furniture", "prompt": "...", "negative_prompt": "...", "count": 4, "seed": null, "image_size": 1024, "mesh_quality": "draft"},
  {"id": "mailbox_001",     "category": "street_furniture", "prompt": "...", "negative_prompt": "...", "count": 4, "seed": null, "image_size": 1024, "mesh_quality": "draft"}
]
```

- [ ] Run full pipeline: `python scripts/run_batch.py manifest/city_objects.json`
- [ ] Monitor VRAM usage — ensure no OOM errors on the 5090
- [ ] Time the total run (expect several hours depending on mesh generation speed)

**Deliverable:** **Phase 1 complete.** 40 PNGs, 40 GLBs, 40 metadata files. A clean local library of city objects manufactured from a manifest.

---

### Technical Decisions & Rationale

| Decision | Choice | Rationale |
|----------|--------|-----------|
| ComfyUI location | Sibling directory or env-configured path | Keep blocky repo clean; ComfyUI is a dependency, not part of the project |
| Flux variant | `flux-schnell` first, fallback to `flux-dev` | Schnell is distilled (4–8 steps), faster, lower VRAM. Quality can be evaluated before committing |
| Hunyuan3D integration | Direct Python/CLI for Phase 1 | Less brittle than building a custom Comfy workflow; focus on pipeline correctness first |
| Mesh generation mode | Sequential per-image | 29 GB total for shape+texture exceeds 5090's 24 GB; sequential avoids OOM |
| Input resolution for mesh gen | Start at 1024×1024, fallback to 512×512 if VRAM constrained | Higher res = better texture detail; document the tradeoff |
| Batch processing | Sequential (one image → one mesh) | Hunyuan3D is memory-heavy; parallel would OOM on consumer hardware |
| Skip logic | `outputs/index.jsonl` state machine | Enables resume, retry, QA tracking, and future Blender cleanup integration |

---

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Hunyuan3D VRAM OOM on 5090 | Medium | High | Use `--low-vram` flags; downsample inputs to 512px; process one at a time |
| Flux output quality insufficient for game assets | Medium | Medium | Tune prompts, try different checkpoints (schnell vs dev), add post-processing |
| Mesh generation very slow (>30 min/image) | High | Low | Acceptable for Phase 1; Phase 2 can optimize or cache |
| ComfyUI API instability | Low | Medium | Add retry logic in `run_batch.py`; log detailed errors |
| GLB files too large/complex for engine import | Medium | Medium | Phase 2 adds mesh decimation and LOD generation |

---

### Phase 1 Success Criteria

**Definition of Done:**

- [ ] `python scripts/run_batch.py manifest/city_objects.json` runs without manual intervention
- [ ] Output: **40 PNG images**, **40 GLB meshes**, **40 metadata JSON files** (beside the mesh, not beside the image)
- [ ] All assets are in the correct directory structure under `outputs/`
- [ ] Meshes open in a 3D viewer with visible geometry and textures
- [ ] Metadata JSONs contain source image path, generation timestamp, and mesh info
- [ ] Re-running the script skips already-completed assets (idempotent via state machine)
- [ ] Failed assets are logged to `outputs/logs/failures.json`
- [ ] `outputs/index.jsonl` accurately reflects asset status for all 40 entries

**What Phase 1 is NOT:**
- ❌ Not training any models
- ❌ Not quality scoring or filtering (only basic sanity checks)
- ❌ Not Blender cleanup or mesh decimation
- ❌ Not engine integration (Blender/Rust)
- ❌ Not a large object catalog (start small, validate the pipeline first)

---

### Phase 2 Preview (Not in scope for this plan)

Once Phase 1 succeeds:
1. **Quality scoring** — auto-evaluate image/mesh quality; flag low-quality assets
2. **Blender cleanup pipeline** — automated mesh decimation, UV unwrapping, naming standards
3. **Asset families** — reusable templates (e.g., "streetlamp" → variants with different heights/styles)
4. **Engine integration** — export to Rust engine format + Blender-ready FBX/OBJ
5. **Manifest versioning** — track which assets were generated from which manifest revision

---

### File Index

| File | Purpose | Status |
|------|---------|--------|
| `plan` | This development plan | ✅ Draft (updated per feedback) |
| `manifest/city_objects.json` | Object definitions for pipeline input | ⬜ To create |
| `workflows/flux_image.json` | ComfyUI Flux image generation workflow | ⬜ To create |
| `scripts/run_batch.py` | Main batch orchestrator + state machine | ⬜ To create |
| `scripts/validate_manifest.py` | Manifest schema validator | ⬜ To create |
| `requirements.txt` | Python dependencies | ⬜ To create |
| `README.md` | Setup and usage documentation | ⬜ To create |
