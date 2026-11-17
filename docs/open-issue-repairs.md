# Xbox issue repairs — 2026-11-17

Based on `origin/port` commit `86566c096` (v1.1.2). Changes and test artifacts
are local; no release, push, or GitHub issue closure was performed.

## Findings and repairs

| Report | Cause | Repair |
| --- | --- | --- |
| [#4: Spy Cam corruption](https://github.com/GTTeancum/Perfect-Dark-X/issues/4) | A copy starting at `(0,0)` skipped the frontend's coordinate conversion. The Xbox backend converted the unconverted Y again, selecting the last framebuffer row and repeating it down the image. | Include zero origins in the coordinate conversion; retain negative coordinates as the scaled-copy sentinel. |
| [#5: rapid menu crash](https://github.com/GTTeancum/Perfect-Dark-X/issues/5) | The hardware report identifies an invalid `NV097_SET_WINDOW_CLIP_HORIZONTAL` command. Clamping only the upper endpoint can leave the lower endpoint beyond it when a dialog moves offscreen. The vertical axis has the same problem. | Clamp both endpoints to the target and skip triangle submission for empty clip rectangles. Menu input speed is unchanged. |
| [#6: missing training statistics](https://github.com/GTTeancum/Perfect-Dark-X/issues/6) | The installed nxdk pdclib formatter omits floating-point conversions. The strings retain only their `s`/`%` suffixes. | Format bounded values with rounded integer hundredths/tenths. Cover firing-range results and HUD accuracy, plus the device/hologram training time callbacks. |

The same formatting defect visibly affected EyeSpy speed and height. Those
two HUD fields now also use integer decimal formatting.

## XEMU evidence

Tests use the existing XEMU 0.8.139 installation copied into `.local/xemu`,
64 MiB guest memory, the supplied US v1.1 ROM (verified MD5
`e03b088b6ac9e0080440efed07c1e40f`), and extracted stock textures. No XBLA
archive extraction is needed for these regressions.

All test output, EEPROMs, guest disk writes, caches, and captures stay inside
this repository. No desktop capture or host keyboard/mouse/controller input
is used. Captures are XEMU's native PNG output. Each emulator process has a
watchdog of at most 180 seconds; these runs use 30–100 seconds.

- **Training text:** eleven assertions run inside the Xbox game. The original
  code fails nine; the repaired code passes all eleven. They cover ordinary,
  zero, negative, minute-format and time-limit values, zero-shot and capped
  accuracy, HUD accuracy, and device/hologram training times.
- **Training screen:** native before/after captures of the same deterministic
  results fixture show `s` / `%` becoming `10.25s` / `83.3%`. These are fixture
  results, not evidence of completing a firing-range challenge.
- **Research EyeSpy:** the original game completes its opening cutscene and
  launches the device through its normal player tick and collision checks.
  UART confirms `active=1 deployed=1 held=0 startup=50`. The baseline capture
  repeats one row across the lens; the fixed capture shows room geometry and
  the guard through the lens with the surrounding HUD intact.
- **Widescreen EyeSpy:** a separate NTSC 480i 16:9 run also shows the room
  through the lens. The final HUD displays `S/MPS 0.00` and `H/M 0.95`;
  device activation remains confirmed through tick 3600.
- **Menus:** 739 original `menuSwipe` calls at three-frame intervals with
  alternating directions, followed by settling, complete during a 100-second
  run. The final native capture shows the Control Player 2 menu and labels.
- **Clip boundaries:** a dedicated Xbox probe submits rectangles beyond all
  four target edges, then restores the viewport. The fixed right-edge case
  `(645,10,20,20)` on a 640×480 target produces legal packed bounds
  `0x02800280`; the old calculation produces `0x02800285`. The bottom-edge
  case also produces equal bounds instead of an inverted interval.
- **Normal release:** a separate 55-second boot of the final, non-qualification
  ISO reaches the opening city flythrough. Its native capture shows textured
  buildings, streets, and the intended letterboxing. This verifies that scene,
  not the rest of the intro or campaign.

XEMU tolerates the baseline's invalid clip bounds: neither the menu stress
nor the direct boundary probe reproduces the physical GPU halt. The repair
addresses the invalid command identified by the hardware report, but the
original menu-crash reproduction still requires physical-Xbox confirmation.

## Follow-up: fast simulated controller input

The original 739-call test bypassed the controller input path. It called
`menuSwipe()` every three main ticks, reversing direction every 45 ticks;
20 calls/second was only the nominal rate at 60 main ticks/second. It was
transition stress, not a reproduction of button presses.

[The reporter](https://github.com/GTTeancum/Perfect-Dark-X/issues/5) describes
rapid left/right movement in controller options and after the first tutorial,
on a 1.6 Xbox launched from XBMC4Gamers in standard definition. The report
does not specify cadence, stick versus D-pad, game build, region, aspect ratio,
or which tutorial. The follow-up uses NTSC 480i 4:3, 64 MiB and stock textures. XEMU
direct-boots the XBE; the 1.6 console and dashboard launch environment are
not reproduced.

Scenarios 5–7 supply mapped controller samples inside `inputReadController`,
before the ordinary libultra sample queue, pressed-edge detection, menu
repeat handling and dialog navigation. They send no host input and make no
direct `menuSwipe` calls. The observer counts only actual menu swipes and
records whether the outgoing dialog was still opening/populating. There is
no added input throttle, GPU wait or buffer flush.

Each scenario runs five consecutive eight-second phases:

| Phase | Input pattern |
| --- | --- |
| 0 | Alternating digital taps at 10/second: 50 ms pressed, 50 ms neutral |
| 1 | Alternating digital taps at 20/second: 25 ms pressed, 25 ms neutral |
| 2 | Stick at +80/-80, reversing every 50 ms |
| 3 | Digital left/right reversal every controller poll, observed at 60/second |
| 4 | Alternating two-second digital holds, exercising normal repeat acceleration |

Controller-options, first-device results and first-hologram results use the
original dialogs and their sibling lists. The results dialogs use deterministic
in-memory fixtures; these tests do not complete or save a tutorial. The US
v1.1 firing-range completion dialog has no sibling page, so it is not used
as a substitute for the two swipable tutorial-result dialogs.

The original clip calculation is also exercised in comparator builds with
the same input harness and other fixes retained. This isolates clipping;
these comparator images are not unmodified upstream binaries.

All three repaired scenarios complete 919 actual menu transitions in 40.00
seconds, with 2,400 controller polls. The per-phase counts are 80, 160, 160,
480 and 39. All 919 outgoing dialogs are still opening/populating when the
next transition arrives. The final captures show readable Control Player 2,
Device List and Holotraining pages after input returns to neutral. Mid-run
captures show partially open/sliding dialogs; they are not evidence that
every animation frame is visually correct. The pre-existing foreground
weapon/model in the direct-CI fixture remains visible.

| Repaired scenario | GPU drain mean / maximum | Presentation queue wait mean / maximum |
| --- | ---: | ---: |
| Controller options | 0.194 / 4.283 ms | 0.301 / 4.982 ms |
| Device results/list | 0.161 / 5.829 ms | 0.283 / 4.724 ms |
| Hologram results/list | 0.315 / 4.550 ms | 0.323 / 5.997 ms |

These are the renderer's existing timing counters aggregated over 2,400
frames per scenario, with 30-frame reporting windows. They show no sustained
GPU drain/queue stall during these emulator tests, but cannot exclude a
hardware-specific synchronization fault.

Ordinary held/repeated pad input reaches the repaired out-of-range clip path
in all three scenarios. For example, controller options generates
`input=651,133,17,195` on the 640×480 target and emits `0x02800280`, keeping
both horizontal endpoints at 640. This connects the bounds repair to actual
menu input; the earlier direct boundary probe alone did not establish that.

Both comparator runs also complete 919 transitions, but their logs show the
old calculation emitting inverted bounds: `0x0280028b` for min=651/max=640.
The hologram-results path also emits `0x02800285` for min=645/max=640,
the same invalid interval used in the earlier hardware-report analysis.
XEMU accepts those commands, so survival in the emulator alone cannot
distinguish the broken and repaired builds. The changed command values are
the differentiating evidence; physical Xbox confirmation remains necessary.

Logs, native capture paths and ISO hashes are in `.local/evidence/` under
`fixed-pad-controls`, `fixed-pad-training-6`, `fixed-pad-training-7`,
`baseline-clip-pad-5` and `baseline-clip-pad-7`. The derived timing/count
summary is `.local/evidence/fast-input-summary.json`.

## Repeating the probes

Build with `-DPD_XBOX_QUALIFICATION_CONFIG=ON`. Pack a `pdx_boot.ini` using
`scripts/pack-loose-xiso.py --boot-ini ...`. Set these keys in `[Game]`:

| Scenario | `BootStage` | `IssueProbe` |
| --- | ---: | ---: |
| Rapid controller-option transitions | 38 | 1 |
| Training text assertions and results fixture | 38 | 2 |
| Actual Research EyeSpy activation | 51 | 3 |
| Direct GPU clip-boundary exercise | 38 | 4 |
| Controller options: fast simulated pad input | 38 | 5 |
| First-device results/list: fast simulated pad input | 38 | 6 |
| First-hologram results/list: fast simulated pad input | 38 | 7 |

Run from the repository with Windows Python and the installed Pillow library:

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
python scripts/pdx_issue_probe.py --iso build-xbox-fixed/spy.iso `
  --name research-spy-repeat --seconds 90 --capture-at 70 `
  --require 'PDX_ISSUE eyespy active=1 deployed=1 held=0 startup=50'

# After packing scenario 5 using the corresponding Game keys:
python scripts/pdx_issue_probe.py --iso build-xbox-fixed/input-5.iso `
  --name pad-controls-repeat --seconds 75 --capture-at 30 65 `
  --require 'PDX_INPUT complete scenario=5'
```

The runner validates repository-local config/media paths, prevents concurrent
probe launches, captures UART, and writes a JSON execution report. Native
capture can use a previously verified pointer RVA via `--native-rva`; that
address is specific to the emulator binary. A successful execution report
requires separate visual review and does not certify gameplay correctness.

Local evidence directories under `.local/evidence` include
`baseline-stats`, `fixed-stats`, `baseline-text-tests`, `fixed-text-tests`,
`baseline-research-spy-isolated`, `fixed-research-spy`, `fixed-menu-stress`,
`baseline-clip`, `fixed-clip`, `fixed-wide-spy-metrics`, and `release-boot`.
Each capture path is recorded in `run.json`.
The release was rebuilt after adding the guarded input probes; the subsequent
`release-after-input-probes` run again shows the textured city intro. Its ISO
hash is the current one in `artifact-hashes.json`. Both normal builds exclude
`PDX_INPUT`, `PDX_ISSUE` and `pdx_boot.ini` qualification markers.
The earlier `baseline-spy` overlay fixture was superseded by the actual
Research device-launch runs. `baseline-research-spy` was rejected by the disk
lock and is excluded from validation.

## Delivery and limits

The normal build is `build-xbox/default.xbe`, with a private local test image
at `build-xbox/perfectdarkx.iso`. Qualification configuration and fixture
strings are absent from the normal XBE. The ISO contains that exact XBE and
owner-extracted game data; it must remain private. Artifact SHA-256 values
are recorded in `.local/evidence/artifact-hashes.json`.

This is targeted SD regression testing, not a complete campaign, combat,
multiplayer, PAL, or 720p qualification. Audio buffers advance with nonzero
samples, but audible output and physical controllers were not tested. The
copied guest disk also reports dashboard-metadata copy access errors at
startup; save persistence and that separate condition remain unverified.
