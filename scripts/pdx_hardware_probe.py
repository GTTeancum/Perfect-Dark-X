#!/usr/bin/env python3
"""Run a bounded process-local issue probe on the configured original Xbox."""
import argparse
import hashlib
import json
import re
import struct
import sys
import threading
import time
sys.dont_write_bytecode = True
from pdx_xbdm import ROOT, Xbdm, target_config, deployment_path, local_path, quoted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True)
    parser.add_argument('--xbe', required=True, type=local_path)
    parser.add_argument('--boot-ini', required=True, type=local_path)
    parser.add_argument('--seconds', type=int, default=100)
    parser.add_argument('--capture-at', nargs='*', type=int, default=[75])
    parser.add_argument('--require', action='append', default=[])
    parser.add_argument('--game-capture', action='store_true', help='Read the game-native completed-frame PPM')
    parser.add_argument('--hal-capture', action='store_true', help='Read the game HAL error framebuffer using its matching pd.map')
    args = parser.parse_args()
    if args.game_capture and args.hal_capture:
        parser.error('Select one capture source')
    if args.game_capture and len(args.capture_at) != 1:
        parser.error('The game fixture produces one completed-frame capture per run')
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', args.name):
        parser.error('Use a simple run name')
    if not 10 <= args.seconds <= 150 or any(t < 0 or t >= args.seconds for t in args.capture_at):
        parser.error('Duration must be 10..150 seconds and captures must precede deadline')
    output = ROOT / '.local/evidence' / args.name
    output.mkdir(exist_ok=False)
    target = target_config()
    xbe = args.xbe.read_bytes()
    report = {'xbe_sha256': hashlib.sha256(xbe).hexdigest(),
              'boot_ini': args.boot_ini.read_text(), 'seconds': args.seconds,
              'captures': [], 'events': [], 'visual_review_required': True,
              'reboot_status': 'not attempted'}
    ready_deadline = time.monotonic() + 45
    while True:
        try:
            with Xbdm(target, timeout=3) as client:
                client.text('dmversion')
            break
        except (OSError, EOFError, RuntimeError):
            if time.monotonic() >= ready_deadline:
                raise
            time.sleep(2)
    with Xbdm(target) as client:
        if b'Perfect-Dark-X' not in client.get(deployment_path('pdx-qa-owner.json')):
            raise RuntimeError('Target is not the owned qualification folder')
        client.send(deployment_path('default.xbe'), xbe)
        if client.get(deployment_path('default.xbe')) != xbe:
            raise RuntimeError('XBE readback mismatch')
        client.send(deployment_path('pdx_boot.ini'), args.boot_ini.read_bytes())
        for name in ('pd.log', 'pdx-qa-frame.ppm'):
            status = client.command('delete name=' + quoted(deployment_path(name)))
            if not status.startswith(('200-', '402-')):
                raise RuntimeError(status)
    rebooted = threading.Event()

    def reboot_to_launcher():
        if rebooted.is_set():
            return
        rebooted.set()
        try:
            with Xbdm(target, timeout=5) as client:
                report['reboot_status'] = client.command('reboot warm')
        except Exception as exc:
            report['reboot_status'] = str(exc)

    started = time.monotonic()
    watchdog = threading.Timer(args.seconds, reboot_to_launcher)
    watchdog.daemon = True
    watchdog.start()
    pending = sorted(args.capture_at)
    last_report = ''
    log = b''
    try:
        with Xbdm(target) as client:
            try:
                report['launch'] = client.command('magicboot title=' + quoted(deployment_path('default.xbe')) + ' debug')
            except EOFError as exc:
                report['launch'] = str(exc)
        print('Launch:', report['launch'], flush=True)
        while time.monotonic() - started < args.seconds - 2 and not rebooted.is_set():
            time.sleep(2)
            elapsed = time.monotonic() - started
            try:
                with Xbdm(target, timeout=5) as client:
                    log = client.get(deployment_path('pd.log'))
                (output / 'pd.log').write_bytes(log)
                markers = [l for l in log.decode(errors='replace').splitlines()
                           if l.startswith(('PDX_INPUT', 'PDX_ISSUE', 'PHASE'))]
                if markers and markers[-1] != last_report:
                    last_report = markers[-1]
                    print(f'{elapsed:.1f}s {last_report}', flush=True)
            except Exception as exc:
                report['events'].append({'elapsed': round(elapsed, 2), 'operation': 'log', 'error': str(exc)})
                if len(report['events']) <= 4:
                    print(f'{elapsed:.1f}s log: {exc}', flush=True)
            try:
                # A failed log transfer must not prevent a native error capture.
                if pending and elapsed >= pending[0] and not rebooted.is_set():
                    pending.pop(0)
                    path = output / f'native-{int(elapsed):03d}.png'
                    with Xbdm(target, timeout=10) as client:
                        if args.hal_capture:
                            from PIL import Image
                            symbols = args.xbe.with_name('pd.map').read_text()
                            def read_symbol(name, size):
                                match = re.search(r'^\s*\S+\s+' + re.escape(name) + r'\s+([0-9a-fA-F]+)\s', symbols, re.M)
                                if not match:
                                    raise ValueError('Missing HAL symbol: ' + name)
                                return client.memory(int(match[1], 16), size)
                            pointer = struct.unpack('<I', read_symbol('__fb', 4))[0]
                            width, height, bpp, refresh = struct.unpack('<4I', read_symbol('_vmCurrent', 16))
                            active = struct.unpack('<I', read_symbol('_pb_debug_screen_active', 4))[0]
                            if bpp != 32 or not 0 < width <= 1920 or not 0 < height <= 1080:
                                raise ValueError('Invalid HAL dimensions')
                            raw = client.memory(pointer, width * height * 4)
                            path.with_suffix('.raw').write_bytes(raw)
                            meta = {'width': width, 'height': height, 'bpp': bpp, 'refresh': refresh,
                                    'source': 'game HAL framebuffer', 'gpu_debug_screen_active': active,
                                    'map_sha256': hashlib.sha256(symbols.encode()).hexdigest()}
                            path.with_suffix('.json').write_text(json.dumps(meta, indent=2))
                            Image.frombytes('RGB', (width, height), raw, 'raw', 'BGRX', width * 4, 1).save(path)
                        elif args.game_capture:
                            from PIL import Image
                            native = client.get(deployment_path('pdx-qa-frame.ppm'))
                            ppm = path.with_suffix('.ppm')
                            ppm.write_bytes(native)
                            with Image.open(ppm) as frame:
                                meta = {'width': frame.width, 'height': frame.height, 'source': 'game completed framebuffer'}
                                frame.save(path)
                        else:
                            meta = client.screenshot(path)
                    report['captures'].append({'elapsed': elapsed, 'path': str(path), 'metadata': meta})
                    print('Native screenshot:', path, flush=True)
            except Exception as exc:
                report['events'].append({'elapsed': round(elapsed, 2), 'operation': 'capture', 'error': str(exc)})
                if len(report['events']) <= 4:
                    print(f'{elapsed:.1f}s {exc}', flush=True)
    finally:
        reboot_to_launcher()
        watchdog.cancel()
        # The deadline callback can still be awaiting a disconnected console.
        # Preserve its outcome before writing the report or ending the process.
        watchdog.join(timeout=18)
        report['elapsed'] = time.monotonic() - started
        report['missing_markers'] = [m for m in args.require if m.encode() not in log]
        report['fault'] = (bool(re.search(rb'FATAL|GPU Error|NV2A TIMEOUT|GPU TIMEOUT|ASSERTION FAILED', log))
                           or any(c['metadata'].get('gpu_debug_screen_active') for c in report['captures']))
        report['execution_ok'] = (bool(log) and not report['missing_markers'] and not report['fault']
                                  and not pending and len(report['captures']) == len(args.capture_at)
                                  and report['reboot_status'].startswith('200-'))
        (output / 'run.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in ('execution_ok', 'missing_markers', 'fault', 'reboot_status')}), flush=True)
    return 0 if report['execution_ok'] else 1


if __name__ == '__main__':
    import msvcrt
    lock_path = ROOT / '.local/xbox-probe.lock'
    with lock_path.open('a+b') as lock:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        raise SystemExit(main())
