#!/usr/bin/env python3
"""Freeze ordered model/seed metadata for comparable runtime lanes; no payload I/O."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import re

MAX_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 4096
from . import inventory as INV


def digest(data):
    return hashlib.sha256(data).hexdigest()


def relative(value):
    if not isinstance(value, str) or not value or Path(value).is_absolute() or '..' in Path(value).parts:
        raise ValueError('snapshot paths must be relative and contained')
    return Path(value)


def regular(root, value, limit=INV.MAX_MANIFEST_BYTES):
    path = Path(root)
    for part in relative(value).parts:
        path = path / part
        if path.is_symlink(): raise ValueError('snapshot input is a symlink')
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError('snapshot input must be a bounded regular file')
    return path.read_bytes()


def seed_record(raw):
    seed = json.loads(raw)
    return {'seed_raw': base64.b64encode(raw).decode(), 'seed_sha256': digest(raw),
            'selection': INV.selection(seed), 'asset_identity': INV.asset_identity(seed)}


def config_inputs(repo, extra=()):
    root = Path(repo)
    paths = set(extra)
    if (root / 'config').is_dir():
        paths.update(str(p.relative_to(root)) for p in (root / 'config').rglob('*.json'))
    return {name: digest(regular(root, name, 4 * 1024 * 1024)) for name in sorted(paths)}


def freeze(mode, cache_root, repo, suite_path=None):
    cache_root, repo = Path(cache_root), Path(repo)
    if mode not in ('cached', 'suite'): raise ValueError('unsupported snapshot selection mode')
    extras, records = [], []
    if mode == 'cached':
        inventory, candidates = INV.discover(cache_root)
        for entry in candidates:
            records.append({'entry': {k: copy.deepcopy(v) for k, v in entry.items() if not k.startswith('_')},
                            **seed_record(entry['_raw'])})
        authored = None
    else:
        source = Path(suite_path or repo / 'config/cached-model-tests.json')
        if source.is_symlink() or source.stat().st_size > MAX_BYTES: raise ValueError('invalid authored suite')
        raw = source.read_bytes(); authored = json.loads(raw)
        if authored.get('schema_version') != 1 or not isinstance(authored.get('entries'), list):
            raise ValueError('invalid authored suite schema')
        inventory = None
        for entry in authored['entries']:
            item = copy.deepcopy(entry)
            if not isinstance(item, dict): raise ValueError('invalid authored entry')
            seed = seed_record(regular(cache_root, item['seed_manifest']))
            if item.get('selection') is not None and item['selection'] != seed['selection']:
                raise ValueError('suite selection differs from frozen seed')
            if 'baseline' in item:
                baseline = json.loads(regular(repo, item['baseline']))
                if {k: baseline.get(k) for k in INV.FIELDS} != seed['selection']:
                    raise ValueError('baseline selection differs from frozen seed')
                extras.append(item['baseline'])
            item['selection'] = seed['selection']
            records.append({'entry': item, **seed})
        authored = {'sha256': digest(raw), 'document': authored}
    document = {'schema_version': 1, 'mode': mode, 'inventory': inventory,
                'authored_suite': authored, 'repository_inputs': config_inputs(repo, extras), 'entries': records}
    validate(document, cache_root, repo)
    if len(json.dumps(document).encode()) > MAX_BYTES: raise ValueError('selection snapshot exceeds size limit')
    return document


def validate(document, cache_root, repo):
    if not isinstance(document, dict) or document.get('schema_version') != 1 or document.get('mode') not in ('cached', 'suite'):
        raise ValueError('invalid selection snapshot schema')
    records = document.get('entries')
    if not isinstance(records, list) or len(records) > MAX_ENTRIES:
        raise ValueError('invalid snapshot entry count')
    if not records and not (document['mode'] == 'cached' and (document.get('inventory') or {}).get('errors')):
        raise ValueError('snapshot selection is empty')
    repository_inputs = document.get('repository_inputs')
    if not isinstance(repository_inputs, dict): raise ValueError('missing frozen repository inputs')
    for name, sha in repository_inputs.items():
        if digest(regular(repo, name, 4 * 1024 * 1024)) != sha:
            raise ValueError('repository configuration changed after selection freeze: ' + name)
    ids, candidates = set(), []
    for record in records:
        entry = copy.deepcopy(record['entry']); identity = entry.get('id')
        if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', identity) or identity == 'runtime' or identity in ids:
            raise ValueError('duplicate or invalid snapshot entry ID')
        ids.add(identity)
        try: raw = base64.b64decode(record['seed_raw'], validate=True)
        except (ValueError, TypeError): raise ValueError('invalid frozen seed encoding') from None
        if len(raw) > INV.MAX_MANIFEST_BYTES or digest(raw) != record['seed_sha256']:
            raise ValueError('frozen seed digest changed')
        seed = json.loads(raw); selected = INV.selection(seed); assets = INV.asset_identity(seed)
        if selected != record['selection'] or selected != entry['selection'] or assets != record['asset_identity']:
            raise ValueError('frozen seed identity changed')
        if document['mode'] == 'cached':
            if identity != 'cached-' + INV.fingerprint(selected) or entry.get('asset_identity') != assets or type(entry.get('conflict')) is not bool:
                raise ValueError('invalid frozen cached identity')
            for alias in entry.get('aliases', []):
                relative(alias['path'])
                if not re.fullmatch('[0-9a-f]{64}', alias['sha256']): raise ValueError('invalid frozen alias')
        elif 'baseline' in entry:
            if entry['baseline'] not in repository_inputs: raise ValueError('unfrozen suite baseline')
        entry.update(_raw=raw, _seed=seed)
        candidates.append(entry)
    inventory = document.get('inventory')
    if document['mode'] == 'cached':
        if not isinstance(inventory, dict) or inventory.get('schema_version') != 1 or not isinstance(inventory.get('errors'), list):
            raise ValueError('invalid frozen inventory')
        if inventory.get('entries') != [{k:v for k,v in x['entry'].items()} for x in records]:
            raise ValueError('inventory and frozen entries disagree')
    elif inventory is not None or not isinstance(document.get('authored_suite'), dict):
        raise ValueError('invalid authored suite snapshot')
    return document['mode'], copy.deepcopy(inventory), candidates


def load(path, cache_root, repo):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError('selection snapshot must be a bounded regular file')
    return validate(json.loads(path.read_text()), cache_root, repo)
