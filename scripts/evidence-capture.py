#!/usr/bin/env python3
"""Bounded saved evidence capture; never traverse a model payload tree."""
import hashlib
import json
from pathlib import Path

MAX_LOG = 4 * 1024 * 1024
MAX_SOURCE = 2 * 1024 * 1024
MAX_SOURCES = 16 * 1024 * 1024


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')


def safe_file(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts: raise ValueError('unsafe capture path')
    source = root
    for part in relative.parts:
        source = source / part
        if source.is_symlink(): raise ValueError('capture source is a symlink')
    if not source.resolve().is_relative_to(root.resolve()): raise ValueError('capture escapes root')
    if not source.is_file(): raise ValueError('capture source is not a regular file')
    return source


def safe_destination(root, relative):
    relative = Path(relative)
    if root.is_symlink() or relative.is_absolute() or '..' in relative.parts:
        raise ValueError('unsafe capture destination')
    target = root
    for part in relative.parts:
        target = target / part
        if target.is_symlink(): raise ValueError('capture destination is a symlink')
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('capture destination escapes root')
    return target


def bounded_copy(source, target, limit=MAX_LOG):
    size = source.stat().st_size
    with source.open('rb') as stream:
        if size <= limit:
            data, ranges = stream.read(), [[0, size]]
        else:
            head = stream.read(limit // 2 - 64)
            stream.seek(size - limit // 2)
            data = head + b'\n[... omitted; bounded log head/tail ...]\n' + stream.read(limit // 2)
            ranges = [[0, len(head)], [size-limit//2, size]]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    metadata = {'original_size': size, 'retained_size': len(data), 'truncated': size > limit, 'captured_ranges': ranges}
    write_json(target.with_name(target.name + '.capture.json'), metadata)
    return metadata


def drain_log(pipe, path, limit=MAX_LOG):
    """Drain through EOF while retaining bounded live head and final tail."""
    head, tail, total = bytearray(), bytearray(), 0
    marker = b'\n[... omitted; bounded log head/tail ...]\n'
    head_limit, tail_limit = limit // 2 - len(marker), limit - limit // 2
    with path.open('wb') as stream:
        while chunk := pipe.read1(8192):
            total += len(chunk)
            if len(head) < head_limit: head.extend(chunk[:head_limit-len(head)])
            tail.extend(chunk)
            if len(tail) > tail_limit: del tail[:-tail_limit]
            if stream.tell() < limit:
                stream.write(chunk[:limit-stream.tell()]); stream.flush()
        if total > limit:
            stream.seek(0); stream.truncate()
            stream.write(head + marker + tail)
        stream.flush()
    write_json(path.with_name(path.name + '.capture.json'), {'original_size': total,
        'retained_size': path.stat().st_size, 'truncated': total > limit,
        'captured_ranges': [[0, len(head)], [total-len(tail), total]] if total > limit else [[0, total]]})


def preparation_snapshot(prepared, suite):
    """Capture compact diagnostics in prepared/failure-evidence for publication."""
    output = prepared / 'failure-evidence'
    references = ['prepared-suite.json', 'inventory.json', 'image-pull.log', 'sdk-acquire.log', 'batch-online.log', 'batch-offline.log',
                  'actions-selection.json', 'resolved-runtime.json', 'target-environment.json',
                  'actions-qualification.log', 'sdk-download/sdk-error.json', 'sdk-download/sdk-acquisition.json']
    references += [name + '.metadata.json' for name in references if name.endswith('.log')]
    for entry in suite.get('entries', []): references.extend(entry.get('evidence', []))
    errors, retained = [], 0
    for relative in dict.fromkeys(references):
        try:
            source = safe_file(prepared, relative)
            if source.suffix not in ('.json', '.log', '.txt'): continue
            size = source.stat().st_size
            if retained + min(size, MAX_LOG) > 128 * 1024 * 1024:
                errors.append(f'{relative}: aggregate capture limit'); continue
            retained += min(size, MAX_LOG)
            target = safe_destination(prepared, Path('failure-evidence') / relative)
            if source.suffix == '.log': bounded_copy(source, target)
            elif source.stat().st_size <= MAX_LOG:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
            else: errors.append(f'{relative}: exceeds capture limit')
        except (ValueError, OSError) as error:
            if (prepared / relative).exists(): errors.append(f'{relative}: {error}')
    write_json(safe_destination(prepared, 'failure-evidence/capture.json'), {'errors': errors})


def failed_sources(prepared, output, suite, result):
    remaining, file_count = 64 * 1024 * 1024, 0
    for entry, model in zip(suite.get('entries', []), result.get('models', [])):
        if not any(cycle.get(key, {}).get('status') == 'failed' for cycle in model.get('cycles', []) for key in ('load', 'readiness')):
            continue
        directory = output / 'models' / entry['id'] / 'pipeline'
        metadata = {'files': [], 'errors': []}
        total = 0
        try:
            manifest = json.loads(safe_file(prepared, entry['inputs_manifest']).read_text())
            for program in manifest.get('programs', []):
                name = program['path']
                if Path(name).suffix != '.loom': continue
                source = safe_file(prepared / 'models' / entry['id'] / 'model', name)
                size = source.stat().st_size
                identity = {'path': name, 'sha256': program['sha256'], 'size': size}
                if size > MAX_SOURCE or total + size > MAX_SOURCES or size > remaining or file_count >= 2048:
                    metadata['files'].append({**identity, 'omitted': 'source capture limit'})
                    continue
                with source.open('rb') as stream: observed = hashlib.file_digest(stream, 'sha256').hexdigest()
                if observed != program['sha256']: raise ValueError(f'program digest changed: {name}')
                target = safe_destination(output, Path('models') / entry['id'] / 'pipeline' / name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
                total += size
                remaining -= size
                file_count += 1
                metadata['files'].append(identity)
        except (KeyError, TypeError, ValueError, OSError) as error:
            metadata['errors'].append(str(error))
        write_json(safe_destination(output, Path('models') / entry['id'] / 'pipeline/capture.json'), metadata)
        model['evidence'].append(f"models/{entry['id']}/pipeline/capture.json")
