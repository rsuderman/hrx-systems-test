"""Portable contract checks for the registered representative model sample."""
import csv
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('sample_sdk_environment', ROOT / 'scripts/sdk-environment.py')
SDK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SDK)


class ModelSampleTests(unittest.TestCase):
    def setUp(self):
        self.suite = json.loads((ROOT / 'config/cached-model-tests.json').read_text())
        self.original = json.loads((ROOT / 'config/single-model-e2e.json').read_text())
        self.profiles = {entry['id']: json.loads((ROOT / entry['baseline']).read_text())
                         for entry in self.suite['entries']}

    def test_distinct_pinned_entries_have_noncolliding_seed_indices(self):
        self.assertEqual(self.suite['schema_version'], 1)
        self.assertEqual(len(self.suite['entries']), 8)
        self.assertEqual(len(self.profiles), 8)
        self.assertEqual(len({p['model_id'] for p in self.profiles.values()}), 8)
        self.assertEqual(self.suite['entries'][0], {
            'id': 'qwen35-08b', 'baseline': 'config/single-model-e2e.json',
            'seed_manifest': 'seed-manifest.json'})
        manifests = set()
        for entry in self.suite['entries']:
            with self.subTest(entry=entry['id']):
                self.assertNotIn(entry['seed_manifest'], manifests)
                manifests.add(entry['seed_manifest'])
                for key in ('baseline', 'seed_manifest'):
                    path = Path(entry[key])
                    self.assertFalse(path.is_absolute())
                    self.assertNotIn('..', path.parts)
                if entry['id'] != 'qwen35-08b':
                    self.assertEqual(entry['seed_manifest'],
                                     f"models/{entry['id']}/seed-manifest.json")

    def test_profiles_match_inventory_pins_and_share_bootstrap_runtime(self):
        with (ROOT / 'catalog/model-size-index.csv').open() as stream:
            inventory = {row['model_repository']: row for row in csv.DictReader(stream)}
        for entry in self.suite['entries']:
            profile, lock, payloads, identity = SDK.load_baseline(ROOT / entry['baseline'])
            with self.subTest(entry=entry['id']):
                row = inventory[profile['repository']]
                self.assertEqual(profile['publication_revision'], row['publication_revision'])
                self.assertEqual(profile['preset'], row['preset'])
                self.assertEqual(profile['variant'], row['variant'])
                self.assertEqual(profile['programs'], 'source')
                self.assertEqual(profile['sdk'], self.original['sdk'])
                self.assertEqual(profile['container'], self.original['container'])
                self.assertEqual(profile['memory_qualification'], 'unknown')
                self.assertEqual(identity['sdk']['version'], '0.1.0')
                self.assertEqual(len(payloads), len(lock['wheels']) + 1)
                execution = profile['published_execution']
                self.assertGreater(execution['rows'], 0)
                self.assertGreater(execution['context_capacity'], 0)
                self.assertGreaterEqual(execution['pool_capacity'], execution['context_capacity'])
                self.assertGreater(execution['prefill_capacity'], 0)

    def test_requests_keep_the_strict_assertion_and_explicit_reasoning_budget(self):
        reasoning = {'qwen3-06b': 'high', 'lfm3-17b': 'high', 'gpt-oss-20b': 'low'}
        for entry_id, profile in self.profiles.items():
            request = profile['smoke_request']
            with self.subTest(entry=entry_id):
                self.assertEqual(profile['smoke_expected_text'], '4')
                expected_messages = ([{'role': 'user', 'content': 'Calculate 2 + 2. Reply with a single numeral.'}]
                                     if entry_id == 'gemma3-1b' else self.original['smoke_request']['messages'])
                self.assertEqual(request['messages'], expected_messages)
                self.assertEqual(request['temperature'], 0)
                self.assertIs(request['stream'], False)
                expected_budget = 8 if entry_id == 'qwen35-08b' else (256 if entry_id in reasoning else 32)
                self.assertEqual(request['max_tokens'], expected_budget)
                self.assertLessEqual(request['max_tokens'], profile['published_execution']['max_tokens'])
                self.assertEqual(request.get('reasoning_effort'), reasoning.get(entry_id))
                self.assertNotIn('enable_thinking', request)
                self.assertNotIn('chat_template_kwargs', request)


if __name__ == '__main__':
    unittest.main()
