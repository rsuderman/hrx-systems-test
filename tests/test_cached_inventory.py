"""Inventory publication boundaries and policy selection, without GPU access."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CONFIG_SPEC = importlib.util.spec_from_file_location('test_model_config', ROOT / 'scripts/model-config.py')
CONFIG = importlib.util.module_from_spec(CONFIG_SPEC)
CONFIG_SPEC.loader.exec_module(CONFIG)

SPEC = importlib.util.spec_from_file_location('cached_inventory', ROOT / 'scripts/cached-inventory.py')
INV = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INV)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cache = Path(self.temporary.name)
        self.seed = {'schema_version': 1, 'selection': {
            'repository': 'loom-fun/example', 'publication_revision': 'a' * 40,
            'model_id': 'example/model', 'preset': 'chat', 'variant': 'gguf', 'programs': 'source'},
            'card_sha256': 'b' * 64, 'lock_sha256': 'c' * 64, 'files': [{
                'installation_path': 'parameter/weights.gguf', 'cache_root': 'loom-assets',
                'cache_path': 'blob/sha256/' + 'd' * 64, 'verified': True, 'roles': ['parameter'],
                'origin': '@checkpoint/weights.gguf', 'resolved': None, 'size': 4, 'sha256': 'd' * 64}]}

    def publish(self, relative, seed=None):
        path = self.cache / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(seed or self.seed))
        return path

    def test_subsets_deduplicate_receipts_and_include_new_publications(self):
        self.publish('seed-manifest.json')
        self.publish('models/catalog/example/seed-manifest.json')
        ignored = copy.deepcopy(self.seed)
        ignored['selection']['model_id'] = 'not-a-model-inventory'
        self.publish('runs/old/seed-manifest.json', ignored)
        first, candidates = INV.discover(self.cache)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(len(candidates[0]['aliases']), 2)
        frozen = candidates[0]['_raw']
        other = copy.deepcopy(self.seed)
        other['selection']['publication_revision'] = 'f' * 40
        self.publish('models/new/seed-manifest.json', other)
        second, candidates = INV.discover(self.cache)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(len(first['entries']), 1)
        self.assertEqual(len(second['entries']), 2)
        self.assertEqual(json.loads(frozen)['selection']['publication_revision'], 'a' * 40)

    def test_conflicting_identity_is_visible_not_arbitrarily_selected(self):
        self.publish('seed-manifest.json')
        other = copy.deepcopy(self.seed)
        other['files'][0]['sha256'] = 'e' * 64
        self.publish('models/alias/seed-manifest.json', other)
        metadata, candidates = INV.discover(self.cache)
        self.assertTrue(candidates[0]['conflict'])
        self.assertEqual(len(metadata['entries']), 1)

    def test_aliases_at_different_asset_locations_do_not_conflict(self):
        self.publish('seed-manifest.json')
        other = copy.deepcopy(self.seed)
        other['files'][0]['cache_root'] = 'huggingface'
        other['files'][0]['cache_path'] = 'blobs/other-location'
        self.publish('models/alias/seed-manifest.json', other)
        _, candidates = INV.discover(self.cache)
        self.assertFalse(candidates[0]['conflict'])

    def test_malformed_symlink_and_path_escape_receipts_are_errors(self):
        self.publish('models/bad/seed-manifest.json').write_text('{')
        self.publish('models/escape/seed-manifest.json')
        bad = copy.deepcopy(self.seed)
        bad['files'][0]['cache_path'] = '../outside'
        self.publish('models/escape/seed-manifest.json', bad)
        outside = self.cache / 'outside'
        outside.mkdir()
        self.publish('outside/seed-manifest.json')
        (self.cache / 'models/linked').symlink_to(outside, target_is_directory=True)
        (self.cache / 'seed-manifest.json').symlink_to(outside / 'seed-manifest.json')
        metadata, candidates = INV.discover(self.cache)
        self.assertFalse(candidates)
        self.assertEqual(len(metadata['errors']), 4)

    def test_no_payload_reads_are_needed_for_membership(self):
        self.publish('seed-manifest.json')
        self.assertFalse((self.cache / self.seed['files'][0]['cache_path']).exists())
        _, candidates = INV.discover(self.cache)
        self.assertEqual(len(candidates), 1)  # Native preparation owns complete asset checks.

    def test_inaccessible_manifest_and_catalog_directory_are_visible(self):
        self.publish('seed-manifest.json')
        self.publish('models/catalog/private/seed-manifest.json')
        original = Path.lstat
        def inaccessible(path):
            if path == self.cache / 'models/catalog' or path == self.cache / 'seed-manifest.json':
                raise PermissionError('fixture denied traversal')
            return original(path)
        with patch.object(Path, 'lstat', inaccessible):
            metadata, candidates = INV.discover(self.cache)
        self.assertFalse(candidates)
        self.assertEqual({e['path'] for e in metadata['errors']}, {'seed-manifest.json', 'models/catalog'})

    def test_workload_derives_native_settings_without_profile_whitelist(self):
        selected = SimpleNamespace(service={'kind': 'text'}, settings={'execution': {'rows': 3}})
        result = INV.workload({}, selected)
        self.assertEqual(result['published_execution'], {'rows': 3})
        self.assertEqual(result['smoke_expected_text'], '4')
        decision = INV.workload({}, SimpleNamespace(service={'kind': 'decision'},
                                                    settings={'execution': {'pending_requests': 64}}))
        self.assertEqual(decision['test_kind'], 'decision_smoke')
        self.assertEqual(decision['smoke_request']['questions']['route']['criteria'],
                         {'support': 'Handle the support request.'})
        with self.assertRaisesRegex(ValueError, 'unsupported_test_kind'):
            INV.workload({}, SimpleNamespace(service={'kind': 'image'}, settings={'execution': {'rows': 1}}))

    def test_existing_profile_is_behavior_override_not_membership(self):
        baseline = CONFIG.default_baseline()[0]
        identity = {k: baseline[k] for k in INV.FIELDS}
        self.assertEqual(INV.override_for(ROOT, identity)['smoke_request'], baseline['smoke_request'])
        identity['publication_revision'] = 'f' * 40
        self.assertIsNone(INV.override_for(ROOT, identity))


if __name__ == '__main__':
    unittest.main()
