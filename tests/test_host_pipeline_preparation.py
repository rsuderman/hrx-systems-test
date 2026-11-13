"""Read-only pipeline-cache composition and sealed original-source evidence."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


F = load(ROOT / 'tests/test_pipeline_preparation.py', 'host_pipeline_fixture')
P, PIPE, RUN = F.PREP, F.PIPE, F.RUN
C = P.CACHE
from hrx_systems.cache import pipeline_writer as W


class HostPipelinePreparationTests(unittest.TestCase):
    def suite(self, root):
        for name in ('huggingface', 'loom-assets'):
            (root / 'cache' / name).mkdir(parents=True)
        first = P.read_json(ROOT / 'config/cached-model-tests.json')['entries'][0]
        entries = [{**first, 'id': name, 'seed_manifest': 'seed.json'} for name in ('one', 'two')]
        suite = root / 'suite.json'
        suite.write_text(json.dumps({'schema_version': 1, 'entries': entries}))
        baseline, _ = P.CONFIG.compose(entries[0])
        selection = P.selection_identity(baseline)
        card, lock, program = b'original fixture card', b'original fixture lock', b'original program'
        seed = {'schema_version': 1, 'selection': selection, 'card_sha256': C.sha(card),
                'lock_sha256': C.sha(lock), 'files': []}
        (root / 'cache/seed.json').write_text(json.dumps(seed))
        location = f"https://huggingface.co/{selection['repository']}/resolve/{selection['publication_revision']}/model.json"
        def identity(data):return {'size': len(data), 'sha256': C.sha(data)}
        manifest = {'schema_version': 1, 'selection': selection,
            'card': {'location': location, **identity(card)},
            'lock': {'location': location + '.lock', **identity(lock)},
            'sources': [{'installation_path': 'control.loom', 'roles': ['program'], 'origin': 'control.loom',
                         'resolved': None, 'identity_authority': 'host_seed', **identity(program)}],
            'origin_roots': {}, 'sdk_identity': {}}
        args = SimpleNamespace(root=root, suite=suite, output=root / 'prepared')
        return args, seed, manifest, {C.sha(data): data for data in (card, lock, program)}

    def test_missing_pipeline_cache_fails_each_entry_with_host_reseed_and_no_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args, _, _, _ = self.suite(root)
            with patch.object(P.subprocess, 'run'), patch.object(P.PROGRESS, 'run') as phase, \
                    patch.object(P, 'source_identity', return_value={'commit': 'fixture'}):
                self.assertEqual(P.run_host(args), 1)
                phase.assert_not_called()
            suite = P.read_json(root / 'prepared/prepared-suite.json')
            self.assertEqual(len(suite['entries']), 2)
            for entry in suite['entries']:
                self.assertEqual(entry['pipeline_cache']['error_kind'], 'missing_root')
                self.assertEqual(entry['preparation']['status'], 'failed')
                self.assertIn('bash scripts/cache/seed-pipelines.sh --root ' + str(root), entry['preparation']['reason'])
                self.assertIn('pipeline-cache-read.json', entry['evidence'][-1])

    def test_both_preparation_passes_are_offline_with_pipeline_cache_readonly_and_no_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args, _, manifest, payloads = self.suite(root)
            W.publish(root / 'cache/pipelines', manifest, payloads)
            before = {str(path.relative_to(root / 'cache/pipelines')): path.read_bytes()
                      for path in (root / 'cache/pipelines').rglob('*') if path.is_file()}
            with patch.object(P.subprocess, 'run'), patch.object(P.PROGRESS, 'run') as phase, \
                    patch.object(P, 'source_identity', return_value={'commit': 'fixture'}), \
                    patch.dict(P.os.environ, {'HF_TOKEN': 'must-not-cross-boundary'}):
                P.run_host(args)
            commands = [call.args[0] for call in phase.call_args_list]
            stages = [command for command in commands if '--stage' in command]
            self.assertEqual(len(stages), 2)
            for command in stages:
                self.assertEqual(command[command.index('--network') + 1], 'none')
                self.assertIn('/source/python/run.py', command)
                self.assertIn('prepare-cached-models', command)
                for source, target in P.layout.source_mounts():
                    self.assertIn(f'type=bind,src={source},dst={target},readonly', command)
                self.assertNotIn('/source/scripts', str(command))
                self.assertNotIn('HF_TOKEN', command)
                self.assertIn('type=bind,src=' + str(root / 'cache/pipelines') + ',dst=/model-cache/pipelines,readonly', command)
            after = {str(path.relative_to(root / 'cache/pipelines')): path.read_bytes()
                     for path in (root / 'cache/pipelines').rglob('*') if path.is_file()}
            self.assertEqual(before, after)
            suite = P.read_json(root / 'prepared/prepared-suite.json')
            self.assertEqual(suite['preparation_contract']['pipeline_source'], 'host_seeded')
            for entry in suite['entries']:
                self.assertEqual(entry['pipeline_cache']['status'], 'hit')
                self.assertTrue((root / 'prepared' / entry['pipeline_cache']['manifest_path']).is_file())

    def sealed(self, root, version):
        fixture = F.PipelinePreparationTests()
        destination, policy, manifest, baseline, entry, suite = fixture.fixture(root, version)
        location = 'https://huggingface.co/example/model/resolve/' + 'a' * 40 + '/model.json'
        source = destination / ('model-original' if policy['pipeline_upgrade']['enabled'] else 'model') / 'program/main.loom'
        cached = {'schema_version': 1, 'selection': entry['selection'],
            'card': {'location': location, 'size': (root / 'work/model.json').stat().st_size, 'sha256': manifest['card_sha256']},
            'lock': {'location': location + '.lock', 'size': (root / 'work/model.json.lock').stat().st_size, 'sha256': manifest['lock_sha256']},
            'sources': [{'installation_path': 'program/main.loom', 'roles': ['program'], 'origin': 'program/main.loom',
                         'resolved': None, 'size': source.stat().st_size, 'sha256': PIPE.digest(source), 'identity_authority': 'host_seed'}],
            'origin_roots': {}, 'sdk_identity': {}}
        PIPE.write_json(destination / 'pipeline-cache-entry.json', cached)
        proof = {'schema_version': 1, 'pipeline_source': 'host_seeded', 'status': 'hit',
            'requested_selection': entry['selection'], 'card_sha256': manifest['card_sha256'], 'lock_sha256': manifest['lock_sha256'],
            'entry_key': C.key_for(entry['selection'], manifest['card_sha256'], manifest['lock_sha256']),
            'manifest_path': 'models/one/pipeline-cache-entry.json', 'manifest_sha256': PIPE.digest(destination / 'pipeline-cache-entry.json'),
            'read_path': 'models/one/pipeline-cache-read.json', 'pipeline_network_requests': 0,
            'pipeline_downloaded_bytes': 0, 'pipeline_cache_writes': 0}
        PIPE.write_json(destination / 'pipeline-cache-read.json', proof)
        proof['read_sha256'] = PIPE.digest(destination / 'pipeline-cache-read.json')
        for value in (entry, manifest, baseline):value['pipeline_cache'] = copy.deepcopy(proof)
        manifest['programs'] = [{'path': 'program/main.loom', 'sha256': PIPE.digest(destination / 'model/program/main.loom')}]
        return manifest, baseline, entry

    def test_sealed_cache_proof_binds_originals_for_release_and_upgraded_targets(self):
        for version in ('0.1.0', '0.2.0.dev0'):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                manifest, baseline, entry = self.sealed(root, version)
                PIPE.verify_pipeline_cache(root, entry, manifest, baseline, required=True)
                (root / entry['pipeline_cache']['manifest_path']).write_text('{}')
                with self.assertRaisesRegex(ValueError, 'cache evidence changed'):
                    PIPE.verify_pipeline_cache(root, entry, manifest, baseline, required=True)

    def test_raw_cache_proof_is_self_contained_after_prepared_removal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, baseline, entry = self.sealed(root, '0.2.0.dev0')
            PIPE.verify_pipeline_cache(root, entry, manifest, baseline, required=True)
            proof = entry['pipeline_cache']
            entry['evidence'] = [proof['manifest_path'], proof['read_path']]
            result = {'evidence': [], 'models': [{'evidence': []}]}
            output = root / 'results'
            output.mkdir()
            RUN.copy_evidence(root, output, {'entries': [entry]}, result)
            original = (root / proof['manifest_path']).read_bytes()
            shutil.rmtree(root / 'models')
            self.assertEqual((output / proof['manifest_path']).read_bytes(), original)
            self.assertEqual(result['models'][0]['pipeline_cache'], proof)
            self.assertEqual(set(result['models'][0]['evidence']), set(entry['evidence']))

    def test_original_source_tampering_and_omitted_cache_proof_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, baseline, entry = self.sealed(root, '0.2.0.dev0')
            (root / 'models/one/model-original/program/main.loom').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'original source identity'):
                PIPE.verify_pipeline_cache(root, entry, manifest, baseline, required=True)
            manifest.pop('pipeline_cache')
            with self.assertRaisesRegex(ValueError, 'lacks pipeline cache'):
                PIPE.verify_pipeline_cache(root, entry, manifest, baseline, required=True)


if __name__ == '__main__':
    unittest.main()
