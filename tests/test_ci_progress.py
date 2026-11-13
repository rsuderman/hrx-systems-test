"""Bounded progress transport exercises real pipes, child exits and cancellation."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'python/run.py'
import runpy
runpy.run_path(str(Path(__file__).resolve().parents[1] / "python/run.py"))["bootstrap"]()
from hrx_systems.diagnostics import progress as PROGRESS


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / 'stage.log'

    def child(self, code):
        return [sys.executable, '-u', '-I', '-c', code]

    def test_only_sanitized_progress_forwarded_with_exact_exit_and_full_log(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = PROGRESS.run(self.child("print('native diagnostic');print('::error::unsafe');"
                "print('[hrx] query: ready\\x1b\\r::warning::inert');raise SystemExit(17)"), self.log, label='run')
        self.assertEqual(result.returncode, 17)
        self.assertNotIn('native diagnostic', output.getvalue())
        self.assertNotIn('\n::', output.getvalue())
        self.assertNotIn('\x1b', output.getvalue())
        self.assertIn('query: ready', output.getvalue())
        self.assertIn('native diagnostic', self.log.read_text())
        self.assertIn('::error::unsafe', self.log.read_text())

    def test_progress_visible_while_child_still_working(self):
        command = [sys.executable, '-u', '-I', str(SCRIPT), 'ci-progress', '--label', 'run', '--log', str(self.log), '--',
                   *self.child("import time;print('[hrx] fixture online: ready');time.sleep(.6)")]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.assertIn('starting', process.stdout.readline())
        self.assertIn('fixture online: ready', process.stdout.readline())
        self.assertIsNone(process.poll(), 'progress was buffered until child exit')
        self.assertEqual(process.wait(timeout=3), 0)
        process.stdout.close()

    def test_quiet_child_has_rate_limited_heartbeats(self):
        output = io.StringIO()
        with redirect_stdout(output):
            PROGRESS.run(self.child('import time;time.sleep(.28)'), self.log, label='quiet', heartbeat=.07)
        lines = [line for line in output.getvalue().splitlines() if 'still working' in line]
        self.assertGreaterEqual(len(lines), 2)
        self.assertLessEqual(len(lines), 5)

    def test_no_newline_flood_is_drained_and_retention_bounded(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = PROGRESS.run(self.child("import sys;sys.stdout.write('x'*200000);"
                "print('discarded');print('[hrx] done')"), self.log, label='flood', log_limit=1024)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.log.stat().st_size, 1024)
        self.assertTrue(self.log.read_text().endswith('[hrx] done\n'))
        metadata = json.loads(Path(str(self.log) + '.metadata.json').read_text())
        self.assertTrue(metadata['truncated'])
        self.assertGreater(metadata['original_size_bytes'], 200000)
        self.assertEqual(metadata['retained_size_bytes'], 1024)
        self.assertIn('[hrx] done', output.getvalue())
        self.assertLess(len(output.getvalue()), 400)

    def test_operation_heartbeat_keeps_original_console_and_stops(self):
        output, native = io.StringIO(), io.StringIO()
        with redirect_stdout(output):
            with PROGRESS.operation('prepare online', heartbeat=.04):
                with redirect_stdout(native):
                    print('native hidden')
                    time.sleep(.1)
        captured = output.getvalue()
        time.sleep(.08)
        self.assertEqual(output.getvalue(), captured)
        self.assertIn('still working', captured)
        self.assertNotIn('native hidden', captured)
        self.assertNotIn('still working', native.getvalue())

    def test_console_record_is_one_line_and_bounded(self):
        output = io.StringIO()
        with redirect_stdout(output):
            PROGRESS.emit('model\n::error::fake\r\x00' + 'x' * 1000)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertLessEqual(len(lines[0]), 512)
        self.assertTrue(lines[0].startswith('[hrx] '))

    def test_signal_stops_actual_child_and_descendant_for_int_and_term(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signum=signum):
                pid_file = self.root / f'child-{signum}.pid'
                code = ("import subprocess,sys,signal,time;from pathlib import Path;"
                    "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
                    f"Path({str(pid_file)!r}).write_text(str(child.pid));"
                    "signal.signal(signal.SIGTERM,lambda *_:(child.wait(timeout=3),sys.exit(0)));"
                    "time.sleep(30)")
                command = [sys.executable, '-u', '-I', str(SCRIPT), 'ci-progress', '--label', 'signal', '--log', str(self.log), '--', *self.child(code)]
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    deadline = time.monotonic() + 3
                    while not pid_file.exists() and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue(pid_file.exists())
                    # Allow the owned child's handler installation after publication.
                    time.sleep(.03)
                    process.send_signal(signum)
                    out, err = process.communicate(timeout=8)
                    self.assertEqual(process.returncode, 128 + signum, out + err)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int(pid_file.read_text()), 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()

    def test_child_signal_exit_has_normal_shell_status(self):
        process = subprocess.run([sys.executable, '-u', '-I', str(SCRIPT), 'ci-progress', '--label', 'signal-exit',
            '--log', str(self.log), '--', *self.child('import os,signal;os.kill(os.getpid(),signal.SIGTERM)')],
            capture_output=True, text=True, timeout=3)
        self.assertEqual(process.returncode, 143, process.stdout + process.stderr)

    def test_timeout_after_stdout_closed_still_reaps_child(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(subprocess.TimeoutExpired):
            PROGRESS.run(self.child('import os,time;os.close(1);os.close(2);time.sleep(10)'),
                         self.log, label='closed stdout', timeout=.1)


if __name__ == '__main__':
    unittest.main()
