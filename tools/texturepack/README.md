# Perfect Dark texture-pack tools

These tools build a reproducible diffuse-only replacement pack from a stock
`ntsc-final` asset extraction and an owner-supplied XBLA `Textures.raw` archive.
No copyrighted source or generated image assets are committed to the repository.

## Pipeline

Install Pillow and NumPy, then run from the repository root:

```powershell
python -m pip install -r tools/texturepack/requirements.txt
python tools/texturepack/extract_n64_textures.py `
  --textures-json src/assets/ntsc-final/textures.json `
  --textures-dir src/assets/ntsc-final/textures `
  --output texturepack-work/n64
python tools/texturepack/match_textures.py `
  --n64-manifest texturepack-work/n64/manifest.json `
  --xbla-manifest "Perfect Dark XBLA/textures/manifest.json" `
  --output texturepack-work/matched
python tools/texturepack/finalize_pack.py `
  --candidate-manifest texturepack-work/matched/manifest.json `
  --decisions tools/texturepack/review_decisions.json `
  --output texturepack-work/release
python tools/texturepack/build_xbox_pack.py `
  --manifest texturepack-work/release/manifest.json `
  --profile tools/texturepack/xbox_release_profile.json `
  --output texturepack-work/release/ext_tex.pak `
  --large-texture-limit 4 `
  --compression-level 9
```

`ext_tex.pak` is the final texture-pack artifact and may be bundled with the
XBE and asset-installer GUI. It is not derived from the user's N64 ROM. The GUI
extracts the user's ROM first, then validates and copies this pack unchanged
into a new ROM-free Xbox folder. Any XISO containing extracted ROM assets is
generated locally on the user's PC and must not be uploaded as a release
artifact.

`match_textures.py` emits labelled sheets under `matched/review`. Every entry it
marks `visual-review-required` must be listed in `review_decisions.json` before
the finalizer succeeds. `search_candidates.py` searches the full XBLA 2D corpus
and sampled atlas crops when a same-index pair looks unrelated.

The final `release/ext_tex` directory follows the external-texture naming scheme
from upstream Perfect Dark PR #653: four-digit lowercase hexadecimal stock
texture IDs. Missing PNGs intentionally fall back to stock N64 textures.

`build_xbox_pack.py` applies the release profile: all ordinary replacements are
bounded to 64 pixels on their longest edge, while only texture IDs `009b`,
`0216`, `089f`, and `0a03` may reach 128 pixels. Hardware-oriented 720p smoke
testing established this as the largest tier that keeps an active scene inside
the 1 MiB residency budget; 128/256 and 128/512 packs caused sustained eviction
and repeated synchronous decompression. The builder writes a seekable
`ext_tex.pak`, then reads back and decompresses every payload. The runtime
indexes its table once and loads textures on demand. The renderer keeps at most
128 stock/replacement cache entries and separately bounds resident external
diffuses to 4 MiB at 480-line output or 1 MiB at native 720p.

The mapping policy is conservative:

- same archive IDs are candidates because XBLA retains the N64 logical dimensions;
- animated sequences and changed dimensions/layouts require visual review;
- unrelated or reorganized XBLA atlases are omitted unless a correct crop exists;
- entries with no stock N64 texture slot are never copied into the general pack;
- XBLA-only bump, normal, specular, cubemap, and other material maps are ignored.

For later visual qualification, compare runtime captures against N64 TMEM/VRAM
dumps. The stock asset decode is sufficient for one-to-one source matching;
runtime dumps remain useful for textures that the N64 combines, animates, or
packs into a larger render-time surface.

## Xbox video modes

Aspect ratio and resolution come exclusively from the Xbox dashboard; the game
does not expose config keys or an in-game resolution control. With 720p enabled
in the dashboard, the game selects guarded native 1280x720 at 16:9. Otherwise,
it uses 640x480 as dashboard-selected 4:3 or anamorphic 16:9, progressive when
480p is enabled and interlaced otherwise. The renderer uses the selected aspect
for Hor+ world projection while the HUD and menus retain their existing
safe-area/aspect policies.

If setting the 720p mode or allocating its pbkit surfaces fails, startup falls
back to dashboard-aspect 640x480. Native 720p disables framebuffer effects,
caps rendering at 30 FPS, reduces the game heap to 14 MiB, and uses the 1 MiB
external-texture residency budget. The 480-line path retains the 16 MiB game
heap and 4 MiB external-texture budget.

Create a disposable XEMU HD qualification profile without modifying the user's
normal EEPROM or XEMU configuration:

```powershell
python tools/texturepack/make_xemu_hd_profile.py `
  --source-eeprom C:\Games\Emulators\Xemu\PerfectDarkX\EEPROM\eeprom_pdx.bin `
  --output-eeprom build-xbox/qualification/eeprom_pdx_720p.bin `
  --source-config C:\Games\Emulators\Xemu\PerfectDarkX\xemu.toml `
  --output-config build-xbox/qualification/xemu_720p.toml `
  --aspect 16:9 --resolution 720p
```

Use `--aspect 16:9 --resolution 480p` for the widescreen 480p profile or
`--aspect 4:3 --resolution 480p` for the 4:3 fallback profile. These disposable
EEPROMs are the qualification equivalent of changing dashboard settings; the
game executable receives no test-only video override. The generated XEMU
configuration also pins the host presentation aspect to the EEPROM aspect.
This is required for trustworthy native screenshots: XEMU's `Auto`
presentation can expose a 4:3 capture surface even while the guest is running
a 16:9 video mode. It is a qualification setting only and is not needed by the
game on an Xbox.

The qualification INIs are deliberately separate from the public release
and normal user install:

- `pdx_widescreen_gameplay_test.ini`: gameplay boot fixture; video comes from the qualification dashboard profile.
- `pdx_4x3_gameplay_test.ini`: 4:3 gameplay fixture used with `xemu_480p_4x3.toml`.
- `pdx_720p_gameplay_test.ini`: gameplay fixture used with `xemu_720p.toml`.
- `pdx_widescreen_mainmenu_test.ini`: main-menu and safe-area checks.
- `pdx_widescreen_menu_test.ini`: agent/menu overlay checks.
- `pdx_widescreen_multiplayer_test.ini`: two-player split-screen runtime check.
- `pdx_multiplayer_3p_test.ini` and `pdx_multiplayer_4p_test.ini`: remaining split layouts.

Developers may pack a local qualification ISO by adding `--boot-ini` to
`pack-loose-xiso.py`. Never distribute that asset-bearing image or place a
qualification boot override in an end-user install.

Run unattended qualification through the smoke harness so XEMU stays minimized
and does not take focus. Captures must use XEMU's native screenshot request, not
a desktop/window capture:

```powershell
python scripts/pdx_xemu_smoke.py `
  --iso build-xbox/pdx-textures-wide480p-test.iso `
  --config-path build-xbox/qualification/xemu_480p_wide.toml `
  --name pdx-textures-wide480p `
  --duration 180 --interval 30 --uart --screenshots `
  --xemu-native-screenshots `
  --xemu-screenshot-dir C:\Games\Emulators\Xemu\PerfectDarkX `
  --xemu-screenshot-flag-rva 0x13a3ff0
```

Runtime acceptance requires the 480p 16:9 gameplay, menus, two-player
split-screen, and sustained texture-pack runs to remain alive without pbkit or
texture allocation failures, page faults, asserts, corruption, or sustained
cache thrashing. Inspect every native screenshot sequentially. The optional
720p tier must satisfy the same checks and hold 30 FPS after loading; failure of
that tier must not compromise the mandatory 480p path. A 720p result is valid
only when all three independent checks agree: `XVideoGetMode` reports
1280x720, pbkit's back buffer is 1280x720, and each gameplay PNG's IHDR is
1280x720 (16:9). A 960x720 or other 4:3 PNG is not a 720p baseline, regardless
of the guest's requested mode.
