"""Bounded native publication ABI evidence for one pinned SDK source commit.

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
PATHS = ('loom/src/loom/serve/service/text/control.c', 'loom/src/loom/serve/service/text/model.c')
DIRECTORY = 'policies/publication-abi'
MAX_SOURCE_BYTES = 1024 * 1024
PROFILES = ('legacy-12', 'control-state-13')
LEGACY_TYPES = ['buffer', 'buffer', 'i32', 'i32', 'i32', 'i32', 'i32', 'buffer', 'buffer', 'buffer', 'buffer', 'buffer']
_TOKEN = re.compile(r'(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)|(?P<space>\s+)|(?P<string>"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')|[A-Za-z_][A-Za-z_0-9]*|[0-9]+|[^\s]')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def tokens(text):
    return [m.group() for m in _TOKEN.finditer(text) if m.lastgroup not in ('comment', 'space')]


def matching(items, start):
    pairs = {'(': ')', '{': '}', '[': ']'}
    stack = []
    for index in range(start, len(items)):
        item = items[index]
        if item in pairs:
            stack.append(pairs[item])
        elif item in pairs.values():
            if not stack or stack.pop() != item:
                raise ValueError('unbalanced native publication source')
            if not stack:
                return index
    raise ValueError('unterminated native publication source')


def function(text, name):
    items = tokens(text)
    found = []
    for index, item in enumerate(items[:-1]):
        if item != name or items[index + 1] != '(':
            continue
        close = matching(items, index + 1)
        if close + 1 < len(items) and items[close + 1] == '{':
            end = matching(items, close + 1)
            found.append(items[close + 2:end])
    if len(found) != 1:
        raise ValueError('native publication function anchor is missing or duplicated')
    return found[0]


def initializer(items, name, declaration):
    prefix = tokens(declaration + ' ' + name)
    found = []
    for index in range(len(items)):
        if items[index:index + len(prefix)] != prefix:
            continue
        pos = index + len(prefix)
        if items[pos:pos + 2] == ['[', ']']:
            pos += 2
        if items[pos:pos + 2] != ['=', '{']:
            continue
        end = matching(items, pos + 1)
        if items[end + 1:end + 2] != [';']:
            raise ValueError('unfamiliar native publication initializer')
        found.append(items[pos + 2:end])
    if len(found) != 1:
        raise ValueError('native publication initializer is missing or duplicated')
    return found[0]


def count_sequence(items, sequence):
    return sum(items[i:i + len(sequence)] == sequence for i in range(len(items)))


def _caller(state):
    borrowed = 'iree_vm_buffer_variant_from_ptr_borrowed(&model->source.vm_types, {})'
    values = ([borrowed.format('model->source.control_state')] if state else []) + [
        borrowed.format('model->control.host.buffers[TEXT_HOST_PLAN]'), borrowed.format('media_plan'),
        'iree_vm_variant_from_i32((int32_t)model->invocation.span_count)',
        'iree_vm_variant_from_i32((int32_t)media->first_feature_count)',
        'iree_vm_variant_from_i32((int32_t)media->next_feature_count)',
        'iree_vm_variant_from_i32(iree_any_bit_set(model->invocation.flags, TEXT_EPOCH_FLAG_VERIFY))',
        'iree_vm_variant_from_i32(iree_any_bit_set(model->invocation.flags, TEXT_EPOCH_FLAG_CONTINUATION))',
        *[borrowed.format('model->control.host.buffers[TEXT_HOST_' + name + ']') for name in
          ('CONTINUATIONS', 'OUTPUTS', 'RESULTS', 'NEXT_RESULTS', 'PROGRESS')]]
    return tokens(','.join(values) + ',')


def recognize(control, model):
    """Recognize two reviewed declaration AND caller shapes, not just arity."""
    if any(not isinstance(raw, bytes) or len(raw) > MAX_SOURCE_BYTES for raw in (control, model)):
        raise ValueError('native publication source exceeds its bound')
    declaration = function(control.decode('utf-8'), 'loom_serve_text_control_initialize')
    for name, expected in (
        ('buffer', '.kind = IREE_VM_SIGNATURE_TYPE_KIND_REF, .value.ref = source->vm_types.buffer'),
        ('i32', '.kind = IREE_VM_SIGNATURE_TYPE_KIND_SCALAR, .value.scalar = IREE_VM_SCALAR_TYPE_I32')):
        if initializer(declaration, name, 'const iree_vm_signature_type_t') != tokens(expected):
            raise ValueError('unfamiliar native publication type alias')
    args = initializer(declaration, 'publish_arguments', 'const iree_vm_signature_type_t')
    types = [t for t in args if t != ',']
    if args not in (tokens(','.join(LEGACY_TYPES)), tokens(','.join(['buffer', *LEGACY_TYPES]))):
        raise ValueError('unsupported native publication argument types/order')
    binding = tokens('{IREE_SVL("publish_epoch"), {publish_arguments, IREE_ARRAYSIZE(publish_arguments)}, {NULL, 0}, &control->publish_epoch}')
    if (count_sequence(declaration, binding) != 1 or
            count_sequence(declaration, tokens('IREE_SVL("publish_epoch")')) != 1):
        raise ValueError('unfamiliar native publication binding/results')
    caller = function(model.decode('utf-8'), 'loom_serve_text_model_complete_epoch')
    state = types == ['buffer', *LEGACY_TYPES]
    if initializer(caller, 'publish_arguments', 'iree_vm_variant_t') != _caller(state):
        raise ValueError('native publication declaration/caller disagree')
    invoke = tokens('loom_serve_text_invoke(model, model->control.publish_epoch, iree_vm_variant_span_from_array(publish_arguments), iree_vm_variant_span_empty())')
    if (count_sequence(caller, invoke) != 1 or
            count_sequence(caller, tokens('model->control.publish_epoch')) != 1):
        raise ValueError('unfamiliar native publication invocation/results')
    return {'profile': 'control-state-13' if state else 'legacy-12', 'argument_types': types, 'result_types': []}


def _identity(snapshot):
    source = snapshot['source']
    if source.get('repository') != REPOSITORY or not re.fullmatch('[0-9a-f]{40}', source.get('tested_sha', '')):
        raise ValueError('publication evidence requires an official pinned SDK source')
    value = {'repository': REPOSITORY, 'tested_sha': source['tested_sha'],
             'wheel_sha256': snapshot['wheel']['sha256'], 'runtime_identity_sha256': snapshot['runtime_identity_sha256']}
    if any(not isinstance(value[key], str) or not re.fullmatch('[0-9a-f]{64}', value[key]) for key in ('wheel_sha256', 'runtime_identity_sha256')):
        raise ValueError('publication evidence requires concrete SDK digests')
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
            receipt['reason'] = 'Pinned native publication source unavailable (' + type(error).__name__ + ')'
            break
        relative = DIRECTORY + '/' + ('control.c.txt', 'model.c.txt')[index]
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
            raise ValueError('invalid publication ABI evidence descriptor')
        receipt = descriptor['receipt']
        if set(receipt) != {'schema_version', 'repository', 'tested_sha', 'wheel_sha256', 'runtime_identity_sha256',
                            'status', 'profile', 'argument_types', 'result_types', 'reason', 'sources'}:
            raise ValueError('invalid publication ABI receipt')
        if type(receipt['schema_version']) is not int or receipt['schema_version'] != 1 or any(receipt[k] != v for k, v in _identity(snapshot).items()):
            raise ValueError('publication ABI evidence does not match selected SDK')
        if descriptor['sha256'] != sha(encoded(receipt)):
            raise ValueError('publication ABI receipt digest changed')
        if receipt['status'] == 'recognized':
            types = LEGACY_TYPES if receipt['profile'] == 'legacy-12' else ['buffer', *LEGACY_TYPES]
            if receipt['profile'] not in PROFILES or receipt['argument_types'] != types or receipt['result_types'] != [] or receipt['reason'] is not None:
                raise ValueError('invalid recognized publication ABI profile')
            required = 2
        elif receipt['status'] in ('unavailable', 'unsupported'):
            if (receipt['profile'] is not None or receipt['argument_types'] != [] or receipt['result_types'] != []
                    or not isinstance(receipt['reason'], str) or not receipt['reason'] or len(receipt['reason']) > 1024):
                raise ValueError('invalid unknown publication ABI diagnostic')
            required = 2 if receipt['status'] == 'unsupported' else None
        else:
            raise ValueError('invalid publication ABI recognition status')
        sources = receipt['sources']
        if not isinstance(sources, list) or len(sources) > 2 or (required is not None and len(sources) != required) or (receipt['status'] == 'unavailable' and len(sources) == 2):
            raise ValueError('invalid publication ABI source closure')
        for index, item in enumerate(sources):
            if (set(item) != {'upstream_path', 'path', 'size_bytes', 'sha256'} or item['upstream_path'] != PATHS[index]
                    or item['path'] != DIRECTORY + '/' + ('control.c.txt', 'model.c.txt')[index]
                    or type(item['size_bytes']) is not int or not 0 <= item['size_bytes'] <= MAX_SOURCE_BYTES
                    or not re.fullmatch('[0-9a-f]{64}', item['sha256'])):
                raise ValueError('invalid publication ABI source record')
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError('malformed publication ABI evidence') from error
    return copy.deepcopy(descriptor)


def files(descriptor):
    return [{'kind': 'publication_abi', 'path': descriptor['path'], 'sha256': descriptor['sha256']},
            *[{'kind': 'publication_abi_source', 'path': x['path'], 'sha256': x['sha256']} for x in descriptor['receipt']['sources']]]


def _read(root, relative, limit):
    path = Path(root)
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError('publication ABI evidence must not be a symlink')
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError('publication ABI evidence missing or oversized')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('publication ABI evidence exceeds its bound')
    return raw


def validate(root, descriptor, snapshot):
    descriptor = validate_descriptor(descriptor, snapshot)
    if _read(root, descriptor['path'], 64 * 1024) != encoded(descriptor['receipt']):
        raise ValueError('publication ABI receipt bytes changed')
    receipt, data = descriptor['receipt'], []
    for item in receipt['sources']:
        raw = _read(root, item['path'], MAX_SOURCE_BYTES)
        if len(raw) != item['size_bytes'] or sha(raw) != item['sha256']:
            raise ValueError('publication ABI source bytes changed')
        data.append(raw)
    if len(data) == 2:
        try:
            result = recognize(*data)
        except (ValueError, UnicodeError):
            if receipt['status'] != 'unsupported':
                raise ValueError('frozen native publication contract changed') from None
        else:
            if receipt['status'] != 'recognized' or any(receipt[k] != v for k, v in result.items()):
                raise ValueError('frozen native publication profile changed')
    return descriptor
