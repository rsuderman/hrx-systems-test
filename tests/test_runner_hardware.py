"""SDK-free physical identity and non-mutating runner registration contracts."""
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
runpy.run_path(str(ROOT / 'python/run.py'))['bootstrap']()
from hrx_systems.host import runner_hardware as H


class RunnerHardwareTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.devices = self.root / 'dev'
        self.sysfs = self.root / 'drm'
        self.kfd = self.root / 'kfd'
        self.devices.mkdir()
        (self.devices / 'renderD128').touch()
        device = self.sysfs / 'renderD128/device'
        device.mkdir(parents=True)
        (device / 'uevent').write_text('DRIVER=amdgpu\nPCI_SLOT_NAME=0000:01:00.0\n')
        self.node(0, 0)
        self.node(1, 123)

    def node(self, number, gpu_id, target=110501, minor=128):
        node = self.kfd / str(number)
        node.mkdir(exist_ok=True, parents=True)
        (node / 'gpu_id').write_text(str(gpu_id))
        (node / 'properties').write_text(f'drm_render_minor {minor}\ngfx_target_version {target}\n')

    def check(self, label='hrx-gfx1151', uri='amdgpu://0'):
        return H.check(label, uri, sysfs=self.sysfs, devices=self.devices, kfd=self.kfd)

    def test_matching_raw_target_passes_without_runtime_qualification(self):
        result = self.check()
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(result['gfx_target_version'], 110501)
        self.assertEqual(set(result), {'requested_label', 'device_uri', 'render_node',
                                     'gfx_target_version', 'status', 'reason'})

    def test_unknown_invalid_and_other_index_fail(self):
        for label, uri in [('hrx-gfx9999', 'amdgpu://0'), ('', 'amdgpu://0'),
                           ('hrx-gfx1151,other', 'amdgpu://0'), ('HRX-gfx1151', 'amdgpu://0'),
                           ('hrx-gfx1151', 'amdgpu://1'), ('hrx-gfx1151', 'cpu://0')]:
            with self.subTest(label=label, uri=uri):
                self.assertEqual(self.check(label, uri)['status'], 'failed')

    def test_missing_extra_and_mismatched_physical_identity_fail(self):
        changes = [lambda: (self.devices / 'renderD128').unlink(),
                   lambda: (self.devices / 'renderD129').touch(),
                   lambda: self.node(1, 123, target=110500),
                   lambda: self.node(1, 0), lambda: self.node(2, 456),
                   lambda: self.node(1, 123, minor=129),
                   lambda: (self.kfd / '1/properties').unlink(),
                   lambda: (self.kfd / '1/gpu_id').write_text('unknown'),
                   lambda: (self.sysfs / 'renderD128/device/uevent').write_text('DRIVER=other\n')]
        for change in changes:
            with self.subTest(change=change):
                self.setUp()
                change()
                self.assertEqual(self.check()['status'], 'failed')

    def test_legacy_label_is_no_longer_an_enabled_registration_target(self):
        self.assertEqual(H.check('hrx-systems-test-bootstrap', devices=Path('/nonexistent'))['status'], 'failed')

    def registration(self, label=None, registered=False):
        state = self.root / 'state'
        runner = state / 'runner'
        runner.mkdir(parents=True)
        (runner / 'config.sh').write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$HRX_SYSTEMS_ROOT/arguments"\n')
        (runner / 'config.sh').chmod(0o700)
        if registered:
            (runner / '.runner').write_text('preserved')
        bins = self.root / 'bin'
        bins.mkdir()
        # Fake only host account and Python invocation; never touch runner state.
        (bins / 'id').write_text('#!/bin/bash\necho 1000\n')
        (bins / 'python3').write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$HRX_SYSTEMS_ROOT/helper-arguments"\nexit "${CHECK_EXIT:-0}"\n')
        for path in bins.iterdir():
            path.chmod(0o700)
        env = dict(os.environ, HRX_SYSTEMS_ROOT=str(state), HRX_RUNNER_NAME='fixture-runner',
                   PATH=str(bins) + os.pathsep + os.environ['PATH'])
        env.pop('HRX_RUNNER_LABEL', None)
        if label is not None:
            env['HRX_RUNNER_LABEL'] = label
        command = ['bash', str(ROOT / 'scripts/host/bootstrap-runner.sh'), 'register',
                   'https://github.com/OWNER/REPO']
        return state, env, command

    def test_registration_forwards_one_label_and_leaves_token_interactive(self):
        state, env, command = self.registration('hrx-gfx1151')
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((state / 'arguments').read_text().splitlines(),
                         ['--url', 'https://github.com/OWNER/REPO', '--name', 'fixture-runner',
                          '--labels', 'hrx-gfx1151', '--work', str(state / 'work')])
        self.assertEqual((state / 'helper-arguments').read_text().splitlines()[-2:], ['--label', 'hrx-gfx1151'])
        self.assertNotIn('--token', (state / 'arguments').read_text())

    def test_registration_requires_an_explicit_hardware_label(self):
        state, env, command = self.registration()
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('set HRX_RUNNER_LABEL', result.stderr)
        self.assertFalse((state / 'arguments').exists())
        self.assertFalse((state / 'helper-arguments').exists())

    def test_registration_rejects_retired_label_through_real_sdk_free_helper(self):
        state, env, command = self.registration('hrx-systems-test-bootstrap')
        (self.root / 'bin/python3').unlink()
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unsupported hardware label', result.stdout)
        self.assertFalse((state / 'arguments').exists())

    def test_preflight_failure_does_not_register(self):
        state, env, command = self.registration('hrx-gfx1151')
        env['CHECK_EXIT'] = '1'
        self.assertNotEqual(subprocess.run(command, env=env, capture_output=True).returncode, 0)
        self.assertFalse((state / 'arguments').exists())

    def test_registered_state_is_preserved_before_helper_or_config(self):
        state, env, command = self.registration('hrx-gfx1151', registered=True)
        self.assertNotEqual(subprocess.run(command, env=env, capture_output=True).returncode, 0)
        self.assertEqual((state / 'runner/.runner').read_text(), 'preserved')
        self.assertFalse((state / 'arguments').exists())
        self.assertFalse((state / 'helper-arguments').exists())


class SmokeWorkflowTests(unittest.TestCase):
    def test_storage_step_uses_disposable_root_and_preserves_shared_cache(self):
        workflow = (ROOT / '.github/workflows/runner-bootstrap.yml').read_text()
        step = workflow.split('      - name: Check container storage boundary\n', 1)[1].split('      - name:', 1)[0]
        self.assertIn('HRX_SYSTEMS_ROOT: ${{ runner.temp }}/hrx-runner-storage-${{ github.run_id }}-${{ github.run_attempt }}', step)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shared = root / 'shared-cache'
            shared.mkdir()
            (shared / 'immutable').write_text('retained')
            binary = root / 'bin'
            binary.mkdir()
            docker = binary / 'docker'
            docker.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
with open(os.environ['DOCKER_CALLS'], 'a') as stream: stream.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1] == 'run':
 for arg in sys.argv:
  if arg.startswith('type=bind,src=') and arg.endswith(',dst=/results'):
   root = pathlib.Path(arg.removeprefix('type=bind,src=').removesuffix(',dst=/results'))
   (root / 'status.txt').write_text('storage-check-passed\\n')
''')
            docker.chmod(0o700)
            scratch = root / 'runner-temp/hrx-runner-storage-123-1'
            command = step.split('        run: ', 1)[1].strip()
            result = subprocess.run(['bash', '-c', command], cwd=ROOT,
                env=dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'],
                         HRX_SYSTEMS_ROOT=str(scratch), DOCKER_CALLS=str(root / 'calls')), text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in (root / 'calls').read_text().splitlines()]
            self.assertIn('--read-only', calls[0])
            self.assertIn('--cap-drop', calls[0])
            self.assertTrue(any(str(scratch) in argument and argument.endswith(',dst=/assets,readonly') for argument in calls[0]))
            self.assertEqual(calls[-1][:2], ['rm', '-f'])
            self.assertEqual(list((scratch / 'cache/bootstrap').iterdir()), [])
            self.assertEqual(list((scratch / 'results').iterdir()), [])
            self.assertEqual(list(shared.iterdir()), [shared / 'immutable'])
            self.assertEqual((shared / 'immutable').read_text(), 'retained')

    def test_smoke_choices_use_only_enabled_hardware_and_single_selector(self):
        for name in ('runner-pickup.yml', 'runner-bootstrap.yml'):
            text = (ROOT / '.github/workflows' / name).read_text()
            self.assertIn('default: hrx-gfx1151', text)
            self.assertIn('type: choice', text)
            self.assertIn('options:\n          - hrx-gfx1151', text)
            self.assertNotIn('hrx-systems-test-bootstrap', text)
            self.assertIn('runs-on: ${{ inputs.hardware }}', text)
            self.assertIn('HRX_RUNNER_LABEL: ${{ inputs.hardware }}', text)
            self.assertIn('requested_label=%s', text)
        bootstrap = (ROOT / '.github/workflows/runner-bootstrap.yml').read_text()
        self.assertIn('bootstrap-runner.sh check', bootstrap)
        self.assertIn('check-container-storage.sh', bootstrap)
        self.assertIn('if: always()', bootstrap)


if __name__ == '__main__':
    unittest.main()
