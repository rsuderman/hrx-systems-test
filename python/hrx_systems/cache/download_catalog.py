#!/usr/bin/env python3
"""Download the pinned small-model catalog through the existing native seeder."""
import argparse
from .. import layout
import json
import os
from pathlib import Path
import pwd
import shutil
import signal
import subprocess
import sys
import time
import uuid

from ..layout import REPO_ROOT as REPO
from . import seed_suite as SUITE


def run(args):
    selection = json.loads((REPO / 'config/download-models-under-20gb.json').read_text())
    entries = selection['entries']
    if args.model:
        unknown = set(args.model) - {e['model_repository'] for e in entries}
        if unknown:
            raise ValueError(f'models absent from download selection: {sorted(unknown)}')
        entries = [e for e in entries if e['model_repository'] in args.model]
    root = Path(args.root or os.environ.get('HRX_SYSTEMS_ROOT', Path.home() / '.local/share/hrx-systems-test')).expanduser()
    if not root.is_absolute() or root == Path('/'):
        raise ValueError('root must be an absolute directory other than /')
    reader_user = getattr(args, 'reader_user', None)
    uid, setfacl = None, None
    if reader_user:
        try:
            uid = pwd.getpwnam(reader_user).pw_uid
        except KeyError as error:
            raise ValueError(f'unknown reader account: {reader_user}') from error
        setfacl = shutil.which('setfacl')
        if not setfacl:
            raise ValueError('--reader-user requires setfacl; ask the machine owner to install it')
    print(f'{len(entries)} publications; {sum(e["gguf_bytes"] for e in entries) / 1e9:.1f} GB before deduplication/cache reuse', flush=True)
    print(f'Cache: {root / "cache"}', flush=True)
    if args.dry_run:
        for entry in entries:
            print(f'{entry["gguf_bytes"] / 1e9:6.2f} GB  {entry["model_repository"]}')
        return 0
    output = (args.output or root / 'runs' / ('download-models-' + uuid.uuid4().hex)).absolute()
    output.mkdir(parents=True, exist_ok=False)
    print(f'Progress and logs: {output}', flush=True)
    config = output / 'config'
    config.mkdir()
    template, runtime_snapshot, lock_path = SUITE.CONFIG.runtime(getattr(args, 'runtime', None))
    lock = template['sdk']['dependency_lock']
    shutil.copyfile(lock_path, config / lock)
    report = {'schema_version': 1, 'status': 'running', 'root': str(root),
              'reader_user': reader_user, 'entries': []}
    for entry in entries:
        report['entries'].append({**entry, 'status': 'not_run'})
    receipt = output / 'progress.json'
    SUITE.SEED.write_json(receipt, report)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for number, entry in enumerate(report['entries'], 1):
            name = entry['model_repository'].split('/')[-1]
            baseline = {k: template[k] for k in ('schema_version', 'container', 'sdk')}
            baseline['resolved_runtime'] = runtime_snapshot
            baseline.update(repository=entry['model_repository'], publication_revision=entry['publication_revision'],
                            model_id=entry['model_id'], preset=entry['preset'], variant=entry['variant'], programs='source')
            baseline_path = config / (name + '.json')
            SUITE.SEED.write_json(baseline_path, baseline)
            manifest = root / 'cache/models/catalog' / name / 'seed-manifest.json'
            command = [*layout.command('seed-models'), '--root', str(root),
                       '--baseline', str(baseline_path), '--output', str(output / name),
                       '--seed-manifest', str(manifest), '--asset-network', args.asset_network]
            print(f'[{number}/{len(entries)}] {name} ({entry["gguf_bytes"] / 1e9:.2f} GB)', flush=True)
            entry.update(status='running', log=str(output / (name + '.log')), manifest=str(manifest))
            SUITE.SEED.write_json(receipt, report)
            started = time.monotonic()
            try:
                with Path(entry['log']).open('w') as log:
                    code = SUITE.run_child(command, log)
            except KeyboardInterrupt:
                entry['status'] = 'interrupted'
                raise
            entry.update(status='passed' if code == 0 else 'failed', exit_code=code,
                         elapsed_seconds=round(time.monotonic() - started, 2))
            if code == 0 and uid is not None:
                entry['status'] = 'running'
                try:
                    SUITE.grant_read(manifest, uid, setfacl)
                    entry['reader_acl'] = {'status': 'passed', 'uid': uid, 'permissions': 'r--'}
                    entry['status'] = 'passed'
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    reason = f'{type(error).__name__}: {error}'
                    entry['reader_acl'] = {'status': 'failed', 'uid': uid, 'reason': reason}
                    entry.update(status='failed', error=f'reader ACL failed: {reason}')
            SUITE.SEED.write_json(receipt, report)
            print(f'  {entry["status"]}: {entry["elapsed_seconds"]:.1f}s; log: {entry["log"]}', flush=True)
        report['status'] = 'passed' if all(e['status'] == 'passed' for e in report['entries']) else 'failed'
        return 0 if report['status'] == 'passed' else 1
    except KeyboardInterrupt:
        report['status'] = 'interrupted'
        for entry in report['entries']:
            if entry['status'] == 'running':
                entry['status'] = 'interrupted'
        return 130
    except Exception as error:
        report.update(status='failed', error=f'{type(error).__name__}: {error}')
        for entry in report['entries']:
            if entry['status'] == 'running':
                entry.update(status='failed', error=report['error'])
        raise
    finally:
        SUITE.SEED.write_json(receipt, report)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        print(f'Summary: {receipt}', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, help='shared runtime config')
    parser.add_argument('--root', type=Path, help='shared cache root; otherwise HRX_SYSTEMS_ROOT or user default')
    parser.add_argument('--output', type=Path, help='new directory for progress, logs and seed reports')
    parser.add_argument('--dry-run', action='store_true', help='list selection without downloading or writing files')
    parser.add_argument('--model', action='append', help='restrict to a listed publication repository; repeatable')
    parser.add_argument('--asset-network', choices=('online', 'offline'), default='online')
    parser.add_argument('--reader-user', help='grant an existing runner account read access after each successful publication')
    try:
        return run(parser.parse_args(argv))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'Download failed: {error}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
