#!/usr/bin/env python3
"""
pack-xiso.py — Build a minimal XDVDFS ISO for Perfect Dark X on original Xbox.

Usage:
    python scripts/pack-xiso.py --xbe default.xbe --rom pd.ntsc-final.z64
    python scripts/pack-xiso.py --xbe default.xbe --rom pd.ntsc-final.z64 --out perfectdarkx.iso

Flags:
    --xbe   PATH    default.xbe downloaded from GitHub Actions CI artifact
    --rom   PATH    pd.ntsc-final.z64 (32 MB .z64 big-endian ROM)
    --gbc   PATH    pd.gbc (optional, unlocks in-game GBC feature)
    --out   PATH    output ISO path (default: perfectdarkx.iso)
    --romid STRING  ROM ID used in disc filename (default: ntsc-final)

Output: XDVDFS image ready for XEMU.
    Machine -> Settings -> DVD -> select the .iso

No external dependencies — pure Python 3.6+ standard library.
"""

import argparse
import os
import struct
import sys
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# XDVDFS constants
# ---------------------------------------------------------------------------

SECTOR_SIZE              = 2048
VOLUME_DESCRIPTOR_SECTOR = 32     # LBA of the Xbox volume descriptor
MAGIC                    = b"MICROSOFT*XBOX*MEDIA"  # 20 bytes

# File attributes
ATTRIB_FILE = 0x20
ATTRIB_DIR  = 0x10

# Volume descriptor field offsets (within sector 32)
_VD_MAGIC1_OFF     =    0   # 20 bytes
_VD_ROOT_SECTOR    =   20   # u32 LE
_VD_ROOT_SIZE      =   24   # u32 LE
_VD_FILETIME       =   28   # i64 LE (Windows FILETIME)
_VD_MAGIC2_OFF     = 2028   # 20 bytes  (must be exactly at end of sector)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sectors(byte_count: int) -> int:
    return (byte_count + SECTOR_SIZE - 1) // SECTOR_SIZE

def _dt_to_filetime(dt: datetime) -> int:
    """Convert a datetime to Windows FILETIME (100-ns intervals since 1601-01-01 UTC)."""
    epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
    return int((dt - epoch).total_seconds() * 10_000_000)

def _read_file(path_or_bytes) -> bytes:
    if isinstance(path_or_bytes, (bytes, bytearray)):
        return bytes(path_or_bytes)
    with open(path_or_bytes, 'rb') as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Directory encoder (BST)
#
# XDVDFS directories are right-skewed BSTs.  Each entry's left/right fields
# are DWORD (4-byte) offsets from the start of the directory sector to the
# child entry.  0xFFFF means "no child".
#
# We use a balanced BST: the median of the sorted file list is stored at
# byte offset 0 (the tree root), left subtree follows, right subtree follows.
# This gives O(log n) lookup and is what most XISO tooling produces.
# ---------------------------------------------------------------------------

def _entry_byte_size(name: str) -> int:
    """Padded byte size of one directory entry."""
    # 2(left) + 2(right) + 4(sector) + 4(size) + 1(attribs) + 1(name_len) + name
    raw = 14 + len(name.encode('ascii'))
    return (raw + 3) & ~3   # DWORD-aligned

def _subtree_byte_size(files: list) -> int:
    return sum(_entry_byte_size(f[0]) for f in files)

def _encode_directory(files: list, base_offset: int = 0) -> bytes:
    """
    Recursively encode files as a XDVDFS BST directory.

    files       : list of (name, start_sector, byte_size, attributes),
                  must be sorted case-insensitively by name.
    base_offset : byte position of the root entry within the directory blob
                  (0 for the top-level call).

    Returns the raw directory bytes; the root entry is always first.
    """
    if not files:
        return b''

    mid   = len(files) // 2
    root  = files[mid]
    left  = files[:mid]
    right = files[mid + 1:]

    root_sz     = _entry_byte_size(root[0])
    left_start  = base_offset + root_sz
    right_start = left_start + _subtree_byte_size(left)

    left_dword  = (left_start  // 4) if left  else 0xFFFF
    right_dword = (right_start // 4) if right else 0xFFFF

    name_bytes = root[0].encode('ascii')
    entry  = struct.pack('<HHIIB',
                left_dword, right_dword,
                root[1],    # start_sector
                root[2],    # byte size
                root[3],    # attributes
             )
    entry += bytes([len(name_bytes)])
    entry += name_bytes
    entry += b'\x00' * ((-len(entry)) % 4)   # DWORD pad

    assert len(entry) == root_sz

    return (entry
            + _encode_directory(left,  left_start)
            + _encode_directory(right, right_start))


# ---------------------------------------------------------------------------
# ISO builder
# ---------------------------------------------------------------------------

def build_iso(out_path: str, disc_files: list) -> None:
    """
    disc_files : list of (disc_filename, local_path_or_bytes)
                 The ROM and XBE must already be in this list.
    """
    # Sort case-insensitively (required by XDVDFS BST)
    disc_files = sorted(disc_files, key=lambda x: x[0].upper())

    # Load all file data
    loaded = []  # (disc_name, data)
    for disc_name, src in disc_files:
        data = _read_file(src)
        loaded.append((disc_name, data))

    # Sector layout
    # 0-31  : system area (zeros)
    # 32    : volume descriptor
    # 33    : root directory  (1 sector is plenty for < 20 files)
    # 34+   : file data in sorted order

    dir_sector  = VOLUME_DESCRIPTOR_SECTOR + 1   # 33

    # --- Pass 1: assume directory fits in 1 sector, assign file sectors ---
    file_sector_base = dir_sector + 1             # 34

    def assign_sectors(base):
        assignments = []   # (name, sector, size, attribs)
        cur = base
        for disc_name, data in loaded:
            assignments.append((disc_name, cur, len(data), ATTRIB_FILE))
            cur += _sectors(len(data))
        return assignments, cur

    assignments, last_sector = assign_sectors(file_sector_base)

    # Build directory
    dir_bytes   = _encode_directory(assignments, base_offset=0)
    dir_sectors = _sectors(len(dir_bytes))

    # --- Pass 2: if directory > 1 sector, re-base file sectors ---
    if dir_sectors > 1:
        file_sector_base = dir_sector + dir_sectors
        assignments, last_sector = assign_sectors(file_sector_base)
        dir_bytes = _encode_directory(assignments, base_offset=0)

    # Pad directory to a whole number of sectors (0xFF fill matches extract-xiso)
    dir_padded = dir_bytes + b'\xff' * (dir_sectors * SECTOR_SIZE - len(dir_bytes))

    # Build volume descriptor sector
    vol = bytearray(SECTOR_SIZE)
    vol[_VD_MAGIC1_OFF : _VD_MAGIC1_OFF + 20] = MAGIC
    struct.pack_into('<I', vol, _VD_ROOT_SECTOR, dir_sector)
    struct.pack_into('<I', vol, _VD_ROOT_SIZE,   len(dir_bytes))
    struct.pack_into('<q', vol, _VD_FILETIME,    _dt_to_filetime(datetime.now(timezone.utc)))
    vol[_VD_MAGIC2_OFF : _VD_MAGIC2_OFF + 20] = MAGIC

    # Stats
    total_mb = (last_sector * SECTOR_SIZE) / (1024 * 1024)
    print(f"Sector layout:")
    print(f"  Sectors  0–31   system area")
    print(f"  Sector   {VOLUME_DESCRIPTOR_SECTOR:2d}    volume descriptor")
    print(f"  Sector   {dir_sector:2d}    root directory  ({len(dir_bytes)} bytes, {dir_sectors} sector(s))")
    for disc_name, sector, size, _ in assignments:
        print(f"  Sector   {sector:2d}    {disc_name:<30s}  ({size:,} bytes)")
    print(f"  Total:         {last_sector} sectors  ({total_mb:.1f} MB)")
    print()

    # Write ISO
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or '.', exist_ok=True)
    with open(out_path, 'wb') as iso:
        # System area: sectors 0-31
        iso.write(b'\x00' * (VOLUME_DESCRIPTOR_SECTOR * SECTOR_SIZE))
        # Volume descriptor: sector 32
        iso.write(bytes(vol))
        # Root directory: sector 33
        iso.write(dir_padded)
        # File data in sorted order
        for _disc_name, data in loaded:
            iso.write(data)
            pad = (-len(data)) % SECTOR_SIZE
            iso.write(b'\x00' * pad)

    iso_size = os.path.getsize(out_path)
    print(f"Written: {out_path}  ({iso_size:,} bytes / {iso_size / 1024 / 1024:.1f} MB)")


# ---------------------------------------------------------------------------
# ROM validation
# ---------------------------------------------------------------------------

_ROM_SIZE     = 33_554_432       # 32 MB
_HEADER_ID    = b"NPDE"          # at offset 0x3B
_HEADER_TITLE = b"Perfect Dark"  # at offset 0x20

def validate_rom(path: str) -> None:
    size = os.path.getsize(path)
    if size != _ROM_SIZE:
        sys.exit(
            f"ERROR: ROM is {size:,} bytes; expected {_ROM_SIZE:,} (32 MB .z64).\n"
            f"Ensure the ROM is in .z64 (big-endian) format, not .n64 or .v64."
        )
    with open(path, 'rb') as fh:
        fh.seek(0x20)
        title = fh.read(12)
        fh.seek(0x3B)
        hdr_id = fh.read(4)
    if hdr_id != _HEADER_ID:
        sys.exit(
            f"ERROR: ROM header ID is {hdr_id!r}, expected {_HEADER_ID!r}.\n"
            f"Wrong region or wrong byte order (need .z64, not .n64/.v64)."
        )
    if not title.startswith(_HEADER_TITLE):
        sys.exit(
            f"ERROR: ROM title field is {title!r}, expected to start with {_HEADER_TITLE!r}.\n"
            f"This does not look like a Perfect Dark ROM."
        )


# ---------------------------------------------------------------------------
# Default pd.ini written to disc
# ---------------------------------------------------------------------------

_PD_INI = (
    b"[Game]\n"
    b"SkipIntro = 1\n"
    # 8 MB game heap: the 32 MB ROM is resident on a 64 MB Xbox, so a larger
    # heap can't get a contiguous block (matches g_OsMemSizeMb default).
    b"MemorySize = 8\n"
    b"\n"
    b"[Video]\n"
    b"DefaultFullscreen = 1\n"
    b"VSync = 1\n"
    b"FramerateLimit = 60\n"
    b"DisplayFPS = 0\n"
    b"\n"
    b"[Audio]\n"
    b"BufferSize = 512\n"
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument('--xbe',   required=True,            help='default.xbe (from CI artifact)')
    ap.add_argument('--rom',   required=True,            help='pd.ntsc-final.z64 (32 MB .z64)')
    ap.add_argument('--gbc',   default=None,             help='pd.gbc (optional GBC ROM)')
    ap.add_argument('--out',   default='perfectdarkx.iso', help='output ISO path')
    ap.add_argument('--romid', default='ntsc-final',     help='ROM suffix (default: ntsc-final)')
    args = ap.parse_args()

    # Validate inputs
    missing = []
    if not os.path.isfile(args.xbe):
        missing.append(f"XBE not found:  {args.xbe}")
    if not os.path.isfile(args.rom):
        missing.append(f"ROM not found:  {args.rom}")
    if missing:
        for m in missing:
            print(f"ERROR: {m}", file=sys.stderr)
        sys.exit(1)

    validate_rom(args.rom)

    rom_disc_name = f"pd.{args.romid}.z64"
    print(f"XBE : {args.xbe}")
    print(f"ROM : {args.rom}  ->  {rom_disc_name}")

    disc_files = [
        ('default.xbe', args.xbe),
        (rom_disc_name, args.rom),
        ('pd.ini',      _PD_INI),
    ]

    if args.gbc:
        if os.path.isfile(args.gbc):
            disc_files.append(('pd.gbc', args.gbc))
            print(f"GBC : {args.gbc}")
        else:
            print(f"WARNING: --gbc path not found, skipping: {args.gbc}")

    print()
    build_iso(args.out, disc_files)

    print()
    print("=== XEMU Usage ===")
    print(f"  1. Open XEMU")
    print(f"  2. Machine -> Settings -> DVD -> {os.path.abspath(args.out)}")
    print(f"  3. Machine -> Start")
    print()
    print("Expected boot sequence (see XBOX_PORT_PLAN.md for full detail):")
    print("  Phase 0   White text on black: 'Xbox entry point reached'")
    print("  Phase 1   crashInit OK (stub)")
    print("  Phase 2   sysInit OK - timer running")
    print("  Phase 3   fsInit OK - D:\\ accessible")
    print("  Phase 4   configInit OK")
    print("  Phase 5   Blue screen: 'videoInit OK - NV2A online'  <- pbkit is up")
    print("  Phase 6   inputInit OK - SDL gamepad ready")
    print("  Phase 7   audioInit OK - SDL audio device open")
    print("  Phase 8   romdataInit OK - ROM loaded (32 MB)")
    print("  Phase 9   gameInit OK - heap allocated")
    print("  Phase 10  bootCreateSched OK")
    print("  Phase 11  entering mainProc() - title screen next")
    print()
    print("Enable View -> Debug -> Serial (UART) in XEMU for log output.")


if __name__ == '__main__':
    main()
