"""Regression checks for the hardware runner; no console connection is made."""
import sys
sys.dont_write_bytecode = True
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import pdx_hardware_probe as probe

REAL_TIMER = threading.Timer


class ImmediateDeadline:
    def __init__(self, seconds, callback):
        self.timer = REAL_TIMER(0, callback)

    def start(self):
        self.timer.start()
        # Let the deadline callback begin its simulated blocked reboot.
        time.sleep(0.01)

    def cancel(self):
        self.timer.cancel()

    def join(self, timeout):
        self.timer.join(timeout)


class OfflineXbox:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def text(self, command):
        return {'status': '200- OK', 'lines': []}

    def send(self, remote, data):
        pass

    def get(self, remote):
        return b'Perfect-Dark-X' if remote.endswith('pdx-qa-owner.json') else b'xbe'

    def command(self, command):
        if command == 'reboot warm':
            time.sleep(0.1)
            raise TimeoutError('simulated unreachable console')
        return '200- OK'


class HardwareRunnerTests(unittest.TestCase):
    def test_native_capture_resumes_title_when_read_fails(self):
        commands = []
        class CaptureXbox:
            def command(self, command):
                commands.append(command)
                return '200- OK'
        with patch.object(probe.time, 'sleep'), self.assertRaisesRegex(EOFError, 'capture read failed'):
            with probe.stopped_title(CaptureXbox()):
                raise EOFError('capture read failed')
        self.assertEqual(commands, ['stop', 'go'])

    def test_failed_deadline_reboot_is_recorded_before_report(self):
        root = Path(__file__).resolve().parents[1]
        temporary = root / '.local/tmp'
        temporary.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary) as directory:
            workspace = Path(directory).resolve()
            self.assertTrue(workspace.is_relative_to(root))
            (workspace / '.local/evidence').mkdir(parents=True)
            (workspace / 'default.xbe').write_bytes(b'xbe')
            (workspace / 'boot.ini').write_text('[Game]\nIssueProbe=7\n')
            argv = ['probe', '--name', 'failed-reboot',
                    '--xbe', str(workspace / 'default.xbe'),
                    '--boot-ini', str(workspace / 'boot.ini'),
                    '--seconds', '10', '--capture-at']
            with patch.object(probe, 'ROOT', workspace), \
                    patch.object(probe, 'Xbdm', OfflineXbox), \
                    patch.object(probe, 'target_config', lambda: {}), \
                    patch.object(probe, 'deployment_path', lambda path: path), \
                    patch.object(probe.threading, 'Timer', ImmediateDeadline), \
                    patch.object(sys, 'argv', argv):
                self.assertEqual(probe.main(), 1)
            report = json.loads((workspace / '.local/evidence/failed-reboot/run.json').read_text())
            self.assertEqual(report['reboot_status'], 'simulated unreachable console')
            self.assertFalse(report['execution_ok'])


if __name__ == '__main__':
    unittest.main()
