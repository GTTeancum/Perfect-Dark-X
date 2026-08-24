#!/usr/bin/env python3
"""List or extract files from an Xbox 360 STFS LIVE/PIRS/CON package."""

from __future__ import annotations

import argparse
import struct
import sys
from dataclasses import dataclass
from pathlib import Path


BLOCK_SIZE = 0x1000
ENTRY_SIZE = 0x40
ENTRIES_PER_BLOCK = BLOCK_SIZE // ENTRY_SIZE


class StfsError(RuntimeError):
    pass


def int24_le(data: bytes) -> int:
    return int.from_bytes(data, "little")


def int24_be(data: bytes) -> int:
    return int.from_bytes(data, "big")


@dataclass
class Entry:
    index: int
    name: str
    is_directory: bool
    is_consecutive: bool
    block_count: int
    first_block: int
    parent_index: int
    size: int


class StfsPackage:
    def __init__(self, path: Path):
        self.path = path
        self.handle = path.open("rb")
        self.size = path.stat().st_size
        magic = self.read_at(0, 4)
        if magic not in (b"CON ", b"LIVE", b"PIRS"):
            self.close()
            raise StfsError(f"not an STFS package (magic {magic!r})")
        self.magic = magic.decode("ascii").strip()
        self.header_size = struct.unpack(">I", self.read_at(0x340, 4))[0]
        self.first_hash_address = (self.header_size + 0xFFF) & 0xFFFFF000
        block_separation = self.read_at(0x37B, 1)[0]
        self.shift = 0 if block_separation & 1 else 1
        self.file_table_block_count = struct.unpack(
            "<H", self.read_at(0x37C, 2)
        )[0]
        self.file_table_first_block = int24_le(self.read_at(0x37E, 3))
        display = self.read_at(0x411, 128)
        self.display_name = display.decode("utf-16-be", errors="replace").rstrip("\0")

    def close(self):
        if not self.handle.closed:
            self.handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def read_at(self, offset: int, size: int) -> bytes:
        if offset < 0 or offset + size > self.size:
            raise StfsError(
                f"read 0x{offset:x}..0x{offset + size:x} exceeds package size"
            )
        self.handle.seek(offset)
        data = self.handle.read(size)
        if len(data) != size:
            raise StfsError(f"short read at 0x{offset:x}")
        return data

    def backing_block_number(self, block: int) -> int:
        result = (((block + 0xAA) // 0xAA) << self.shift) + block
        if block < 0xAA:
            return result
        if block < 0x70E4:
            return result + (((block + 0x70E4) // 0x70E4) << self.shift)
        return (1 << self.shift) + result + (
            ((block + 0x70E4) // 0x70E4) << self.shift
        )

    def block_address(self, block: int) -> int:
        return (self.backing_block_number(block) << 12) + self.first_hash_address

    def level0_hash_block_number(self, block: int) -> int:
        if block < 0xAA:
            return 0
        step = 0xAB if self.shift == 0 else 0xAC
        result = (block // 0xAA) * step
        result += ((block // 0x70E4) + 1) << self.shift
        if block // 0x70E4 == 0:
            return result
        return result + (1 << self.shift)

    def next_block(self, block: int) -> int | None:
        hash_block = self.level0_hash_block_number(block)
        hash_address = (hash_block << 12) + self.first_hash_address
        record_address = hash_address + (block % 0xAA) * 0x18
        next_value = int24_be(self.read_at(record_address + 0x15, 3))
        return None if next_value >= 0xFFFFFE else next_value

    def entries(self) -> list[Entry]:
        entries = []
        current_block = self.file_table_first_block
        for block_index in range(self.file_table_block_count):
            address = self.block_address(current_block)
            for slot in range(ENTRIES_PER_BLOCK):
                raw = self.read_at(address + slot * ENTRY_SIZE, ENTRY_SIZE)
                name_length = raw[0x28] & 0x3F
                if not name_length:
                    continue
                name_data = raw[:name_length]
                if any(byte < 0x20 or byte > 0x7E for byte in name_data):
                    continue
                flags = raw[0x28]
                parent = int.from_bytes(raw[0x32:0x34], "big")
                entries.append(
                    Entry(
                        index=len(entries),
                        name=name_data.decode("ascii"),
                        is_directory=bool(flags & 0x80),
                        is_consecutive=bool(flags & 0x40),
                        block_count=int24_le(raw[0x29:0x2C]),
                        first_block=int24_le(raw[0x2F:0x32]),
                        parent_index=-1 if parent == 0xFFFF else parent,
                        size=int.from_bytes(raw[0x34:0x38], "big"),
                    )
                )
            if block_index + 1 < self.file_table_block_count:
                following = self.next_block(current_block)
                if following is None:
                    raise StfsError("file-table block chain ended early")
                current_block = following
        return entries

    @staticmethod
    def relative_path(entry: Entry, entries: list[Entry]) -> Path:
        parts = [entry.name]
        parent = entry.parent_index
        visited = set()
        while parent != -1:
            if parent in visited or parent < 0 or parent >= len(entries):
                raise StfsError(f"invalid directory chain for {entry.name!r}")
            visited.add(parent)
            directory = entries[parent]
            if not directory.is_directory:
                raise StfsError(f"parent of {entry.name!r} is not a directory")
            parts.insert(0, directory.name)
            parent = directory.parent_index
        if any(part in ("", ".", "..") or "/" in part or "\\" in part for part in parts):
            raise StfsError(f"unsafe STFS path for {entry.name!r}")
        return Path(*parts)

    def extract_file(self, entry: Entry, destination: Path):
        remaining = entry.size
        current_block = entry.first_block
        with destination.open("wb") as output:
            for block_index in range(entry.block_count):
                if not remaining:
                    break
                amount = min(remaining, BLOCK_SIZE)
                output.write(self.read_at(self.block_address(current_block), amount))
                remaining -= amount
                if remaining:
                    if entry.is_consecutive:
                        current_block += 1
                    else:
                        following = self.next_block(current_block)
                        if following is None:
                            raise StfsError(f"block chain ended early for {entry.name!r}")
                        current_block = following
        if remaining:
            raise StfsError(f"{entry.name!r} is missing {remaining} bytes")


def run(args) -> int:
    package_path = args.package.resolve()
    with StfsPackage(package_path) as package:
        entries = package.entries()
        paths = [(entry, package.relative_path(entry, entries)) for entry in entries]
        print(f"Package: {package.magic}; name: {package.display_name}")
        print(f"Entries: {len(entries)}")
        if args.list:
            for entry, relative in paths:
                kind = "DIR " if entry.is_directory else "FILE"
                suffix = "" if entry.is_directory else f" ({entry.size} bytes)"
                print(f"{kind}: {relative}{suffix}")
            return 0

        output = args.output.resolve()
        output.mkdir(parents=True, exist_ok=True)
        for entry, relative in paths:
            destination = output / relative
            if entry.is_directory:
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and not args.overwrite:
                raise StfsError(
                    f"refusing to overwrite {destination}; use --overwrite or a new directory"
                )
            print(f"Extracting {relative} ({entry.size} bytes)")
            package.extract_file(entry, destination)
        print(f"Extraction complete: {output}")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="path to the STFS package")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="list package contents")
    mode.add_argument("--output", type=Path, help="extract into this directory")
    parser.add_argument("--overwrite", action="store_true")
    return parser


if __name__ == "__main__":
    try:
        raise SystemExit(run(build_parser().parse_args()))
    except (OSError, StfsError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
