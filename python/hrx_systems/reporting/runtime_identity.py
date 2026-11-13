#!/usr/bin/env python3
"""Pure, saved-data-only HRX identity extraction and human presentation."""
import html
import math
import re


def scalar(value):
    return value if isinstance(value, (str, int, float)) and not isinstance(value, bool) else None


def plain(value):
    value = scalar(value)
    return ' '.join(str(value).split())[:512] if value is not None else None


def describe(runtime, *, server=None, installation=None, mode=None):
    """Return display facts; configured pins alone never establish execution."""
    runtime = runtime if isinstance(runtime, dict) else {}
    legacy = runtime.get('sdk')
    if not isinstance(legacy, dict):
        identity = runtime.get('sdk_identity')
        legacy = identity.get('sdk', {}) if isinstance(identity, dict) else {}
    legacy = legacy if isinstance(legacy, dict) else {}
    fields = {key: plain(legacy.get(key)) for key in ('name', 'version', 'repository', 'release', 'source_commit')}
    build = {}
    if 'resolved_runtime' in runtime:
        resolved = runtime['resolved_runtime']
        if not isinstance(resolved, dict) or not isinstance(resolved.get('source'), dict) or not isinstance(resolved.get('wheel'), dict):
            raise ValueError('Malformed saved resolved runtime identity')
        source, wheel = resolved['source'], resolved['wheel']
        resolved_fields = {**{key: plain(wheel.get(key)) for key in ('name', 'version')},
            **{key: plain(source.get(key)) for key in ('repository', 'release')},
            'source_commit': plain(source.get('tested_sha'))}
        for key, value in fields.items():
            if value is not None and resolved_fields[key] is not None and value != resolved_fields[key]:
                raise ValueError(f'Saved resolved and legacy HRX identities disagree: {key}')
        fields = resolved_fields
        build = {key: source.get(key) for key in ('kind', 'run_id', 'run_attempt', 'requested_head_sha', 'relation_to_head', 'build_age_seconds')}
        # Saved optional display metadata may be malformed. Keep unknown relations
        # visible without letting an unhashable value abort fallback publication.
        build['relation_to_head'] = plain(build.get('relation_to_head'))
    installed = False
    if isinstance(installation, dict) and installation.get('status') == 'ready':
        observed_identity = installation.get('identity')
        observed = observed_identity.get('sdk') if isinstance(observed_identity, dict) else None
        details = installation.get('installation')
        if isinstance(observed, dict) and isinstance(details, dict):
            for key in ('name', 'version', 'repository', 'release', 'source_commit'):
                actual = plain(observed.get(key))
                if fields[key] is None and 'resolved_runtime' not in runtime:
                    fields[key] = actual
                if fields[key] is not None and actual is not None and fields[key] != actual:
                    raise ValueError(f'Saved installed and selected HRX identities disagree: {key}')
            actual_version = plain(details.get('sdk_version'))
            if actual_version is not None and fields['version'] is not None and actual_version != fields['version']:
                raise ValueError('Saved installed HRX package version disagrees with selected version')
            installed = bool(actual_version and fields['version'] == actual_version and plain(observed.get('name')) == fields['name'])
    start = server.get('start') if isinstance(server, dict) else None
    exercised = mode == 'real' and isinstance(start, dict) and start.get('status') == 'passed'
    state = 'fixture' if mode == 'fixture' else ('exercised' if exercised else ('installed' if installed else 'selected'))
    if not any(fields.values()): state = 'unavailable'
    return {**fields, 'state': state, 'build': build}


def commit_url(repository, commit):
    if isinstance(repository, str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) and all(part not in ('.', '..') for part in repository.split('/')) and isinstance(commit, str) and re.fullmatch(r'[0-9a-fA-F]{40}', commit):
        return f'https://github.com/{repository}/commit/{commit}'
    return None


def rows(identity):
    """A small common display model, with URLs constructed only from identities."""
    package = ' '.join(value for value in (identity['name'], identity['version']) if value) or 'Unavailable'
    result = [('HRX runtime', package, None)]
    states = {'selected': 'Selected; execution not reached/unconfirmed',
        'installed': 'Installed; execution not reached/unconfirmed', 'exercised': 'Exercised; model outcomes reported below',
        'fixture': 'Fixture evidence; not proof of runtime execution', 'unavailable': 'Unavailable'}
    result.append(('Runtime evidence', states[identity['state']], None))
    if identity['release']: result.append(('Release', identity['release'], None))
    commit = identity['source_commit']
    result.append(('HRX source', commit[:12] if commit else 'Unavailable', commit_url(identity['repository'], commit)))
    build = identity['build']
    run, attempt = build.get('run_id'), build.get('run_attempt')
    if build.get('kind') == 'github_actions':
        label = f'Run {run}' + (f' / attempt {attempt}' if type(attempt) is int and attempt > 0 else '') if type(run) is int and run > 0 else 'Unavailable'
        repository = identity['repository']
        url = f'https://github.com/{repository}/actions/runs/{run}' if type(run) is int and run > 0 and commit_url(repository, '0' * 40) else None
        result.append(('Upstream build', label, url))
    requested = plain(build.get('requested_head_sha'))
    if requested:
        result.append(('Requested main', requested[:12], commit_url(identity['repository'], requested)))
        relation = build.get('relation_to_head')
        result.append(('Build relation', {'equal': 'Matches requested HEAD', 'behind': 'Behind requested HEAD'}.get(relation, 'Unconfirmed'), None))
    age = build.get('build_age_seconds')
    if type(age) in (int, float) and math.isfinite(age) and age >= 0:
        result.append(('Build age at selection', f'{age:g} seconds', None))
    return result


def markdown(identity):
    def escape(value):
        value = re.sub(r'([\\`|\[\]()*_!#])', r'\\\1', str(value))
        return html.escape(value, quote=True)
    lines = ['### HRX build', '']
    for label, value, url in rows(identity):
        display = f'[{escape(value)}]({url})' if url else escape(value)
        lines.append(f'- **{label}:** {display}')
    return lines + ['']


def render_html(identity):
    values = []
    for label, value, url in rows(identity):
        display = html.escape(value)
        if url: display = f'<a href="{html.escape(url, quote=True)}">{display}</a>'
        values.append(f'<div><strong>{html.escape(label)}:</strong> {display}</div>')
    return '<section aria-label="HRX build"><h2>HRX build</h2>' + ''.join(values) + '</section>'
