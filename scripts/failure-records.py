#!/usr/bin/env python3
"""Derive readable failure cases from saved attempt facts, without execution."""
import hashlib
import html
import json
from pathlib import Path, PurePosixPath
import re

MAX_JSON = 4 * 1024 * 1024
PHASES = ('load', 'readiness', 'query', 'assertion', 'unload')
STATUSES = ('passed', 'failed', 'not_run', 'incomplete')


def safe_file(root, relative):
    if not isinstance(relative, str): raise ValueError('unsafe evidence path')
    path = PurePosixPath(relative)
    if not relative or path.is_absolute() or '..' in path.parts or '\\' in relative:
        raise ValueError('unsafe evidence path')
    target = root
    for part in path.parts:
        target = target / part
        if target.is_symlink():
            raise ValueError('symlink evidence is forbidden')
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('evidence escapes attempt')
    return target


def read_document(root, relative):
    path = safe_file(root, relative)
    if not path.is_file() or path.stat().st_size > MAX_JSON:
        raise ValueError(f'missing or oversized JSON: {relative}')
    document = json.loads(path.read_text())
    if not isinstance(document, dict):
        raise ValueError(f'expected JSON object: {relative}')
    return document


def summarize(diagnostic, response=None, expected=None):
    """Shorten observed diagnostics; do not diagnose an unobserved root cause."""
    literal = ' '.join(str(diagnostic or 'No diagnostic was recorded').split())
    # Native interface diagnostics may contain long nested RuntimeError wrappers.
    arguments = re.search(r'(?:expects?|expected)\s+(\d+)\s+arguments?.*?(?:got|declares?|provided|source[^\d]*)\s*(\d+)', literal, re.I)
    if arguments:
        return f'Server expects {arguments[1]} arguments; pipeline declares {arguments[2]}'
    if response and isinstance(response, dict):
        choices = response.get('choices')
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            choice = choices[0]
            message, usage = choice.get('message'), response.get('usage')
            if not isinstance(message, dict) or (usage is not None and not isinstance(usage, dict)):
                return literal[:512]
            if choice.get('finish_reason') == 'length' and message.get('content') in ('', None):
                tokens = (usage or {}).get('completion_tokens')
                details = ['Empty final answer', 'finish_reason=length']
                if isinstance(tokens, int): details.append(f'{tokens} completion tokens')
                if expected is not None: details.append(f'expected {expected}')
                return '; '.join(details)[:512]
    if 'index.assume' in literal:
        offset = literal.index('index.assume')
        literal = literal[max(0, offset - 50):offset + 350]
    return literal[:512]


def display_name(selection, fallback):
    repository = selection.get('repository', '')
    model = selection.get('model_id') or fallback
    publisher = repository.split('/')[-1] if '/' not in repository else repository.split('/')[0]
    return f'{publisher} / {model}' if publisher else str(model)


def collect(attempt, results_dir=None):
    """Return failures.json v1. Evidence source paths stay attempt-relative."""
    root = Path(attempt)
    def read(relative):
        if results_dir is not None:
            if not relative.startswith('results/'):
                raise ValueError('Standalone results exclude attempt context')
            return read_document(Path(results_dir), relative[len('results/'):])
        return read_document(root, relative)
    document = {'schema_version': 1, 'attempt_id': root.name,
        'counts': {'expected': None, **{key: None for key in STATUSES}},
        'cases': [], 'shared_evidence': [], 'evidence_errors': []}
    folders = set()

    def case(scope, selection_id, selection, cycle, phase, reason, observations=None, references=()):
        case_id = f'{selection_id}:cycle-{cycle}' if cycle else f'{selection_id}:{phase}'
        if scope == 'run': case_id = f'run:{phase}:' + hashlib.sha256(str(reason).encode()).hexdigest()[:12]
        label = display_name(selection, selection_id) if scope == 'model' else f'Run / {phase}'
        slug = re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')[:80] or 'failure'
        suffix = hashlib.sha256(case_id.encode()).hexdigest()[:12]
        folder = f'cases/{slug}--{suffix}'
        if folder in folders: return
        folders.add(folder)
        response = None
        for reference in references:
            if isinstance(reference, str) and reference.endswith('response.json'):
                try: response = read(reference)
                except (OSError, ValueError): pass
        entry = {'case_id': case_id, 'scope': scope, 'selection_id': selection_id if scope == 'model' else None,
            'selection': selection or None, 'display_name': label, 'cycle': cycle, 'phase': phase,
            'summary': summarize(reason, response, expected.get(selection_id)), 'diagnostic': str(reason or 'No diagnostic recorded')[:32768],
            'folder': folder, 'evidence': [], 'missing_evidence': [], 'observations': observations or []}
        for reference in dict.fromkeys(references):
            if isinstance(reference, str):
                entry['evidence'].append({'source': reference, 'path': folder + '/evidence/' + reference,
                    'attribution': 'case'})
        if scope == 'model' and not any(str(p).endswith('request.json') for p in references):
            entry['missing_evidence'].append('No saved request: query was not reached or request capture is unavailable')
        document['cases'].append(entry)

    expected = {}
    raw = None
    try:
        raw = read('results/suite-result.json')
        if raw.get('schema_version') != 1 or not isinstance(raw.get('expected_entries'), list) or not isinstance(raw.get('models'), list):
            raise ValueError('invalid suite-result schema')
        if not isinstance(raw.get('server', {}), dict):
            raise ValueError('invalid server result')
        document['attempt_id'] = raw.get('attempt_id') or root.name
    except (ValueError, OSError) as error:
        document['evidence_errors'].append(str(error))
        raw = None
    prepared = None
    for relative in ('preparation-evidence/prepared-suite.json', 'prepared/prepared-suite.json'):
        try:
            prepared = read(relative)
            break
        except (ValueError, OSError): pass
    inventory = None
    for relative in ('results/inventory.json', 'preparation-evidence/inventory.json', 'prepared/inventory.json'):
        try:
            inventory = read(relative)
            break
        except (ValueError, OSError): pass
    entries = raw.get('expected_entries', []) if raw else (prepared or inventory or {}).get('entries', [])
    if raw or prepared or inventory:
        ids = [entry.get('id') for entry in entries if isinstance(entry, dict)]
        if len(ids) != len(entries) or len(set(ids)) != len(ids) or any(not isinstance(i, str) for i in ids):
            raise ValueError('invalid expected selection identities')
        document['counts'] = {'expected': len(entries), **{key: 0 for key in STATUSES}}
        expected = {entry['id']: entry.get('expected_text') for entry in entries}
        models = {item['id']: item for item in (raw or {}).get('models', []) if isinstance(item, dict) and isinstance(item.get('id'), str)}
        for entry in entries:
            model = models.get(entry['id'], entry if not raw else {})
            selection = model.get('selection') or entry.get('selection') or {}
            if not isinstance(selection, dict): raise ValueError('invalid selection result')
            state = model.get('status')
            if not raw: state = 'failed' if model.get('preparation', {}).get('status') == 'failed' else 'not_run'
            if state not in STATUSES: state = 'incomplete'
            cycles = model.get('cycles', [])
            if not isinstance(cycles, list) or any(not isinstance(cycle, dict) for cycle in cycles):
                raise ValueError('invalid cycle result')
            if any(key in cycle and not isinstance(cycle[key], dict) for cycle in cycles for key in PHASES):
                raise ValueError('invalid cycle phase result')
            if raw:
                def phase_status(value):
                    if not isinstance(value, dict): return 'incomplete'
                    status = value.get('status', 'incomplete')
                    status = 'passed' if status == 'ready' else status
                    if status not in STATUSES: raise ValueError('invalid phase status')
                    return status
                observed = [phase_status(model.get('preparation'))]
                observed += [phase_status(cycle.get(key)) for cycle in cycles for key in PHASES]
                if not cycles: observed.append('not_run' if state == 'not_run' else 'incomplete')
                if state != 'passed': observed.append(state)
                state = next((value for value in ('failed', 'incomplete', 'not_run') if value in observed), 'passed')
            document['counts'][state] += 1
            found = False
            references = ['results/' + p for p in model.get('evidence', []) if isinstance(p, str)]
            if not raw:
                base = 'preparation-evidence/' if (root / 'preparation-evidence').exists() else 'prepared/'
                references = [base + p for p in model.get('evidence', []) if isinstance(p, str)]
            for number, cycle in enumerate(cycles, 1):
                failed = [(key, cycle[key]) for key in PHASES if isinstance(cycle.get(key), dict) and cycle[key].get('status') == 'failed']
                if not failed: continue
                found = True
                cycle_refs = list(references)
                cycle_refs += ['results/' + cycle[key] for key in ('request_path', 'response_path') if isinstance(cycle.get(key), str)]
                for key in PHASES:
                    cycle_refs += ['results/' + p for p in cycle.get(key, {}).get('evidence', []) if isinstance(p, str)]
                case('model', entry['id'], selection, number, failed[0][0], failed[0][1].get('reason'),
                     [{'phase': key, **facts} for key, facts in failed], cycle_refs)
            if state in ('failed', 'incomplete') and not found:
                preparation = model.get('preparation') or {}
                if not isinstance(preparation, dict): raise ValueError('invalid preparation result')
                stage = 'preparation' if preparation.get('status') != 'ready' else 'execution'
                case('model', entry['id'], selection, None, stage, preparation.get('reason') or model.get('reason') or 'Required execution evidence is incomplete', references=references)
        if raw and (set(models) - set(ids) or len(models) != len(raw['models'])):
            case('run', 'inventory', {}, None, 'inventory', 'Unexpected or duplicate model result identities')
    for error in (raw or prepared or inventory or {}).get('inventory_errors', (inventory or {}).get('errors', [])):
        case('run', 'inventory', {}, None, 'inventory', json.dumps(error, ensure_ascii=False))
    for key, facts in (raw or {}).get('server', {}).items():
        if isinstance(facts, dict) and facts.get('status') == 'failed':
            case('run', key, {}, None, 'server-' + key, facts.get('reason'))
    try:
        stages = read('stages.json')
        for name, value in stages.get('stages', {}).items():
            state = value.get('status') if isinstance(value, dict) else value
            if state in ('failed', 'failure', 'cancelled'):
                if name == 'run' and document['cases']: continue
                case('run', name, {}, None, name, f'{name} stage {state}', references=[name + '.log'])
        if stages.get('interrupted'): case('run', 'interrupted', {}, None, 'interrupted', f"Execution interrupted ({stages['interrupted']})")
    except (ValueError, OSError): pass
    if raw and not entries:
        case('run', 'inventory', {}, None, 'inventory', 'The frozen expected suite is empty')
    if raw is None:
        case('run', 'results', {}, None, 'results', 'Execution results unavailable or malformed; see retained stage evidence')
    elif raw.get('status') != 'passed' and not document['cases']:
        case('run', 'execution', {}, None, 'execution', raw.get('reason') or 'Execution did not pass; no specific failure evidence')
    return document


def collect_results(results_dir):
    """Normalize a standalone saved results directory for the report renderer."""
    results_dir = Path(results_dir)
    return collect(results_dir.parent, results_dir=results_dir)


def fallback_summary(manifest):
    """Human-readable saved-facts fallback when the full renderer cannot run."""
    def md(value):
        value = html.escape(' '.join(str(value).split()), quote=False)
        return value.translate(str.maketrans({'|': '&#124;', '[': '&#91;', ']': '&#93;',
            '`': '&#96;', '*': '&#42;', '_': '&#95;'}))
    counts = manifest.get('counts', {})
    count = lambda key: counts.get(key) if counts.get(key) is not None else 'unknown'
    lines = ['## Verification incomplete', '', 'The full report is unavailable; retained stage evidence follows.', '',
        f"Expected: **{count('expected')}** · Passed: **{count('passed')}** · Failed: **{count('failed')}** · Not run: **{count('not_run')}** · Incomplete: **{count('incomplete')}**", '',
        '## Failures', '', '| Model or stage | Operation | Observed detail |', '|---|---|---|']
    cases = manifest.get('cases', [])
    for case in cases[:20]:
        lines.append(f"| {md(case.get('display_name', 'Unavailable'))} | {md(case.get('phase', 'Unknown'))} | {md(case.get('summary', 'No diagnostic recorded'))} |")
    if len(cases) > 20: lines += ['', f'{len(cases) - 20} additional failure cases are listed in the failure bundle.']
    errors = manifest.get('evidence_errors', [])
    if errors: lines += ['', 'Evidence availability: ' + '; '.join(md(error)[:512] for error in errors[:5])]
    return '\n'.join(lines) + '\n'
