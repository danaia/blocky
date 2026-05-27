#!/usr/bin/env python3
"""
Optimize GLB meshes by shrinking embedded PBR textures without changing topology.
"""

from pathlib import Path
import numpy as np
import trimesh
from PIL import Image

try:
    RESAMPLE_LANCZOS = Image.Resampling.LANCZOS
    RESAMPLE_BILINEAR = Image.Resampling.BILINEAR
except AttributeError:
    RESAMPLE_LANCZOS = Image.LANCZOS
    RESAMPLE_BILINEAR = Image.BILINEAR

def extract_textures(mesh):
    """Extract PBR textures from a trimesh geometry."""
    textures = {}
    
    if not hasattr(mesh, 'visual') or mesh.visual is None:
        return textures
    
    visual = mesh.visual
    if not hasattr(visual, 'material'):
        return textures
    
    mat = visual.material
    if not hasattr(mat, '__class__') or 'PBR' not in type(mat).__name__:
        # Try to get base color anyway (may be StandardMaterial)
        pass
    
    # Extract base color texture
    if hasattr(mat, 'baseColorTexture'):
        tex = mat.baseColorTexture
        if tex is not None and hasattr(tex, 'size') and len(tex.size) == 2:
            textures['baseColor'] = Image.fromarray(np.array(tex))
    
    # Extract metallic/roughness texture (often packed in RGB channels)
    if hasattr(mat, 'metallicRoughnessTexture'):
        tex = mat.metallicRoughnessTexture
        if tex is not None and hasattr(tex, 'size') and len(tex.size) == 2:
            textures['metallicRoughness'] = Image.fromarray(np.array(tex))
    
    # Extract normal map if present
    if hasattr(mat, 'normalTexture'):
        tex = mat.normalTexture
        if tex is not None and hasattr(tex, 'size') and len(tex.size) == 2:
            textures['normal'] = Image.fromarray(np.array(tex))
    
    return textures


def shrink_textures(textures, max_texture_size: int):
    """Downscale extracted textures to reduce embedded GLB size."""
    if max_texture_size <= 0:
        return textures

    resized = {}
    for name, image in textures.items():
        width, height = image.size
        longest_edge = max(width, height)
        if longest_edge <= max_texture_size:
            resized[name] = image
            continue

        scale = max_texture_size / float(longest_edge)
        new_size = (
            max(1, int(round(width * scale))),
            max(1, int(round(height * scale))),
        )
        resample = RESAMPLE_BILINEAR if name == 'normal' else RESAMPLE_LANCZOS
        resized[name] = image.resize(new_size, resample)

    return resized


def apply_textures(mesh, textures, original_uv=None):
    """Apply extracted textures back to a mesh."""
    if not textures:
        return
    
    if 'baseColor' in textures:
        uv = None
        
        if original_uv is not None:
            uv = original_uv
        
        # Fallback: simple cubic projection (lower quality)
        if uv is None:
            bounds = mesh.bounds  # [[min_x,min_y,min_z],[max_x,max_y,max_z]]
            centers = mesh.vertices.mean(axis=0)
            # Project each vertex onto the dominant face of a cube
            diffs = mesh.vertices - centers
            abs_diffs = np.abs(diffs)
            max_axis = abs_diffs.argmax(axis=1)  # 0=x, 1=y, 2=z
            uv = np.zeros((len(mesh.vertices), 2))
            for i in range(len(mesh.vertices)):
                if max_axis[i] == 0:  # x is dominant
                    uv[i] = [diffs[i][2], diffs[i][1]] / (abs_diffs[i][max_axis[i]] + 1e-8)
                elif max_axis[i] == 1:  # y is dominant
                    uv[i] = [diffs[i][0], diffs[i][2]] / (abs_diffs[i][max_axis[i]] + 1e-8)
                else:  # z is dominant
                    uv[i] = [diffs[i][0], diffs[i][1]] / (abs_diffs[i][max_axis[i]] + 1e-8)
            # Normalize to [0, 1]
            uv_min = uv.min(axis=0)
            uv_max = uv.max(axis=0)
            range_uv = uv_max - uv_min
            range_uv[range_uv == 0] = 1.0
            uv = (uv - uv_min) / range_uv
        
        # Create a proper PBR material using trimesh's built-in class
        from trimesh.visual.material import PBRMaterial
        material = PBRMaterial(
            baseColorTexture=textures['baseColor'],
            metallicRoughnessTexture=textures.get('metallicRoughness'),
            normalTexture=textures.get('normal'),
            baseColorFactor=np.array([1.0, 1.0, 1.0, 1.0]),
        )
        mesh.visual = trimesh.visual.TextureVisuals(uv=uv, material=material)


def optimize_mesh(
    input_path: Path,
    output_dir: Path,
    max_texture_size: int = 1024,
):
    """Load a GLB, shrink embedded textures, and save to output_dir."""
    
    print(f"Processing {input_path.name}...")
    
    # Load mesh (GLBs often load as Scenes with multiple geometries)
    loaded = trimesh.load(input_path, file_type='glb', process=False)
    
    if isinstance(loaded, trimesh.Scene):
        meshes = list(loaded.geometry.values())
    else:
        meshes = [loaded]

    # Combine into a single mesh for optimization (better decimation results)
    mesh = trimesh.util.concatenate(meshes)

    # Extract textures BEFORE any modification
    textures = extract_textures(mesh)
    original_uv = None
    if hasattr(mesh.visual, 'uv') and mesh.visual.uv is not None:
        original_uv = np.array(mesh.visual.uv)
        print(f"  📐 Preserved {len(original_uv)} UV coordinates")
    if textures:
        textures = shrink_textures(textures, max_texture_size=max_texture_size)
        print(f"  📸 Found {len(textures)} texture(s): {', '.join(textures.keys())}")
        print(f"  🗜 Texture cap: {max_texture_size}px")

    # Fix normals (ensure consistent winding)
    if not mesh.is_watertight:
        try:
            trimesh.repair.fix_normals(mesh)
            print("  ✓ Fixed normals")
        except Exception as e:
            print(f"  ⚠ Normal fix failed: {e}")

    # Re-apply textures to the original mesh
    if textures:
        try:
            apply_textures(mesh, textures, original_uv=original_uv)
            print("  🎨 Textures re-applied")
        except Exception as e:
            print(f"  ⚠ Texture re-application failed ({e}), continuing without textures")

    # Save output
    out_path = output_dir / input_path.name
    try:
        mesh.export(out_path)
        print(f"  ✓ Saved to {out_path}")
        
        # Stats
        file_size_kb = out_path.stat().st_size / 1024
        print(f"  📊 Size: {file_size_kb:.1f} KB")
    except Exception as e:
        print(f"  ✗ Save failed: {e}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Optimize GLB meshes while preserving textures."
    )
    parser.add_argument("paths", nargs="*", help="Specific GLB files to optimize")
    parser.add_argument(
        "--max-texture-size",
        type=int,
        default=1024,
        help="Maximum width or height for embedded textures (default: 1024)",
    )
    args = parser.parse_args()

    # Default paths
    base_dir = Path(__file__).parent.parent
    input_dir = base_dir / "outputs" / "meshes"
    output_dir = base_dir / "optimized_meshes_test"
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # If specific files passed as args, use those; otherwise process all
    if args.paths:
        targets = [Path(f) for f in args.paths]
    else:
        targets = list(input_dir.glob("**/*.glb"))

    print(f"Found {len(targets)} meshes to optimize.\n")
    
    for glb_path in targets:
        # Ensure subdirectory structure is preserved or flattened? 
        # Let's flatten into output_dir for simplicity, or keep category.
        # Keeping category is better for organization.
        cat = glb_path.parent.name
        (output_dir / cat).mkdir(exist_ok=True)
        
        optimize_mesh(
            glb_path,
            output_dir / cat,
            max_texture_size=args.max_texture_size,
        )
        print()
