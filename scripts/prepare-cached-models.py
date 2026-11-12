#!/usr/bin/env python3
"""Prepare fresh pipelines and verified read-only model assets for offline tests."""
import argparse
import contextlib
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
INVENTORY = helper('cached-inventory')
ASSETS = helper('asset-verification')
PROGRESS = helper('ci-progress')
CAPTURE = helper('evidence-capture')
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
                         ('prepare-cached-models.py', 'seed-models.py', 'sdk-environment.py', 'cached-inventory.py', 'asset-verification.py', 'ci-progress.py', 'evidence-capture.py', 'failure-records.py')}}


def indexed_cache(selection, seed, cache_root, work, verifier=None):
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
    verifier = verifier or ASSETS.Verifier()

    class IndexedCache(ContentCache):
        def __init__(self):
            super().__init__(code.root, huggingface=code.huggingface)
            self.verifier = verifier

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
            self.verifier.verify(path, identity.size, identity.sha256)
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
    if baseline.get('selection_mode') == 'cached':
        baseline.update(INVENTORY.workload(baseline, selection))
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
    return baseline


def offline(baseline, seed, cache_root, work, destination, entry_id, sdk_identity, verifier=None):
    from loom.model.fetch import FetchPlan, install
    from loom.model.fetch_source import Document, Source
    from loom.model.lock import FileIdentity
    saved = read_json(work / 'sources.json')
    document = Document(**saved['document'])
    selection = SEED.selection_for(baseline, document.text)
    cache = indexed_cache(selection, seed, cache_root, work, verifier)
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
            item = index[name]
            path = ASSETS.asset_path(cache_root, item)
            receipt = cache.verifier.verify(path, item['size'], item['sha256'])
            installed_receipt = (receipt if installed.samefile(path) else
                                 cache.verifier.verify(installed, item['size'], item['sha256']))
            assets.append({**item, 'verification': receipt, 'installed_verification': installed_receipt})
    manifest = {'schema_version': 1, 'selection': selection_identity(baseline),
        'model_id': baseline['model_id'], 'model_card_path': f'/inputs/{entry_id}/model/model.json',
        'card_sha256': seed['card_sha256'], 'lock_sha256': seed['lock_sha256'],
        'installed_card_sha256': digest(destination / 'model/model.json'),
        'programs': programs, 'assets': assets, 'sdk_identity': sdk_identity,
        'effective_execution': baseline['published_execution'],
        'smoke_request': baseline['smoke_request'], 'smoke_expected_text': baseline['smoke_expected_text'],
        'test_kind': baseline.get('test_kind', 'text_smoke'),
        'profile_identity': baseline.get('profile_identity', 'explicit-baseline'),
        'service_kind': baseline.get('service_kind', 'text'),
        'asset_phase_network': 'disabled', 'asset_verification': dict(cache.verifier.stats), 'model_executed': False}
    write_json(destination / 'prepared-inputs.json', manifest)
    return manifest


def stage(args):
    if args.stage.startswith('batch-'):
        operation = args.stage.removeprefix('batch-')
        if operation == 'online':
            with PROGRESS.operation('prepare SDK installation'):
                subprocess.run([sys.executable, '-u', '-I', '/source/scripts/sdk-environment.py', 'install',
                    '--baseline', '/prepared/config/runtime/baseline.json', '--payload-root', '/prepared/sdk-payloads',
                    '--output', '/prepared/sdk-prepare'], check=True)
        return subprocess.run(['/run/venv/bin/python', '-u', '-I', '/source/scripts/prepare-cached-models.py',
                               '--native-batch', operation]).returncode
    entry = read_json(Path('/prepared/config') / args.entry_id / 'baseline.json')
    seed = read_json(Path('/prepared/models') / args.entry_id / 'seed-manifest.json')
    sdk_output = Path('/prepared/models') / args.entry_id / ('sdk-' + args.stage)
    subprocess.run([sys.executable, '-I', '/source/scripts/sdk-environment.py', 'install',
        '--baseline', '/prepared/config/runtime/baseline.json', '--payload-root', '/prepared/sdk-payloads',
        '--output', str(sdk_output)], check=True)
    return subprocess.run(['/run/venv/bin/python', '-u', '-I', '/source/scripts/prepare-cached-models.py',
                           '--native', args.stage, args.entry_id]).returncode


def native(operation, entry_id, work=None, verifier=None):
    work = work or Path('/run')
    baseline = read_json(Path('/prepared/config') / entry_id / 'baseline.json')
    seed = read_json(Path('/prepared/models') / entry_id / 'seed-manifest.json')
    if operation == 'online':
        baseline = online(baseline, seed, Path('/model-cache'), work)
        write_json(Path('/prepared/config') / entry_id / 'baseline.json', baseline)
    else:
        for name in ('huggingface', 'loom-assets'):
            if not os.statvfs(Path('/model-cache') / name).f_flag & os.ST_RDONLY:
                raise ValueError('model cache mount is not read-only')
        offline(baseline, seed, Path('/model-cache'), work,
                Path('/prepared/models') / entry_id, entry_id,
                read_json(Path('/prepared/models') / entry_id / 'sdk-offline/sdk.json')['identity'], verifier)


def native_batch(operation):
    """One installed SDK for the full batch; model working files stay separate."""
    suite_path = Path('/prepared/prepared-suite.json')
    suite = read_json(suite_path)
    verifier = ASSETS.Verifier(ASSETS.load_index('/prepared/asset-verification.json'),
                              full=suite.get('full_integrity', False))
    ids = set(read_json('/prepared/batch-entry-ids.json'))
    for position, record in enumerate(suite['entries'], 1):
        if record['id'] not in ids:
            continue
        model_dir = Path('/prepared/models') / record['id']
        if operation == 'offline' and not (model_dir / 'online-complete.json').is_file():
            continue
        work = Path('/run/models') / record['id']
        work.mkdir(parents=True, exist_ok=True)
        log_path = model_dir / (operation + '.log')
        record['evidence'].append(f"models/{record['id']}/{operation}.log")
        label = f"prepare {position}/{len(suite['entries'])} {operation}: {record['selection']['model_id']}"
        try:
            sdk_dir = model_dir / ('sdk-' + operation)
            sdk_dir.mkdir(exist_ok=True)
            shutil.copyfile('/prepared/sdk-prepare/sdk.json', sdk_dir / 'sdk.json')
            record['evidence'].append(f"models/{record['id']}/sdk-{operation}/sdk.json")
            # Native output stays in the per-model log. Heartbeats use the saved
            # trusted console stream despite redirect_stdout inside the operation.
            with PROGRESS.operation(label):
                with log_path.open('w') as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                    native(operation, record['id'], work, verifier)
            if operation == 'online':
                baseline, _, _, identity = SDK.load_baseline(Path('/prepared') / record['baseline'])
                record.update(baseline_sha256=identity['baseline_sha256'],
                    smoke_request=baseline['smoke_request'], smoke_expected_text=baseline['smoke_expected_text'],
                    effective_execution=baseline['published_execution'], test_kind=baseline.get('test_kind', 'text_smoke'),
                    service_kind=baseline.get('service_kind', 'text'), profile_identity=baseline.get('profile_identity', 'explicit-baseline'))
                write_json(model_dir / 'online-complete.json', {'status': 'passed'})
            else:
                record['preparation'] = {'status': 'ready', 'reason': None}
                record['inputs_manifest'] = f"models/{record['id']}/prepared-inputs.json"
                record['inputs_manifest_sha256'] = digest(model_dir / 'prepared-inputs.json')
        except Exception as error:
            record['preparation'] = {'status': 'failed', 'reason': f'{operation}: {type(error).__name__}: {error}'}
        finally:
            if operation == 'offline':
                suite['asset_verification'] = dict(verifier.stats)
            write_json(suite_path, suite)
            failed = record['preparation']['status'] == 'failed' and record['preparation']['reason'] != 'preparation not reached'
            outcome = ('FAIL ' + record['preparation']['reason']) if failed else (
                'pipeline fetched; assets available' if operation == 'online' else 'prepared; cache reused')
            PROGRESS.emit(label + ': ' + outcome)


def run_host(args):
    root = Path(args.root or os.environ.get('HRX_SYSTEMS_ROOT', Path.home() / '.local/share/hrx-systems-test')).expanduser()
    if not root.is_absolute() or root == Path('/'):
        raise ValueError('root must be an absolute directory other than /')
    root = root.resolve(strict=True)
    cache_root = root / 'cache'
    cached = getattr(args, 'selection', None) == 'cached'
    if getattr(args, 'runtime_baseline', None) and not cached:
        raise ValueError('--runtime-baseline requires --selection cached')
    if cached and args.suite:
        raise ValueError('--selection cached and --suite are mutually exclusive')
    suite_path = None if cached else (args.suite or REPO / 'config/cached-model-tests.json').resolve(strict=True)
    inventory, candidates = INVENTORY.discover(cache_root) if cached else (None, [])
    suite = {'schema_version': 1, 'entries': candidates} if cached else read_json(suite_path)
    if suite.get('schema_version') != 1 or (not suite.get('entries') and not (cached and inventory['errors'])):
        raise ValueError('suite must have schema_version1 and nonempty entries')
    ids = [entry['id'] for entry in suite['entries']]
    if 'runtime' in ids or len(set(ids)) != len(ids) or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', value) for value in ids):
        raise ValueError('suite IDs must be unique path-safe strings other than reserved runtime')
    subprocess.run(['docker', 'info'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    output = (args.output or root / 'runs' / ('prepared-' + uuid.uuid4().hex)).absolute()
    output.mkdir(parents=True, exist_ok=False)
    ASSETS.write_index(output / ASSETS.INDEX_NAME, ASSETS.load_index(cache_root / ASSETS.INDEX_NAME))
    if cached:
        write_json(output / 'inventory.json', inventory)
        template_path = (getattr(args, 'runtime_baseline', None) or REPO / 'config/single-model-e2e.json').resolve(strict=True)
        template, _, _, _ = SDK.load_baseline(template_path)
        generated = output / 'selection-config'
        generated.mkdir()
        shutil.copyfile(template_path.parent / template['sdk']['dependency_lock'], generated / template['sdk']['dependency_lock'])
        for candidate in candidates:
            baseline = {k: template[k] for k in ('schema_version', 'sdk', 'container')}
            baseline.update(candidate['selection'], selection_mode='cached',
                            test_override=INVENTORY.override_for(REPO, candidate['selection']))
            baseline_path = generated / (candidate['id'] + '.json')
            write_json(baseline_path, baseline)
            candidate['_baseline'] = baseline_path
    attempt = {'schema_version': 1, 'attempt_id': output.name, 'source': source_identity(REPO),
        'suite_sha256': None if cached else digest(suite_path), 'runtime': None,
        'selection_mode': 'cached' if cached else 'suite',
        'inventory_errors': inventory['errors'] if cached else [],
        'full_integrity': getattr(args, 'full_integrity', False),
        'cache_roots': {name: {'host_path': str((cache_root / name).resolve(strict=True)),
                             'container_path': '/model-cache/' + name}
                        for name in ('huggingface', 'loom-assets')},
        'entries': [{'id': entry['id'], 'test_kind': 'unresolved' if cached else 'text_smoke',
                     'preparation': {'status': 'failed', 'reason': 'preparation not reached'},
                     'inputs_manifest': None, 'evidence': []} for entry in suite['entries']]}
    if cached:
        attempt['inventory_sha256'] = digest(output / 'inventory.json')
        attempt['inventory_path'] = 'inventory.json'
    PROGRESS.emit(f"prepare: {len(attempt['entries'])} selections; cache verification " +
                  ('full integrity' if attempt['full_integrity'] else 'unchanged asset receipts'))
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
                if cached:
                    record['selection'] = config['selection']
                    if config['conflict']:
                        raise ValueError('conflicting_seed_manifests: aliases disagree on immutable asset identities')
                baseline_path = config['_baseline'] if cached else contained(REPO, config['baseline'])
                baseline, lock, _, identity = SDK.load_baseline(baseline_path)
                record.update(baseline_sha256=identity['baseline_sha256'],
                    dependency_lock_sha256=identity['dependency_lock_sha256'],
                    selection=selection_identity(baseline), smoke_request=baseline.get('smoke_request'),
                    smoke_expected_text=baseline.get('smoke_expected_text'), effective_execution=baseline.get('published_execution'))
                runtime_key = {'image': baseline['container']['image'], 'platform': baseline['container']['platform'],
                               'sdk': baseline['sdk'], 'dependency_lock_sha256': identity['dependency_lock_sha256']}
                config_dir = output / 'config' / entry_id
                config_dir.mkdir(parents=True)
                shutil.copyfile(baseline_path, config_dir / 'baseline.json')
                shutil.copyfile(baseline_path.parent / baseline['sdk']['dependency_lock'],
                                config_dir / baseline['sdk']['dependency_lock'])
                model_dir = output / 'models' / entry_id
                model_dir.mkdir(parents=True)
                if cached:
                    (model_dir / 'seed-manifest.json').write_bytes(config['_raw'])
                else:
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
                PROGRESS.emit(f"prepare: SDK {runtime['sdk']['version']} at {runtime['sdk']['source_commit']}")
                PROGRESS.run(['docker', 'pull', '--platform', runtime['platform'], runtime['image']],
                    output / 'image-pull.log', label='prepare container image', check=True, timeout=300)
                PROGRESS.run([sys.executable, '-u', '-I', str(SCRIPT_DIR / 'sdk-environment.py'), 'acquire',
                    '--baseline', str(output / runtime['sdk_baseline']), '--payload-root', str(output / 'sdk-payloads'),
                    '--output', str(output / 'sdk-download')], output / 'sdk-acquire.log',
                    label='prepare SDK acquisition', check=True, timeout=600)
            except Exception as error:
                PROGRESS.emit(f'prepare: FAIL common runtime: {type(error).__name__}: {error}')
                for record, _, _ in contexts:
                    record['preparation'] = {'status': 'failed', 'reason': f'common runtime: {type(error).__name__}: {error}'}
                write_json(output / 'prepared-suite.json', attempt)
                return 1
        if contexts:
            write_json(output / 'batch-entry-ids.json', [record['id'] for record, _, _ in contexts])
            with tempfile.TemporaryDirectory(prefix='.prepare-work-', dir=output) as temporary:
                common = ['docker', 'run', '--rm', '--pull=never', '--name', container_name, '--read-only',
                    '--platform', runtime['platform'], '--user', f'{os.getuid()}:{os.getgid()}',
                    '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                    '--tmpfs', '/tmp:rw,size=128m,mode=1777', '--env', 'HOME=/tmp', '--env', 'PYTHONNOUSERSITE=1',
                    '--mount', f'type=bind,src={SCRIPT_DIR},dst=/source/scripts,readonly',
                    '--mount', f'type=bind,src={output},dst=/prepared',
                    '--mount', f'type=bind,src={temporary},dst=/run']
                for name in ('huggingface', 'loom-assets'):
                    common += ['--mount', f'type=bind,src={cache_root / name},dst=/model-cache/{name},readonly']
                for operation in ('online', 'offline'):
                    command = common + ['--network', 'bridge' if operation == 'online' else 'none']
                    if operation == 'online' and os.environ.get('HF_TOKEN'):
                        command += ['--env', 'HF_TOKEN']
                    command += [runtime['image'], 'python', '-u', '-I', '/source/scripts/prepare-cached-models.py',
                                '--stage', 'batch-' + operation]
                    try:
                        PROGRESS.run(command, output / ('batch-' + operation + '.log'),
                                     label='prepare batch ' + operation, check=True, timeout=600 * len(contexts))
                    except Exception as error:
                        PROGRESS.emit(f'prepare batch {operation}: FAIL {type(error).__name__}: {error}')
                        attempt = read_json(output / 'prepared-suite.json')
                        for record in attempt['entries']:
                            if record['preparation']['reason'] == 'preparation not reached':
                                record['preparation'] = {'status': 'failed', 'reason': f'batch {operation}: {error}'}
                        write_json(output / 'prepared-suite.json', attempt)
                        return 1
                    finally:
                        subprocess.run(['docker', 'rm', '-f', container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                attempt = read_json(output / 'prepared-suite.json')
        print(f'Prepared suite: {output / "prepared-suite.json"}', flush=True)
        return int(bool(attempt['inventory_errors']) or any(item['preparation']['status'] != 'ready' for item in attempt['entries']))
    except KeyboardInterrupt:
        PROGRESS.emit('prepare: interrupted; cleaning up owned container')
        attempt = read_json(output / 'prepared-suite.json')
        for record in attempt['entries']:
            if record['preparation']['reason'] == 'preparation not reached':
                record['preparation'] = {'status': 'failed', 'reason': 'preparation interrupted'}
        write_json(output / 'prepared-suite.json', attempt)
        return 1
    finally:
        try:
            CAPTURE.preparation_snapshot(output, read_json(output / 'prepared-suite.json'))
        except (ValueError, OSError) as error:
            print(f'Preparation evidence capture unavailable: {error}', file=sys.stderr)
        subprocess.run(['docker', 'rm', '-f', container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for sig, previous in old_handlers.items():
            signal.signal(sig, previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path)
    parser.add_argument('--selection', choices=('cached',), help='derive the suite from this runner’s published seed manifests')
    parser.add_argument('--runtime-baseline', type=Path, help='shared SDK/image baseline for cached selection')
    parser.add_argument('--full-integrity', action='store_true', help='hash every distinct physical asset once instead of trusting unchanged seed receipts')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--stage', choices=('online', 'offline', 'batch-online', 'batch-offline'), help=argparse.SUPPRESS)
    parser.add_argument('--entry-id', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.stage:
        return stage(args)
    try:
        return run_host(args)
    except (ValueError, OSError, subprocess.SubprocessError, KeyError, TypeError) as error:
        PROGRESS.emit(f'prepare: FAIL {type(error).__name__}: {error}')
        print(f'Cannot prepare: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--native-batch':
        native_batch(sys.argv[2])
    elif len(sys.argv) == 4 and sys.argv[1] == '--native':
        native(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit(main())
