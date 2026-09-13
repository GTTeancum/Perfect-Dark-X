#!/usr/bin/env python3
"""Run sequential Xbox video soaks with native captures and UART liveness checks.

No host input is generated. Screenshots use XEMU's process-local native capture
facility through the existing smoke harness. A passing run establishes emulator
liveness and mode selection; visual review and hardware qualification are separate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
MODES = {
    "ntsc480i": ("ntsc", "480i", "4:3", 640, 480, 60),
    "pal50": ("pal50", "480i", "4:3", 640, 480, 50),
    "pal60": ("pal60", "480i", "16:9", 640, 480, 60),
    "480p": ("ntsc", "480p", "4:3", 640, 480, 60),
    "720p": ("ntsc", "720p", "16:9", 1280, 720, 60),
}
FAULT = re.compile(
    rb"FATAL(?: ERROR|:)|GPU TIMEOUT|NV2A TIMEOUT|pb_reset: (?:too long|bad getaddr)|"
    rb"Push buffer overflow|Unhandled exception|ASSERTION FAILED", re.I
)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--xbe", type=Path, required=True)
    parser.add_argument("--map-file", type=Path, required=True)
    parser.add_argument("--source-config", type=Path, required=True)
    parser.add_argument("--source-eeprom", type=Path, required=True)
    parser.add_argument("--xemu-exe", type=Path, default=Path(
        r"C:\Games\Emulators\Xemu\PerfectDarkX\xemu.exe"))
    parser.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    parser.add_argument("--duration", type=int, default=600)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--first-shot-delay", type=float, default=20,
                        help="Seconds before the first native capture; use 0 for menu transitions")
    parser.add_argument("--name", default="pdx-video-soak")
    parser.add_argument("--scenario", choices=("gameplay", "menu"), required=True,
                        help="Scene packaged in the ISO's qualification boot config")
    parser.add_argument("--port", type=int, default=4481)
    parser.add_argument("--require-safe-present", action="store_true",
                        help="Require presentation trace windows with zero scanout overlap")
    parser.add_argument("--native-rva", help="Previously verified native capture flag pointer RVA")
    parser.add_argument("--dump-ram", action="store_true",
                        help="Save final guest RAM for diagnosing a stalled guest thread")
    args = parser.parse_args()
    if args.duration < 120 or args.interval < 1:
        parser.error("use at least 120 seconds and a positive capture interval")
    for name in ("iso", "xbe", "map_file", "source_config", "source_eeprom", "xemu_exe"):
        path = getattr(args, name).resolve()
        if not path.is_file():
            parser.error(f"missing {name}: {path}")
        setattr(args, name, path)

    run_id = f"{args.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    output = ROOT / "scripts" / "output" / run_id
    output.mkdir(parents=True)
    summary = {
        "run": run_id, "scenario": args.scenario, "duration_per_mode": args.duration,
        "iso": str(args.iso), "iso_sha256": sha256(args.iso),
        "xbe": str(args.xbe), "xbe_sha256": sha256(args.xbe),
        "xemu_sha256": sha256(args.xemu_exe),
        "visual_review": "pending", "hardware_verified": False, "modes": [],
    }
    summary_path = output / "summary.json"

    for mode in args.modes:
        standard, resolution, aspect, width, height, refresh = MODES[mode]
        config = output / f"{mode}.toml"
        subprocess.run([
            sys.executable, str(ROOT / "tools/texturepack/make_xemu_hd_profile.py"),
            "--source-config", str(args.source_config), "--source-eeprom", str(args.source_eeprom),
            "--output-config", str(config), "--output-eeprom", str(output / f"{mode}.bin"),
            "--standard", standard, "--resolution", resolution, "--aspect", aspect,
        ], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
        name = f"{run_id}-{mode}"
        console = output / f"{mode}.console.log"
        command = [
            sys.executable, str(ROOT / "scripts/pdx_xemu_smoke.py"),
            "--iso", str(args.iso), "--map-file", str(args.map_file),
            "--config-path", str(config), "--xemu-exe", str(args.xemu_exe),
            "--name", name, "--duration", str(args.duration),
            "--interval", str(args.interval), "--first-shot-delay", str(args.first_shot_delay),
            "--port", str(args.port), "--uart", "--screenshots",
            "--xemu-native-screenshots", "--xemu-screenshot-dir", str(args.xemu_exe.parent),
        ]
        if args.native_rva:
            command.extend(["--xemu-screenshot-flag-rva", args.native_rva])
        if args.dump_ram:
            command.extend(["--dump-phys", "0:0x4000000:guest_ram"])
        print(f"Starting {mode}: {args.duration}s; {console}", flush=True)
        start = last_growth = time.monotonic()
        previous_frames = 0
        growth_samples = []
        stalls = []
        uart = None
        with console.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            while process.poll() is None:
                time.sleep(10)
                paths = list((ROOT / "scripts/output").glob(f"{name}_*.uart.bin"))
                if paths:
                    uart = paths[0]
                    data = uart.read_bytes()
                    frames = data.count(b"NV2A PERF wall_fps=")
                    now = time.monotonic()
                    if frames > previous_frames:
                        growth_samples.append(round(now - start, 1))
                        last_growth = now
                        previous_frames = frames
                    elif now - start > 90 and now - last_growth > 60:
                        stalls.append(round(now - start, 1))
        data = uart.read_bytes() if uart else b""
        text = console.read_text(encoding="utf-8", errors="replace")
        fps = [float(value) for value in re.findall(rb"NV2A PERF wall_fps=([\d.]+)", data)]
        expected = f"verified raster video={width}x{height} pbkit={width}x{height}"
        mode_ok = bool(re.search(re.escape(expected.encode()) + rb"[^\n]*refresh=" + str(refresh).encode() + rb"Hz", data))
        shots = re.findall(r"shot=\d+ t=([\d.]+) ok=True .*?file=(.+)", text)
        failed_shots = len(re.findall(r"shot=\d+ .*?ok=False", text))
        faults = [match.group().decode(errors="replace") for match in FAULT.finditer(data)]
        presentation = [tuple(map(int, row)) for row in re.findall(
            rb"NV2A PERF PRESENT frames=(\d+) start_overlap=(\d+) end_overlap=(\d+) acquire_waits=(\d+)", data)]
        present_totals = [sum(row[index] for row in presentation) for index in range(4)]
        deferred_swaps = [int(value) for value in re.findall(rb"deferred_swaps=(\d+)", data)]
        present_ok = bool(presentation and present_totals[1] == 0 and present_totals[2] == 0)
        alive = "alive_at_end" in text
        liveness_ok = bool(growth_samples and growth_samples[-1] >= args.duration - 30 and not stalls)
        result = {
            "mode": mode, "mode_verified": mode_ok, "process_alive_at_end": alive,
            "liveness_verified": liveness_ok, "stall_sample_seconds": stalls,
            "perf_windows": len(fps), "fps_window_min": min(fps) if fps else None,
            "fps_window_max": max(fps) if fps else None,
            "fps_window_mean": sum(fps) / len(fps) if fps else None,
            "faults": faults, "native_captures": shots, "failed_captures": failed_shots,
            "presentation_trace": dict(zip(
                ("frames", "start_overlap", "end_overlap", "acquire_waits"), present_totals)),
            "pbkit_deferred_swaps": max(deferred_swaps, default=0),
            "uart": str(uart), "console": str(console), "returncode": process.returncode,
            "emulator_checks_passed": bool(process.returncode == 0 and mode_ok and alive and liveness_ok and not faults and shots and not failed_shots and (not args.require_safe_present or present_ok)),
        }
        summary["modes"].append(result)
        summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(f"Finished {mode}: emulator checks={result['emulator_checks_passed']}; visual review pending", flush=True)
        if not result["emulator_checks_passed"]:
            print("Stopping the matrix after a failed mode; inspect its evidence before retrying.", flush=True)
            break
    print(f"Summary: {summary_path}", flush=True)
    return 0 if all(result["emulator_checks_passed"] for result in summary["modes"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
