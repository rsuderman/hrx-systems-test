"""Frozen runtime policies preserve release behavior and exact request scope."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

import runpy
runpy.run_path(str(Path(__file__).resolve().parents[1] / "python/run.py"))["bootstrap"]()
from hrx_systems.preparation import runtime_policy as POLICY
from hrx_systems.sdk import environment as SDK
from _publication_fixture import publication_abi


def resolve(snap, **kwargs):
    if POLICY.needs_publication_abi(snap, kwargs.get('pipeline_upgrade', 'auto')):
        kwargs['publication_abi'] = publication_abi(snap)
    return POLICY.resolve(snap, **kwargs)


def snapshot(version='0.2.0.dev0', origin='github_actions'):
    value = SDK.resolve_runtime(ROOT / 'config/runtime.json')
    value['source']['kind'] = origin
    value['source']['release'] = 'v' + version if origin == 'release' else None
    value['source']['run_id'] = 123 if origin == 'github_actions' else None
    value['source']['run_attempt'] = 1 if origin == 'github_actions' else None
    value['wheel']['version'] = version
    value['wheel']['filename'] = f'hrx_loom-{version}-py3-none-manylinux_2_39_x86_64.whl'
    value['runtime_identity_sha256'] = SDK.runtime_identity_sha256(value)
    return value


class RuntimePolicyTests(unittest.TestCase):
    def test_01_preserves_requests_and_server_container_defaults(self):
        for origin in ('release', 'github_actions'):
            policy = resolve(snapshot('0.1.0', origin))
            self.assertFalse(policy['pipeline_upgrade']['enabled'])
            self.assertEqual(policy['pipeline_upgrade']['reason'], 'target-0.1.0')
            self.assertEqual(POLICY.server_args(policy), [])
            self.assertIsNone(policy['seccomp'])
            request = {'messages': [], 'reasoning_effort': 'high', 'max_tokens': 256}
            effective = POLICY.request_profile(policy, {}, request, '4')
            self.assertEqual(effective['smoke_request'], request)
            self.assertEqual(effective['smoke_expected_text'], '4')
            self.assertEqual(effective['override'], {})
            self.assertEqual(request['reasoning_effort'], 'high')
            self.assertEqual(POLICY.validate(policy, snapshot('0.1.0', origin)), policy)

    def test_02_stable_and_actions_select_identical_policy_semantics(self):
        for version, origin in [('0.2.0', 'release'), ('0.2.0.dev0', 'github_actions')]:
            snap = snapshot(version, origin)
            policy = resolve(snap)
            self.assertTrue(policy['pipeline_upgrade']['enabled'])
            self.assertEqual(policy['pipeline_upgrade']['migration_id'], 'hrx-pipelines-0.1.0-to-0.2.0')
            self.assertEqual(POLICY.server_args(policy), ['--pool_backing=fixed'])
            self.assertIsNotNone(policy['seccomp'])
            self.assertEqual(POLICY.validate(policy, snap), policy)

    def test_only_exact_six_field_selections_get_one_request_field(self):
        policy = resolve(snapshot())
        configured = json.loads((ROOT / POLICY.CONFIG_PATH).read_text())['request_profiles']['overrides']
        self.assertEqual(policy['request_profiles']['overrides'], configured)
        base = {'messages': [{'role': 'user', 'content': 'digit only'}], 'temperature': 0, 'max_tokens': 256, 'stream': False}
        original = copy.deepcopy(base)
        for rule in policy['request_profiles']['overrides']:
            identity = rule['selection']
            effective = POLICY.request_profile(policy, identity, base, '4')
            self.assertEqual(effective['smoke_request'], {**base, 'reasoning_effort': 'none'})
            self.assertEqual(effective['smoke_expected_text'], '4')
            self.assertNotEqual(effective['published_request_sha256'], effective['effective_request_sha256'])
            for field in identity:
                other = {**identity, field: identity[field] + '-different'}
                self.assertEqual(POLICY.request_profile(policy, other, base, '4')['smoke_request'], base)
        self.assertEqual(base, original)

    def historical_config(self):
        config = json.loads((ROOT / POLICY.CONFIG_PATH).read_text())
        unique = {}
        for entry in config['request_profiles']['overrides']:
            logical = tuple(entry['selection'][field] for field in POLICY.SELECTION_FIELDS if field != 'publication_revision')
            unique.setdefault(logical, entry)
        config['request_profiles']['overrides'] = list(unique.values())
        self.assertEqual(len(unique), 3)
        return config

    def test_revision_variants_preserve_three_families_and_all_exact_guards(self):
        original = self.historical_config()
        self.assertEqual(POLICY._validate_config(copy.deepcopy(original)), original)
        variants = copy.deepcopy(original)
        for row, revision in zip(original['request_profiles']['overrides'][:2], ('a' * 40, 'b' * 40)):
            variants['request_profiles']['overrides'].append({**copy.deepcopy(row),
                'selection': {**row['selection'], 'publication_revision': revision}})
        self.assertEqual(len(POLICY._validate_config(variants)['request_profiles']['overrides']), 5)
        invalid = []
        duplicate = copy.deepcopy(variants); duplicate['request_profiles']['overrides'].append(copy.deepcopy(duplicate['request_profiles']['overrides'][0])); invalid.append(duplicate)
        for fields in ({'reasoning_effort': 'high'}, {'reasoning_effort': 'none', 'max_tokens': 1024}):
            changed = copy.deepcopy(variants); changed['request_profiles']['overrides'][0]['request_fields'] = fields; invalid.append(changed)
        for selection_change in ({'publication_revision': 'main'}, {'publication_revision': None}):
            changed = copy.deepcopy(variants); changed['request_profiles']['overrides'][0]['selection'].update(selection_change); invalid.append(changed)
        incomplete = copy.deepcopy(variants); incomplete['request_profiles']['overrides'][0]['selection'].pop('programs'); invalid.append(incomplete)
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(ValueError): POLICY._validate_config(config)

    def test_family_cardinality_remains_bounded_independently_of_revisions(self):
        original = self.historical_config()
        too_few = copy.deepcopy(original); too_few['request_profiles']['overrides'].pop()
        too_many = copy.deepcopy(original)
        row = copy.deepcopy(original['request_profiles']['overrides'][0]); row['selection']['variant'] += '-different'
        too_many['request_profiles']['overrides'].append(row)
        one_family = copy.deepcopy(original)
        row = original['request_profiles']['overrides'][0]
        one_family['request_profiles']['overrides'] = [{**copy.deepcopy(row), 'selection': {
            **row['selection'], 'publication_revision': digit * 40}} for digit in ('a', 'b', 'c')]
        for config in (too_few, too_many, one_family):
            with self.subTest(config=config), self.assertRaisesRegex(ValueError, 'three logical'):
                POLICY._validate_config(config)

    def test_actual_refresh_configuration_producer_feeds_02_policy_and_frozen_validation(self):
        from hrx_systems.cache import refresh_publications as REFRESH
        historical = self.historical_config()
        old = historical['request_profiles']['overrides'][0]['selection']
        revision = 'a' * 40 if old['publication_revision'] != 'a' * 40 else 'b' * 40
        name = old['repository']; card, lock = '{}', '{}'
        metadata = {name: {'repository': name, 'revision': revision, 'status': 'available',
            'card': card, 'lock': lock, 'card_sha256': REFRESH.sha(card.encode()), 'lock_sha256': REFRESH.sha(lock.encode())}}
        inspected = {name: {'status': 'inspected', 'errors': {},
            'card_sha256': metadata[name]['card_sha256'], 'lock_sha256': metadata[name]['lock_sha256'],
            'selections': {REFRESH.key(old): {**{field: old[field] for field in ('model_id', 'preset', 'variant', 'programs')},
                'service_kind': 'text', 'assets': [], 'gguf_bytes': 100,
                'published_execution': {'rows': 4, 'context_capacity': 128}}}}}
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory); shutil.copytree(ROOT / 'config', repo / 'config')
            (repo / POLICY.CONFIG_PATH).write_text(REFRESH.formatted(historical))
            snap = snapshot()
            with patch.object(POLICY, 'REPO', repo): before = resolve(snap)
            plan = REFRESH.make_plan(repo, [{'selection': old, 'aliases': [], 'assets': [], 'cataloged': False}],
                                     metadata, inspected, {'fixture': True}, False, 'loom-fun', {})
            REFRESH.apply_plan(plan, repo)
            with patch.object(POLICY, 'REPO', repo): policy = resolve(snap)
            self.assertEqual(POLICY.validate(policy, snap), policy)
            self.assertEqual(len(policy['request_profiles']['overrides']), 4)
            self.assertEqual(policy['launch'], before['launch'])
            self.assertEqual(policy['seccomp'], before['seccomp'])
            request = {'messages': [], 'max_tokens': 256, 'temperature': 0}
            for selected in (old, {**old, 'publication_revision': revision}):
                actual = POLICY.request_profile(policy, selected, request, '4')
                self.assertEqual(actual['smoke_request'], {**request, 'reasoning_effort': 'none'})
                self.assertEqual(actual['smoke_expected_text'], '4')
            unrelated = {**old, 'publication_revision': 'f' * 40}
            self.assertEqual(POLICY.request_profile(policy, unrelated, request, '4')['smoke_request'], request)

    def test_none_bypasses_source_upgrade_without_silently_changing_settings(self):
        policy = resolve(snapshot(), pipeline_upgrade='none')
        self.assertFalse(policy['pipeline_upgrade']['enabled'])
        self.assertEqual(policy['pipeline_upgrade']['reason'], 'explicit-none')
        self.assertEqual(POLICY.server_args(policy), ['--pool_backing=fixed'])
        self.assertIsNotNone(policy['request_profiles'])

    def test_unknown_future_auto_fails_but_explicit_raw_mode_is_diagnostic(self):
        with self.assertRaisesRegex(ValueError, 'unsupported'):
            resolve(snapshot('0.3.0'))
        policy = resolve(snapshot('0.3.0'), pipeline_upgrade='none')
        self.assertEqual(POLICY.server_args(policy), [])
        self.assertIsNone(policy['seccomp'])
        self.assertEqual(POLICY.validate(policy, snapshot('0.3.0')), policy)

    def test_launch_options_are_positive_and_mutually_exclusive(self):
        for value in ['8gib', '8192mib', 8589934592]:
            policy = resolve(snapshot(), memory_bytes=value)
            self.assertEqual(policy['launch'], {'pool_backing': 'elastic', 'memory_bytes': 8589934592})
            self.assertEqual(POLICY.server_args(policy), ['--memory_bytes=8589934592'])
        with self.assertRaisesRegex(ValueError, 'requires'):
            resolve(snapshot(), pool_backing='elastic')
        for backing, size in [('fixed', '8gib'), ('bad', None), ('elastic', 0), ('elastic', -1), ('elastic', True), ('elastic', 'all')]:
            with self.assertRaises(ValueError): resolve(snapshot(), pool_backing=backing, memory_bytes=size)
        with self.assertRaisesRegex(ValueError, '0.1.0'):
            resolve(snapshot('0.1.0'), memory_bytes='8gib')

    def test_runtime_and_policy_tampering_are_detected(self):
        snap = snapshot()
        policy = resolve(snap)
        for field in ('runtime_version', 'runtime_identity_sha256', 'policy_id'):
            changed = copy.deepcopy(policy)
            changed[field] = 'changed'
            with self.assertRaises(ValueError): POLICY.validate(changed, snap)
        changed = copy.deepcopy(policy)
        changed['launch']['memory_bytes'] = 1
        with self.assertRaises(ValueError): POLICY.validate(changed, snap)
        changed = copy.deepcopy(snap)
        changed['wheel']['version'] = '0.1.0'
        with self.assertRaises(ValueError): resolve(changed)

    def test_frozen_policy_never_needs_live_configuration_to_validate(self):
        policy = resolve(snapshot())
        with tempfile.TemporaryDirectory() as directory:
            old = POLICY.REPO
            POLICY.REPO = Path(directory)
            try:
                self.assertEqual(POLICY.validate(policy, snapshot()), policy)
                selection = policy['request_profiles']['overrides'][0]['selection']
                self.assertEqual(POLICY.request_profile(policy, selection, {}, '4')['smoke_request'], {'reasoning_effort': 'none'})
            finally: POLICY.REPO = old

    def test_seccomp_is_exactly_captured_default_plus_mbind(self):
        policy = resolve(snapshot())
        descriptor = policy['seccomp']
        import hashlib
        base_bytes = (ROOT / descriptor['base_source_path']).read_bytes()
        profile_bytes = (ROOT / descriptor['source_path']).read_bytes()
        self.assertEqual(hashlib.sha256(base_bytes).hexdigest(), descriptor['base_sha256'])
        self.assertEqual(hashlib.sha256(profile_bytes).hexdigest(), descriptor['sha256'])
        expected = json.loads(base_bytes)
        expected['syscalls'].append({'names': ['mbind'], 'action': 'SCMP_ACT_ALLOW', 'args': []})
        self.assertEqual(json.loads(profile_bytes), expected)
        self.assertEqual(json.loads(profile_bytes)['defaultAction'], 'SCMP_ACT_ERRNO')


if __name__ == '__main__': unittest.main()
