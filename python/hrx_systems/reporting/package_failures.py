#!/usr/bin/env python3
"""Build one bounded failure-evidence tree from saved attempt files."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import re
import sys
import zipfile

from . import failures as RECORDS
MAX_LOG = 4 * 1024 * 1024
MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
MAX_FILES = 4096
ALLOWED = {'.json', '.log', '.txt', '.loom'}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def package(attempt, output, archive=None):
    root, output = Path(attempt).resolve(), Path(output).absolute()
    if output.is_symlink() or output == root or not output.is_relative_to(root):
        raise ValueError('failure output must be a non-symlink directory inside the attempt')
    # Reject symlink parents before any write/removal.
    RECORDS.safe_file(root, output.relative_to(root).as_posix())
    if output.exists():
        if not (output / 'failures.json').is_file(): raise ValueError('refusing to replace an unrelated output directory')
        shutil.rmtree(output)
    output.mkdir(parents=True)
    snapshot = root / 'prepared/failure-evidence'
    if snapshot.is_dir() and not snapshot.is_symlink():
        for source in snapshot.rglob('*'):
            relative = source.relative_to(snapshot)
            if source.is_symlink(): raise ValueError('symlink preparation evidence')
            if source.is_file():
                checked = RECORDS.safe_file(root, source.relative_to(root).as_posix())
                if checked.suffix not in ALLOWED or checked.stat().st_size > (32 * 1024 * 1024 if checked.name == 'selection-snapshot.json' else MAX_LOG): continue
                target = RECORDS.safe_file(root, 'preparation-evidence/' + relative.as_posix())
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(checked, target)
    unsafe = False
    try:
        manifest = RECORDS.collect(root)
    except (ValueError, OSError, TypeError, KeyError) as error:
        unsafe = True
        manifest = {'schema_version': 1, 'attempt_id': root.name, 'counts': {'expected': None}, 'cases': [],
            'shared_evidence': [], 'evidence_errors': [f'Failure extraction failed: {error}']}
    total, count = 0, 0
    copied = {}

    def copy(reference, destination, attribution):
        nonlocal total, count, unsafe
        record = {'path': destination, 'attribution': attribution, 'original_size': None, 'retained_size': 0,
                  'truncated': False, 'missing': False}
        try:
            source = RECORDS.safe_file(root, reference)
            if source.suffix not in ALLOWED: raise ValueError('unexpected evidence file type')
            allowed_names = {'workflow.json', 'stages.json', 'suite-result.json', 'prepared-suite.json',
                'inventory.json', 'prepared-inputs.json', 'seed-manifest.json', 'request.json', 'response.json',
                'serve-result.json', 'pool-reclamation.json', 'capture.json', 'sdk.json',
                'actions-selection.json', 'resolved-runtime.json', 'target-environment.json',
                'sdk-error.json', 'sdk-acquisition.json', 'ci-plan.json', 'lane-selection.json',
                'runtime-policy.json', 'seccomp.json', 'pipeline-upgrade.json', 'publication-card.json',
                'publication-lock.json', 'selection-snapshot.json', 'runtime-selection.json',
                'pipeline-cache-entry.json', 'pipeline-cache-read.json'}
            if source.suffix == '.json' and source.name not in allowed_names and not re.fullmatch(r'(?:results/|preparation-evidence/)?config/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.json', reference) and not source.name.endswith(('.log.capture.json', '.log.metadata.json')):
                raise ValueError('unrecognized evidence JSON; assets/configuration are not copied')
            if source.suffix == '.txt' and source.name not in ('diagnostics.txt', 'error.txt'):
                raise ValueError('unrecognized text evidence')
            if source.suffix == '.loom' and '/pipeline/' not in reference:
                raise ValueError('pipeline source was not captured by the allowlisted producer')
            if not source.is_file():
                record.update(missing=True, reason='evidence not captured')
                return record
            size = source.stat().st_size
            record['original_size'] = size
            limit = 32 * 1024 * 1024 if source.name == 'selection-snapshot.json' else (MAX_LOG if source.suffix == '.log' or source.name == 'runtime-selection.json' else MAX_FILE)
            if count >= MAX_FILES or total + min(size, limit) > MAX_TOTAL:
                record.update(missing=True, reason='bundle limit reached')
                return record
            if source.suffix == '.loom' and size > limit:
                record.update(missing=True, reason='source too large; not truncated')
                return record
            target = output / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open('rb') as stream:
                if size > limit and source.suffix == '.log':
                    head = stream.read(limit // 2 - 64)
                    stream.seek(size - limit // 2)
                    data = head + b'\n[... omitted; bounded log head/tail ...]\n' + stream.read(limit // 2)
                    record.update(truncated=True, captured_ranges=[[0, len(head)], [size-limit//2, size]])
                elif size > limit:
                    record.update(missing=True, reason='evidence exceeds file limit')
                    return record
                else: data = stream.read(limit)
            target.write_bytes(data)
            record['retained_size'] = len(data)
            total += len(data); count += 1
        except (ValueError, OSError) as error:
            record.update(missing=True, reason=str(error))
            if isinstance(error, ValueError): unsafe = True
            manifest['evidence_errors'].append(f'{reference}: {error}')
        return record

    shared = ['ci-plan.json', 'lane-selection.json', 'results/policies/runtime-policy.json', 'results/policies/seccomp.json', 'workflow.json', 'stages.json', 'prepare.log', 'run.log', 'cleanup.log', 'report.log',
        'results/server.log', 'results/server.log.capture.json', 'results/container.log',
        'results/sdk-runtime/sdk.json', 'results/inventory.json', 'preparation-evidence/inventory.json',
        'preparation-evidence/prepared-suite.json', 'preparation-evidence/image-pull.log', 'preparation-evidence/sdk-acquire.log', 'preparation-evidence/batch-online.log', 'preparation-evidence/batch-offline.log',
        'preparation-evidence/actions-selection.json', 'preparation-evidence/resolved-runtime.json',
        'preparation-evidence/target-environment.json', 'preparation-evidence/actions-qualification.log',
        'preparation-evidence/sdk-download/sdk-error.json', 'preparation-evidence/sdk-download/sdk-acquisition.json']
    if manifest['cases'] or unsafe:
        raw = None
        try: raw = RECORDS.read_document(root, 'results/suite-result.json')
        except (ValueError, OSError): pass
        # Common identity/coverage facts once, excluding passing per-case payloads.
        if raw:
            identity = {key: raw.get(key) for key in ('attempt_id', 'source', 'runtime', 'hardware', 'expected_entries', 'inventory_sha256', 'asset_verification', 'server')}
            write_json(output / 'shared/identity.json', identity)
        shared += [name + '.metadata.json' for name in shared if name.endswith('.log')]
        shared += [name + '.capture.json' for name in shared if name.endswith('.log') and name + '.capture.json' not in shared]
        for relative in shared:
            if (root / relative).exists() or (root / relative).is_symlink():
                copied[relative] = copy(relative, 'shared/' + relative, 'shared')
                manifest['shared_evidence'].append(copied[relative])
        for case in manifest['cases']:
            records = []
            references = list(case['evidence'])
            for item in list(references):
                for suffix in ('.metadata.json', '.capture.json'):
                    metadata = item['source'] + suffix
                    if (root / metadata).is_file():
                        references.append({'source': metadata, 'path': item['path'] + suffix})
            for item in references:
                reference = item['source']
                if reference not in copied:
                    copied[reference] = copy(reference, item['path'], 'case')
                records.append(copied[reference])
            selection_id = case.get('selection_id')
            if selection_id and '/' not in selection_id and '..' not in selection_id:
                pipeline = root / 'results/models' / selection_id / 'pipeline'
                if pipeline.is_dir() and not pipeline.is_symlink():
                    # This tree is explicitly produced by allowlisted source capture.
                    for source in sorted(pipeline.rglob('*')):
                        if source.is_file() or source.is_symlink():
                            relative = source.relative_to(root).as_posix()
                            if relative not in copied:
                                copied[relative] = copy(relative, case['folder'] + '/pipeline/' + source.relative_to(pipeline).as_posix(), 'case')
                            records.append(copied[relative])
            case['evidence'] = records
            write_json(output / case['folder'] / 'result.json', case)
        lines = ['# Model failure evidence', '', f"Attempt: {manifest['attempt_id']}", '',
            f"Coverage: {manifest['counts']}", '', 'Shared logs are run-wide context; they are not exclusively attributable to an individual model.', '']
        def md(value):
            return re.sub(r'([\\`*_{}\[\]<>|])', r'\\\1', ' '.join(str(value).split()))
        for case in manifest['cases']:
            label = md(case['display_name'])
            reason = md(case['summary'])
            lines.append(f"- [{label}]({case['folder']}/result.json): {md(case['phase'])} — {reason}")
        if manifest['evidence_errors']: lines += ['', '## Evidence errors', '', *['- ' + md(error) for error in manifest['evidence_errors']]]
        (output / 'README.md').write_text('\n'.join(lines) + '\n')
        context = raw
        if not context:
            for relative in ('preparation-evidence/prepared-suite.json', 'prepared/prepared-suite.json'):
                try:
                    context = RECORDS.read_document(root, relative)
                    break
                except (ValueError, OSError): pass
        receipt = None
        try: receipt = RECORDS.read_document(root, 'results/sdk-runtime/sdk.json')
        except (ValueError, OSError): pass
        context = context or {}
        (output / 'summary.md').write_text(RECORDS.fallback_summary(manifest,
            context.get('runtime'), server=context.get('server'), installation=receipt, mode=context.get('mode')))
    write_json(output / 'failures.json', manifest)
    if archive:
        archive = Path(archive).absolute()
        if archive.is_symlink() or archive == output or archive.is_relative_to(output): raise ValueError('ZIP must be outside the failure tree')
        if manifest['cases'] or unsafe:
            archive.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
                for path in sorted(output.rglob('*')):
                    if path.is_file(): bundle.write(path, path.relative_to(output))
        else: archive.unlink(missing_ok=True)
    return manifest, int(unsafe)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--attempt', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--zip', dest='archive', type=Path)
    args = parser.parse_args(argv)
    try:
        manifest, code = package(args.attempt, args.output, args.archive)
        print(f"Failure evidence: {len(manifest['cases'])} cases; {args.output}")
        return code
    except (OSError, ValueError) as error:
        print(f'Failure packaging failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
