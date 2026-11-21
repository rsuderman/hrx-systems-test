# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
"""Bounded native runner ownership ABI evidence for one pinned SDK source commit.

Only stage() performs source acquisition. validate() uses sealed local bytes only.
The source-to-wheel association is the existing official build provenance, not
independent binary attestation.
"""
import base64
import copy
import hashlib
import json
from pathlib import Path
import re

REPOSITORY = 'ROCm/hrx-system'
PATHS = ('loom/src/loom/runtime/program/module.c', 'loom/src/loom/serve/service/text/control.c')
DIRECTORY = 'policies/runner-abi'
MAX_SOURCE_BYTES = 1024 * 1024
# Structural signatures from the reviewed ownership transition. Comments and
# unrelated source edits do not participate. Unknown signature/export wiring fails.
RUNNER_BLOCK_PROFILES = {'d07e21e3e9e2109a5175db922da9d0d9cb6a6dc22a0f464d973517138a46a1b1': 'borrowed-ptr', 'a3592f603e2aec049b48b0ace1c474b18f64b1363983554304ea8f1e834ae715': 'owning-ref'}
# This constructor supports both forms; the reviewed control selection below
# routes our borrowed-input derivatives to its PTR branch.
SELECTED_OWNERSHIP_BLOCK = 'b02907258acb1155566a3cf17b58cad9ddf412a945fa12a15c2150f0de86dd2b'
PROFILES = ('owning-ref', 'borrowed-ptr')
PROFILE_TYPES = {'owning-ref': ['REF'], 'borrowed-ptr': ['PTR']}
from .publication_abi import tokens, function, initializer, count_sequence


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def recognize(module, control):
    if any(not isinstance(raw, bytes) or len(raw) > MAX_SOURCE_BYTES for raw in (module, control)):
        raise ValueError('native runner source exceeds its bound')
    body = function(module.decode('utf-8'), 'loom_runtime_module_create')
    for anchor in ('module->types[0] = types->buffer;', 'module->types[1] = types->fence;',
                   '.ref_types = {module->types, IREE_ARRAYSIZE(module->types)}'):
        if count_sequence(body, tokens(anchor)) != 1:
            raise ValueError('runner canonical HAL reference types changed')
    start = tokens('module->arguments[0] =')
    end = tokens('module->exports[ordinal + 3] =')
    def position(sequence):
        found = [i for i in range(len(body)) if body[i:i + len(sequence)] == sequence]
        if len(found) != 1:
            raise ValueError('runner signature anchor missing or duplicated')
        return found[0]
    lo, hi = position(start), position(end)
    from .publication_abi import matching
    opening = body.index('{', hi)
    close = matching(body, opening)
    block = body[lo:close + 2]
    signature = sha(' '.join(block).encode())
    selected = signature == SELECTED_OWNERSHIP_BLOCK
    profile = 'borrowed-ptr' if selected else RUNNER_BLOCK_PROFILES.get(signature)
    if profile is None:
        raise ValueError('unsupported native runner signature/export construction')
    public = function(control.decode('utf-8'), 'loom_serve_text_control_initialize')
    if selected:
        selection = tokens('''
            iree_vm_signature_type_t device_buffer = {
                .kind = IREE_VM_SIGNATURE_TYPE_KIND_PTR,
                .value.ref = source->types.buffer};
            if (source->v1) {
                IREE_RETURN_IF_ERROR(loom_runtime_preparation_device_buffer_argument(
                    source->preparation, source->module, IREE_SV("epoch"),
                    source->types.buffer, &device_buffer));
            }
            const bool owned_hal_arguments =
                device_buffer.kind == IREE_VM_SIGNATURE_TYPE_KIND_REF;
        ''')
        options = tokens('''const loom_runtime_module_options_t options = {
            .owned_hal_arguments = owned_hal_arguments, .binding_capacity = 7,
            .stages = {stage_count, stages},
            .feedback = {IREE_ARRAYSIZE(feedback), feedback},};''')
        create = tokens('''loom_runtime_module_create(&source->types, execution, options,
            host_allocator, &control->native_module)''')
        if (count_sequence(public, selection) != 1 or count_sequence(public, options) != 1
                or count_sequence(public, create) != 1
                or count_sequence(public, tokens('&device_buffer')) != 1
                or count_sequence(public, tokens('device_buffer')) != 21
                or count_sequence(public, tokens('options')) != 4
                or count_sequence(public, tokens('owned_hal_arguments')) != 6):
            raise ValueError('unfamiliar runner ownership selection')
    kind = 'PTR' if profile == 'borrowed-ptr' else 'REF'
    for name, value in (
        ('buffer', '.kind = IREE_VM_SIGNATURE_TYPE_KIND_REF, .value.ref = source->vm_types.buffer'),
        ('device_buffer', '.kind = IREE_VM_SIGNATURE_TYPE_KIND_' + kind + ', .value.ref = source->types.buffer')):
        declaration = 'iree_vm_signature_type_t' if selected and name == 'device_buffer' else 'const iree_vm_signature_type_t'
        if initializer(public, name, declaration) != tokens(value):
            raise ValueError('native runner/public buffer ownership disagrees')
    for name, types in (('step_arguments', ['buffer'] + ['i32'] * 3 + ['device_buffer'] * 6),
                        ('epoch_arguments', ['buffer'] + ['i32'] * 10 + ['device_buffer'] * 12)):
        if initializer(public, name, 'const iree_vm_signature_type_t') != tokens(','.join(types)):
            raise ValueError('unfamiliar public borrowed entry signature')
    if initializer(public, 'step_results', 'const iree_vm_signature_type_t') != tokens('i64,i32'):
        raise ValueError('public step result contract changed')
    for value in (
        '{IREE_SVL("epoch"), {epoch_arguments, IREE_ARRAYSIZE(epoch_arguments)}, {&i64, 1}, &control->epoch_step}',
        'const loom_runtime_program_binding_t step = {IREE_SVL("step"), {step_arguments, IREE_ARRAYSIZE(step_arguments)}, {step_results, IREE_ARRAYSIZE(step_results)}, &control->step}',
        'loom_runtime_program_bind_service(control->program, IREE_ARRAYSIZE(bindings), bindings)',
        'loom_runtime_program_bind_service(control->program, 1, &step)'):
        if count_sequence(public, tokens(value)) != 1:
            raise ValueError('unfamiliar public runner binding linkage')
    return {'profile': profile, 'argument_types': PROFILE_TYPES[profile], 'result_types': []}


def _identity(snapshot):
    source = snapshot['source']
    if source.get('repository') != REPOSITORY or not re.fullmatch('[0-9a-f]{40}', source.get('tested_sha', '')):
        raise ValueError('runner evidence requires an official pinned SDK source')
    value = {'repository': REPOSITORY, 'tested_sha': source['tested_sha'],
             'wheel_sha256': snapshot['wheel']['sha256'], 'runtime_identity_sha256': snapshot['runtime_identity_sha256']}
    if any(not isinstance(value[key], str) or not re.fullmatch('[0-9a-f]{64}', value[key]) for key in ('wheel_sha256', 'runtime_identity_sha256')):
        raise ValueError('runner evidence requires concrete SDK digests')
    return value


def stage(root, snapshot, client):
    """Acquire once into per-attempt policy storage; unavailable is explicit."""
    root = Path(root)
    receipt = {'schema_version': 1, **_identity(snapshot), 'status': 'unavailable', 'profile': None,
               'argument_types': [], 'result_types': [], 'reason': None, 'sources': []}
    data = []
    for index, path in enumerate(PATHS):
        try:
            value = client.json('/repos/' + REPOSITORY + '/contents/' + path + '?ref=' + receipt['tested_sha'])
            if not isinstance(value, dict):
                raise ValueError('native source response is not an object')
            content = value.get('content')
            if (value.get('type') != 'file' or value.get('path') != path or value.get('encoding') != 'base64'
                    or not isinstance(content, str) or len(content) > 2 * MAX_SOURCE_BYTES):
                raise ValueError('malformed bounded source response')
            raw = base64.b64decode(''.join(content.split()), validate=True)
            if len(raw) > MAX_SOURCE_BYTES:
                raise ValueError('native source exceeds its bound')
        except (ValueError, OSError, KeyError, TypeError) as error:
            receipt['reason'] = 'Pinned native runner source unavailable (' + type(error).__name__ + ')'
            break
        relative = DIRECTORY + '/' + ('module.c.txt', 'control.c.txt')[index]
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        receipt['sources'].append({'upstream_path': path, 'path': relative, 'size_bytes': len(raw), 'sha256': sha(raw)})
        data.append(raw)
    if len(data) == 2:
        try:
            receipt.update(recognize(*data), status='recognized', reason=None)
        except (ValueError, UnicodeError) as error:
            receipt.update(status='unsupported', reason=str(error)[:1024])
    raw = encoded(receipt)
    path = DIRECTORY + '/receipt.json'
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_bytes(raw)
    return {'receipt': receipt, 'path': path, 'sha256': sha(raw)}


def validate_descriptor(descriptor, snapshot):
    try:
        if set(descriptor) != {'receipt', 'path', 'sha256'} or descriptor['path'] != DIRECTORY + '/receipt.json':
            raise ValueError('invalid runner ABI evidence descriptor')
        receipt = descriptor['receipt']
        if set(receipt) != {'schema_version', 'repository', 'tested_sha', 'wheel_sha256', 'runtime_identity_sha256',
                            'status', 'profile', 'argument_types', 'result_types', 'reason', 'sources'}:
            raise ValueError('invalid runner ABI receipt')
        if type(receipt['schema_version']) is not int or receipt['schema_version'] != 1 or any(receipt[k] != v for k, v in _identity(snapshot).items()):
            raise ValueError('runner ABI evidence does not match selected SDK')
        if descriptor['sha256'] != sha(encoded(receipt)):
            raise ValueError('runner ABI receipt digest changed')
        if receipt['status'] == 'recognized':
            types = PROFILE_TYPES.get(receipt['profile'])
            if receipt['profile'] not in PROFILES or receipt['argument_types'] != types or receipt['result_types'] != [] or receipt['reason'] is not None:
                raise ValueError('invalid recognized runner ABI profile')
            required = 2
        elif receipt['status'] in ('unavailable', 'unsupported'):
            if (receipt['profile'] is not None or receipt['argument_types'] != [] or receipt['result_types'] != []
                    or not isinstance(receipt['reason'], str) or not receipt['reason'] or len(receipt['reason']) > 1024):
                raise ValueError('invalid unknown runner ABI diagnostic')
            required = 2 if receipt['status'] == 'unsupported' else None
        else:
            raise ValueError('invalid runner ABI recognition status')
        sources = receipt['sources']
        if not isinstance(sources, list) or len(sources) > 2 or (required is not None and len(sources) != required) or (receipt['status'] == 'unavailable' and len(sources) == 2):
            raise ValueError('invalid runner ABI source closure')
        for index, item in enumerate(sources):
            if (set(item) != {'upstream_path', 'path', 'size_bytes', 'sha256'} or item['upstream_path'] != PATHS[index]
                    or item['path'] != DIRECTORY + '/' + ('module.c.txt', 'control.c.txt')[index]
                    or type(item['size_bytes']) is not int or not 0 <= item['size_bytes'] <= MAX_SOURCE_BYTES
                    or not re.fullmatch('[0-9a-f]{64}', item['sha256'])):
                raise ValueError('invalid runner ABI source record')
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError('malformed runner ABI evidence') from error
    return copy.deepcopy(descriptor)


def files(descriptor):
    return [{'kind': 'runner_abi', 'path': descriptor['path'], 'sha256': descriptor['sha256']},
            *[{'kind': 'runner_abi_source', 'path': x['path'], 'sha256': x['sha256']} for x in descriptor['receipt']['sources']]]


def _read(root, relative, limit):
    path = Path(root)
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError('runner ABI evidence must not be a symlink')
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError('runner ABI evidence missing or oversized')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('runner ABI evidence exceeds its bound')
    return raw


def validate(root, descriptor, snapshot):
    descriptor = validate_descriptor(descriptor, snapshot)
    if _read(root, descriptor['path'], 64 * 1024) != encoded(descriptor['receipt']):
        raise ValueError('runner ABI receipt bytes changed')
    receipt, data = descriptor['receipt'], []
    for item in receipt['sources']:
        raw = _read(root, item['path'], MAX_SOURCE_BYTES)
        if len(raw) != item['size_bytes'] or sha(raw) != item['sha256']:
            raise ValueError('runner ABI source bytes changed')
        data.append(raw)
    if len(data) == 2:
        try:
            result = recognize(*data)
        except (ValueError, UnicodeError):
            if receipt['status'] != 'unsupported':
                raise ValueError('frozen native runner contract changed') from None
        else:
            if receipt['status'] != 'recognized' or any(receipt[k] != v for k, v in result.items()):
                raise ValueError('frozen native runner profile changed')
    return descriptor
