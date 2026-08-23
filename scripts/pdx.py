#!/usr/bin/env python3
"""
pdx.py — drive the Perfect Dark X xemu instance without ever touching the
user's window focus.

  launch   start xemu with serial + QEMU monitor, leave it running
  shot     screendump the guest framebuffer via the monitor -> PNG
  stop     kill it
  serial   tail the UART log

xemu inherits QEMU's monitor, so `screendump` gives the real scanned-out
frame. That beats PrintWindow, which cannot read an OpenGL surface.
"""

import os
import socket
import struct
import subprocess
import sys
import time
import zlib

DIR = os.path.dirname(os.path.abspath(__file__))
INST = r'C:\Games\Emulators\Xemu\PerfectDarkX'
XEMU = os.path.join(INST, 'xemu.exe')
SERIAL = os.path.join(DIR, 'pdx-serial-live.txt')
MON_HOST, MON_PORT = '127.0.0.1', 55555


def launch():
    subprocess.run(['taskkill', '/F', '/IM', 'xemu.exe'],
                   capture_output=True)
    time.sleep(0.6)
    for f in (SERIAL,):
        try:
            os.remove(f)
        except OSError:
            pass
    args = [XEMU,
            '-device', 'lpc47m157',
            '-serial', 'file:' + SERIAL,
            '-monitor', 'telnet:%s:%d,server,nowait' % (MON_HOST, MON_PORT)]
    p = subprocess.Popen(args, cwd=INST,
                         stdout=open(os.path.join(DIR, 'xemu-out.txt'), 'wb'),
                         stderr=subprocess.STDOUT)
    print('launched pid=%d' % p.pid)
    return p


def monitor(cmd, settle=0.4):
    s = socket.create_connection((MON_HOST, MON_PORT), timeout=10)
    s.settimeout(2.0)
    time.sleep(0.3)
    try:
        s.recv(65536)          # banner
    except socket.timeout:
        pass
    s.sendall((cmd + '\n').encode())
    time.sleep(settle)
    out = b''
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
    except socket.timeout:
        pass
    s.close()
    return out.decode('utf-8', 'replace')


def _png(path, w, h, rgb):
    def chunk(tag, data):
        c = tag + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
    raw = b''.join(b'\x00' + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))
    png = (b'\x89PNG\r\n\x1a\n'
           + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
           + chunk(b'IDAT', zlib.compress(raw, 6))
           + chunk(b'IEND', b''))
    open(path, 'wb').write(png)


def shot(tag='shot'):
    ppm = os.path.join(DIR, 'dump-%s.ppm' % tag).replace('\\', '/')
    try:
        os.remove(ppm)
    except OSError:
        pass
    monitor('screendump %s' % ppm, settle=1.2)
    for _ in range(20):
        if os.path.exists(ppm) and os.path.getsize(ppm) > 64:
            break
        time.sleep(0.3)
    if not os.path.exists(ppm):
        print('screendump produced nothing')
        return None

    data = open(ppm, 'rb').read()
    # P6 <w> <h> <maxval>\n
    parts, idx = [], 0
    while len(parts) < 4:
        while idx < len(data) and data[idx:idx + 1].isspace():
            idx += 1
        if data[idx:idx + 1] == b'#':
            while data[idx:idx + 1] != b'\n':
                idx += 1
            continue
        start = idx
        while idx < len(data) and not data[idx:idx + 1].isspace():
            idx += 1
        parts.append(data[start:idx])
    idx += 1
    w, h = int(parts[1]), int(parts[2])
    rgb = data[idx:idx + w * h * 3]

    png = os.path.join(DIR, 'dump-%s.png' % tag)
    _png(png, w, h, rgb)

    # quick content stats so a black/frozen frame is obvious without looking
    nb = 0
    step = max(1, (w * h) // 4000)
    n = 0
    for i in range(0, w * h, step):
        r, g, b = rgb[i * 3], rgb[i * 3 + 1], rgb[i * 3 + 2]
        n += 1
        if r > 12 or g > 12 or b > 12:
            nb += 1
    print('%s  %dx%d  nonblack %d/%d (%.1f%%)  -> %s'
          % (tag, w, h, nb, n, 100.0 * nb / max(1, n), png))
    return png


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'shot'
    if cmd == 'launch':
        launch()
    elif cmd == 'shot':
        shot(sys.argv[2] if len(sys.argv) > 2 else 'shot')
    elif cmd == 'stop':
        subprocess.run(['taskkill', '/F', '/IM', 'xemu.exe'], capture_output=True)
        print('stopped')
    elif cmd == 'mon':
        print(monitor(' '.join(sys.argv[2:])))
    elif cmd == 'serial':
        print(open(SERIAL, 'rb').read().decode('utf-8', 'replace')[-4000:])
