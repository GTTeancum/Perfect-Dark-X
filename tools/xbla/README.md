# Perfect Dark XBLA asset extraction

These tools extract assets from a user-supplied, legally obtained Perfect Dark XBLA package. The game package and extracted assets are intentionally ignored by Git and must not be committed or redistributed.

## STFS package

List the supplied LIVE package, or extract its file system before decoding the asset archives:

```powershell
python tools/xbla/extract_stfs.py `
  "Perfect Dark XBLA/35C1CDD22DD0D4E54B858859C0052124FFFAD17958" `
  --list

python tools/xbla/extract_stfs.py `
  "Perfect Dark XBLA/35C1CDD22DD0D4E54B858859C0052124FFFAD17958" `
  --output "Perfect Dark XBLA/extracted"
```

Extraction refuses to replace existing files unless `--overwrite` is supplied.

## Texture archive

`Textures.raw` contains two big-endian 52-byte tables for 5,747 textures followed by individually XMem/LZX-compressed texture allocations. The second table embeds Xenos texture fetch constants, which describe format, endianness, pitch, dimensions, tiling, mip levels, and cubemap state.

The extractor currently supports every format present in the supplied package:

- Xenos format 6: tiled A8R8G8B8
- Xenos format 18: DXT1
- Xenos format 19: DXT3
- Xenos format 20: DXT5

Install the PNG dependency and extract all base levels:

```powershell
python -m pip install -r tools/xbla/requirements.txt
python tools/xbla/extract_textures.py `
  "Perfect Dark XBLA/extracted/DataFiles/Textures.raw" `
  --output "Perfect Dark XBLA/textures" `
  --output-format png
```

For a quick validation set:

```powershell
python tools/xbla/extract_textures.py `
  "Perfect Dark XBLA/extracted/DataFiles/Textures.raw" `
  --output "Perfect Dark XBLA/texture-samples" `
  --ids 0,3,123,3517,3731,3777 `
  --output-format both
```

ID arguments are decimal unless prefixed with `0x` or they contain `a`-`f`. Output filenames use the stable texture index in four-digit hexadecimal. `manifest.json` preserves archive dimensions, original N64-era dimensions, format, pitch, endianness, Xenos fetch constants, and source payload locations for later matching and conversion.

## Packed segments (models and related data)

`PackedSegFile` contains 2,616 logical slots and a contiguous pool of 1,590 physical segments. In the supplied package, 1,461 segments use the same XMem/LZX framing as the textures and 129 are stored raw. Extract and hash every segment with:

```powershell
python tools/xbla/extract_segments.py `
  "Perfect Dark XBLA/extracted/DataFiles/PackedSegFile" `
  --output "Perfect Dark XBLA/segments"
```

The resulting `.bin` files are lossless decoded segments, named by their logical archive slot. `manifest.json` records all 2,616 raw table rows as well as each physical segment's archive offset, decoded/stored sizes, compression mode, optional property index, and SHA-256 hash. Mesh, skeleton, material, and object-record conversion is the next layer; this tool deliberately preserves the source records unchanged until those internal structures are mapped.

## Matching XBLA textures to the N64 port

Do not assume that every XBLA archive index is a drop-in replacement ID. First test direct correspondence against the port's texture preset IDs and original dimensions. Then generate deterministic N64 texture dumps and rendered captures and match them against XBLA outputs using dimensions, alpha coverage, color/perceptual hashes, and visual confirmation. Animated textures, combined atlases, and entries whose dimensions changed must be confirmed in-frame; VRAM or renderer-upload captures are the ground truth when metadata and IDs are ambiguous.

The vendored LZX decoder is a minimal Python subset derived from Binary Refinery and remains under its BSD-3-Clause license in `vendor/BINARY_REFINERY_LICENSE.md`.

## Format references

- [Free60 STFS format documentation](https://free60.org/System-Software/Formats/STFS/)
- [Xenia Xenos texture formats](https://github.com/xenia-project/xenia/blob/master/src/xenia/gpu/xenos.h)
- [Xenia tiled texture addressing](https://github.com/xenia-project/xenia/blob/master/src/xenia/gpu/texture_address.h)
- [Binary Refinery](https://github.com/binref/refinery)
