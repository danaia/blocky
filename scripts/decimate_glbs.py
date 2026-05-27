#!/usr/bin/env python3
"""Batch decimate generated GLBs with Blender.

This script must be run via Blender because it depends on ``bpy``:

    blender --background --python scripts/decimate_glbs.py -- ...

LM Studio is not used by this script.
"""

import argparse
import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector


DEFAULT_OUTPUT_DIR = Path("in_game_assets/street_furniture")

# Default cleanup + decimation settings.
# Change these values here for the common case, or override them on the CLI.
DEFAULT_DECIMATE_RATIO = 0.2
DEFAULT_MERGE_THRESHOLD = 0.001
DEFAULT_FILL_HOLES_SIDES = 4
DEFAULT_SMOOTH_ANGLE_DEG = 45.0

DECIMATION_PRESETS = {
    "aggressive": {
        "ratio": 0.15,
        "merge_threshold": 0.001,
        "fill_holes_sides": 4,
        "smooth_angle_deg": 45.0,
    },
    "game_ready": {
        "ratio": DEFAULT_DECIMATE_RATIO,
        "merge_threshold": DEFAULT_MERGE_THRESHOLD,
        "fill_holes_sides": DEFAULT_FILL_HOLES_SIDES,
        "smooth_angle_deg": DEFAULT_SMOOTH_ANGLE_DEG,
    },
    "conservative": {
        "ratio": 0.3,
        "merge_threshold": 0.001,
        "fill_holes_sides": 4,
        "smooth_angle_deg": 45.0,
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
        default="game_ready",
        help="Named decimation preset to use before applying any explicit overrides",
    )
    parser.add_argument("--ratio", type=float, default=None, help="Decimate ratio override")
    parser.add_argument("--merge-threshold", type=float, default=None, dest="merge_threshold")
    parser.add_argument("--fill-holes-sides", type=int, default=None, dest="fill_holes_sides")
    parser.add_argument("--smooth-angle-deg", type=float, default=None, dest="smooth_angle_deg")
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


def process_object(obj: bpy.types.Object, ratio: float, merge_threshold: float, fill_holes_sides: int, smooth_angle_deg: float) -> tuple[int, int]:
    ensure_object_mode()
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    start_faces = len(obj.data.polygons)

    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.remove_doubles(threshold=merge_threshold)
    bpy.ops.mesh.fill_holes(sides=fill_holes_sides)
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.mesh.decimate(ratio=ratio)
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.shade_smooth_by_angle(angle=math.radians(smooth_angle_deg))

    return start_faces, len(obj.data.polygons)


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

        total_start_faces = 0
        total_final_faces = 0
        for obj in mesh_objects:
            start_faces, final_faces = process_object(
                obj,
                ratio=settings["ratio"],
                merge_threshold=settings["merge_threshold"],
                fill_holes_sides=settings["fill_holes_sides"],
                smooth_angle_deg=settings["smooth_angle_deg"],
            )
            total_start_faces += start_faces
            total_final_faces += final_faces

        original_height_m = None
        scale_factor = None
        final_height_m = None
        if target_height_m is not None:
            original_height_m, scale_factor, final_height_m = scale_and_ground_objects(
                imported_objects,
                mesh_objects,
                target_height_m,
            )

        output_path = output_dir / glb_path.name
        export_glb(output_path)
        results.append(
            {
                "input": str(glb_path),
                "output": str(output_path),
                "start_faces": total_start_faces,
                "final_faces": total_final_faces,
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
