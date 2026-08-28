"""Transactional, asset-clean installer backend for Perfect Dark X.

The distributable core contains no game data. This module validates an
owner-supplied NTSC-final ROM, extracts the loose runtime tree, copies the
separately distributed XBE, and optionally installs a separate texture pack.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import threading
import uuid
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

try:
    from .xdvdfs import build_xiso
except ImportError:
    from xdvdfs import build_xiso


ROM_SIZE = 33_554_432
ROM_MD5 = "e03b088b6ac9e0080440efed07c1e40f"
ROM_DATA_OFFSET = 0x39850
ROM_FILES_OFFSET = 0x28080
TEXTURE_MAGIC = b"PDTXPAK1"
TEXTURE_ENTRY = struct.Struct("<IIIHH")
XBOX_TITLE_ID = 0x50440001
DASHBOARD_ASSET_NAMES = ("TitleImage.xbx", "SaveImage.xbx", "TitleMeta.xbx")

# Generated from ROMSEG_LIST in port/src/romdata.c for ntsc-final. Entries
# with a zero declared size extend to the next live segment.
NTSC_FINAL_SEGMENTS = (
    ("fontjpnsingle", 0x194B20, 0),
    ("fontjpnmulti", 0x19FB40, 0),
    ("animations", 0x1A15C0, 0),
    ("mpconfigs", 0x7D0A40, 0x11E0),
    ("mpstringsE", 0x7D1C20, 0x3700),
    ("mpstringsJ", 0x7D5320, 0x3700),
    ("mpstringsP", 0x7D8A20, 0x3700),
    ("mpstringsG", 0x7DC120, 0x3700),
    ("mpstringsF", 0x7DF820, 0x3700),
    ("mpstringsS", 0x7E2F20, 0x3700),
    ("mpstringsI", 0x7E6620, 0x3700),
    ("firingrange", 0x7E9D20, 0x1550),
    ("fonttahoma", 0x7F7860, 0),
    ("fontnumeric", 0x7F8B20, 0),
    ("fonthandelgothicsm", 0x7F9D30, 0),
    ("fonthandelgothicxs", 0x7FBFB0, 0),
    ("fonthandelgothicmd", 0x7FDD80, 0),
    ("fonthandelgothiclg", 0x8008E0, 0),
    ("sfxctl", 0x80A250, 0x2FB80),
    ("sfxtbl", 0x839DD0, 0x4C2160),
    ("seqctl", 0xCFBF30, 0xA060),
    ("seqtbl", 0xD05F90, 0x17C070),
    ("sequences", 0xE82000, 0x563A0),
    ("texturesdata", 0x1D65F40, 0),
    ("textureslist", 0x1FF7CA0, 0),
    ("copyright", 0x1FFEA20, 0xB30),
)

ProgressCallback = Callable[[int, str], None]


class InstallCancelled(RuntimeError):
    """Raised when the user cancels an active installation."""


@dataclass(frozen=True)
class InstallRequest:
    xbe: Path
    rom: Path
    output: Path
    texture_pack: Path | None = None
    create_xiso: bool = False


@dataclass(frozen=True)
class InstallResult:
    output: Path
    file_count: int
    segment_count: int
    texture_pack_installed: bool
    rom_md5: str
    texture_pack_sha256: str | None
    xiso: Path | None


def _emit(callback: ProgressCallback | None, percent: int, message: str) -> None:
    if callback:
        callback(max(0, min(100, int(percent))), message)


def _cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event and cancel_event.is_set():
        raise InstallCancelled("Installation cancelled.")


def _safe_relative(name: str) -> Path:
    value = name.replace("\\", "/")
    relative = PurePosixPath(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError(f"ROM contains an unsafe file name: {name!r}")
    if any(part in ("", ".") for part in relative.parts):
        raise ValueError(f"ROM contains an invalid file name: {name!r}")
    return Path(*relative.parts)


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _hash_file(
    path: Path,
    algorithm: str,
    progress: ProgressCallback | None,
    start: int,
    end: int,
    label: str,
    cancel_event: threading.Event | None,
) -> str:
    digest = hashlib.new(algorithm)
    size = path.stat().st_size
    done = 0
    with path.open("rb") as source:
        while True:
            _cancelled(cancel_event)
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            done += len(chunk)
            fraction = done / size if size else 1.0
            _emit(progress, start + int((end - start) * fraction), label)
    return digest.hexdigest()


def validate_xbe(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"Core XBE not found: {path}")
    if path.stat().st_size < 4096:
        raise ValueError("The selected core XBE is too small.")
    data = path.read_bytes()
    if data[:4] != b"XBEH":
        raise ValueError("The selected core file is not an Xbox executable.")
    image_base = struct.unpack_from("<I", data, 0x104)[0]
    certificate_va = struct.unpack_from("<I", data, 0x118)[0]
    title_id_offset = certificate_va - image_base + 8
    if title_id_offset < 0 or title_id_offset + 4 > len(data):
        raise ValueError("The selected core XBE has an invalid certificate.")
    title_id = struct.unpack_from("<I", data, title_id_offset)[0]
    if title_id != XBOX_TITLE_ID:
        raise ValueError(
            f"The core XBE uses title ID 0x{title_id:08X}; "
            f"expected 0x{XBOX_TITLE_ID:08X}."
        )


def validate_dashboard_assets(directory: Path) -> tuple[Path, ...]:
    assets = tuple(directory / name for name in DASHBOARD_ASSET_NAMES)
    expected_sizes = {"TitleImage.xbx": 10_240, "SaveImage.xbx": 4_096}
    for asset in assets:
        if not asset.is_file():
            raise FileNotFoundError(f"Xbox dashboard asset not found: {asset}")
        data = asset.read_bytes()
        expected_size = expected_sizes.get(asset.name)
        if expected_size is not None and (
            len(data) != expected_size or data[:4] != b"XPR0"
        ):
            raise ValueError(f"Invalid Xbox dashboard image: {asset}")
        if asset.name == "TitleMeta.xbx" and (
            not data.startswith(b"\xff\xfe")
            or "TitleName=Perfect Dark" not in data.decode("utf-16").splitlines()
        ):
            raise ValueError(f"Invalid Xbox title metadata: {asset}")
    return assets


def validate_rom(
    path: Path,
    progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"ROM not found: {path}")
    if path.stat().st_size != ROM_SIZE:
        raise ValueError(
            f"The ROM is {path.stat().st_size:,} bytes; expected {ROM_SIZE:,} "
            "bytes for an NTSC-final .z64 ROM."
        )
    digest = _hash_file(
        path, "md5", progress, 3, 15, "Verifying NTSC-final ROM...", cancel_event
    )
    if digest.lower() != ROM_MD5:
        raise ValueError(
            "ROM checksum mismatch. Perfect Dark X currently requires the "
            f"NTSC-final/US v1.1 .z64 ROM (MD5 {ROM_MD5})."
        )
    return digest.lower()


def _rzip_inflate(data: bytes) -> bytes:
    if len(data) < 5 or data[:2] != b"\x11\x73":
        raise ValueError("The ROM data segment is not rzip-compressed.")
    try:
        return zlib.decompress(data[5:], wbits=-15)
    except zlib.error:
        inflater = zlib.decompressobj(-15)
        result = inflater.decompress(data[5:])
        result += inflater.flush()
        return result


def _read_file_table(rom: bytes, inflated_data: bytes) -> tuple[list[int], dict[int, str]]:
    offsets = [0]
    while True:
        pos = ROM_FILES_OFFSET + 4 * len(offsets)
        if pos + 4 > len(inflated_data):
            raise ValueError("ROM file-offset table is truncated.")
        value = struct.unpack_from(">I", inflated_data, pos)[0]
        if value == 0:
            break
        if value > len(rom):
            raise ValueError("ROM file-offset table points outside the ROM.")
        offsets.append(value)
        if len(offsets) > 10_000:
            raise ValueError("ROM file-offset table did not terminate.")

    if len(offsets) < 3:
        raise ValueError("ROM file-offset table is empty.")
    name_table = offsets[-1]
    names: dict[int, str] = {}
    index = 1

    while True:
        table_pos = name_table + 4 * index
        if table_pos + 4 > len(rom):
            raise ValueError("ROM file-name table is truncated.")
        value = struct.unpack_from(">I", rom, table_pos)[0]
        if value == 0:
            break
        string_pos = name_table + value
        if string_pos >= len(rom):
            raise ValueError("ROM file-name table points outside the ROM.")
        try:
            end = rom.index(b"\0", string_pos)
        except ValueError as exc:
            raise ValueError("ROM file-name table contains an unterminated name.") from exc
        names[index] = rom[string_pos:end].decode("ascii", "strict")
        index += 1
        if index > 10_000:
            raise ValueError("ROM file-name table did not terminate.")

    return offsets, names


def extract_rom_assets(
    rom_path: Path,
    destination: Path,
    progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[int, int]:
    _cancelled(cancel_event)
    _emit(progress, 16, "Reading ROM...")
    rom = rom_path.read_bytes()
    _cancelled(cancel_event)
    _emit(progress, 18, "Inflating ROM data table...")
    inflated_data = _rzip_inflate(rom[ROM_DATA_OFFSET:])
    offsets, names = _read_file_table(rom, inflated_data)

    file_total = len(offsets) - 2
    file_count = 0
    name_list: list[str] = []
    for index in range(1, len(offsets) - 1):
        _cancelled(cancel_event)
        name = names.get(index)
        start, end = offsets[index], offsets[index + 1]
        if not name:
            name_list.append(f"_unused_{index:04d}")
        else:
            name_list.append(name)
            if end > start and end <= len(rom):
                relative = _safe_relative(name)
                _write(destination / "files" / relative, rom[start:end])
                file_count += 1
        if index == 1 or index % 32 == 0 or index == file_total:
            fraction = index / max(1, file_total)
            _emit(
                progress,
                20 + int(58 * fraction),
                f"Extracting ROM files ({index:,}/{file_total:,})...",
            )

    _write(
        destination / "filenames.lst",
        ("\n".join(name_list) + "\n").encode("ascii"),
    )

    segment_count = 0
    for index, (name, offset, declared_size) in enumerate(NTSC_FINAL_SEGMENTS):
        _cancelled(cancel_event)
        next_offset = (
            NTSC_FINAL_SEGMENTS[index + 1][1]
            if index + 1 < len(NTSC_FINAL_SEGMENTS)
            else len(rom)
        )
        size = declared_size or (next_offset - offset)
        if offset <= 0 or size <= 0 or offset + size > len(rom):
            raise ValueError(f"Invalid embedded segment definition for {name}.")
        _write(destination / "segs" / name, rom[offset : offset + size])
        segment_count += 1
        _emit(
            progress,
            79 + int(8 * (index + 1) / len(NTSC_FINAL_SEGMENTS)),
            f"Extracting runtime segments ({index + 1}/{len(NTSC_FINAL_SEGMENTS)})...",
        )

    return file_count, segment_count


def validate_texture_pack(
    path: Path,
    progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"Texture pack not found: {path}")
    sha256 = _hash_file(
        path, "sha256", progress, 88, 89, "Hashing texture pack...", cancel_event
    )
    archive_size = path.stat().st_size
    with path.open("rb") as archive:
        if archive.read(len(TEXTURE_MAGIC)) != TEXTURE_MAGIC:
            raise ValueError("The selected file is not a Perfect Dark X texture pack.")
        count_data = archive.read(4)
        if len(count_data) != 4:
            raise ValueError("The texture pack header is truncated.")
        entry_count = struct.unpack("<I", count_data)[0]
        if entry_count == 0 or entry_count > 65_536:
            raise ValueError("The texture pack entry count is invalid.")
        table_offset = len(TEXTURE_MAGIC) + 4
        table_size = entry_count * TEXTURE_ENTRY.size
        table = archive.read(table_size)
        if len(table) != table_size:
            raise ValueError("The texture pack table is truncated.")

        present = 0
        for index in range(entry_count):
            _cancelled(cancel_event)
            offset, compressed_size, raw_size, width, height = TEXTURE_ENTRY.unpack_from(
                table, index * TEXTURE_ENTRY.size
            )
            if compressed_size == 0:
                if (offset, raw_size, width, height) != (0, 0, 0, 0):
                    raise ValueError(f"Texture pack slot {index:04x} is malformed.")
                continue
            if width == 0 or height == 0 or raw_size != width * height * 4:
                raise ValueError(f"Texture pack slot {index:04x} has invalid dimensions.")
            if offset < table_offset + table_size or offset + compressed_size > archive_size:
                raise ValueError(f"Texture pack slot {index:04x} points outside the archive.")
            archive.seek(offset)
            compressed = archive.read(compressed_size)
            try:
                raw = zlib.decompress(compressed)
            except zlib.error as exc:
                raise ValueError(f"Texture pack slot {index:04x} is corrupt.") from exc
            if len(raw) != raw_size:
                raise ValueError(f"Texture pack slot {index:04x} has a size mismatch.")
            present += 1
            if present % 64 == 0:
                _emit(progress, 89 + int(5 * index / entry_count), "Verifying texture pack...")
    if present == 0:
        raise ValueError("The texture pack contains no replacement textures.")
    _emit(progress, 94, f"Verified {present:,} replacement textures.")
    return sha256


def _copy_with_progress(
    source: Path,
    destination: Path,
    progress: ProgressCallback | None,
    start: int,
    end: int,
    message: str,
    cancel_event: threading.Event | None,
) -> None:
    total = source.stat().st_size
    done = 0
    with source.open("rb") as reader, destination.open("wb") as writer:
        while True:
            _cancelled(cancel_event)
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            writer.write(chunk)
            done += len(chunk)
            _emit(progress, start + int((end - start) * done / max(1, total)), message)


def install_game(
    request: InstallRequest,
    progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> InstallResult:
    xbe = request.xbe.expanduser().resolve()
    rom = request.rom.expanduser().resolve()
    output = request.output.expanduser().resolve()
    texture_pack = request.texture_pack.expanduser().resolve() if request.texture_pack else None
    xiso_output = output.with_suffix(".iso") if request.create_xiso else None

    if output.parent == output:
        raise ValueError("Choose a game folder, not a drive root.")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("The output folder must be new or empty.")
    if xiso_output and xiso_output.exists():
        raise ValueError(f"The local XISO already exists: {xiso_output}")

    output.parent.mkdir(parents=True, exist_ok=True)
    stage = output.parent / f".{output.name}.pdx-install-{uuid.uuid4().hex}"
    xiso_stage = (
        output.parent / f".{output.name}.pdx-xiso-{uuid.uuid4().hex}.iso"
        if xiso_output
        else None
    )
    if stage.exists():
        raise FileExistsError(f"Temporary install path already exists: {stage}")

    _emit(progress, 1, "Validating Perfect Dark X core...")
    validate_xbe(xbe)
    dashboard_assets = validate_dashboard_assets(xbe.parent)
    rom_md5 = validate_rom(rom, progress, cancel_event)
    stage.mkdir()

    published_output = False
    published_xiso = False
    try:
        file_count, segment_count = extract_rom_assets(rom, stage, progress, cancel_event)
        _cancelled(cancel_event)
        _emit(progress, 87, "Installing default.xbe...")
        shutil.copy2(xbe, stage / "default.xbe")
        for asset in dashboard_assets:
            shutil.copy2(asset, stage / asset.name)

        texture_sha256 = None
        if texture_pack:
            texture_sha256 = validate_texture_pack(texture_pack, progress, cancel_event)
            _copy_with_progress(
                texture_pack,
                stage / "ext_tex.pak",
                progress,
                94,
                96,
                "Installing separate texture pack...",
                cancel_event,
            )

        _cancelled(cancel_event)
        manifest = {
            "format": 1,
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "rom": {"version": "ntsc-final", "md5": rom_md5},
            "core": {"file": "default.xbe", "sha256": hashlib.sha256(xbe.read_bytes()).hexdigest()},
            "dashboard_assets": [asset.name for asset in dashboard_assets],
            "texture_pack": (
                {"file": "ext_tex.pak", "sha256": texture_sha256}
                if texture_sha256
                else None
            ),
            "files": file_count,
            "segments": segment_count,
            "local_xiso_created": request.create_xiso,
        }
        (stage / "pdx-install.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
        )

        if xiso_stage:
            _emit(progress, 97, "Creating user-local XDVDFS image...")
            build_xiso(
                stage,
                xiso_stage,
                progress=progress,
                cancel_check=lambda: _cancelled(cancel_event),
            )
            if xiso_stage.stat().st_size < 1024 * 1024:
                raise ValueError("The generated local XISO is unexpectedly small.")

        if output.exists():
            output.rmdir()
        stage.replace(output)
        published_output = True
        if xiso_stage and xiso_output:
            xiso_stage.replace(xiso_output)
            published_xiso = True
        _emit(progress, 100, "Installation complete.")
        return InstallResult(
            output=output,
            file_count=file_count,
            segment_count=segment_count,
            texture_pack_installed=texture_pack is not None,
            rom_md5=rom_md5,
            texture_pack_sha256=texture_sha256,
            xiso=xiso_output,
        )
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        if xiso_stage:
            xiso_stage.unlink(missing_ok=True)
        if published_xiso and xiso_output:
            xiso_output.unlink(missing_ok=True)
        if published_output:
            shutil.rmtree(output, ignore_errors=True)
        raise
