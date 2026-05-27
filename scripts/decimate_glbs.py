#!/usr/bin/env python3
"""Batch decimate generated GLBs with Blender.

This script must be run via Blender because it depends on ``bpy``:

    blender --background --python scripts/decimate_glbs.py -- ...

LM Studio is not used by this script.
"""

import argparse
import math
import sys
from pathlib import Path

import bpy


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


def parse_args() -> argparse.Namespace:
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = []

    parser = argparse.ArgumentParser(description="Batch decimate GLB meshes in Blender")
    parser.add_argument("inputs", nargs="+", help="Input GLB files")
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
    imported = [obj for obj in bpy.data.objects if obj.name not in before and obj.type == "MESH"]
    if not imported:
        imported = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    return imported


def ensure_object_mode() -> None:
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")


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

        reset_scene()
        mesh_objects = imported_mesh_objects(glb_path)
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

        output_path = output_dir / glb_path.name
        export_glb(output_path)
        results.append(
            {
                "input": str(glb_path),
                "output": str(output_path),
                "start_faces": total_start_faces,
                "final_faces": total_final_faces,
            }
        )

    for result in results:
        print(result)


if __name__ == "__main__":
    main()
