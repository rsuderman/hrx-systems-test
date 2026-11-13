#!/usr/bin/env python3
"""Seed each configured suite entry through the existing single-model command."""
import argparse
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import uuid


from ..layout import REPO_ROOT as REPO
from . import seed_assets as SEED
from ..selection import configuration as CONFIG


def contained(root, value, *, exists=False):
    if not isinstance(value, str) or not value:
        raise ValueError('suite references must be nonempty relative paths')
    relative = Path(value)
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise ValueError(f'suite reference must stay inside its root: {value}')
    root = root.resolve()
    path = (root / relative).resolve(strict=exists)
    if not path.is_relative_to(root) or path == root:
        raise ValueError(f'suite reference escapes its root: {value}')
    if path.exists() and not path.is_file():
        raise ValueError(f'suite reference is not a file: {value}')
    return path


def load_entries(suite_path, cache_root):
    suite = SEED.read_json(suite_path)
    if (not isinstance(suite, dict) or suite.get('schema_version') != 1
            or not isinstance(suite.get('entries'), list) or not suite['entries']):
        raise ValueError('suite must have schema_version1 and nonempty entries')
    ids, manifests, entries = set(), set(), []
    for entry in suite['entries']:
        if not isinstance(entry, dict):
            raise ValueError('suite entries must be objects')
        entry_id = entry.get('id')
        if (not isinstance(entry_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', entry_id)
                or entry_id == 'runtime' or entry_id in ids):
            raise ValueError('suite IDs must be unique path-safe strings other than reserved runtime')
        baseline = contained(REPO, entry['baseline'], exists=True) if 'baseline' in entry else None
        if baseline: SEED.read_json(baseline)
        else:
            identity = entry.get('selection', {})
            if any(not isinstance(identity.get(field), str) or not identity[field] for field in CONFIG.FIELDS):
                raise ValueError('sample selection fields must be nonempty strings')
            CONFIG.override_for(identity)
        manifest = contained(cache_root, entry['seed_manifest'])
        if manifest in manifests:
            raise ValueError('suite entries must have distinct seed manifest paths')
        ids.add(entry_id)
        manifests.add(manifest)
        entries.append((entry, baseline, manifest))
    return entries


def grant_read(manifest, uid, setfacl):
    information = manifest.lstat()
    if not stat.S_ISREG(information.st_mode) or information.st_uid != os.geteuid():
        raise ValueError('reader ACL requires an owned regular manifest file')
    # Extend the existing mask by read only. Default mask recomputation could
    # accidentally unmask pre-existing write/execute entries on an extended ACL.
    mask = ((information.st_mode >> 3) & 7) | 4
    permissions = ''.join(letter if mask & bit else '-' for bit, letter in ((4, 'r'), (2, 'w'), (1, 'x')))
    subprocess.run([setfacl, '--no-mask', '-m', f'u:{uid}:r--,m::{permissions}', '--', str(manifest)], check=True)


def run_child(command, log):
    child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
    try:
        return child.wait()
    except BaseException:
        child.terminate()
        try:
            child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        raise


def run(args):
    root = Path(args.root or os.environ.get('HRX_SYSTEMS_ROOT', Path.home() / '.local/share/hrx-systems-test')).expanduser()
    if not root.is_absolute() or root == Path('/'):
        raise ValueError('cache root must be an absolute directory other than /')
    suite_path = (args.suite or REPO / 'config/cached-model-tests.json').resolve(strict=True)
    entries = load_entries(suite_path, root / 'cache')
    uid, setfacl = None, None
    if args.reader_user:
        uid = pwd.getpwnam(args.reader_user).pw_uid
        setfacl = shutil.which('setfacl')
        if not setfacl:
            raise ValueError('--reader-user requires setfacl; ask the machine owner to install it')
    output = (args.output or root / 'runs' / ('seed-suite-' + uuid.uuid4().hex)).absolute()
    output.mkdir(parents=True, exist_ok=False)
    report = {'schema_version': 1, 'status': 'running', 'suite_sha256': SEED.digest(suite_path),
        'asset_network': args.asset_network, 'reader_user': args.reader_user,
        'entries': [{'id': entry['id'], 'selection': entry.get('selection'), 'seed_manifest': entry['seed_manifest'],
                     'seeding': {'status': 'not_run', 'exit_code': None}} for entry, _, _ in entries]}
    report_path = output / 'seed-suite.json'
    SEED.write_json(report_path, report)
    interruption = 130
    def interrupted(signum, frame):
        nonlocal interruption
        interruption = 128 + signum
        raise KeyboardInterrupt
    old_handlers = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    current = None
    try:
        for (entry, baseline, manifest), current in zip(entries, report['entries']):
            print(f"Seeding {entry['id']}; log: {output / (entry['id'] + '.log')}", flush=True)
            if baseline is None:
                composed, lock_path = CONFIG.compose(entry, getattr(args, 'runtime', None))
                directory = output / 'config' / entry['id']
                directory.mkdir(parents=True)
                baseline = directory / 'baseline.json'
                SEED.write_json(baseline, composed)
                shutil.copyfile(lock_path, directory / composed['sdk']['dependency_lock'])
            command = ['bash', str(REPO / 'scripts/cache/seed-models.sh'), '--baseline', str(baseline),
                       '--root', str(root), '--output', str(output / entry['id']),
                       '--seed-manifest', str(manifest), '--asset-network', args.asset_network]
            if getattr(args, 'runtime', None): command += ['--runtime', str(args.runtime.resolve())]
            try:
                with (output / (entry['id'] + '.log')).open('w') as log:
                    code = run_child(command, log)
                current['seeding'] = {'status': 'ready' if code == 0 else 'failed', 'exit_code': code}
                if code == 0 and uid is not None:
                    grant_read(manifest, uid, setfacl)
                    current['reader_acl'] = {'uid': uid, 'permissions': 'r--'}
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                current['seeding']['status'] = 'failed'
                current['seeding']['reason'] = f'{type(error).__name__}: {error}'
            SEED.write_json(report_path, report)
        report['status'] = 'ready' if all(e['seeding']['status'] == 'ready' for e in report['entries']) else 'failed'
        return 0 if report['status'] == 'ready' else 1
    except KeyboardInterrupt:
        report['status'] = 'interrupted'
        if current is not None:
            current['seeding'] = {'status': 'failed', 'exit_code': interruption, 'reason': 'seeding interrupted'}
        return interruption
    finally:
        SEED.write_json(report_path, report)
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        print(f'Seed suite {report["status"]}; report: {report_path}', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path)
    parser.add_argument('--runtime', type=Path, help='shared runtime config')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--asset-network', choices=('online', 'offline'), default='online')
    parser.add_argument('--reader-user', help='grant this existing user read-only ACL access to published manifests')
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (KeyError, ValueError, OSError, TypeError) as error:
        print(f'Cannot seed suite: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
