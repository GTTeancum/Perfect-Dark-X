#!/usr/bin/env bash
# build-xiso.sh — Build Perfect Dark X XBE and create an XISO for XEMU.
#
# Prerequisites (Linux / WSL2):
#   - NXDK installed at $NXDK_DIR (or set below)
#   - cmake, make
#   - extract-xiso  (from https://github.com/XboxDev/extract-xiso)
#     or compile from NXDK: tools/extract-xiso/build/extract-xiso
#
# Usage:
#   ./scripts/build-xiso.sh [--rom /path/to/pd.ntsc-final.z64]
#
# Output:
#   dist/xbox/perfectdarkx.iso  — mount or pass directly to XEMU

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$REPO_ROOT/build-xbox"
DISK_DIR="$REPO_ROOT/build-xbox-disk"
OUT_ISO="$REPO_ROOT/dist/xbox/perfectdarkx.iso"
ROMID="ntsc-final"

# NXDK location — override with NXDK_DIR env var or -n flag
NXDK_DIR="${NXDK_DIR:-}"

# ROM file — override with --rom flag
ROM_FILE=""

# ── Arg parsing ───────────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --rom)        ROM_FILE="$2";   shift 2 ;;
        --nxdk)       NXDK_DIR="$2";  shift 2 ;;
        --romid)      ROMID="$2";     shift 2 ;;
        --clean)      rm -rf "$BUILD_DIR" "$DISK_DIR"; echo "Cleaned."; exit 0 ;;
        -h|--help)
            echo "Usage: $0 [--rom <z64>] [--nxdk <path>] [--romid ntsc-final|pal-final|jpn-final] [--clean]"
            exit 0 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# ── Validate NXDK ─────────────────────────────────────────────────────────────

if [[ -z "$NXDK_DIR" ]]; then
    echo "ERROR: NXDK_DIR is not set."
    echo "Install NXDK then either:"
    echo "  export NXDK_DIR=/path/to/nxdk"
    echo "  ./scripts/build-xiso.sh --nxdk /path/to/nxdk ..."
    exit 1
fi

if [[ ! -f "$NXDK_DIR/lib/pbkit/pbkit.c" ]]; then
    echo "ERROR: $NXDK_DIR does not look like a valid NXDK checkout."
    exit 1
fi

echo "NXDK: $NXDK_DIR"

# ── Locate extract-xiso ───────────────────────────────────────────────────────

EXTRACT_XISO=""
for candidate in \
    "extract-xiso" \
    "$NXDK_DIR/tools/extract-xiso/build/extract-xiso" \
    "/usr/local/bin/extract-xiso"; do
    if command -v "$candidate" &>/dev/null || [[ -x "$candidate" ]]; then
        EXTRACT_XISO="$candidate"
        break
    fi
done

if [[ -z "$EXTRACT_XISO" ]]; then
    echo "WARNING: extract-xiso not found."
    echo "Build it from: https://github.com/XboxDev/extract-xiso"
    echo "Then re-run.  Skipping ISO creation for now."
    SKIP_ISO=1
else
    SKIP_ISO=0
    echo "extract-xiso: $EXTRACT_XISO"
fi

# ── Validate ROM ──────────────────────────────────────────────────────────────

ROM_DST_NAME="pd.${ROMID}.z64"

if [[ -n "$ROM_FILE" ]]; then
    if [[ ! -f "$ROM_FILE" ]]; then
        echo "ERROR: ROM file not found: $ROM_FILE"
        exit 1
    fi
    ROM_SIZE=$(stat -c%s "$ROM_FILE" 2>/dev/null || stat -f%z "$ROM_FILE")
    if [[ "$ROM_SIZE" != "33554432" ]]; then
        echo "ERROR: ROM is $ROM_SIZE bytes, expected 33554432 (32 MB)."
        echo "Ensure it is in .z64 (big-endian) format, not .n64 or .v64."
        exit 1
    fi
    echo "ROM: $ROM_FILE ($ROM_SIZE bytes) ✓"
else
    echo "WARNING: No --rom specified.  The XBE will be built without the ROM."
    echo "The game will crash at Phase 8 (romdataInit) until you add:"
    echo "  $ROM_DST_NAME  →  alongside default.xbe on the disc"
    echo ""
fi

# ── Build XBE ─────────────────────────────────────────────────────────────────

echo ""
echo "=== Building XBE ==="
mkdir -p "$BUILD_DIR"

cmake \
    -DCMAKE_TOOLCHAIN_FILE="$REPO_ROOT/cmake/toolchain-nxdk.cmake" \
    -DNXDK_DIR="$NXDK_DIR" \
    -DROMID="$ROMID" \
    -DCMAKE_BUILD_TYPE=Debug \
    -B "$BUILD_DIR" \
    "$REPO_ROOT"

cmake --build "$BUILD_DIR" --parallel "$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)"

XBE_FILE="$BUILD_DIR/default.xbe"
if [[ ! -f "$XBE_FILE" ]]; then
    echo "ERROR: XBE not found at $XBE_FILE"
    echo "Check that cxbe is in your PATH or the NXDK tools directory."
    exit 1
fi

echo "XBE built: $XBE_FILE"

# ── Assemble disc image directory ─────────────────────────────────────────────

echo ""
echo "=== Assembling disc image ==="
rm -rf "$DISK_DIR"
mkdir -p "$DISK_DIR"

cp "$XBE_FILE" "$DISK_DIR/default.xbe"
echo "  default.xbe"

if [[ -n "$ROM_FILE" ]]; then
    cp "$ROM_FILE" "$DISK_DIR/$ROM_DST_NAME"
    echo "  $ROM_DST_NAME"
fi

# Optional: Game Boy Color ROM for the in-game feature
GBC_ROM="$REPO_ROOT/pd.gbc"
if [[ -f "$GBC_ROM" ]]; then
    cp "$GBC_ROM" "$DISK_DIR/pd.gbc"
    echo "  pd.gbc (GBC ROM)"
fi

# Default config — sets 480p if available, skips intro for faster testing
cat > "$DISK_DIR/pd.ini" <<'INI'
[Game]
SkipIntro = 1
MemorySize = 24

[Video]
DefaultFullscreen = 1
VSync = 1
FramerateLimit = 60

[Audio]
BufferSize = 512
INI
echo "  pd.ini (default config)"

echo "Disc directory: $DISK_DIR"

# ── Create XISO ───────────────────────────────────────────────────────────────

if [[ $SKIP_ISO -eq 0 ]]; then
    echo ""
    echo "=== Creating XISO ==="
    mkdir -p "$(dirname "$OUT_ISO")"
    rm -f "$OUT_ISO"

    "$EXTRACT_XISO" -c "$DISK_DIR" "$OUT_ISO"

    if [[ -f "$OUT_ISO" ]]; then
        ISO_MB=$(( $(stat -c%s "$OUT_ISO" 2>/dev/null || stat -f%z "$OUT_ISO") / 1048576 ))
        echo ""
        echo "✓  XISO created: $OUT_ISO  (${ISO_MB} MB)"
        echo ""
        echo "=== XEMU Usage ==="
        echo "  1. Open XEMU"
        echo "  2. Machine → Settings → DVD → select $OUT_ISO"
        echo "  3. Machine → Start"
        echo ""
        echo "Expected boot sequence:"
        echo "  Phase 0  White text on black: 'Xbox entry point reached'"
        echo "  Phase 1  crashInit OK"
        echo "  Phase 2  sysInit OK"
        echo "  Phase 3  fsInit OK"
        echo "  Phase 4  configInit OK"
        echo "  Phase 5  Blue screen: 'videoInit OK - NV2A online'  ← pbkit is up"
        echo "  Phase 6  inputInit OK"
        echo "  Phase 7  audioInit OK"
        if [[ -n "$ROM_FILE" ]]; then
        echo "  Phase 8  romdataInit OK - ROM loaded (32 MB)"
        echo "  Phase 9  gameInit OK"
        echo "  Phase 11 title screen appears"
        else
        echo "  Phase 8  *** WILL SHOW ERROR: ROM missing ***"
        echo "           Add $ROM_DST_NAME to $DISK_DIR and rebuild."
        fi
    else
        echo "ERROR: extract-xiso failed to create ISO."
        exit 1
    fi
else
    echo ""
    echo "Skipped ISO creation (extract-xiso not found)."
    echo "Disc directory is ready at: $DISK_DIR"
    echo "Once extract-xiso is available, run:"
    echo "  extract-xiso -c $DISK_DIR $OUT_ISO"
fi
