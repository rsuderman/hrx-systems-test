#!/usr/bin/env python3
"""Compose model selections and test profiles with one shared runtime input."""
import copy
import json
from pathlib import Path

from ..layout import REPO_ROOT as REPO
from ..sdk import environment as SDK
FIELDS = ('repository', 'publication_revision', 'model_id', 'preset', 'variant', 'programs')


def profiles(repo=REPO):
    document = json.loads((repo / 'config/model-test-profiles.json').read_text())
    if document.get('schema_version') != 1 or not isinstance(document.get('defaults'), dict):
        raise ValueError('invalid model test profile schema')
    seen = set()
    for entry in document.get('overrides', []):
        key = tuple(entry['selection'][field] for field in FIELDS)
        if key in seen: raise ValueError('duplicate exact-selection test override')
        seen.add(key)
        if any(key in entry for key in ('sdk', 'container', 'published_execution')):
            raise ValueError('test profiles cannot override runtime or card capacities')
    return document


def override_for(identity, repo=REPO):
    for entry in profiles(repo).get('overrides', []):
        if all(entry['selection'][field] == identity[field] for field in FIELDS):
            return {key: copy.deepcopy(entry[key]) for key in ('smoke_request', 'smoke_expected_text')}
    return None


def runtime(path=None):
    path = Path(path or REPO / 'config/runtime.json').resolve(strict=True)
    try:
        settings, lock, _, identity = SDK.load_runtime(path)
        settings['_dependency_lock'], settings['_input_identity'] = lock, identity
        snapshot = SDK.resolve_runtime(settings)
        return SDK.runtime_baseline(settings, snapshot), snapshot, path.parent / settings['sdk']['dependency_lock']
    except SDK.SdkError as error:
        raise ValueError(str(error)) from error


def compose(entry, runtime_path=None, *, repo=REPO, runtime_context=None):
    adapter, snapshot, lock = runtime_context or runtime(runtime_path)
    if 'baseline' in entry:
        reference = Path(entry['baseline'])
        if reference.is_absolute() or '..' in reference.parts:
            raise ValueError('legacy baseline reference must stay inside repository')
        path = (repo / reference).resolve(strict=True)
        if not path.is_relative_to(repo.resolve()): raise ValueError('baseline escapes repository')
        value, _, _, _ = SDK.load_baseline(path)
        if runtime_path is None and runtime_context is None:
            adapter, snapshot, lock = runtime(path)
        elif value['sdk'] != adapter['sdk'] or value['container'] != adapter['container']:
            raise ValueError('legacy baseline and runtime settings conflict')
        model = {k: v for k, v in value.items() if k not in ('sdk', 'container', 'schema_version')}
    else:
        identity = entry['selection']
        if any(not isinstance(identity.get(k), str) or not identity[k] for k in FIELDS):
            raise ValueError('selection fields must be nonempty strings')
        model = copy.deepcopy(identity)
        model.update({k: copy.deepcopy(entry[k]) for k in ('published_execution', 'memory_qualification') if k in entry})
        model.update(override_for(identity, repo) or profiles(repo)['defaults']['text'])
    return {**adapter, **model, 'resolved_runtime': snapshot}, lock


def default_baseline(runtime_path=None):
    suite = json.loads((REPO / 'config/cached-model-tests.json').read_text())
    return compose(suite['entries'][0], runtime_path)
