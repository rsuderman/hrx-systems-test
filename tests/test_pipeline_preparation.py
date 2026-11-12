"""Source-derivative identities and frozen runtime handoff integration."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location('test_' + name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PIPE = load('pipeline-preparation')
PREP = load('prepare-cached-models')
RUN = load('run-prepared-models')
SDK = PREP.SDK


def runtime(version):
    snapshot = SDK.resolve_runtime(ROOT / 'config/runtime.json')
    snapshot['wheel']['version'] = version
    snapshot['runtime_identity_sha256'] = SDK.runtime_identity_sha256(snapshot)
    return snapshot


class PipelinePreparationTests(unittest.TestCase):
    def fixture(self, root, version='0.2.0.dev0', upgrade='auto'):
        work, destination = root / 'work', root / 'models/one'
        work.mkdir(); (destination / 'model/program').mkdir(parents=True)
        card = {'schema_version': 1, 'presets': {'chat': {'execution': {'rows': 4, 'pool_capacity': 32}}}}
        text = json.dumps(card) + '\n'
        for path in (work / 'model.json', destination / 'model/model.json'):path.write_text(text)
        (work / 'model.json.lock').write_text('{"source":"verified-original"}\n')
        (destination / 'model/program/main.loom').write_text('%v = vector.constant 7 : vector<4xi32>\n')
        asset = root / 'payload.gguf'; asset.write_bytes(b'checkpoint bytes never transformed')
        (destination / 'model/weights.gguf').symlink_to(asset)
        seed = {'card_sha256': PIPE.digest(work / 'model.json'), 'lock_sha256': PIPE.digest(work / 'model.json.lock')}
        snapshot = runtime(version)
        policy, files = PIPE.freeze_policy(root, snapshot, pipeline_upgrade=upgrade)
        identity = {'repository': 'example/model', 'publication_revision': 'a'*40,
                    'model_id': 'Example', 'preset': 'chat', 'variant': 'gguf', 'programs': 'source'}
        baseline = {**identity, 'smoke_request': {'max_tokens': 256}, 'smoke_expected_text': '4',
                    'profile_identity': 'historical-profile', 'published_execution': card['presets']['chat']['execution']}
        baseline = PIPE.profile(baseline, policy)
        metadata = PIPE.seal_upgrade(destination, work, seed, policy, 'one')
        baseline['effective_execution'] = copy.deepcopy(baseline['published_execution'])
        if policy['pipeline_upgrade']['enabled']:baseline['effective_execution'].pop('pool_capacity')
        manifest = {**seed, 'pipeline_upgrade': metadata, 'published_execution': baseline['published_execution'],
                    'effective_execution': baseline['effective_execution'], 'effective_test_profile': baseline['effective_test_profile']}
        entry = {'id': 'one', 'selection': identity, 'pipeline_upgrade': metadata}
        suite = {'runtime': {'resolved_runtime': snapshot}, 'runtime_policy': policy, 'policy_files': files}
        return destination, policy, manifest, baseline, entry, suite

    def test_upgrade_preserves_publication_and_asset_but_executes_derived_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination, policy, manifest, baseline, entry, suite = self.fixture(root)
            self.assertEqual(PIPE.validate_policy(root, suite), policy)
            PIPE.verify_upgrade(root, entry, manifest, baseline, policy)
            self.assertIn('vector.splati', (destination / 'model/program/main.loom').read_text())
            self.assertIn('vector.constant', (destination / 'model-original/program/main.loom').read_text())
            self.assertNotIn('pool_capacity', json.loads((destination / 'model/model.json').read_text())['presets']['chat']['execution'])
            self.assertIn('pool_capacity', baseline['published_execution'])
            self.assertEqual((destination / 'model/weights.gguf').readlink(), root / 'payload.gguf')
            self.assertEqual((root / 'payload.gguf').read_bytes(), b'checkpoint bytes never transformed')
            self.assertEqual(PIPE.digest(destination / 'publication-card.json'), manifest['card_sha256'])

    def test_release_and_explicit_none_leave_source_and_request_unchanged(self):
        for version, mode in [('0.1.0','auto'),('0.2.0.dev0','none')]:
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                destination, policy, manifest, baseline, entry, suite = self.fixture(root, version, mode)
                PIPE.verify_upgrade(root, entry, manifest, baseline, policy)
                self.assertEqual(manifest['pipeline_upgrade']['status'], 'skipped')
                self.assertFalse((destination / 'model-original').exists())
                self.assertIn('vector.constant', (destination / 'model/program/main.loom').read_text())
                self.assertEqual(baseline['profile_identity'], 'historical-profile')
                self.assertEqual(baseline['smoke_request'], {'max_tokens':256})
                if version=='0.1.0':self.assertIsNone(policy['seccomp'])

    def test_source_report_publication_request_and_policy_tampering_is_rejected(self):
        for target in ['output','source','report','publication','request','policy','seccomp']:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary)
                destination,policy,manifest,baseline,entry,suite=self.fixture(root)
                if target=='output':(destination/'model/program/main.loom').write_text('changed')
                elif target=='source':(destination/'model-original/program/main.loom').write_text('changed')
                elif target=='report':(destination/'pipeline-upgrade.json').write_text('{}')
                elif target=='publication':(destination/'publication-card.json').write_text('{}')
                elif target=='request':baseline['smoke_request']['max_tokens']=1
                elif target=='policy':suite['runtime_policy']['launch']['memory_bytes']=1
                else:(root/'policies/seccomp.json').write_text('{}')
                with self.assertRaises(ValueError):
                    checked=PIPE.validate_policy(root,suite)
                    PIPE.verify_upgrade(root,entry,manifest,baseline,checked)

    def test_report_cannot_omit_original_publication_or_add_source_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            destination,policy,manifest,baseline,entry,suite=self.fixture(root)
            manifest['pipeline_upgrade']['publication']=[]
            with self.assertRaisesRegex(ValueError,'publication'):
                PIPE.verify_upgrade(root,entry,manifest,baseline,policy)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            destination,policy,manifest,baseline,entry,suite=self.fixture(root)
            (destination/'model/program/extra.loom').write_text('// unrecorded')
            with self.assertRaisesRegex(ValueError,'closure'):
                PIPE.verify_upgrade(root,entry,manifest,baseline,policy)

    def test_frozen_resolution_failure_keeps_denominator_without_docker_or_alias_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            for name in ('huggingface','loom-assets'):(root/'cache'/name).mkdir(parents=True)
            selection=root/'runtime-selection.json'
            selection.write_text(json.dumps({'schema_version':1,'status':'failed',
                'selector':{'kind':'release_latest','repository':'ROCm/hrx-system','mode':'latest-stable'},
                'error':{'kind':'resolution','message':'upstream metadata unavailable'}}))
            args=SimpleNamespace(root=root,suite=None,selection=None,output=root/'prepared',runtime=None,
                runtime_baseline=None,runtime_source=None,runtime_selection=selection,full_integrity=False)
            with patch.object(PREP.subprocess,'run') as child,patch.object(PREP,'source_identity',return_value={'commit':'fixture'}):
                self.assertEqual(PREP.run_host(args),1)
                child.assert_not_called()
            suite=json.loads((root/'prepared/prepared-suite.json').read_text())
            self.assertEqual(len(suite['entries']),8)
            self.assertTrue(all(e['preparation']['status']=='failed' for e in suite['entries']))
            self.assertEqual(json.loads((root/'prepared/sdk-download/sdk-error.json').read_text())['error_kind'],'resolution')
            run_args=SimpleNamespace(prepared=root/'prepared',output=root/'results',device_uri='amdgpu://0',workflow=None,cycles=2)
            with patch.object(RUN.subprocess,'run') as child:
                self.assertEqual(RUN.run_host(run_args),1)
                child.assert_not_called()
            result=json.loads((root/'results/suite-result.json').read_text())
            self.assertEqual(len(result['models']),8)
            self.assertTrue(all(e['status']=='failed' for e in result['models']))
            self.assertEqual(result['server']['cleanup']['status'],'passed')
            self.assertIn('runtime-selection.json', result['evidence'])
            self.assertEqual((root/'results/runtime-selection.json').read_bytes(), args.runtime_selection.read_bytes())

    def test_malformed_resolution_failure_rejects_unbounded_text_and_source(self):
        value={'schema_version':1,'status':'failed','selector':{'kind':'release_latest','repository':'ROCm/hrx-system','mode':'latest-stable'},'error':{'kind':'resolution','message':'failure'}}
        self.assertEqual(PIPE.failed_selection(value),value)
        for bad in [{**value,'source':{}},{**value,'error':{'kind':'resolution','message':'x'*4097}}]:
            with self.assertRaises(ValueError):PIPE.failed_selection(bad)


if __name__=='__main__':unittest.main()
