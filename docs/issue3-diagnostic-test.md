# v1.1.1 — issue 3 diagnostic test

This is an instrumented test executable, not a confirmed fix. The exact moving band has not been reproduced visually in XEMU. 480i is confirmed affected; PAL specificity is unconfirmed. The band can appear at different heights.

## Install

1. Use an existing working v1.1 installation on the Xbox hard drive. Keep a backup of its original `default.xbe` and your save data.
2. Replace only `default.xbe` with the executable in this package. Keep the existing game assets, texture pack, and dashboard files. No ROM or game assets are included here.
3. Launch normally. There is no autoplay, simulated controller input, forced mission, or qualification configuration. Keep your usual video setup for the first run.

The diagnostic preserves the pre-candidate presentation behavior: no scanout ownership guard, original queue retirement, original swap-interval return, and empty-frame submission. Logging itself adds overhead and can change whether the symptom appears.

## Test and return evidence

Start a phone video when launching the game if practical; otherwise note launch time and the approximate elapsed time of each occurrence. Play the scenes that previously showed the band, including actual gameplay. Also try the sweep into Joanna's computer/menu if that previously triggered it. A 10–15 minute session in the affected setting is useful; stop sooner once you have clear evidence.

Capture the whole display so the height and movement of the band can be compared. Note the mission/location, elapsed time, and whether the strip moves, flickers, or stays still. A clean session is useful too; record its duration. Change only one video condition between sessions and collect each session's logs separately.

After an occurrence, return to the dashboard and copy these files from the game folder **before launching the game again**:

- `pd.log` — newest timestamped history.
- `pd.previous.log` — older history if rotation occurred; it may not exist.
- `pd.startup.log` — startup settings and initial output.

Zip those files with the completed `TEST-REPORT.txt` and your photos/video, and upload them in a comment on https://github.com/GTTeancum/Perfect-Dark-X/issues/3. Logs are overwritten on the next launch. `pd.previous.log` may belong to an older session when the current session has not rotated; return all files and identify the session. On a frozen console, copy the files after restarting into the dashboard, before relaunching the game. The last buffered records may be missing after a freeze or power loss.

Use a writable hard-drive installation for this test. If writing beside the XBE fails, the existing logger falls back to the title's `T:\pd.log` location; finding that dashboard-specific mapping is less convenient. A missing log is a failed diagnostic collection, not a clean test.

Restore the original XBE when finished.

## What is recorded

Every rendered completion records frame number, render target, scanout at start/end, VBlank counters, and overlap. Approximately every 60 completions there are swap queue indices/ready slots/buffer addresses, raster/pitch, physical-memory availability, GPU interrupt/status/command pointers, viewport/scissor/depth state, and draw/texture/framebuffer-copy counters. Existing asset, stage, timing, and error logs remain enabled. Startup includes encoder flags, output scan type, negotiated refresh and raster, and build identification. Disk records have milliseconds since system initialization.

Queue snapshots are read-only and approximate because interrupts can advance the queue between individual reads. `retired_without_scanout` counts observed original queue-retirement mismatches. The existing `deferred_swaps` field reports that same counter in this diagnostic; no deferral fix is enabled. Overlap and mismatch counts are hypotheses to correlate with video, not automatic detection of the visible band. Viewport/state snapshots describe the sampling point, not every draw within a frame.

The current log rotates at about 8 MiB and retains one previous segment, plus up to about 128 KiB of startup output. Frame records are buffered; aggregate records flush periodically. There is no per-draw GPU synchronization, framebuffer readback, or logging from interrupt handlers. GPU/queue waits exceeding two seconds emit repeated status reports without forcing a reset. A total CPU/kernel hang can still prevent logging. Some shorter renderer waits are outside these reports.

## Validation limits

Build and short NTSC 480i emulator startup/log checks are performed locally. Hardware behavior, log rotation at the size limit, and recovery after physical-console hangs still require volunteer testing. This package does not claim a successful extended soak or a reproduced/fixed band.

Release builds must configure `-DPD_XBOX_ISSUE3_DIAGNOSTIC=ON` and `-DPD_XBOX_QUALIFICATION_CONFIG=OFF`. The diagnostic option also enables presentation tracing.
