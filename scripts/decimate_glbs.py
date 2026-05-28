#!/usr/bin/env python3
"""Batch decimate generated GLBs with Blender.

This script must be run via Blender because it depends on ``bpy``:

    blender --background --python scripts/decimate_glbs.py -- ...

LM Studio is not used by this script.

Output: one clean triangular mesh per input asset (no LOD bundling).
"""

import argparse
import bmesh
import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector


DEFAULT_OUTPUT_DIR = Path("in_game_assets/street_furniture")

# Default cleanup + decimation settings for tessellation base meshes.
# Tessellation_base uses ratio 0.65 (keeps ~65% of original faces) so the
# game engine's dynamic tessellation shader has enough edge flow to work with.
DEFAULT_DECIMATE_RATIO = 0.65
DEFAULT_MERGE_THRESHOLD = 0.001
DEFAULT_FILL_HOLES_SIDES = 4
DEFAULT_SMOOTH_ANGLE_DEG = 45.0
DEFAULT_UV_QUALITY_THRESHOLD = 0.7
DEFAULT_SMART_UV_ANGLE_DEG = 30.0
DEFAULT_UV_ISLAND_MARGIN = 0.01
UV_REWRAP_LAYER_NAME = "_UVReunwrap"

DECIMATION_PRESETS = {
    # Tessellation base: high-density mesh with clean topology.
    # The game engine shader will subdivide this further at runtime.
    # Keep ratio >= 0.5 so edge flow and silhouette survive decimation.
    "tessellation_base": {
        "ratio": 0.65,
        "merge_threshold": DEFAULT_MERGE_THRESHOLD,
        "fill_holes_sides": DEFAULT_FILL_HOLES_SIDES,
        "smooth_angle_deg": DEFAULT_SMOOTH_ANGLE_DEG,
    },
    # Game-ready: moderate decimation for static rendering (no tessellation).
    "game_ready": {
        "ratio": 0.28,
        "merge_threshold": DEFAULT_MERGE_THRESHOLD,
        "fill_holes_sides": DEFAULT_FILL_HOLES_SIDES,
        "smooth_angle_deg": DEFAULT_SMOOTH_ANGLE_DEG,
    },
}


def build_settings(args: argparse.Namespace) -> dict:
    settings = dict(DECIMATION_PRESETS[args.preset])
    if args.ratio is not None:
        settings["ratio"] = args.ratio
    if args.merge_threshold is not None:
        settings["merge_threshold"] = args.merge_threshold
    if args.fill_holes_sides is not None:
        settings["fill_holes_sides"] = args.fill_holes_sides
    if args.smooth_angle_deg is not None:
        settings["smooth_angle_deg"] = args.smooth_angle_deg
    return settings


def load_manifest_heights(manifest_path: str | None) -> dict[str, float]:
    if not manifest_path:
        return {}
    with open(manifest_path) as f:
        data = json.load(f)
    return {item["id"]: float(item["target_height_m"]) for item in data}


def parse_args() -> argparse.Namespace:
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = []

    parser = argparse.ArgumentParser(description="Batch decimate GLB meshes in Blender")
    parser.add_argument("inputs", nargs="+", help="Input GLB files")
    parser.add_argument("--manifest", default=None, help="Optional manifest JSON used to look up target_height_m per asset")
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        dest="output_dir",
        help=f"Output directory for decimated GLBs (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--preset",
        choices=sorted(DECIMATION_PRESETS.keys()),
        default="tessellation_base",
        help="Named decimation preset (default: tessellation_base for dynamic tessellation shaders)",
    )
    parser.add_argument("--ratio", type=float, default=None, help="Decimate ratio (1.0 = no decimation)")
    parser.add_argument("--merge-threshold", type=float, default=None, dest="merge_threshold")
    parser.add_argument("--fill-holes-sides", type=int, default=None, dest="fill_holes_sides")
    parser.add_argument("--smooth-angle-deg", type=float, default=None, dest="smooth_angle_deg")
    parser.add_argument(
        "--uv-mode",
        choices=("preserve", "auto", "force_unwrap"),
        default="auto",
        dest="uv_mode",
        help="UV handling mode: preserve existing UVs, auto-detect and fix, or always unwrap",
    )
    return parser.parse_args(argv)


def reset_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for datablocks in (bpy.data.meshes, bpy.data.materials, bpy.data.images, bpy.data.cameras, bpy.data.lights):
        for datablock in list(datablocks):
            if datablock.users == 0:
                datablocks.remove(datablock)


def imported_mesh_objects(glb_path: Path):
    before = {obj.name for obj in bpy.data.objects}
    bpy.ops.import_scene.gltf(filepath=str(glb_path))
    imported_objects = [obj for obj in bpy.data.objects if obj.name not in before]
    mesh_objects = [obj for obj in imported_objects if obj.type == "MESH"]
    if not mesh_objects:
        imported_objects = list(bpy.context.scene.objects)
        mesh_objects = [obj for obj in imported_objects if obj.type == "MESH"]
    return imported_objects, mesh_objects


def ensure_object_mode() -> None:
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")


def bounds_for_mesh_objects(mesh_objects: list[bpy.types.Object]) -> tuple[float, float]:
    min_z = None
    max_z = None
    for obj in mesh_objects:
        for corner in obj.bound_box:
            world_corner = obj.matrix_world @ Vector(corner)
            min_z = world_corner.z if min_z is None else min(min_z, world_corner.z)
            max_z = world_corner.z if max_z is None else max(max_z, world_corner.z)
    if min_z is None or max_z is None:
        raise RuntimeError("Unable to measure imported mesh bounds")
    return min_z, max_z


def root_objects(imported_objects: list[bpy.types.Object]) -> list[bpy.types.Object]:
    imported_names = {obj.name for obj in imported_objects}
    return [obj for obj in imported_objects if obj.parent is None or obj.parent.name not in imported_names]


def scale_and_ground_objects(imported_objects: list[bpy.types.Object], mesh_objects: list[bpy.types.Object], target_height_m: float) -> tuple[float, float, float]:
    min_z, max_z = bounds_for_mesh_objects(mesh_objects)
    current_height = max_z - min_z
    if current_height <= 0:
        raise RuntimeError("Imported mesh height is zero; cannot scale to target height")

    scale_factor = target_height_m / current_height
    for obj in root_objects(imported_objects):
        obj.location = obj.location * scale_factor
        obj.scale = tuple(component * scale_factor for component in obj.scale)

    bpy.context.view_layer.update()
    min_z, max_z = bounds_for_mesh_objects(mesh_objects)
    z_offset = -min_z
    for obj in root_objects(imported_objects):
        obj.location.z += z_offset

    bpy.context.view_layer.update()
    _, final_max_z = bounds_for_mesh_objects(mesh_objects)
    return current_height, scale_factor, final_max_z


def uv_layer_name_set(obj: bpy.types.Object) -> set[str]:
    if obj.type != "MESH":
        return set()
    return {layer.name for layer in obj.data.uv_layers}


def uv_sample_hash(layer: bpy.types.MeshUVLoopLayer) -> tuple[float, ...]:
    if not layer.data:
        return ()

    sample_size = min(64, len(layer.data))
    step = max(1, len(layer.data) // sample_size)
    sample: list[float] = []
    for index in range(0, len(layer.data), step):
        uv = layer.data[index].uv
        sample.extend((round(float(uv.x), 5), round(float(uv.y), 5)))
        if len(sample) >= sample_size * 2:
            break
    return tuple(sample)


def uv_map_quality(obj: bpy.types.Object) -> dict:
    if obj.type != "MESH":
        return {
            "has_uv": False,
            "uv_count": 0,
            "quality_score": 0.0,
            "issues": [f"{obj.name}: object is not a mesh"],
        }

    mesh = obj.data
    issues: list[str] = []
    layer_scores: list[float] = []

    if not mesh.uv_layers:
        issues.append(f"{obj.name}: missing UV layers")
        return {
            "has_uv": False,
            "uv_count": 0,
            "quality_score": 0.0,
            "issues": issues,
        }

    for layer in mesh.uv_layers:
        layer_score = 1.0
        if not layer.data:
            issues.append(f"{obj.name}:{layer.name}: empty UV data")
            layer_scores.append(0.0)
            continue
        if len(layer.data) != len(mesh.loops):
            issues.append(
                f"{obj.name}:{layer.name}: UV loop count {len(layer.data)} does not match mesh loop count {len(mesh.loops)}"
            )
            layer_score -= 0.5

        min_u = math.inf
        min_v = math.inf
        max_u = -math.inf
        max_v = -math.inf
        invalid_values = 0

        for uv_loop in layer.data:
            uv = uv_loop.uv
            if not (math.isfinite(float(uv.x)) and math.isfinite(float(uv.y))):
                invalid_values += 1
                continue
            min_u = min(min_u, float(uv.x))
            min_v = min(min_v, float(uv.y))
            max_u = max(max_u, float(uv.x))
            max_v = max(max_v, float(uv.y))

        if invalid_values:
            issues.append(f"{obj.name}:{layer.name}: {invalid_values} UV values are non-finite")
            layer_score -= 0.5

        if min_u is math.inf or min_v is math.inf:
            issues.append(f"{obj.name}:{layer.name}: could not evaluate UV bounds")
            layer_scores.append(max(0.0, layer_score - 0.5))
            continue

        width = max_u - min_u
        height = max_v - min_v
        if width <= 1e-6 or height <= 1e-6:
            issues.append(f"{obj.name}:{layer.name}: UV bounds are collapsed")
            layer_score -= 0.7
        else:
            aspect_ratio = max(width, height) / max(min(width, height), 1e-6)
            if aspect_ratio > 250.0:
                issues.append(f"{obj.name}:{layer.name}: extreme UV bounds ratio {aspect_ratio:.1f}")
                layer_score -= 0.35

        if max(abs(min_u), abs(max_u), abs(min_v), abs(max_v)) > 10000.0:
            issues.append(f"{obj.name}:{layer.name}: UV bounds are extremely large")
            layer_score -= 0.25

        layer_scores.append(max(0.0, min(1.0, layer_score)))

    quality_score = sum(layer_scores) / len(layer_scores) if layer_scores else 0.0
    return {
        "has_uv": bool(mesh.uv_layers),
        "uv_count": len(mesh.uv_layers),
        "quality_score": round(quality_score, 3),
        "issues": issues,
    }


def snapshot_uv_state(obj: bpy.types.Object) -> dict:
    if obj.type != "MESH":
        return {
            "has_uv": False,
            "layer_names": [],
            "layer_hashes": {},
            "uv_quality": uv_map_quality(obj),
        }

    mesh = obj.data
    return {
        "has_uv": bool(mesh.uv_layers),
        "layer_names": [layer.name for layer in mesh.uv_layers],
        "loop_count": len(mesh.loops),
        "layer_hashes": {layer.name: uv_sample_hash(layer) for layer in mesh.uv_layers},
        "uv_quality": uv_map_quality(obj),
    }


def check_uv_after_decimate(obj: bpy.types.Object, uv_snapshot: dict) -> dict:
    quality = uv_map_quality(obj)
    issues = list(quality["issues"])
    current_layer_names = uv_layer_name_set(obj)
    expected_names = set(uv_snapshot["layer_names"])
    missing_names = sorted(expected_names - current_layer_names)

    if uv_snapshot["has_uv"] and not quality["has_uv"]:
        issues.append(f"{obj.name}: all UV layers were lost during decimation")
    if missing_names:
        issues.append(f"{obj.name}: UV layers missing after decimation: {', '.join(missing_names)}")

    previous_quality = uv_snapshot["uv_quality"]["quality_score"]
    if uv_snapshot["has_uv"] and quality["quality_score"] + 0.25 < previous_quality:
        issues.append(
            f"{obj.name}: UV quality dropped from {previous_quality:.3f} to {quality['quality_score']:.3f} after decimation"
        )

    sample_hashes = {layer.name: uv_sample_hash(layer) for layer in obj.data.uv_layers}
    for layer_name, previous_hash in uv_snapshot["layer_hashes"].items():
        current_hash = sample_hashes.get(layer_name)
        if previous_hash and current_hash == ():
            issues.append(f"{obj.name}:{layer_name}: UV sample data disappeared after decimation")

    return {
        "needs_reunwrap": bool(issues) and uv_snapshot["has_uv"],
        "issues": issues,
        "quality": quality,
    }


def make_uv_layer_active(mesh: bpy.types.Mesh, layer_name: str) -> bpy.types.MeshUVLoopLayer:
    for index, layer in enumerate(mesh.uv_layers):
        if layer.name == layer_name:
            mesh.uv_layers.active_index = index
            if hasattr(layer, "active_render"):
                layer.active_render = True
            return layer
    raise KeyError(layer_name)


def select_faces_for_material(obj: bpy.types.Object, material_index: int | None) -> bool:
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    any_selected = False
    for face in bm.faces:
        face.select = material_index is None or face.material_index == material_index
        any_selected = any_selected or face.select
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    return any_selected


def hard_edge_hint(obj: bpy.types.Object) -> bool:
    return any(edge.use_edge_sharp for edge in obj.data.edges)


def unwrap_selected_uvs(strategy: str) -> None:
    if strategy == "smart_uv":
        bpy.ops.uv.smart_project(
            angle_limit=math.radians(DEFAULT_SMART_UV_ANGLE_DEG),
            island_margin=DEFAULT_UV_ISLAND_MARGIN,
            area_weight=0.0,
            correct_aspect=True,
            scale_to_bounds=False,
        )
        return
    if strategy == "angle_based":
        bpy.ops.uv.unwrap(method="ANGLE_BASED", margin=DEFAULT_UV_ISLAND_MARGIN)
        return
    if strategy == "project_from_view":
        bpy.ops.uv.project_from_view(orthographic=False, correct_aspect=True, scale_to_bounds=True)
        return
    raise ValueError(f"Unknown unwrap strategy: {strategy}")


def ensure_unique_layer_name(mesh: bpy.types.Mesh, preferred_name: str) -> str:
    existing_names = {layer.name for layer in mesh.uv_layers}
    if preferred_name not in existing_names:
        return preferred_name

    suffix = 1
    while True:
        candidate = f"{preferred_name}_{suffix}"
        if candidate not in existing_names:
            return candidate
        suffix += 1


def reunwrap_mesh(obj: bpy.types.Object, preserve_existing: bool = True) -> dict:
    ensure_object_mode()
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    mesh = obj.data
    original_active_name = mesh.uv_layers.active.name if mesh.uv_layers.active else None
    original_quality = uv_map_quality(obj)
    target_layer_name = ensure_unique_layer_name(mesh, UV_REWRAP_LAYER_NAME)
    mesh.uv_layers.new(name=target_layer_name)
    mesh.uv_layers.active_index = len(mesh.uv_layers) - 1
    target_layer = mesh.uv_layers[mesh.uv_layers.active_index]
    if hasattr(target_layer, "active_render"):
        target_layer.active_render = True

    strategies = ["smart_uv", "angle_based"]
    if hard_edge_hint(obj):
        strategies.append("project_from_view")

    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_mode(type="FACE")

    multi_material = len(mesh.materials) > 1
    material_groups = sorted({poly.material_index for poly in mesh.polygons}) if multi_material else [None]

    strategy_errors: list[str] = []
    unwrap_status = "rewrapped_failed"
    final_quality = original_quality
    for strategy in strategies:
        bpy.ops.mesh.select_all(action="DESELECT")
        strategy_failed = False
        for material_index in material_groups:
            has_selection = select_faces_for_material(obj, material_index)
            if not has_selection:
                continue
            try:
                unwrap_selected_uvs(strategy)
            except RuntimeError as exc:
                strategy_failed = True
                strategy_errors.append(f"{obj.name}: {strategy} failed for material {material_index}: {exc}")
                break

        bpy.ops.object.mode_set(mode="OBJECT")
        final_quality = uv_map_quality(obj)
        if not strategy_failed and final_quality["quality_score"] >= DEFAULT_UV_QUALITY_THRESHOLD:
            unwrap_status = f"rewrapped_{strategy}"
            break
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_mode(type="FACE")
    else:
        bpy.ops.object.mode_set(mode="OBJECT")

    final_name = original_active_name or "UVMap"
    target_layer = make_uv_layer_active(mesh, target_layer_name)
    if preserve_existing:
        for layer in mesh.uv_layers:
            if layer.name == final_name and layer.name != target_layer.name:
                layer.name = ensure_unique_layer_name(mesh, f"{final_name}_preserved")
                break
    target_layer.name = final_name
    make_uv_layer_active(mesh, final_name)

    return {
        "status": unwrap_status,
        "quality": final_quality,
        "issues": strategy_errors + final_quality["issues"],
    }


def process_object(obj: bpy.types.Object, ratio: float, merge_threshold: float, fill_holes_sides: int, smooth_angle_deg: float) -> dict:
    ensure_object_mode()
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    start_faces = len(obj.data.polygons)
    uv_snapshot = snapshot_uv_state(obj)

    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.remove_doubles(threshold=merge_threshold)
    bpy.ops.mesh.fill_holes(sides=fill_holes_sides)
    bpy.ops.mesh.normals_make_consistent(inside=False)
    if ratio < 1.0:
        bpy.ops.mesh.decimate(ratio=ratio)
    bpy.ops.mesh.quads_convert_to_tris(quad_method="SHORTEST_DIAGONAL", ngon_method="BEAUTY")
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.shade_smooth_by_angle(angle=math.radians(smooth_angle_deg))

    uv_check = check_uv_after_decimate(obj, uv_snapshot)
    return {
        "start_faces": start_faces,
        "final_faces": len(obj.data.polygons),
        "uv_needs_reunwrap": uv_check["needs_reunwrap"],
        "uv_issues": uv_check["issues"],
        "uv_quality": uv_check["quality"],
    }


def join_mesh_objects(mesh_objects: list[bpy.types.Object]) -> tuple[bpy.types.Object, dict]:
    ensure_object_mode()

    join_info = {
        "source_missing_uv": False,
        "expected_layer_names": [],
        "issues": [],
    }

    used_names: set[str] = set()
    for index, obj in enumerate(mesh_objects):
        if obj.type != "MESH":
            continue
        if not obj.data.uv_layers:
            join_info["source_missing_uv"] = True
            join_info["issues"].append(f"{obj.name}: missing UV layers before join")
            continue

        for layer in obj.data.uv_layers:
            if not layer.data:
                join_info["source_missing_uv"] = True
                join_info["issues"].append(f"{obj.name}:{layer.name}: empty UV data before join")
            base_name = layer.name or "UVMap"
            candidate = base_name
            suffix = 1
            while candidate in used_names:
                suffix_token = "" if suffix == 1 else f"_{suffix}"
                candidate = f"{base_name}_part{index + 1}{suffix_token}"
                suffix += 1
            if candidate != layer.name:
                layer.name = candidate
            used_names.add(candidate)

    join_info["expected_layer_names"] = sorted(used_names)

    if len(mesh_objects) == 1:
        target = mesh_objects[0]
        missing_layers = sorted(set(join_info["expected_layer_names"]) - uv_layer_name_set(target))
        if missing_layers:
            join_info["issues"].append(f"{target.name}: missing UV layers after join: {', '.join(missing_layers)}")
        return target, join_info

    bpy.ops.object.select_all(action="DESELECT")
    for obj in mesh_objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = mesh_objects[0]
    bpy.ops.object.join()
    target = bpy.context.active_object

    missing_layers = sorted(set(join_info["expected_layer_names"]) - uv_layer_name_set(target))
    if missing_layers:
        join_info["issues"].append(f"{target.name}: missing UV layers after join: {', '.join(missing_layers)}")
    return target, join_info


def export_single_mesh(
    asset_id: str,
    mesh_objects: list[bpy.types.Object],
    output_path: Path,
    ratio: float,
    merge_threshold: float,
    fill_holes_sides: int,
    smooth_angle_deg: float,
    uv_mode: str,
) -> dict:
    """Join, optimize, and export one single mesh GLB."""
    target, join_info = join_mesh_objects(mesh_objects)

    initial_uv_quality = uv_map_quality(target)
    uv_status = "preserved"
    uv_issues = list(join_info["issues"])

    should_reunwrap_before_process = uv_mode == "auto" and (
        not initial_uv_quality["has_uv"]
        or initial_uv_quality["quality_score"] < DEFAULT_UV_QUALITY_THRESHOLD
        or join_info["source_missing_uv"]
        or bool(set(join_info["expected_layer_names"]) - uv_layer_name_set(target))
    )

    if should_reunwrap_before_process:
        unwrap_result = reunwrap_mesh(target, preserve_existing=True)
        uv_status = unwrap_result["status"]
        uv_issues.extend(unwrap_result["issues"])

    process_stats = process_object(
        target,
        ratio=ratio,
        merge_threshold=merge_threshold,
        fill_holes_sides=fill_holes_sides,
        smooth_angle_deg=smooth_angle_deg,
    )

    uv_issues.extend(process_stats["uv_issues"])
    if uv_mode == "force_unwrap" or (uv_mode == "auto" and process_stats["uv_needs_reunwrap"]):
        unwrap_result = reunwrap_mesh(target, preserve_existing=True)
        uv_status = unwrap_result["status"]
        uv_issues.extend(unwrap_result["issues"])

    final_uv_quality = uv_map_quality(target)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    export_glb(output_path)

    return {
        "name": asset_id,
        "start_faces": process_stats["start_faces"],
        "final_faces": process_stats["final_faces"],
        "uv_status": uv_status,
        "uv_quality_score": final_uv_quality["quality_score"],
        "uv_issues": sorted(set(uv_issues)),
    }


def export_glb(output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.object.select_all(action="DESELECT")
    for obj in bpy.context.scene.objects:
        if obj.type == "MESH":
            obj.select_set(True)
    bpy.ops.export_scene.gltf(
        filepath=str(output_path),
        export_format="GLB",
        use_selection=True,
        export_apply=True,
    )


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    settings = build_settings(args)
    manifest_heights = load_manifest_heights(args.manifest)
    results = []

    print(
        {
            "output_dir": str(output_dir),
            "preset": args.preset,
            "uv_mode": args.uv_mode,
            **settings,
        }
    )

    for input_name in args.inputs:
        glb_path = Path(input_name).resolve()
        if not glb_path.exists():
            raise FileNotFoundError(glb_path)

        asset_id = glb_path.stem
        manifest_id = asset_id.rsplit("_", 1)[0]
        target_height_m = manifest_heights.get(manifest_id)

        reset_scene()
        imported_objects, mesh_objects = imported_mesh_objects(glb_path)
        if not mesh_objects:
            raise RuntimeError(f"No mesh objects imported from {glb_path}")

        original_height_m = None
        scale_factor = None
        final_height_m = None
        if target_height_m is not None:
            original_height_m, scale_factor, final_height_m = scale_and_ground_objects(
                imported_objects,
                mesh_objects,
                target_height_m,
            )

        mesh_stats = export_single_mesh(
            asset_id,
            mesh_objects,
            output_dir / glb_path.name,
            ratio=settings["ratio"],
            merge_threshold=settings["merge_threshold"],
            fill_holes_sides=settings["fill_holes_sides"],
            smooth_angle_deg=settings["smooth_angle_deg"],
            uv_mode=args.uv_mode,
        )

        results.append(
            {
                "input": str(glb_path),
                "output": str(output_dir / glb_path.name),
                "ratio": settings["ratio"],
                "mesh_stats": [mesh_stats],
                "target_height_m": target_height_m,
                "original_height_m": original_height_m,
                "scale_factor": scale_factor,
                "final_height_m": final_height_m,
            }
        )

    for result in results:
        print(result)


if __name__ == "__main__":
    main()
