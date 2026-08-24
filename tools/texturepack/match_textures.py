#!/usr/bin/env python3
"""Match decoded XBLA textures to stock N64 texture presets and build a pack."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
from collections import Counter
from pathlib import Path


class MatchError(RuntimeError):
    pass


EXPLOSION_FRAME_IDS = set(range(0x001E, 0x003C))


def correlation(left: list[int], right: list[int]) -> float:
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    left_delta = [value - left_mean for value in left]
    right_delta = [value - right_mean for value in right]
    denominator = math.sqrt(
        sum(value * value for value in left_delta)
        * sum(value * value for value in right_delta)
    )
    if not denominator:
        return 1.0 if left == right else 0.0
    return sum(a * b for a, b in zip(left_delta, right_delta)) / denominator


def compare_images(n64_image, xbla_image) -> dict:
    from PIL import Image

    resized = xbla_image.resize(n64_image.size, Image.Resampling.BOX)
    n64_pixels = list(n64_image.get_flattened_data())
    xbla_pixels = list(resized.get_flattened_data())
    n64_luma = [(77 * r + 150 * g + 29 * b) >> 8 for r, g, b, _ in n64_pixels]
    xbla_luma = [(77 * r + 150 * g + 29 * b) >> 8 for r, g, b, _ in xbla_pixels]
    count = len(n64_pixels)
    return {
        "luma_correlation": round(correlation(n64_luma, xbla_luma), 8),
        "luma_mae": round(
            sum(abs(a - b) for a, b in zip(n64_luma, xbla_luma))
            / (255 * count),
            8,
        ),
        "alpha_mae": round(
            sum(abs(a[3] - b[3]) for a, b in zip(n64_pixels, xbla_pixels))
            / (255 * count),
            8,
        ),
    }


def checkerboard(size: tuple[int, int]):
    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, (88, 88, 88))
    draw = ImageDraw.Draw(image)
    cell = 12
    for y in range(0, size[1], cell):
        for x in range(0, size[0], cell):
            if (x // cell + y // cell) & 1:
                draw.rectangle((x, y, x + cell - 1, y + cell - 1), fill=(128, 128, 128))
    return image


def presentation(image, size: tuple[int, int]):
    from PIL import Image, ImageOps

    image = image.convert("RGBA")
    contained = ImageOps.contain(image, size, Image.Resampling.NEAREST)
    background = checkerboard(size)
    x = (size[0] - contained.width) // 2
    y = (size[1] - contained.height) // 2
    background.paste(contained, (x, y), contained)
    return background


def write_review_sheets(entries: list[dict], n64_root: Path, xbla_root: Path, output: Path):
    from PIL import Image, ImageDraw

    output.mkdir(parents=True, exist_ok=True)
    pairs_per_sheet = 12
    cell_width, cell_height = 360, 220
    preview = (160, 160)
    for sheet_index in range(0, len(entries), pairs_per_sheet):
        group = entries[sheet_index : sheet_index + pairs_per_sheet]
        sheet = Image.new("RGB", (cell_width * 3, cell_height * 4), (32, 32, 32))
        draw = ImageDraw.Draw(sheet)
        for position, entry in enumerate(group):
            column, row = position % 3, position // 3
            left, top = column * cell_width, row * cell_height
            n64_image = Image.open(n64_root / entry["n64"]["output_file"])
            xbla_image = Image.open(xbla_root / entry["xbla"]["output_file"])
            sheet.paste(presentation(n64_image, preview), (left + 12, top + 34))
            sheet.paste(presentation(xbla_image, preview), (left + 184, top + 34))
            reasons = ", ".join(entry["review_reasons"])
            draw.text((left + 12, top + 8), f"{entry['id_hex']}  {reasons}", fill="white")
            draw.text((left + 12, top + 198), f"N64 {n64_image.width}x{n64_image.height}", fill=(210, 210, 210))
            draw.text((left + 184, top + 198), f"XBLA {xbla_image.width}x{xbla_image.height}", fill=(210, 210, 210))
        path = output / f"review_{sheet_index // pairs_per_sheet + 1:02d}.png"
        sheet.save(path)


def run(args) -> int:
    try:
        from PIL import Image
    except ImportError as exc:
        raise MatchError("Pillow is required: python -m pip install Pillow") from exc

    n64_manifest_path = args.n64_manifest.resolve()
    xbla_manifest_path = args.xbla_manifest.resolve()
    n64_root = n64_manifest_path.parent
    xbla_root = xbla_manifest_path.parent
    output = args.output.resolve()
    pack = output / "pack" / "ext_tex"
    reviews = output / "review"
    pack.mkdir(parents=True, exist_ok=True)

    n64_manifest = json.loads(n64_manifest_path.read_text(encoding="utf-8"))
    xbla_manifest = json.loads(xbla_manifest_path.read_text(encoding="utf-8"))
    xbla_by_id = {entry["id"]: entry for entry in xbla_manifest["textures"]}
    matches = []
    failures = []
    review_entries = []

    for index, n64 in enumerate(n64_manifest["textures"]):
        texture_id = n64["id"]
        entry = {
            "id": texture_id,
            "id_hex": n64["id_hex"],
            "match_basis": "same-archive-index",
            "n64": n64,
        }
        matches.append(entry)
        if n64.get("status") == "empty":
            entry["status"] = "excluded-empty-n64-slot"
            continue
        xbla = xbla_by_id.get(texture_id)
        if not xbla:
            entry["status"] = "unmatched"
            failures.append(f"{n64['id_hex']}: XBLA index is absent")
            continue
        if xbla["dimension"] != "2d":
            entry["status"] = "unmatched"
            failures.append(f"{n64['id_hex']}: XBLA candidate is {xbla['dimension']}")
            continue
        xbla_file = xbla.get("output_files", [f"{xbla['id_hex']}.png"])[0]
        xbla_copy = dict(xbla)
        xbla_copy["output_file"] = xbla_file
        entry["xbla"] = xbla_copy
        n64_image = Image.open(n64_root / n64["output_file"]).convert("RGBA")
        xbla_image = Image.open(xbla_root / xbla_file).convert("RGBA")
        entry["metrics"] = compare_images(n64_image, xbla_image)
        entry["n64_dimensions"] = [n64_image.width, n64_image.height]
        entry["xbla_original_dimensions"] = [
            xbla["original_width"],
            xbla["original_height"],
        ]
        entry["xbla_dimensions"] = [xbla_image.width, xbla_image.height]
        entry["material_policy"] = "eligible: replaces an N64 base texture preset"

        reasons = []
        if entry["n64_dimensions"] != entry["xbla_original_dimensions"]:
            reasons.append("dimension-changed")
        if texture_id in EXPLOSION_FRAME_IDS:
            reasons.append("animated-sequence")
        n64_aspect = n64_image.width / n64_image.height
        xbla_aspect = xbla_image.width / xbla_image.height
        if max(n64_aspect / xbla_aspect, xbla_aspect / n64_aspect) > 1.1:
            reasons.append("layout-changed")
        entry["review_reasons"] = reasons
        entry["status"] = "visual-review-required" if reasons else "verified-direct"
        if reasons:
            review_entries.append(entry)

        destination = pack / f"{texture_id:04x}.png"
        shutil.copyfile(xbla_root / xbla_file, destination)
        entry["pack_file"] = str(destination.relative_to(output)).replace("\\", "/")
        entry["pack_sha256"] = hashlib.sha256(destination.read_bytes()).hexdigest()
        if args.progress_every and (
            index + 1 == len(n64_manifest["textures"])
            or (index + 1) % args.progress_every == 0
        ):
            print(f"Matched {index + 1}/{len(n64_manifest['textures'])} N64 presets")

    write_review_sheets(review_entries, n64_root, xbla_root, reviews)
    excluded_xbla = [
        {
            "id": entry["id"],
            "id_hex": entry["id_hex"],
            "reason": "no stock N64 texture preset; XBLA-only material/asset",
        }
        for entry in xbla_manifest["textures"]
        if entry["id"] >= n64_manifest["entry_count"]
    ]
    manifest = {
        "n64_manifest": str(n64_manifest_path),
        "xbla_manifest": str(xbla_manifest_path),
        "policy": {
            "included": "same-index XBLA replacement for a non-empty stock N64 texture preset",
            "excluded": "XBLA-only textures, including XBLA-only bump/specular/material maps",
        },
        "match_count": sum("pack_file" in entry for entry in matches),
        "status_counts": dict(Counter(entry["status"] for entry in matches)),
        "visual_review_count": len(review_entries),
        "excluded_xbla_count": len(excluded_xbla),
        "matches": matches,
        "excluded_xbla": excluded_xbla,
        "failures": failures,
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        f"Pack textures: {manifest['match_count']}; visual review: {len(review_entries)}; "
        f"XBLA-only excluded: {len(excluded_xbla)}; failures: {len(failures)}; "
        f"manifest: {manifest_path}"
    )
    return 1 if failures else 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n64-manifest", type=Path, required=True)
    parser.add_argument("--xbla-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--progress-every", type=int, default=500)
    return parser


if __name__ == "__main__":
    try:
        raise SystemExit(run(build_parser().parse_args()))
    except (OSError, MatchError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
