#!/usr/bin/env python3
"""Pure, frozen runtime-specific source, request and shared-server policies.

Only resolve() reads checked-in configuration. Execution consumes a frozen policy
and verifies its referenced seccomp bytes separately, without reading live config.
"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re

REPO = Path(__file__).resolve().parents[1]
CONFIG_PATH = 'config/runtime-policies/0.2.0/policy.json'
SELECTION_FIELDS = ('repository', 'publication_revision', 'model_id', 'preset', 'variant', 'programs')
MIGRATION_ID = 'hrx-pipelines-0.1.0-to-0.2.0'
SHA256 = re.compile(r'[0-9a-f]{64}\Z')
VERSION = re.compile(r'0\.(1|2)\.0(?:(?:a|b|rc|\.dev|\.post)[0-9]+)*(?:\+[a-zA-Z0-9][a-zA-Z0-9._-]*)?\Z')
_SPEC = importlib.util.spec_from_file_location('policy_sdk_environment', Path(__file__).with_name('sdk-environment.py'))
SDK = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(SDK)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha(value):
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValueError('runtime policy requires a lowercase SHA256 identity')
    return value


def _relative(value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError('policy source path must be a contained repository-relative path')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or '.' in path.parts or str(path) != value:
        raise ValueError('policy source path must be a contained repository-relative path')
    return value


def _runtime(snapshot):
    try:
        if snapshot.get('schema_version') != 1:
            raise ValueError('unsupported runtime snapshot schema')
        version = snapshot['wheel']['version']
        source = snapshot['source']
        if (snapshot['wheel'].get('name') != 'hrx-loom' or not isinstance(version, str) or
                source.get('repository') != 'ROCm/hrx-system' or
                source.get('kind') not in ('release', 'github_actions') or
                not re.fullmatch(r'[0-9a-f]{40}', source.get('tested_sha', ''))):
            raise ValueError('unsupported runtime policy origin or SDK identity')
        identity = _sha(snapshot['runtime_identity_sha256'])
        if SDK.runtime_identity_sha256(snapshot) != identity:
            raise ValueError('runtime snapshot identity changed')
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError('malformed resolved runtime snapshot') from error
    match = VERSION.fullmatch(version)
    return identity, version, ('0.' + match[1] + '.0') if match else 'unsupported'


def _memory(value):
    if type(value) is int:
        result = value
    elif isinstance(value, str):
        match = re.fullmatch(r'([0-9]+)(b|kib|mib|gib|tib)?', value.strip().lower())
        if not match:
            raise ValueError('memory allowance requires a positive byte count or binary size')
        result = int(match[1]) * {'b': 1, 'kib': 1024, 'mib': 1024**2, 'gib': 1024**3, 'tib': 1024**4, None: 1}[match[2]]
    else:
        raise ValueError('memory allowance requires a positive byte count')
    if result <= 0 or result > 2**63 - 1:
        raise ValueError('memory allowance must be positive and representable as signed i64 bytes')
    return result


def _launch(backing, memory):
    if backing not in ('fixed', 'elastic'):
        raise ValueError('unsupported shared-server backing policy')
    if backing == 'fixed':
        if memory is not None:
            raise ValueError('fixed backing and an elastic memory allowance are mutually exclusive')
        return {'pool_backing': 'fixed', 'memory_bytes': None}
    if memory is None:
        raise ValueError('elastic backing requires an explicit positive memory allowance')
    return {'pool_backing': 'elastic', 'memory_bytes': _memory(memory)}


def _validate_config(config):
    try:
        if config['schema_version'] != 1 or config['runtime_family'] != '0.2.0' or config['profile_id'] != 'hrx-runtime-0.2.0-v1':
            raise ValueError('unsupported runtime policy configuration')
        if _launch(**{'backing': config['default_launch']['pool_backing'], 'memory': config['default_launch']['memory_bytes']}) != config['default_launch']:
            raise ValueError('default launch policy must use canonical byte values')
        seccomp = config['seccomp']
        for field in ('source_path', 'base_source_path'):
            _relative(seccomp[field])
        for field in ('sha256', 'base_sha256'):
            _sha(seccomp[field])
        if (seccomp['profile_id'] != 'docker-default-plus-mbind-v1' or
                seccomp['change'] != 'append unconditional allow rule for mbind only' or
                seccomp['base_source_url'] != 'https://raw.githubusercontent.com/moby/profiles/main/seccomp/default.json'):
            raise ValueError('unsupported seccomp policy configuration')
        profiles = config['request_profiles']
        if profiles['profile_id'] != 'hrx-0.2.0-exact-reasoning-none-v1':
            raise ValueError('unsupported request profile configuration')
        seen = set()
        for override in profiles['overrides']:
            selection = override['selection']
            if set(selection) != set(SELECTION_FIELDS) or any(not isinstance(selection[key], str) or not selection[key] for key in SELECTION_FIELDS):
                raise ValueError('request profile requires a complete six-field selection')
            if not re.fullmatch(r'[0-9a-f]{40}', selection['publication_revision']):
                raise ValueError('request profile requires an exact publication revision')
            identity = tuple(selection[field] for field in SELECTION_FIELDS)
            if identity in seen or override['request_fields'] != {'reasoning_effort': 'none'}:
                raise ValueError('duplicate request selection or unsupported request override')
            seen.add(identity)
        if len(seen) != 3:
            raise ValueError('runtime policy expects three exact reasoning selections')
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError('malformed runtime policy configuration') from error
    return config


def _configuration():
    path = REPO / CONFIG_PATH
    if path.is_symlink() or not path.resolve(strict=True).is_relative_to(REPO.resolve(strict=True)):
        raise ValueError('policy configuration escapes the repository')
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError('runtime policy configuration exceeds its size bound')
    content = _validate_config(json.loads(raw))
    for key in ('source_path', 'base_source_path'):
        source = REPO / content['seccomp'][key]
        if source.is_symlink() or not source.resolve(strict=True).is_relative_to(REPO.resolve(strict=True)):
            raise ValueError('seccomp source escapes the repository')
        data = source.read_bytes()
        expected = content['seccomp']['sha256' if key == 'source_path' else 'base_sha256']
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('checked-in seccomp profile identity changed')
    return {'source_path': CONFIG_PATH, 'source_sha256': hashlib.sha256(raw).hexdigest(),
            'content_sha256': digest(content), 'content': content}


def _upgrade(mode, family):
    if mode not in ('auto', 'none'):
        raise ValueError('pipeline upgrade mode must be auto or none')
    if family == 'unsupported' and mode == 'auto':
        raise ValueError('unsupported runtime family for automatic pipeline upgrading')
    enabled = family == '0.2.0' and mode == 'auto'
    return {'mode': mode, 'enabled': enabled, 'migration_id': MIGRATION_ID if enabled else None,
            'reason': 'explicit-none' if mode == 'none' else 'target-' + family}


def resolve(snapshot, pipeline_upgrade='auto', pool_backing=None, memory_bytes=None):
    """Select policy after SDK metadata verification; read repository config once."""
    runtime_id, version, family = _runtime(snapshot)
    upgrade = _upgrade(pipeline_upgrade, family)
    config = _configuration() if family == '0.2.0' else None
    if config is None:
        if pool_backing is not None or memory_bytes is not None:
            raise ValueError('launch overrides are unsupported for unchanged 0.1.0 or unknown runtime policy')
        launch, seccomp, profiles = {'pool_backing': 'default', 'memory_bytes': None}, None, None
    else:
        document = config['content']
        backing = pool_backing if pool_backing is not None else ('elastic' if memory_bytes is not None else document['default_launch']['pool_backing'])
        memory = memory_bytes if memory_bytes is not None else (document['default_launch']['memory_bytes'] if pool_backing is None else None)
        launch = _launch(backing, memory)
        seccomp = copy.deepcopy(document['seccomp'])
        profiles = {**copy.deepcopy(document['request_profiles']), 'config_path': config['source_path'], 'config_sha256': config['source_sha256']}
    policy = {'schema_version': 1, 'policy_id': 'hrx-runtime-0.2.0-v1' if config else 'hrx-unmodified-v1',
              'runtime_identity_sha256': runtime_id, 'runtime_version': version, 'runtime_family': family,
              'pipeline_upgrade': upgrade, 'launch': launch, 'seccomp': seccomp,
              'request_profiles': profiles, 'configuration': config}
    policy['policy_sha256'] = digest(policy)
    return policy


def validate(policy, snapshot):
    """Check a frozen policy against its runtime using embedded data only."""
    try:
        value = copy.deepcopy(policy)
        recorded = _sha(value.pop('policy_sha256'))
        if recorded != digest(value) or value['schema_version'] != 1:
            raise ValueError('frozen runtime policy identity changed')
        runtime_id, version, family = _runtime(snapshot)
        if (value['runtime_identity_sha256'], value['runtime_version'], value['runtime_family']) != (runtime_id, version, family):
            raise ValueError('runtime policy does not match the selected SDK')
        if value['pipeline_upgrade'] != _upgrade(value['pipeline_upgrade']['mode'], family):
            raise ValueError('frozen pipeline upgrade decision changed')
        if family == '0.2.0':
            config = value['configuration']
            _relative(config['source_path']); _sha(config['source_sha256'])
            document = _validate_config(config['content'])
            if config['content_sha256'] != digest(document):
                raise ValueError('frozen runtime configuration identity changed')
            if value['policy_id'] != document['profile_id'] or value['seccomp'] != document['seccomp']:
                raise ValueError('frozen seccomp/configuration policy mismatch')
            profiles = {**copy.deepcopy(document['request_profiles']), 'config_path': config['source_path'], 'config_sha256': config['source_sha256']}
            if value['request_profiles'] != profiles or _launch(value['launch']['pool_backing'], value['launch']['memory_bytes']) != value['launch']:
                raise ValueError('frozen request or launch policy changed')
        elif (value['policy_id'] != 'hrx-unmodified-v1' or value['configuration'] is not None or
              value['request_profiles'] is not None or value['seccomp'] is not None or
              value['launch'] != {'pool_backing': 'default', 'memory_bytes': None}):
            raise ValueError('unchanged runtime received new launch, request or container settings')
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError('malformed frozen runtime policy') from error
    return copy.deepcopy(policy)


def server_args(policy):
    """Translate the verified frozen launch block; no configuration reads."""
    launch = policy['launch']
    if launch == {'pool_backing': 'default', 'memory_bytes': None}:
        return []
    checked = _launch(launch['pool_backing'], launch['memory_bytes'])
    return ['--pool_backing=fixed'] if checked['pool_backing'] == 'fixed' else ['--memory_bytes=' + str(checked['memory_bytes'])]


def request_profile(policy, exact_selection, base_request, expected_text):
    """Apply only exact request fields; preserve every other request/assertion."""
    if not isinstance(base_request, dict):
        raise ValueError('base smoke request must be an object')
    request = copy.deepcopy(base_request)
    fields = {}
    profiles = policy.get('request_profiles')
    if profiles:
        for override in profiles['overrides']:
            if all(exact_selection.get(field) == override['selection'][field] for field in SELECTION_FIELDS):
                fields = copy.deepcopy(override['request_fields'])
                request.update(fields)
                break
    original = {'smoke_request': base_request, 'smoke_expected_text': expected_text}
    effective = {'smoke_request': request, 'smoke_expected_text': expected_text}
    return {**effective, 'profile_identity': profiles['profile_id'] if fields else 'unchanged',
            'published_request_sha256': digest(original), 'effective_request_sha256': digest(effective), 'override': fields}
