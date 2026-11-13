"""Frozen SDK selection reaches acquisition without another alias lookup."""
import hashlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SDK = load(ROOT / 'scripts/sdk-environment.py', 'pipeline_sdk')
R = load(ROOT / 'tests/test_release_runtime.py', 'pipeline_release_fixture')
F = R.F


class FrozenSdkAdapterTests(unittest.TestCase):
    def test_frozen_actions_and_release_download_exact_payload_without_resolving_again(self):
        settings, lock, _, _ = SDK.load_runtime(ROOT / 'config/runtime.json')
        for kind in ('actions', 'release'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                if kind == 'release':
                    client = R.Client()
                    client.body = F.wheel_bytes()
                    client.latest['assets'][0].update(size=len(client.body), digest='sha256:' + hashlib.sha256(client.body).hexdigest())
                    selector = R.R.SELECTOR
                else:
                    client = F.Client([F.run(1)])
                    archive = io.BytesIO()
                    with zipfile.ZipFile(archive, 'w') as bundle:
                        bundle.writestr(R.NAME, F.wheel_bytes())
                    client.body = archive.getvalue()
                    client.artifacts[1][0].update(size_in_bytes=len(client.body), digest='sha256:' + hashlib.sha256(client.body).hexdigest())
                    selector = F.SELECTOR
                frozen = R.R.freeze_selection(selector, client=client, now=F.NOW)
                client.calls.clear()
                qualified = []

                def qualifier(wheel, dependency_lock, environment, dependency_directory=None):
                    self.assertEqual(dependency_lock, lock)
                    qualified.append(dependency_directory)
                    return {'name': 'hrx-loom', 'version': '0.2.0', 'requires_dist': []}, 'compatible'

                with patch.object(SDK, 'acquire_payload', return_value={'downloaded_bytes': 0}):
                    snapshot, events = SDK.acquire_actions_runtime(settings, Path(temporary) / 'payloads',
                        selector=selector, selection=frozen, client=client, environment=F.ENV, qualifier=qualifier)
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(client.calls[0][0], 'download')
                self.assertEqual(snapshot['source']['tested_sha'], frozen['source']['tested_sha'])
                self.assertEqual(snapshot['wheel']['version'], '0.2.0')
                self.assertEqual(events[0]['downloaded_bytes'], len(client.body))
                self.assertEqual(qualified, [None, Path(temporary) / 'payloads/python'])
                adapter = SDK.runtime_baseline(settings, snapshot)
                self.assertEqual(adapter['sdk']['source_commit'], frozen['source']['tested_sha'])
                self.assertEqual(adapter['sdk']['release'], 'v0.2.0' if kind == 'release' else None)
                # Compose the real verified acquisition fixture with frozen ABI
                # policy, the migration adapter and offline prepared verification.
                from test_pipeline_preparation import PipelinePreparationTests, PIPE
                composed = Path(temporary) / 'prepared'; composed.mkdir()
                dest, policy, manifest, baseline, entry, suite = PipelinePreparationTests().fixture(
                    composed, snapshot_override=snapshot, abi_profile='state' if kind == 'actions' else 'legacy', epoch=True)
                self.assertEqual(policy['publication_abi']['receipt']['runtime_identity_sha256'], snapshot['runtime_identity_sha256'])
                with patch('urllib.request.urlopen', side_effect=AssertionError('offline verification must not acquire anything')):
                    PIPE.verify_upgrade(composed, entry, manifest, baseline, PIPE.validate_policy(composed, suite))
                self.assertEqual(policy['publication_abi']['receipt']['profile'], 'control-state-13' if kind == 'actions' else 'legacy-12')


if __name__ == '__main__':
    unittest.main()
