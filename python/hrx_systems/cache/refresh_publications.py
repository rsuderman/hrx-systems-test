"""Discover immutable publication updates and apply a saved configuration plan.

Planning reads cards and locks, never model payloads. Cache seeding and model
execution remain explicit commands owned by the host operator.
"""
import argparse
import concurrent.futures
import copy
from datetime import datetime, timezone
import difflib
import fcntl
import hashlib
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
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .. import layout
from ..selection import configuration as CONFIG, inventory as INVENTORY, snapshot as SNAPSHOT
from . import seed_assets as SEED

CONFIG_PATHS = (
    'config/cached-model-tests.json',
    'config/download-models-under-20gb.json',
    'config/model-test-profiles.json',
    'config/runtime-policies/0.2.0/policy.json',
)
INPUT_PATHS = (*CONFIG_PATHS, 'config/ci-model-exclusions.json')
FIELDS = INVENTORY.FIELDS
MAX_DOCUMENT = 8 * 1024 * 1024
MAX_PLAN = 128 * 1024 * 1024
MAX_REPOSITORIES = 1000
REPOSITORY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*')
REVISION = re.compile(r'[0-9a-f]{40}')
SHA = re.compile(r'[0-9a-f]{64}')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def formatted(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + '\n'


def parse(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key: ' + key)
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=unique)


def regular(root, relative, limit=MAX_DOCUMENT):
    """Read only a bounded regular file with no symlink component."""
    if (not isinstance(relative, str) or not relative or '\\' in relative or
            relative.startswith('/') or any(p in ('', '.', '..') for p in relative.split('/'))):
        raise ValueError('invalid relative metadata path')
    root = Path(root).resolve(strict=True)
    path = root
    for part in relative.split('/'):
        path /= part
        if path.is_symlink():
            raise ValueError('metadata path traverses a symlink')
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError('metadata must be a bounded regular file')
    return path.read_text(encoding='utf-8')


def identity(value):
    if (not isinstance(value, dict) or set(value) != set(FIELDS) or
            any(not isinstance(value[k], str) or not value[k] for k in FIELDS) or
            not REPOSITORY.fullmatch(value['repository']) or
            not REVISION.fullmatch(value['publication_revision'])):
        raise ValueError('invalid immutable publication selection')
    return value


def key(value):
    return sha(canonical(identity(value)))


def request_choices(values):
    result = {}
    for value in values:
        try:
            repository, choice = value.split('=', 1)
            preset, variant = choice.split(',')
        except ValueError as error:
            raise ValueError('--select requires REPOSITORY=PRESET,VARIANT') from error
        if (not REPOSITORY.fullmatch(repository) or not preset or not variant or
                repository in result):
            raise ValueError('invalid or duplicate explicit model choice')
        result[repository] = {'preset': preset, 'variant': variant}
    return result


class RemoteError(ValueError):
    pass


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if redirected and urllib.parse.urlsplit(request.full_url).hostname != urllib.parse.urlsplit(newurl).hostname:
            redirected.remove_header('Authorization')
        return redirected


class Hub:
    """Bounded Hugging Face metadata transport; no payload acquisition endpoint."""
    def __init__(self):
        self.opener = urllib.request.build_opener(_Redirect())
        self.received_bytes = 0
        self.transfer_lock = threading.Lock()

    def get(self, url):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname != 'huggingface.co' or parsed.username or parsed.password:
            raise ValueError('metadata requests must use Hugging Face HTTPS')
        headers = {'User-Agent': 'hrx-systems-test-publication-refresh/1'}
        token = os.environ.get('HF_TOKEN')
        if token:
            headers['Authorization'] = 'Bearer ' + token
        try:
            with self.opener.open(urllib.request.Request(url, headers=headers), timeout=30) as response:
                raw = response.read(MAX_DOCUMENT + 1)
                link = response.headers.get('Link', '')
        except urllib.error.HTTPError as error:
            raise RemoteError('HTTP ' + str(error.code)) from error
        except urllib.error.URLError as error:
            raise RemoteError('metadata transport failed: ' + str(error.reason)) from error
        if len(raw) > MAX_DOCUMENT:
            raise RemoteError('remote metadata exceeds size limit')
        with self.transfer_lock:
            self.received_bytes += len(raw)
            if self.received_bytes > MAX_PLAN // 2:
                raise RemoteError('metadata batch exceeds transfer limit')
        return raw.decode('utf-8'), link

    def repositories(self, author):
        url = 'https://huggingface.co/api/models?' + urllib.parse.urlencode(
            {'author': author, 'limit': 1000, 'full': 'true'})
        result, visited = {}, set()
        while url:
            if url in visited:
                raise RemoteError('repository listing pagination loop')
            visited.add(url)
            text, link = self.get(url)
            page = parse(text)
            if not isinstance(page, list):
                raise RemoteError('repository listing must be an array')
            for value in page:
                name, revision = value.get('id'), value.get('sha')
                if (not isinstance(name, str) or not REPOSITORY.fullmatch(name) or
                        name.split('/')[0] != author or not isinstance(revision, str) or
                        not REVISION.fullmatch(revision) or name in result):
                    raise RemoteError('invalid or duplicate immutable repository listing')
                result[name] = {'repository': name, 'revision': revision,
                                'last_modified': value.get('lastModified')}
            if len(result) > MAX_REPOSITORIES:
                raise RemoteError('repository listing exceeds limit')
            matches = re.findall(r'<([^>]+)>;\s*rel="next"', link)
            if len(matches) > 1:
                raise RemoteError('ambiguous repository pagination')
            url = matches[0] if matches else None
        return result

    def metadata(self, repository, listed=None):
        try:
            if listed is None:
                text, _ = self.get('https://huggingface.co/api/models/' + repository)
                value = parse(text)
                if (not isinstance(value, dict) or value.get('id') != repository or
                        not isinstance(value.get('sha'), str) or not REVISION.fullmatch(value['sha'])):
                    raise RemoteError('repository identity changed; explicit remapping required')
                listed = {'repository': repository, 'revision': value['sha'],
                          'last_modified': value.get('lastModified')}
            base = f"https://huggingface.co/{repository}/resolve/{listed['revision']}/"
            card, _ = self.get(base + 'model.json')
            lock, _ = self.get(base + 'model.json.lock')
            return {**listed, 'status': 'available', 'card': card, 'lock': lock,
                    'card_sha256': sha(card.encode()), 'lock_sha256': sha(lock.encode())}
        except (RemoteError, ValueError, UnicodeError) as error:
            return {'repository': repository, 'status': 'unavailable', 'reason': str(error)}


def existing_selections(documents, cache_root, author):
    result = {}

    def add(value, aliases=(), assets=(), *, cataloged=False):
        value = identity(copy.deepcopy(value))
        if value['repository'].split('/')[0] != author:
            return
        record = result.setdefault(key(value), {'selection': value, 'aliases': [], 'assets': [], 'cataloged': False})
        record['cataloged'] |= cataloged
        for alias in aliases:
            if alias not in record['aliases']:
                record['aliases'].append(alias)
        for asset in assets:
            item = {k: asset[k] for k in ('sha256', 'size')}
            if item not in record['assets']:
                record['assets'].append(item)

    for entry in documents[CONFIG_PATHS[0]]['entries']:
        add(entry['selection'])
    for entry in documents[CONFIG_PATHS[1]]['entries']:
        add({'repository': entry['model_repository'], 'publication_revision': entry['publication_revision'],
             **{k: entry[k] for k in ('model_id', 'preset', 'variant')}, 'programs': 'source'}, cataloged=True)
    if Path(cache_root).exists():
        inventory, candidates = INVENTORY.discover(cache_root)
        if inventory['errors'] or any(x['conflict'] for x in candidates):
            raise ValueError('cached inventory contains errors or conflicting receipts')
        for candidate in candidates:
            add(candidate['selection'], candidate['aliases'], candidate['_seed']['files'])
    return sorted(result.values(), key=lambda x: tuple(x['selection'][k] for k in FIELDS))


def inspect_native(batch):
    """The installed SDK owns card validation, selection and lock coverage."""
    from loom.model import ModelCard
    from loom.model.lock import ModelLock
    result = {}
    for item in batch:
        name = item['repository']
        try:
            card, lock = ModelCard.parse(item['card']), ModelLock.parse(item['lock'])

            def describe(preset, variant):
                selected = card.select(preset=preset, variant=variant, programs='source')
                if selected.service.get('kind') not in ('text', 'decision'):
                    raise ValueError('unsupported service kind')
                if getattr(selected, 'partition', None) is not None:
                    raise ValueError('partitioned selections are not supported by this harness')
                execution = selected.settings.get('execution')
                if not isinstance(execution, dict) or not execution:
                    raise ValueError('selected preset lacks published execution settings')
                covered = lock.cover(selected)
                assets = []
                for path, file in selected.files.items():
                    if not file.roles - {'program', 'auxiliary'}:
                        continue
                    record = covered.get(path)
                    if record is None:
                        raise ValueError('asset lacks a locked identity: ' + path)
                    assets.append({'path': path, 'roles': sorted(file.roles), 'size': record.size,
                                   'sha256': record.sha256, 'origin': record.origin,
                                   'resolved': record.resolved, 'transform': record.transform})
                return {'model_id': selected.model_id, 'preset': selected.preset,
                        'variant': selected.variant, 'programs': selected.programs,
                        'service_kind': selected.service['kind'], 'assets': sorted(assets, key=lambda x: x['path']),
                        'published_execution': execution,
                        'gguf_bytes': sum(a['size'] for a in assets if 'parameter' in a['roles'] and a['path'].endswith('.gguf'))}

            selections, errors = {}, {}
            if item['existing']:
                for old in item['existing']:
                    try:
                        value = describe(old['preset'], old['variant'])
                        if value['model_id'] != old['model_id'] or value['programs'] != old['programs']:
                            raise ValueError('published model identity changed')
                        selections[key(old)] = value
                    except (ValueError, KeyError, TypeError) as error:
                        errors[key(old)] = str(error)
            else:
                choices = []
                explicit = item.get('choice')
                for preset in sorted(card.document.get('presets', {})):
                    for variant in sorted(card.document['variants']):
                        if explicit and explicit != {'preset': preset, 'variant': variant}:
                            continue
                        try:
                            value = describe(preset, variant)
                            if value not in choices:
                                choices.append(value)
                        except (ValueError, KeyError, TypeError) as error:
                            errors[preset + '/' + variant] = str(error)
                            continue
                default = None
                if not explicit:
                    try:
                        chosen = card.select()
                        default = next((v for v in choices if v['preset'] == chosen.preset and v['variant'] == chosen.variant), None)
                    except (ValueError, KeyError, TypeError):
                        pass
                if default is not None:
                    choices = [{**value, 'card_default': value == default} for value in choices]
                selections['new'] = choices
            result[name] = {'status': 'inspected', 'selections': selections, 'errors': errors,
                            'card_sha256': item['card_sha256'], 'lock_sha256': item['lock_sha256']}
        except (ValueError, KeyError, TypeError) as error:
            result[name] = {'status': 'incompatible', 'reason': str(error)}
    return result


def installed_inspection(batch, output, runtime=None):
    """One disposable, offline SDK container per metadata plan; no cache mount."""
    baseline, dependency_lock = CONFIG.default_baseline(runtime)
    sdk_evidence = output / 'sdk-inspection'
    sdk_evidence.mkdir()
    with tempfile.TemporaryDirectory(prefix='.sdk-', dir=output) as temporary:
        work = Path(temporary)
        (work / 'config').mkdir()
        SEED.write_json(work / 'config/baseline.json', baseline)
        shutil.copyfile(dependency_lock, work / 'config' / baseline['sdk']['dependency_lock'])
        SEED.write_json(work / 'inspection-input.json', batch)
        with (sdk_evidence / 'acquire.log').open('w') as log:
            subprocess.run([*layout.command('sdk-environment'), 'acquire', '--baseline', str(work / 'config/baseline.json'),
                            '--payload-root', str(work / 'payloads'), '--output', str(sdk_evidence / 'download')],
                           check=True, stdout=log, stderr=subprocess.STDOUT)
        name = 'hrx-publication-inspection-' + uuid.uuid4().hex
        command = ['docker', 'run', '--rm', '--pull=never', '--name', name, '--read-only',
                   '--platform', baseline['container']['platform'], '--user', f'{os.getuid()}:{os.getgid()}',
                   '--network', 'none', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                   '--tmpfs', '/tmp:rw,size=128m,mode=1777', '--env', 'HOME=/tmp',
                   '--env', 'PYTHONDONTWRITEBYTECODE=1', *layout.mount_arguments(),
                   '--mount', f'type=bind,src={work},dst=/run',
                   '--mount', f'type=bind,src={sdk_evidence},dst=/results',
                   baseline['container']['image'], *layout.command('refresh-publications', interpreter='python', container=True),
                   'inspect-stage']
        try:
            with (sdk_evidence / 'inspect.log').open('w') as log:
                subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT, timeout=240)
        finally:
            subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return parse(regular(sdk_evidence, 'inspection.json')), parse(regular(sdk_evidence, 'installed/sdk.json'))['identity']


def model_rows(existing, metadata, inspections, include_new, max_bytes):
    rows, known = [], {x['selection']['repository'] for x in existing}
    available_assets = {a['sha256'] for x in existing for a in x['assets']}
    by_repository = {}
    for old in existing:
        by_repository.setdefault(old['selection']['repository'], []).append(old)
    for name in sorted(metadata):
        source, inspection = metadata[name], inspections.get(name, {})
        olds = by_repository.get(name, [])
        for old in olds or ([None] if include_new and name not in known else []):
            row = {'repository': name, 'old_selection': old['selection'] if old else None,
                   'aliases': copy.deepcopy(old['aliases']) if old else [], 'kind': 'update' if old else 'addition'}
            if source['status'] != 'available':
                row.update(status='unavailable', reason=source['reason'])
            elif inspection.get('status') != 'inspected':
                row.update(status='incompatible', reason=inspection.get('reason', 'SDK inspection missing'))
            else:
                value = inspection['selections'].get(key(old['selection'])) if old else inspection['selections'].get('new', [])
                if not old and value:
                    eligible = [v for v in value if type(v.get('gguf_bytes')) is int and 0 < v['gguf_bytes'] <= max_bytes]
                    defaults = [v for v in eligible if v.get('card_default')]
                    if defaults:
                        eligible = defaults
                    if not eligible:
                        row.update(status='outside_asset_policy', reason='no supported selection has a known positive GGUF size within the catalog limit')
                        rows.append(row)
                        continue
                    value = eligible
                if old and value is None:
                    row.update(status='incompatible', reason=inspection['errors'].get(key(old['selection']), 'selection unavailable'))
                elif not old and not value:
                    reasons = '; '.join(f'{choice}: {reason}' for choice, reason in sorted(inspection.get('errors', {}).items()))
                    row.update(status='unsupported', reason='no supported named-preset source selection' + (': ' + reasons if reasons else ''))
                elif not old and len(value) != 1:
                    row.update(status='needs_selection', reason='several eligible preset/variant combinations',
                               choices=[{k: v[k] for k in ('model_id', 'preset', 'variant', 'programs')} for v in value])
                else:
                    selected = value if old else value[0]
                    selection = {'repository': name, 'publication_revision': source['revision'],
                                 **{k: selected[k] for k in ('model_id', 'preset', 'variant', 'programs')}}
                    identity(selection)
                    row.update(selection=selection, selection_id=key(selection), assets=selected['assets'],
                               gguf_bytes=selected['gguf_bytes'], service_kind=selected['service_kind'],
                               published_execution=selected['published_execution'],
                               card_sha256=source['card_sha256'], lock_sha256=source['lock_sha256'])
                    sizes = {a['sha256']: a['size'] for a in selected['assets'] if a['sha256'] not in available_assets}
                    row['missing_asset_bytes'] = sum(sizes.values())
                    row['status'] = ('unchanged' if old and old['selection'] == selection else 'updated' if old else 'added')
                    if not old and (type(row['gguf_bytes']) is not int or row['gguf_bytes'] <= 0):
                        row.update(status='unsupported', reason='no locked GGUF parameter size')
                    elif not old and row['gguf_bytes'] > max_bytes:
                        row.update(status='outside_asset_policy', reason='GGUF bytes exceed catalog limit')
                    elif old and old.get('cataloged') and (type(row['gguf_bytes']) is not int or not 0 < row['gguf_bytes'] <= max_bytes):
                        row.update(status='outside_asset_policy', reason='updated GGUF size is unknown or exceeds the existing catalog limit')
            rows.append(row)
    return rows


def proposed_configs(inputs, rows):
    documents = {name: parse(record['text']) for name, record in inputs.items()}
    accepted = [r for r in rows if r['status'] in ('updated', 'added')]
    mapping = {key(r['old_selection']): r for r in accepted if r['old_selection']}
    catalog = documents[CONFIG_PATHS[1]]
    for entry in catalog['entries']:
        old = {'repository': entry['model_repository'], 'publication_revision': entry['publication_revision'],
               **{k: entry[k] for k in ('model_id', 'preset', 'variant')}, 'programs': 'source'}
        row = mapping.get(key(old))
        if row and (type(row['gguf_bytes']) is not int or not 0 < row['gguf_bytes'] <= catalog['max_gguf_bytes']):
            raise ValueError('updated catalog model has no eligible locked GGUF size: ' + entry['model_repository'])
        if row:
            entry['publication_revision'] = row['selection']['publication_revision']
            entry['gguf_bytes'] = row['gguf_bytes']
    for row in accepted:
        if row['kind'] == 'addition':
            selected = row['selection']
            catalog['entries'].append({'model_repository': selected['repository'],
                **{k: selected[k] for k in ('publication_revision', 'preset', 'variant', 'model_id')}, 'gguf_bytes': row['gguf_bytes']})
    catalog['entries'].sort(key=lambda x: (x['gguf_bytes'], x['model_repository'], x['preset'], x['variant']))
    for entry in documents[CONFIG_PATHS[0]]['entries']:
        row = mapping.get(key(entry['selection']))
        if row:
            entry['selection'] = copy.deepcopy(row['selection'])
            entry['published_execution'] = copy.deepcopy(row['published_execution'])
    for name, path in ((CONFIG_PATHS[2], ('overrides',)), (CONFIG_PATHS[3], ('request_profiles', 'overrides'))):
        target = documents[name]
        for part in path:
            target = target[part]
        indexed = {key(x['selection']): x for x in target}
        if len(indexed) != len(target):
            raise ValueError('duplicate exact-selection test profile')
        additions = []
        for entry in target:
            row = mapping.get(key(entry['selection']))
            if row:
                addition = {**copy.deepcopy(entry), 'selection': copy.deepcopy(row['selection'])}
                new_key = key(row['selection'])
                if new_key in indexed and indexed[new_key] != addition:
                    raise ValueError('conflicting exact-selection test profile')
                if new_key not in indexed:
                    additions.append(addition); indexed[new_key] = addition
        target.extend(sorted(additions, key=lambda x: key(x['selection'])))
    return {name: formatted(documents[name]) for name in CONFIG_PATHS}


def generated_suite(rows, batch_id):
    entries = {}
    for row in rows:
        if row['status'] not in ('updated', 'added'):
            continue
        ident = row['selection_id']
        entries[ident] = {'id': 'refresh-' + ident, 'selection': row['selection'],
                          'published_execution': row['published_execution'],
                          'seed_manifest': f'publication-refresh/{batch_id}/{ident}/seed-manifest.json'}
    return {'schema_version': 1, 'entries': [entries[k] for k in sorted(entries)]}


def make_plan(repo, existing, metadata, inspections, inspection_identity, include_new, author, choices):
    inputs = {name: {'text': regular(repo, name)} for name in INPUT_PATHS}
    for record in inputs.values():
        record['sha256'] = sha(record['text'].encode())
    max_bytes = parse(inputs[CONFIG_PATHS[1]]['text'])['max_gguf_bytes']
    rows = model_rows(existing, metadata, inspections, include_new, max_bytes)
    proposed = proposed_configs(inputs, rows)
    document = {'schema_version': 1, 'author': author, 'include_new': include_new,
                'explicit_choices': choices, 'inputs': inputs, 'existing': existing,
                'metadata': metadata, 'inspections': inspections, 'inspection_identity': inspection_identity,
                'models': rows, 'proposed': {name: {'text': text, 'sha256': sha(text.encode())} for name, text in proposed.items()}}
    document['batch_id'] = sha(canonical({'rows': rows, 'inputs': {k: v['sha256'] for k, v in inputs.items()}}))[:24]
    document['suite'] = generated_suite(rows, document['batch_id'])
    document['plan_sha256'] = sha(canonical(document))
    return document


def validate_plan(plan):
    if not isinstance(plan, dict) or type(plan.get('schema_version')) is not int or plan['schema_version'] != 1:
        raise ValueError('unsupported refresh plan')
    copy_plan = {k: v for k, v in plan.items() if k != 'plan_sha256'}
    if plan.get('plan_sha256') != sha(canonical(copy_plan)):
        raise ValueError('refresh plan digest changed')
    if (type(plan.get('include_new')) is not bool or not isinstance(plan.get('author'), str) or
            not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', plan['author'])):
        raise ValueError('invalid refresh plan discovery parameters')
    if set(plan['inputs']) != set(INPUT_PATHS) or set(plan['proposed']) != set(CONFIG_PATHS):
        raise ValueError('refresh plan has undeclared configuration paths')
    for record in [*plan['inputs'].values(), *plan['proposed'].values()]:
        if not isinstance(record['text'], str) or len(record['text'].encode()) > MAX_DOCUMENT or record['sha256'] != sha(record['text'].encode()):
            raise ValueError('refresh configuration identity changed')
        parse(record['text'])
    for name, source in plan['metadata'].items():
        if not REPOSITORY.fullmatch(name) or source['repository'] != name:
            raise ValueError('invalid planned repository')
        if source['status'] == 'available':
            if not REVISION.fullmatch(source['revision']):
                raise ValueError('planned revision must be immutable')
            for kind in ('card', 'lock'):
                if sha(source[kind].encode()) != source[kind + '_sha256']:
                    raise ValueError('saved publication metadata changed')
            inspection = plan['inspections'].get(name, {})
            if inspection.get('status') == 'inspected' and any(inspection.get(k) != source[k] for k in ('card_sha256', 'lock_sha256')):
                raise ValueError('SDK inspection describes different card/lock bytes')
    rows = model_rows(plan['existing'], plan['metadata'], plan['inspections'], plan['include_new'],
                      parse(plan['inputs'][CONFIG_PATHS[1]]['text'])['max_gguf_bytes'])
    if rows != plan['models'] or proposed_configs(plan['inputs'], rows) != {k: v['text'] for k, v in plan['proposed'].items()}:
        raise ValueError('refresh plan does not match its saved selection evidence')
    if plan['suite'] != generated_suite(rows, plan['batch_id']):
        raise ValueError('refresh suite differs from saved selections')
    return plan


class Interrupted(KeyboardInterrupt):
    def __init__(self, signum):
        self.signum = signum
        super().__init__('interrupted by signal ' + str(signum))


def apply_plan(plan, repo=layout.REPO_ROOT, only=None):
    # Directory inode locking needs no new source-tree state or host service.
    fd = os.open(Path(repo) / 'config', os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('another publication apply owns the configuration lock') from error
        return _apply_locked(plan, repo, only)
    finally:
        os.close(fd)


def candidate_snapshot(plan, cache_root, repo=layout.REPO_ROOT, only=None):
    """Freeze staged receipts without making them part of live discovery.

    Cached-mode preparation derives service kind, execution and strict defaults
    from the verified card, unlike an authored sample suite's frozen baseline.
    """
    validate_plan(plan)
    selected = {r['selection_id'] for r in plan['models'] if r['status'] in ('updated', 'added')}
    if only is not None:
        if not isinstance(only, list) or any(not isinstance(x, str) for x in only) or len(set(only)) != len(only) or set(only) - selected:
            raise ValueError('--only must contain unique planned selection IDs')
        selected = set(only)
    for name in CONFIG_PATHS:
        if sha(regular(repo, name).encode()) != plan['proposed'][name]['sha256']:
            raise ValueError('apply the full configuration plan before freezing validation selection: ' + name)
    records = []
    for item in plan['suite']['entries']:
        selection_id = key(item['selection'])
        if selection_id not in selected:
            continue
        raw = SNAPSHOT.regular(cache_root, item['seed_manifest'])
        frozen = SNAPSHOT.seed_record(raw)
        if frozen['selection'] != item['selection']:
            raise ValueError('candidate seed selection differs from refresh plan')
        row = next(r for r in plan['models'] if r.get('selection_id') == selection_id)
        seed = parse(raw.decode('utf-8'))
        if any(seed[field] != row[field] for field in ('card_sha256', 'lock_sha256')):
            raise ValueError('candidate seed card/lock differs from refresh plan')
        entry = {'id': 'cached-' + INVENTORY.fingerprint(item['selection']), 'selection': item['selection'],
                 'aliases': [{'path': item['seed_manifest'], 'sha256': frozen['seed_sha256']}],
                 'asset_identity': frozen['asset_identity'], 'conflict': False}
        records.append({'entry': entry, **frozen})
    inventory = {'schema_version': 1, 'mode': 'cached', 'snapshot_at': datetime.now(timezone.utc).isoformat(),
                 'entries': [r['entry'] for r in records], 'errors': []}
    result = {'schema_version': 1, 'mode': 'cached', 'inventory': inventory,
              'authored_suite': None, 'repository_inputs': SNAPSHOT.config_inputs(repo), 'entries': records}
    SNAPSHOT.validate(result, cache_root, repo)
    if len(formatted(result).encode()) > SNAPSHOT.MAX_BYTES:
        raise ValueError('candidate snapshot exceeds size limit')
    return result


def _apply_locked(plan, repo, only):
    validate_plan(plan)
    known = {r['selection_id'] for r in plan['models'] if r['status'] in ('updated', 'added')}
    if only is not None and (not isinstance(only, list) or any(not isinstance(x, str) for x in only) or len(set(only)) != len(only) or set(only) - known):
        raise ValueError('--only must contain unique planned selection IDs')
    rows = plan['models'] if only is None else [r for r in plan['models'] if r.get('selection_id') in only]
    desired = proposed_configs(plan['inputs'], rows)
    before, changes, observed = {}, {}, {}
    for name in INPUT_PATHS:
        text = regular(repo, name)
        observed[name] = text
        allowed = {plan['inputs'][name]['sha256']}
        if name in desired:
            allowed |= {plan['proposed'][name]['sha256'], sha(desired[name].encode())}
        if sha(text.encode()) not in allowed:
            raise ValueError('configuration changed since planning: ' + name)
        if name in desired and text != desired[name]:
            before[name], changes[name] = text, desired[name]
    staged, written, handlers = {}, [], {}
    if threading.current_thread() is threading.main_thread():
        def interrupt(signum, frame):
            raise Interrupted(signum)
        handlers = {s: signal.signal(s, interrupt) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        for name, text in changes.items():
            path = Path(repo) / name
            fd, temporary = tempfile.mkstemp(prefix='.refresh-', dir=path.parent)
            staged[name] = Path(temporary)
            with os.fdopen(fd, 'w') as stream:
                stream.write(text); stream.flush(); os.fsync(stream.fileno())
            os.chmod(temporary, path.stat().st_mode & 0o777)
        # Recheck immediately before publication, after all new bytes are staged.
        for name, text in observed.items():
            if regular(repo, name) != text:
                raise ValueError('configuration changed during apply: ' + name)
        for name in changes:
            os.replace(staged[name], Path(repo) / name); written.append(name)
    except BaseException:
        for signum in handlers:
            signal.signal(signum, signal.SIG_IGN)
        for name in reversed(written):
            path = Path(repo) / name
            fd, temporary = tempfile.mkstemp(prefix='.refresh-rollback-', dir=path.parent)
            try:
                with os.fdopen(fd, 'w') as stream:
                    stream.write(before[name])
                os.chmod(temporary, path.stat().st_mode & 0o777)
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        raise
    finally:
        for path in staged.values():
            path.unlink(missing_ok=True)
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
    return {'schema_version': 1, 'status': 'applied' if changes else 'unchanged',
            'plan_sha256': plan['plan_sha256'], 'files': sorted(changes),
            'selected_ids': sorted(known if only is None else only)}


def plan_host(args, *, repo=layout.REPO_ROOT, client=None, inspector=installed_inspection):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', args.author):
        raise ValueError('invalid publication author')
    choices = request_choices(args.select)
    if choices and not args.include_new:
        raise ValueError('--select requires --include-new')
    documents = {name: parse(regular(repo, name)) for name in INPUT_PATHS}
    root = Path(args.root or os.environ.get('HRX_SYSTEMS_ROOT', Path.home() / '.local/share/hrx-systems-test')).expanduser()
    if not root.is_absolute() or root == Path('/'):
        raise ValueError('cache root must be absolute and other than /')
    existing = existing_selections(documents, root / 'cache', args.author)
    client = client or Hub()
    listed = client.repositories(args.author)
    names = {x['selection']['repository'] for x in existing}
    if args.include_new:
        names |= set(listed)
    if args.model:
        unknown = set(args.model) - names
        if unknown:
            raise ValueError('requested models absent from configured/cached/discovered inventory: ' + ', '.join(sorted(unknown)))
        names &= set(args.model)
        existing = [x for x in existing if x['selection']['repository'] in names]
    if set(choices) - names or set(choices) & {x['selection']['repository'] for x in existing}:
        raise ValueError('explicit choices must name discovered new models only')
    if len(names) > MAX_REPOSITORIES:
        raise ValueError('selected repository count exceeds limit')
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        metadata = dict(pool.map(lambda name: (name, client.metadata(name, listed.get(name))), sorted(names)))
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    batch = [{**metadata[name], 'existing': [x['selection'] for x in existing if x['selection']['repository'] == name],
              'choice': choices.get(name)} for name in sorted(metadata) if metadata[name]['status'] == 'available']
    try:
        inspections, sdk_identity = inspector(batch, output, args.runtime) if batch else ({}, None)
        plan = make_plan(repo, existing, metadata, inspections, sdk_identity, args.include_new, args.author, choices)
        validate_plan(plan)
        if len(formatted(plan).encode()) > MAX_PLAN:
            raise ValueError('refresh plan exceeds size limit')
        SEED.write_json(output / 'plan.json', plan)
        SEED.write_json(output / 'suite.json', plan['suite'])
        diff = ''.join(''.join(difflib.unified_diff(plan['inputs'][name]['text'].splitlines(True),
                     plan['proposed'][name]['text'].splitlines(True), fromfile='a/' + name, tofile='b/' + name)) for name in CONFIG_PATHS)
        (output / 'configuration.diff').write_text(diff)
        lines = ['# Publication refresh plan', '', '| Repository | Result | Old revision | New revision | Missing asset bytes |',
                 '| --- | --- | --- | --- | ---: |']
        for row in plan['models']:
            lines.append('| ' + ' | '.join(str(x).replace('|', '\\|').replace('\n', ' ') for x in
                (row['repository'], row['status'], (row['old_selection'] or {}).get('publication_revision', '')[:12],
                 row.get('selection', {}).get('publication_revision', '')[:12], row.get('missing_asset_bytes', 'unknown'))) + ' |')
            if row.get('reason'):
                lines.append('\n' + row['repository'] + ': ' + row['reason'].replace('\n', ' ') + '\n')
        (output / 'summary.md').write_text('\n'.join(lines) + '\n')
        return plan
    except BaseException as error:
        SEED.write_json(output / 'plan-error.json', {'schema_version': 1, 'status': 'failed', 'reason': str(error)})
        raise


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ('inspect-stage', 'inspect-native'):
        private = argparse.ArgumentParser(description='Private publication metadata SDK inspection')
        private.add_argument('command', choices=('inspect-stage', 'inspect-native'))
        operation = private.parse_args(argv).command
        if operation == 'inspect-stage':
            subprocess.run([*layout.command('sdk-environment', container=True), 'install', '--baseline', '/run/config/baseline.json',
                            '--payload-root', '/run/payloads', '--output', '/results/installed'], check=True)
            return subprocess.run([*layout.command('refresh-publications', interpreter='/run/venv/bin/python', container=True), 'inspect-native']).returncode
        SEED.write_json('/results/inspection.json', inspect_native(parse(regular('/run', 'inspection-input.json', MAX_PLAN))))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('plan', help='fetch bounded metadata and produce a reviewable configuration plan')
    p.add_argument('--author', default='loom-fun'); p.add_argument('--root', type=Path)
    p.add_argument('--include-new', action='store_true'); p.add_argument('--select', action='append', default=[], metavar='REPOSITORY=PRESET,VARIANT')
    p.add_argument('--model', action='append', default=[]); p.add_argument('--runtime', type=Path)
    p.add_argument('--output', type=Path, required=True)
    p = sub.add_parser('apply', help='apply a saved plan offline; no downloads, seeding or model execution')
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--only', type=Path, help='JSON array of ready selection IDs; empty array restores original pins')
    p = sub.add_parser('snapshot', help='freeze staged seed receipts for normal cached-mode preparation')
    p.add_argument('--plan', type=Path, required=True); p.add_argument('--root', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--only', type=Path, help='JSON array of candidate selection IDs whose receipts are ready')
    args = parser.parse_args(argv)
    try:
        if args.command == 'apply':
            plan = parse(regular(args.plan.parent, args.plan.name, MAX_PLAN))
            result = apply_plan(plan, only=parse(regular(args.only.parent, args.only.name)) if args.only else None)
            print(formatted(result), end='')
        elif args.command == 'snapshot':
            plan = parse(regular(args.plan.parent, args.plan.name, MAX_PLAN))
            result = candidate_snapshot(plan, args.root/'cache', only=parse(regular(args.only.parent, args.only.name)) if args.only else None)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open('x') as stream:
                stream.write(formatted(result))
            print(formatted({'status': 'frozen', 'snapshot': str(args.output), 'count': len(result['entries'])}), end='')
        else:
            plan = plan_host(args)
            counts = {}
            for row in plan['models']:
                counts[row['status']] = counts.get(row['status'], 0) + 1
            print(formatted({'status': 'planned', 'plan': str(args.output / 'plan.json'), 'counts': counts}), end='')
        return 0
    except Interrupted as error:
        print('Publication refresh failed: ' + str(error), file=sys.stderr)
        return 128 + error.signum
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print('Publication refresh failed: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
