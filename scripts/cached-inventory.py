"""Bounded seed-receipt discovery and repository-owned smoke test policy."""
from datetime import datetime, timezone
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat

FIELDS = ('repository', 'publication_revision', 'model_id', 'preset', 'variant', 'programs')
MAX_MANIFEST_BYTES = 2 * 1024 * 1024


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def selection(seed):
    value = seed['selection']
    if seed.get('schema_version') != 1 or not isinstance(value, dict):
        raise ValueError('unsupported seed manifest')
    result = {key: value[key] for key in FIELDS}
    if any(not isinstance(v, str) or not v for v in result.values()):
        raise ValueError('selection fields must be nonempty strings')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', result['repository']):
        raise ValueError('invalid hosted repository')
    if not re.fullmatch(r'[0-9a-f]{40}', result['publication_revision']):
        raise ValueError('publication must be an immutable commit')
    return result


def asset_identity(seed):
    files = seed['files']
    if not isinstance(files, list) or not files:
        raise ValueError('seed manifest has no asset closure')
    identities = []
    for item in files:
        if not isinstance(item, dict) or item.get('verified') is not True:
            raise ValueError('asset is not a verified record')
        for key in ('installation_path', 'cache_path'):
            value = item[key]
            if not isinstance(value, str) or not value or Path(value).is_absolute() or '..' in Path(value).parts:
                raise ValueError('asset paths must be relative and contained')
        if item['cache_root'] not in ('huggingface', 'loom-assets'):
            raise ValueError('unknown asset cache root')
        if type(item['size']) is not int or item['size'] < 0 or not re.fullmatch(r'[0-9a-f]{64}', item['sha256']):
            raise ValueError('invalid asset size/digest')
        identities.append({**{k: item[k] for k in ('installation_path', 'origin', 'resolved', 'size', 'sha256')},
                           'roles': sorted(item['roles'])})
    if len({f['installation_path'] for f in identities}) != len(identities):
        raise ValueError('duplicate asset destination')
    for key in ('card_sha256', 'lock_sha256'):
        if not re.fullmatch(r'[0-9a-f]{64}', seed[key]):
            raise ValueError('invalid card/lock digest')
    return fingerprint({'card': seed['card_sha256'], 'lock': seed['lock_sha256'],
                        'files': sorted(identities, key=lambda f: f['installation_path'])})


def discover(cache):
    """Return frozen metadata and selected receipt bytes; never inspect payloads."""
    cache = Path(cache).resolve(strict=True)
    errors, paths, grouped = [], [], {}

    def error(path, reason):
        errors.append({'path': str(path.relative_to(cache)), 'reason': str(reason)})

    def children(directory):
        try:
            info = directory.lstat()
        except FileNotFoundError:
            return []
        except OSError as exc:
            error(directory, exc)
            return []
        if stat.S_ISLNK(info.st_mode):
            error(directory, 'symlinked inventory directory is not allowed')
            return []
        try:
            return sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError as exc:
            error(directory, exc)
            return []

    def manifest(path):
        try:
            path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            error(path, exc)
            return
        paths.append(path)

    manifest(cache / 'seed-manifest.json')
    for child in children(cache / 'models'):
        path = Path(child.path)
        if child.is_symlink():
            error(path, 'symlinked inventory directory is not allowed')
        elif child.is_dir(follow_symlinks=False):
            directories = children(path) if child.name == 'catalog' else [child]
            for directory in directories:
                if directory.is_symlink():
                    error(Path(directory.path), 'symlinked inventory directory is not allowed')
                elif directory.is_dir(follow_symlinks=False):
                    manifest(Path(directory.path) / 'seed-manifest.json')
    for path in sorted(paths):
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MANIFEST_BYTES:
                raise ValueError('receipt must be a bounded regular file')
            # O_NOFOLLOW keeps a replacement symlink from escaping the inventory.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as stream:
                raw = stream.read(MAX_MANIFEST_BYTES + 1)
            if len(raw) > MAX_MANIFEST_BYTES:
                raise ValueError('receipt exceeds size limit')
            seed = json.loads(raw)
            identity = selection(seed)
            assets = asset_identity(seed)
            key = fingerprint(identity)
            alias = {'path': str(path.relative_to(cache)), 'sha256': hashlib.sha256(raw).hexdigest()}
            candidate = grouped.setdefault(key, {'id': 'cached-' + key, 'selection': identity,
                'aliases': [], 'asset_identity': assets, 'conflict': False, '_seed': seed, '_raw': raw})
            candidate['aliases'].append(alias)
            candidate['conflict'] |= candidate['asset_identity'] != assets
        except (OSError, ValueError, KeyError, TypeError) as exc:
            error(path, exc)
    candidates = sorted(grouped.values(), key=lambda e: e['id'])
    metadata = {'schema_version': 1, 'mode': 'cached', 'snapshot_at': datetime.now(timezone.utc).isoformat(),
                'entries': [{k: v for k, v in e.items() if not k.startswith('_')} for e in candidates],
                'errors': errors}
    return metadata, candidates


def profile_document(repo):
    import importlib.util
    spec = importlib.util.spec_from_file_location('inventory_model_config', Path(__file__).with_name('model-config.py'))
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper.profiles(repo)


def override_for(repo, identity):
    """Exact-selection test behavior is independent of suite membership."""
    for entry in profile_document(repo)['overrides']:
        if all(entry['selection'][field] == identity[field] for field in FIELDS):
            return {key: entry[key] for key in ('smoke_request', 'smoke_expected_text')}
    return None


def freeze_test_policy(repo, identity):
    """Snapshot only test behavior needed after the host/container boundary."""
    document = profile_document(repo)
    defaults = {}
    for kind in ('text', 'decision'):
        value = document['defaults'].get(kind)
        if (not isinstance(value, dict) or not isinstance(value.get('smoke_request'), dict)
                or not isinstance(value.get('smoke_expected_text'), str)):
            raise ValueError(f'invalid {kind} smoke test defaults')
        defaults[kind] = {key: copy.deepcopy(value[key]) for key in
                          ('smoke_request', 'smoke_expected_text')}
    override = next((entry for entry in document['overrides'] if all(
        entry['selection'][field] == identity[field] for field in FIELDS)), None)
    return {'test_defaults': defaults, 'test_override': None if override is None else
            {key: copy.deepcopy(override[key]) for key in ('smoke_request', 'smoke_expected_text')}}


def workload(baseline, selected):
    kind = selected.service.get('kind')
    execution = selected.settings.get('execution')
    if not isinstance(execution, dict) or not execution:
        raise ValueError('selected preset lacks published execution settings')
    if kind not in ('text', 'decision'):
        raise ValueError(f'unsupported_test_kind: {kind}')
    defaults = baseline.get('test_defaults')
    if not isinstance(defaults, dict) or not isinstance(defaults.get(kind), dict):
        raise ValueError('cached model baseline lacks frozen smoke test defaults')
    values = copy.deepcopy(defaults[kind])
    values['test_kind'] = 'text_smoke' if kind == 'text' else 'decision_smoke'
    if kind == 'text':
        values.update(copy.deepcopy(baseline.get('test_override') or {}))
    return {**values, 'published_execution': execution, 'service_kind': kind,
            'profile_identity': 'cached-smoke-v1'}
