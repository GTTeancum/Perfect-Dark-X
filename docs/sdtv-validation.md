# SDTV startup correction — 2026-09-08

## Reports and change

GitHub [#1](https://github.com/GTTeancum/Perfect-Dark-X/issues/1) reports a PAL
black screen and freeze; [#2](https://github.com/GTTeancum/Perfect-Dark-X/issues/2)
reports NTSC 480i with an invalid GPU push-buffer stream and a `pb_reset`
timeout. The previous startup code reserved a 1 MiB push buffer only for
720p, leaving 480i/480p and allocation fallback on 512 KiB. The correction
uses 1 MiB for every mode and retains it through fallback. This costs an
additional 512 KiB at SD resolutions.

All mode requests now use the HAL's dashboard refresh selection instead of
forcing 60 Hz. The final active refresh is recorded and returned by the
window manager, including PAL-50. Framebuffer effects remain enabled at SD.

## Validation

The Release Xbox build compiled, linked, and generated `build-xbox/default.xbe`.
SDTV-test executable SHA-256 (before adding the visible v1.1 label):

`37517f578e3e34f0e9ffc03520bfe5cfd1de345a2229fcd4b74b56f435fe4fe8`

XEMU used the existing debug BIOS, 64 MiB RAM, an emulated HDTV AV pack,
disposable EEPROM/config files, and a ROM-free loose-asset ISO. Each run
completed GPU frames beyond the first reset with no UART fault markers.

| Dashboard profile | Verified video / pbkit raster | Active refresh | Run |
| --- | --- | --- | --- |
| NTSC 480i, 4:3 | 640x480 / 640x480 | 60 Hz | 45 s |
| PAL-50 480i, 4:3 | 640x480 / 640x480 | 50 Hz | 35 s |
| PAL-60 480i, 16:9 | 640x480 / 640x480 | 60 Hz | 30 s |
| NTSC 480p, 4:3 | 640x480 / 640x480 | 60 Hz | 30 s |
| NTSC 720p, 16:9 | 1280x720 / 1280x720 | 60 Hz | 30 s |

UART logs and monitor reports are under `scripts/output/pdx-sdtv-*` locally.
These are startup/render-submission checks, not visual or physical SDTV
qualification. XEMU's PAL frame timing is not proof of hardware timing.
Allocation-failure fallback was reviewed but not fault-injected. The test
ISO did not include the optional external texture pack. The user subsequently
confirmed that the fix works and authorized the v1.1 release. The console and
display details of that confirmation were not recorded here.

The final v1.1 executable also passed a 480i startup run; its splash labels
were inspected using native XEMU screenshots. Released executable SHA-256:

`c35f8d8561abe22e8cdfabdda4fc8481474c4e02404eb61505481237ec8160fc`

An initial `--headless` run stalled in the debug kernel before any game
startup markers; it is excluded. The runs above used XEMU's normal renderer
without screenshots or host/window input.

## Repeating PAL qualification

Use `tools/texturepack/make_xemu_hd_profile.py` with separate source/output
EEPROM and TOML paths, `--resolution 480i`, and `--standard pal50` or
`--standard pal60`. `--standard ntsc` explicitly selects NTSC, while the
default `source` preserves the original standard and refresh flags.
Generated factory/user checksums were independently checked against the
[XEMU checksum algorithm](https://github.com/xemu-project/xemu/blob/master/hw/xbox/eeprom_generation.c),
and the encrypted security section was verified unchanged.

Pack the rebuilt XBE with `scripts/pack-loose-xiso.py`, then run:

```powershell
python scripts/pdx_xemu_smoke.py --iso <test.iso> --config-path <disposable.toml> --name pdx-sdtv-pal50 --duration 35 --uart --port 4481
```

Require the expected `verified raster ... refresh=50Hz` (or `60Hz`),
`NV2A HWTRACE F1b pushbuffer reset complete`, and continuing `NV2A PERF`
records. Process survival alone is not a pass.
