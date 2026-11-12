"""Read-only native preparation fixtures plus small host contract checks."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('prepare_cached_models', ROOT / 'scripts/prepare-cached-models.py')
PREP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREP)
try:
    import loom
except ImportError:
    loom = None


class HostContractTests(unittest.TestCase):
    def test_batch_stages_install_sdk_once_and_share_the_environment(self):
        with patch.object(PREP.subprocess, 'run') as child:
            child.return_value.returncode = 0
            for operation in ('batch-online', 'batch-offline'):
                self.assertEqual(PREP.stage(type('Args', (), {'stage': operation})()), 0)
        commands = [call.args[0] for call in child.call_args_list]
        self.assertEqual(sum('install' in command for command in commands), 1)
        self.assertEqual([command[-1] for command in commands if '--native-batch' in command], ['online', 'offline'])
        self.assertTrue(all(command[0] == '/run/venv/bin/python' for command in commands if '--native-batch' in command))

    def test_batch_online_progress_reports_actual_success_and_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture_root = Path(directory)
            prepared = fixture_root / 'prepared'
            model_dir = prepared / 'models/example'
            model_dir.mkdir(parents=True)
            (prepared / 'sdk-prepare').mkdir()
            (prepared / 'sdk-prepare/sdk.json').write_text('{}')
            real_copy = PREP.shutil.copyfile
            def mapped_path(value):
                value = str(value)
                return fixture_root / value.lstrip('/') if value.startswith(('/prepared', '/run')) else Path(value)
            def copy(source, destination):
                return real_copy(mapped_path(source), destination)
            def batch(reason=None):
                suite = {'schema_version': 1, 'entries': [{'id': 'example',
                    'baseline': 'config/example/baseline.json',
                    'selection': {'model_id': 'Example'}, 'evidence': [],
                    'preparation': {'status': 'failed', 'reason': 'preparation not reached'}}]}
                output = io.StringIO()
                def read(path):
                    return ['example'] if str(path).endswith('batch-entry-ids.json') else suite
                with patch.object(PREP, 'Path', side_effect=mapped_path), \
                        patch.object(PREP, 'read_json', side_effect=read), \
                        patch.object(PREP.ASSETS, 'load_index', return_value={}), \
                        patch.object(PREP.shutil, 'copyfile', side_effect=copy), \
                        patch.object(PREP.SDK, 'load_baseline', return_value=({'smoke_request': {},
                            'smoke_expected_text': '4', 'published_execution': {}}, None, None, {'baseline_sha256': 'fixture'})), \
                        patch.object(PREP, 'native', side_effect=reason), redirect_stdout(output):
                    PREP.native_batch('online')
                return output.getvalue()
            good = batch()
            self.assertIn('prepare 1/1 online: Example: pipeline fetched; assets available', good)
            self.assertNotIn('preparation not reached', good)
            # The batch uses an existing per-operation SDK directory on a repeat.
            bad = batch(RuntimeError('fixture fetch failed'))
            self.assertIn('FAIL online: RuntimeError: fixture fetch failed', bad)
            self.assertNotIn('pipeline fetched; assets available', bad)

    def test_path_rejects_parent_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'root'
            root.mkdir()
            outside = Path(directory) / 'outside'
            outside.write_text('immutable')
            (root / 'link').symlink_to(outside)
            for path in ('../outside', 'link', str(outside)):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    PREP.contained(root, path)

    def test_empty_and_duplicate_suites_fail_before_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            suite = root / 'suite.json'
            for entries in ([], [{'id': 'same'}, {'id': 'same'}]):
                suite.write_text(json.dumps({'schema_version': 1, 'entries': entries}))
                args = type('Args', (), {'root': root, 'suite': suite, 'output': root / 'output'})()
                with patch.object(PREP.subprocess, 'run') as run, self.assertRaises(ValueError):
                    PREP.run_host(args)
                run.assert_not_called()


    def test_invalid_entries_are_preserved_with_known_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('huggingface', 'loom-assets'):
                (root / 'cache' / name).mkdir(parents=True)
            suite = root / 'suite.json'
            suite.write_text(json.dumps({'schema_version': 1, 'entries': [
                {'id': 'bad-baseline', 'baseline': 'config/missing.json', 'seed_manifest': 'missing.json'},
                {'id': 'missing-seed', 'baseline': 'config/single-model-e2e.json', 'seed_manifest': 'missing.json'}]}))
            args = type('Args', (), {'root': root, 'suite': suite, 'output': root / 'prepared'})()
            with patch.object(PREP.subprocess, 'run'), patch.object(PREP, 'source_identity', return_value={'commit': 'fixture'}):
                self.assertEqual(PREP.run_host(args), 1)
            result = PREP.read_json(root / 'prepared/prepared-suite.json')
            self.assertEqual([e['id'] for e in result['entries']], ['bad-baseline', 'missing-seed'])
            self.assertTrue(all(e['preparation']['status'] == 'failed' for e in result['entries']))
            self.assertEqual(result['entries'][1]['selection']['model_id'], 'Qwen/Qwen3.5-0.8B')
            self.assertIsNone(result['runtime'])


    def test_interruption_preserves_every_expected_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('huggingface', 'loom-assets'):
                (root / 'cache' / name).mkdir(parents=True)
            suite = root / 'suite.json'
            suite.write_text(json.dumps({'schema_version': 1, 'entries': [
                {'id': name, 'baseline': 'config/single-model-e2e.json', 'seed_manifest': 'seed-manifest.json'}
                for name in ('one', 'two', 'three')]}))
            args = type('Args', (), {'root': root, 'suite': suite, 'output': root / 'prepared'})()
            with patch.object(PREP.subprocess, 'run'), patch.object(PREP, 'source_identity', return_value={'commit': 'fixture'}), \
                    patch.object(PREP.SDK, 'load_baseline', side_effect=KeyboardInterrupt):
                self.assertEqual(PREP.run_host(args), 1)
            result = PREP.read_json(root / 'prepared/prepared-suite.json')
            self.assertEqual([e['id'] for e in result['entries']], ['one', 'two', 'three'])
            self.assertTrue(all(e['preparation']['reason'] == 'preparation interrupted' for e in result['entries']))


@unittest.skipUnless(loom, 'requires installed pinned SDK; run native-container gate')
class NativePreparationTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('seed_fixture', ROOT / 'tests/test_seed_models.py')
        fixture_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture_module)
        self.fixture = fixture_module.NativeSeedTests('test_repeat_seed_refreshes_program_and_reuses_verified_assets_offline')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.cache = self.fixture.root, self.fixture.cache
        self.baseline = self.fixture.baseline
        _, _, self.seed = self.fixture.attempt('seed')

    def prepare(self, name='prepared'):
        work, output = self.root / (name + '-work'), self.root / name
        work.mkdir()
        output.mkdir()
        PREP.online(self.baseline, self.seed, self.cache, work, location=self.fixture.url)
        manifest = PREP.offline(self.baseline, self.seed, self.cache, work, output, 'tiny', {'fixture': True})
        return output, manifest

    def test_fresh_code_and_offline_verified_assets_keep_cache_unchanged(self):
        before = {str(p.relative_to(self.cache)): p.read_bytes() for p in self.cache.rglob('*') if p.is_file()}
        (self.fixture.remote / 'control.loom').write_bytes(b'fresh pipeline for preparation')
        count = len(self.fixture.requests)
        output, manifest = self.prepare()
        requests = self.fixture.requests[count:]
        self.assertIn('/control.loom', requests)
        self.assertNotIn('/model.gguf', requests)
        self.assertNotIn('/tokenizer.json', requests)
        self.assertEqual((output / 'model/control.loom').read_bytes(), b'fresh pipeline for preparation')
        self.assertEqual(manifest['model_card_path'], '/inputs/tiny/model/model.json')
        self.assertEqual(manifest['asset_phase_network'], 'disabled')
        self.assertEqual(len(manifest['assets']), 2)
        after = {str(p.relative_to(self.cache)): p.read_bytes() for p in self.cache.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_corrupt_asset_fails_without_ready_manifest(self):
        item = self.seed['files'][0]
        (self.cache / item['cache_root'] / item['cache_path']).write_bytes(b'corrupt')
        with self.assertRaisesRegex(Exception, 'integrity|SHA-256'):
            self.prepare()
        self.assertFalse((self.root / 'prepared/prepared-inputs.json').exists())

    def test_missing_asset_fails_without_downloading_replacement(self):
        item = self.seed['files'][0]
        (self.cache / item['cache_root'] / item['cache_path']).unlink()
        before = len(self.fixture.requests)
        with self.assertRaises(FileNotFoundError):
            self.prepare()
        self.assertNotIn('/model.gguf', self.fixture.requests[before:])
        self.assertNotIn('/tokenizer.json', self.fixture.requests[before:])

    def test_asset_path_escape_is_rejected(self):
        self.seed['files'][0]['cache_path'] = '../../remote/model.gguf'
        with self.assertRaisesRegex(ValueError, 'designated root'):
            self.prepare()

    def test_missing_seed_lock_coverage_is_rejected(self):
        self.seed['files'].pop()
        with self.assertRaisesRegex(ValueError, 'asset closure'):
            self.prepare()

    def test_changed_publication_lock_is_rejected(self):
        self.seed['lock_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'card/lock disagree'):
            self.prepare()

    def test_index_identity_disagreement_is_rejected(self):
        self.seed['files'][0]['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'identity/revision'):
            self.prepare()

    def test_cached_text_derives_published_execution_and_reuses_assets(self):
        card = json.loads((self.fixture.remote / 'model.json').read_text())
        card['presets']['chat']['execution'] = {'rows': 2, 'context_capacity': 128}
        (self.fixture.remote / 'model.json').write_text(json.dumps(card))
        _, _, seed = self.fixture.attempt('cached-seed')
        baseline = {k: v for k, v in self.baseline.items()
                    if k not in ('published_execution', 'smoke_request', 'smoke_expected_text')}
        baseline['selection_mode'] = 'cached'
        work, output = self.root / 'cached-work', self.root / 'cached-output'
        work.mkdir(); output.mkdir()
        count = len(self.fixture.requests)
        normalized = PREP.online(baseline, seed, self.cache, work, location=self.fixture.url)
        manifest = PREP.offline(normalized, seed, self.cache, work, output, 'cached-fixture', {'fixture': True})
        self.assertEqual(manifest['effective_execution'], card['presets']['chat']['execution'])
        self.assertEqual(manifest['test_kind'], 'text_smoke')
        self.assertNotIn('/model.gguf', self.fixture.requests[count:])

    def test_cached_decision_uses_native_card_selection_and_offline_asset_path(self):
        card = json.loads((self.fixture.remote / 'model.json').read_text())
        card['services']['chat']['kind'] = 'decision'
        card['presets']['chat']['execution'] = {'pending_requests': 64}
        (self.fixture.remote / 'model.json').write_text(json.dumps(card))
        _, _, seed = self.fixture.attempt('decision-seed')
        baseline = {k: v for k, v in self.baseline.items()
                    if k not in ('published_execution', 'smoke_request', 'smoke_expected_text')}
        baseline['selection_mode'] = 'cached'
        work, output = self.root / 'decision-work', self.root / 'decision-output'
        work.mkdir(); output.mkdir()
        normalized = PREP.online(baseline, seed, self.cache, work, location=self.fixture.url)
        manifest = PREP.offline(normalized, seed, self.cache, work, output, 'decision-fixture', {'fixture': True})
        self.assertEqual(manifest['test_kind'], 'decision_smoke')
        self.assertEqual(manifest['effective_execution'], {'pending_requests': 64})
        self.assertEqual(manifest['smoke_expected_text'], 'support')


if __name__ == '__main__':
    unittest.main()
