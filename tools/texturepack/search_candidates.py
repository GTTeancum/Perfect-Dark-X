#!/usr/bin/env python3
"""Search the XBLA diffuse corpus for whole-image and atlas-crop N64 matches.

This is deliberately a review aid, not an automatic matcher.  Low-resolution N64
art can score well against unrelated smooth material textures, so every result is
written to a labelled contact sheet for a human decision.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps


SAMPLE_SIZE = 32


@dataclass(frozen=True)
class Candidate:
    score: float
    texture_id: int
    texture_hex: str
    output_file: str
    crop: tuple[int, int, int, int]


def parse_id(value: str) -> int:
    return int(value, 16)


def checkerboard(size: tuple[int, int]) -> Image.Image:
    image = Image.new("RGB", size, (88, 88, 88))
    draw = ImageDraw.Draw(image)
    cell = 12
    for y in range(0, size[1], cell):
        for x in range(0, size[0], cell):
            if (x // cell + y // cell) & 1:
                draw.rectangle(
                    (x, y, x + cell - 1, y + cell - 1), fill=(128, 128, 128)
                )
    return image


def presentation(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    image = image.convert("RGBA")
    contained = ImageOps.contain(image, size, Image.Resampling.NEAREST)
    background = checkerboard(size)
    left = (size[0] - contained.width) // 2
    top = (size[1] - contained.height) // 2
    background.paste(contained, (left, top), contained)
    return background


def feature(image: Image.Image) -> np.ndarray:
    rgba = np.asarray(
        image.convert("RGBA").resize(
            (SAMPLE_SIZE, SAMPLE_SIZE), Image.Resampling.BILINEAR
        ),
        dtype=np.float32,
    ) / 255.0
    rgb = rgba[:, :, :3]
    alpha = rgba[:, :, 3:4]
    luma = (
        rgb[:, :, 0:1] * 0.299
        + rgb[:, :, 1:2] * 0.587
        + rgb[:, :, 2:3] * 0.114
    )
    dx = np.diff(luma, axis=1, append=luma[:, -1:, :])
    dy = np.diff(luma, axis=0, append=luma[-1:, :, :])
    # Shape/edge agreement is more stable across the XBLA art's color and
    # lighting changes; low-weight RGB and alpha still disambiguate icons.
    value = np.concatenate((luma, dx * 2.0, dy * 2.0, rgb * 0.35, alpha * 0.5), axis=2)
    value -= value.mean(axis=(0, 1), keepdims=True)
    norm = float(np.linalg.norm(value))
    return value.ravel() / norm if norm else value.ravel()


def crop_boxes(width: int, height: int, aspect: float) -> list[tuple[int, int, int, int]]:
    boxes = {(0, 0, width, height)}
    for fraction in (1.0, 0.75, 0.5, 0.375, 0.25):
        if width / height >= aspect:
            crop_height = max(1, round(height * fraction))
            crop_width = max(1, min(width, round(crop_height * aspect)))
        else:
            crop_width = max(1, round(width * fraction))
            crop_height = max(1, min(height, round(crop_width / aspect)))
        x_steps = max(1, min(7, math.ceil(width / crop_width * 2)))
        y_steps = max(1, min(7, math.ceil(height / crop_height * 2)))
        for yi in range(y_steps):
            top = 0 if y_steps == 1 else round((height - crop_height) * yi / (y_steps - 1))
            for xi in range(x_steps):
                left = 0 if x_steps == 1 else round((width - crop_width) * xi / (x_steps - 1))
                boxes.add((left, top, left + crop_width, top + crop_height))
    return sorted(boxes)


def best_candidate(
    query_feature: np.ndarray,
    query_aspect: float,
    entry: dict,
    xbla_root: Path,
) -> Candidate:
    output_file = entry.get("output_files", [f"{entry['id_hex']}.png"])[0]
    with Image.open(xbla_root / output_file) as image:
        image = image.convert("RGBA")
        best_score = -2.0
        best_box = (0, 0, image.width, image.height)
        for box in crop_boxes(image.width, image.height, query_aspect):
            score = float(np.dot(query_feature, feature(image.crop(box))))
            if score > best_score:
                best_score = score
                best_box = box
    return Candidate(best_score, entry["id"], entry["id_hex"], output_file, best_box)


def load_coarse_features(
    entries: list[dict], xbla_root: Path
) -> tuple[np.ndarray, list[str]]:
    features = []
    files = []
    for entry in entries:
        output_file = entry.get("output_files", [f"{entry['id_hex']}.png"])[0]
        with Image.open(xbla_root / output_file) as image:
            features.append(feature(image))
        files.append(output_file)
    return np.stack(features), files


def write_sheet(
    texture_id: int,
    query: Image.Image,
    candidates: list[Candidate],
    xbla_root: Path,
    output: Path,
) -> None:
    cell_width, cell_height = 360, 225
    columns = 3
    rows = math.ceil((len(candidates) + 1) / columns)
    sheet = Image.new("RGB", (cell_width * columns, cell_height * rows), (32, 32, 32))
    draw = ImageDraw.Draw(sheet)
    sheet.paste(presentation(query, (200, 170)), (12, 34))
    draw.text((12, 8), f"N64 {texture_id:04x} query", fill="white")
    draw.text((12, 207), f"{query.width}x{query.height}", fill=(210, 210, 210))
    for offset, candidate in enumerate(candidates, start=1):
        column, row = offset % columns, offset // columns
        left, top = column * cell_width, row * cell_height
        with Image.open(xbla_root / candidate.output_file) as image:
            crop = image.convert("RGBA").crop(candidate.crop)
        sheet.paste(presentation(crop, (200, 170)), (left + 12, top + 34))
        draw.text(
            (left + 12, top + 8),
            f"XBLA {candidate.texture_hex} score {candidate.score:.3f}",
            fill="white",
        )
        draw.text((left + 12, top + 207), f"crop {candidate.crop}", fill=(210, 210, 210))
    sheet.save(output / f"{texture_id:04x}.png")


def run(args: argparse.Namespace) -> int:
    n64_manifest_path = args.n64_manifest.resolve()
    xbla_manifest_path = args.xbla_manifest.resolve()
    n64_root = n64_manifest_path.parent
    xbla_root = xbla_manifest_path.parent
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    n64_manifest = json.loads(n64_manifest_path.read_text(encoding="utf-8"))
    xbla_manifest = json.loads(xbla_manifest_path.read_text(encoding="utf-8"))
    n64_by_id = {entry["id"]: entry for entry in n64_manifest["textures"]}
    xbla_entries = [
        entry for entry in xbla_manifest["textures"] if entry["dimension"] == "2d"
    ]
    print(f"Indexing {len(xbla_entries)} XBLA 2D textures...")
    coarse_features, _ = load_coarse_features(xbla_entries, xbla_root)
    result = []
    for texture_id in args.ids:
        n64 = n64_by_id.get(texture_id)
        if not n64 or n64.get("status") != "decoded":
            raise ValueError(f"N64 texture {texture_id:04x} is not decoded")
        with Image.open(n64_root / n64["output_file"]) as image:
            query = image.convert("RGBA")
        query_feature = feature(query)
        query_aspect = query.width / query.height
        coarse_scores = coarse_features @ query_feature
        coarse_count = min(args.coarse_limit, len(xbla_entries))
        coarse_indices = np.argpartition(coarse_scores, -coarse_count)[-coarse_count:]
        # Always include the authoritative same-index slot and nearby entries;
        # XBLA frequently consolidated adjacent N64 crops into a shared atlas.
        selected_indices = set(int(index) for index in coarse_indices)
        for index, entry in enumerate(xbla_entries):
            if abs(entry["id"] - texture_id) <= args.nearby:
                selected_indices.add(index)
        candidates = sorted(
            (
                best_candidate(
                    query_feature, query_aspect, xbla_entries[index], xbla_root
                )
                for index in selected_indices
            ),
            key=lambda item: item.score,
            reverse=True,
        )[: args.limit]
        write_sheet(texture_id, query, candidates, xbla_root, output)
        result.append(
            {
                "id": texture_id,
                "id_hex": f"{texture_id:04x}",
                "candidates": [
                    {
                        "xbla_id": item.texture_id,
                        "xbla_id_hex": item.texture_hex,
                        "score": round(item.score, 8),
                        "output_file": item.output_file,
                        "crop": list(item.crop),
                    }
                    for item in candidates
                ],
            }
        )
        print(f"Searched {texture_id:04x}: {candidates[0].texture_hex} ({candidates[0].score:.3f})")
    (output / "manifest.json").write_text(
        json.dumps({"queries": result}, indent=2) + "\n", encoding="utf-8"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n64-manifest", type=Path, required=True)
    parser.add_argument("--xbla-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--coarse-limit", type=int, default=250)
    parser.add_argument("--nearby", type=int, default=32)
    parser.add_argument("ids", type=parse_id, nargs="+")
    return parser


if __name__ == "__main__":
    try:
        raise SystemExit(run(build_parser().parse_args()))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
