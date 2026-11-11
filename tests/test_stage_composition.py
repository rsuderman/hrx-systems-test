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
    --root|--suite|--prepared|--results|--workflow|--device-uri|--cycles) shift 2;;
    --cleanup) shift;;
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
    source = (ROOT / '.github/workflows/single-model-e2e.yml').read_text()
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
        scripts = self.root / 'repo/scripts'
        scripts.mkdir(parents=True)
        self.script = scripts / 'run-cached-models.sh'
        shutil.copyfile(ROOT / 'scripts/run-cached-models.sh', self.script)
        for name, kind in [('prepare-cached-models.sh', 'prepare'),
                           ('run-prepared-models.sh', 'run'),
                           ('generate-model-report.sh', 'report')]:
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
        while 'run' not in self.order() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn('run', self.order())
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=25)
        self.assertEqual(process.returncode, 143, stdout + stderr)
        self.assertEqual(self.order(), ['prepare', 'run', 'cleanup', 'report'])
        self.assertEqual(self.status('run')['exit_code'], 143)
        self.assertTrue((self.output / 'prepared').exists())

    def test_workflow_final_gate_rejects_upload_and_stage_failures(self):
        script = workflow_run_block('Require all stages and publication to pass')
        env = {**self.env, **{name + '_OUTCOME': 'success'
               for name in ('INITIALIZE', 'PREPARE', 'RUN', 'CLEANUP', 'REPORT', 'UPLOAD', 'SUMMARY')}}
        good = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stderr)
        for stage in ('PREPARE', 'RUN', 'CLEANUP', 'REPORT', 'UPLOAD', 'SUMMARY'):
            with self.subTest(stage=stage):
                bad = subprocess.run(['bash', '-c', script], env={**env, stage + '_OUTCOME': 'failure'},
                    capture_output=True, text=True)
                self.assertNotEqual(bad.returncode, 0)

    def publish_summary(self, *, upload='success', artifact_url=None, rendered=True, workflow_metadata=True):
        # Recorded real run/artifact identity; this test performs no network calls.
        self.artifact_url = 'https://github.com/rsuderman/hrx-systems-test/actions/runs/42933895801/artifacts/13816663497'
        self.output.mkdir()
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
            generated = subprocess.run([sys.executable, '-I', str(ROOT / 'scripts/generate-model-report.py'),
                '--results', str(results), '--output', str(self.output / 'report')],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(generated.returncode, 0, generated.stderr)
        summary_path = self.root / 'github-summary.md'
        env = {**self.env, 'E2E_OUTPUT': str(self.output), 'GITHUB_STEP_SUMMARY': str(summary_path),
               'ARTIFACT_URL': self.artifact_url if artifact_url is None else artifact_url,
               'UPLOAD_OUTCOME': upload, 'PREPARE_OUTCOME': 'success', 'RUN_OUTCOME': 'success',
               'CLEANUP_OUTCOME': 'success', 'REPORT_OUTCOME': 'success' if rendered else 'failure'}
        published = subprocess.run(['bash', '-c', workflow_run_block('Publish readable summary and report download')],
            env=env, capture_output=True, text=True, timeout=10)
        return published, summary_path.read_text()

    def test_workflow_publishes_summary_after_upload_with_exact_artifact_url(self):
        workflow = (ROOT / '.github/workflows/single-model-e2e.yml').read_text()
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
        workflow = (ROOT / '.github/workflows/single-model-e2e.yml').read_text()
        for name in ('Prepare fresh pipelines and read-only cached assets',
                     'Run the prepared suite offline through one server'):
            section = workflow.split('      - name: ' + name + '\n', 1)[1].split('\n      - ', 1)[0]
            self.assertIn("!cancelled() && steps.initialize.outcome == 'success'", section)
        for name in ('Confirm execution-owned container cleanup',
                     'Generate verification artifacts from saved results'):
            section = workflow.split('      - name: ' + name + '\n', 1)[1].split('\n      - ', 1)[0]
            self.assertIn("always() && steps.initialize.outcome == 'success'", section)

    def test_workflow_cache_root_preserves_runner_environment_without_repo_override(self):
        script = workflow_run_block('Prepare fresh pipelines and read-only cached assets')
        self.output.mkdir()
        for configured, expected in [('', str(self.cache_root)), ('/explicit-repo-cache', '/explicit-repo-cache')]:
            with self.subTest(configured=configured):
                prepared = self.output / 'prepared'
                if prepared.exists():
                    shutil.rmtree(prepared)
                result = subprocess.run(['bash', '-c', script], cwd=self.script.parent.parent,
                    env={**self.env, 'E2E_OUTPUT': str(self.output), 'CONFIGURED_CACHE_ROOT': configured,
                         'HRX_SYSTEMS_ROOT': str(self.cache_root)}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                arguments = (self.arguments / 'prepare').read_bytes().decode().split('\0')
                self.assertEqual(arguments[arguments.index('--root') + 1], expected)

    def test_workflow_uses_runner_owned_outputs_and_compact_allowlist(self):
        workflow = (ROOT / '.github/workflows/single-model-e2e.yml').read_text()
        self.assertIn('output="$RUNNER_TEMP/', workflow)
        self.assertIn('persist-credentials: false', workflow)
        self.assertIn('cancel-in-progress: false', workflow)
        upload = workflow.split('          path: |\n', 1)[1].split('          if-no-files-found:', 1)[0]
        self.assertIn('/report/', upload)
        for suffix in ('container.log', 'models/*/prepared-inputs.json', 'models/*/seed-manifest.json',
                       'models/*/online.log', 'models/*/offline.log',
                       'models/*/sdk-online/sdk.json', 'models/*/sdk-offline/sdk.json'):
            self.assertIn('/results/' + suffix, upload)
        self.assertIn('CONFIGURED_CACHE_ROOT: ${{ vars.HRX_SYSTEMS_ROOT }}', workflow)
        self.assertNotIn('      HRX_SYSTEMS_ROOT: ${{ vars.HRX_SYSTEMS_ROOT }}', workflow)
        self.assertNotRegex(upload, r'/(?:prepared|cache|sdk-payloads)/|/results/\s*$')
        for name in ('prepare-cached-models.sh', 'run-prepared-models.sh', 'generate-model-report.sh'):
            self.assertIn('bash scripts/' + name, workflow)


if __name__ == '__main__':
    unittest.main()
