"""Trusted isolated imports and implementation provenance after module extraction."""
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location('test_modules_' + name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ScriptModulesTests(unittest.TestCase):
    def test_public_commands_import_under_isolation_outside_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            for script in ('prepare-cached-models.py', 'seed-pipelines.py', 'run-prepared-models.py'):
                with self.subTest(script=script):
                    result = subprocess.run(['python3', '-I', str(ROOT / 'scripts' / script), '--help'],
                        cwd=temporary, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('usage:', result.stdout)

    def test_native_protocol_import_requires_no_installed_sdk(self):
        code = """import importlib.util, pathlib, sys
path = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('isolated_prepare', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
service = module.native_preparation()
assert type(service).__module__ == 'hrx_systems.pipelines.preparation'
assert 'loom' not in sys.modules
assert module.helper('pipeline-cache').read_entry.__module__ == 'hrx_systems.pipelines.cache_reader'
"""
        result = subprocess.run(['python3', '-I', '-c', code, str(ROOT / 'scripts/prepare-cached-models.py')],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_preparation_provenance_includes_bootstrap_and_actual_internal_sources(self):
        prepare = load('prepare-cached-models')
        identities = prepare.source_identity(ROOT)['files_sha256']
        expected = [ROOT / 'scripts/_bootstrap.py', *sorted((ROOT / 'scripts/lib/hrx_systems').rglob('*.py'))]
        for path in expected:
            self.assertEqual(identities[str(path.relative_to(ROOT))], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_loader_rejects_paths_outside_its_repository(self):
        bootstrap = load('_bootstrap')
        for value in ('../runtime-policy', '/tmp/module', 'runtime-policy.py/extra'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bootstrap.load_public(value)
        for value in ('../outside.py', '/tmp/outside.py', 'pipelines/../outside.py'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bootstrap.load_internal(value)


if __name__ == '__main__':
    unittest.main()
