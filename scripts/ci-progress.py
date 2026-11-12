#!/usr/bin/env python3
"""Small trusted progress producer and subprocess transport, shared by CI/local use."""
import argparse
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import threading
import time

PREFIX = '[hrx] '
HEARTBEAT_SECONDS = 30
MAX_MESSAGE = 512
MAX_LOG_BYTES = 4 * 1024 * 1024
_output_lock = threading.Lock()
_summary = None


def summarize(diagnostic):
    """Use the shared failure formatter when shipped with the producer."""
    global _summary
    if _summary is None:
        path = Path(__file__).with_name('failure-records.py')
        if path.is_file():
            spec = importlib.util.spec_from_file_location('progress_failure_records', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _summary = module.summarize
        else:
            _summary = str
    return _summary(diagnostic)


class Interrupted(KeyboardInterrupt):
    def __init__(self, signum):
        self.signum = signum


def clean(value, limit=MAX_MESSAGE - len(PREFIX)):
    """Keep dynamic values on one inert console line, including ANSI/control text."""
    text = ' '.join(''.join(ch if ch.isprintable() else ' ' for ch in str(value)).split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def emit(message, *, stream=None):
    with _output_lock:
        print(PREFIX + clean(message), file=stream or sys.stdout, flush=True)


@contextmanager
def operation(label, *, timeout=None, heartbeat=HEARTBEAT_SECONDS):
    """Describe a quiet native operation without forwarding its diagnostic stream."""
    started = time.monotonic()
    stopped = threading.Event()
    console = sys.stdout
    emit(label + ': starting', stream=console)

    def beat():
        while not stopped.wait(heartbeat):
            elapsed = time.monotonic() - started
            emit(f'{label}: still working ({elapsed:.0f}s' +
                 (f'; timeout {timeout:g}s)' if timeout is not None else ')'), stream=console)

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        yield
    except BaseException as error:
        stopped.set()
        thread.join()
        emit(f'{label}: FAIL {summarize(f"{type(error).__name__}: {error}")} ({time.monotonic() - started:.1f}s)', stream=console)
        raise
    else:
        stopped.set()
        thread.join()
        emit(f'{label}: complete ({time.monotonic() - started:.1f}s)', stream=console)
    finally:
        stopped.set()
        thread.join()


def run(command, log_path, *, label, timeout=None, check=False,
        heartbeat=HEARTBEAT_SECONDS, log_limit=MAX_LOG_BYTES):
    """Own the real child group; capture diagnostics and forward only progress lines.

    A bounded partial-line buffer prevents a noisy/no-newline child exhausting memory.
    Signal/timeout cleanup includes descendants even if the direct child has exited.
    Native server diagnostics are captured separately, never sent to this transport.
    """
    started = last_event = time.monotonic()
    signal_received = None
    handlers = {}
    child = None
    completed = capture_complete = False
    written = total = 0
    head, tail = bytearray(), bytearray()
    gap = b'\n[hrx log truncated; retained head and tail; see metadata]\n'
    if len(gap) >= log_limit:
        gap = b''
    head_limit = (log_limit - len(gap)) // 2
    tail_limit = log_limit - len(gap) - head_limit
    emit(f'{label}: starting')

    def interrupted(signum, frame):
        nonlocal signal_received
        signal_received = signum

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            handlers[sig] = signal.signal(sig, interrupted)
        with Path(log_path).open('wb') as log:
            child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     start_new_session=True)
            pending = b''
            discard_line = False
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if signal_received is not None:
                        raise Interrupted(signal_received)
                    elapsed = time.monotonic() - started
                    if timeout is not None and elapsed >= timeout:
                        raise subprocess.TimeoutExpired(command, timeout)
                    for key, _ in selector.select(min(0.2, heartbeat)):
                        chunk = os.read(key.fileobj.fileno(), 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            break
                        kept = chunk[:max(0, log_limit - written)]
                        log.write(kept)
                        written += len(kept)
                        total += len(chunk)
                        head.extend(chunk[:max(0, head_limit - len(head))])
                        tail.extend(chunk)
                        if len(tail) > tail_limit:
                            del tail[:len(tail) - tail_limit]
                        log.flush()
                        pending += chunk
                        while b'\n' in pending:
                            line, pending = pending.split(b'\n', 1)
                            if not discard_line and line.startswith(PREFIX.encode()):
                                emit(line.decode('utf-8', 'replace')[len(PREFIX):])
                                last_event = time.monotonic()
                            discard_line = False
                        if len(pending) > 8192:
                            pending = b''
                            discard_line = True
                    if time.monotonic() - last_event >= heartbeat:
                        emit(f'{label}: still working ({elapsed:.0f}s' +
                             (f'; timeout {timeout:g}s)' if timeout is not None else ')'))
                        last_event = time.monotonic()
            if pending and not discard_line and pending.startswith(PREFIX.encode()):
                emit(pending.decode('utf-8', 'replace')[len(PREFIX):])
            # A process may close its stdout before it exits. Keep observing the same
            # deadline/signals rather than waiting forever after the pipe reaches EOF.
            while child.poll() is None:
                if signal_received is not None:
                    raise Interrupted(signal_received)
                elapsed = time.monotonic() - started
                if timeout is not None and elapsed >= timeout:
                    raise subprocess.TimeoutExpired(command, timeout)
                if time.monotonic() - last_event >= heartbeat:
                    emit(f'{label}: still working ({elapsed:.0f}s)')
                    last_event = time.monotonic()
                time.sleep(min(0.05, heartbeat))
            if signal_received is not None:
                raise Interrupted(signal_received)
            result = subprocess.CompletedProcess(command, child.returncode)
            capture_complete = True
            emit(f'{label}: exited {result.returncode} ({time.monotonic() - started:.1f}s)')
            if check:
                result.check_returncode()
            completed = True
            return result
    finally:
        if child is not None:
            # Kill the owned process group, including descendants that inherited a
            # pipe after their parent exited. No unrelated process/group is touched.
            if not completed:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    pass
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=5)
            child.stdout.close()
        for sig, previous in handlers.items():
            signal.signal(sig, previous)
        if Path(log_path).is_file():
            if total > log_limit:
                with Path(log_path).open('r+b') as log:
                    log.write(head)
                    log.write(gap)
                    log.write(tail)
                    log.truncate()
            metadata = {'original_size_bytes': total if capture_complete else None,
                        'observed_size_bytes': total,
                        'retained_size_bytes': min(total, log_limit),
                        'capture_complete': capture_complete, 'truncated': total > log_limit,
                        'captured_ranges': ([[0, head_limit], [total - tail_limit, total]] if total > log_limit
                                            else [[0, total]])}
            Path(str(log_path) + '.metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log', type=Path, required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('a child command is required')
    try:
        code = run(command, args.log, label=args.label).returncode
        return code if code >= 0 else 128 - code
    except Interrupted as error:
        # GitHub/shell callers use SIGTERM as cancellation; this wrapper must stay
        # nonzero and the owning shell retains its own exact interruption status.
        emit(f'{args.label}: interrupted; child group stopped')
        return 128 + error.signum


if __name__ == '__main__':
    sys.exit(main())
