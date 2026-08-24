#!/usr/bin/env python3
"""Convert the reviewed PNG pack to one bounded Original-Xbox texture archive."""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path

from PIL import Image, ImageOps


MAGIC = b"PDTXPAK1"
ENTRY = struct.Struct("<IIIHH")
RUNTIME_MAX_DIMENSION = 512


def parse_texture_id(value: str | int) -> int:
    if isinstance(value, int):
        return value
    text = value.strip().lower()
    try:
        return int(text, 0)
    except ValueError:
        return int(text, 16)


def verify_archive(path: Path, expected_entries: int, expected_present: int) -> None:
    """Read back every archive entry and verify its complete zlib payload."""
    archive_size = path.stat().st_size
    with path.open("rb") as archive:
        if archive.read(len(MAGIC)) != MAGIC:
            raise ValueError("archive verification failed: bad magic")
        count_data = archive.read(4)
        if len(count_data) != 4:
            raise ValueError("archive verification failed: truncated entry count")
        entry_count = struct.unpack("<I", count_data)[0]
        if entry_count != expected_entries:
            raise ValueError(
                f"archive verification failed: expected {expected_entries} slots, "
                f"found {entry_count}"
            )
        table_data = archive.read(entry_count * ENTRY.size)
        if len(table_data) != entry_count * ENTRY.size:
            raise ValueError("archive verification failed: truncated table")

        present = 0
        for texture_id in range(entry_count):
            item = ENTRY.unpack_from(table_data, texture_id * ENTRY.size)
            offset, compressed_size, raw_size, width, height = item
            if compressed_size == 0:
                if item != (0, 0, 0, 0, 0):
                    raise ValueError(
                        f"archive verification failed: partial empty slot {texture_id:04x}"
                    )
                continue
            present += 1
            if width == 0 or height == 0 or raw_size != width * height * 4:
                raise ValueError(
                    f"archive verification failed: invalid dimensions for {texture_id:04x}"
                )
            if offset < len(MAGIC) + 4 + entry_count * ENTRY.size:
                raise ValueError(
                    f"archive verification failed: payload overlaps table for {texture_id:04x}"
                )
            if offset + compressed_size > archive_size:
                raise ValueError(
                    f"archive verification failed: payload outside file for {texture_id:04x}"
                )
            archive.seek(offset)
            compressed = archive.read(compressed_size)
            if len(compressed) != compressed_size:
                raise ValueError(
                    f"archive verification failed: truncated payload for {texture_id:04x}"
                )
            try:
                raw = zlib.decompress(compressed)
            except zlib.error as exc:
                raise ValueError(
                    f"archive verification failed: zlib error for {texture_id:04x}: {exc}"
                ) from exc
            if len(raw) != raw_size:
                raise ValueError(
                    f"archive verification failed: raw size mismatch for {texture_id:04x}"
                )

    if present != expected_present:
        raise ValueError(
            f"archive verification failed: expected {expected_present} textures, "
            f"found {present}"
        )


def run(args: argparse.Namespace) -> int:
    manifest_path = args.manifest.resolve()
    source_root = manifest_path.parent
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest["entries"]
    profile: dict[str, object] = {}
    if args.profile:
        profile = json.loads(args.profile.resolve().read_text(encoding="utf-8"))

    base_max_dimension = (
        args.max_dimension
        if args.max_dimension is not None
        else int(profile.get("base_max_dimension", 256))
    )
    large_max_dimension = (
        args.large_max_dimension
        if args.large_max_dimension is not None
        else int(profile.get("large_max_dimension", base_max_dimension))
    )
    promote_ids = {
        parse_texture_id(value) for value in profile.get("promote_ids", [])
    }
    promote_ids.update(parse_texture_id(value) for value in args.promote_id)

    for label, dimension in (
        ("base max dimension", base_max_dimension),
        ("large max dimension", large_max_dimension),
    ):
        if dimension < 1 or dimension > RUNTIME_MAX_DIMENSION:
            raise ValueError(
                f"{label} must be between 1 and {RUNTIME_MAX_DIMENSION}, got {dimension}"
            )
    if large_max_dimension < base_max_dimension:
        raise ValueError("large max dimension cannot be smaller than the base tier")

    manifest_ids = {entry["id"] for entry in entries if entry.get("pack_file")}
    missing_promotions = sorted(promote_ids - manifest_ids)
    if missing_promotions:
        raise ValueError(
            "promoted IDs have no packed diffuse: "
            + ", ".join(f"{value:04x}" for value in missing_promotions)
        )
    if args.large_texture_limit is not None and len(promote_ids) > args.large_texture_limit:
        raise ValueError(
            f"profile promotes {len(promote_ids)} textures, exceeding "
            f"--large-texture-limit {args.large_texture_limit}"
        )

    entry_count = max(entry["id"] for entry in entries) + 1
    table = [(0, 0, 0, 0, 0)] * entry_count
    blobs: list[tuple[int, bytes, int, int, int]] = []
    tier_counts: dict[int, int] = {}
    raw_total = 0

    for entry in entries:
        pack_file = entry.get("pack_file")
        if not pack_file:
            continue
        with Image.open(source_root / pack_file) as source:
            image = ImageOps.exif_transpose(source).convert("RGBA")
            max_dimension = (
                large_max_dimension if entry["id"] in promote_ids
                else base_max_dimension
            )
            if max(image.size) > max_dimension:
                image.thumbnail(
                    (max_dimension, max_dimension), Image.Resampling.LANCZOS
                )
            width, height = image.size
            raw = image.tobytes()
        tier_counts[max_dimension] = tier_counts.get(max_dimension, 0) + 1
        raw_total += len(raw)
        compressed = zlib.compress(raw, args.compression_level)
        blobs.append((entry["id"], compressed, len(raw), width, height))

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    header_size = len(MAGIC) + 4 + entry_count * ENTRY.size
    offset = header_size
    for texture_id, compressed, raw_size, width, height in blobs:
        table[texture_id] = (offset, len(compressed), raw_size, width, height)
        offset += len(compressed)

    fd, temporary_name = tempfile.mkstemp(
        prefix=output.name + ".", suffix=".tmp", dir=output.parent
    )
    try:
        with os.fdopen(fd, "wb") as archive:
            archive.write(MAGIC)
            archive.write(struct.pack("<I", entry_count))
            for item in table:
                archive.write(ENTRY.pack(*item))
            for _, compressed, _, _, _ in blobs:
                archive.write(compressed)
        os.replace(temporary_name, output)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise

    verify_archive(output, entry_count, len(blobs))
    print(
        f"Xbox texture archive: {len(blobs)}/{entry_count} slots, "
        f"base {base_max_dimension}px, promoted {len(promote_ids)} at "
        f"{large_max_dimension}px, {output.stat().st_size} bytes, "
        f"all payloads verified -> {output}"
    )
    print(
        "Tier inputs: "
        + ", ".join(f"{dimension}px={count}" for dimension, count in sorted(tier_counts.items()))
        + f"; raw RGBA total={raw_total / (1024 * 1024):.2f} MiB"
    )
    if promote_ids:
        print("Promoted IDs: " + ", ".join(f"{value:04x}" for value in sorted(promote_ids)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", type=Path,
                        help="JSON release profile containing tier dimensions and promoted IDs")
    parser.add_argument("--max-dimension", type=int, default=None,
                        help="Base tier maximum; overrides the profile")
    parser.add_argument("--large-max-dimension", type=int, default=None,
                        help="Promoted tier maximum; overrides the profile")
    parser.add_argument("--promote-id", action="append", default=[],
                        help="Texture ID to place in the promoted tier (repeatable; hex accepted)")
    parser.add_argument("--large-texture-limit", type=int, default=4,
                        help="Safety limit for promoted textures (default: 4)")
    parser.add_argument("--compression-level", type=int, choices=range(0, 10), default=6)
    return parser


if __name__ == "__main__":
    try:
        raise SystemExit(run(build_parser().parse_args()))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
