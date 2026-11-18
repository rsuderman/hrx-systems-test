#!/usr/bin/env python3
"""Adapt source publication ABI13 to ABI14, independently of SDK package version.

Read-only plan: python SCRIPT SOURCE
Write a new derivative: python SCRIPT SOURCE --output DESTINATION

Supports the audited cached plain publishers and single-executed-epoch speculative
publishers. Captures actual device candidates before rejection, EOS and output
credit, extends verifier feedback and preserves non-speculative zero writes.
Unknown publishers, bytecode-only trees and actual two-epoch sources require
source review/rebuilding; continuation-owner aggregation is not inferred.
Never modifies inputs or copies checkpoint payloads; link external assets first.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

MIGRATION_ID = 'hrx-pipelines-publication-13-to-14'
IMPLEMENTATION_REVISION = 1
SYMBOLS = ('publish_epoch', 'plain_publish_epoch', 'text_publish_epoch', 'text_publish_epoch_v1', 'sequence_publish_epoch_v1')
PLAIN_HASHES = {'9e248d1c91fc0db00735201e32864bbb0cbd2df03cd210090d69a8d6bdfc6756', '6e8c5d58223caf7fa146b79210b97d81f65413890580b8cede84eea50b3fe04e'}
SPEC_HASHES = {'d2c1501c5d45df390a47123fc6ac5350a8c47b6d6a6f438c1899b3f7a65111a5', '29a48fd66d8fa789b9390178531eca323d9309d2efe292ac4f2fccd89a71b9ed', 'a2eb8c7b5564bf46c63f4ec4a97751d9b0bf5ed9622f2cb8b313939d0a49d716', 'e99a0c52ed6ee311d2aa6d0105c825be2458b89202b3ab4de24387232686c844'}
KERNEL_HASHES = {'64a9b7ba913ce10d139138b12cab9b9f683710ddf700e6724705dbf96268e408', '8ca3b6e44486f1fc8a608f891c118825286de1c2d8329491539130446d6fe150'}
EPOCH_HASHES = {'66896083d79bfb334dea040671a390bbff0efd49d33b17d1649734520e7f185a', 'c872d7d024efecd65fdcfa4fd01e8f0e000c3c8ae5ce66a911ab28455fde1034', '7d16e0f94141c403310e6212735559821d1b81f2550e61eef87d4356b0baaf7f', '28da79cccdf0cdf1d87dfd896b5677d0f6889e53a19c3b840e180d62faf08957', 'af11d0c8581f35200ac802aefaaf0243b180ee8f225218c590e45b56e07e240f', '3594be70316c7706a9c55873f6826a4883dec02fb6a3a894ee9d0b7c70499641', '9c118116b12ddcaa7259082afdf7dd8445fbb8b3b58643aa15f24ff49e065c3e', '8c13f023693c5d0c5f1704f6c50d70c2304b1383e7edc9376d23b346fca86833', '540f79f83144e028960ce0db58d5e771ac2ecf57d3841f1d4c988d2eff0ccd97', 'd911ae2f32286b9f5269a00adcec232bbc89b64e445db9de3deaf06242711118', '69b66d6dd6fd33cbe4f4c0ac6e9df16e6326daa0ce14e9613d71415b4b5e7497'}
WEIGHTS = {'.gguf', '.safetensors', '.bin', '.pt', '.pth'}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def identity(name, data):
    return {'path': name, 'size_bytes': len(data), 'sha256': sha(data)}


def balanced_end(text, start):
    depth = 1
    for i in range(start + 1, len(text)):
        depth += (text[i] == '{') - (text[i] == '}')
        if depth == 0:
            return i + 1
    raise ValueError('unbalanced source definition')


def functions(text, kind='func'):
    for match in re.finditer(r'^' + kind + r'\.def[^\n]*?@([\w.]+)\(', text, re.M):
        if match.group(1) not in (*SYMBOLS, 'epoch', 'g_spec_accept'):
            continue
        end = balanced_end(text, text.index('{', match.start()))
        if kind == 'kernel':
            end = balanced_end(text, text.index('{', end))
        yield match.group(1), match.start(), end, text[match.start():end]


def body_hash(block):
    return sha(block[block.index('{'):].encode())


def once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('unfamiliar proposal feedback closure: ' + old)
    return text.replace(old, new)


def plain_body(block):
    return once(block, '  scf.for %i = [%c0 to %length step %c1] {',
        '  %fixup_proposals = buffer.view %proposal_counts[%origin] : buffer -> view<[%length]xi32>\n'
        '  scf.for %i = [%c0 to %length step %c1] {\n'
        '    view.store %c0_i32, %fixup_proposals[%i] : i32, view<[%length]xi32>')


def speculative_body(block):
    block = block.replace('x10xi32>', 'x11xi32>')
    return once(block, '    scf.for %i = [%c0 to %length step %c1] {',
        '    %fixup_proposals = buffer.view %proposal_counts[%origin] : buffer -> view<[%length]xi32>\n'
        '    scf.for %i = [%c0 to %length step %c1] {\n'
        '      %fixup_actual = view.load %records[%i, 10] : view<[%length]x11xi32> -> i32\n'
        '      view.store %fixup_actual, %fixup_proposals[%i] : i32, view<[%length]xi32>')


def kernel_body(block):
    block = block.replace('x10xi32>', 'x11xi32>')
    # The actual device SPEC flag handles gated anchor-only and lookup work.
    # Capture before acceptance, EOS and credit; those do not change proposals.
    return once(block, '    %spec = scalar.cmpi ne, %spec_bits, %c0_i32 : i32',
        '    %spec = scalar.cmpi ne, %spec_bits, %c0_i32 : i32\n'
        '    %fixup_after_anchor = scalar.subi %length32, %c1_i32 : i32\n'
        '    %fixup_nonnegative = scalar.maxsi %fixup_after_anchor, %c0_i32 : i32\n'
        '    %fixup_candidates = scf.select %spec, %fixup_nonnegative, %c0_i32 : i32\n'
        '    view.store %fixup_candidates, %out[%sb, 10] : i32, view<[%spans]x11xi32>')


def signatures(text):
    names = '|'.join(SYMBOLS)
    pattern = re.compile(r'(?P<head>(?:func\.(?:def(?: public)?|decl)|func\.call)\s+@(?:' + names + r')\()(?P<args>[^()\n]*)(?P<tail>\)(?: : \([^()\n]*\))?)')
    def update(m):
        args, tail = m['args'], m['tail']
        if '%proposal_counts' in args:
            return m.group(0)
        if m['head'].startswith('func.call'):
            if not args.endswith('%progress') or not tail.endswith('buffer)'):
                raise ValueError('unfamiliar publication forwarding call')
            return m['head'] + args + ', %proposal_counts' + tail[:-1] + ', buffer)'
        if not args.endswith('%progress: buffer'):
            raise ValueError('unfamiliar publication signature')
        return m['head'] + args + ', %proposal_counts: buffer' + tail
    return pattern.sub(update, text)


def reverse(text):
    text = text.replace(', %proposal_counts: buffer', '').replace(', %proposal_counts)', ')')
    text = re.sub(r'(func.call @(?:' + '|'.join(SYMBOLS) + r')\([^\n]*\) : \([^\n]*), buffer\)', r'\1)', text)
    text = re.sub(r'^.*%fixup_(?:proposals|actual|after_anchor|nonnegative|candidates).*\n', '', text, flags=re.M)
    text = text.replace('x11xi32>', 'x10xi32>')
    for new, old in CONSTANTS:
        text = text.replace(new, old)
    return text


CONSTANTS = [('%result_stride = scalar.constant 92 : i64', '%result_stride = scalar.constant 84 : i64'),
             ('%first_bank_stride = scalar.constant 44 : i64', '%first_bank_stride = scalar.constant 40 : i64'),
             ('%record = scalar.constant 44 : i64', '%record = scalar.constant 40 : i64')]


def transform(programs):
    definitions = [(p, n, s, e, b) for p, t in programs.items() for n, s, e, b in functions(t)]
    providers = [f for f in definitions if f[1] == 'publish_epoch']
    if not providers:
        if any('publish_epoch' in t for t in programs.values()):
            raise ValueError('unfamiliar publisher form; source rebuild/review required')
        return {}, {'publishers': 0, 'proposal_feedback': 0}
    if len(providers) != 1:
        raise ValueError('multiple publication providers')
    provider = providers[0]
    params = provider[4].split(') {', 1)[0].split('(', 1)[1]
    arity = len(params.split(','))
    if arity not in (13, 14):
        raise ValueError('requires control-state ABI13 source; run legacy migration first')
    if arity == 14:
        original = {p: reverse(t) for p, t in programs.items()}
        changes, counts = transform(original)
        if {p: changes.get(p, t) for p, t in original.items()} != programs:
            raise ValueError('unverified proposal-count ABI14 implementation')
        return {}, counts
    expected_tail = '%plan: buffer, %media_plan: buffer, %count: i32, %first_media_count: i32, %next_media_count: i32, %verified: i32, %continues: i32, %next_speculative: buffer, %ordinary: buffer, %first_results: buffer, %next_results: buffer, %progress: buffer'
    if params not in tuple(state + ': buffer, ' + expected_tail for state in ('%control_state', '%fixup_epoch_control_state')):
        raise ValueError('unfamiliar publication provider signature')
    values = ', '.join(arg.split(':')[0] for arg in params.split(', '))
    types = ', '.join(arg.split(': ')[1] for arg in params.split(', '))
    expected_decl = 'func.decl @publish_epoch(' + params + ')'
    if sum(t.count(expected_decl) for t in programs.values()) != 1:
        raise ValueError('publication declaration does not match provider')
    for alias in SYMBOLS[2:]:
        expected_alias = ('func.def public @' + alias + '(' + params + ') {\n'
                          '  func.call @publish_epoch(' + values + ') : (' + types + ')\n'
                          '  func.return\n}')
        if sum(t.count(expected_alias) for t in programs.values()) != 1:
            raise ValueError('unfamiliar publication forwarding export')
    speculative = body_hash(provider[4]) in SPEC_HASHES
    if not speculative and body_hash(provider[4]) not in PLAIN_HASHES:
        raise ValueError('unsupported publisher body; actual counts require source review')
    # Every ABI forwarding reference belongs to the known complete closure.
    expected = {'publish_epoch': 5, 'text_publish_epoch': 1, 'text_publish_epoch_v1': 1, 'sequence_publish_epoch_v1': 1}
    for name, count in expected.items():
        if sum(len(re.findall(r'@' + name + r'\(', t)) for t in programs.values()) != count:
            raise ValueError('partial or unfamiliar publication forwarding closure')
    plain = [f for f in definitions if f[1] == 'plain_publish_epoch']
    if len(plain) != int(speculative):
        raise ValueError('incomplete speculative/plain publication provider closure')
    edits = {}
    for p, n, s, e, b in definitions:
        if n in ('publish_epoch', 'plain_publish_epoch'):
            allowed = SPEC_HASHES if n == 'publish_epoch' and speculative else PLAIN_HASHES
            if body_hash(b) not in allowed:
                raise ValueError('unsupported plain/speculative publisher body')
            new = speculative_body(b) if n == 'publish_epoch' and speculative else plain_body(b)
            edits.setdefault(p, []).append((s, e, new))
    if speculative:
        epochs = [f for f in definitions if f[1] == 'epoch']
        if len(epochs) != 1 or body_hash(epochs[0][4]) not in EPOCH_HASHES or any(re.search(r'%' + arg + r'\b', epochs[0][4].split('{', 1)[1])
                for arg in ('epoch_count', 'continuation_shape', 'continuation_proposes', 'next_results')):
            raise ValueError('unsupported completed continuation; owner-tag aggregation requires review')
        kernels = [(p, s, e, b) for p, t in programs.items() for n, s, e, b in functions(t, 'kernel') if n == 'g_spec_accept']
        if len(kernels) != 1 or body_hash(kernels[0][3]) not in KERNEL_HASHES:
            raise ValueError('unsupported verifier feedback producer')
        p, s, e, b = kernels[0]
        edits.setdefault(p, []).append((s, e, kernel_body(b)))
    result = {}
    for p, t in programs.items():
        for s, e, new in sorted(edits.get(p, []), reverse=True):
            t = t[:s] + new + t[e:]
        result[p] = signatures(t)
    if speculative:
        # SSA-labelled allocation/readback constants, never unrelated40-byte rows.
        for new, old in CONSTANTS:
            owners = [p for p, t in result.items() if old in t]
            if len(owners) != 1:
                raise ValueError('unsupported feedback allocation/readback closure: ' + old)
            p = owners[0]
            result[p] = once(result[p], old, new)
        if any('x10xi32>' in t for t in result.values()):
            raise ValueError('unreviewed ten-word feedback consumer')
    return {p: t for p, t in result.items() if t != programs[p]}, {'publishers': 1, 'proposal_feedback': int(speculative)}


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
    if card.get('programs'):
        for name, text in programs.items():
            if 'func.def public @publish_epoch(' in text and name not in declared_paths:
                raise ValueError('publisher source is outside declared source program closure')
    if not programs:
        raise ValueError('bytecode-only publications require rebuilding from source')
    if not any('func.def public @publish_epoch(' in t for t in programs.values()):
        kinds = {v.get('kind') for v in card.get('services', {}).values() if isinstance(v, dict)}
        if kinds != {'decision'}:
            raise ValueError('missing publication source provider; bytecode-only publications require rebuilding')
    updates, counts = transform(programs)
    changes = {p: t.encode() for p, t in updates.items()}
    report = {'schema_version': 1, 'migration_id': MIGRATION_ID, 'implementation_revision': IMPLEMENTATION_REVISION,
              'implementation_sha256': sha(Path(__file__).read_bytes()), 'source': str(source),
              'source_contract': 'control-state-13', 'target_contract': 'proposal-counts-14',
              'status': 'applied' if changes else 'unchanged', 'publication_ready': False, 'locks_modified': False,
              'operation_counts': counts, 'source_files': [identity(p, b) for p, b in sources.items()],
              'output_files': [identity(p, changes.get(p, b)) for p, b in sources.items()],
              'files': [{'path': p, 'kind': 'proposal_counts'} for p in changes]}
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
