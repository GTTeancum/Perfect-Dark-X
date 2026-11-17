# Xbox issue repairs — 2026-11-17

Based on `origin/port` commit `86566c096` (v1.1.2). Repairs and fast-input
qualification were pushed directly to `port` in `b2eda4647` and `e94dd542f`.
The physical follow-up is in `2eca85d62`. These repairs are included in the
full v1.1.3 release. Game data and native captures remain private in the
repository's ignored `.local` directory.

## Findings and repairs

| Report | Cause | Repair |
| --- | --- | --- |
| [#4: Spy Cam corruption](https://github.com/GTTeancum/Perfect-Dark-X/issues/4) | A copy starting at `(0,0)` skipped the frontend's coordinate conversion. The Xbox backend converted the unconverted Y again, selecting the last framebuffer row and repeating it down the image. | Include zero origins in the coordinate conversion; retain negative coordinates as the scaled-copy sentinel. |
| [#5: rapid menu crash](https://github.com/GTTeancum/Perfect-Dark-X/issues/5) | The hardware report identifies an invalid `NV097_SET_WINDOW_CLIP_HORIZONTAL` command. Clamping only the upper endpoint can leave the lower endpoint beyond it when a dialog moves offscreen. The vertical axis has the same problem. | Clamp both endpoints to the target and skip triangle submission for empty clip rectangles. Menu input speed is unchanged. |
| [#6: missing training statistics](https://github.com/GTTeancum/Perfect-Dark-X/issues/6) | The installed nxdk pdclib formatter omits floating-point conversions. The strings retain only their `s`/`%` suffixes. | Format bounded values with rounded integer hundredths/tenths. Cover firing-range results and HUD accuracy, plus the device/hologram training time callbacks. |

The same formatting defect visibly affected EyeSpy speed and height. Those
two HUD fields now also use integer decimal formatting. The physical follow-up
also found and repaired the mission-end accuracy callback using the same
integer tenths, with zero-shot, ordinary and capped-accuracy checks.

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
original menu-crash reproduction required the physical follow-up below.

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
the differentiating emulator evidence; see the physical follow-up below.

Logs, native capture paths and ISO hashes are in `.local/evidence/` under
`fixed-pad-controls`, `fixed-pad-training-6`, `fixed-pad-training-7`,
`baseline-clip-pad-5` and `baseline-clip-pad-7`. The derived timing/count
summary is `.local/evidence/fast-input-summary.json`.

## Physical Xbox follow-up

The user's retail 64 MiB CerBIOS console was tested through XBDM 1.00.5933.1.
The initial launch selected the console's configured 720p mode. It is excluded
from SD results; XBDM's ordinary screenshot command also failed inside the
nxdk game. Subsequent qualification images explicitly selected NTSC 480i,
640x480, 4:3, stock textures. This override is compile-guarded, restricted to
the tested NTSC HDTV adapter, and does not change EEPROM preferences.

An owned `PDX-QA-*` folder holds the deployment. All 2,043 deployed files
were read back and verified against the host bytes. The runner verifies each
replacement XBE, clears only its own previous log/capture, and warm-reboots
to the launcher after each bounded run when XBDM remains reachable. It sends
no controller commands over XBDM: the same process-local input harness drives
the original menu code.
The console's hardware revision and XBMC4Gamers launch path have not been
matched to the issue reporter's setup.

| SD menu | Actual transitions / duration | Controller polls | Maximum poll gap |
| --- | ---: | ---: | ---: |
| Control options | 918 / 40.005 s | 2,397 | 28.354 ms |
| First-device results/list | 918 / 40.004 s | 2,396 | 36.352 ms |
| First-hologram results/list | 918 / 40.005 s | 2,397 | 25.313 ms |

All 918 transitions in each run arrived while the previous dialog was still
opening/populating. Phase counts were 80, 160, 160, 480 and 38. The fastest
phase therefore delivered 60 actual transitions/second for eight seconds.
The legal empty bounds were reached through normal held/repeated input.
No input rate limit, added GPU wait or flush was used to obtain this result.

Existing 30-frame timing windows covering 2,400 frames per test report mean
GPU drain waits of 0.002 ms. Maximum drain waits were 0.010 / 0.005 / 0.009 ms
for controls/device/hologram respectively; maximum presentation queue waits
were 0.112 / 0.126 / 0.232 ms. These counters do not measure all GPU work,
but they do not show a sustained drain/queue stall in these repaired tests.

The game-native captures show readable control and training dialogs. The
numeric fixture passes all eleven checks and visibly shows `10.25s` and
`83.3%`. However, the captured menu backgrounds are black, including a
Research-menu test after its opening cutscene. The floating foreground model
also remains visible. These captures establish menu survival and numeric
rendering, not full menu visual correctness; the background needs a separate
diagnosis. Native reads of both the small blur texture and its full-screen
render target contain the room image, narrowing the fault to composition.
A post-stress draw trace confirms the background triangles are submitted.
Disabling unnecessary depth processing did not fix the visible result, and
that experimental change was reverted. No speculative renderer change is
included in this follow-up. The unattended mission-failure screen also exposed
a `%f` accuracy field outside the training screens covered by #6. That field
now uses integer tenths, and all three additional checks pass on hardware
(`0.0%`, `83.3%`, `100.0%`), alongside the eleven training checks.
The original mission-results dialog visibly shows `83.3%`, 12 shots and
10 head hits in `hardware-sd-mission-fixture/native-038.png`. This is a
display-only fixture: its status/title fields do not represent real mission
completion or saved progression. Unattended attempts to capture the same
field were unreliable: one caught the red death transition, and the later
one still showed active EyeSpy. They are not accepted as mission-results proof.

A 100-second EyeSpy capture was rejected as camera evidence:
Joanna had died while unattended, and a missing-agent save prompt covered
the screen. Cancelling that prompt exposed the mission-failure screen.
Execution markers alone had incorrectly suggested the lens was still visible.
The subsequent `hardware-sd-spy-early` native capture, taken 50 seconds after
the first game tick, shows the room through the lens, a guard at its left
edge, and readable `S/MPS 0.00` / `H/M 0.98` values while Joanna is alive.

The original-clipping comparator stopped responding during phase 4 of
`hardware-sd-matched-baseline`. XBDM became unreachable, so neither native
error capture nor the watchdog's warm reboot could complete. The user
power-cycled the console; XBDM and the launcher were then verified again.
The recovered on-disk log ends immediately after:

```text
NV2A PERF scissor baseline_invalid=1 input=645,126,24,206 target=640x480 packed=02800285,01620094
```

This is min X=645/max X=640, matching the inverted interval identified in
the original hardware report. The paired fixed build completes 919 transitions
in 40.009 seconds and emits legal equal endpoints. This supports the invalid
clip diagnosis; there is no native photograph/capture of the physical halt,
so its exact on-screen error text is unverified. The old runner also exposed
a race when a reboot timed out: its JSON lacked `reboot_status`. The runner
now waits for the watchdog result and has a passing simulated-disconnect
regression check. `fault=false` in that old report only means no error text
was captured; it is not evidence that the console remained functional.

The paired fixed capture ended on Game Files after an automatic startup menu
change. It is not accepted as proof of sustained input on the hologram pair.
The harness now waits for startup to settle and counts swipes outside the
original dialog definitions; accepted repeat runs require
`unexpected_swipes=0` as well as the completion marker.

The verified repeats all complete exactly 918 transitions in 40.000 seconds,
with 2,398 controller polls and zero unexpected swipes. Maximum poll gaps are
25.413 ms (controls), 23.714 ms (device) and 25.163 ms (hologram). Their native
captures show the intended dialog pairs. These are
`hardware-sd-final-controls`, `hardware-sd-final-device`, and
`hardware-sd-dialog-verified`. The hologram run includes a single renderer
trace at frame 4000, after the timed input interval; controls/device do not.

Qualification builds now store saves/configuration in `D:\qa-saves` within
their deployment, and skip normal dashboard-metadata installation. Earlier
hardware probes used the normal title save path. The direct-stage EyeSpy
fixture cancels only the known missing-agent/save-elsewhere prompts using
their original cancel handlers. This is not a save-persistence test.

Native image capture writes one completed game framebuffer to a lossless PPM,
then transfers it through XBDM and converts it to PNG. Capture occurs after
the timed input interval, so disk/image work does not slow the stress phases.
The optional HAL capture reads the game's native pbkit error framebuffer
using symbols from the exact matching `pd.map`; it does not capture the host
desktop. Every JSON execution result still requires separate visual review.

Host evidence is under `.local/evidence/hardware-sd-*`, with the deployment
manifest in `hardware-deploy` and aggregate measurements in
`hardware-summary.json`. The first HD run's old `execution_ok` flag is not
accepted as proof: the runner was subsequently corrected to fail missing
captures. The initial `hardware-sd-training-7` attempt failed before launch
while XBDM was restarting; `hardware-sd-training-7-retry` is the measured run.
Visual acceptance/rejection is recorded separately in
`.local/evidence/physical-visual-review.json`, rather than inferred from
`execution_ok`. At handoff, the normal release XBE replaced the qualification
image in the owned deployment, its bytes were read back and verified, the
test boot INI was removed, and XDK Launcher was confirmed running.

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
| Mission-end accuracy display fixture (10 hits / 12 shots) | 38 | 8 |

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

For the physical console, keep the endpoint and owned deployment path in
ignored `.local/hardware-target.json` (`debug_host`, `debug_port`,
`deployment_directory`). The folder must contain `pdx-qa-owner.json`
identifying this project and the privately extracted loose assets. Use
`scripts/pdx_xbdm.py` for native file transfer; every mutating path must stay
inside that owned deployment. Set `IssueForceSD=1` and
`IssueCaptureAfterMs=60000` in the qualification boot INI for menu probes.
For Research EyeSpy, capture at 50000 ms, before unattended combat kills
Joanna. These settings are not available in normal release images.

```powershell
python scripts/pdx_hardware_probe.py --xbe build-xbox-fixed/default.xbe `
  --boot-ini .local/boot-hardware-7.ini --name hardware-holo-repeat `
  --seconds 100 --capture-at 82 --game-capture `
  --require 'PDX_INPUT complete scenario=7' `
  --require 'unexpected_swipes=0' --require 'PDX_CAPTURE written=1'
```

The runners validate repository-local config/media paths, prevent concurrent
probe launches, capture UART or XBDM logs, and write a JSON execution report.
The hardware runner's disconnected-watchdog regression can be repeated with
`python scripts/test_pdx_hardware_probe.py` without a console connection.
XEMU native capture can use a previously verified pointer RVA via `--native-rva`; that
address is specific to the emulator binary. A successful execution report
requires separate visual review and does not certify gameplay correctness.

Local evidence directories under `.local/evidence` include
`baseline-stats`, `fixed-stats`, `baseline-text-tests`, `fixed-text-tests`,
`baseline-research-spy-isolated`, `fixed-research-spy`, `fixed-menu-stress`,
`baseline-clip`, `fixed-clip`, `fixed-wide-spy-metrics`, and `release-boot`.
Each capture path is recorded in `run.json`.
The release was rebuilt after adding the guarded input probes; the subsequent
`release-after-input-probes` run again shows the textured city intro. Its ISO
hash is recorded in `artifact-hashes.json`. Both normal builds exclude
`PDX_INPUT`, `PDX_ISSUE` and `pdx_boot.ini` qualification markers.
The earlier `baseline-spy` overlay fixture was superseded by the actual
Research device-launch runs. `baseline-research-spy` was rejected by the disk
lock and is excluded from validation.

## Delivery and limits

The earlier normal build and private test image are recorded in
`.local/evidence/artifact-hashes.json`. They precede the v1.1.3 version bump.

The full v1.1.3 ZIP contains ten allowlisted distribution files and verified
checksums, with SHA-256
`d4c536f2d2c03e961a44e1fd2557f815896d936c35acb265319ec61bb5418ad8`.
Its clean-built normal XBE has SHA-256
`13335f0f7119b59d5426341d4a34123cbcf22d0f05a4fdda6bb970725b21eca5`.
Qualification markers are absent, and the compiled version is 1.1.3.
The published v1.1.2 installer and texture pack are reused byte-for-byte.
The installer backend extracted 2,011 files and 26 segments from the owner's
ROM, copied the packaged files and generated a private XISO containing the
exact release XBE. The source ROM was not copied into the game folder.
The installer GUI was not operated in this session.

A 55-second XEMU run of this fresh installation completed without a detected
fatal error. The native capture at 40 seconds shows the expected textured
city buildings and letterboxing; the black seven-second capture is not
accepted as visual evidence. This verifies the selected intro scene, not
every frame or the entire intro. Local records are
`.local/release-1.1.3/validation.json` and
`.local/evidence/release-1-1-3-boot/run.json`. The installer-generated XISO
contains owner-extracted data and remains private.

This is targeted SD regression testing, not a complete campaign, combat,
multiplayer, PAL, or 720p qualification. Audio buffers advance with nonzero
samples, but audible output and physical controllers were not tested. The
copied guest disk also reports dashboard-metadata copy access errors at
startup; save persistence and that separate condition remain unverified.
On physical hardware, menu backgrounds remain black despite populated blur
textures. This unresolved visual defect prevents full menu qualification.
