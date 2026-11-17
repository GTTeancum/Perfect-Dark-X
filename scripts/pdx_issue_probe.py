#!/usr/bin/env python3
"""Run one repository-contained XEMU probe without host UI/input automation.

Scenarios are executed inside a qualification XBE. Capture uses XEMU's own
screenshot facility; no desktop/window capture or host input is used.
"""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import threading
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
from xemu_smoke import (cap_windows_process_affinity,
                        xemu_find_screenshot_flag_pointer_rva,
                        xemu_trigger_native_screenshot)


def local_file(value):
    path = Path(value).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():
        raise argparse.ArgumentTypeError(f"Expected a file inside {ROOT}: {path}")
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--iso', required=True, type=local_file)
    ap.add_argument('--xemu', type=local_file, default=str(ROOT / '.local/xemu/xemu.exe'))
    ap.add_argument('--config', type=local_file, default=str(ROOT / '.local/xemu/xemu.toml'))
    ap.add_argument('--name', required=True)
    ap.add_argument('--seconds', type=int, default=90)
    ap.add_argument('--capture-at', type=float, nargs='*', default=[])
    ap.add_argument('--native-rva', type=lambda value: int(value, 0),
                    help='Previously verified native screenshot pointer RVA for this XEMU binary')
    ap.add_argument('--require', action='append', default=[], help='Required UART text (repeatable)')
    args = ap.parse_args()
    if not 1 <= args.seconds <= 180:
        ap.error('Run duration must be 1..180 seconds')
    if Path(args.name).name != args.name or args.name in ('.', '..'):
        ap.error('Name must be a simple directory name')
    if any(t < 0 or t >= args.seconds for t in args.capture_at):
        ap.error('Captures must occur within the run')
    config = tomllib.loads(args.config.read_text(encoding='utf-8'))
    for key, value in config.get('sys', {}).get('files', {}).items():
        if value:
            local_file(value)
    for key in ('screenshot_dir', 'games_dir'):
        value = config.get('general', {}).get(key)
        if not value or not Path(value).resolve().is_relative_to(ROOT):
            ap.error(f'{key} must explicitly point inside the repository')
    output = ROOT / '.local/evidence' / args.name
    output.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    for key in ('TEMP', 'TMP', 'APPDATA', 'LOCALAPPDATA', 'XDG_CACHE_HOME',
                'XDG_CONFIG_HOME', '__GL_SHADER_DISK_CACHE_PATH'):
        path = output / key.lower()
        path.mkdir()
        env[key] = str(path)
    env['MESA_SHADER_CACHE_DIR'] = str(output / 'xdg_cache_home')
    env['__GL_SHADER_DISK_CACHE'] = '0'
    rva = args.native_rva
    if args.capture_at and rva is None:
        rva = xemu_find_screenshot_flag_pointer_rva(str(args.xemu))
    if args.capture_at and rva is None:
        ap.error('This XEMU build has no recognized native screenshot entry point')
    command = [str(args.xemu), '-config_path', str(args.config),
               '-dvd_path', str(args.iso), '-device', 'lpc47m157',
               '-serial', 'file:' + str(output / 'uart.log'),
               '-monitor', 'none']
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 7  # SW_SHOWMINNOACTIVE; no window activation/input.
    report = {'command': command, 'captures': [], 'seconds': args.seconds,
              'iso_sha256': hashlib.sha256(args.iso.read_bytes()).hexdigest(),
              'xemu_sha256': hashlib.sha256(args.xemu.read_bytes()).hexdigest()}
    with (output / 'xemu.log').open('wb') as log:
        proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log,
                                stderr=subprocess.STDOUT, startupinfo=startup,
                                creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS)
        expired = threading.Event()
        def stop_at_deadline():
            expired.set()
            if proc.poll() is None:
                proc.kill()
        watchdog = threading.Timer(args.seconds, stop_at_deadline)
        watchdog.daemon = True
        watchdog.start()
        print(f'XEMU pid={proc.pid}, watchdog={args.seconds}s, output={output}', flush=True)
        started = time.monotonic()
        pending = sorted(args.capture_at)
        try:
            cap_windows_process_affinity(proc.pid)
            while proc.poll() is None and time.monotonic() - started < args.seconds:
                elapsed = time.monotonic() - started
                if pending and elapsed >= pending[0]:
                    pending.pop(0)
                    ok, detail, path = xemu_trigger_native_screenshot(
                        proc.pid, str(args.xemu), config['general']['screenshot_dir'],
                        pointer_rva=rva)
                    report['captures'].append({'elapsed': elapsed, 'ok': ok,
                                               'detail': detail, 'path': path})
                    print(detail, flush=True)
                time.sleep(0.2)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
            watchdog.cancel()
            report['elapsed'] = time.monotonic() - started
            report['exit_code'] = proc.returncode
            uart = (output / 'uart.log').read_bytes() if (output / 'uart.log').exists() else b''
            report['completed_duration'] = expired.is_set() or report['elapsed'] >= args.seconds
            report['missing_markers'] = [s for s in args.require if s.encode() not in uart]
            report['fault'] = bool(re.search(
                rb'FATAL|GPU Error|NV2A TIMEOUT|GPU TIMEOUT|Unhandled exception|ASSERTION FAILED', uart))
            report['execution_ok'] = (report['completed_duration'] and not report['fault']
                and not report['missing_markers'] and not pending
                and all(c['ok'] for c in report['captures']))
            (output / 'run.json').write_text(json.dumps(report, indent=2))
    return 0 if report['execution_ok'] else 1


if __name__ == '__main__':
    # A second emulator must never share a writable guest disk with this one.
    import msvcrt
    lock_path = ROOT / '.local/xemu-probe.lock'
    lock_path.parent.mkdir(exist_ok=True)
    with lock_path.open('a+b') as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise SystemExit('Another repository XEMU probe is still running')
        raise SystemExit(main())
