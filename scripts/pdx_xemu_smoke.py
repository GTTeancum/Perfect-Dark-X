#!/usr/bin/env python3
"""Perfect Dark X adapter for the shared Xbox/XEMU smoke-test harness.

The vendored shared harness supplies process supervision, QEMU-monitor
sampling, EIP tracking, reports, and optional UART capture. This adapter keeps
all PDX paths and defaults in one command:

    python scripts/pdx_xemu_smoke.py --duration 100

Pass --harness (or set XEMU_SMOKE_HARNESS) only to test a newer external copy.
Extra arguments are forwarded to the shared harness.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import runpy
import sys
from time import strftime


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_XEMU = Path(r"C:\Games\Emulators\Xemu\PerfectDarkX\xemu.exe")
DEFAULT_CONFIG = Path(r"C:\Games\Emulators\Xemu\PerfectDarkX\xemu.toml")


def find_harness(explicit: str | None) -> Path:
    candidates = [
        explicit,
        os.environ.get("XEMU_SMOKE_HARNESS"),
        ROOT / "scripts" / "xemu_smoke.py",
        ROOT.parent / "Star-Trek-Elite-Force-X" / "scripts" / "ja_xemu_smoke.py",
        ROOT.parent / "Jedi-Academy-X" / "scripts" / "ja_xemu_smoke.py",
    ]

    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser().resolve()
        if path.is_file():
            return path

    raise SystemExit(
        "Shared XEMU harness not found. Pass --harness PATH or set "
        "XEMU_SMOKE_HARNESS."
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the shared XEMU smoke harness with Perfect Dark X defaults",
        add_help=True,
    )
    parser.add_argument("--harness", help="path to ja_xemu_smoke.py")
    parser.add_argument("--iso", default=str(ROOT / "build-xbox" / "pdx-loose.iso"))
    parser.add_argument("--map-file", default=str(ROOT / "build-xbox" / "pd.map"))
    parser.add_argument("--xemu-exe", default=str(DEFAULT_XEMU))
    parser.add_argument("--config-path", default=str(DEFAULT_CONFIG))
    parser.add_argument("--name", default="pdx-smoke")
    parser.add_argument("--duration", type=int, default=100)
    parser.add_argument("--interval", type=int, default=10)
    parser.add_argument(
        "--uart",
        action="store_true",
        help="attach COM1 and capture UART output to scripts/output",
    )
    parser.add_argument(
        "--screenshots",
        action="store_true",
        help="enable the shared harness screenshot path",
    )
    args, forwarded = parser.parse_known_args()

    harness = find_harness(args.harness)
    iso = Path(args.iso).resolve()
    map_file = Path(args.map_file).resolve()

    if not iso.is_file():
        raise SystemExit(f"ISO not found: {iso}")
    if not map_file.is_file():
        raise SystemExit(f"map file not found: {map_file}")

    harness_args = [
        str(harness),
        "--iso", str(iso),
        "--name", args.name,
        "--duration", str(args.duration),
        "--interval", str(args.interval),
        "--xemu-exe", str(Path(args.xemu_exe).resolve()),
        "--config-path", str(Path(args.config_path).resolve()),
        "--map-file", str(map_file),
        "--sample-eip-interval", "1",
        "--visible",
    ]

    if not args.screenshots:
        harness_args.append("--no-screenshots")

    if args.uart:
        output_dir = ROOT / "scripts" / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        uart = output_dir / f"{args.name}_{strftime('%Y%m%d_%H%M%S')}.uart.bin"
        harness_args.extend(
            [
                "--xemu-arg=-device",
                "--xemu-arg=lpc47m157",
                "--xemu-arg=-serial",
                f"--xemu-arg=file:{uart}",
            ]
        )

    sys.argv = harness_args + forwarded
    try:
        runpy.run_path(str(harness), run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
