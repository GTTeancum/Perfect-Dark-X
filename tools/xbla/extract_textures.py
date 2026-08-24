#!/usr/bin/env python3
"""Extract editable textures from Perfect Dark XBLA's Textures.raw archive."""

from __future__ import annotations

import argparse
import io
import json
import struct
import sys
from collections import Counter
from pathlib import Path

from vendor.lzx import LzxDecoder


ENTRY_WORDS = 13
ENTRY_SIZE = ENTRY_WORDS * 4
XMEM_WINDOW_BITS = 17
CUBE_FACE_COUNT = 6
FORMAT_INFO = {
    6: ("A8R8G8B8", 1, 1, 4, None),
    18: ("DXT1", 4, 4, 8, b"DXT1"),
    19: ("DXT3", 4, 4, 16, b"DXT3"),
    20: ("DXT5", 4, 4, 16, b"DXT5"),
}
ENDIAN_NAMES = {0: "none", 1: "8in16", 2: "8in32", 3: "16in32"}
DIMENSION_NAMES = {0: "1d", 1: "2d", 2: "3d", 3: "cube"}


class ArchiveError(RuntimeError):
    pass


def read_table(handle, count: int) -> list[tuple[int, ...]]:
    table = []
    for index in range(count):
        data = handle.read(ENTRY_SIZE)
        if len(data) != ENTRY_SIZE:
            raise ArchiveError(f"truncated descriptor table at entry {index}")
        table.append(struct.unpack(">13I", data))
    return table


def parse_archive(path: Path):
    size = path.stat().st_size
    handle = path.open("rb")
    count_data = handle.read(4)
    if len(count_data) != 4:
        handle.close()
        raise ArchiveError("archive is smaller than its entry-count header")
    count = struct.unpack(">I", count_data)[0]
    if not count or 4 + count * ENTRY_SIZE * 2 >= size:
        handle.close()
        raise ArchiveError(f"implausible texture count {count}")
    storage = read_table(handle, count)
    metadata = read_table(handle, count)
    data_base = handle.tell()
    for index, descriptor in enumerate(storage):
        offset, compressed_size = descriptor[0], descriptor[6]
        if offset + compressed_size > size - data_base:
            handle.close()
            raise ArchiveError(
                f"texture {index:04x} payload exceeds the archive: "
                f"0x{offset:x}+0x{compressed_size:x}"
            )
    return handle, storage, metadata, data_base


def parse_xmem_frames(blob: bytes, texture_id: int):
    frames = []
    position = 0
    while position < len(blob):
        if blob[position : position + 5] == b"\0" * 5:
            position += 5
            break
        if blob[position] == 0xFF:
            if position + 5 > len(blob):
                raise ArchiveError(f"texture {texture_id:04x} has a truncated XMem header")
            output_size = int.from_bytes(blob[position + 1 : position + 3], "big")
            compressed_size = int.from_bytes(blob[position + 3 : position + 5], "big")
            position += 5
        else:
            if position + 2 > len(blob):
                raise ArchiveError(f"texture {texture_id:04x} has a truncated XMem header")
            output_size = 0x8000
            compressed_size = int.from_bytes(blob[position : position + 2], "big")
            position += 2
        end = position + compressed_size
        if not output_size or not compressed_size or end > len(blob):
            raise ArchiveError(f"texture {texture_id:04x} has an invalid XMem frame")
        frames.append((output_size, memoryview(blob)[position:end]))
        position = end
    if position != len(blob):
        raise ArchiveError(
            f"texture {texture_id:04x} has {len(blob) - position} trailing payload bytes"
        )
    if not frames:
        raise ArchiveError(f"texture {texture_id:04x} has no XMem frames")
    return frames


def decompress_xmem(blob: bytes, expected_size: int, texture_id: int) -> bytes:
    decoder = LzxDecoder(False)
    decoder.set_params_and_alloc(XMEM_WINDOW_BITS)
    output = []
    total = 0
    for frame_index, (output_size, compressed) in enumerate(
        parse_xmem_frames(blob, texture_id)
    ):
        decoder.keep_history = frame_index != 0
        chunk = bytes(decoder.decompress(compressed, output_size))
        if len(chunk) != output_size:
            raise ArchiveError(
                f"texture {texture_id:04x} frame {frame_index} decoded to "
                f"{len(chunk)} bytes, expected {output_size}"
            )
        output.append(chunk)
        total += len(chunk)
    if total != expected_size:
        raise ArchiveError(
            f"texture {texture_id:04x} decoded to {total} bytes, expected {expected_size}"
        )
    return b"".join(output)


def swap_endian(block: bytes, endian: int) -> bytes:
    if endian == 0:
        return block
    if endian == 1:
        if len(block) & 1:
            raise ArchiveError("8in16 endian conversion requires an even block size")
        return b"".join(block[i : i + 2][::-1] for i in range(0, len(block), 2))
    if len(block) & 3:
        raise ArchiveError("32-bit endian conversion requires a four-byte block size")
    if endian == 2:
        return b"".join(block[i : i + 4][::-1] for i in range(0, len(block), 4))
    if endian == 3:
        return b"".join(
            block[i + 2 : i + 4] + block[i : i + 2]
            for i in range(0, len(block), 4)
        )
    raise ArchiveError(f"unsupported endian mode {endian}")


def tiled_2d_offset(x: int, y: int, pitch: int, log2_bytes_per_block: int) -> int:
    outer_blocks = (((y >> 5) * (pitch >> 5) + (x >> 5)) << 6)
    inner_blocks = (((y >> 1) & 0b111) << 3) | (x & 0b111)
    outer_inner_bytes = (outer_blocks | inner_blocks) << log2_bytes_per_block
    bank = (y >> 4) & 1
    pipe = ((x >> 3) & 0b11) ^ (((y >> 3) & 1) << 1)
    return (
        ((y & 1) << 4)
        | (pipe << 6)
        | (bank << 11)
        | (outer_inner_bytes & 0b1111)
        | (((outer_inner_bytes >> 4) & 1) << 5)
        | (((outer_inner_bytes >> 5) & 0b111) << 8)
        | ((outer_inner_bytes >> 8) << 12)
    )


def fetch_info(metadata: tuple[int, ...]) -> dict:
    preamble = metadata[:7]
    xg = metadata[7:13]
    dword_0, dword_1, dword_2, dword_3, dword_4, dword_5 = xg
    dimension = (dword_5 >> 9) & 3
    return {
        "preamble": [f"0x{word:08x}" for word in preamble],
        "fetch_dwords": [f"0x{word:08x}" for word in xg],
        "type": dword_0 & 3,
        "pitch": ((dword_0 >> 22) & 0x1FF) * 32,
        "tiled": bool(dword_0 >> 31),
        "format_id": dword_1 & 0x3F,
        "endian_id": (dword_1 >> 6) & 3,
        "fetch_width": (dword_2 & 0x1FFF) + 1,
        "fetch_height": ((dword_2 >> 13) & 0x1FFF) + 1,
        "swizzle": f"0x{((dword_3 >> 1) & 0xFFF):03x}",
        "mip_min": (dword_4 >> 2) & 0xF,
        "mip_max": (dword_4 >> 6) & 0xF,
        "dimension_id": dimension,
        "packed_mips": bool((dword_5 >> 11) & 1),
    }


def extract_base_surface(
    raw: bytes, info: dict, width: int, height: int, face_offset: int = 0
) -> bytes:
    format_id = info["format_id"]
    try:
        _, block_width, block_height, bytes_per_block, _ = FORMAT_INFO[format_id]
    except KeyError as exc:
        raise ArchiveError(f"unsupported Xenos texture format {format_id}") from exc
    width_blocks = (width + block_width - 1) // block_width
    height_blocks = (height + block_height - 1) // block_height
    pitch_pixels = info["pitch"] or ((width + 31) & ~31)
    pitch_blocks = (pitch_pixels + block_width - 1) // block_width
    result = bytearray(width_blocks * height_blocks * bytes_per_block)
    log2_bpb = bytes_per_block.bit_length() - 1
    for y in range(height_blocks):
        for x in range(width_blocks):
            if info["tiled"]:
                source = face_offset + tiled_2d_offset(x, y, pitch_blocks, log2_bpb)
            else:
                source = face_offset + (y * pitch_blocks + x) * bytes_per_block
            end = source + bytes_per_block
            if end > len(raw):
                raise ArchiveError(
                    f"base surface address 0x{source:x} exceeds 0x{len(raw):x} bytes"
                )
            destination = (y * width_blocks + x) * bytes_per_block
            result[destination : destination + bytes_per_block] = swap_endian(
                raw[source:end], info["endian_id"]
            )
    return bytes(result)


def make_dds(width: int, height: int, format_id: int, surface: bytes) -> bytes:
    name, _, _, _, fourcc = FORMAT_INFO[format_id]
    if fourcc:
        pixel_format = (32, 4, int.from_bytes(fourcc, "little"), 0, 0, 0, 0, 0)
        flags = 0x00081007
        pitch_or_linear_size = len(surface)
    elif name == "A8R8G8B8":
        pixel_format = (
            32,
            0x41,
            0,
            32,
            0x00FF0000,
            0x0000FF00,
            0x000000FF,
            0xFF000000,
        )
        flags = 0x0000100F
        pitch_or_linear_size = width * 4
    else:
        raise ArchiveError(f"DDS output is not implemented for {name}")
    header = struct.pack(
        "<7I11I8I5I",
        124,
        flags,
        height,
        width,
        pitch_or_linear_size,
        0,
        0,
        *([0] * 11),
        *pixel_format,
        0x1000,
        0,
        0,
        0,
        0,
    )
    return b"DDS " + header + surface


def save_png(path: Path, width: int, height: int, format_id: int, surface: bytes):
    try:
        from PIL import Image
    except ImportError as exc:
        raise ArchiveError("PNG output requires Pillow: python -m pip install Pillow") from exc
    if format_id == 6:
        image = Image.frombytes("RGBA", (width, height), surface, "raw", "BGRA")
    else:
        image = Image.open(io.BytesIO(make_dds(width, height, format_id, surface)))
        image.load()
    image.save(path)


def parse_number(value: str) -> int:
    value = value.strip().lower()
    if value.startswith("0x"):
        return int(value, 16)
    if any(character in "abcdef" for character in value):
        return int(value, 16)
    return int(value, 10)


def parse_ids(specification: str | None, count: int) -> list[int]:
    if not specification:
        return list(range(count))
    result = set()
    for part in specification.split(","):
        part = part.strip()
        if "-" in part:
            first, last = (parse_number(value) for value in part.split("-", 1))
            if last < first:
                raise ValueError(f"descending texture range {part!r}")
            result.update(range(first, last + 1))
        else:
            result.add(parse_number(part))
    invalid = [value for value in result if value < 0 or value >= count]
    if invalid:
        raise ValueError(f"texture IDs outside 0..{count - 1}: {invalid}")
    return sorted(result)


def manifest_entry(index: int, storage: tuple[int, ...], metadata: tuple[int, ...]):
    info = fetch_info(metadata)
    format_name = FORMAT_INFO.get(info["format_id"], ("unknown",))[0]
    return {
        "id": index,
        "id_hex": f"{index:04x}",
        "payload_offset": storage[0],
        "width": storage[1],
        "height": storage[2],
        "original_width": storage[3],
        "original_height": storage[4],
        "decoded_size": storage[5],
        "compressed_size": storage[6],
        "storage_flags": [f"0x{word:08x}" for word in storage[7:]],
        "format": format_name,
        "endian": ENDIAN_NAMES.get(info["endian_id"], "unknown"),
        "dimension": DIMENSION_NAMES.get(info["dimension_id"], "unknown"),
        **info,
    }


def run(args) -> int:
    archive = args.archive.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    handle, storage_table, metadata_table, data_base = parse_archive(archive)
    count = len(storage_table)
    selected = parse_ids(args.ids, count)
    if args.limit is not None:
        selected = selected[: args.limit]
    selected_set = set(selected)
    manifest = {
        "source": str(archive),
        "source_size": archive.stat().st_size,
        "entry_count": count,
        "data_base": data_base,
        "format_counts": dict(
            Counter(
                FORMAT_INFO.get(fetch_info(item)["format_id"], ("unknown",))[0]
                for item in metadata_table
            )
        ),
        "textures": [],
    }
    failures = []
    try:
        completed = 0
        for index, (storage, metadata) in enumerate(zip(storage_table, metadata_table)):
            entry = manifest_entry(index, storage, metadata)
            entry["selected"] = index in selected_set
            manifest["textures"].append(entry)
            if index not in selected_set or args.manifest_only:
                continue
            name = entry["id_hex"]
            format_id = entry["format_id"]
            if format_id not in FORMAT_INFO:
                failures.append(f"{name}: unsupported format {format_id}")
                continue
            handle.seek(data_base + storage[0])
            blob = handle.read(storage[6])
            try:
                raw = decompress_xmem(blob, storage[5], index)
                face_count = CUBE_FACE_COUNT if entry["dimension_id"] == 3 else 1
                if face_count > 1 and len(raw) % face_count:
                    raise ArchiveError("cubemap allocation is not divisible by six")
                face_stride = len(raw) // face_count
                files = []
                for face in range(face_count):
                    suffix = f"_face{face}" if face_count > 1 else ""
                    surface = extract_base_surface(
                        raw,
                        entry,
                        storage[1],
                        storage[2],
                        face * face_stride,
                    )
                    if args.output_format in ("dds", "both"):
                        path = output / f"{name}{suffix}.dds"
                        if args.overwrite or not path.exists():
                            path.write_bytes(
                                make_dds(storage[1], storage[2], format_id, surface)
                            )
                        files.append(path.name)
                    if args.output_format in ("png", "both"):
                        path = output / f"{name}{suffix}.png"
                        if args.overwrite or not path.exists():
                            save_png(path, storage[1], storage[2], format_id, surface)
                        files.append(path.name)
                entry["output_files"] = files
            except Exception as exc:
                failures.append(f"{name}: {exc}")
            completed += 1
            if args.progress_every and (
                completed == len(selected) or completed % args.progress_every == 0
            ):
                print(f"Extracted {completed}/{len(selected)} textures")
    finally:
        handle.close()
    manifest["failures"] = failures
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        f"Archive: {count} textures; selected: {len(selected)}; "
        f"failures: {len(failures)}; manifest: {manifest_path}"
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
    parser.add_argument("archive", type=Path, help="path to DataFiles/Textures.raw")
    parser.add_argument("--output", type=Path, required=True, help="output directory")
    parser.add_argument(
        "--output-format", choices=("png", "dds", "both"), default="png"
    )
    parser.add_argument(
        "--ids", help="comma-separated decimal/hex IDs and inclusive ranges"
    )
    parser.add_argument("--limit", type=int, help="extract only the first N selected IDs")
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
