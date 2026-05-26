#!/usr/bin/env python3
"""Blocky Phase 1: Manifest → Image → GLB Batch Pipeline.

Usage:
    python scripts/run_batch.py manifest/city_objects.json
    python scripts/run_batch.py manifest/city_objects.json --dry-run
    python scripts/run_batch.py manifest/city_objects.json --comfyui-url http://localhost:8188
    python scripts/run_batch.py manifest/city_objects.json --hunyuan-url http://localhost:8081

Asset State Machine (outputs/index.jsonl):
  status: "pending" | "image_complete" | "mesh_complete" | "failed"
"""

import argparse
import base64
import hashlib
import json
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

try:
    from rembg import remove as rembg_remove, new_session as rembg_session
    HAS_REMBG = True
except ImportError:
    HAS_REMBG = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

COMFYUI_URL = "http://localhost:8188"
HUNYUAN_URL = "http://localhost:8081"

OUTPUT_DIR = Path("outputs")
LOG_DIR = OUTPUT_DIR / "logs"
INDEX_PATH = OUTPUT_DIR / "index.jsonl"

# ComfyUI workflow node IDs — must match workflows/flux_image.json
COMFYUI_POSITIVE_NODE = "4"   # CLIPTextEncode — positive prompt text
COMFYUI_SEED_NODE = "8"       # RandomNoise — noise_seed
COMFYUI_LATENT_NODE = "5"     # EmptySD3LatentImage — width / height
COMFYUI_OUTPUT_NODE = "13"    # SaveImage — images list

# Hunyuan3D generation parameters per mesh_quality.
# NOTE: The current model_worker.py ignores all params except `image` —
# it always runs full 50-step diffusion + PBR texturing regardless of these values.
# Params are sent anyway for forward-compatibility if the worker is updated.
MESH_QUALITY_PARAMS: dict = {
    "draft": {
        "texture": False,
        "octree_resolution": 256,
        "num_inference_steps": 5,
        "guidance_scale": 5.0,
        "face_count": 40000,
    },
    "standard": {
        "texture": True,
        "octree_resolution": 256,
        "num_inference_steps": 5,
        "guidance_scale": 5.0,
        "face_count": 40000,
    },
}

IMAGE_GENERATOR = "flux-schnell"
MESH_GENERATOR = "hunyuan3d-2.1"


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def derive_seed(asset_id: str) -> int:
    """Return a stable deterministic uint32 seed derived from asset_id via SHA-256."""
    digest = hashlib.sha256(asset_id.encode()).digest()
    return struct.unpack(">I", digest[:4])[0]


# ---------------------------------------------------------------------------
# State machine  (outputs/index.jsonl)
# ---------------------------------------------------------------------------

def load_index() -> dict:
    index: dict = {}
    if INDEX_PATH.exists():
        with open(INDEX_PATH) as f:
            for line in f:
                line = line.strip()
                if line:
                    entry = json.loads(line)
                    index[entry["asset_id"]] = entry
    return index


def save_index(index: dict) -> None:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(INDEX_PATH, "w") as f:
        for asset_id in sorted(index.keys()):
            json.dump(index[asset_id], f)
            f.write("\n")


def update_asset_status(asset_id: str, status: str, **kwargs) -> None:
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
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    index[asset_id]["status"] = status
    index[asset_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
    for key, value in kwargs.items():
        index[asset_id][key] = value
    save_index(index)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def load_manifest(path: str) -> list:
    with open(path) as f:
        data = json.load(f)

    required = {"id", "category", "prompt", "negative_prompt", "count", "seed", "image_size", "mesh_quality"}
    for i, item in enumerate(data):
        missing = required - set(item.keys())
        if missing:
            raise ValueError(f"Entry {i} ({item.get('id', 'UNKNOWN')}) missing fields: {missing}")
        if not isinstance(item["count"], int) or item["count"] < 1:
            raise ValueError(f"'{item['id']}': count must be a positive integer")
        if len(item["prompt"]) > 500 or len(item["negative_prompt"]) > 500:
            raise ValueError(f"'{item['id']}': prompt/negative_prompt must be under 500 chars")

    ids = [item["id"] for item in data]
    dupes = {x for x in ids if ids.count(x) > 1}
    if dupes:
        raise ValueError(f"Duplicate IDs in manifest: {dupes}")

    return data


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def http_get(url: str, timeout: int = 10) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
        return resp.read()


def http_post(url: str, payload: dict, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def check_service_health(url: str, name: str, path: str = "/health") -> None:
    """Verify a service is reachable at url+path. Exits the process on failure."""
    try:
        http_get(f"{url}{path}", timeout=5)
        print(f"  ✓ {name} reachable at {url}")
    except Exception as e:
        print(f"  ✗ {name} not reachable at {url}: {e}")
        print(f"    Start {name} before running the pipeline.")
        sys.exit(1)


def free_comfyui_vram(comfyui_url: str) -> None:
    """Unload ComfyUI models from VRAM so Hunyuan3D has full GPU memory for texturing."""
    try:
        http_post(f"{comfyui_url}/free", {"unload_models": True, "free_memory": True}, timeout=10)
    except Exception:
        pass  # non-fatal — texturing will just be slower


# ---------------------------------------------------------------------------
# ComfyUI — image generation
# ---------------------------------------------------------------------------

def _build_workflow(prompt: str, seed: int, image_size: int) -> dict:
    with open(Path("workflows/flux_image.json")) as f:
        workflow = json.load(f)
    # Strip any non-node top-level keys (e.g. _readme) — ComfyUI's validator
    # expects every top-level value to be a dict and will 500 on strings.
    workflow = {k: v for k, v in workflow.items() if isinstance(v, dict)}
    workflow[COMFYUI_POSITIVE_NODE]["inputs"]["text"] = prompt
    workflow[COMFYUI_SEED_NODE]["inputs"]["noise_seed"] = seed
    workflow[COMFYUI_LATENT_NODE]["inputs"]["width"] = image_size
    workflow[COMFYUI_LATENT_NODE]["inputs"]["height"] = image_size
    return workflow


def _poll_comfyui(prompt_id: str, comfyui_url: str, timeout: int = 300) -> Optional[dict]:
    """Poll /history/{prompt_id} until the output node appears. Returns output dict or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data = json.loads(http_get(f"{comfyui_url}/history/{prompt_id}", timeout=10))
            if prompt_id in data:
                outputs = data[prompt_id].get("outputs", {})
                if COMFYUI_OUTPUT_NODE in outputs:
                    return outputs[COMFYUI_OUTPUT_NODE]
        except Exception:
            pass
        time.sleep(3)
    return None


def generate_image(
    prompt: str,
    output_path: Path,
    comfyui_url: str,
    image_size: int,
    seed: int,
) -> None:
    """Queue a Flux job in ComfyUI, wait for completion, download the PNG to output_path."""
    workflow = _build_workflow(prompt, seed, image_size)
    result = http_post(f"{comfyui_url}/prompt", {"prompt": workflow})
    prompt_id = result["prompt_id"]

    node_output = _poll_comfyui(prompt_id, comfyui_url)
    if not node_output:
        raise RuntimeError(f"ComfyUI job {prompt_id} timed out or produced no output")

    images = node_output.get("images", [])
    if not images:
        raise RuntimeError(f"ComfyUI job {prompt_id}: output node has no images")

    img_info = images[0]
    qs = urllib.parse.urlencode({
        "filename": img_info["filename"],
        "subfolder": img_info.get("subfolder", ""),
        "type": img_info.get("type", "output"),
    })
    img_bytes = http_get(f"{comfyui_url}/view?{qs}", timeout=30)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(img_bytes)


# ---------------------------------------------------------------------------
# Background removal — strips background to alpha before Hunyuan3D ingestion
# ---------------------------------------------------------------------------

_rembg_session = None


def remove_background(image_path: Path) -> None:
    """Remove background from image_path in-place using rembg (u2net model).

    Overwrites the file with a transparent-background RGBA PNG. The original
    solid-background PNG is what causes Hunyuan3D to reconstruct flat planes.
    No-op if rembg is not installed.
    """
    if not HAS_REMBG:
        return
    global _rembg_session
    if _rembg_session is None:
        _rembg_session = rembg_session("birefnet-general", providers=["CPUExecutionProvider"])
    with open(image_path, "rb") as f:
        raw = f.read()
    result = rembg_remove(raw, session=_rembg_session)
    # Threshold: kill any semi-transparent halo pixels that confuse Hunyuan3D
    from PIL import Image as _Image
    import io as _io
    import numpy as _np
    img = _Image.open(_io.BytesIO(result)).convert("RGBA")
    arr = _np.array(img)
    arr[:, :, 3] = _np.where(arr[:, :, 3] < 10, 0, arr[:, :, 3])  # hard-zero near-transparent
    buf = _io.BytesIO()
    _Image.fromarray(arr).save(buf, format="PNG")
    image_path.write_bytes(buf.getvalue())


# ---------------------------------------------------------------------------
# Hunyuan3D — mesh generation  (REST API at localhost:8081)
# ---------------------------------------------------------------------------

def _poll_hunyuan(uid: str, hunyuan_url: str, timeout: int = 1200) -> bytes:
    """Poll /status/{uid} until completed. Returns raw GLB bytes. Raises on error/timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data = json.loads(http_get(f"{hunyuan_url}/status/{uid}", timeout=15))
            status = data.get("status")
            if status == "completed":
                return base64.b64decode(data["model_base64"])
            if status == "error":
                raise RuntimeError(f"Hunyuan3D error for {uid}: {data.get('message', 'unknown')}")
        except RuntimeError:
            raise
        except Exception:
            pass
        time.sleep(5)
    raise RuntimeError(f"Hunyuan3D task {uid} timed out after {timeout}s")


def generate_mesh(
    image_path: Path,
    glb_path: Path,
    mesh_quality: str,
    seed: int,
    hunyuan_url: str,
) -> dict:
    """Send image to Hunyuan3D API, wait for GLB, write to glb_path. Returns info dict."""
    quality_params = MESH_QUALITY_PARAMS.get(mesh_quality, MESH_QUALITY_PARAMS["draft"])
    image_b64 = base64.b64encode(image_path.read_bytes()).decode()

    payload = {
        "image": image_b64,
        "seed": seed,
        "remove_background": True,
        **quality_params,
    }

    t0 = time.time()
    result = http_post(f"{hunyuan_url}/send", payload, timeout=30)
    uid = result["uid"]

    glb_bytes = _poll_hunyuan(uid, hunyuan_url)
    glb_path.parent.mkdir(parents=True, exist_ok=True)
    glb_path.write_bytes(glb_bytes)

    return {
        "uid": uid,
        "generation_time_seconds": round(time.time() - t0, 1),
    }


# ---------------------------------------------------------------------------
# Mesh sanity checks
# ---------------------------------------------------------------------------

def sanity_check_mesh(glb_path: Path) -> dict:
    checks: dict = {
        "exists": glb_path.exists(),
        "file_size_bytes": 0,
        "size_ok": False,
        "loads_ok": False,
        "vertex_count": None,
        "has_textures": False,
    }
    if not checks["exists"]:
        return checks

    checks["file_size_bytes"] = glb_path.stat().st_size
    checks["size_ok"] = checks["file_size_bytes"] > 102400  # > 100 KB

    try:
        import trimesh
        scene = trimesh.load(str(glb_path), process=False, force="scene")
        checks["loads_ok"] = True
        if hasattr(scene, "geometry") and scene.geometry:
            checks["vertex_count"] = sum(
                len(g.vertices)
                for g in scene.geometry.values()
                if hasattr(g, "vertices")
            )
            checks["has_textures"] = any(
                hasattr(g, "visual") and g.visual is not None
                for g in scene.geometry.values()
            )
        elif hasattr(scene, "vertices"):
            checks["vertex_count"] = len(scene.vertices)
            checks["has_textures"] = hasattr(scene, "visual") and scene.visual is not None
    except Exception:
        pass

    return checks


# ---------------------------------------------------------------------------
# Per-asset metadata JSON  (written beside each .glb)
# ---------------------------------------------------------------------------

def write_asset_metadata(
    asset_id: str,
    manifest_item: dict,
    seed: int,
    image_path: Path,
    glb_path: Path,
    mesh_info: dict,
    sanity: dict,
) -> None:
    meta = {
        "asset_id": asset_id,
        "source_prompt": manifest_item["prompt"],
        "negative_prompt": manifest_item["negative_prompt"],
        "image_path": str(image_path),
        "glb_path": str(glb_path),
        "seed": seed,
        "image_size": manifest_item["image_size"],
        "mesh_quality": manifest_item["mesh_quality"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "generator": {
            "image": IMAGE_GENERATOR,
            "mesh": MESH_GENERATOR,
        },
        "mesh_info": mesh_info,
        "sanity": {
            "file_size_bytes": sanity["file_size_bytes"],
            "vertex_count": sanity["vertex_count"],
            "loads_ok": sanity["loads_ok"],
            "has_textures": sanity["has_textures"],
        },
    }
    glb_path.with_suffix(".json").write_text(json.dumps(meta, indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Blocky Phase 1: Manifest → Image → GLB pipeline"
    )
    parser.add_argument("manifest", help="Path to manifest JSON")
    parser.add_argument("--comfyui-url", default=COMFYUI_URL, dest="comfyui_url",
                        help=f"ComfyUI API URL (default: {COMFYUI_URL})")
    parser.add_argument("--hunyuan-url", default=HUNYUAN_URL, dest="hunyuan_url",
                        help=f"Hunyuan3D API URL (default: {HUNYUAN_URL})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate manifest and show planned work without executing")
    args = parser.parse_args()

    items = load_manifest(args.manifest)
    total = sum(item["count"] for item in items)

    print(f"\n{'='*60}")
    print("BLOCKY — Manifest → Image → GLB Batch Pipeline")
    print(f"  Manifest:     {args.manifest}")
    print(f"  Object types: {len(items)}")
    print(f"  Total assets: {total}")
    print(f"  ComfyUI:      {args.comfyui_url}")
    print(f"  Hunyuan3D:    {args.hunyuan_url}")
    print(f"{'='*60}\n")

    if args.dry_run:
        index = load_index()
        print("DRY RUN — planned work:\n")
        for item in items:
            for v in range(1, item["count"] + 1):
                asset_id = f"{item['id']}_{v:03d}"
                cur = index.get(asset_id, {}).get("status", "pending")
                seed = item["seed"] if item["seed"] is not None else derive_seed(asset_id)
                print(f"  {asset_id:<44}  status={cur:<15}  seed={seed}")
        print(f"\n{total} assets total. Remove --dry-run to execute.")
        return

    # Service health checks
    print("Checking services...")
    check_service_health(args.comfyui_url, "ComfyUI", path="/system_stats")
    check_service_health(args.hunyuan_url, "Hunyuan3D", path="/health")
    print()

    # Ensure output dirs exist
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "images").mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "meshes").mkdir(parents=True, exist_ok=True)

    log_path = LOG_DIR / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    failures: list = []
    done_images = 0
    done_meshes = 0

    all_assets = [(item, v) for item in items for v in range(1, item["count"] + 1)]
    iterator = tqdm(all_assets, unit="asset") if HAS_TQDM else all_assets

    for item, v in iterator:
        asset_id = f"{item['id']}_{v:03d}"
        seed = item["seed"] if item["seed"] is not None else derive_seed(asset_id)

        img_dir = OUTPUT_DIR / "images" / item["category"]
        mesh_dir = OUTPUT_DIR / "meshes" / item["category"]
        image_path = img_dir / f"{asset_id}.png"
        glb_path = mesh_dir / f"{asset_id}.glb"

        if HAS_TQDM:
            iterator.set_description(asset_id[:38])

        index = load_index()
        cur_status = index.get(asset_id, {}).get("status", "pending")

        if cur_status == "mesh_complete":
            done_images += 1
            done_meshes += 1
            if not HAS_TQDM:
                print(f"  ✓ {asset_id} — already complete, skipping")
            continue

        # --- Image generation ---
        if cur_status != "image_complete" or not image_path.exists():
            try:
                if not HAS_TQDM:
                    print(f"  [{asset_id}] image (seed={seed})…")
                generate_image(
                    item["prompt"], image_path, args.comfyui_url,
                    item.get("image_size", 1024), seed,
                )
                remove_background(image_path)
                update_asset_status(asset_id, "image_complete", image_path=str(image_path))
                done_images += 1
                if not HAS_TQDM:
                    print(f"    → {image_path}")
                # Free ComfyUI VRAM so Hunyuan3D has the full GPU for texturing
                free_comfyui_vram(args.comfyui_url)
            except Exception as e:
                failures.append({"id": asset_id, "stage": "image", "error": str(e)})
                update_asset_status(asset_id, "failed", error=f"image: {e}")
                if not HAS_TQDM:
                    print(f"    ✗ IMAGE FAILED: {e}")
                continue
        else:
            done_images += 1
            # Image already done but mesh not started — free VRAM in case ComfyUI still holds it
            free_comfyui_vram(args.comfyui_url)

        # --- Mesh generation ---
        try:
            if not HAS_TQDM:
                print(f"  [{asset_id}] mesh…")
            mesh_info = generate_mesh(
                image_path, glb_path, item.get("mesh_quality", "draft"), seed, args.hunyuan_url,
            )
            sanity = sanity_check_mesh(glb_path)
            if not sanity["exists"] or not sanity["size_ok"]:
                raise RuntimeError(f"Sanity check failed: {sanity}")

            write_asset_metadata(asset_id, item, seed, image_path, glb_path, mesh_info, sanity)
            update_asset_status(
                asset_id, "mesh_complete",
                glb_path=str(glb_path),
                vertex_count=sanity["vertex_count"],
                file_size_bytes=sanity["file_size_bytes"],
            )
            done_meshes += 1
            if not HAS_TQDM:
                sz_kb = sanity["file_size_bytes"] // 1024
                print(f"    → {glb_path} ({sanity['vertex_count']} verts, {sz_kb} KB)")
        except Exception as e:
            failures.append({"id": asset_id, "stage": "mesh", "error": str(e)})
            update_asset_status(asset_id, "failed", error=f"mesh: {e}")
            if not HAS_TQDM:
                print(f"    ✗ MESH FAILED: {e}")

    # Summary
    print(f"\n{'='*60}")
    print("PIPELINE COMPLETE")
    print(f"  Images:   {done_images}/{total}")
    print(f"  Meshes:   {done_meshes}/{total}")
    print(f"  Failures: {len(failures)}")
    print(f"{'='*60}\n")

    if failures:
        fail_log = LOG_DIR / "failures.json"
        fail_log.write_text(json.dumps(failures, indent=2))
        print(f"Failures written to {fail_log}")
        for f in failures:
            print(f"  - {f['id']} [{f['stage']}]: {f['error']}")

    with open(log_path, "a") as f:
        f.write(
            f"{datetime.now(timezone.utc).isoformat()} | "
            f"images={done_images}/{total} meshes={done_meshes}/{total} failures={len(failures)}\n"
        )
    print(f"\nLog: {log_path}")


if __name__ == "__main__":
    main()
