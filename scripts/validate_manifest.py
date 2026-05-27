#!/usr/bin/env python3
"""Validate a Blocky manifest JSON file.

Usage:
    python scripts/validate_manifest.py manifest/city_objects.json
"""
import json
import sys

REQUIRED_FIELDS = {"id", "category", "prompt", "negative_prompt", "count", "seed", "image_size", "mesh_quality", "target_height_m"}
VALID_IMAGE_SIZES = {512, 768, 1024}
VALID_MESH_QUALITIES = {"draft", "standard"}


def validate_manifest(path: str) -> list:
    with open(path) as f:
        data = json.load(f)

    if not isinstance(data, list) or len(data) == 0:
        raise ValueError("Manifest must be a non-empty JSON array")

    errors = []

    for i, item in enumerate(data):
        entry_id = item.get("id", f"<entry {i}>")

        missing = REQUIRED_FIELDS - set(item.keys())
        if missing:
            errors.append(f"[{entry_id}] Missing required fields: {sorted(missing)}")
            continue  # cannot validate further without all fields

        if not isinstance(item["count"], int) or not (1 <= item["count"] <= 20):
            errors.append(f"[{entry_id}] 'count' must be an integer 1–20, got: {item['count']!r}")

        if not item["prompt"] or len(item["prompt"]) > 500:
            errors.append(f"[{entry_id}] 'prompt' must be non-empty and under 500 chars (got {len(item['prompt'])})")

        if not item["negative_prompt"] or len(item["negative_prompt"]) > 500:
            errors.append(f"[{entry_id}] 'negative_prompt' must be non-empty and under 500 chars (got {len(item['negative_prompt'])})")

        if item["image_size"] not in VALID_IMAGE_SIZES:
            errors.append(f"[{entry_id}] 'image_size' must be one of {sorted(VALID_IMAGE_SIZES)}, got: {item['image_size']!r}")

        if item["mesh_quality"] not in VALID_MESH_QUALITIES:
            errors.append(f"[{entry_id}] 'mesh_quality' must be one of {sorted(VALID_MESH_QUALITIES)}, got: {item['mesh_quality']!r}")

        if item["seed"] is not None and not isinstance(item["seed"], int):
            errors.append(f"[{entry_id}] 'seed' must be null or an integer, got: {item['seed']!r}")

        if not isinstance(item["target_height_m"], (int, float)) or item["target_height_m"] <= 0:
            errors.append(
                f"[{entry_id}] 'target_height_m' must be a positive number in meters, got: {item['target_height_m']!r}"
            )

    # Duplicate ID check
    ids = [item.get("id") for item in data if "id" in item]
    seen: set = set()
    for item_id in ids:
        if item_id in seen:
            errors.append(f"Duplicate ID: '{item_id}'")
        seen.add(item_id)

    if errors:
        raise ValueError("Manifest validation failed:\n" + "\n".join(f"  - {e}" for e in errors))

    return data


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/validate_manifest.py <manifest.json>")
        sys.exit(1)

    try:
        data = validate_manifest(sys.argv[1])
        total = sum(item["count"] for item in data)
        print(f"✓ Manifest valid: {len(data)} object types, {total} total assets")
    except (ValueError, json.JSONDecodeError) as e:
        print(f"✗ {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
