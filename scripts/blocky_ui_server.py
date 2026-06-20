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
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


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
        return [
            {k: v for k, v in job.items() if k != "process"}
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
    }


def run_command_for(mode: str, manifest_name: str) -> list[str]:
    manifest = f"manifest/{manifest_name}"
    if mode == "pipeline":
        return ["bash", "scripts/run_pipeline.sh", manifest]
    if mode == "images":
        return [".venv/bin/python", "scripts/run_batch.py", manifest, "--phase", "images"]
    if mode == "meshes":
        return [
            ".venv/bin/python",
            "scripts/run_batch.py",
            manifest,
            "--phase",
            "meshes",
            "--hunyuan-url",
            "http://localhost:8081",
        ]
    if mode == "validate":
        return [".venv/bin/python", "scripts/validate_manifest.py", manifest]
    if mode == "dry-run":
        return [".venv/bin/python", "scripts/run_batch.py", manifest, "--dry-run"]
    if mode == "decimate":
        return ["bash", "-lc", f"find outputs/meshes -type f -name '*.glb' -print0 | xargs -0 -r /usr/local/bin/blender --background --python scripts/decimate_glbs.py -- --manifest {manifest} --preset game_ready --output-dir in_game_assets/manual"]
    raise ValueError(f"Unknown mode: {mode}")


def start_job(manifest_name: str, mode: str) -> dict[str, Any]:
    safe_manifest_path(manifest_name)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    job_id = time.strftime("%Y%m%d_%H%M%S") + f"_{len(JOBS) + 1}"
    log_path = LOG_DIR / f"ui_job_{job_id}.log"
    command = run_command_for(mode, manifest_name)
    log_file = open(log_path, "w", buffering=1)
    log_file.write(f"$ {' '.join(command)}\n\n")
    proc = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    job = {
        "id": job_id,
        "manifest": manifest_name,
        "mode": mode,
        "command": command,
        "log_path": rel_path(log_path),
        "status": "running",
        "returncode": None,
        "started_at": time.time(),
        "ended_at": None,
        "pid": proc.pid,
        "process": proc,
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    return {k: v for k, v in job.items() if k != "process"}


def stop_job(job_id: str) -> dict[str, Any]:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise ValueError("Job not found")
        proc = job.get("process")
        if proc and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            job["status"] = "stopping"
        return {k: v for k, v in job.items() if k != "process"}


class BlockyHandler(SimpleHTTPRequestHandler):
    server_version = "BlockyUI/1.0"

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
                self.send_json({"name": path.name, "content": path.read_text()})
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
                content = payload.get("content", "")
                json.loads(content)
                path.write_text(content.rstrip() + "\n")
                self.send_json({"ok": True, "name": path.name})
                return
            if parsed.path == "/api/jobs":
                job = start_job(payload.get("manifest", ""), payload.get("mode", "pipeline"))
                self.send_json(job)
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
