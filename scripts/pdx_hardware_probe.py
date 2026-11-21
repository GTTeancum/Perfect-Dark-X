#!/usr/bin/env python3
"""Run a bounded process-local issue probe on the configured original Xbox."""
import argparse
from contextlib import contextmanager, nullcontext
import hashlib
import json
import re
import struct
import sys
import threading
import time
sys.dont_write_bytecode = True
from pdx_xbdm import ROOT, Xbdm, target_config, deployment_path, local_path, quoted


@contextmanager
def stopped_title(client):
    """Debugger stop/go for a stable native read, with no controller input."""
    try:
        status = client.command('stop')
        if not status.startswith('200-'):
            raise RuntimeError('Cannot stop title for capture: ' + status)
        time.sleep(0.1)  # Allow already-submitted GPU work to finish.
        yield
    finally:
        status = client.command('go')
        if not status.startswith('200-'):
            raise RuntimeError('Cannot resume title after capture: ' + status)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True)
    parser.add_argument('--xbe', required=True, type=local_path)
    parser.add_argument('--boot-ini', required=True, type=local_path)
    parser.add_argument('--seconds', type=int, default=100)
    parser.add_argument('--capture-at', nargs='*', type=int, default=[75])
    parser.add_argument('--require', action='append', default=[])
    parser.add_argument('--min-free-pages', type=int, default=0, help='Require successful qualification memory samples above this 4 KiB page floor')
    parser.add_argument('--game-capture', action='store_true', help='Read the game-native completed-frame PPM')
    parser.add_argument('--hal-capture', action='store_true', help='Read the game HAL error framebuffer using its matching pd.map')
    parser.add_argument('--framebuffer-capture', action='store_true', help='Stop the title briefly and read its completed framebuffer using its matching pd.map; use after timed input tests')
    args = parser.parse_args()
    if sum((args.game_capture, args.hal_capture, args.framebuffer_capture)) > 1:
        parser.error('Select one capture source')
    if args.game_capture and len(args.capture_at) != 1:
        parser.error('The game fixture produces one completed-frame capture per run')
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', args.name):
        parser.error('Use a simple run name')
    if not 10 <= args.seconds <= 150 or any(t < 0 or t >= args.seconds for t in args.capture_at):
        parser.error('Duration must be 10..150 seconds and captures must precede deadline')
    if args.min_free_pages < 0:
        parser.error('Memory floor must be nonnegative')
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
                if b'FATAL' in log:
                    print('Fatal log marker; ending the run and returning to launcher', flush=True)
                    break
                markers = [l for l in log.decode(errors='replace').splitlines()
                           if l.startswith(('PDX_INPUT', 'PDX_ISSUE', 'PDX_PAUSE', 'PHASE'))]
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
                        if args.hal_capture or args.framebuffer_capture:
                            if args.framebuffer_capture and b'PDX_INPUT begin' in log and b'PDX_INPUT complete' not in log:
                                raise ValueError('Refusing debugger pause during timed input test')
                            with stopped_title(client) if args.framebuffer_capture else nullcontext():
                                from PIL import Image
                                symbols = args.xbe.with_name('pd.map').read_text()
                                def read_symbol(name, size):
                                    match = re.search(r'^\s*\S+\s+' + re.escape(name) + r'\s+([0-9a-fA-F]+)\s', symbols, re.M)
                                    if not match:
                                        raise ValueError('Missing framebuffer symbol: ' + name)
                                    return client.memory(int(match[1], 16), size)
                                active = struct.unpack('<I', read_symbol('_pb_debug_screen_active', 4))[0]
                                if args.framebuffer_capture:
                                    def word(name):
                                        return struct.unpack('<I', read_symbol('_g_SPXBFramebuffer' + name, 4))[0]
                                    pointer, pitch, width, height = (word(n) for n in ('Data', 'Pitch', 'Width', 'Height'))
                                    bpp, refresh = 32, None
                                else:
                                    pointer = struct.unpack('<I', read_symbol('__fb', 4))[0]
                                    width, height, bpp, refresh = struct.unpack('<4I', read_symbol('_vmCurrent', 16))
                                    pitch = width * 4
                                if bpp != 32 or not 0 < width <= 1920 or not 0 < height <= 1080:
                                    raise ValueError('Invalid framebuffer dimensions')
                                if not pointer or not width * 4 <= pitch <= 8192:
                                    raise ValueError('Invalid framebuffer pointer or pitch')
                                raw = client.memory(pointer, pitch * height)
                                if args.framebuffer_capture and raw != client.memory(pointer, pitch * height):
                                    raise ValueError('Framebuffer changed while title stopped')
                                path.with_suffix('.raw').write_bytes(raw)
                                meta = {'width': width, 'height': height, 'bpp': bpp, 'refresh': refresh,
                                        'source': 'renderer completed framebuffer' if args.framebuffer_capture else 'game HAL framebuffer',
                                        'pitch': pitch, 'gpu_debug_screen_active': active,
                                        'map_sha256': hashlib.sha256(symbols.encode()).hexdigest()}
                                if args.framebuffer_capture:
                                    meta['capture_method'] = 'XBDM stop/go; two identical native framebuffer reads'
                                path.with_suffix('.json').write_text(json.dumps(meta, indent=2))
                                Image.frombytes('RGB', (width, height), raw, 'raw', 'BGRX', pitch, 1).save(path)
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
        pages = [int(n) for n in re.findall(rb'PDX_MEMORY status=00000000 available_pages=(\d+)', log)]
        report['minimum_free_pages'] = min(pages) if pages else None
        report['memory_ok'] = not args.min_free_pages or bool(pages and min(pages) >= args.min_free_pages)
        report['fault'] = (bool(re.search(rb'FATAL|GPU Error|NV2A TIMEOUT|GPU TIMEOUT|ASSERTION FAILED', log))
                           or any(c['metadata'].get('gpu_debug_screen_active') for c in report['captures']))
        report['execution_ok'] = (bool(log) and not report['missing_markers'] and not report['fault'] and report['memory_ok']
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
