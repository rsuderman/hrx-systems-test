"""Advisory qualification from immutable metadata, without model/SDK execution."""
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import runpy
runpy.run_path(str(Path(__file__).resolve().parents[1] / "python/run.py"))["bootstrap"]()
from hrx_systems.host import hardware_advisory as QUALIFY
PROVENANCE = {'method': 'reviewed fixture evidence', 'reference': 'fixture://synthetic-only',
              'reviewer': 'fixture reviewer', 'observed_at': '2026-11-11T00:00:00Z'}


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = {'sdk_identity': {'dependency_lock_sha256': 'a'*64, 'base_image': 'image@sha256:'+'b'*64,
                        'sdk': {'name': 'hrx-loom', 'version': 'fixture'}, 'wheel_sha256': 'c'*64}}
        self.entry = {'id': 'example', 'selection': {'repository': 'loom-fun/example',
                      'publication_revision': 'd'*40, 'model_id': 'example', 'preset': 'chat',
                      'variant': 'gguf', 'programs': 'source'},
                      'effective_execution': {'context_length': 128}, 'smoke_request': {'max_tokens': 32},
                      'test_kind': 'text_smoke', 'preparation': {'status': 'ready'}}
        self.suite = {'schema_version': 1, 'attempt_id': 'fixture', 'runtime': self.runtime,
                      'entries': [self.entry], 'inventory_sha256': 'e'*64}
        self.identity = QUALIFY.runtime_identity(self.runtime)
        self.hardware = {'device_uri': 'amdgpu://0', 'pci_slot': '0000:01:00.0', 'driver': 'amdgpu',
                         'canonical_target': 'gfx1100', 'memory_topology': 'dedicated', 'driver_version': 'fixture-driver',
                         'mapping_qualified': True, 'memory_qualified': True,
                         'usable_total_bytes': 1000, 'usable_free_bytes': 1000,
                         'target_catalog': {'runtime_identity': self.identity, 'canonical_target': 'gfx1100',
                         'complete': True, 'provenance': PROVENANCE,
                         'compatible_targets': [{'target': 'gfx1100', 'features': ['feature-a']},
                                                {'target': 'gfx11-generic', 'features': []}]}}
        self.manifest = {'selection': self.entry['selection'], 'programs': [{'path': 'control.loom', 'sha256': 'f'*64}],
                         'assets': [{'installation_path': 'weights.gguf', 'size': 123}]}
        self.profile = {'selection': self.entry['selection'], 'runtime_identity': self.identity,
                        'workload_identity': QUALIFY.workload_identity(self.entry, 2),
                        'hardware': {k: self.hardware[k] for k in ('canonical_target', 'driver', 'driver_version', 'memory_topology')},
                        'provenance': PROVENANCE, 'programs': [{'path': 'control.loom',
                        'targets': [{'target': 'gfx1100', 'features': ['feature-a']}]}],
                        'memory': {'peak_bytes': 600, 'margin_bytes': 100,
                        'components': {'weights': 400, 'context_state': 100, 'companions': 0, 'workspace_runtime': 100},
                        'provenance': PROVENANCE},
                        'runtime_compatibility': {'status': 'pass', 'provenance': PROVENANCE}}

    def evaluate(self, profiles=None, **kwargs):
        return QUALIFY.evaluate(self.suite, profiles or {'schema_version': 1, 'profiles': [self.profile]},
                                self.hardware, {'example': self.manifest}, storage_free=10**9, **kwargs)

    def test_exact_and_catalog_generic_match(self):
        for target in ('gfx1100', 'gfx11-generic'):
            self.profile['programs'][0]['targets'] = [{'target': target}]
            report = self.evaluate()
            self.assertEqual(report['entries'][0]['recommendation'], 'seed')
            self.assertTrue(report['entries'][0]['runnable_now'])
            self.assertEqual(report['entries'][0]['required_bytes'], 700)

    def test_present_malformed_runtime_snapshot_never_uses_legacy_identity(self):
        for value in (None, [], 'bad'):
            runtime = {**self.runtime, 'resolved_runtime': value}
            with self.assertRaisesRegex(ValueError, 'resolved runtime identity must be an object'):
                QUALIFY.runtime_identity(runtime)

    def test_incompatible_targets_no_prefix_matching(self):
        for target in ('gfx1101', 'gfx12-generic', 'gfx11-unpublished', 'amdgpu-rdna3-5'):
            self.profile['programs'][0]['targets'] = [{'target': target}]
            entry = self.evaluate()['entries'][0]
            self.assertEqual(entry['recommendation'], 'skip')
            self.assertEqual(entry['checks']['pipeline']['reason'], 'pipeline_target_incompatible')

    def test_features_and_incomplete_catalog_unknown(self):
        self.profile['programs'][0]['targets'][0]['features'] = ['not-supported']
        self.assertEqual(self.evaluate()['entries'][0]['checks']['pipeline']['status'], 'fail')
        self.hardware['target_catalog']['complete'] = False
        self.assertEqual(self.evaluate()['entries'][0]['recommendation'], 'needs_information')

    def test_no_catalog_cannot_even_certify_exact_target(self):
        self.hardware.pop('target_catalog')
        entry = self.evaluate()['entries'][0]
        self.assertEqual(entry['checks']['pipeline']['status'], 'unknown')
        self.assertEqual(entry['recommendation'], 'needs_information')

    def test_all_programs_require_coverage(self):
        self.manifest['programs'].append({'path': 'kernels.loom'})
        self.assertEqual(self.evaluate()['entries'][0]['checks']['pipeline']['reason'], 'program_target_coverage_incomplete')
        self.profile['programs'].append({'path': 'kernels.loom', 'target_independent': True})
        self.assertEqual(self.evaluate()['entries'][0]['recommendation'], 'seed')
        self.profile['programs'].append({'path': 'missing.loom', 'target_independent': True})
        self.assertEqual(self.evaluate()['entries'][0]['checks']['pipeline']['status'], 'fail')

    def test_total_capacity_busy_and_reserve_applied_once(self):
        self.hardware['usable_total_bytes'] = 799
        self.hardware['usable_free_bytes'] = 799
        entry = self.evaluate(reserve=100)['entries'][0]
        self.assertEqual(entry['recommendation'], 'skip')
        self.assertEqual(entry['checks']['memory']['reason'], 'insufficient_capacity')
        self.hardware['usable_total_bytes'] = 800
        self.hardware['usable_free_bytes'] = 799
        entry = self.evaluate(reserve=100)['entries'][0]
        self.assertEqual(entry['recommendation'], 'seed')
        self.assertFalse(entry['runnable_now'])
        self.assertEqual(entry['checks']['memory']['reason'], 'busy')
        self.hardware['usable_free_bytes'] = 800
        self.assertTrue(self.evaluate(reserve=100)['entries'][0]['runnable_now'])

    def test_shared_pool_is_never_sum_of_raw_counters(self):
        self.hardware.update(memory_topology='shared', memory_qualified=False,
                             usable_total_bytes=None, usable_free_bytes=None,
                             observed_counters={'mem_info_vram_total': 512, 'mem_info_gtt_total': 100000})
        self.profile['hardware']['memory_topology'] = 'shared'
        entry = self.evaluate()['entries'][0]
        self.assertEqual(entry['checks']['memory']['reason'], 'shared_pool_budget_unqualified')
        self.assertIsNone(entry['runnable_now'])

    def test_missing_stale_profiles_never_remove_inventory(self):
        for field in ('runtime_identity', 'workload_identity'):
            original = self.profile[field]
            self.profile[field] = 'stale'
            report = self.evaluate()
            self.assertEqual(len(report['entries']), 1)
            self.assertEqual(report['entries'][0]['recommendation'], 'needs_information')
            self.profile[field] = original
        report = self.evaluate({'schema_version': 1, 'profiles': []})
        self.assertEqual(report['entries'][0]['recommendation'], 'needs_information')
        self.assertEqual(self.suite['entries'], [self.entry])

    def test_known_capacity_with_unknown_free_memory_does_not_claim_ready(self):
        self.hardware['usable_free_bytes'] = None
        entry = self.evaluate()['entries'][0]
        self.assertEqual(entry['recommendation'], 'seed')
        self.assertIsNone(entry['runnable_now'])
        self.assertEqual(entry['checks']['memory']['reason'], 'capacity_fit_free_memory_unqualified')

    def test_runtime_compatibility_and_output_storage(self):
        self.profile['runtime_compatibility']['status'] = 'fail'
        self.assertEqual(self.evaluate()['entries'][0]['checks']['runtime']['status'], 'fail')
        self.profile['runtime_compatibility'] = {}
        self.assertEqual(self.evaluate()['entries'][0]['checks']['runtime']['status'], 'unknown')
        report = QUALIFY.evaluate(self.suite, {'schema_version': 1, 'profiles': [self.profile]},
                                 self.hardware, {'example': self.manifest}, storage_free=1)
        self.assertEqual(report['entries'][0]['checks']['storage']['reason'], 'insufficient_output_storage')

    def test_invalid_or_duplicate_profiles_fail_explicitly(self):
        with self.assertRaisesRegex(ValueError, 'duplicate exact'):
            self.evaluate({'schema_version': 1, 'profiles': [self.profile, copy.deepcopy(self.profile)]})
        self.profile['memory']['provenance'] = {}
        with self.assertRaisesRegex(ValueError, 'provenance'):
            self.evaluate()

    def test_runtime_snapshot_and_semantic_legacy_identity(self):
        other = copy.deepcopy(self.runtime)
        other['sdk_identity']['baseline_sha256'] = 'other-model'
        self.assertEqual(QUALIFY.runtime_identity(other), self.identity)
        other['resolved_runtime'] = {'runtime_identity_sha256': '9'*64}
        self.assertEqual(QUALIFY.runtime_identity(other), '9'*64)
        other['resolved_runtime']['runtime_identity_sha256'] = 'bad'
        with self.assertRaises(ValueError): QUALIFY.runtime_identity(other)

    def test_sysfs_observation_is_bounded_partial_evidence(self):
        devices, sysfs = self.root/'dev', self.root/'sys'
        devices.mkdir()
        (devices/'renderD128').touch()
        device = sysfs/'renderD128/device'
        device.mkdir(parents=True)
        (device/'uevent').write_text('DRIVER=amdgpu\nPCI_SLOT_NAME=0000:01:00.0\nPCI_ID=1002:ABC\n')
        (device/'mem_info_vram_total').write_text('512')
        (device/'mem_info_gtt_total').write_text('999999')
        kfd = self.root/'kfd'
        (kfd/'1').mkdir(parents=True)
        (kfd/'1/properties').write_text('drm_render_minor 128\ngfx_target_version 110501\n')
        (kfd/'1/gpu_id').write_text('12140')
        observed = QUALIFY.observe_amd('amdgpu://0', sysfs, devices, kfd)
        self.assertEqual(observed['kernel_device_mapping']['gfx_target_version'], 110501)
        self.assertEqual(observed['pci_slot'], '0000:01:00.0')
        self.assertEqual(observed['observed_counters']['mem_info_vram_total'], 512)
        self.assertIsNone(observed['canonical_target'])
        self.assertIsNone(observed['usable_total_bytes'])
        self.assertFalse(observed['mapping_qualified'])
        missing = QUALIFY.observe_amd('amdgpu://1', sysfs, devices)
        self.assertIn('render_node_unavailable', missing['gaps'])

    def test_saved_observation_requires_sdk_and_pci_mapping_and_pool_evidence(self):
        observed = {'device_uri': 'amdgpu://0', 'pci_slot': '0000:01:00.0', 'driver': 'amdgpu', 'driver_version': 'fixture-driver', 'evidence': []}
        saved = {**self.hardware, 'schema_version': 1, 'runtime_identity': self.identity,
                 'provenance': PROVENANCE, 'memory_provenance': PROVENANCE}
        qualified = QUALIFY.qualify_observation(observed, saved, self.identity,
                                                    now=datetime(2026, 11, 11, tzinfo=timezone.utc))
        self.assertEqual(qualified['usable_total_bytes'], 1000)
        self.assertEqual(qualified['usable_free_bytes'], 1000)
        stale = QUALIFY.qualify_observation(observed, saved, self.identity, now=datetime(2026, 11, 12, tzinfo=timezone.utc))
        self.assertIsNone(stale['usable_free_bytes'])
        self.assertEqual(stale['usable_total_bytes'], 1000)
        for key, value in [('runtime_identity', 'stale'), ('pci_slot', '0000:02:00.0')]:
            bad = dict(saved, **{key: value})
            with self.assertRaises(ValueError): QUALIFY.qualify_observation(observed, bad, self.identity)
        saved.pop('memory_provenance')
        with self.assertRaises(ValueError): QUALIFY.qualify_observation(observed, saved, self.identity)

    def test_cli_writes_only_report_and_summary_and_does_not_mutate_prepared(self):
        prepared = self.root/'prepared'
        prepared.mkdir()
        path = prepared/'manifest.json'
        payload = json.dumps(self.manifest).encode()
        path.write_bytes(payload)
        self.entry.update(inputs_manifest='manifest.json', inputs_manifest_sha256=hashlib.sha256(payload).hexdigest())
        (prepared/'prepared-suite.json').write_text(json.dumps(self.suite))
        before = {p.name: p.read_bytes() for p in prepared.iterdir()}
        output = self.root/'output'
        command = ['bash', str(ROOT/'scripts/qualify-cached-models.sh'), '--prepared', str(prepared), '--output', str(output)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((output/'hardware-selection-report.json').read_text())
        self.assertEqual(report['entries'][0]['recommendation'], 'needs_information')
        self.assertEqual({p.name for p in output.iterdir()}, {'hardware-selection-report.json', 'summary.md'})
        self.assertEqual(before, {p.name: p.read_bytes() for p in prepared.iterdir()})
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)

    def test_bad_manifest_digest_or_symlink_never_reads_asset(self):
        prepared = self.root/'prepared'
        prepared.mkdir()
        external = self.root/'private.json'
        external.write_text(json.dumps(self.manifest))
        (prepared/'manifest.json').symlink_to(external)
        self.entry.update(inputs_manifest='manifest.json', inputs_manifest_sha256=hashlib.sha256(external.read_bytes()).hexdigest())
        (prepared/'prepared-suite.json').write_text(json.dumps(self.suite))
        with patch.object(QUALIFY, 'observe_amd', return_value=self.hardware):
            report = QUALIFY.generate(prepared, self.root/'output')
        self.assertEqual(report['entries'][0]['checks']['storage']['status'], 'unknown')

    def test_bad_program_shapes_and_runtime_fail_cleanly(self):
        self.profile['programs'] = [None]
        with self.assertRaisesRegex(ValueError, 'program paths'):
            self.evaluate()
        with self.assertRaisesRegex(ValueError, 'runtime must be an object'):
            QUALIFY.runtime_identity([])

    def test_malicious_names_are_not_active_markdown(self):
        self.entry['selection']['model_id'] = 'bad | [link](file:///private) <script>'
        report = self.evaluate({'schema_version': 1, 'profiles': []})
        markdown = QUALIFY.render_markdown(report)
        self.assertIn('\\[link\\]', markdown)
        self.assertIn('\\<script\\>', markdown)


if __name__ == '__main__':
    unittest.main()
