"""Exercise the real Bash composition using bounded sibling-stage fixtures."""
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
FAKE_STAGE = r'''#!/usr/bin/env bash
set -euo pipefail
kind=KIND
if [[ "$kind" = run && "${1:-}" = --cleanup ]]; then kind=cleanup; fi
printf '%s\n' "$kind" >> "$FIXTURE_CALLS"
printf '%s\0' "$@" > "$FIXTURE_ARGUMENTS/$kind"
output=""
while (($#)); do
  case "$1" in
    --output) output="$2"; shift 2;;
    --selection-snapshot|--runtime-selection|--root|--suite|--selection|--runtime|--runtime-baseline|--prepared|--results|--workflow|--device-uri|--cycles|--failures) shift 2;;
    --cleanup|--full-integrity) shift;;
    *) exit 99;;
  esac
done
key="TEST_${kind^^}_EXIT"
result="${!key:-0}"
if [[ "$kind" = prepare ]]; then
  mkdir -- "$output"
  if [[ "${TEST_NO_MANIFEST:-0}" = 0 ]]; then printf '{}\n' > "$output/prepared-suite.json"; fi
elif [[ "$kind" = run ]]; then
  mkdir -- "$output"
  if [[ "${TEST_NO_RAW:-0}" = 0 ]]; then printf '{"status":"failed"}\n' > "$output/suite-result.json"; fi
elif [[ "$kind" = report ]]; then
  mkdir -- "$output"
  printf '<html>fixture result</html>\n' > "$output/index.html"
  printf '{"status":"failed"}\n' > "$output/report.json"
fi
if [[ "${TEST_BLOCK_STAGE:-}" = "$kind" ]]; then
  while :; do :; done
fi
exit "$result"
'''


def workflow_run_block(name):
    source = (ROOT / '.github/workflows/_model-verification-worker.yml').read_text()
    section = source.split('      - name: ' + name + '\n', 1)[1]
    code = section.split('        run: |\n', 1)[1]
    lines = []
    for line in code.splitlines():
        if line and not line.startswith('          '):
            break
        lines.append(line[10:])
    return '\n'.join(lines) + '\n'


class StageCompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        scripts = self.repo / 'scripts'
        shutil.copytree(ROOT / 'scripts', scripts, symlinks=True)
        shutil.copytree(ROOT / 'python', self.repo / 'python', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(ROOT / 'tools', self.repo / 'tools')
        self.script = scripts / 'test/run-cached-models.sh'
        for name, kind in [('test/prepare-cached-models.sh', 'prepare'),
                           ('test/run-prepared-models.sh', 'run'),
                           ('report/generate-model-report.sh', 'report')]:
            (scripts / name).write_text(FAKE_STAGE.replace('kind=KIND', 'kind=' + kind))
        self.cache_root = self.root / 'read only cache root'
        (self.cache_root / 'cache').mkdir(parents=True)
        (self.cache_root / 'cache/sentinel').write_text('immutable model fixture')
        self.output = self.root / 'runner owned output'
        self.arguments = self.root / 'arguments'
        self.arguments.mkdir()
        self.calls = self.root / 'calls'
        self.env = {**os.environ, 'FIXTURE_ARGUMENTS': str(self.arguments),
                    'FIXTURE_CALLS': str(self.calls), 'PYTHONNOUSERSITE': '1'}
        for key in list(self.env):
            if key.startswith('GITHUB_') or key.startswith('TEST_'):
                self.env.pop(key)
        self.command = ['bash', str(self.script), '--root', str(self.cache_root), '--output', str(self.output)]

    def invoke(self, **env):
        result = subprocess.run(self.command, env={**self.env, **env}, capture_output=True, text=True, timeout=15)
        self.assertEqual((self.cache_root / 'cache/sentinel').read_text(), 'immutable model fixture')
        return result

    def order(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def status(self, stage):
        return json.loads((self.output / 'stages.json').read_text())['stages'][stage]

    def initialize_workflow(self, event, inputs=None):
        number = getattr(self, 'initializations', 0) + 1
        self.initializations = number
        env_file = self.root / 'github-env'
        env_file.write_text('')
        environment = {**self.env, 'RUNNER_TEMP': str(self.root / 'runner-temp'),
            'GITHUB_ENV': str(env_file), 'GITHUB_EVENT_NAME': event,
            'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_RUN_ID': str(number),
            'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_REF': 'refs/heads/main',
            'GITHUB_SHA': 'a' * 40, 'RUNNER_NAME': 'fixture-runner',
            'GITHUB_SERVER_URL': 'https://github.com',
            'RAW_DISPATCH_INPUTS': json.dumps({} if inputs is None else inputs),
            'TRIGGER_SCHEDULE': '0 4 * * *' if event == 'schedule' else '',
            'GITHUB_TOKEN': 'secret-not-to-copy'}
        import runpy
        runpy.run_path(str(ROOT / 'python/run.py'))['bootstrap']()
        from hrx_systems.ci import paired as module
        try:
            values = module.normalize(event, {} if inputs is None else inputs)
            plan_dir = self.root / ('plan-' + str(number)); plan_dir.mkdir()
            module.write(plan_dir / 'selection.json', {'entries': []})
            module.write(plan_dir / 'runtime.json', {})
            mode = values['runtime_source'] if values['runtime_source'] != 'both' else 'latest-release'
            module.write(plan_dir / 'ci-plan.json', {'schema_version':1,'inputs':values,
                'selection_snapshot':'selection.json','selection_snapshot_sha256':module.sha(plan_dir/'selection.json'),
                'expected_entries':[], 'lanes':[{'id':'latest-release','runtime_source':mode,
                    'runtime_selection':'runtime.json','runtime_selection_sha256':module.sha(plan_dir/'runtime.json')}]})
            from unittest.mock import patch
            with patch.dict(os.environ, environment):
                exported = module.initialize(plan_dir/'ci-plan.json','latest-release',self.root/('initialized-'+str(number)))
            identity = json.loads((Path(exported['E2E_OUTPUT'])/'workflow.json').read_text())
            return subprocess.CompletedProcess([],0,'',''), exported, identity
        except (ValueError, TypeError):
            return subprocess.CompletedProcess([],1,'','invalid inputs'), {}, None

    def test_all_trigger_types_normalize_defaults_before_stages(self):
        for event in ('workflow_dispatch', 'push', 'schedule'):
            result, exported, identity = self.initialize_workflow(event)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(exported['MODEL_SELECTION'], 'cached')
            self.assertEqual(exported['RUNTIME_SOURCE'], 'latest-release')
            self.assertEqual(exported['DEVICE_URI'], 'amdgpu://0')
            self.assertEqual(exported['TEST_CYCLES'], '2')
            self.assertEqual(exported['FULL_INTEGRITY'], 'false')
            self.assertEqual(identity['event_name'], event)
            self.assertEqual(identity['normalized_inputs']['cycles'], 2)
            self.assertIn('+00:00', identity['observed_at'])
            self.assertEqual(identity['observed_time_source'], 'runner initialization clock')
            self.assertNotIn('secret-not-to-copy', json.dumps(identity))
            if event == 'schedule':
                self.assertEqual(identity['schedule'], '0 4 * * *')
                self.assertEqual(identity['schedule_timezone'], 'America/Los_Angeles')
            else:
                self.assertNotIn('schedule', identity)

    def test_automatic_events_do_not_use_manual_overrides(self):
        for event in ('push', 'schedule'):
            result, exported, _ = self.initialize_workflow(event, {
                'selection': 'suite', 'runtime_source': 'release', 'cycles': 0, 'full_integrity': True})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(exported['MODEL_SELECTION'], 'cached')
            self.assertEqual(exported['RUNTIME_SOURCE'], 'latest-release')
            self.assertEqual(exported['TEST_CYCLES'], '2')
            self.assertEqual(exported['FULL_INTEGRITY'], 'false')

    def test_manual_release_sample_and_explicit_options_are_preserved(self):
        result, exported, identity = self.initialize_workflow('workflow_dispatch', {
            'selection': 'suite', 'runtime_source': 'release', 'cycles': '3',
            'full_integrity': True, 'device_uri': 'amdgpu://1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(exported['MODEL_SELECTION'], 'suite')
        self.assertEqual(exported['RUNTIME_SOURCE'], 'release')
        self.assertEqual(exported['TEST_CYCLES'], '3')
        self.assertEqual(exported['DEVICE_URI'], 'amdgpu://1')
        self.assertEqual(exported['FULL_INTEGRITY'], 'true')
        self.assertIs(identity['normalized_inputs']['full_integrity'], True)

    def test_manual_replay_identifiers_are_normalized(self):
        result, exported, identity = self.initialize_workflow('workflow_dispatch', {
            'runtime_source': 'actions-replay', 'runtime_run_id': '123', 'runtime_run_attempt': '2',
            'runtime_artifact_id': '456', 'runtime_wheel_sha256': 'b' * 64})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(exported['RUNTIME_SOURCE'], 'actions-replay')
        self.assertEqual(exported['RUNTIME_RUN_ID'], '123')
        self.assertEqual(identity['normalized_inputs']['runtime_artifact_id'], '456')

    def test_invalid_manual_values_fail_instead_of_using_defaults(self):
        for inputs in ({'cycles': 0}, {'cycles': False}, {'cycles': -1}, {'cycles': 1.5},
                       {'selection': 'unknown'}, {'runtime_source': 'unknown'},
                       {'device_uri': 'amdgpu://0\nINJECT=value'}, {'full_integrity': 'yes'},
                       {'runtime_run_id': '123'}, {'runtime_source': 'actions-replay'}):
            result, exported, identity = self.initialize_workflow('workflow_dispatch', inputs)
            self.assertNotEqual(result.returncode, 0, inputs)
            self.assertIsNone(identity)
            self.assertNotIn('TEST_CYCLES', exported)

    def test_workflow_declares_main_push_and_local_time_nightly(self):
        workflow = (ROOT / '.github/workflows/single-model-e2e.yml').read_text()
        self.assertIn('  push:\n    branches: [main]', workflow)
        self.assertIn("- cron: '0 4 * * *'\n      timezone: America/Los_Angeles", workflow)
        self.assertIn('RAW_DISPATCH_INPUTS: ${{ toJSON(inputs) }}', workflow)
        self.assertNotIn('TEST_CYCLES: ${{ inputs.cycles', workflow)

    def test_success_composes_stages_and_preserves_raw_results(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.order(), ['prepare', 'run', 'cleanup', 'report'])
        self.assertTrue((self.output / 'results/suite-result.json').is_file())
        self.assertTrue((self.output / 'report/index.html').is_file())
        self.assertFalse((self.output / 'prepared').exists())
        arguments = (self.arguments / 'run').read_bytes().decode().split('\0')
        self.assertEqual(arguments[arguments.index('--workflow') + 1], str(self.output / 'workflow.json'))
        self.assertEqual(self.status('report')['exit_code'], 0)

    def test_cached_selection_and_runtime_override_are_forwarded(self):
        self.command += ['--selection', 'cached', '--runtime-baseline', '/explicit/runtime.json', '--full-integrity']
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = (self.arguments / 'prepare').read_bytes().decode().split('\0')
        self.assertEqual(arguments[arguments.index('--selection') + 1], 'cached')
        self.assertIn('--full-integrity', arguments)
        self.assertEqual(arguments[arguments.index('--runtime-baseline') + 1], '/explicit/runtime.json')

    def test_shared_runtime_is_forwarded_for_explicit_suite(self):
        self.command += ['--runtime', '/fixture/runtime.json']
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        args = (self.arguments / 'prepare').read_bytes().decode().split('\0')
        self.assertEqual(args[args.index('--runtime') + 1], '/fixture/runtime.json')

    def test_conflicting_runtime_inputs_fail_before_work(self):
        self.command += ['--selection', 'cached', '--runtime', '/runtime', '--runtime-baseline', '/legacy']
        result = self.invoke()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.order(), [])

    def test_cached_selection_and_explicit_suite_conflict_before_work(self):
        self.command += ['--selection', 'cached', '--suite', '/explicit/suite.json']
        result = self.invoke()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.order(), [])

    def test_partial_preparation_failure_runs_and_reports_but_still_fails(self):
        result = self.invoke(TEST_PREPARE_EXIT='7')
        self.assertEqual(result.returncode, 7)
        self.assertEqual(self.order(), ['prepare', 'run', 'cleanup', 'report'])
        self.assertEqual(self.status('prepare')['exit_code'], 7)
        self.assertEqual(self.status('report')['exit_code'], 0)

    def test_no_manifest_never_starts_execution(self):
        result = self.invoke(TEST_PREPARE_EXIT='8', TEST_NO_MANIFEST='1')
        self.assertEqual(result.returncode, 8)
        self.assertEqual(self.order(), ['prepare'])
        self.assertEqual(self.status('run')['status'], 'not_run')
        self.assertTrue((self.output / 'prepare.log').is_file())
        self.assertTrue((self.output / 'model-failures.zip').is_file())

    def test_execution_failure_survives_successful_report(self):
        result = self.invoke(TEST_RUN_EXIT='9')
        self.assertEqual(result.returncode, 9)
        self.assertEqual(self.order(), ['prepare', 'run', 'cleanup', 'report'])
        self.assertTrue((self.output / 'report/report.json').is_file())
        self.assertEqual(self.status('run')['exit_code'], 9)

    def test_reporting_failure_keeps_raw_evidence_and_fails(self):
        result = self.invoke(TEST_REPORT_EXIT='5')
        self.assertEqual(result.returncode, 5)
        self.assertTrue((self.output / 'results/suite-result.json').is_file())
        self.assertEqual(self.status('report')['exit_code'], 5)
        self.assertTrue((self.output / 'model-failures.zip').is_file())
        manifest = json.loads((self.output / 'failures/failures.json').read_text())
        self.assertIn('report', [case['phase'] for case in manifest['cases']])

    def test_cleanup_failure_retains_prepared_inputs_for_owned_container(self):
        result = self.invoke(TEST_CLEANUP_EXIT='6')
        self.assertEqual(result.returncode, 6)
        self.assertTrue((self.output / 'prepared/prepared-suite.json').is_file())
        self.assertEqual(self.status('cleanup')['exit_code'], 6)

    def test_absent_raw_results_cannot_become_green(self):
        result = self.invoke(TEST_NO_RAW='1')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('report', self.order())
        self.assertEqual(self.status('report')['status'], 'not_run')

    def test_keep_prepared_and_explicit_device_cycles_are_forwarded(self):
        self.command += ['--keep-prepared', '--device-uri', 'amdgpu://1', '--cycles', '2']
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.output / 'prepared/prepared-suite.json').is_file())
        arguments = (self.arguments / 'run').read_bytes().decode().split('\0')
        self.assertEqual(arguments[arguments.index('--cycles') + 1], '2')
        self.assertEqual(arguments[arguments.index('--device-uri') + 1], 'amdgpu://1')

    def test_existing_output_is_preserved_without_starting_stages(self):
        self.output.mkdir()
        (self.output / 'sentinel').write_text('prior attempt')
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.order(), [])
        self.assertEqual((self.output / 'sentinel').read_text(), 'prior attempt')

    def test_workflow_identity_excludes_credentials(self):
        result = self.invoke(GITHUB_REPOSITORY='owner/repo', GITHUB_RUN_ID='42', GITHUB_RUN_ATTEMPT='2',
            GITHUB_REF='refs/heads/test', GITHUB_SHA='a' * 40, RUNNER_NAME='fixture-runner',
            GITHUB_TOKEN='secret-not-to-copy')
        self.assertEqual(result.returncode, 0)
        text = (self.output / 'workflow.json').read_text()
        self.assertNotIn('secret-not-to-copy', text)
        self.assertEqual(json.loads(text)['run_url'], 'https://github.com/owner/repo/actions/runs/42')

    def test_interruption_delegates_cleanup_and_reports_available_results(self):
        process = subprocess.Popen(self.command, env={**self.env, 'TEST_BLOCK_STAGE': 'run'},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 5
        while ('run' not in self.order() or not (self.output / 'results/suite-result.json').exists()) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn('run', self.order())
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=25)
        self.assertEqual(process.returncode, 143, stdout + stderr)
        self.assertEqual(self.order(), ['prepare', 'run', 'cleanup', 'report'])
        self.assertEqual(self.status('run')['exit_code'], 143)
        self.assertTrue((self.output / 'prepared').exists())

    def test_workflow_packages_before_and_after_render_and_uploads_directory(self):
        workflow = (ROOT / '.github/workflows/_model-verification-worker.yml').read_text()
        collect = workflow.index('      - name: Save pre-report outcomes and collect failure evidence')
        render = workflow.index('      - name: Generate verification artifacts from saved results')
        final = workflow.index('      - name: Finalize failure bundle including report errors')
        upload = workflow.index('      - name: Upload one failure-evidence download')
        self.assertLess(collect, render)
        self.assertLess(render, final)
        self.assertLess(final, upload)
        section = workflow[upload:workflow.index('      - name: Upload compact report')]
        self.assertIn('/failures/', section)
        self.assertNotIn('.zip', section)
        self.assertIn('retention-days: 7', section)

    def test_workflow_final_gate_rejects_upload_and_stage_failures(self):
        script = workflow_run_block('Require all stages and publication to pass')
        env = {**self.env, **{name + '_OUTCOME': 'success'
               for name in ('INITIALIZE', 'PREPARE', 'RUN', 'CLEANUP', 'REPORT', 'COLLECT', 'PACKAGE', 'UPLOAD', 'SUMMARY', 'LANE_RESULT', 'LANE_UPLOAD')}, 'HAS_FAILURES': 'false', 'FAILURE_UPLOAD_OUTCOME': 'skipped'}
        good = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stderr)
        for stage in ('PREPARE', 'RUN', 'CLEANUP', 'REPORT', 'COLLECT', 'PACKAGE', 'UPLOAD', 'SUMMARY'):
            with self.subTest(stage=stage):
                bad = subprocess.run(['bash', '-c', script], env={**env, stage + '_OUTCOME': 'failure'},
                    capture_output=True, text=True)
                self.assertNotEqual(bad.returncode, 0)

    def publish_summary(self, *, upload='success', artifact_url=None, rendered=True, workflow_metadata=True,
                        failure_url='', failure_upload='skipped', fallback=None, package='success'):
        # Recorded real run/artifact identity; this test performs no network calls.
        self.artifact_url = 'https://github.com/rsuderman/hrx-systems-test/actions/runs/42933895801/artifacts/13816663497'
        self.output.mkdir()
        if fallback is not None:
            (self.output / 'failures').mkdir()
            (self.output / 'failures/summary.md').write_text(fallback)
        if rendered:
            results = self.root / 'saved-results'
            shutil.copytree(ROOT / 'tests/fixtures/model-report/passing', results)
            raw_path = results / 'suite-result.json'
            raw = json.loads(raw_path.read_text())
            workflow_identity = {
                'schema_version': 1, 'repository': 'rsuderman/hrx-systems-test',
                'run_id': '42933895801', 'run_attempt': '1', 'ref': 'refs/heads/main',
                'sha': 'faaa58382503bfa38b50e8c2a4c6d270f37e3bc6', 'runner': 'dogwood-hrx-account',
                'run_url': 'https://github.com/rsuderman/hrx-systems-test/actions/runs/42933895801'}
            if workflow_metadata:
                raw['workflow'] = workflow_identity
            raw_path.write_text(json.dumps(raw))
            generated = subprocess.run([sys.executable, '-I', str(ROOT / 'python/run.py'), 'generate-model-report',
                '--results', str(results), '--output', str(self.output / 'report')],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(generated.returncode, 0, generated.stderr)
        summary_path = self.root / 'github-summary.md'
        env = {**self.env, 'E2E_OUTPUT': str(self.output), 'GITHUB_STEP_SUMMARY': str(summary_path),
               'ARTIFACT_URL': self.artifact_url if artifact_url is None else artifact_url,
               'UPLOAD_OUTCOME': upload, 'PREPARE_OUTCOME': 'success', 'RUN_OUTCOME': 'success',
               'CLEANUP_OUTCOME': 'success', 'REPORT_OUTCOME': 'success' if rendered else 'failure',
               'COLLECT_OUTCOME': 'success', 'PACKAGE_OUTCOME': package,
               'HAS_FAILURES': 'true' if failure_upload != 'skipped' else 'false',
               'FAILURE_ARTIFACT_URL': failure_url, 'FAILURE_UPLOAD_OUTCOME': failure_upload}
        published = subprocess.run(['bash', '-c', workflow_run_block('Publish readable summary and report download')],
            env=env, capture_output=True, text=True, timeout=10)
        return published, summary_path.read_text()

    def test_workflow_publishes_summary_after_upload_with_exact_artifact_url(self):
        workflow = (ROOT / '.github/workflows/_model-verification-worker.yml').read_text()
        self.assertLess(workflow.index('      - name: Upload compact report'),
                        workflow.index('      - name: Publish readable summary and report download'))
        self.assertIn('ARTIFACT_URL: ${{ steps.upload.outputs.artifact-url }}', workflow)
        published, summary = self.publish_summary()
        self.assertEqual(published.returncode, 0, published.stderr)
        self.assertIn('[Download verification report bundle](' + self.artifact_url + ')', summary)
        self.assertIn('open `report/index.html`', summary)
        links = re.findall(r'\[[^\]]*\]\(([^)]+)\)', summary)
        self.assertTrue(links)
        self.assertTrue(all(link.startswith('https://') for link in links), links)
        self.assertNotIn('](index.html)', summary)
        self.assertNotIn('](report.json)', summary)
        self.assertNotIn('](suite-result.json)', summary)

    def test_workflow_rejects_local_summary_links_when_run_metadata_is_missing(self):
        published, summary = self.publish_summary(workflow_metadata=False)
        self.assertNotEqual(published.returncode, 0)
        local_summary = (self.output / 'report/summary.md').read_text()
        self.assertIn('](index.html)', local_summary)
        self.assertIn('Verification summary unavailable', summary)
        self.assertIn('[Download verification report bundle](' + self.artifact_url + ')', summary)
        links = re.findall(r'\[[^\]]*\]\(([^)]+)\)', summary)
        self.assertTrue(all(link.startswith('https://') for link in links), links)
        self.assertNotIn('](index.html)', summary)

    def test_workflow_failure_download_is_independent_and_uses_exact_url(self):
        url = 'https://github.com/owner/repo/actions/runs/42/artifacts/123'
        result, summary = self.publish_summary(failure_url=url, failure_upload='success')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('[Download all failure details and logs](' + url + ')', summary)
        self.assertIn('[Download verification report bundle](' + self.artifact_url + ')', summary)

    def test_workflow_failure_upload_error_is_explicit_without_broken_link(self):
        result, summary = self.publish_summary(failure_upload='failure')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Failure bundle unavailable', summary)
        self.assertNotIn('[Download all failure details and logs]', summary)
        self.assertIn('| failure_upload | failure |', summary)

    def test_workflow_packaging_error_is_visible_even_without_failure_archive(self):
        result, summary = self.publish_summary(package='failure')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Failure evidence incomplete', summary)
        self.assertIn('| package | failure |', summary)

    def test_workflow_renderer_failure_publishes_saved_readable_fallback(self):
        result, summary = self.publish_summary(rendered=False,
            fallback='## Verification incomplete\n\nExpected: **45**\n\n## Failures\n\nSDK acquisition: access denied\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Expected: **45**', summary)
        self.assertIn('SDK acquisition: access denied', summary)

    def test_workflow_upload_failure_keeps_summary_without_download_link(self):
        published, summary = self.publish_summary(upload='failure')
        self.assertEqual(published.returncode, 0, published.stderr)
        self.assertIn('test entries passed', summary)
        self.assertIn('artifact upload did not succeed', summary)
        self.assertIn('| upload | failure |', summary)
        self.assertNotIn('[Download verification report bundle]', summary)
        self.assertNotIn(self.artifact_url, summary)
        self.assertTrue(all(link.startswith('https://') for link in re.findall(r'\[[^\]]*\]\(([^)]+)\)', summary)))

    def test_workflow_missing_upload_url_fails_publication_without_broken_link(self):
        published, summary = self.publish_summary(artifact_url='')
        self.assertNotEqual(published.returncode, 0)
        self.assertIn('upload returned no usable HTTPS download URL', summary)
        self.assertNotIn('[Download verification report bundle]', summary)

    def test_workflow_without_rendered_report_links_only_available_diagnostics_bundle(self):
        published, summary = self.publish_summary(rendered=False)
        self.assertEqual(published.returncode, 0, published.stderr)
        self.assertIn('Verification incomplete', summary)
        self.assertIn('[Download verification report bundle](' + self.artifact_url + ')', summary)
        self.assertIn('inspect the retained stage logs and raw results', summary)
        self.assertNotIn('open `report/index.html`', summary)

    def test_workflow_cancellation_prevents_new_model_work_but_keeps_cleanup(self):
        workflow = (ROOT / '.github/workflows/_model-verification-worker.yml').read_text()
        for name in ('Prepare host-seeded pipelines and read-only cached assets',
                     'Run the prepared suite offline through one server'):
            section = workflow.split('      - name: ' + name + '\n', 1)[1].split('\n      - ', 1)[0]
            self.assertIn("!cancelled() && steps.initialize.outcome == 'success'", section)
        for name in ('Confirm execution-owned container cleanup',
                     'Generate verification artifacts from saved results'):
            section = workflow.split('      - name: ' + name + '\n', 1)[1].split('\n      - ', 1)[0]
            self.assertIn("always() && steps.initialize.outcome == 'success'", section)

    def test_workflow_cache_root_preserves_runner_environment_without_repo_override(self):
        script = workflow_run_block('Prepare host-seeded pipelines and read-only cached assets')
        self.output.mkdir()
        (self.repo / 'python/hrx_systems/ci/paired.py').write_text("def main(argv=None):\n import sys; sys.stdout.buffer.write(b'--selection-snapshot\\0fixture.json\\0')\n")
        for configured, expected in [('', str(self.cache_root)), ('/explicit-repo-cache', '/explicit-repo-cache')]:
            with self.subTest(configured=configured):
                prepared = self.output / 'prepared'
                if prepared.exists():
                    shutil.rmtree(prepared)
                result = subprocess.run(['bash', '-c', script], cwd=self.repo,
                    env={**self.env, 'E2E_OUTPUT': str(self.output), 'CONFIGURED_CACHE_ROOT': configured,
                         'HRX_SYSTEMS_ROOT': str(self.cache_root), 'CI_PLAN':'fixture', 'LANE_ID':'latest-release'}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                arguments = (self.arguments / 'prepare').read_bytes().decode().split('\0')
                self.assertEqual(arguments[arguments.index('--root') + 1], expected)

    def test_workflow_uses_runner_owned_outputs_and_compact_allowlist(self):
        workflow = (ROOT / '.github/workflows/_model-verification-worker.yml').read_text()
        self.assertIn('output="$RUNNER_TEMP/', workflow)
        self.assertIn('persist-credentials: false', workflow)
        self.assertNotIn('concurrency:', workflow)
        self.assertIn('cancel-in-progress: false', (ROOT / '.github/workflows/single-model-e2e.yml').read_text())
        upload = workflow.split('      - name: Upload compact report',1)[1].split('          path: |\n', 1)[1].split('          if-no-files-found:', 1)[0]
        self.assertIn('/report/', upload)
        for suffix in ('container.log', 'models/*/prepared-inputs.json', 'models/*/seed-manifest.json',
                       'models/*/online.log', 'models/*/offline.log',
                       'models/*/sdk-online/sdk.json', 'models/*/sdk-offline/sdk.json'):
            self.assertIn('/results/' + suffix, upload)
        self.assertIn('CONFIGURED_CACHE_ROOT: ${{ vars.HRX_SYSTEMS_ROOT }}', workflow)
        self.assertNotIn('      HRX_SYSTEMS_ROOT: ${{ vars.HRX_SYSTEMS_ROOT }}', workflow)
        self.assertNotRegex(upload, r'/(?:prepared|cache|sdk-payloads)/|/results/\s*$')
        for name in ('test/prepare-cached-models.sh', 'test/run-prepared-models.sh', 'report/generate-model-report.sh'):
            self.assertIn('bash scripts/' + name, workflow)


if __name__ == '__main__':
    unittest.main()
