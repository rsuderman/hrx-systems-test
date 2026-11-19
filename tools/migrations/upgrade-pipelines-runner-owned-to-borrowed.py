#!/usr/bin/env python3
"""Adapt closed, forwarding HAL-buffer inputs to the borrowed runner contract.

This recipe targets source-verified native borrowed handles, independently of
package versions. Only invocation arguments and their forwarding closure are
supported. HAL producers, returns, storage, foreign consumers and mixed ownership
require source review. Inputs and publication locks remain immutable.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

MIGRATION_ID = 'hrx-pipelines-runner-owned-to-borrowed'
IMPLEMENTATION_REVISION = 1
WEIGHTS = {'.gguf', '.safetensors', '.bin', '.pt', '.pth'}
ALIAS = 'type.decl import("hal", "buffer") @HalBuffer'
HAL = 'hal.buffer'
BORROWED = 'ptr<@HalBuffer>'
PUBLIC = {'step', 'epoch', 'text_step', 'text_step_v1', 'text_epoch',
          'text_epoch_v1', 'sequence_epoch_v1', 'text_image_run_v1',
          'decision_run_v1', 'run'}
VALUE = r'%[A-Za-z_][A-Za-z_0-9]*'
FUNC = re.compile(r'^func\.(decl|def)(.*?)@([\w.]+)\(([^()]*)\)(.*)$')
CALL = re.compile(r'^(?:(?:' + VALUE + r')(?:,\s*' + VALUE + r')* = )?func\.call @([\w.]+)\(([^()]*)\) : \(([^()]*)\)(.*)$')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def identity(name, data):
    return {'path': name, 'size_bytes': len(data), 'sha256': sha(data)}


def clean(text, mask_strings=True):
    # Do not interpret comments or string literals as executable type uses.
    return re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"',
                  lambda m: m.group() if not mask_strings and m.group().startswith(chr(34))
                  else re.sub(r'[^\n]', ' ', m.group()), text)


def parts(text):
    return [x.strip() for x in text.split(',')] if text.strip() else []


def transform(programs):
    """Validate the complete HAL forwarding graph before changing any bytes."""
    legacy = any(re.search(r'\bhal\.buffer\b', clean(t)) for t in programs.values())
    borrowed = any(BORROWED in clean(t) for t in programs.values())
    if legacy and borrowed:
        raise ValueError('mixed HAL ownership requires review')
    normalized = {}
    signatures = {}
    declarations = []
    for path, text in programs.items():
        if 'hal.fence' in clean(text):
            raise ValueError('HAL fence producers and consumers require review')
        if HAL not in clean(text) and BORROWED not in clean(text):
            continue
        if borrowed:
            if text.splitlines().count(ALIAS) != 1:
                raise ValueError('missing canonical HAL buffer identity')
            text = text.replace(ALIAS + '\n', '', 1).replace(BORROWED, HAL)
            if re.search(r'@HalBuffer\b', clean(text)):
                raise ValueError('non-borrowed canonical HAL identity requires review')
        elif re.search(r'@HalBuffer\b', clean(text)):
            raise ValueError('HalBuffer symbol conflicts with canonical imported identity')
        normalized[path] = text
        for line in clean(text, mask_strings=False).splitlines():
            line = line.strip()
            match = FUNC.match(line)
            if not match:
                continue
            kind, prefix, name, arguments, suffix = match.groups()
            ending = clean(suffix).strip()
            pattern = r'(?:-> \([^()]*\))?\s*' + (r'\{' if kind == 'def' else '')
            if not re.fullmatch(pattern, ending):
                raise ValueError('unfamiliar function signature or inline body')
            args = parts(arguments)
            types = []
            for arg in args:
                param = re.fullmatch('(' + VALUE + r'):\s*(.+)', arg)
                if not param:
                    raise ValueError('unfamiliar HAL module function signature')
                if HAL in param[2] and param[2] != HAL:
                    raise ValueError('nested HAL ownership type requires review')
                types.append(param[2])
            if HAL in suffix:
                raise ValueError('HAL producer or function result requires review')
            signature = tuple(types)
            if name in signatures and signatures[name] != signature:
                raise ValueError('inconsistent HAL forwarding declarations')
            signatures[name] = signature
            if HAL in signature:
                if kind == 'def' and 'public' in prefix and name not in PUBLIC:
                    raise ValueError('unreviewed public HAL entrypoint')
                if 'import' in prefix:
                    declarations.append((line, name, signature, suffix))
    if not normalized:
        return {}, {'hal_input_files': 0, 'hal_input_types': 0}
    runners = 0
    for line, name, signature, suffix in declarations:
        if not re.match(r'^func\.decl import\("runner"\) @', line):
            raise ValueError('foreign HAL consumer requires review')
        execute = re.fullmatch(r'execute_([0-9]+)', name)
        if execute:
            arity = int(execute[1])
            valid = 1 <= arity <= 64 and signature == ('i32',) + (HAL,) * arity
            runners += 1
        else:
            valid = name == 'feedback' and signature == ('i32', HAL, 'i64', 'i64')
        if not valid or suffix.strip() != '-> (i64)':
            raise ValueError('unfamiliar native runner declaration')
    if not runners:
        raise ValueError('HAL forwarding graph lacks runner consumer')
    for path, text in normalized.items():
        active = set()
        depth = 0
        branches = []
        for original in clean(text).splitlines():
            line = original.strip()
            match = FUNC.match(line)
            if match:
                active = {arg.split(':')[0].strip() for arg in parts(match[4]) if arg.split(':', 1)[1].strip() == HAL}
                depth = line.count('{') - line.count('}') if match[1] == 'def' else 0
                continue
            call = CALL.search(line)
            conditional = re.fullmatch('(' + VALUE + r') = scf\.if ' + VALUE + r' -> \(hal\.buffer\) \{', line)
            forwarded = re.fullmatch('scf.yield (' + VALUE + r') : hal\.buffer', line)
            uses = set(re.findall(VALUE, line)) & active
            if conditional:
                if uses:
                    raise ValueError('HAL handle cannot be a branch condition')
                branches.append({'depth': depth, 'result': conditional[1], 'yields': [0]})
            elif call and (HAL in call[3] or uses):
                name, arguments, types, results = call.groups()
                argtypes, values = tuple(parts(types)), parts(arguments)
                if (not re.fullmatch(r'(?:\s*-> \([^()]*\))?\s*', results)
                        or HAL in results or name not in signatures or signatures[name] != argtypes
                        or len(values) != len(argtypes)
                        or any((value in active) != (typ == HAL) for value, typ in zip(values, argtypes))):
                    raise ValueError('unclosed or escaping HAL forwarding call')
            elif forwarded:
                if (forwarded[1] not in active or not branches
                        or depth != branches[-1]['depth'] + 1):
                    raise ValueError('HAL yield lacks invocation-local borrowed root')
                branches[-1]['yields'][-1] += 1
            elif HAL in line or uses:
                raise ValueError('unreviewed HAL ownership operation: ' + line[:120])
            if branches and line == '} else {' and depth == branches[-1]['depth'] + 1:
                branches[-1]['yields'].append(0)
            depth += line.count('{') - line.count('}')
            if branches and depth == branches[-1]['depth']:
                branch = branches.pop()
                if branch['yields'] != [1, 1]:
                    raise ValueError('HAL conditional requires two borrowed forwarding yields')
                active.add(branch['result'])
            if depth <= 0:
                active = set()
    changes = {}
    count = 0
    if legacy:
        for path, text in normalized.items():
            # Executable occurrences were fully audited above. Preserve comments
            # and strings verbatim by replacing only in the lexical code spans.
            code = clean(text)
            positions = list(re.finditer(r'\bhal\.buffer\b', code))
            for match in reversed(positions):
                text = text[:match.start()] + BORROWED + text[match.end():]
            count += len(positions)
            changes[path] = ALIAS + '\n\n' + text
    return changes, {'hal_input_files': len(changes), 'hal_input_types': count}


def plan(source):
    source = Path(source).resolve(strict=True)
    if not (source / 'model.json').is_file() or (source / 'model.json').is_symlink():
        raise ValueError('requires regular model.json and source; bytecode-only publications require rebuilding')
    card = json.loads((source / 'model.json').read_text())
    if card.get('programs') and not card['programs'].get('source'):
        raise ValueError('bytecode-only publications require rebuilding from source')
    declared_paths = set()
    def check_paths(value):
        if isinstance(value, dict):
            if isinstance(value.get('path'), str):
                relative = Path(value['path'])
                if relative.is_absolute() or '..' in relative.parts:
                    raise ValueError('program paths must remain inside source tree')
                declared_paths.add(str(relative))
                candidate = source / relative
                if not candidate.exists():
                    raise ValueError('missing declared program source')
                if candidate.suffix == '.loom' and candidate.is_symlink():
                    raise ValueError('linked declared program source requires review')
                if any(parent.is_symlink() for parent in candidate.parents if parent != source and parent.is_relative_to(source)):
                    raise ValueError('linked program source directories require review')
            for child in value.values():
                check_paths(child)
        elif isinstance(value, list):
            for child in value:
                check_paths(child)
    check_paths(card.get('programs', {}))
    paths = sorted(source.rglob('*'))
    for p in paths:
        if p.suffix == '.loom' and (p.is_symlink() or not p.is_file()):
            raise ValueError('linked publication source requires review')
        if p.is_file() and not p.is_symlink() and p.suffix.lower() in WEIGHTS:
            raise ValueError('refusing to copy model weights; link immutable external assets')
        if p.is_symlink() and p.is_dir():
            raise ValueError('linked source directories require review')
    source_paths = sorted(set([source / 'model.json', *[p for p in paths if p.suffix in ('.loom', '.lock') and p.is_file() and not p.is_symlink()]]))
    sources = {str(p.relative_to(source)): p.read_bytes() for p in source_paths}
    programs = {p: b.decode() for p, b in sources.items() if p.endswith('.loom')}
    if not programs:
        raise ValueError('bytecode-only publications require rebuilding from source')
    updates, counts = transform(programs)
    changes = {p: t.encode() for p, t in updates.items()}
    report = {'schema_version': 1, 'migration_id': MIGRATION_ID, 'implementation_revision': IMPLEMENTATION_REVISION,
              'implementation_sha256': sha(Path(__file__).read_bytes()), 'source': str(source),
              'source_contract': 'owned-hal-inputs', 'target_contract': 'borrowed-hal-inputs',
              'status': 'applied' if changes else 'unchanged', 'publication_ready': False, 'locks_modified': False,
              'operation_counts': counts, 'source_files': [identity(p, b) for p, b in sources.items()],
              'output_files': [identity(p, changes.get(p, b)) for p, b in sources.items()],
              'changed_file_count': len(changes), 'files': [{'path': p, 'kind': 'borrowed_hal_inputs'} for p in changes]}
    return changes, report


def write_derivative(source, output):
    source, output = Path(source).resolve(strict=True), Path(output).absolute()
    changes, report = plan(source)
    receipt = output.with_name(output.name + '.upgrade.json')
    if any(p.exists() or p.is_symlink() for p in (output, receipt)):
        raise ValueError('output and receipt must not already exist')
    if output.resolve().is_relative_to(source) or source.is_relative_to(output.resolve()):
        raise ValueError('input and output must not overlap')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.publication-upgrade-', dir=output.parent))
    created_receipt = False
    try:
        tree = temporary / 'model'
        shutil.copytree(source, tree, symlinks=True)
        for item in report['source_files']:
            if sha((tree / item['path']).read_bytes()) != item['sha256']:
                raise ValueError('source changed during migration')
        for p, b in changes.items():
            (tree / p).write_bytes(b)
        if plan(tree)[0]:
            raise ValueError('adapter is not idempotent')
        output.mkdir()
        try:
            with receipt.open('x') as stream:
                created_receipt = True
                json.dump(report, stream, indent=2)
                stream.write('\n')
            os.replace(tree, output)
        except BaseException:
            if created_receipt:
                receipt.unlink(missing_ok=True)
            if not any(output.iterdir()):
                output.rmdir()
            raise
        return report, receipt
    finally:
        shutil.rmtree(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.output:
        report, _ = write_derivative(args.source, args.output)
    else:
        _, report = plan(args.source)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
