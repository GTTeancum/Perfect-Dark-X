# 480i presentation investigation — issue #3

## Scope

[Issue #3](https://github.com/GTTeancum/Perfect-Dark-X/issues/3) reports a
horizontal band moving through gameplay. The user confirmed 480i, but did not
confirm that the problem is specific to PAL. The other reported occurrence was
the transition into the main menu with Joanna at the computer. Intro-only runs
do not qualify this defect.

The additional [camera photo](https://github.com/GTTeancum/Perfect-Dark-X/issues/3#issuecomment-5649725135)
shows the dark band over the upper portion of the red doors, whereas the
earlier gameplay photo places it lower. The Joanna reference shows a broad
blue region with missing scene detail and some surviving geometry. The
reproduction target is a broad corruption band at variable vertical positions,
not a fixed center line. The exact reported artifact has not been reproduced
visually in XEMU; the buffer-ownership finding remains a candidate explanation.

## Finding and candidate correction

The released renderer calls `pb_finished()` after drawing, then lets pbkit
rotate its three render surfaces. In the installed nxdk implementation,
`pb_finished()` checks presentation-queue space; it does not acquire the next
render surface against the surface still being scanned out. Without a check
before drawing, the renderer can catch that surface and clear it while it is
still displayed.

A 120-second NTSC 480i diagnostic run using the released presentation behavior
recorded 6,540 frames in complete trace windows. The render target matched
`PCRTC_START` at frame start 1,509 times, and at GPU completion seven times.
The largest start-overlap window was 48 of 60 frames. Native screenshots
confirmed Defection gameplay. This demonstrates unsafe buffer reuse; it does
not establish that every reported hardware artifact has the same cause.

The candidate checks the next back buffer after draining the preceding
buffer-selection commands. If that surface is still selected for display,
it yields for 1 ms and rechecks scanout before any clear or draw. It applies the same rule to the
loading presenter. A free back buffer proceeds immediately, preserving the
ability to render ahead without adding an unconditional VBlank wait at 720p.

The window manager also now accepts a request for its fixed swap interval of
one. Previously it returned failure, which caused the shared video code to
treat Xbox as unsynchronized and choose its 240 FPS fallback at SD resolutions.

The backend also skips `finish_render()` when no new display list has begun a
render frame. The scheduler can produce these empty completions, as can the
current `videoClearScreen()` implementation. Previously they advanced the
presentation queue without rendering the next buffer and republished the old
capture pointer. The first candidate trace exposed two such startup events;
the revised trace confirms two empty completions skipped. Completion-overlap
counts in the original baseline therefore include empty submissions as well
as rendered frames; its start-overlap counts refer to actual render starts.

The ownership guard exposed a lost-swap condition in longer 720p runs.
Revision 2 stopped after 3,660 traced frames; revision 3 changed the VBlank
event wait to a 1 ms sleep/recheck and still stopped after 17,460 frames.
A diagnostic repeat stopped after 6,780 frames. Two guest RAM snapshots
showed the acquisition retry count continuing to increase, an empty ready
queue, and the next back buffer still selected in `PCRTC_START`.

The first snapshot was initially misinterpreted using the previous completed
frame pointer as the next target. Reading the actual `pb_FBAddr` table
corrected that: back index 1 was `0x0344c000`, matching scanout. The second
snapshot had back index 0 at `0x030c8000`, also matching scanout. Both waits
were waking and retrying; neither an event-delivery failure nor a stuck sleep
explains the stall. Replacing the wait alone was insufficient.

In pbkit, the ISR normally writes scanout, while `pb_vbl_handler()` retires
the queue entry. The DPC can loop with INTA disabled and handle another
VBlank without that ISR write, consuming a completed frame without displaying
it. Revision 4 instrumented a project-local core and observed deferred-handler
scanout corrections during a short 720p run. Revision 5 tightens the invariant:
the handler retires a ready entry only if `PCRTC_START` already selects it.
Otherwise the entry remains queued for the next ISR. This keeps scanout writes
at VBlank onset and counts these deferrals in `g_SPXBPbkitDeferredSwaps`.
The renderer ownership guard remains in place. See
`port/src/xbox/PBKIT_PATCH.md` for source provenance and linking details.

## Test method

`PD_XBOX_PRESENT_TRACE=ON` records aggregate start/end scanout overlap and
acquisition waits under `NV2A PERF PRESENT`. The option defaults off.

`PD_XBOX_QUALIFICATION_CONFIG=ON` allows a read-only `pdx_boot.ini` to select
the test scene. `pdx_video_soak_gameplay.ini` selects Defection and a fake
controller. Its process-local input turns and periodically fires after the
first 30 seconds. `pdx_video_soak_menu.ini` selects Carrington Institute with
profile autoselection. Native captures must verify the resulting scene;
configuration alone is not evidence of scene coverage.

The preliminary gameplay baseline used the second N64 stick, which strafed
instead of turning. Later captures left the rooftop. The candidate test uses
the first N64 stick for turning. The preliminary run is not a long-soak pass.

The candidate gameplay matrix includes the replacement texture pack and uses
660 seconds per setting: NTSC 480i, PAL-50 480i, PAL-60 480i, NTSC 480p, and
NTSC 720p. Each run records executable/image hashes, negotiated raster and
refresh, UART frame progress, fault markers, presentation counters, and native
XEMU captures. No host keyboard, mouse, window input, or desktop capture is used.

Example (paths below are relative to the repository):

```powershell
python scripts/pdx_video_soak.py --iso build-xbox-issue3/gameplay-pack-v5.iso --xbe build-xbox-issue3/default.xbe --map-file build-xbox-issue3/pd.map --source-config <source.toml> --source-eeprom <source.bin> --duration 660 --interval 60 --scenario gameplay --require-safe-present --dump-ram
```

`--require-safe-present` requires trace windows with zero start/end overlap.
Liveness also requires frame-counter progress near the end of the run, rather
than merely process survival. Visual review is a separate gate and remains
marked pending in the machine-generated summary until reviewed.

## Results

Revision 5 gameplay validation is in progress. No hardware fix or
release qualification is claimed yet.

Revision 3 menu tests completed:

| Mode | Run | Traced frames | Start/end overlaps | Acquisition polls | Captures reviewed |
| --- | ---: | ---: | ---: | ---: | ---: |
| NTSC 480i, 4:3 | 120 s | 6,540 | 0 / 0 | 9,274 | 24 |
| PAL-50 480i, 4:3 | 120 s | 6,660 | 0 / 0 | 10 | 24 |
| PAL-60 480i, 16:9 | 120 s | 6,600 | 0 / 0 | 986 | 24 |

All three passed the automated checks. All 72 captures were inspected in
sequence: startup, the computer-camera transition, then the animated
"Choose Your Reality" menu. No reported sky-colored horizontal band was
visible in these samples. The PAL-60 sweep sample passes through Joanna's
head model; it does not establish that the whole transition renders perfectly.
These are five-second samples, not a frame-by-frame recording.

Revision 2 results below are diagnostic history, not qualification of revision 3.

| Candidate gameplay mode | Run | Traced frames | Start/end overlaps | Acquisition waits | Native captures reviewed |
| --- | ---: | ---: | ---: | ---: | ---: |
| NTSC 480i, 4:3 | 660 s | 38,400 | 0 / 0 | 1,003 | 11 |
| PAL-50 480i, 4:3 | 660 s | 38,760 | 0 / 0 | 22 | 11 |
| PAL-60 480i, 16:9 | 660 s | 38,520 | 0 / 0 | 197 | 11 |
| NTSC 480p, 4:3 | 660 s | 38,580 | 0 / 0 | 109 | 11 |
| NTSC 720p, 16:9 | stopped after stall | 3,660 | 0 / 0 | 2 | 5 |

The four completed 660-second runs passed mode, frame-progress, fault-marker, capture-success,
and presentation-trace checks. All 11 native captures per run were inspected sequentially;
the first shows the opening mission cutscene and subsequent captures show
gameplay. No moving horizontal band was visible in those samples. Sampling is
not continuous visual coverage. Stock ammunition runs out during the soak;
camera rotation and trigger input continue afterward.
This is a rotating rooftop scene, not a full mission traversal or a worst-case
combat benchmark. PAL-50 mode negotiation was verified at 50 Hz, but XEMU's
render counters remained near 60 FPS; they do not establish physical PAL field
cadence.

The installed nxdk revision was `fb5a9a7a58a431e8d70a9e7da87898059df376c0`,
with no local changes under `lib/pbkit`. Its linked `libpbkit.lib` SHA-256 was
`b3396b37ace12f6d9e9f7b37f3506e7b8fb7ac326f29f3f1053ba18492d348a6`.

The initial intro-only run was stopped after the user clarified the affected
scenes. It is excluded from gameplay evidence. Its capture watcher pointed at
the disposable profile directory rather than XEMU's native screenshot directory;
subsequent runs use the verified native output directory explicitly.

## Hardware gate

XEMU framebuffer captures and counters do not qualify physical interlaced
scanout, field timing, or analog video output. A final hardware check must cover
the reported gameplay and Joanna/menu transition on affected 480i equipment.
PAL-50, PAL-60, and NTSC are separate test conditions, not assumed root causes.

## Hardware diagnostics pivot (2026-09-12)

At the user's request, further emulator matrices were stopped in favor of a
volunteer diagnostic package. The revision-5 720p run ended early and failed
the process-liveness check: 17,880 aggregate frames, zero logged start/end
overlaps, and 96 deferred swaps. This is not a qualification pass; the cause
of process exit is not established. Only the first seven native captures
from that run were reviewed. The exact reported band remains unreproduced.

See [diagnostic test instructions](issue3-diagnostic-test.md). Diagnostic 1
preserves original presentation behavior and adds bounded file logging,
per-frame ownership history, sampled queue/GPU/render/memory state, and wait
reports. No further long soak is required before collecting hardware evidence.
