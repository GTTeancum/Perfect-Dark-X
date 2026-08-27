"""Minimal streaming XDVDFS writer for user-local Perfect Dark X images."""

from __future__ import annotations

import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


SECTOR_SIZE = 2048
VOLUME_DESCRIPTOR_SECTOR = 32
MAGIC = b"MICROSOFT*XBOX*MEDIA"
ATTRIB_FILE = 0x20
ATTRIB_DIR = 0x10
ProgressCallback = Callable[[int, str], None]
CancelCheck = Callable[[], None]


def _sectors(byte_count: int) -> int:
    return (byte_count + SECTOR_SIZE - 1) // SECTOR_SIZE


def _filetime_now() -> int:
    epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
    return int((datetime.now(timezone.utc) - epoch).total_seconds() * 10_000_000)


def _entry_size(name: str) -> int:
    raw = 14 + len(name.encode("ascii"))
    return (raw + 3) & ~3


def _encode_directory(rows: list[tuple[str, int, int, int]]) -> bytes:
    if not rows:
        return b""

    def make_tree(items):
        if not items:
            return None
        middle = len(items) // 2
        return {
            "row": items[middle],
            "left": make_tree(items[:middle]),
            "right": make_tree(items[middle + 1 :]),
            "offset": 0,
        }

    root = make_tree(rows)
    cursor = 0

    def assign(node) -> None:
        nonlocal cursor
        if node is None:
            return
        size = _entry_size(node["row"][0])
        sector_offset = cursor % SECTOR_SIZE
        if sector_offset and sector_offset + size > SECTOR_SIZE:
            cursor += SECTOR_SIZE - sector_offset
        node["offset"] = cursor
        cursor += size
        assign(node["left"])
        assign(node["right"])

    assign(root)
    table = bytearray(b"\xff" * cursor)

    def write(node) -> None:
        if node is None:
            return
        left = node["left"]["offset"] // 4 if node["left"] else 0
        right = node["right"]["offset"] // 4 if node["right"] else 0
        if left > 0xFFFF or right > 0xFFFF:
            raise ValueError("XDVDFS directory child offset exceeds its 16-bit field.")
        name, sector, size, attributes = node["row"]
        encoded_name = name.encode("ascii")
        entry = struct.pack(
            "<HHIIBB", left, right, sector, size, attributes, len(encoded_name)
        ) + encoded_name
        offset = node["offset"]
        table[offset : offset + len(entry)] = entry
        write(node["left"])
        write(node["right"])

    write(root)
    return bytes(table)


class Node:
    __slots__ = ("name", "is_dir", "children", "source", "size", "sector", "table")

    def __init__(self, name: str, is_dir: bool) -> None:
        self.name = name
        self.is_dir = is_dir
        self.children: dict[str, Node] = {}
        self.source: Path | None = None
        self.size = 0
        self.sector = 0
        self.table = b""


def _children(node: Node) -> list[Node]:
    return sorted(node.children.values(), key=lambda child: child.name.upper())


def _add(root: Node, relative: Path, source: Path) -> None:
    parts = relative.parts
    node = root
    for part in parts[:-1]:
        part.encode("ascii")
        key = part.upper()
        child = node.children.get(key)
        if child is None:
            child = Node(part, True)
            node.children[key] = child
        if not child.is_dir:
            raise ValueError(f"XDVDFS path conflict at {relative.as_posix()}")
        node = child

    name = parts[-1]
    name.encode("ascii")
    leaf = Node(name, False)
    leaf.source = source
    leaf.size = source.stat().st_size
    node.children[name.upper()] = leaf


def build_xiso(
    source_root: Path,
    output_path: Path,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> None:
    """Write every file under source_root to a new XDVDFS image."""
    source_root = source_root.resolve()
    output_path = output_path.resolve()
    root = Node("", True)
    source_files = sorted(
        (path for path in source_root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(source_root).as_posix().upper(),
    )
    if not source_files:
        raise ValueError("Cannot create an XISO from an empty install folder.")
    for source in source_files:
        if source.is_symlink():
            raise ValueError(f"Symbolic links are not supported in XDVDFS: {source}")
        _add(root, source.relative_to(source_root), source)

    stack = [root]
    while stack:
        node = stack.pop()
        size_rows = [
            (child.name, 0, 0, ATTRIB_DIR if child.is_dir else ATTRIB_FILE)
            for child in _children(node)
        ]
        node.size = len(_encode_directory(size_rows))
        stack.extend(child for child in node.children.values() if child.is_dir)

    cursor = VOLUME_DESCRIPTOR_SECTOR + 1
    directories: list[Node] = []
    queue = [root]
    while queue:
        node = queue.pop(0)
        node.sector = cursor
        cursor += max(1, _sectors(node.size))
        directories.append(node)
        queue.extend(child for child in _children(node) if child.is_dir)

    files: list[Node] = []

    def walk(node: Node) -> None:
        for child in _children(node):
            if child.is_dir:
                walk(child)
            else:
                files.append(child)

    walk(root)
    for file_node in files:
        file_node.sector = cursor
        cursor += _sectors(file_node.size)

    for node in directories:
        rows = [
            (
                child.name,
                child.sector,
                child.size,
                ATTRIB_DIR if child.is_dir else ATTRIB_FILE,
            )
            for child in _children(node)
        ]
        node.table = _encode_directory(rows)
        if len(node.table) != node.size:
            raise ValueError(f"XDVDFS directory size changed for {node.name!r}.")

    volume = bytearray(SECTOR_SIZE)
    volume[0:20] = MAGIC
    struct.pack_into("<I", volume, 20, root.sector)
    struct.pack_into("<I", volume, 24, root.size)
    struct.pack_into("<q", volume, 28, _filetime_now())
    volume[2028:2048] = MAGIC

    total_bytes = sum(node.size for node in files)
    written = 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("xb") as image:
        image.write(bytes(VOLUME_DESCRIPTOR_SECTOR * SECTOR_SIZE))
        image.write(volume)
        for node in directories:
            span = max(1, _sectors(node.size)) * SECTOR_SIZE
            image.write(node.table)
            image.write(b"\xff" * (span - len(node.table)))

        for index, node in enumerate(files, 1):
            if cancel_check:
                cancel_check()
            assert node.source is not None
            with node.source.open("rb") as source:
                while True:
                    if cancel_check:
                        cancel_check()
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    image.write(chunk)
                    written += len(chunk)
            padding = (-node.size) % SECTOR_SIZE
            if padding:
                image.write(bytes(padding))
            if progress and (index == 1 or index % 64 == 0 or index == len(files)):
                progress(
                    97 + int(2 * written / max(1, total_bytes)),
                    f"Creating local XISO ({index:,}/{len(files):,})...",
                )
