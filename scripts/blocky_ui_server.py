#!/usr/bin/env python3
"""Local Blocky control-panel server.

Serves index.html and a small JSON API for manifest editing, validation, job
launching, and log polling. This is intentionally stdlib-only so the UI works
from the repo venv without adding another web framework.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_DIR = ROOT / "manifest"
OUTPUTS_DIR = ROOT / "outputs"
LOG_DIR = OUTPUTS_DIR / "logs"
INDEX_PATH = OUTPUTS_DIR / "index.jsonl"
ENV_PATH = ROOT / ".env"
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
ASSET_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+_\d{3}$")
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_NEGATIVE_PROMPT = (
    "text, watermark, people, clutter, floor plane, ground plane, base plate, "
    "shadow, multiple objects, cropped, partial view"
)
CANCEL_PATTERNS = [
    "scripts/run_batch.py",
    "scripts/run_pipeline.sh",
    "scripts/decimate_glbs.py",
    "ComfyUI/main.py",
    "api_server.py",
]
CANCEL_PORTS = [8188, 8081]
HUNYUAN_PROCESS: subprocess.Popen[Any] | None = None
ACTIVE_JOB_STATUSES = {"preparing", "running", "stopping"}
VRAM_FREE_THRESHOLD_MIB = 20000


def rel_path(path: Path) -> str:
    return str(path.relative_to(ROOT))


def safe_manifest_path(name: str) -> Path:
    name = unquote(name).strip()
    if not name:
        raise ValueError("Manifest name is required")
    path = (MANIFEST_DIR / name).resolve()
    if MANIFEST_DIR.resolve() not in path.parents or path.suffix != ".json":
        raise ValueError("Manifest must be a JSON file inside manifest/")
    return path


def read_json_body(handler: SimpleHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    return json.loads(raw or "{}")


def load_env() -> dict[str, str]:
    values = dict(os.environ)
    if not ENV_PATH.exists():
        return values
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return values


def normalize_manifest_item(item: dict[str, Any], index: int = 0) -> dict[str, Any]:
    raw_id = str(item.get("id") or f"asset_{index + 1:03d}").strip().lower()
    asset_id = re.sub(r"[^a-z0-9_]+", "_", raw_id).strip("_") or f"asset_{index + 1:03d}"
    category = re.sub(r"[^a-z0-9_]+", "_", str(item.get("category") or "misc").strip().lower()).strip("_") or "misc"
    prompt = str(item.get("prompt") or "").strip()
    if not prompt:
        prompt = (
            f"single {asset_id.replace('_', ' ')}, pure white background, floating in void, "
            "alpha channel around object, game asset 3D model, clean product render, no floor, no shadows"
        )
    negative_prompt = str(item.get("negative_prompt") or DEFAULT_NEGATIVE_PROMPT).strip()
    seed = item.get("seed")
    if seed in ("", "null"):
        seed = None
    if seed is not None:
        seed = int(seed)
    image_size = int(item.get("image_size") or 1024)
    if image_size not in {512, 768, 1024}:
        image_size = 1024
    mesh_quality = str(item.get("mesh_quality") or "draft").strip()
    if mesh_quality not in {"draft", "standard"}:
        mesh_quality = "draft"
    return {
        "id": asset_id,
        "category": category,
        "prompt": prompt[:500],
        "negative_prompt": negative_prompt[:500],
        "count": max(1, int(item.get("count") or 1)),
        "seed": seed,
        "image_size": image_size,
        "mesh_quality": mesh_quality,
        "target_height_m": max(0.01, float(item.get("target_height_m") or 1.0)),
    }


def normalize_manifest_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise ValueError("Manifest entries must be a list")
    normalized = [normalize_manifest_item(item, idx) for idx, item in enumerate(items) if isinstance(item, dict)]
    seen: dict[str, int] = {}
    for item in normalized:
        base_id = item["id"]
        seen[base_id] = seen.get(base_id, 0) + 1
        if seen[base_id] > 1:
            item["id"] = f"{base_id}_{seen[base_id]:02d}"
    return normalized


def generate_manifest_entries_with_openai(payload: dict[str, Any]) -> list[dict[str, Any]]:
    env = load_env()
    api_key = env.get("OPENAI_API_KEY") or env.get("OPENAI_API")
    if not api_key:
        raise ValueError("OPENAI_API_KEY or OPENAI_API is missing from .env")

    model = env.get("OPENAI_MODEL", "gpt-4o-mini")
    model_types = str(payload.get("model_types") or "").strip()
    category = str(payload.get("category") or "interior").strip()
    style = str(payload.get("style") or "").strip()
    entries_per_type = max(1, min(12, int(payload.get("entries_per_type") or 1)))
    image_count = max(1, min(12, int(payload.get("image_count") or 4)))
    image_size = int(payload.get("image_size") or 1024)
    mesh_quality = str(payload.get("mesh_quality") or "draft")
    target_height_m = max(0.01, float(payload.get("target_height_m") or 1.0))

    if not model_types:
        raise ValueError("Describe at least one model type for the AI generator")

    system_prompt = (
        "You create production-ready JSON manifest entries for an image-to-3D game asset pipeline. "
        "Return only valid JSON with an entries array. Each entry must include id, category, prompt, "
        "negative_prompt, count, seed, image_size, mesh_quality, and target_height_m. Prompts must describe "
        "one isolated object or modular kit piece, floating on a pure white background, suitable for image-to-3D. "
        "Use snake_case ids without a trailing variant number. Keep prompts and negative prompts under 500 characters."
    )
    user_prompt = {
        "model_types": model_types,
        "category": category,
        "style": style,
        "entries_per_type": entries_per_type,
        "count_per_entry": image_count,
        "image_size": image_size,
        "mesh_quality": mesh_quality,
        "target_height_m_default": target_height_m,
    }
    request_payload = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_prompt)},
        ],
        "temperature": 0.7,
    }
    data = json.dumps(request_payload).encode("utf-8")
    req = urllib.request.Request(
        OPENAI_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"OpenAI request failed: {detail[:500]}") from exc

    content = raw["choices"][0]["message"]["content"]
    generated = json.loads(content)
    entries = generated.get("entries", generated if isinstance(generated, list) else [])
    normalized = normalize_manifest_items(entries)
    if not normalized:
        raise ValueError("AI returned no manifest entries")
    return normalized


def load_index() -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    if not INDEX_PATH.exists():
        return entries
    for line in INDEX_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            entries[entry["asset_id"]] = entry
        except (json.JSONDecodeError, KeyError):
            continue
    return entries


def manifest_assets(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    assets = []
    for item in items:
        count = int(item.get("count", 0) or 0)
        for variant in range(1, count + 1):
            asset_id = f"{item.get('id')}_{variant:03d}"
            assets.append(
                {
                    "asset_id": asset_id,
                    "category": item.get("category"),
                    "image_path": f"outputs/images/{item.get('category')}/{asset_id}.png",
                    "mesh_path": f"outputs/meshes/{item.get('category')}/{asset_id}.glb",
                    "game_path": f"in_game_assets/{item.get('category')}/{asset_id}.glb",
                }
            )
    return assets


def read_manifest_summary(path: Path, index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    try:
        items = json.loads(path.read_text())
        assets = manifest_assets(items)
        enriched_assets = []
        for asset in assets:
            tracked = index.get(asset["asset_id"], {})
            enriched_assets.append(
                {
                    **asset,
                    "status": tracked.get("status", "pending"),
                    "vertex_count": tracked.get("vertex_count"),
                    "file_size_bytes": tracked.get("file_size_bytes"),
                    "error": tracked.get("error"),
                    "image_exists": (ROOT / asset["image_path"]).exists(),
                    "mesh_exists": (ROOT / asset["mesh_path"]).exists(),
                    "game_exists": (ROOT / asset["game_path"]).exists(),
                }
            )
        statuses = [asset["status"] for asset in enriched_assets]
        complete = statuses.count("mesh_complete")
        failed = statuses.count("failed")
        image_complete = statuses.count("image_complete")
        return {
            "name": path.name,
            "path": rel_path(path),
            "valid_json": True,
            "object_count": len(items) if isinstance(items, list) else 0,
            "asset_count": len(assets),
            "mesh_complete": complete,
            "image_complete": image_complete,
            "failed": failed,
            "pending": max(0, len(assets) - complete - image_complete - failed),
            "assets": enriched_assets,
            "updated_at": path.stat().st_mtime,
        }
    except Exception as exc:
        return {
            "name": path.name,
            "path": rel_path(path),
            "valid_json": False,
            "error": str(exc),
            "object_count": 0,
            "asset_count": 0,
            "mesh_complete": 0,
            "image_complete": 0,
            "failed": 0,
            "pending": 0,
            "updated_at": path.stat().st_mtime,
        }


def service_status(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def gpu_memory() -> dict[str, int | None]:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total,memory.free,memory.used", "--format=csv,noheader,nounits"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if result.returncode != 0:
            return {"total_mib": None, "free_mib": None, "used_mib": None}
        first_line = result.stdout.strip().splitlines()[0]
        total, free, used = [int(part.strip()) for part in first_line.split(",")[:3]]
        return {"total_mib": total, "free_mib": free, "used_mib": used}
    except Exception:
        return {"total_mib": None, "free_mib": None, "used_mib": None}


def wait_for_service(url: str, timeout: float = 180.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if service_status(url, timeout=2.0):
            return True
        time.sleep(2)
    return False


def wait_for_service_down(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not service_status(url, timeout=1.0):
            return True
        time.sleep(1)
    return not service_status(url, timeout=1.0)


def wait_for_vram(threshold_mib: int = VRAM_FREE_THRESHOLD_MIB, timeout: float = 90.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    last = gpu_memory()
    while time.time() < deadline:
        last = gpu_memory()
        free_mib = last.get("free_mib")
        if free_mib is None or free_mib >= threshold_mib:
            return {"ok": True, "timed_out": False, **last}
        time.sleep(3)
    return {"ok": False, "timed_out": True, **last}


def current_jobs() -> list[dict[str, Any]]:
    with JOBS_LOCK:
        for job in JOBS.values():
            proc = job.get("process")
            if proc and job["status"] == "running":
                code = proc.poll()
                if code is not None:
                    job["status"] = "succeeded" if code == 0 else "failed"
                    job["returncode"] = code
                    job["ended_at"] = time.time()
                    if job.get("mode") == "meshes":
                        _set_step(
                            job,
                            "mesh_batch",
                            "done" if code == 0 else "failed",
                            "Mesh batch complete" if code == 0 else f"Mesh batch failed with exit code {code}",
                        )
        return [
            _public_job(job)
            for job in sorted(JOBS.values(), key=lambda item: item["started_at"], reverse=True)
        ]


def state_payload() -> dict[str, Any]:
    index = load_index()
    manifests = [read_manifest_summary(path, index) for path in sorted(MANIFEST_DIR.glob("*.json"))]
    return {
        "manifests": manifests,
        "index": list(index.values()),
        "jobs": current_jobs(),
        "services": {
            "comfyui": service_status("http://localhost:8188/system_stats"),
            "hunyuan": service_status("http://localhost:8081/health"),
        },
        "gpu": gpu_memory(),
    }


def validate_asset_ids(asset_ids: Any) -> list[str]:
    if asset_ids is None:
        return []
    if not isinstance(asset_ids, list):
        raise ValueError("asset_ids must be a list")
    cleaned: list[str] = []
    for asset_id in asset_ids:
        if not isinstance(asset_id, str) or not ASSET_ID_RE.match(asset_id):
            raise ValueError(f"Invalid asset id: {asset_id!r}")
        cleaned.append(asset_id)
    return sorted(set(cleaned))


def run_command_for(mode: str, manifest_name: str, asset_ids: list[str] | None = None) -> list[str]:
    manifest = f"manifest/{manifest_name}"
    if mode in ("pipeline", "images"):
        return [".venv/bin/python", "scripts/run_batch.py", manifest, "--phase", "images"]
    if mode == "meshes":
        command = [
            ".venv/bin/python",
            "scripts/run_batch.py",
            manifest,
            "--phase",
            "meshes",
            "--hunyuan-url",
            "http://localhost:8081",
        ]
        for asset_id in asset_ids or []:
            command.extend(["--asset-id", asset_id])
        return command
    if mode == "validate":
        return [".venv/bin/python", "scripts/validate_manifest.py", manifest]
    if mode == "dry-run":
        return [".venv/bin/python", "scripts/run_batch.py", manifest, "--dry-run"]
    if mode == "decimate":
        return ["bash", "-lc", f"find outputs/meshes -type f -name '*.glb' -print0 | xargs -0 -r /usr/local/bin/blender --background --python scripts/decimate_glbs.py -- --manifest {manifest} --preset game_ready --output-dir in_game_assets/manual"]
    raise ValueError(f"Unknown mode: {mode}")


def stop_comfyui() -> dict[str, Any]:
    was_running = service_status("http://localhost:8188/system_stats", timeout=1.0)
    killed_process = _pkill_pattern("ComfyUI/main.py")
    killed_port = _kill_port(8188)
    stopped = wait_for_service_down("http://localhost:8188/system_stats", timeout=30.0)
    return {
        "was_running": was_running,
        "killed_process": killed_process,
        "killed_port": killed_port,
        "stopped": stopped,
    }


def ensure_hunyuan_service() -> dict[str, Any]:
    global HUNYUAN_PROCESS
    if service_status("http://localhost:8081/health", timeout=2.0):
        return {"ok": True, "started": False, "log_path": "outputs/logs/hunyuan_api.log"}

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "hunyuan_api.log"
    log_file = open(log_path, "a", buffering=1)
    log_file.write("Starting Hunyuan3D API from Blocky UI...\n")
    HUNYUAN_PROCESS = subprocess.Popen(
        ["bash", "scripts/start_hunyuan_api.sh"],
        cwd=ROOT,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    if not wait_for_service("http://localhost:8081/health", timeout=180.0):
        raise ValueError(f"Hunyuan did not become ready. See {rel_path(log_path)}")
    return {"ok": True, "started": True, "log_path": rel_path(log_path), "pid": HUNYUAN_PROCESS.pid}


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in job.items() if k not in {"process", "log_file"}}


def _set_step(job: dict[str, Any], key: str, status: str, detail: str) -> None:
    now = time.time()
    steps = job.setdefault("steps", [])
    for step in steps:
        if step["key"] == key:
            step.update({"status": status, "detail": detail, "updated_at": now})
            return
    steps.append({"key": key, "label": key.replace("_", " ").title(), "status": status, "detail": detail, "updated_at": now})


def _job_log(job: dict[str, Any], message: str) -> None:
    log_file = job.get("log_file")
    if log_file:
        log_file.write(f"{message}\n")


def _mesh_prep_cancelled(job_id: str) -> bool:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return True
        if job["status"] != "preparing":
            _job_log(job, "Mesh preparation cancelled before launch.")
            return True
        return False


def _launch_job_process(job: dict[str, Any]) -> None:
    command = job["command"]
    _job_log(job, f"$ {' '.join(command)}\n")
    proc = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=job["log_file"],
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    job["process"] = proc
    job["pid"] = proc.pid
    job["status"] = "running"


def _mesh_job_worker(job_id: str) -> None:
    try:
        with JOBS_LOCK:
            job = JOBS[job_id]
            _set_step(job, "stop_comfyui", "running", "Stopping ComfyUI before mesh generation")
            _job_log(job, "Preparing mesh job: stopping ComfyUI...")
        stop_result = stop_comfyui()
        if _mesh_prep_cancelled(job_id):
            return
        with JOBS_LOCK:
            job = JOBS[job_id]
            detail = "ComfyUI stopped" if stop_result["stopped"] else "ComfyUI stop timed out; continuing"
            if not stop_result["was_running"]:
                detail = "ComfyUI was not running"
            _set_step(job, "stop_comfyui", "done", detail)
            _set_step(job, "wait_vram", "running", f"Waiting for >{VRAM_FREE_THRESHOLD_MIB} MiB free VRAM")
            _job_log(job, f"{detail}. Waiting for VRAM...")

        vram = wait_for_vram()
        if _mesh_prep_cancelled(job_id):
            return
        free_mib = vram.get("free_mib")
        with JOBS_LOCK:
            job = JOBS[job_id]
            if vram.get("timed_out"):
                detail = f"Timed out at {free_mib or '?'} MiB free; continuing"
            elif free_mib is None:
                detail = "VRAM could not be read; continuing"
            else:
                detail = f"{free_mib} MiB free"
            _set_step(job, "wait_vram", "done", detail)
            hunyuan_detail = "Starting or reusing Hunyuan3D"
            if stop_result["was_running"] and service_status("http://localhost:8081/health", timeout=1.0):
                hunyuan_detail = "Restarting Hunyuan3D after ComfyUI handoff"
                _job_log(job, "ComfyUI was running; restarting Hunyuan3D for a clean CUDA context.")
                _pkill_pattern("api_server.py")
                _kill_port(8081)
                wait_for_service_down("http://localhost:8081/health", timeout=30.0)
            _set_step(job, "start_hunyuan", "running", hunyuan_detail)
            _job_log(job, f"VRAM check: {detail}. Ensuring Hunyuan3D...")

        hunyuan = ensure_hunyuan_service()
        if _mesh_prep_cancelled(job_id):
            return
        with JOBS_LOCK:
            job = JOBS[job_id]
            detail = "Hunyuan3D started" if hunyuan.get("started") else "Hunyuan3D already running"
            _set_step(job, "start_hunyuan", "done", detail)
            _set_step(job, "mesh_batch", "running", "Running selected assets through Hunyuan3D")
            _job_log(job, f"{detail}. Launching mesh batch.")
            _launch_job_process(job)
    except Exception as exc:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job:
                _set_step(job, "mesh_prep", "failed", str(exc))
                _job_log(job, f"Mesh preparation failed: {exc}")
                job["status"] = "failed"
                job["returncode"] = None
                job["ended_at"] = time.time()


def start_job(manifest_name: str, mode: str, asset_ids: Any = None) -> dict[str, Any]:
    safe_manifest_path(manifest_name)
    selected_asset_ids = validate_asset_ids(asset_ids)
    if selected_asset_ids and mode != "meshes":
        raise ValueError("Selected asset ids can only be used with mesh jobs")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with JOBS_LOCK:
        for running in JOBS.values():
            proc = running.get("process")
            if running["status"] in ACTIVE_JOB_STATUSES and (not proc or proc.poll() is None):
                raise ValueError(f"Job {running['id']} is already running. Cancel it before starting another job.")

        job_id = time.strftime("%Y%m%d_%H%M%S") + f"_{len(JOBS) + 1}"
        log_path = LOG_DIR / f"ui_job_{job_id}.log"
        command = run_command_for(mode, manifest_name, selected_asset_ids)
        log_file = open(log_path, "w", buffering=1)
        job = {
            "id": job_id,
            "manifest": manifest_name,
            "mode": mode,
            "command": command,
            "log_path": rel_path(log_path),
            "status": "preparing" if mode == "meshes" else "running",
            "returncode": None,
            "started_at": time.time(),
            "ended_at": None,
            "pid": None,
            "asset_ids": selected_asset_ids,
            "process": None,
            "steps": [],
            "log_file": log_file,
        }
        if mode == "meshes":
            _set_step(job, "stop_comfyui", "pending", "Queued")
            _set_step(job, "wait_vram", "pending", f"Need >{VRAM_FREE_THRESHOLD_MIB} MiB free")
            _set_step(job, "start_hunyuan", "pending", "Queued")
            _set_step(job, "mesh_batch", "pending", f"{len(selected_asset_ids)} selected asset(s)")
            _job_log(job, "Mesh job queued. The UI will stop ComfyUI before starting Hunyuan3D.")
        else:
            _launch_job_process(job)
        JOBS[job_id] = job
    if mode == "meshes":
        threading.Thread(target=_mesh_job_worker, args=(job_id,), daemon=True).start()
    return _public_job(job)


def stop_job(job_id: str) -> dict[str, Any]:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise ValueError("Job not found")
        proc = job.get("process")
        if proc and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            job["status"] = "stopping"
        elif job["status"] == "preparing":
            _mark_job_cancelled(job)
        return _public_job(job)


def _mark_job_cancelled(job: dict[str, Any]) -> None:
    job["status"] = "cancelled"
    job["returncode"] = None
    job["ended_at"] = time.time()


def _terminate_process_group(proc: subprocess.Popen[Any]) -> bool:
    if proc.poll() is not None:
        return False
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False


def _pkill_pattern(pattern: str) -> bool:
    result = subprocess.run(
        ["pkill", "-TERM", "-f", pattern],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _kill_port(port: int) -> bool:
    result = subprocess.run(
        ["fuser", "-k", f"{port}/tcp"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def cancel_all_processes() -> dict[str, Any]:
    cancelled_jobs: list[str] = []
    killed_patterns: list[str] = []
    killed_ports: list[int] = []

    with JOBS_LOCK:
        for job in JOBS.values():
            proc = job.get("process")
            if proc and job["status"] in {"running", "stopping"}:
                if _terminate_process_group(proc):
                    cancelled_jobs.append(job["id"])
                _mark_job_cancelled(job)

    time.sleep(0.5)

    for pattern in CANCEL_PATTERNS:
        if _pkill_pattern(pattern):
            killed_patterns.append(pattern)

    time.sleep(0.5)

    for port in CANCEL_PORTS:
        if _kill_port(port):
            killed_ports.append(port)

    return {
        "ok": True,
        "cancelled_jobs": cancelled_jobs,
        "killed_patterns": killed_patterns,
        "killed_ports": killed_ports,
    }


class BlockyHandler(SimpleHTTPRequestHandler):
    server_version = "BlockyUI/1.0"

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def translate_path(self, path: str) -> str:
        parsed = urlparse(path)
        clean_path = parsed.path
        if clean_path == "/":
            clean_path = "/index.html"
        return str((ROOT / clean_path.lstrip("/")).resolve())

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message: str, status: int = 400) -> None:
        self.send_json({"error": message}, status=status)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            self.send_json(state_payload())
            return
        if parsed.path == "/api/manifest":
            query = parse_qs(parsed.query)
            try:
                path = safe_manifest_path(query.get("name", [""])[0])
                content = path.read_text()
                self.send_json({"name": path.name, "content": content, "entries": json.loads(content)})
            except Exception as exc:
                self.send_error_json(str(exc))
            return
        if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/log"):
            job_id = parsed.path.split("/")[3]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                self.send_error_json("Job not found", status=HTTPStatus.NOT_FOUND)
                return
            log_path = ROOT / job["log_path"]
            tail = int(parse_qs(parsed.query).get("tail", ["40000"])[0])
            data = log_path.read_text(errors="replace") if log_path.exists() else ""
            self.send_json({"job_id": job_id, "log": data[-tail:]})
            return
        return super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = read_json_body(self)
            if parsed.path == "/api/manifest":
                path = safe_manifest_path(payload.get("name", ""))
                if "entries" in payload:
                    entries = normalize_manifest_items(payload.get("entries"))
                    content = json.dumps(entries, indent=2)
                else:
                    content = payload.get("content", "")
                    entries = normalize_manifest_items(json.loads(content))
                    content = json.dumps(entries, indent=2)
                path.write_text(content.rstrip() + "\n")
                self.send_json({"ok": True, "name": path.name})
                return
            if parsed.path == "/api/ai/manifest-entries":
                entries = generate_manifest_entries_with_openai(payload)
                self.send_json({"entries": entries})
                return
            if parsed.path == "/api/jobs":
                job = start_job(
                    payload.get("manifest", ""),
                    payload.get("mode", "images"),
                    payload.get("asset_ids"),
                )
                self.send_json(job)
                return
            if parsed.path == "/api/cancel":
                self.send_json(cancel_all_processes())
                return
            if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/stop"):
                job_id = parsed.path.split("/")[3]
                self.send_json(stop_job(job_id))
                return
            self.send_error_json("Unknown endpoint", status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_error_json(str(exc))


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the local Blocky UI server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    MANIFEST_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), BlockyHandler)
    print(f"Blocky UI: http://{args.host}:{args.port}/index.html")
    server.serve_forever()


if __name__ == "__main__":
    main()
