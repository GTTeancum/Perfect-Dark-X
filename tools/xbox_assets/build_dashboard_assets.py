#!/usr/bin/env python3
"""Build Original Xbox dashboard/save XPR resources from the project icon."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image


TITLE_NAME = "Perfect Dark X"


def _write_bmp(icon: Image.Image, size: int, output: Path) -> None:
    resized = icon.resize((size, size), Image.Resampling.LANCZOS)
    background = Image.new("RGBA", (size, size), (0, 0, 0, 255))
    background.alpha_composite(resized)
    background.convert("RGB").save(output, format="BMP")


def _rdf(resource_name: str, source_name: str, width: int, height: int) -> str:
    return (
        f"out_header         {resource_name.lower()}.h\n"
        f"out_packedresource {resource_name}.xbx\n"
        f"out_error          {resource_name.lower()}.err\n"
        f"out_prefix         {resource_name.lower()}\n\n"
        f"Texture {resource_name}\n"
        "{\n"
        f"   Source      {source_name}\n"
        f"   Width       {width}\n"
        f"   Height      {height}\n"
        "   Format      D3DFMT_DXT1\n"
        "   Levels      1\n"
        "}\n"
    )


def build(icon_path: Path, output_dir: Path, bundler: Path) -> None:
    if not icon_path.is_file():
        raise FileNotFoundError(icon_path)
    if not bundler.is_file():
        raise FileNotFoundError(bundler)

    output_dir.mkdir(parents=True, exist_ok=True)
    icon = Image.open(icon_path).convert("RGBA")

    with tempfile.TemporaryDirectory(prefix="pdx-xbox-assets-") as temporary:
        work = Path(temporary)
        _write_bmp(icon, 128, work / "titleimage_128.bmp")
        _write_bmp(icon, 64, work / "saveimage_64.bmp")
        (work / "titleimage.rdf").write_text(
            _rdf("TitleImage", "titleimage_128.bmp", 128, 128), encoding="ascii"
        )
        (work / "saveimage.rdf").write_text(
            _rdf("SaveImage", "saveimage_64.bmp", 64, 64), encoding="ascii"
        )

        for rdf in ("titleimage.rdf", "saveimage.rdf"):
            subprocess.run([str(bundler), rdf], cwd=work, check=True)

        outputs = {
            "TitleImage.xbx": 10_240,
            "SaveImage.xbx": 4_096,
        }
        for name, expected_size in outputs.items():
            generated = work / name
            data = generated.read_bytes()
            if len(data) != expected_size or data[:4] != b"XPR0":
                raise ValueError(f"Unexpected {name} output: {len(data)} bytes")
            shutil.copy2(generated, output_dir / name)

    title_meta = f"[default]\r\nTitleName={TITLE_NAME}\r\n".encode("utf-16")
    (output_dir / "TitleMeta.xbx").write_bytes(title_meta)
    print(f"Built Xbox dashboard assets in {output_dir.resolve()}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--icon", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--bundler",
        type=Path,
        default=Path(r"C:\XDK_5558\XDK\xbox\bin\bundler.exe"),
    )
    args = parser.parse_args()
    build(args.icon.resolve(), args.out.resolve(), args.bundler.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
