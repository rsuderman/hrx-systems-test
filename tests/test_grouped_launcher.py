"""Real isolated dispatcher and saved-report producer/consumer checks."""
import importlib.util
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / 'python/run.py'
FIXTURE = ROOT / 'tests/fixtures/model-report/passing'
runpy.run_path(str(LAUNCHER))['bootstrap']()
from hrx_systems import cli, layout


class GroupedLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cwd = self.root / 'foreign'; self.cwd.mkdir()
        fake = self.cwd / 'hrx_systems'; fake.mkdir()
        (fake / '__init__.py').write_text('raise RuntimeError("foreign package imported")\n')
        (self.cwd / 'json.py').write_text('raise RuntimeError("foreign stdlib imported")\n')
        self.env = {**os.environ, 'PYTHONPATH': str(self.cwd)}

    def call(self, *arguments, launcher=LAUNCHER):
        return subprocess.run([sys.executable, '-I', str(launcher), *map(str, arguments)],
            cwd=self.cwd, env=self.env, capture_output=True, text=True)

    def test_fixed_roles_fail_before_importing_any_command(self):
        for role in ('os', 'hrx_systems.reporting.render', '../generate-model-report', 'unknown'):
            result = self.call(role, '--output', self.root / 'must-not-exist')
            self.assertEqual(result.returncode, 2)
            self.assertIn('invalid choice', result.stderr)
        self.assertFalse((self.root / 'must-not-exist').exists())
        with self.assertRaises(ValueError): layout.command('os')

    def test_actual_report_and_legacy_bash_alias_are_identical_from_foreign_cwd(self):
        results = self.root / 'results'; shutil.copytree(FIXTURE, results)
        canonical, legacy = self.root / 'canonical', self.root / 'legacy'
        a = self.call('generate-model-report', '--results', results, '--output', canonical)
        b = subprocess.run(['bash', str(ROOT/'scripts/generate-model-report.sh'), '--results', str(results),
                            '--output', str(legacy)], cwd=self.cwd, env=self.env, text=True, capture_output=True)
        self.assertEqual((a.returncode,b.returncode),(0,0),(a.stderr,b.stderr))
        self.assertEqual(json.loads((canonical/'report.json').read_text()),json.loads((legacy/'report.json').read_text()))
        for name in ('index.html','summary.md','suite-result.json'):
            self.assertEqual((canonical/name).read_bytes(),(legacy/name).read_bytes(),name)
        raw = json.loads((canonical/'report.json').read_text())
        import hashlib
        self.assertEqual(raw['renderer']['source_sha256'],hashlib.sha256((ROOT/'python/hrx_systems/reporting/render.py').read_bytes()).hexdigest())

    def test_actual_failure_packaging_command_remains_saved_only(self):
        attempt=self.root/'attempt';attempt.mkdir();shutil.copytree(FIXTURE,attempt/'results')
        raw_path=attempt/'results/suite-result.json';raw=json.loads(raw_path.read_text())
        raw['models'][0]['status']='failed'
        raw['models'][0]['cycles'][0]['readiness']={'status':'failed','reason':'host expects 13 arguments; source declares 12'}
        raw['status']='failed';raw_path.write_text(json.dumps(raw))
        result=self.call('package-model-failures','--attempt',attempt,'--output',attempt/'failures','--zip',attempt/'failures.zip')
        self.assertEqual(result.returncode,0,result.stderr)
        manifest=json.loads((attempt/'failures/failures.json').read_text())
        self.assertEqual(manifest['counts']['failed'],1)
        self.assertTrue((attempt/'failures.zip').is_file())

    def test_package_markers_and_dispatch_are_lazy(self):
        code = f'''import runpy,sys
runpy.run_path({str(LAUNCHER)!r})['bootstrap']()
import hrx_systems,hrx_systems.cli,hrx_systems.sdk,hrx_systems.execution,hrx_systems.reporting
assert not any(n == 'loom' or n.startswith('loom.') for n in sys.modules)
assert 'hrx_systems.reporting.render' not in sys.modules
assert 'hrx_systems.preparation.command' not in sys.modules
assert 'hrx_systems.execution.command' not in sys.modules
'''
        result=subprocess.run([sys.executable,'-I','-c',code],cwd=self.cwd,env=self.env,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_preloaded_foreign_package_or_submodule_root_is_rejected(self):
        for name in ('hrx_systems','hrx_systems.reporting'):
            code=f'''import runpy,sys,types
loader=runpy.run_path({str(LAUNCHER)!r})
if {name!r} != 'hrx_systems': loader['bootstrap']()
fake=types.ModuleType({name!r});fake.__file__='/foreign/package/__init__.py';fake.__path__=['/foreign/package']
sys.modules[{name!r}]=fake
try: loader['bootstrap']()
except ImportError: pass
else: raise AssertionError('accepted a foreign package root')
'''
            result=subprocess.run([sys.executable,'-I','-c',code],cwd=self.cwd,env=self.env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_report_works_with_only_python_mount_and_no_git_or_configuration(self):
        isolated=self.root/'mounted/python';shutil.copytree(ROOT/'python',isolated,ignore=shutil.ignore_patterns('__pycache__'))
        results=self.root/'results';shutil.copytree(FIXTURE,results)
        result=self.call('generate-model-report','--results',results,'--output',self.root/'report',launcher=isolated/'run.py')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse((isolated.parent/'.git').exists())
        self.assertFalse((isolated.parent/'config').exists())

    def test_layout_has_one_fixed_code_and_mount_authority(self):
        self.assertEqual(layout.REPO_ROOT,ROOT)
        self.assertEqual(layout.command('generate-model-report',['--help'],interpreter='/run/venv/bin/python',container=True),
            ['/run/venv/bin/python','-I','/source/python/run.py','generate-model-report','--help'])
        self.assertEqual(layout.source_mounts(),[(str(ROOT/'python'),'/source/python'),(str(ROOT/'tools/migrations'),'/source/tools/migrations')])
        files=layout.implementation_files()
        self.assertIn('python/run.py',files)
        self.assertIn(layout.MIGRATION_RELATIVE,files)
        self.assertIn('python/hrx_systems/reporting/render.py',files)
        self.assertFalse(any('tests/'in name or '__pycache__'in name for name in files))
        for name in ('../config/runtime.json','/etc/passwd','python/../scripts'):
            with self.assertRaises(ValueError):layout.repository_path(name)

class GroupedCompositionTests(unittest.TestCase):
    def test_all_roles_help_from_foreign_cwd_and_public_python_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, 'PYTHONPATH': tmp}
            for role in cli.ROLES:
                with self.subTest(role=role):
                    result = subprocess.run([sys.executable, '-I', str(LAUNCHER), role, '--help'],
                                            cwd=tmp, env=env, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('usage:', result.stdout)
            for name in ('paired-ci', 'release-runtime'):
                result = subprocess.run([sys.executable, '-I', str(ROOT / 'scripts' / (name + '.py')), '--help'],
                                        cwd=tmp, env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_paired_error_path_preserves_diagnostic_and_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            for prefix in ([str(LAUNCHER), 'paired-ci'], [str(ROOT / 'scripts/paired-ci.py')]):
                result = subprocess.run([sys.executable, '-I', *prefix, 'initialize', '--plan',
                    str(Path(tmp) / 'missing.json'), '--lane', 'latest-ci', '--output', str(Path(tmp) / 'output')],
                    cwd=tmp, text=True, capture_output=True)
                self.assertEqual(result.returncode, 1)
                self.assertIn('Paired CI error:', result.stderr)
                self.assertNotIn('Traceback', result.stderr)

    def test_sdk_inspection_is_fixed_dispatch_and_emits_same_environment_fields(self):
        result = subprocess.run([sys.executable, '-I', str(LAUNCHER), 'sdk-environment', 'target-environment'],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value['python_full_version'], '.'.join(map(str, sys.version_info[:3])))
        self.assertEqual(value['sys_platform'], sys.platform)
        from hrx_systems.preparation import command
        with self.assertRaises(ValueError):
            command.container_json({}, 'print("untrusted")')

    def test_hidden_native_routes_use_package_mains(self):
        from unittest.mock import patch
        from hrx_systems.preparation import command as prep
        from hrx_systems.cache import seed_assets as seed
        with patch.object(prep, 'native_batch') as batch, patch.object(prep, 'native') as native:
            self.assertEqual(cli.main(['prepare-cached-models', '--native-batch', 'online']), 0)
            batch.assert_called_once_with('online')
            self.assertEqual(cli.main(['prepare-cached-models', '--native', 'offline', 'one']), 0)
            native.assert_called_once_with('offline', 'one')
        with patch.object(seed, 'read_json', return_value={'fixture': True}), \
                patch.object(seed, 'prepare') as prepare, patch.object(seed, 'fill') as fill:
            self.assertEqual(cli.main(['seed-models', '--native', 'prepare', 'online']), 0)
            prepare.assert_called_once()
            self.assertEqual(cli.main(['seed-models', '--native', 'fill', 'offline']), 0)
            self.assertTrue(fill.call_args.args[-1])

    def test_stage_reexecution_uses_only_canonical_launcher_and_fixed_native_roles(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from hrx_systems.preparation import command as prep
        from hrx_systems.cache import seed_assets, seed_pipelines
        for module, argv, native_role in [(seed_assets, ['--stage', 'fill'], 'seed-models'),
                                          (seed_pipelines, ['--stage'], 'seed-pipelines')]:
            with self.subTest(role=native_role), patch.object(module.subprocess, 'run',
                    return_value=SimpleNamespace(returncode=0)) as child:
                self.assertEqual(module.main(argv), 0)
                install, native = [call.args[0] for call in child.call_args_list]
                self.assertEqual(install[:4], [sys.executable, '-I', '/source/python/run.py', 'sdk-environment'])
                self.assertEqual(native[:4], ['/run/venv/bin/python', '-I', '/source/python/run.py', native_role])
        with patch.object(prep.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as child:
            self.assertEqual(prep.stage(SimpleNamespace(stage='batch-online')), 0)
            install, native = [call.args[0] for call in child.call_args_list]
            self.assertEqual(install[:5], [sys.executable, '-u', '-I', '/source/python/run.py', 'sdk-environment'])
            self.assertEqual(native[:5], ['/run/venv/bin/python', '-u', '-I', '/source/python/run.py', 'prepare-cached-models'])

    def test_host_bootstrap_prepares_only_requested_disposable_root_via_both_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            for alias in ('scripts/host/bootstrap-runner.sh', 'scripts/bootstrap-runner.sh'):
                state = Path(tmp) / str(len(list(Path(tmp).iterdir())))
                result = subprocess.run(['bash', str(ROOT / alias), 'prepare'], cwd=tmp,
                    env={**os.environ, 'HRX_SYSTEMS_ROOT': str(state)}, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((state / 'runner').is_dir())
                self.assertTrue((state / 'cache/huggingface').is_dir())
                self.assertTrue((state / 'cache/loom-assets').is_dir())
                self.assertTrue((state / 'runs').is_dir())

    def test_storage_script_uses_same_pinned_config_and_cleans_disposable_resources(self):
        # The Docker transport is replaced; genuine Bash root/config/temp/cleanup
        # behavior runs without touching Docker or the shared cache.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp); binary = tmp / 'bin'; binary.mkdir()
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
            docker.chmod(0o755)
            for alias in ('scripts/host/check-container-storage.sh', 'scripts/check-container-storage.sh'):
                state = tmp / 'state'; calls = tmp / 'docker.jsonl'
                result = subprocess.run(['bash', str(ROOT / alias)], cwd=tmp, env={**os.environ,
                    'PATH': str(binary) + ':' + os.environ['PATH'], 'HRX_SYSTEMS_ROOT': str(state),
                    'DOCKER_CALLS': str(calls)}, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                rows = [json.loads(line) for line in calls.read_text().splitlines()]
                self.assertEqual(rows[-1][:2], ['rm', '-f'])
                self.assertIn('--read-only', rows[-2])
                self.assertEqual(list((state / 'cache/bootstrap').iterdir()), [])
                self.assertEqual(list((state / 'results').iterdir()), [])
