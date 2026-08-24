#!/usr/bin/env python3
"""Decode Perfect Dark's extracted N64 texture payloads into reference PNGs."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zlib
from collections import Counter
from pathlib import Path


FORMAT_NAMES = (
    "RGBA32",
    "RGBA16",
    "RGB24",
    "RGB15",
    "IA16",
    "IA8",
    "IA4",
    "I8",
    "I4",
    "RGBA16_CI8",
    "RGBA16_CI4",
    "IA16_CI8",
    "IA16_CI4",
)
FORMAT_CHANNELS = (4, 3, 3, 3, 2, 2, 1, 1, 1, 1, 1, 1, 1)
FORMAT_ONE_BIT_ALPHA = (0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0)
FORMAT_CHANNEL_SIZES = (256, 32, 256, 32, 256, 16, 8, 256, 16, 256, 16, 256, 16)
FORMAT_BITS = (32, 16, 24, 15, 16, 8, 4, 8, 4, 16, 16, 16, 16)
COMPRESSION_NAMES = (
    "uncompressed0",
    "uncompressed1",
    "huffman",
    "huffman-per-channel",
    "rle",
    "lookup",
    "huffman-lookup",
    "rle-lookup",
    "huffman-blur",
    "rle-blur",
)
PALETTED_FORMATS = {9, 10, 11, 12}


class TextureError(RuntimeError):
    pass


class BitReader:
    def __init__(self, data: bytes):
        self.data = data
        self.index = 0
        self.value = 0
        self.bits = 0

    def read(self, count: int) -> int:
        if count < 0:
            raise TextureError(f"invalid negative bit count {count}")
        if count == 0:
            return 0
        while self.bits < count:
            if self.index >= len(self.data):
                raise TextureError("texture bitstream ended early")
            self.value = (self.value << 8) | self.data[self.index]
            self.index += 1
            self.bits += 8
        self.bits -= count
        return (self.value >> self.bits) & ((1 << count) - 1)

    def remaining_bytes(self) -> bytes:
        if self.bits:
            raise TextureError("expected a byte-aligned bitstream")
        return self.data[self.index :]


def expand(value: int, bits: int) -> int:
    maximum = (1 << bits) - 1
    return (value * 255 + maximum // 2) // maximum


def packed_to_rgba(format_id: int, value: int) -> tuple[int, int, int, int]:
    if format_id == 0:
        return value >> 24, (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF
    if format_id in (1, 9, 10):
        return (
            expand((value >> 11) & 0x1F, 5),
            expand((value >> 6) & 0x1F, 5),
            expand((value >> 1) & 0x1F, 5),
            255 if value & 1 else 0,
        )
    if format_id == 2:
        return value >> 16, (value >> 8) & 0xFF, value & 0xFF, 255
    if format_id == 3:
        return (
            expand((value >> 10) & 0x1F, 5),
            expand((value >> 5) & 0x1F, 5),
            expand(value & 0x1F, 5),
            255,
        )
    if format_id in (4, 11, 12):
        intensity, alpha = value >> 8, value & 0xFF
        return intensity, intensity, intensity, alpha
    if format_id == 5:
        intensity, alpha = expand(value >> 4, 4), expand(value & 0xF, 4)
        return intensity, intensity, intensity, alpha
    if format_id == 6:
        intensity, alpha = expand(value >> 1, 3), 255 if value & 1 else 0
        return intensity, intensity, intensity, alpha
    if format_id == 7:
        return value, value, value, 255
    if format_id == 8:
        intensity = expand(value, 4)
        return intensity, intensity, intensity, 255
    raise TextureError(f"unsupported N64 texture format {format_id}")


def channels_to_rgba(format_id: int, channels: list[int], pixels: int) -> bytes:
    count = FORMAT_CHANNELS[format_id]
    output = bytearray(pixels * 4)
    for index in range(pixels):
        values = [channels[index + channel * pixels] for channel in range(count)]
        if format_id == 0:
            rgba = values[0], values[1], values[2], values[3]
        elif format_id == 1:
            alpha = channels[index + 3 * pixels]
            rgba = expand(values[0], 5), expand(values[1], 5), expand(values[2], 5), 255 if alpha else 0
        elif format_id == 2:
            rgba = values[0], values[1], values[2], 255
        elif format_id == 3:
            rgba = expand(values[0], 5), expand(values[1], 5), expand(values[2], 5), 255
        elif format_id == 4:
            rgba = values[0], values[0], values[0], values[1]
        elif format_id == 5:
            rgba = expand(values[0], 4), expand(values[0], 4), expand(values[0], 4), expand(values[1], 4)
        elif format_id == 6:
            alpha = channels[index + 3 * pixels]
            rgba = expand(values[0], 3), expand(values[0], 3), expand(values[0], 3), 255 if alpha else 0
        elif format_id == 7:
            rgba = values[0], values[0], values[0], 255
        elif format_id == 8:
            intensity = expand(values[0], 4)
            rgba = intensity, intensity, intensity, 255
        else:
            raise TextureError(f"channel compression is invalid for format {format_id}")
        offset = index * 4
        output[offset : offset + 4] = bytes(rgba)
    return bytes(output)


def inflate_huffman(reader: BitReader, iterations: int, channel_size: int) -> list[int]:
    frequencies = [reader.read(8) for _ in range(channel_size)] + [9999] * (2048 - channel_size)
    nodes = [[-1, -1] for _ in range(2048)]
    minimum_1 = minimum_2 = 9999
    index_1 = index_2 = 0
    for index in range(channel_size):
        frequency = frequencies[index]
        if frequency < minimum_1:
            if minimum_2 < minimum_1:
                minimum_1, index_1 = frequency, index
            else:
                minimum_2, index_2 = frequency, index
        elif frequency < minimum_2:
            minimum_2, index_2 = frequency, index

    root = 0
    while True:
        combined = frequencies[index_1] + frequencies[index_2]
        if combined == 0:
            combined = 1
        frequencies[index_1] = frequencies[index_2] = 9999
        if nodes[index_1] == [-1, -1]:
            nodes[index_1][0] = index_1 + 10000
            root = index_1
            frequencies[index_1] = combined
            nodes[index_1][1] = index_2 + 10000 if nodes[index_2] == [-1, -1] else index_2
        elif nodes[index_2] == [-1, -1]:
            nodes[index_2][0] = index_2 + 10000
            root = index_2
            frequencies[index_2] = combined
            nodes[index_2][1] = index_1 + 10000 if nodes[index_1] == [-1, -1] else index_1
        else:
            root = 0
            while nodes[root] != [-1, -1] or frequencies[root] < 9999:
                root += 1
            frequencies[root] = combined
            nodes[root] = [index_1, index_2]

        minimum_1 = minimum_2 = 9999
        for index in range(channel_size):
            frequency = frequencies[index]
            if frequency < minimum_1:
                if minimum_1 > minimum_2:
                    minimum_1, index_1 = frequency, index
                else:
                    minimum_2, index_2 = frequency, index
            elif frequency < minimum_2:
                minimum_2, index_2 = frequency, index
        if minimum_1 == 9999 or minimum_2 == 9999:
            break

    output = []
    for _ in range(iterations):
        node = root
        while node < 10000:
            node = nodes[node][reader.read(1)]
            if node < 0:
                raise TextureError("invalid Huffman tree traversal")
        output.append(node - 10000)
    return output


def inflate_rle(reader: BitReader, total: int) -> list[int]:
    backtrack_bits = reader.read(3)
    run_bits = reader.read(3)
    block_bits = reader.read(4)
    if not block_bits:
        raise TextureError("RLE block size is zero")
    cost = backtrack_bits + run_bits + block_bits + 1
    fudge = 0
    while cost > 0:
        cost -= block_bits + 1
        fudge += 1
    output = []
    while len(output) < total:
        if reader.read(1) == 0:
            output.append(reader.read(block_bits))
            continue
        start = len(output) - reader.read(backtrack_bits) - 1
        run = reader.read(run_bits) + fudge
        if start < 0 or len(output) + run >= total + 1:
            raise TextureError("invalid RLE back-reference")
        for index in range(start, start + run):
            output.append(output[index])
        if len(output) < total:
            output.append(reader.read(block_bits))
    if len(output) != total:
        raise TextureError(f"RLE decoded {len(output)} values, expected {total}")
    return output


def build_lookup(reader: BitReader, bits_per_pixel: int) -> list[int]:
    count = reader.read(11)
    if not count:
        raise TextureError("lookup table is empty")
    return [reader.read(bits_per_pixel) for _ in range(count)]


def bit_size(count: int) -> int:
    return max(0, (count - 1).bit_length())


def blur(values: list[int], width: int, height: int, method: int, channel_size: int):
    for y in range(height):
        for x in range(width):
            index = y * width + x
            current = values[index] + channel_size * 2
            left = values[index - 1] if x else 0
            above = values[index - width] if y else 0
            above_left = values[index - width - 1] if x and y else 0
            if method == 0:
                predictor = left
            elif method == 1:
                predictor = above
            elif method == 2:
                predictor = above_left
            elif method == 3:
                predictor = left + above - above_left
            elif method == 4:
                predictor = int((above - above_left) / 2) + left
            elif method == 5:
                predictor = int((left - above_left) / 2) + above
            elif method == 6:
                predictor = int((left + above) / 2)
            else:
                raise TextureError(f"invalid blur method {method}")
            values[index] = (current + predictor) % channel_size


def decode_zlib(reader: BitReader) -> dict:
    format_id = reader.read(8)
    if format_id not in PALETTED_FORMATS:
        raise TextureError(f"zlib texture uses non-paletted format {format_id}")
    palette_count = reader.read(8) + 1
    palette = [reader.read(16) for _ in range(palette_count)]
    width, height = reader.read(8), reader.read(8)
    compressed = reader.remaining_bytes()
    if len(compressed) < 6 or compressed[:2] != b"\x11\x73":
        raise TextureError("zlib texture has no Rarezip frame")
    try:
        indices = zlib.decompress(compressed[5:], wbits=-15)
    except zlib.error as exc:
        raise TextureError(f"Rarezip decode failed: {exc}") from exc
    pixels = width * height
    if format_id in (9, 11):
        if len(indices) < pixels:
            raise TextureError("CI8 index buffer is truncated")
        unpacked = indices[:pixels]
    else:
        required = (width + 1) // 2 * height
        if len(indices) < required:
            raise TextureError("CI4 index buffer is truncated")
        unpacked = bytearray()
        position = 0
        for _ in range(height):
            row = indices[position : position + (width + 1) // 2]
            position += (width + 1) // 2
            for x in range(width):
                packed = row[x // 2]
                unpacked.append(packed >> 4 if x % 2 == 0 else packed & 0xF)
    try:
        rgba = b"".join(bytes(packed_to_rgba(format_id, palette[index])) for index in unpacked)
    except IndexError as exc:
        raise TextureError("palette index exceeds palette size") from exc
    return {
        "format_id": format_id,
        "compression_id": "rarezip",
        "width": width,
        "height": height,
        "palette_count": palette_count,
        "rgba": rgba,
    }


def decode_non_zlib(reader: BitReader) -> dict:
    format_id = reader.read(4)
    width, height = reader.read(8), reader.read(8)
    compression = reader.read(4)
    if format_id >= len(FORMAT_NAMES):
        raise TextureError(f"invalid texture format {format_id}")
    if compression >= len(COMPRESSION_NAMES):
        raise TextureError(f"invalid compression method {compression}")
    pixels = width * height
    if not width or not height or pixels > 0x2000:
        raise TextureError(f"invalid texture dimensions {width}x{height}")

    if compression in (0, 1):
        packed = [reader.read(FORMAT_BITS[format_id]) for _ in range(pixels)]
        rgba = b"".join(bytes(packed_to_rgba(format_id, value)) for value in packed)
    elif compression in (2, 3, 4, 8, 9):
        channel_count = FORMAT_CHANNELS[format_id]
        channel_size = FORMAT_CHANNEL_SIZES[format_id]
        total = channel_count * pixels
        method = reader.read(3) if compression in (8, 9) else None
        if compression in (2, 8):
            channels = inflate_huffman(reader, total, channel_size)
        elif compression == 3:
            channels = []
            for _ in range(channel_count):
                channels.extend(inflate_huffman(reader, pixels, channel_size))
        else:
            channels = inflate_rle(reader, total)
        if compression in (8, 9):
            blur(channels, width, channel_count * height, method, channel_size)
        if FORMAT_ONE_BIT_ALPHA[format_id]:
            channels.extend(reader.read(1) for _ in range(pixels))
        rgba = channels_to_rgba(format_id, channels, pixels)
    elif compression in (5, 6, 7):
        lookup = build_lookup(reader, FORMAT_BITS[format_id])
        index_bits = bit_size(len(lookup))
        if compression == 5:
            indices = [reader.read(index_bits) for _ in range(pixels)]
        elif compression == 6:
            indices = inflate_huffman(reader, pixels, len(lookup))
        else:
            indices = inflate_rle(reader, pixels)
        try:
            rgba = b"".join(bytes(packed_to_rgba(format_id, lookup[index])) for index in indices)
        except IndexError as exc:
            raise TextureError("lookup index exceeds table size") from exc
    else:
        raise TextureError(f"unsupported compression method {compression}")
    return {
        "format_id": format_id,
        "compression_id": compression,
        "width": width,
        "height": height,
        "palette_count": None,
        "rgba": rgba,
    }


def decode_texture(data: bytes) -> dict:
    reader = BitReader(data)
    has_lod_data = bool(reader.read(1))
    is_zlib = bool(reader.read(1))
    declared_lods = reader.read(6)
    decoded = decode_zlib(reader) if is_zlib else decode_non_zlib(reader)
    decoded.update(
        {
            "has_lod_data": has_lod_data,
            "is_zlib": is_zlib,
            "declared_lods": declared_lods,
        }
    )
    return decoded


def image_metrics(rgba: bytes) -> dict:
    pixels = len(rgba) // 4
    opaque = transparent = translucent = 0
    for alpha in rgba[3::4]:
        if alpha == 255:
            opaque += 1
        elif alpha == 0:
            transparent += 1
        else:
            translucent += 1
    return {
        "opaque_pixels": opaque,
        "transparent_pixels": transparent,
        "translucent_pixels": translucent,
        "alpha_coverage": round((opaque + translucent) / pixels, 8),
    }


def run(args) -> int:
    try:
        from PIL import Image
    except ImportError as exc:
        raise TextureError("Pillow is required: python -m pip install Pillow") from exc

    source = args.source.resolve()
    metadata_path = args.metadata.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    manifest = {
        "source": str(source),
        "metadata": str(metadata_path),
        "entry_count": len(metadata),
        "textures": [],
        "failures": [],
    }
    for index, row in enumerate(metadata):
        texture_id = int(Path(row["file"]).stem, 16)
        path = source / row["file"]
        entry = {
            "id": texture_id,
            "id_hex": f"{texture_id:04x}",
            "symbol": row["id"],
            "source_file": row["file"],
            "source_size": path.stat().st_size,
            "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "flag00": row["flag00"],
            "surface_type": row["surfacetype"],
        }
        manifest["textures"].append(entry)
        if entry["source_size"] == 0:
            entry["status"] = "empty"
            continue
        try:
            decoded = decode_texture(path.read_bytes())
            rgba = decoded.pop("rgba")
            entry.update(decoded)
            entry["format"] = FORMAT_NAMES[entry["format_id"]]
            compression_id = entry["compression_id"]
            entry["compression"] = (
                compression_id
                if isinstance(compression_id, str)
                else COMPRESSION_NAMES[compression_id]
            )
            entry["rgba_sha256"] = hashlib.sha256(rgba).hexdigest()
            entry.update(image_metrics(rgba))
            output_path = output / f"{texture_id:04x}.png"
            if args.overwrite or not output_path.exists():
                Image.frombytes(
                    "RGBA", (entry["width"], entry["height"]), rgba
                ).save(output_path)
            entry["output_file"] = output_path.name
            entry["status"] = "decoded"
        except Exception as exc:
            manifest["failures"].append(f"{texture_id:04x}: {exc}")
        if args.progress_every and (
            index + 1 == len(metadata) or (index + 1) % args.progress_every == 0
        ):
            print(f"Decoded {index + 1}/{len(metadata)} stock textures")

    manifest["format_counts"] = dict(
        Counter(entry.get("format", "failed") for entry in manifest["textures"])
    )
    manifest["compression_counts"] = dict(
        Counter(entry.get("compression", "failed") for entry in manifest["textures"])
    )
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    failures = manifest["failures"]
    print(
        f"Stock textures: {len(metadata)}; failures: {len(failures)}; "
        f"manifest: {manifest_path}"
    )
    for failure in failures[:30]:
        print(f"ERROR: {failure}", file=sys.stderr)
    if len(failures) > 30:
        print(f"ERROR: {len(failures) - 30} more failures", file=sys.stderr)
    return 1 if failures else 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("src/assets/ntsc-final/textures"),
        help="directory containing the ROM-extracted texture .bin files",
    )
    parser.add_argument(
        "--metadata",
        type=Path,
        default=Path("src/assets/ntsc-final/textures.json"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--progress-every", type=int, default=250)
    return parser


if __name__ == "__main__":
    try:
        raise SystemExit(run(build_parser().parse_args()))
    except (OSError, TextureError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
