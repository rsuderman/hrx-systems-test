"""Configuration migration proves preserved selections and independent identities."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('runtime_config_tests', ROOT / 'scripts/model-config.py')
CONFIG = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONFIG)
SDK = CONFIG.SDK


class RuntimeConfigTests(unittest.TestCase):
    def test_eight_effective_samples_preserve_historical_behavior(self):
        suite = json.loads((ROOT / 'config/cached-model-tests.json').read_text())
        expected = json.loads((ROOT / 'tests/fixtures/model-config/expected-sample.json').read_text())
        runtime_ids = set()
        for entry in suite['entries']:
            value, _ = CONFIG.compose(entry)
            actual = {k: v for k, v in value.items() if k not in ('sdk', 'container', 'schema_version', 'resolved_runtime')}
            self.assertEqual(actual, expected[entry['id']])
            runtime_ids.add(value['resolved_runtime']['runtime_identity_sha256'])
        self.assertEqual(len(runtime_ids), 1)
        self.assertEqual(len(suite['entries']), 8)

    def test_runtime_identity_is_not_changed_by_resolution_time(self):
        path = ROOT / 'config/runtime.json'
        first, second = SDK.resolve_runtime(path), SDK.resolve_runtime(path)
        self.assertEqual(first['runtime_identity_sha256'], second['runtime_identity_sha256'])
        self.assertNotEqual(first['source']['requested_at'], second['source']['requested_at'])
        self.assertEqual(first['wheel']['source_commit_evidence'], 'configured_release_pin')
        self.assertEqual(first['dependencies']['compatibility_status'], 'not_checked')
        self.assertEqual(len(first['dependencies']['wheels']), 15)

    def test_actions_settings_validate_without_inventing_a_wheel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = json.loads((ROOT / 'config/runtime.json').read_text())
            settings['sdk']['source'] = {'kind': 'github_actions', 'repository': 'ROCm/hrx-system',
                'branch': 'main', 'mode': 'latest-successful', 'artifact_name': 'loom-sdk-wheel-linux-Release'}
            path = root / 'runtime.json'
            path.write_text(json.dumps(settings))
            (root / settings['sdk']['dependency_lock']).write_bytes((ROOT / 'config/sdk-requirements.lock.json').read_bytes())
            authored, lock, payloads, identity = SDK.load_runtime(path)
            self.assertNotIn('wheel', authored['sdk']['source'])
            self.assertTrue(all(kind == 'python' for kind, _ in payloads))
            with self.assertRaisesRegex(SDK.SdkError, 'not been resolved'):
                SDK.runtime_baseline(authored)
            with self.assertRaisesRegex(SDK.SdkError, 'Actions resolver'):
                SDK.resolve_runtime(authored)

    def test_legacy_runtime_adapter_is_equivalent_and_conflicts_reject(self):
        baseline, lock_path = CONFIG.default_baseline()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'baseline.json'
            path.write_text(json.dumps(baseline))
            (root / baseline['sdk']['dependency_lock']).write_bytes(lock_path.read_bytes())
            loaded, _, _, _ = SDK.load_baseline(path)
            self.assertEqual(loaded['sdk'], baseline['sdk'])
            baseline['sdk']['source_commit'] = 'f' * 40
            path.write_text(json.dumps(baseline))
            with self.assertRaisesRegex(SDK.SdkError, 'snapshot disagrees'):
                SDK.load_baseline(path)

    def test_present_malformed_snapshot_never_uses_legacy_adapter(self):
        baseline, lock_path = CONFIG.default_baseline()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / baseline['sdk']['dependency_lock']).write_bytes(lock_path.read_bytes())
            path = root / 'baseline.json'
            for malformed in (None, {}, []):
                path.write_text(json.dumps({**baseline, 'resolved_runtime': malformed}))
                with self.assertRaisesRegex(SDK.SdkError, 'frozen runtime snapshot'):
                    SDK.load_baseline(path)

    def test_legacy_model_cannot_replace_an_already_frozen_shared_runtime(self):
        baseline, lock = CONFIG.default_baseline()
        context = CONFIG.runtime()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline.pop('resolved_runtime')
            baseline['sdk']['version'] = 'different-legacy-build'
            (root / 'legacy.json').write_text(json.dumps(baseline))
            (root / baseline['sdk']['dependency_lock']).write_bytes(lock.read_bytes())
            with self.assertRaisesRegex(ValueError, 'runtime settings conflict'):
                CONFIG.compose({'baseline': 'legacy.json'}, repo=root, runtime_context=context)

    def test_duplicate_overrides_and_capacity_in_profiles_are_rejected(self):
        original = json.loads((ROOT / 'config/model-test-profiles.json').read_text())
        for change in ('duplicate', 'capacity'):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'config').mkdir()
                document = copy.deepcopy(original)
                if change == 'duplicate': document['overrides'].append(document['overrides'][0])
                else: document['overrides'][0]['published_execution'] = {'rows': 1}
                (root / 'config/model-test-profiles.json').write_text(json.dumps(document))
                with self.assertRaises(ValueError): CONFIG.profiles(root)

    def test_unlisted_selection_has_default_test_behavior_without_membership_filter(self):
        identity = dict(json.loads((ROOT / 'config/cached-model-tests.json').read_text())['entries'][0]['selection'])
        identity['publication_revision'] = 'f' * 40
        self.assertIsNone(CONFIG.override_for(identity))
        default = CONFIG.profiles()['defaults']['text']
        self.assertEqual(default['smoke_expected_text'], '4')
        self.assertEqual(default['smoke_request']['max_tokens'], 256)
