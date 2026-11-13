"""Trusted isolated imports, package boundaries and implementation provenance."""
import hashlib
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
runpy.run_path(str(ROOT / 'python/run.py'))['bootstrap']()
from hrx_systems import layout
from hrx_systems.preparation import command as prepare


class ScriptModulesTests(unittest.TestCase):
    def test_public_commands_import_under_isolation_outside_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            for role in ('prepare-cached-models', 'seed-pipelines', 'run-prepared-models'):
                with self.subTest(role=role):
                    result = subprocess.run(layout.command(role, ['--help']), cwd=temporary,
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('usage:', result.stdout)

    def test_native_protocol_and_reader_import_require_no_installed_sdk_or_writer(self):
        code = """import pathlib, runpy, sys
runpy.run_path(sys.argv[1])['bootstrap']()
from hrx_systems.cache import pipeline_reader
assert 'hrx_systems.cache.pipeline_writer' not in sys.modules
from hrx_systems.preparation import command
service = command.native_preparation()
assert type(service).__module__ == 'hrx_systems.preparation.native'
assert 'loom' not in sys.modules
assert command.CACHE.read_entry.__module__ == 'hrx_systems.cache.pipeline_reader'
"""
        result = subprocess.run([sys.executable, '-I', '-c', code, str(layout.launcher_path())],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_preparation_provenance_includes_launcher_and_actual_package_sources(self):
        identities = prepare.source_identity(ROOT)['files_sha256']
        self.assertEqual(set(identities), set(layout.implementation_files()))
        for path in layout.implementation_files():
            self.assertEqual(identities[path], hashlib.sha256((ROOT / path).read_bytes()).hexdigest())

    def test_only_documented_python_aliases_remain_and_paths_cannot_escape(self):
        self.assertEqual({p.name for p in (ROOT / 'scripts').glob('*.py')},
                         {'paired-ci.py', 'release-runtime.py', 'single-model-e2e.py',
                          'upgrade-pipelines-0.1.0-to-0.2.0.py'})
        self.assertFalse((ROOT / 'scripts/lib').exists())
        for value in ('../outside.py', '/tmp/outside.py', 'pipelines/../outside.py'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                layout.repository_path(value)


if __name__ == '__main__':
    unittest.main()
