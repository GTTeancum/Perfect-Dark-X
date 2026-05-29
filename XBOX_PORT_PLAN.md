# Perfect Dark X — Original Xbox Port Plan

> Testing via XEMU → FTP to hardware once stable per phase.

---

## ROM Requirements

| Field | Value |
|---|---|
| **Game** | Perfect Dark (USA) v1.1 |
| **Region** | NTSC (North American cartridge) |
| **File name** | `pd.ntsc-final.z64` |
| **Format** | `.z64` — big-endian (N64 native byte order) |
| **Size** | Exactly **32 MB** (33,554,432 bytes) |
| **Header ID** | `NPDE` at offset 0x3B |
| **Header title** | `Perfect Dark` at offset 0x20 |

> **Format note:** If your dump is `.n64` (little-endian) or `.v64` (word-swapped),
> convert it to `.z64` before use. Tool: `n64-conv` or `Tool64`.
> The port validates the header and will display a clear error if the format is wrong.

**Optional:** `pd.gbc` — Perfect Dark Game Boy Color ROM (4 MB) unlocks the in-game
GBC feature. Not required for core testing.

---

## XEMU Setup

XEMU is a cycle-accurate original Xbox emulator. Download from **xemu.app**.

### Required files (obtain legally)

| File | Notes |
|---|---|
| Xbox BIOS | 256 KB or 1 MB retail BIOS dump from your own console |
| MCPX ROM | `mcpx_1.0.bin` or `mcpx_1.1.bin` — the boot ROM chip |
| HDD image | XEMU can generate a blank one via **Machine → Settings → Hard Disk → Generate** |

### XEMU settings

```
Machine → Settings
  → System  →  Memory: 64 MB
  → Display →  Scale: 2× or 3×
  → DVD     →  select perfectdarkx.iso after each build
```

Enable **View → Debug → Serial (UART)** to see `debugPrint` output in the XEMU
console — this captures phases 0–4 before the pbkit framebuffer takes over.

---

## Build Instructions (WSL2 / Linux)

```bash
# 1. Clone NXDK (once)
git clone https://github.com/XboxDev/nxdk.git
cd nxdk && make -j$(nproc)
export NXDK_DIR=$(pwd)

# 2. Install extract-xiso (once)
cd tools/extract-xiso && cmake . && make
# binary: tools/extract-xiso/extract-xiso

# 3. Build XBE + XISO
cd /path/to/perfect-dark-x
chmod +x scripts/build-xiso.sh
./scripts/build-xiso.sh --rom /path/to/pd.ntsc-final.z64

# Output: dist/xbox/perfectdarkx.iso
```

> **Windows users:** Use WSL2 (Ubuntu) for the build. The NXDK LLVM toolchain
> runs on Linux. All other work (editing, git) stays in Windows as normal.

---

## Phase Plan

Each phase has:
- What the screen should show in XEMU
- What the UART / `D:\pd.log` will contain
- What a failure looks like and where to look

---

### Phase 0 — Xbox Entry Point

**Goal:** Verify the XBE boots and our `void main()` is reached.

**Screen (debugPrint — white text on black):**
```
Perfect Dark X - Xbox Port
Phase 0: entry point reached
[00] Xbox entry point reached
```

**Log:** `PHASE [00] Xbox entry point reached`

**Failure modes:**
| Symptom | Cause |
|---|---|
| Xbox dashboard / error 06 | XBE is corrupt or cxbe conversion failed |
| Error 07 / 08 | Kernel version mismatch — try a different BIOS |
| Green screen / hang before text | pbkit crash before `debugPrint` — check NXDK init |
| Text appears then immediately crashes | Stack overflow — check `bootAllocateStack` |

---

### Phase 1 — Crash Init

**Screen:**
```
[01] crashInit OK (stub)
```
**Log:** `PHASE [01] crashInit OK (stub)`

Trivial stub — if this fails the XBE hasn't even linked correctly.

---

### Phase 2 — System Init

**Goal:** Verify `KeQueryPerformanceCounter` timer and log file creation.

**Screen:**
```
[02] sysInit OK - timer running
```

**Log:** Check `D:\pd.log` exists and has the startup date line:
```
startup date: 29 May 2026 ...
perf counter frequency: 733333333 Hz  (approx — varies by Xbox model)
Xbox system initialised
```

**Failure modes:**
| Symptom | Cause |
|---|---|
| Freeze at Phase 2 | `KeQueryPerformanceFrequency` not available — NXDK version issue |
| Log not created | `D:\` not writable; try `T:\pd.log` fallback (already in code) |

---

### Phase 3 — Filesystem Init

**Goal:** Verify `D:\` is accessible as the game directory.

**Screen:**
```
[03] fsInit OK - D:\ accessible
```

**Failure modes:**
| Symptom | Cause |
|---|---|
| Freeze / crash | `fsInit` calling SDL before SDL_Init — check include order |

---

### Phase 4 — Config Init

**Screen:**
```
[04] configInit OK
```

Config defaults are applied. `pd.ini` is read from `D:\` if present.

---

### Phase 5 — Video Init (pbkit + NV2A)

**Goal:** pbkit initialises, NV2A GPU online, display switches from debugPrint
text mode to the pbkit framebuffer. **This is the critical GPU milestone.**

**Screen changes:** The raw text mode disappears and is replaced by a dark blue
background with all phases printed via `pb_print`:
```
=== Perfect Dark X - Xbox Port ===
Phase debug output

[00] Xbox entry point reached
[01] crashInit OK (stub)
[02] sysInit OK - timer running
[03] fsInit OK - D:\ accessible
[04] configInit OK
[05] videoInit OK - NV2A online

(check D:\pd.log for full output)
```

**Log:**
```
Xbox video mode: 640x480 interlaced   (or progressive/720p depending on TV)
Xbox window manager initialised
NV2A renderer initialised
PHASE [05] videoInit OK - NV2A online
```

**Failure modes:**
| Symptom | Cause |
|---|---|
| Freeze after Phase 4 | `pb_init()` failed — check NXDK pbkit version |
| Screen goes black and stays black | `XVideoSetMode` called with unsupported mode; try 640×480 always |
| `pb_init() failed (err N)` in log | NV2A not responding — known XEMU issue with some BIOS+MCPX combos |
| Text never switches to blue screen | `dbgNotifyPbkitUp()` not called — check `gfx_xbox_wm.cpp` |

> **XEMU note:** XEMU's NV2A emulation is excellent but requires the correct MCPX ROM.
> If pb_init fails, try `mcpx_1.0.bin` vs `mcpx_1.1.bin`.

---

### Phase 6 — Input Init

**Screen:**
```
[06] inputInit OK - SDL gamepad ready
```

**Log:** `SDL gamepad init: N controllers connected`

**Notes:**
- Xbox controllers are exposed by XEMU as SDL joysticks
- Mouse events are suppressed — no mouse on Xbox
- If 0 controllers shown: connect a virtual Xbox controller in XEMU settings

---

### Phase 7 — Audio Init

**Screen:**
```
[07] audioInit OK - SDL audio device open
```

**Log:** `SDL_OpenAudioDevice: 22020 Hz, stereo, S16`

**Failure modes:**
| Symptom | Cause |
|---|---|
| Crash at audioInit | NXDK's SDL2-Xbox audio backend not linked — check CMakeLists |
| `SDL_OpenAudio error` in log | Audio device unavailable in this XEMU build; non-fatal for now |

---

### Phase 8 — ROM Load

**Goal:** Locate and validate `pd.ntsc-final.z64` from `D:\`.

**Screen:**
```
[08] romdataInit OK - ROM loaded (32 MB)
```

**Log:**
```
ROM file: pd.ntsc-final.z64
romdataInit: loaded rom, size = 33554432
loading segment animations from ROM (offset 1a15c0 ...)
...
```

**Failure modes:**
| Symptom | Cause |
|---|---|
| `FATAL ERROR: Could not open ROM file` on screen | ROM not in ISO — rebuild with `--rom` flag |
| `Wrong ROM file. ROM size does not match` | Wrong dump size; ensure 32 MB `.z64` |
| `ROM header does not match` | Wrong region or byte order (use `.z64`, not `.n64`/`.v64`) |
| `ROM is in an archive file` | Accidentally included a `.zip` — extract first |
| Freeze after ROM load | `rzipInflate` crashing — likely 64-bit pointer issue; check 32-bit build |

> **Memory note:** The ROM is loaded entirely into RAM (32 MB). Combined with
> the 16 MB game heap this uses ~48 MB of the Xbox's 64 MB. If allocations fail,
> reduce `Game.MemorySize` in `pd.ini` to 12.

---

### Phase 9 — Game Init & Heap

**Screen:**
```
[09] gameInit OK - heap allocated
```

**Log:**
```
memp heap at 0xXXXXXXXX (16 MB)
rom  file at 0xYYYYYYYY (32 MB)
```

---

### Phase 10 — Scheduler

**Screen:**
```
[10] bootCreateSched OK
```

The N64 OS message queue and scheduler are set up. This is thin wrapper code — if it crashes, check `libultra.c` threading assumptions on Xbox.

---

### Phase 11 — mainProc / Title Screen

**Goal:** The game's main loop starts and the Perfect Dark title screen renders.

**Screen:** 2-second pause showing all phases, then the pbkit framebuffer transitions
to game rendering. The N64-era title logo should appear.

**What to look for:**
- Title screen renders (even if glitchy — expected at first)
- Frame rate visible via `Video.DisplayFPS = 1` in `pd.ini`
- Controller input moves menu cursor

**Expected initial issues (known, will be fixed per issue):**
- Colour combiner mismatches → some geometry wrong colour
- Missing textures → black rectangles
- Audio crackling → buffer tuning needed
- No readable text → font rendering combiner not yet translated

---

## Reporting Results

For each phase, post:
1. **Screenshot** of the XEMU window
2. **UART log** from XEMU's serial console (View → Debug → Serial)
3. **`D:\pd.log`** contents if accessible (extract from ISO or via XEMU file browser)

If a phase crashes, the screen will show:
```
FATAL ERROR:
<message here>

Press power to reboot.
```

---

## Known Issues & TODO

| Priority | Issue | File |
|---|---|---|
| P0 | NV2A colour combiner — partial coverage | `gfx_nv2a.cpp:build_combiner_state` |
| P0 | Texture format — linear only, no swizzle | `gfx_nv2a.cpp:nv2a_upload_texture` |
| P1 | Font rendering may fail (combiner mismatch) | Phase 11 testing |
| P1 | Framebuffer copy / blit not implemented | `gfx_nv2a.cpp:nv2a_copy_framebuffer` |
| P2 | PAL Xbox: switch scheduler to `OS_VI_PAL_LAN1` | `main_xbox.c:bootCreateSched` |
| P2 | Mipmap generation | `gfx_nv2a.cpp:nv2a_upload_texture` |
| P3 | Controller rumble via SDL2-Xbox | `port/src/input.c` |
| P3 | Save game path `E:\TDATA\PerfectDarkX\` verification | `system_xbox.c:sysGetHomePath` |

---

## File Map

```
Perfect Dark X/
├── cmake/toolchain-nxdk.cmake     NXDK cross-compilation toolchain
├── scripts/build-xiso.sh          Full build + XISO creation script
├── XBOX_PORT_PLAN.md              This file
├── port/
│   ├── fast3d/
│   │   ├── gfx_nv2a.cpp           NV2A pbkit rendering backend
│   │   ├── gfx_nv2a.h
│   │   ├── gfx_xbox_wm.cpp        Xbox window manager (XVideoSetMode + pbkit)
│   │   └── gfx_xbox_wm.h
│   └── src/xbox/
│       ├── main_xbox.c            void main() entry with phase debug
│       ├── system_xbox.c          Timer, sleep, paths, logging
│       ├── crash_xbox.c           Stub crash handler
│       ├── debug_xbox.c           Phase debug overlay (debugPrint / pb_print)
│       └── debug_xbox.h
└── src/include/platform.h         PLATFORM_XBOX added
```
