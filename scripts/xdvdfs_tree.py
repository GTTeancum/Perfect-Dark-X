#!/usr/bin/env python3
"""
xdvdfs_tree.py — XDVDFS image builder with nested directory support.

pack-xiso.py's original builder only emits root-level files, which is fine for
the ROM layout (default.xbe + pd.ntsc-final.z64 + pd.ini). The loose-file
layout needs real directories: files/ (with a bgdata/ subdirectory) and segs/.

A directory entry's size depends only on its name length, so every directory
table can be sized before a single sector is assigned. That removes the
chicken-and-egg between "which sector does this subdirectory live in" and "how
large is the parent table", and lets sectors be handed out in one pass:

    0-31   system area
    32     volume descriptor
    33..   directory tables, parents before children
    then   file data

The BST entry encoder is reused from pack_xiso unchanged — it is already
proven, since the flat ROM image it produces reads correctly on hardware.
"""

import importlib.util
import sys
import os
import struct
from datetime import datetime, timezone

# pack-xiso.py has a hyphen in its name, so it cannot be imported normally.
_spec = importlib.util.spec_from_file_location(
    'pack_xiso', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pack-xiso.py'))
pack_xiso = importlib.util.module_from_spec(_spec)
sys.modules['pack_xiso'] = pack_xiso
_spec.loader.exec_module(pack_xiso)

from pack_xiso import (
    SECTOR_SIZE,
    VOLUME_DESCRIPTOR_SECTOR,
    MAGIC,
    ATTRIB_FILE,
    ATTRIB_DIR,
    _VD_MAGIC1_OFF,
    _VD_ROOT_SECTOR,
    _VD_ROOT_SIZE,
    _VD_FILETIME,
    _VD_MAGIC2_OFF,
    _sectors,
    _dt_to_filetime,
    _read_file,
    _entry_byte_size,
    _encode_directory,
)


class Node:
    __slots__ = ('name', 'is_dir', 'children', 'src', 'size', 'sector', 'table')

    def __init__(self, name, is_dir):
        self.name = name
        self.is_dir = is_dir
        self.children = {}      # UPPERCASE name -> Node
        self.src = None
        self.size = 0           # file: byte size / dir: table byte size
        self.sector = 0
        self.table = b''


def _add(root, disc_path, src):
    parts = [p for p in disc_path.replace('\\', '/').split('/') if p]
    node = root
    for part in parts[:-1]:
        key = part.upper()
        if key not in node.children:
            node.children[key] = Node(part, True)
        node = node.children[key]
        if not node.is_dir:
            raise SystemExit('path conflict at %s' % disc_path)

    leaf = Node(parts[-1], False)
    leaf.src = src
    leaf.size = len(src) if isinstance(src, (bytes, bytearray)) else os.path.getsize(src)
    node.children[parts[-1].upper()] = leaf


def _kids(node):
    # XDVDFS orders entries by uppercased name; the existing flat builder
    # relies on the same ordering and reads correctly on hardware.
    return sorted(node.children.values(), key=lambda n: n.name.upper())


def collect_tree(local_root):
    """Every file beneath local_root as (disc_relative_path, local_path)."""
    found = []
    for dirpath, _dirs, filenames in os.walk(local_root):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, local_root).replace(os.sep, '/')
            found.append((rel, full))
    return found


def build(out_path, entries, quiet=False):
    """entries : list of (disc_relative_path, local_path_or_bytes)"""
    root = Node('', True)
    for disc_path, src in entries:
        _add(root, disc_path, src)

    # Size every directory table up front. This includes any sector-boundary
    # gaps needed to keep individual XDVDFS entries within one sector.
    stack = [root]
    while stack:
        node = stack.pop()
        size_rows = [(c.name, 0, 0, ATTRIB_DIR if c.is_dir else ATTRIB_FILE)
                     for c in _kids(node)]
        node.size = len(_encode_directory(size_rows, base_offset=0))
        stack.extend(c for c in node.children.values() if c.is_dir)

    # directory tables: breadth-first, parents before children
    cursor = VOLUME_DESCRIPTOR_SECTOR + 1
    dirs = []
    queue = [root]
    while queue:
        node = queue.pop(0)
        node.sector = cursor
        cursor += max(1, _sectors(node.size))
        dirs.append(node)
        queue.extend(c for c in _kids(node) if c.is_dir)

    # then file data, in the exact order it will be written
    files = []

    def walk(node):
        for child in _kids(node):
            if child.is_dir:
                walk(child)
            else:
                files.append(child)

    walk(root)

    for f in files:
        f.sector = cursor
        cursor += _sectors(f.size)

    # encode tables now that every child sector is known
    for node in dirs:
        rows = [(c.name, c.sector, c.size, ATTRIB_DIR if c.is_dir else ATTRIB_FILE)
                for c in _kids(node)]
        node.table = _encode_directory(rows, base_offset=0)
        if len(node.table) != node.size:
            raise SystemExit('table size mismatch for %r: %d vs %d'
                             % (node.name, len(node.table), node.size))

    vol = bytearray(SECTOR_SIZE)
    vol[_VD_MAGIC1_OFF:_VD_MAGIC1_OFF + 20] = MAGIC
    struct.pack_into('<I', vol, _VD_ROOT_SECTOR, root.sector)
    struct.pack_into('<I', vol, _VD_ROOT_SIZE, root.size)
    struct.pack_into('<q', vol, _VD_FILETIME, _dt_to_filetime(datetime.now(timezone.utc)))
    vol[_VD_MAGIC2_OFF:_VD_MAGIC2_OFF + 20] = MAGIC

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or '.', exist_ok=True)
    with open(out_path, 'wb') as iso:
        iso.write(bytes(VOLUME_DESCRIPTOR_SECTOR * SECTOR_SIZE))
        iso.write(bytes(vol))
        at = VOLUME_DESCRIPTOR_SECTOR + 1

        for node in dirs:
            if node.sector != at:
                raise SystemExit('directory sector drift at %r' % node.name)
            span = max(1, _sectors(node.size)) * SECTOR_SIZE
            iso.write(node.table)
            iso.write(b'\xff' * (span - len(node.table)))
            at += span // SECTOR_SIZE

        for f in files:
            if f.sector != at:
                raise SystemExit('file sector drift at %r' % f.name)
            data = _read_file(f.src)
            if len(data) != f.size:
                raise SystemExit('file changed size while packing: %s' % f.name)
            iso.write(data)
            pad = (-len(data)) % SECTOR_SIZE
            if pad:
                iso.write(bytes(pad))
            at += _sectors(f.size)

    if not quiet:
        total = sum(f.size for f in files)
        print('  directories : %d' % len(dirs))
        print('  files       : %d  (%.1f MB)' % (len(files), total / 1048576.0))
        print('  root dir    : sector %d, %d bytes' % (root.sector, root.size))
        print('Written: %s  (%d bytes / %.1f MB)'
              % (out_path, os.path.getsize(out_path),
                 os.path.getsize(out_path) / 1048576.0))
