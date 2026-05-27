#!/usr/bin/env python3
"""Rewrite embedded GLB textures without changing mesh topology."""

import argparse
import base64
import io
import shutil
from pathlib import Path

from PIL import Image
from pygltflib import BufferView, GLTF2

try:
    RESAMPLE_LANCZOS = Image.Resampling.LANCZOS
    RESAMPLE_BILINEAR = Image.Resampling.BILINEAR
except AttributeError:
    RESAMPLE_LANCZOS = Image.LANCZOS
    RESAMPLE_BILINEAR = Image.BILINEAR


def _align4(blob: bytearray) -> None:
    while len(blob) % 4:
        blob.extend(b"\x00")


def _decode_image_bytes(gltf: GLTF2, image_index: int, blob: bytes) -> bytes:
    image = gltf.images[image_index]
    if image.uri:
        header, encoded = image.uri.split(",", 1)
        if ";base64" not in header:
            raise ValueError(f"Image {image_index} uses unsupported URI encoding")
        return base64.b64decode(encoded)

    if image.bufferView is None:
        raise ValueError(f"Image {image_index} has neither uri nor bufferView")

    view = gltf.bufferViews[image.bufferView]
    start = view.byteOffset or 0
    end = start + view.byteLength
    return blob[start:end]


def _collect_image_roles(gltf: GLTF2) -> dict[int, set[str]]:
    roles: dict[int, set[str]] = {}
    textures = gltf.textures or []

    def add_role(texture_info, role: str) -> None:
        if texture_info is None or texture_info.index is None:
            return
        if texture_info.index >= len(textures):
            return
        source = textures[texture_info.index].source
        if source is None:
            return
        roles.setdefault(source, set()).add(role)

    for material in gltf.materials or []:
        pbr = getattr(material, "pbrMetallicRoughness", None)
        if pbr is not None:
            add_role(getattr(pbr, "baseColorTexture", None), "baseColor")
            add_role(getattr(pbr, "metallicRoughnessTexture", None), "metallicRoughness")
        add_role(getattr(material, "normalTexture", None), "normal")
        add_role(getattr(material, "occlusionTexture", None), "occlusion")
        add_role(getattr(material, "emissiveTexture", None), "emissive")

    return roles


def _resample_for_roles(roles: set[str]):
    if roles & {"normal", "metallicRoughness", "occlusion"}:
        return RESAMPLE_BILINEAR
    return RESAMPLE_LANCZOS


def _rewrite_texture_bytes(
    raw_bytes: bytes,
    roles: set[str],
    max_texture_size: int,
    jpeg_quality: int,
) -> tuple[bytes, str, tuple[int, int]]:
    with Image.open(io.BytesIO(raw_bytes)) as source:
        image = source.copy()

    width, height = image.size
    longest_edge = max(width, height)
    if max_texture_size > 0 and longest_edge > max_texture_size:
        scale = max_texture_size / float(longest_edge)
        new_size = (
            max(1, int(round(width * scale))),
            max(1, int(round(height * scale))),
        )
        image = image.resize(new_size, _resample_for_roles(roles))

    has_alpha = "A" in image.getbands() and image.getchannel("A").getextrema() != (255, 255)
    encoded = io.BytesIO()
    if has_alpha:
        image = image.convert("RGBA")
        image.save(encoded, format="PNG", optimize=True)
        return encoded.getvalue(), "image/png", image.size

    image = image.convert("RGB")
    image.save(
        encoded,
        format="JPEG",
        quality=jpeg_quality,
        optimize=True,
        progressive=True,
        subsampling=0,
    )
    return encoded.getvalue(), "image/jpeg", image.size


def rewrite_glb_embedded_textures(
    glb_path: Path,
    max_texture_size: int = 1024,
    jpeg_quality: int = 85,
) -> dict:
    gltf = GLTF2().load_binary(str(glb_path))
    if not gltf.images:
        return {"rewritten_images": 0, "jpeg_images": 0, "png_images": 0}

    original_blob = gltf.binary_blob() or b""
    original_views = []
    for view in gltf.bufferViews or []:
        start = view.byteOffset or 0
        end = start + view.byteLength
        original_views.append(original_blob[start:end])

    roles_by_image = _collect_image_roles(gltf)
    image_view_indices = {image.bufferView for image in gltf.images if image.bufferView is not None}
    if gltf.bufferViews is None:
        gltf.bufferViews = []

    new_blob = bytearray()
    for view_index, view in enumerate(gltf.bufferViews):
        if view_index in image_view_indices:
            continue
        _align4(new_blob)
        view.byteOffset = len(new_blob)
        payload = original_views[view_index]
        view.byteLength = len(payload)
        new_blob.extend(payload)

    jpeg_images = 0
    png_images = 0
    for image_index, image in enumerate(gltf.images):
        rewritten, mime_type, _ = _rewrite_texture_bytes(
            _decode_image_bytes(gltf, image_index, original_blob),
            roles_by_image.get(image_index, set()),
            max_texture_size=max_texture_size,
            jpeg_quality=jpeg_quality,
        )
        _align4(new_blob)
        if image.bufferView is None:
            image.bufferView = len(gltf.bufferViews)
            gltf.bufferViews.append(BufferView(buffer=0, byteOffset=0, byteLength=0))
        view = gltf.bufferViews[image.bufferView]
        view.buffer = 0
        view.byteOffset = len(new_blob)
        view.byteLength = len(rewritten)
        new_blob.extend(rewritten)
        image.uri = None
        image.mimeType = mime_type
        if mime_type == "image/jpeg":
            jpeg_images += 1
        else:
            png_images += 1

    gltf.buffers[0].byteLength = len(new_blob)
    gltf.buffers[0].uri = None
    gltf.set_binary_blob(bytes(new_blob))
    gltf.save_binary(str(glb_path))

    return {
        "rewritten_images": len(gltf.images),
        "jpeg_images": jpeg_images,
        "png_images": png_images,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rewrite embedded GLB textures as smaller JPEG/PNG payloads."
    )
    parser.add_argument("paths", nargs="+", help="GLB files to rewrite")
    parser.add_argument(
        "--max-texture-size",
        type=int,
        default=1024,
        help="Maximum width or height for embedded textures (default: 1024)",
    )
    parser.add_argument(
        "--jpeg-quality",
        type=int,
        default=85,
        help="JPEG quality for opaque embedded textures (default: 85)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Optional output directory; if omitted, rewrites files in place",
    )
    args = parser.parse_args()

    for raw_path in args.paths:
        input_path = Path(raw_path)
        output_path = input_path
        if args.output_dir is not None:
            args.output_dir.mkdir(parents=True, exist_ok=True)
            output_path = args.output_dir / input_path.name
            shutil.copy2(input_path, output_path)

        stats = rewrite_glb_embedded_textures(
            output_path,
            max_texture_size=args.max_texture_size,
            jpeg_quality=args.jpeg_quality,
        )
        print(
            f"{output_path}: {stats['rewritten_images']} textures, "
            f"{stats['jpeg_images']} JPEG, {stats['png_images']} PNG"
        )


if __name__ == "__main__":
    main()