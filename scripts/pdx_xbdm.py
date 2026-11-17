#!/usr/bin/env python3
"""Small original-Xbox XBDM client for repository-contained qualification.

Uses native file transfer, title launch and screenshot commands, never UI
input. Protocol reference: https://xboxdevwiki.net/Xbox_Debug_Monitor
"""
import json
from pathlib import Path, PureWindowsPath
import re
import socket
import struct

ROOT = Path(__file__).resolve().parents[1]


def local_path(value):
    path = Path(value).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError('Host artifacts must stay inside this repository')
    return path


def quoted(value):
    if any(c in str(value) for c in ('"', '\r', '\n', '\0')):
        raise ValueError('Invalid XBDM string')
    return '"' + str(value) + '"'


class Xbdm:
    def __init__(self, target, timeout=10):
        self.sock = socket.create_connection((target['debug_host'], target['debug_port']), timeout)
        self.sock.settimeout(timeout)
        self.stream = self.sock.makefile('rb')
        try:
            status = self.line()
        except Exception:
            self.close()
            raise
        if not status.startswith(('201-', '200-')):
            self.close()
            raise RuntimeError(status)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.stream.close()
        self.sock.close()

    def line(self):
        raw = self.stream.readline(65536)
        if not raw.endswith(b'\r\n'):
            raise EOFError('XBDM disconnected or sent an invalid line')
        return raw[:-2].decode('ascii', errors='replace')

    def exact(self, size):
        data = self.stream.read(size)
        if len(data) != size:
            raise EOFError(f'XBDM returned {len(data)} of {size} bytes')
        return data

    def command(self, command):
        if '\r' in command or '\n' in command:
            raise ValueError('Invalid command')
        self.sock.sendall((command + '\r\n').encode('ascii'))
        return self.line()

    def text(self, command):
        status = self.command(command)
        lines = []
        if status.startswith('202-'):
            while True:
                line = self.line()
                if line == '.':
                    break
                lines.append(line)
        elif not status.startswith('200-'):
            raise RuntimeError(status)
        return {'status': status, 'lines': lines}

    def mkdir(self, remote):
        status = self.command('mkdir name=' + quoted(remote))
        if not status.startswith(('200-', '410-')):
            raise RuntimeError(status)
        return status

    def send(self, remote, data):
        status = self.command(f'sendfile name={quoted(remote)} length=0x{len(data):x}')
        if not status.startswith('204-'):
            raise RuntimeError(status)
        self.sock.sendall(data)
        status = self.line()
        if not status.startswith('200-'):
            raise RuntimeError(status)

    def get(self, remote):
        status = self.command('getfile name=' + quoted(remote))
        if not status.startswith('203-'):
            raise RuntimeError(status)
        size = struct.unpack('<I', self.exact(4))[0]
        if size > 64 * 1024 * 1024:
            raise ValueError('Unexpectedly large file')
        return self.exact(size)

    def memory(self, address, size):
        if not 0 < size <= 16 * 1024 * 1024:
            raise ValueError('Invalid read size')
        status = self.command(f'getmem2 addr=0x{address:x} length=0x{size:x}')
        if not status.startswith('203-'):
            raise RuntimeError(status)
        return self.exact(size)

    def screenshot(self, destination):
        from PIL import Image
        path = local_path(destination)
        status = self.command('screenshot')
        if not status.startswith('203-'):
            raise RuntimeError(status)
        header = self.line()
        meta = {k: int(v, 0) for k, v in re.findall(r'(\w+)=(0x[0-9a-fA-F]+|[0-9]+)', header)}
        size = meta['framebuffersize']
        if not 0 < size <= 16 * 1024 * 1024:
            raise ValueError('Invalid native framebuffer size')
        raw = self.exact(size)
        path.with_suffix('.raw').write_bytes(raw)
        path.with_suffix('.json').write_text(json.dumps(meta, indent=2))
        if meta['format'] not in (0x1e, 0x12):
            raise ValueError(f'Unsupported native screenshot format: {meta}')
        Image.frombytes('RGB', (meta['width'], meta['height']), raw,
                        'raw', 'BGRX', meta['pitch'], 1).save(path)
        return meta


def target_config():
    return json.loads((ROOT / '.local/hardware-target.json').read_text())


def deployment_path(relative=''):
    target = target_config()
    base = PureWindowsPath(target['deployment_directory'])
    part = PureWindowsPath(relative)
    if part.is_absolute() or part.drive or '..' in part.parts:
        raise ValueError('Invalid deployment-relative path')
    if base.drive.upper() != 'E:' or not base.name.startswith('PDX-QA-'):
        raise ValueError('Expected isolated PDX-QA folder on E:')
    return str(base / part)
