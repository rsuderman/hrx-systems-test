"""Saved provenance works across source layouts without historical source files."""
import copy
import hashlib
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
runpy.run_path(str(ROOT / 'python/run.py'))['bootstrap']()
from hrx_systems import layout
from hrx_systems.preparation import command as PREP
from hrx_systems.execution import command as RUN
from hrx_systems.pipelines import seals as PIPE, upgrader
import test_run_prepared_models as HANDOFF
import test_pipeline_preparation as PIPE_FIXTURE

TOOL_SHA = '563ee7e8f4d1cdcc81e059475600ca1409099a164c8c84137f3d0d087129100a'


class SourceLayoutTests(unittest.TestCase):
    def handoff(self):
        fixture = HANDOFF.PreparedHandoffTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_producer_hashes_actual_implementation_and_preflight_accepts_new_layout(self):
        source = PREP.source_identity(ROOT)
        self.assertEqual(source['layout_version'], 2)
        self.assertEqual(source['migration_implementation_path'], layout.MIGRATION_RELATIVE)
        self.assertEqual(set(source['files_sha256']), set(layout.implementation_files()))
        self.assertEqual(source['files_sha256'][layout.MIGRATION_RELATIVE], TOOL_SHA)
        self.assertNotIn('scripts/prepare-cached-models.py', source['files_sha256'])
        fixture = self.handoff()
        fixture.suite['source'] = source
        fixture.verify()

    def test_real_preflight_rejects_explicit_invalid_markers_and_missing_references(self):
        fixture = self.handoff()
        source = PREP.source_identity(ROOT)
        invalid = [dict(source, layout_version=value) for value in (None, True, '2', 1, 3)]
        invalid += [dict(source, migration_implementation_path=value) for value in
                    (None, '../tools/migrations/x.py', 'scripts/upgrade-pipelines-0.1.0-to-0.2.0.py')]
        invalid.append(dict(source, files_sha256={}))
        for value in invalid:
            with self.subTest(value=value):
                fixture.suite['source'] = value
                with self.assertRaises(ValueError):
                    fixture.verify()

    def test_absent_marker_keeps_legacy_saved_map_without_old_checkout(self):
        old = {'files_sha256': {'scripts/upgrade-pipelines-0.1.0-to-0.2.0.py': 'b' * 64}}
        self.assertEqual(PIPE.migration_implementation_sha256(old), 'b' * 64)
        new = dict(old, layout_version=2, migration_implementation_path=layout.MIGRATION_RELATIVE)
        with self.assertRaises(ValueError):
            PIPE.migration_implementation_sha256(new)
        fixture = self.handoff()
        fixture.suite['source'] = old
        fixture.verify()

    def test_layout_does_not_select_policy_or_publication_abi(self):
        fixture = PIPE_FIXTURE.PipelinePreparationTests()
        for version, profile in [('0.1.0', 'legacy'), ('0.2.0.dev0', 'legacy'), ('0.2.0.dev0', 'state')]:
            with self.subTest(version=version, profile=profile), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dest, policy, manifest, baseline, entry, suite = fixture.fixture(root, version, abi_profile=profile, epoch=True)
                report = PIPE.read_json(dest / 'pipeline-upgrade.json')
                for source in [PREP.source_identity(ROOT), {'files_sha256': {
                        'scripts/upgrade-pipelines-0.1.0-to-0.2.0.py': TOOL_SHA}}]:
                    checked = PIPE.validate_policy(root, suite)
                    PIPE.verify_upgrade(root, entry, manifest, baseline, checked)
                    if policy['pipeline_upgrade']['enabled']:
                        self.assertEqual(report['implementation_sha256'], PIPE.migration_implementation_sha256(source))
                        self.assertEqual(report['epoch_publication_abi'], 'legacy-12' if profile == 'legacy' else 'control-state-13')

    def test_tool_is_byte_identical_and_runs_copied_alone(self):
        tool = layout.migration_path()
        self.assertEqual(hashlib.sha256(tool.read_bytes()).hexdigest(), TOOL_SHA)
        self.assertTrue((ROOT / 'scripts' / tool.name).is_symlink())
        self.assertEqual((ROOT / 'scripts' / tool.name).resolve(), tool)
        with tempfile.TemporaryDirectory() as tmp:
            isolated = Path(tmp) / tool.name
            shutil.copyfile(tool, isolated)
            result = subprocess.run([sys.executable, '-I', str(isolated), '--help'], cwd=tmp,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--epoch-publication-abi', result.stdout)
            self.assertEqual(upgrader.load().__file__, str(tool))
