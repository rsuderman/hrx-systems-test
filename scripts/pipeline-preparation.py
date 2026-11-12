"""Frozen pipeline-upgrade provenance shared by preparation and offline preflight.

No source resolution, model asset reads, or source transformation occurs while
verifying a prepared bundle. Published and installed identities stay separate.
"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil

SCRIPTS = Path(__file__).resolve().parent
MAX_REPORT_BYTES = 4 * 1024 * 1024


def helper(name):
    spec = importlib.util.spec_from_file_location('pipeline_' + name.replace('-', '_'), SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def contained(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('pipeline provenance path must stay inside prepared bundle')
    lexical = Path(root) / relative
    if lexical.is_symlink():
        raise ValueError('pipeline provenance must not be a symlink')
    target = lexical.resolve(strict=True)
    if not target.is_relative_to(Path(root).resolve(strict=True)):
        raise ValueError('pipeline provenance path escapes prepared bundle')
    return target


def write_json(path, value):
    path = Path(path)
    encoded = (json.dumps(value, indent=2) + '\n').encode()
    if len(encoded) > MAX_REPORT_BYTES:
        raise ValueError('pipeline provenance exceeds bounded evidence limit')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)


def read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_REPORT_BYTES:
        raise ValueError('pipeline provenance must be a bounded regular file')
    return json.loads(path.read_text())


def freeze_policy(prepared, snapshot, *, pipeline_upgrade='auto', pool_backing=None, memory_bytes=None):
    policy = helper('runtime-policy').resolve(snapshot, pipeline_upgrade=pipeline_upgrade,
                                              pool_backing=pool_backing, memory_bytes=memory_bytes)
    files = []
    destination = Path(prepared) / 'policies/runtime-policy.json'
    write_json(destination, policy)
    files.append({'kind': 'runtime_policy', 'path': 'policies/runtime-policy.json', 'sha256': digest(destination)})
    if policy['seccomp'] is not None:
        descriptor = policy['seccomp']
        source = contained(SCRIPTS.parent, descriptor['source_path'])
        if source.is_symlink() or digest(source) != descriptor['sha256']:
            raise ValueError('runtime seccomp source changed before preparation')
        target = Path(prepared) / 'policies/seccomp.json'
        shutil.copyfile(source, target)
        files.append({'kind': 'seccomp', 'path': 'policies/seccomp.json', 'sha256': descriptor['sha256']})
    return policy, files


def validate_policy(prepared, suite):
    policy = suite.get('runtime_policy')
    if policy is None:
        if suite.get('policy_files'):
            raise ValueError('policy files require a frozen runtime policy')
        return None
    policy = helper('runtime-policy').validate(policy, suite['runtime']['resolved_runtime'])
    records = suite.get('policy_files')
    if not isinstance(records, list):
        raise ValueError('frozen runtime policy lacks file identities')
    expected = {'runtime_policy'} | ({'seccomp'} if policy['seccomp'] else set())
    if {record.get('kind') for record in records} != expected or len(records) != len(expected):
        raise ValueError('frozen runtime policy file closure changed')
    for record in records:
        target = contained(prepared, record['path'])
        if target.is_symlink() or digest(target) != record['sha256']:
            raise ValueError('frozen runtime policy file changed')
        if record['kind'] == 'runtime_policy':
            if read_json(target) != policy:
                raise ValueError('frozen runtime policy disagrees with its receipt')
        elif record['sha256'] != policy['seccomp']['sha256']:
            raise ValueError('frozen seccomp file disagrees with runtime policy')
    return policy


def validate_frozen_inputs(prepared, suite):
    expected = {'selection_snapshot': ('selection-snapshot.json', 32 * 1024 * 1024),
                'runtime_selection': ('runtime-selection.json', MAX_REPORT_BYTES)}
    records = suite.get('frozen_inputs', [])
    if not isinstance(records, list) or len(records) > 2:
        raise ValueError('invalid frozen input evidence')
    seen = set()
    for record in records:
        kind = record.get('kind')
        if kind not in expected or kind in seen or set(record) != {'kind', 'path', 'sha256'}:
            raise ValueError('invalid frozen input evidence closure')
        seen.add(kind)
        name, limit = expected[kind]
        if record['path'] != name:
            raise ValueError('invalid frozen input evidence path')
        path = contained(prepared, name)
        if not path.is_file() or path.stat().st_size > limit or digest(path) != record['sha256']:
            raise ValueError('frozen input evidence changed')
    return records


def profile(baseline, policy):
    """Freeze request changes after native source selection determines service kind."""
    output = copy.deepcopy(baseline)
    original = {'smoke_request': copy.deepcopy(baseline['smoke_request']),
                'smoke_expected_text': baseline['smoke_expected_text'],
                'profile_identity': baseline.get('profile_identity', 'explicit-baseline')}
    identity = {key: baseline[key] for key in ('repository', 'publication_revision', 'model_id', 'preset', 'variant', 'programs')}
    selected = helper('runtime-policy').request_profile(policy, identity, original['smoke_request'], original['smoke_expected_text'])
    if not selected['override']:
        selected['profile_identity'] = original['profile_identity']
    output.update({key: selected[key] for key in ('smoke_request', 'smoke_expected_text', 'profile_identity')})
    output['runtime_policy_sha256'] = policy['policy_sha256']
    output['published_test_profile'] = original
    output['effective_test_profile'] = selected
    return output


def source_identities(root):
    root = Path(root)
    paths = sorted({root / 'model.json', *root.rglob('*.loom'), *root.rglob('*.lock')})
    identities = []
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError('pipeline source identity requires regular source files')
        identities.append({'path': str(path.relative_to(root)), 'sha256': digest(path), 'size_bytes': path.stat().st_size})
    return identities


def seal_upgrade(destination, work, seed, policy, entry_id):
    """Retain verified original input and make the executable derivative in place."""
    destination, work = Path(destination), Path(work)
    original = destination / 'model'
    publication = []
    for source_name, target_name, expected in (
            ('model.json', 'publication-card.json', seed['card_sha256']),
            ('model.json.lock', 'publication-lock.json', seed['lock_sha256'])):
        source = work / source_name
        if source.is_symlink() or digest(source) != expected:
            raise ValueError('verified original publication card/lock changed')
        target = destination / target_name
        shutil.copyfile(source, target)
        publication.append({'path': f'models/{entry_id}/{target_name}', 'sha256': expected})
    enabled = policy['pipeline_upgrade']['enabled']
    if enabled:
        retained = destination / 'model-original'
        original.rename(retained)
        upgrader = helper('upgrade-pipelines-0.1.0-to-0.2.0')
        report, adjacent = upgrader.write_derivative(retained, original)
        report['source'], report['output'] = 'model-original', 'model'
        adjacent.unlink()
        source_tree = f'models/{entry_id}/model-original'
    else:
        identities = source_identities(original)
        report = {'schema_version': 1, 'status': 'skipped', 'reason': policy['pipeline_upgrade']['reason'],
                  'source_files': identities, 'output_files': identities, 'files': [],
                  'changed_file_count': 0, 'operation_counts': {}, 'publication_ready': False,
                  'locks_modified': False, 'migration_id': None}
        source_tree = f'models/{entry_id}/model'
    report.update(policy_sha256=policy['policy_sha256'],
                  runtime_identity_sha256=policy['runtime_identity_sha256'])
    path = destination / 'pipeline-upgrade.json'
    write_json(path, report)
    return {'schema_version': 1, 'status': report['status'], 'reason': report.get('reason'),
            'migration_id': report.get('migration_id'), 'policy_sha256': policy['policy_sha256'],
            'source_tree': source_tree, 'publication': publication,
            'report_path': f'models/{entry_id}/pipeline-upgrade.json', 'report_sha256': digest(path)}


def _verify_identities(root, records):
    if not isinstance(records, list) or not records:
        raise ValueError('pipeline report lacks source identities')
    names = [record['path'] for record in records]
    if len(names) != len(set(names)):
        raise ValueError('pipeline report repeats source identities')
    if set(names) != {record['path'] for record in source_identities(root)}:
        raise ValueError('pipeline source file closure changed')
    for record in records:
        path = contained(root, record['path'])
        if path.is_symlink() or path.stat().st_size != record['size_bytes'] or digest(path) != record['sha256']:
            raise ValueError('pipeline source identity changed')


def verify_upgrade(prepared, entry, manifest, baseline, policy):
    metadata = manifest.get('pipeline_upgrade')
    if policy is None:
        if metadata is not None:
            raise ValueError('upgraded input requires a frozen runtime policy')
        return
    if not isinstance(metadata, dict) or entry.get('pipeline_upgrade') != metadata:
        raise ValueError('pipeline upgrade metadata disagrees with prepared entry')
    if metadata.get('schema_version') != 1 or metadata.get('policy_sha256') != policy['policy_sha256']:
        raise ValueError('pipeline upgrade policy identity changed')
    report_path = contained(prepared, metadata['report_path'])
    if digest(report_path) != metadata['report_sha256']:
        raise ValueError('pipeline upgrade report changed')
    report = read_json(report_path)
    if (report.get('status') != metadata['status'] or report.get('migration_id') != metadata['migration_id'] or
            report.get('policy_sha256') != policy['policy_sha256'] or
            report.get('runtime_identity_sha256') != policy['runtime_identity_sha256']):
        raise ValueError('pipeline report runtime/migration identity changed')
    if policy['pipeline_upgrade']['enabled']:
        if (type(report.get('implementation_revision')) is not int or report['implementation_revision'] < 1 or
                not isinstance(report.get('implementation_sha256'), str) or
                not re.fullmatch('[0-9a-f]{64}', report['implementation_sha256'])):
            raise ValueError('pipeline migration implementation identity is invalid')
        if metadata['status'] not in ('applied', 'unchanged') or metadata['migration_id'] != policy['pipeline_upgrade']['migration_id']:
            raise ValueError('required pipeline upgrade was not applied')
    elif metadata['status'] != 'skipped':
        raise ValueError('raw runtime unexpectedly has upgraded source')
    source = contained(prepared, metadata['source_tree'])
    output = contained(prepared, f"models/{entry['id']}/model")
    expected_source = f"models/{entry['id']}/model-original" if policy['pipeline_upgrade']['enabled'] else f"models/{entry['id']}/model"
    if metadata['source_tree'] != expected_source:
        raise ValueError('pipeline original source tree path changed')
    _verify_identities(source, report['source_files'])
    _verify_identities(output, report['output_files'])
    publications = metadata.get('publication')
    if not isinstance(publications, list) or len(publications) != 2:
        raise ValueError('original publication evidence closure changed')
    expected_paths = [f"models/{entry['id']}/publication-card.json", f"models/{entry['id']}/publication-lock.json"]
    if [item.get('path') for item in publications] != expected_paths:
        raise ValueError('original publication evidence paths changed')
    for publication, expected in zip(publications, (manifest['card_sha256'], manifest['lock_sha256'])):
        if publication['sha256'] != expected or digest(contained(prepared, publication['path'])) != expected:
            raise ValueError('original publication identity changed')
    original = baseline.get('published_test_profile')
    if not isinstance(original, dict) or baseline.get('runtime_policy_sha256') != policy['policy_sha256']:
        raise ValueError('frozen model baseline lacks runtime policy/profile')
    selection = entry['selection']
    selected = helper('runtime-policy').request_profile(policy, selection, original['smoke_request'], original['smoke_expected_text'])
    if not selected['override']:
        selected['profile_identity'] = original['profile_identity']
    if (baseline.get('effective_test_profile') != selected or
            any(baseline[key] != selected[key] for key in ('smoke_request', 'smoke_expected_text', 'profile_identity')) or
            manifest.get('effective_test_profile') != selected):
        raise ValueError('frozen effective request profile changed')
    if manifest.get('published_execution') != baseline['published_execution']:
        raise ValueError('original published execution settings changed')
    if manifest['effective_execution'] != baseline.get('effective_execution'):
        raise ValueError('effective executable settings changed')


def failed_selection(value):
    """Accept a bounded, tagged resolver failure for ordinary per-entry reporting."""
    if not isinstance(value, dict) or value.get('status') != 'failed':
        return None
    if set(value) != {'schema_version', 'status', 'selector', 'error'} or value['schema_version'] != 1:
        raise ValueError('invalid frozen runtime resolution failure')
    selector, error = value['selector'], value['error']
    if not isinstance(selector, dict) or selector.get('kind') not in ('release_latest', 'github_actions', 'release'):
        raise ValueError('invalid frozen failure selector')
    if selector['kind'] == 'release_latest':
        if selector != {'kind': 'release_latest', 'repository': 'ROCm/hrx-system', 'mode': 'latest-stable'}:
            raise ValueError('invalid frozen latest-release failure selector')
    elif selector['kind'] == 'github_actions':
        fields = {'kind', 'repository', 'branch', 'mode', 'artifact_name'}
        if selector.get('mode') == 'replay':
            fields |= {'run_id', 'run_attempt', 'artifact_id', 'wheel_sha256'}
            if (any(type(selector.get(key)) is not int or selector[key] <= 0 for key in ('run_id', 'run_attempt', 'artifact_id')) or
                    not isinstance(selector.get('wheel_sha256'), str) or not re.fullmatch('[0-9a-f]{64}', selector['wheel_sha256'])):
                raise ValueError('invalid frozen replay failure selector')
        if (set(selector) != fields or selector.get('repository') != 'ROCm/hrx-system' or
                selector.get('branch') != 'main' or selector.get('artifact_name') != 'loom-sdk-wheel-linux-Release' or
                selector.get('mode') not in ('latest-successful', 'replay')):
            raise ValueError('invalid frozen Actions failure selector')
    elif (selector.get('repository') != 'ROCm/hrx-system' or
          any(not isinstance(value, (str, int, dict)) for value in selector.values())):
        raise ValueError('invalid frozen pinned-release failure selector')
    if not isinstance(error, dict) or set(error) != {'kind', 'message'}:
        raise ValueError('invalid frozen resolution error')
    if error['kind'] != 'resolution':
        raise ValueError('invalid frozen resolution error kind')
    if (not isinstance(error['message'], str) or not error['message'] or
            len(error['message'].encode()) > 4096 or any(ord(char) < 32 and char not in '\n\t' for char in error['message'])):
        raise ValueError('frozen resolution error exceeds evidence bound')
    if len(json.dumps(value).encode()) > 8192:
        raise ValueError('frozen resolution failure exceeds evidence bound')
    return copy.deepcopy(value)
