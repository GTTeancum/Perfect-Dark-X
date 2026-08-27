"""Create an allowlisted, ROM-clean Perfect Dark X release ZIP."""

from __future__ import annotations

import argparse
import hashlib
import struct
import tempfile
import zipfile
from pathlib import Path


RELEASE_NAMES = {
    "default.xbe",
    "PerfectDarkXAssetInstaller.exe",
    "ext_tex.pak",
    "readme.txt",
    "LICENSE",
    "SHA256SUMS.txt",
    "TitleImage.xbx",
    "SaveImage.xbx",
    "TitleMeta.xbx",
}
FORBIDDEN_SUFFIXES = {".iso", ".z64", ".n64", ".v64", ".bin", ".rom"}
XBOX_TITLE_ID = 0x41500001


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest().upper()


def validate_inputs(
    xbe: Path,
    installer: Path,
    texture_pack: Path,
    readme: Path,
    license_path: Path,
) -> None:
    for path in (xbe, installer, texture_pack, readme, license_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    with xbe.open("rb") as source:
        if source.read(4) != b"XBEH":
            raise ValueError("Core executable is not an XBE file.")
    xbe_data = xbe.read_bytes()
    image_base = struct.unpack_from("<I", xbe_data, 0x104)[0]
    certificate_va = struct.unpack_from("<I", xbe_data, 0x118)[0]
    title_id_offset = certificate_va - image_base + 8
    if title_id_offset < 0 or title_id_offset + 4 > len(xbe_data):
        raise ValueError("Core XBE has an invalid certificate.")
    title_id = struct.unpack_from("<I", xbe_data, title_id_offset)[0]
    if title_id != XBOX_TITLE_ID:
        raise ValueError(
            f"Core XBE title ID is 0x{title_id:08X}; expected 0x{XBOX_TITLE_ID:08X}."
        )
    with installer.open("rb") as source:
        if source.read(2) != b"MZ":
            raise ValueError("Asset installer is not a Windows executable.")
    with texture_pack.open("rb") as source:
        if source.read(8) != b"PDTXPAK1":
            raise ValueError("Texture pack is not a Perfect Dark X texture pack.")
    for name, expected_size in (("TitleImage.xbx", 10_240), ("SaveImage.xbx", 4_096)):
        asset = xbe.parent / name
        if not asset.is_file() or asset.stat().st_size != expected_size:
            raise ValueError(f"Missing or invalid Xbox dashboard asset: {asset}")
        if asset.read_bytes()[:4] != b"XPR0":
            raise ValueError(f"Xbox dashboard asset is not XPR0: {asset}")
    title_meta = xbe.parent / "TitleMeta.xbx"
    if not title_meta.is_file() or not title_meta.read_bytes().startswith(b"\xff\xfe"):
        raise ValueError(f"Missing or invalid Xbox title metadata: {title_meta}")


def package_core(
    xbe: Path,
    installer: Path,
    texture_pack: Path,
    readme: Path,
    license_path: Path,
    output: Path,
) -> Path:
    xbe = xbe.resolve()
    installer = installer.resolve()
    texture_pack = texture_pack.resolve()
    readme = readme.resolve()
    license_path = license_path.resolve()
    output = output.resolve()
    validate_inputs(xbe, installer, texture_pack, readme, license_path)
    if output.suffix.lower() != ".zip":
        raise ValueError("Core release output must use the .zip extension.")
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    sums = (
        f"{sha256(xbe)}  default.xbe\n"
        f"{sha256(installer)}  PerfectDarkXAssetInstaller.exe\n"
        f"{sha256(texture_pack)}  ext_tex.pak\n"
        f"{sha256(xbe.parent / 'TitleImage.xbx')}  TitleImage.xbx\n"
        f"{sha256(xbe.parent / 'SaveImage.xbx')}  SaveImage.xbx\n"
        f"{sha256(xbe.parent / 'TitleMeta.xbx')}  TitleMeta.xbx\n"
    ).encode("ascii")

    with tempfile.NamedTemporaryFile(
        prefix=f".{output.stem}-", suffix=".tmp", dir=output.parent, delete=False
    ) as temporary:
        temp_path = Path(temporary.name)

    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            archive.write(xbe, "default.xbe")
            archive.write(installer, "PerfectDarkXAssetInstaller.exe")
            archive.write(texture_pack, "ext_tex.pak")
            archive.write(xbe.parent / "TitleImage.xbx", "TitleImage.xbx")
            archive.write(xbe.parent / "SaveImage.xbx", "SaveImage.xbx")
            archive.write(xbe.parent / "TitleMeta.xbx", "TitleMeta.xbx")
            archive.write(readme, "readme.txt")
            archive.write(license_path, "LICENSE")
            archive.writestr("SHA256SUMS.txt", sums)

        with zipfile.ZipFile(temp_path, "r") as archive:
            names = set(archive.namelist())
            if names != RELEASE_NAMES:
                raise ValueError(f"Core release allowlist mismatch: {sorted(names)}")
            for name in names:
                if Path(name).suffix.lower() in FORBIDDEN_SUFFIXES:
                    raise ValueError(f"Asset-bearing file entered core release: {name}")
                archive.read(name)
        temp_path.replace(output)
        return output
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xbe", required=True, type=Path)
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--texture-pack", required=True, type=Path)
    parser.add_argument(
        "--readme",
        default=Path("tools/pdx_asset_installer/release_readme.txt"),
        type=Path,
    )
    parser.add_argument("--license", dest="license_path", default=Path("LICENSE"), type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    output = package_core(
        args.xbe,
        args.installer,
        args.texture_pack,
        args.readme,
        args.license_path,
        args.out,
    )
    print(f"Created ROM-clean release: {output}")
    print(f"SHA256 {sha256(output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
