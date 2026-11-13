"""Frozen runtime acquisition composition and early-failure evidence."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


import runpy
runpy.run_path(str(Path(__file__).resolve().parents[1] / "python/run.py"))["bootstrap"]()
from hrx_systems.preparation import command as PREP
SDK = PREP.SDK
from hrx_systems.reporting import package_failures as PACKAGE
FIXTURE = load(ROOT / 'tests/test_actions_runtime.py', 'integration_actions_fixture')


class ActionsIntegrationTests(unittest.TestCase):
    def test_acquisition_selects_once_and_stages_only_dependencies_after_hrx(self):
        settings, lock, _, _ = SDK.load_runtime(ROOT / 'config/runtime.json')
        archive = io.BytesIO()
        filename = 'hrx_loom-0.2.0-py3-none-manylinux_2_39_x86_64.whl'
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr(filename, FIXTURE.wheel_bytes())
        client = FIXTURE.Client([FIXTURE.run(1)])
        client.body = archive.getvalue()
        client.artifacts[1][0].update(size_in_bytes=len(client.body),
            digest='sha256:' + hashlib.sha256(client.body).hexdigest())
        qualifiers, observed = [], []

        def qualify(wheel, dependency_lock, environment, dependency_directory=None):
            qualifiers.append(dependency_directory)
            self.assertEqual(dependency_lock, lock)
            return {'name': 'hrx-loom', 'version': '0.2.0', 'requires_dist': []}, 'compatible'

        def dependency(root, kind, payload):
            self.assertEqual(kind, 'python')
            return {'kind': kind, 'filename': payload['filename'], 'downloaded_bytes': payload['size_bytes']}

        with tempfile.TemporaryDirectory() as directory, patch.object(SDK, 'acquire_payload', side_effect=dependency) as transfer:
            payloads = Path(directory) / 'payloads'
            snapshot, events = SDK.acquire_actions_runtime(settings, payloads,
                selector=FIXTURE.SELECTOR, environment=FIXTURE.ENV, qualifier=qualify,
                client=client, observe_source=observed.append)
            self.assertEqual(transfer.call_count, len(lock['wheels']))
            self.assertEqual(sum(isinstance(item, tuple) and item[0] == 'download' for item in client.calls), 1)
            self.assertEqual(sum(isinstance(item, str) and item.endswith('/commits/main') for item in client.calls), 1)
            self.assertEqual(qualifiers, [None, payloads / 'python'])
            self.assertEqual(observed[0]['requested_head_sha'], FIXTURE.HEAD)
            self.assertEqual(observed[-1]['tested_sha'], FIXTURE.OLD)
            self.assertTrue(snapshot['source']['artifact']['digest_verified'])
            self.assertEqual(events[0]['kind'], 'hrx')
            self.assertEqual(snapshot['wheel']['version'], '0.2.0')
            baseline = SDK.runtime_baseline(settings, snapshot)
            self.assertIsNone(baseline['sdk']['release'])
            self.assertEqual(baseline['sdk']['wheel']['url'], snapshot['wheel']['source_url'])

    def fixture(self, root):
        for name in ('huggingface', 'loom-assets'):
            (root / 'cache' / name).mkdir(parents=True)
        return SimpleNamespace(root=root, suite=None, selection=None, output=root / 'prepared',
                               runtime=None, runtime_baseline=None, full_integrity=False,
                               runtime_source=None)

    def test_missing_runtime_and_invalid_lock_preserve_explicit_eight_denominator(self):
        for problem in ('missing_runtime', 'invalid_lock'):
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                args = self.fixture(root)
                args.runtime = root / 'runtime.json'
                if problem == 'invalid_lock':
                    settings = json.loads((ROOT / 'config/runtime.json').read_text())
                    args.runtime.write_text(json.dumps(settings))
                    (root / settings['sdk']['dependency_lock']).write_text('{}')
                with patch.object(PREP.subprocess, 'run') as child, \
                        patch.object(PREP, 'source_identity', return_value={'commit': 'fixture'}):
                    self.assertEqual(PREP.run_host(args), 1)
                    child.assert_not_called()
                frozen = json.loads((root / 'prepared/prepared-suite.json').read_text())
                self.assertEqual(len(frozen['entries']), 8)
                self.assertTrue(all(entry['selection'].get('model_id') for entry in frozen['entries']))
                self.assertTrue(all(entry['preparation']['status'] == 'failed' for entry in frozen['entries']))
                manifest, code = PACKAGE.package(root, root / 'failures')
                self.assertEqual(code, 0)
                self.assertEqual(manifest['counts']['expected'], 8)
                self.assertTrue((root / 'failures/shared/preparation-evidence/sdk-download/sdk-error.json').is_file())

    def test_actions_auth_failure_retains_request_and_never_claims_release_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.fixture(root)
            args.runtime_source = 'actions-main'

            def failed(settings, selector, output, attempt):
                selection = {'kind': 'github_actions', 'requested_head_sha': 'a' * 40,
                             'repository': 'ROCm/hrx-system'}
                attempt['runtime'] = {'source_selection': selection}
                PREP.write_json(output / 'actions-selection.json', selection)
                raise PREP.ACTIONS.ActionsError('Upstream permission failure', 'authentication')

            with patch.object(PREP.subprocess, 'run'), patch.object(PREP, 'actions_runtime', side_effect=failed), \
                    patch.object(PREP, 'source_identity', return_value={'commit': 'fixture'}):
                self.assertEqual(PREP.run_host(args), 1)
            frozen = json.loads((root / 'prepared/prepared-suite.json').read_text())
            self.assertEqual(len(frozen['entries']), 8)
            self.assertNotIn('sdk', frozen['runtime'])
            manifest, code = PACKAGE.package(root, root / 'failures')
            self.assertEqual(code, 0)
            self.assertEqual(manifest['counts']['expected'], 8)
            self.assertTrue((root / 'failures/shared/preparation-evidence/actions-selection.json').is_file())
            summary = (root / 'failures/summary.md').read_text()
            self.assertNotIn('hrx-loom 0.1.0', summary)
            self.assertIn('Unavailable', summary)

    def test_qualification_container_never_receives_source_token(self):
        with patch.object(PREP.subprocess, 'run') as child:
            child.return_value = SimpleNamespace(stdout='{}', stderr='', check_returncode=lambda: None)
            PREP.container_json({'image': 'pinned@sha256:' + 'a' * 64}, 'target-environment')
        command = child.call_args.args[0]
        self.assertIn('none', command)
        self.assertNotIn('HRX_ACTIONS_TOKEN', str(command))
        self.assertNotIn('--device', command)
