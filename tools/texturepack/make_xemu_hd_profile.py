#!/usr/bin/env python3
"""Create a disposable XEMU profile with deterministic dashboard video flags.

The source EEPROM and TOML are never modified. The user-settings CRC follows
the original Xbox EEPROM algorithm used by Cxbx-Reloaded and the dashboard.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import struct


EEPROM_SIZE = 256
USER_CHECKSUM_OFFSET = 0x60
USER_DATA_OFFSET = 0x64
USER_DATA_SIZE = 0x5C
VIDEO_FLAGS_OFFSET = 0x94

VIDEO_WIDESCREEN = 0x00010000
VIDEO_MODE_720P = 0x00020000
VIDEO_MODE_1080I = 0x00040000
VIDEO_MODE_480P = 0x00080000
VIDEO_SETTING_MASK = (
    VIDEO_WIDESCREEN | VIDEO_MODE_480P | VIDEO_MODE_720P | VIDEO_MODE_1080I
)


def eeprom_crc(data: bytes) -> bytes:
    """Return the four-byte Xbox EEPROM section checksum."""
    if len(data) < 2:
        raise ValueError("EEPROM checksum section is too short")
    rotated = data[-1:] + data[:-1]
    result = bytearray(4)
    for pos in range(4):
        value = 0xFFFF
        for offset in range(pos, len(data), 4):
            word = rotated[offset:offset + 2]
            if len(word) < 2:
                word += b"\0"
            value = (value - int.from_bytes(word, "little")) & 0xFFFF
        result[pos] = (value >> 8) & 0xFF
    return bytes(result)


def set_toml_string(config: str, section: str, key: str, value: str) -> str:
    """Set one string key without duplicating a TOML section."""
    header = f"[{section}]"
    section_match = re.search(
        rf"(?m)^\[{re.escape(section)}\]\s*$", config
    )
    assignment = f"{key} = '{value}'"
    if section_match is None:
        return config.rstrip() + f"\n\n{header}\n{assignment}\n"

    section_start = section_match.end()
    next_section = re.search(r"(?m)^\[[^\]]+\]\s*$", config[section_start:])
    section_end = (
        section_start + next_section.start() if next_section is not None
        else len(config)
    )
    block = config[section_start:section_end]
    block, count = re.subn(
        rf"(?m)^{re.escape(key)}\s*=\s*.*$", assignment, block, count=1
    )
    if count == 0:
        block = block.rstrip() + f"\n{assignment}\n"
    return config[:section_start] + block + config[section_end:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-eeprom", required=True, type=Path)
    parser.add_argument("--output-eeprom", required=True, type=Path)
    parser.add_argument("--source-config", required=True, type=Path)
    parser.add_argument("--output-config", required=True, type=Path)
    parser.add_argument(
        "--aspect", choices=("4:3", "16:9"), default="16:9",
        help="dashboard aspect ratio to encode (default: 16:9)",
    )
    parser.add_argument(
        "--resolution", choices=("480i", "480p", "720p"), default="720p",
        help="highest dashboard video mode to enable (default: 720p)",
    )
    args = parser.parse_args()

    if args.resolution == "720p" and args.aspect != "16:9":
        parser.error("720p requires the dashboard's 16:9 aspect setting")

    image = bytearray(args.source_eeprom.read_bytes())
    if len(image) != EEPROM_SIZE:
        raise SystemExit(
            f"expected a {EEPROM_SIZE}-byte EEPROM, got {len(image)} bytes"
        )

    old_flags = struct.unpack_from("<I", image, VIDEO_FLAGS_OFFSET)[0]
    requested_flags = VIDEO_WIDESCREEN if args.aspect == "16:9" else 0
    if args.resolution in ("480p", "720p"):
        requested_flags |= VIDEO_MODE_480P
    if args.resolution == "720p":
        requested_flags |= VIDEO_MODE_720P
    new_flags = (old_flags & ~VIDEO_SETTING_MASK) | requested_flags
    struct.pack_into("<I", image, VIDEO_FLAGS_OFFSET, new_flags)
    checksum = eeprom_crc(
        bytes(image[USER_DATA_OFFSET:USER_DATA_OFFSET + USER_DATA_SIZE])
    )
    image[USER_CHECKSUM_OFFSET:USER_CHECKSUM_OFFSET + 4] = checksum

    args.output_eeprom.parent.mkdir(parents=True, exist_ok=True)
    args.output_eeprom.write_bytes(image)

    config = args.source_config.read_text(encoding="utf-8")
    replacement = "eeprom_path = '" + str(args.output_eeprom.resolve()) + "'"
    config, count = re.subn(
        r"(?m)^eeprom_path\s*=\s*.*$", lambda _: replacement, config, count=1
    )
    if count != 1:
        raise SystemExit("source config has no unique eeprom_path setting")
    xemu_aspect = "16x9" if args.aspect == "16:9" else "4x3"
    xemu_window = "1280x720" if args.aspect == "16:9" else "1280x960"
    config = set_toml_string(config, "display.window", "startup_size", xemu_window)
    config = set_toml_string(config, "display.ui", "fit", "scale")
    config = set_toml_string(config, "display.ui", "aspect_ratio", xemu_aspect)
    args.output_config.parent.mkdir(parents=True, exist_ok=True)
    args.output_config.write_text(config, encoding="utf-8", newline="\n")

    print(f"source flags : 0x{old_flags:08x}")
    print(f"dashboard    : {args.aspect} {args.resolution}")
    print(f"output flags : 0x{new_flags:08x}")
    print(f"user CRC     : {checksum.hex()}")
    print(f"EEPROM       : {args.output_eeprom}")
    print(f"config       : {args.output_config}")
    print(f"XEMU display : {xemu_window} {xemu_aspect}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
