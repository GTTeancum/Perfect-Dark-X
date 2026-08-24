#!/usr/bin/env python3
"""Extract the physical segment pool from Perfect Dark XBLA's PackedSegFile."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

from extract_textures import ArchiveError, decompress_xmem, parse_ids


DESCRIPTOR_SIZE = 16


def parse_archive(path: Path):
    size = path.stat().st_size
    handle = path.open("rb")
    count_data = handle.read(4)
    if len(count_data) != 4:
        handle.close()
        raise ArchiveError("archive is smaller than its entry-count header")
    count = struct.unpack(">I", count_data)[0]
    table_end = 4 + count * DESCRIPTOR_SIZE
    if not count or table_end >= size:
        handle.close()
        raise ArchiveError(f"implausible segment-slot count {count}")

    table_data = handle.read(count * DESCRIPTOR_SIZE)
    descriptors = [
        struct.unpack_from(">4I", table_data, index * DESCRIPTOR_SIZE)
        for index in range(count)
    ]
    physical = []
    for slot, (offset, decoded_size, compressed_size, property_index) in enumerate(
        descriptors
    ):
        stored_size = compressed_size or decoded_size
        if (
            offset >= table_end
            and decoded_size > 0
            and stored_size > 0
            and offset + stored_size <= size
        ):
            physical.append(
                {
                    "slot": slot,
                    "slot_hex": f"{slot:04x}",
                    "offset": offset,
                    "decoded_size": decoded_size,
                    "compressed_size": compressed_size,
                    "stored_size": stored_size,
                    "property_index": None
                    if property_index == 0xFFFFFFFF
                    else property_index,
                    "compression": "xmem-lzx" if compressed_size else "none",
                }
            )

    if not physical:
        handle.close()
        raise ArchiveError("no physical segment descriptors were found")
    ordered = sorted(physical, key=lambda item: item["offset"])
    cursor = table_end
    for entry in ordered:
        if entry["offset"] != cursor:
            handle.close()
            raise ArchiveError(
                f"segment pool is not contiguous at 0x{cursor:x}; "
                f"next descriptor begins at 0x{entry['offset']:x}"
            )
        cursor += entry["stored_size"]
    if cursor != size:
        handle.close()
        raise ArchiveError(
            f"segment pool ends at 0x{cursor:x}, archive ends at 0x{size:x}"
        )
    slots = []
    physical_slots = {entry["slot"] for entry in physical}
    for slot, descriptor in enumerate(descriptors):
        if slot in physical_slots:
            classification = "physical-segment"
        elif descriptor == (0, 0, 0, 0):
            classification = "empty"
        else:
            classification = "unmapped-metadata"
        slots.append(
            {
                "slot": slot,
                "slot_hex": f"{slot:04x}",
                "classification": classification,
                "words": [f"0x{word:08x}" for word in descriptor],
            }
        )
    return handle, count, table_end, slots, physical


def run(args) -> int:
    archive = args.archive.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    handle, slot_count, table_end, slots, physical = parse_archive(archive)
    physical_by_slot = {entry["slot"]: entry for entry in physical}
    if args.ids:
        selected_slots = parse_ids(args.ids, slot_count)
        selected = [physical_by_slot[slot] for slot in selected_slots if slot in physical_by_slot]
        missing = [slot for slot in selected_slots if slot not in physical_by_slot]
        if missing:
            handle.close()
            raise ArchiveError(
                "requested slots have no physical segment: "
                + ", ".join(f"0x{slot:04x}" for slot in missing)
            )
    else:
        selected = physical
    if args.limit is not None:
        selected = selected[: args.limit]

    manifest = {
        "source": str(archive),
        "source_size": archive.stat().st_size,
        "logical_slot_count": slot_count,
        "physical_segment_count": len(physical),
        "compressed_segment_count": sum(
            entry["compression"] == "xmem-lzx" for entry in physical
        ),
        "raw_segment_count": sum(entry["compression"] == "none" for entry in physical),
        "table_end": table_end,
        "slots": slots,
        "segments": physical,
        "failures": [],
    }
    selected_slots = {entry["slot"] for entry in selected}
    try:
        completed = 0
        for entry in physical:
            entry["selected"] = entry["slot"] in selected_slots
            if not entry["selected"] or args.manifest_only:
                continue
            handle.seek(entry["offset"])
            stored = handle.read(entry["stored_size"])
            try:
                if entry["compression"] == "xmem-lzx":
                    decoded = decompress_xmem(
                        stored, entry["decoded_size"], entry["slot"]
                    )
                else:
                    decoded = stored
                output_path = output / f"{entry['slot_hex']}.bin"
                if args.overwrite or not output_path.exists():
                    output_path.write_bytes(decoded)
                entry["output_file"] = output_path.name
                entry["sha256"] = hashlib.sha256(decoded).hexdigest()
                entry["decoded_prefix_hex"] = decoded[:16].hex()
            except Exception as exc:
                manifest["failures"].append(f"{entry['slot_hex']}: {exc}")
            completed += 1
            if args.progress_every and (
                completed == len(selected) or completed % args.progress_every == 0
            ):
                print(f"Extracted {completed}/{len(selected)} segments")
    finally:
        handle.close()

    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    failures = manifest["failures"]
    print(
        f"Archive: {slot_count} slots, {len(physical)} physical segments; "
        f"selected: {len(selected)}; failures: {len(failures)}; "
        f"manifest: {manifest_path}"
    )
    if failures:
        for failure in failures[:20]:
            print(f"ERROR: {failure}", file=sys.stderr)
        if len(failures) > 20:
            print(f"ERROR: {len(failures) - 20} additional failures", file=sys.stderr)
        return 1
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="path to DataFiles/PackedSegFile")
    parser.add_argument("--output", type=Path, required=True, help="output directory")
    parser.add_argument(
        "--ids", help="comma-separated decimal/0x-prefixed slot IDs and inclusive ranges"
    )
    parser.add_argument("--limit", type=int, help="extract only the first N selected slots")
    parser.add_argument("--manifest-only", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--progress-every", type=int, default=100)
    return parser


if __name__ == "__main__":
    try:
        raise SystemExit(run(build_parser().parse_args()))
    except (ArchiveError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
