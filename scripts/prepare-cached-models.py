#!/usr/bin/env python3
"""Prepare fresh pipelines and verified read-only model assets for offline tests."""
import argparse
from dataclasses import asdict, replace
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import uuid


SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parent
SELECTION_FIELDS = ('repository', 'publication_revision', 'model_id', 'preset', 'variant', 'programs')


def helper(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), SCRIPT_DIR / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SEED = helper('seed-models')
SDK = helper('sdk-environment')
read_json, write_json, digest = SEED.read_json, SEED.write_json, SEED.digest


def contained(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError(f'path must stay inside its designated root: {relative}')
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root.resolve(strict=True)):
        raise ValueError(f'path escapes designated root: {relative}')
    return path


def selection_identity(baseline):
    return {key: baseline[key] for key in SELECTION_FIELDS}


def source_identity(repo):
    commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    return {'commit': commit, 'dirty': bool(subprocess.check_output(
        ['git', '-C', str(repo), 'status', '--porcelain'], text=True).strip()),
        'files_sha256': {f'scripts/{name}': digest(repo / 'scripts' / name) for name in
                         ('prepare-cached-models.py', 'seed-models.py', 'sdk-environment.py')}}


def indexed_cache(selection, seed, cache_root, work):
    """Native cache for fresh code; immutable indexed files for selected assets."""
    from loom.model.fetch_storage import AcquiredFile, ContentCache
    from loom.model.lock import file_key
    entries = {item['installation_path']: item for item in seed['files']}
    wanted = {name for name, file in selection.files.items() if not SEED.code_file(file)}
    if len(entries) != len(seed['files']) or set(entries) != wanted:
        raise ValueError('seed manifest asset closure does not match selected card')
    code_origins = {file_key(file.origin) for file in selection.files.values() if SEED.code_file(file)}
    by_origin = {}
    for name in wanted:
        file = selection.files[name]
        if file.transform is not None:
            raise ValueError('initial prepared baseline requires untransformed assets')
        key = file_key(file.origin)
        if key in code_origins:
            raise ValueError('mixed-origin code/asset selection needs explicit seed routing')
        item = entries[name]
        if sorted(file.roles) != sorted(item['roles']) or not item.get('verified'):
            raise ValueError(f'seed roles or verification do not match: {name}')
        if file_key(item['origin']) != key:
            raise ValueError(f'seed origin does not match: {name}')
        previous = by_origin.setdefault(key, item)
        if previous['sha256'] != item['sha256'] or previous['size'] != item['size']:
            raise ValueError('conflicting indexed identities for one asset source')
    code = SEED.native_cache(work / 'pipeline-cache', work / 'pipeline-hf', threading.Event())

    class IndexedCache(ContentCache):
        def __init__(self):
            super().__init__(code.root, huggingface=code.huggingface)

        def indexed(self, source, identity):
            item = by_origin.get(file_key(source.origin))
            if item is None:
                return None
            if (identity is None or identity.sha256 != item['sha256'] or
                    identity.size != item['size'] or source.resolved != item['resolved']):
                raise ValueError('seed identity/revision disagrees with selected lock')
            if item['cache_root'] not in ('huggingface', 'loom-assets'):
                raise ValueError('invalid model cache root')
            return contained(cache_root / item['cache_root'], item['cache_path'])

        def read_document(self, location, *, offline):
            return code.read_document(location, offline=offline)

        def prepare_source(self, source, identity, *, offline):
            path = self.indexed(source, identity)
            if path is not None:
                return replace(source, transport={'kind': 'local', 'path': str(path)})
            return code.prepare_source(source, identity, offline=offline)

        def acquire(self, source, identity, *, offline, progress=None):
            path = self.indexed(source, identity)
            if path is None:
                return code.acquire(source, identity, offline=offline, progress=progress)
            identity.verify(path)
            return AcquiredFile(path, identity)

    return IndexedCache()


def online(baseline, seed, cache_root, work, *, location=None):
    from loom.model.fetch import plan_fetch
    from loom.model.fetch_source import Document
    if seed.get('schema_version') != 1 or seed['selection'] != selection_identity(baseline):
        raise ValueError('seed selection does not match baseline')
    repository, revision = baseline['repository'], baseline['publication_revision']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('pipeline publication must identify one repository and immutable commit')
    location = location or f'https://huggingface.co/{repository}/resolve/{revision}/model.json'
    documents = SEED.native_cache(work / 'pipeline-cache', work / 'pipeline-hf', threading.Event())
    document = documents.read_document(location, offline=False)
    lock = documents.read_document(location + '.lock', offline=False)
    (work / 'model.json').write_text(document.text)
    (work / 'model.json.lock').write_text(lock.text)
    if digest(work / 'model.json') != seed['card_sha256'] or digest(work / 'model.json.lock') != seed['lock_sha256']:
        raise ValueError('published card/lock disagree with seeded selection')
    selection = SEED.selection_for(baseline, document.text)
    cache = indexed_cache(selection, seed, cache_root, work)
    plan = plan_fetch(Document(location, document.text), selection, cache,
                      lock_location=str(work / 'model.json.lock'))
    for name, file in selection.files.items():
        if not SEED.code_file(file):
            continue
        if file.transform is not None:
            raise ValueError('initial prepared baseline requires untransformed programs')
        item = cache.acquire(plan.sources[name], plan.identities.get(name), offline=False)
        plan.sources[name] = replace(plan.sources[name], transport={'kind': 'local', 'path': str(item.path)})
    write_json(work / 'sources.json', {'document': asdict(plan.document),
        'sources': {name: asdict(source) for name, source in plan.sources.items()},
        'identities': {name: asdict(identity) for name, identity in plan.identities.items()},
        'origins': plan.origins, 'lock_mode': plan.lock_mode, 'lock_scope': plan.lock_scope})


def offline(baseline, seed, cache_root, work, destination, entry_id, sdk_identity):
    from loom.model.fetch import FetchPlan, install
    from loom.model.fetch_source import Document, Source
    from loom.model.lock import FileIdentity
    saved = read_json(work / 'sources.json')
    document = Document(**saved['document'])
    selection = SEED.selection_for(baseline, document.text)
    cache = indexed_cache(selection, seed, cache_root, work)
    plan = FetchPlan(document, selection,
        {name: Source(**source) for name, source in saved['sources'].items()},
        {name: FileIdentity(**identity) for name, identity in saved['identities'].items()},
        saved['origins'], True, saved['lock_mode'], saved['lock_scope'], None)
    # Local asset transports make native sharing probe the actual immutable files,
    # avoiding provider metadata writes or a second asset store.
    for name, source in plan.sources.items():
        if not SEED.code_file(selection.files[name]):
            path = cache.indexed(source, plan.identities.get(name))
            plan.sources[name] = replace(source, transport={'kind': 'local', 'path': str(path)})
    install(plan, cache, destination / 'model')
    assets, programs = [], []
    index = {item['installation_path']: item for item in seed['files']}
    for name, file in selection.files.items():
        installed = destination / 'model' / name
        if not installed.is_file():
            raise ValueError(f'installed file is missing from final mount: {name}')
        if SEED.code_file(file):
            programs.append({'path': name, 'sha256': digest(installed)})
        else:
            plan.identities[name].verify(installed)
            assets.append(index[name])
    manifest = {'schema_version': 1, 'selection': selection_identity(baseline),
        'model_id': baseline['model_id'], 'model_card_path': f'/inputs/{entry_id}/model/model.json',
        'card_sha256': seed['card_sha256'], 'lock_sha256': seed['lock_sha256'],
        'installed_card_sha256': digest(destination / 'model/model.json'),
        'programs': programs, 'assets': assets, 'sdk_identity': sdk_identity,
        'effective_execution': baseline['published_execution'],
        'smoke_request': baseline['smoke_request'], 'smoke_expected_text': baseline['smoke_expected_text'],
        'test_kind': 'text_smoke', 'asset_phase_network': 'disabled', 'model_executed': False}
    write_json(destination / 'prepared-inputs.json', manifest)
    return manifest


def stage(args):
    entry = read_json(Path('/prepared/config') / args.entry_id / 'baseline.json')
    seed = read_json(Path('/prepared/models') / args.entry_id / 'seed-manifest.json')
    sdk_output = Path('/prepared/models') / args.entry_id / ('sdk-' + args.stage)
    subprocess.run([sys.executable, '-I', '/source/scripts/sdk-environment.py', 'install',
        '--baseline', '/prepared/config/runtime/baseline.json', '--payload-root', '/prepared/sdk-payloads',
        '--output', str(sdk_output)], check=True)
    return subprocess.run(['/run/venv/bin/python', '-I', '/source/scripts/prepare-cached-models.py',
                           '--native', args.stage, args.entry_id]).returncode


def native(operation, entry_id):
    baseline = read_json(Path('/prepared/config') / entry_id / 'baseline.json')
    seed = read_json(Path('/prepared/models') / entry_id / 'seed-manifest.json')
    if operation == 'online':
        online(baseline, seed, Path('/model-cache'), Path('/run'))
    else:
        for name in ('huggingface', 'loom-assets'):
            if not os.statvfs(Path('/model-cache') / name).f_flag & os.ST_RDONLY:
                raise ValueError('model cache mount is not read-only')
        offline(baseline, seed, Path('/model-cache'), Path('/run'),
                Path('/prepared/models') / entry_id, entry_id,
                read_json(Path('/prepared/models') / entry_id / 'sdk-offline/sdk.json')['identity'])


def run_host(args):
    root = Path(args.root or os.environ.get('HRX_SYSTEMS_ROOT', Path.home() / '.local/share/hrx-systems-test')).expanduser()
    if not root.is_absolute() or root == Path('/'):
        raise ValueError('root must be an absolute directory other than /')
    root = root.resolve(strict=True)
    cache_root = root / 'cache'
    suite_path = (args.suite or REPO / 'config/cached-model-tests.json').resolve(strict=True)
    suite = read_json(suite_path)
    if suite.get('schema_version') != 1 or not suite.get('entries'):
        raise ValueError('suite must have schema_version1 and nonempty entries')
    ids = [entry['id'] for entry in suite['entries']]
    if 'runtime' in ids or len(set(ids)) != len(ids) or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', value) for value in ids):
        raise ValueError('suite IDs must be unique path-safe strings other than reserved runtime')
    subprocess.run(['docker', 'info'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    output = (args.output or root / 'runs' / ('prepared-' + uuid.uuid4().hex)).absolute()
    output.mkdir(parents=True, exist_ok=False)
    attempt = {'schema_version': 1, 'attempt_id': output.name, 'source': source_identity(REPO),
        'suite_sha256': digest(suite_path), 'runtime': None,
        'cache_roots': {name: {'host_path': str((cache_root / name).resolve(strict=True)),
                             'container_path': '/model-cache/' + name}
                        for name in ('huggingface', 'loom-assets')},
        'entries': [{'id': entry['id'], 'test_kind': 'text_smoke',
                     'preparation': {'status': 'failed', 'reason': 'preparation not reached'},
                     'inputs_manifest': None, 'evidence': []} for entry in suite['entries']]}
    container_name = 'hrx-prepare-' + uuid.uuid4().hex
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    old_handlers = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    write_json(output / 'prepared-suite.json', attempt)
    try:
        contexts = []
        for config, record in zip(suite['entries'], attempt['entries']):
            entry_id = config['id']
            try:
                baseline_path = contained(REPO, config['baseline'])
                baseline, lock, _, identity = SDK.load_baseline(baseline_path)
                record.update(baseline_sha256=identity['baseline_sha256'],
                    dependency_lock_sha256=identity['dependency_lock_sha256'],
                    selection=selection_identity(baseline), smoke_request=baseline['smoke_request'],
                    smoke_expected_text=baseline['smoke_expected_text'], effective_execution=baseline['published_execution'])
                runtime_key = {'image': baseline['container']['image'], 'platform': baseline['container']['platform'],
                               'sdk': baseline['sdk'], 'dependency_lock_sha256': identity['dependency_lock_sha256']}
                config_dir = output / 'config' / entry_id
                config_dir.mkdir(parents=True)
                shutil.copyfile(baseline_path, config_dir / 'baseline.json')
                shutil.copyfile(baseline_path.parent / baseline['sdk']['dependency_lock'],
                                config_dir / baseline['sdk']['dependency_lock'])
                model_dir = output / 'models' / entry_id
                model_dir.mkdir(parents=True)
                seed_path = contained(cache_root, config['seed_manifest'])
                shutil.copyfile(seed_path, model_dir / 'seed-manifest.json')
                record['baseline'] = f'config/{entry_id}/baseline.json'
                record['evidence'] = [f'models/{entry_id}/seed-manifest.json']
                if attempt['runtime'] is None:
                    runtime_dir = output / 'config/runtime'
                    shutil.copytree(config_dir, runtime_dir)
                    attempt['runtime'] = {**runtime_key, 'sdk_identity': identity,
                        'sdk_payloads': 'sdk-payloads', 'sdk_baseline': 'config/runtime/baseline.json'}
                elif any(attempt['runtime'][key] != value for key, value in runtime_key.items()):
                    raise ValueError('suite entries must share one pinned image/SDK/dependency identity')
                contexts.append((record, runtime_key, model_dir))
            except Exception as error:
                record['preparation'] = {'status': 'failed', 'reason': f'{type(error).__name__}: {error}'}
            finally:
                write_json(output / 'prepared-suite.json', attempt)
        # Snapshot every expected entry/config before any acquisition begins.
        if contexts:
            try:
                runtime = attempt['runtime']
                subprocess.run(['docker', 'pull', '--platform', runtime['platform'], runtime['image']], check=True,
                               stdout=subprocess.DEVNULL, timeout=300)
                subprocess.run([sys.executable, '-I', str(SCRIPT_DIR / 'sdk-environment.py'), 'acquire',
                    '--baseline', str(output / runtime['sdk_baseline']), '--payload-root', str(output / 'sdk-payloads'),
                    '--output', str(output / 'sdk-download')], check=True, timeout=600)
            except Exception as error:
                for record, _, _ in contexts:
                    record['preparation'] = {'status': 'failed', 'reason': f'common runtime: {type(error).__name__}: {error}'}
                write_json(output / 'prepared-suite.json', attempt)
                return 1
        for record, runtime_key, model_dir in contexts:
            entry_id = record['id']
            try:
                with tempfile.TemporaryDirectory(prefix='.prepare-work-', dir=output) as temporary:
                    work = Path(temporary)
                    common = ['docker', 'run', '--rm', '--pull=never', '--name', container_name, '--read-only',
                        '--platform', runtime_key['platform'], '--user', f'{os.getuid()}:{os.getgid()}',
                        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                        '--tmpfs', '/tmp:rw,size=128m,mode=1777', '--env', 'HOME=/tmp', '--env', 'PYTHONNOUSERSITE=1',
                        '--mount', f'type=bind,src={SCRIPT_DIR},dst=/source/scripts,readonly',
                        '--mount', f'type=bind,src={output},dst=/prepared',
                        '--mount', f'type=bind,src={work},dst=/run']
                    for name in ('huggingface', 'loom-assets'):
                        common += ['--mount', f'type=bind,src={cache_root / name},dst=/model-cache/{name},readonly']
                    try:
                        for operation in ('online', 'offline'):
                            command = common + ['--network', 'bridge' if operation == 'online' else 'none']
                            if operation == 'online' and os.environ.get('HF_TOKEN'):
                                command += ['--env', 'HF_TOKEN']
                            command += [runtime_key['image'], 'python', '-I', '/source/scripts/prepare-cached-models.py',
                                        '--stage', operation, '--entry-id', entry_id]
                            record['evidence'].append(f'models/{entry_id}/{operation}.log')
                            with (model_dir / (operation + '.log')).open('w') as log:
                                subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT, timeout=600)
                            shutil.rmtree(work / 'venv')
                            record['evidence'].append(f'models/{entry_id}/sdk-{operation}/sdk.json')
                    finally:
                        subprocess.run(['docker', 'rm', '-f', container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                record['preparation'] = {'status': 'ready', 'reason': None}
                record['inputs_manifest'] = f'models/{entry_id}/prepared-inputs.json'
                record['inputs_manifest_sha256'] = digest(model_dir / 'prepared-inputs.json')
            except Exception as error:
                record['preparation'] = {'status': 'failed', 'reason': f'{type(error).__name__}: {error}'}
            finally:
                write_json(output / 'prepared-suite.json', attempt)
        print(f'Prepared suite: {output / "prepared-suite.json"}', flush=True)
        return int(any(item['preparation']['status'] != 'ready' for item in attempt['entries']))
    except KeyboardInterrupt:
        for record in attempt['entries']:
            if record['preparation']['reason'] == 'preparation not reached':
                record['preparation'] = {'status': 'failed', 'reason': 'preparation interrupted'}
        write_json(output / 'prepared-suite.json', attempt)
        return 1
    finally:
        subprocess.run(['docker', 'rm', '-f', container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for sig, previous in old_handlers.items():
            signal.signal(sig, previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--stage', choices=('online', 'offline'), help=argparse.SUPPRESS)
    parser.add_argument('--entry-id', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.stage:
        return stage(args)
    try:
        return run_host(args)
    except (ValueError, OSError, subprocess.SubprocessError, KeyError, TypeError) as error:
        print(f'Cannot prepare: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--native':
        native(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit(main())
