"""Saved runtime provenance, human presentation and early-failure truth."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

import runpy
runpy.run_path(str(ROOT / 'python/run.py'))['bootstrap']()
from hrx_systems.reporting import render, failures, package_failures, runtime_identity


SUMMARY = runtime_identity
REPORT = render
PACKAGE = package_failures
RECORDS = failures
SHA = 'd01d1c18b154189fd073777329052d23f6071f14'
SDK = {'name': 'hrx-loom', 'version': '0.1.0', 'release': 'v0.1.0',
       'repository': 'ROCm/hrx-system', 'source_commit': SHA}


class RuntimeSummaryTests(unittest.TestCase):
    def test_apostrophes_and_literal_entities_remain_readable_and_inert(self):
        for module in (SUMMARY, REPORT):
            value = "model's <script> &lt;fake&gt; [bad](javascript:alert(1))"
            rendered = ('\n'.join(module.markdown(SUMMARY.describe({'sdk': {**SDK, 'name': value}})))
                        if module is SUMMARY else module.markdown(value))
            self.assertIn("model&#x27;s", rendered)
            self.assertNotIn('&\\#x27;', rendered)
            self.assertNotIn('<script>', rendered)
            self.assertIn('&amp;lt;fake&amp;gt;', rendered)
            self.assertNotIn('[bad](javascript:', rendered)

    def test_installed_receipt_can_supply_missing_legacy_metadata(self):
        receipt = {'status': 'ready', 'identity': {'sdk': SDK}, 'installation': {'sdk_version': '0.1.0'}}
        identity = SUMMARY.describe({}, installation=receipt)
        self.assertEqual(identity['version'], '0.1.0')
        self.assertEqual(identity['source_commit'], SHA)
        self.assertEqual(identity['state'], 'installed')

    def test_legacy_release_adapter_and_selected_state(self):
        identity = SUMMARY.describe({'sdk_identity': {'sdk': SDK}})
        self.assertEqual(identity['state'], 'selected')
        self.assertEqual(identity['source_commit'], SHA)
        md = '\n'.join(SUMMARY.markdown(identity))
        self.assertIn('hrx-loom 0.1.0', md)
        self.assertIn('**Release:** v0.1.0', md)
        self.assertIn(f'https://github.com/ROCm/hrx-system/commit/{SHA}', md)
        self.assertIn('execution not reached/unconfirmed', md)

    def test_receipt_and_server_prove_different_states(self):
        receipt = {'status': 'ready', 'identity': {'sdk': SDK}, 'installation': {'sdk_version': '0.1.0'}}
        self.assertEqual(SUMMARY.describe({'sdk': SDK}, installation=receipt)['state'], 'installed')
        self.assertEqual(SUMMARY.describe({'sdk': SDK}, server={'start': {'status': 'passed'}}, mode='real')['state'], 'exercised')
        self.assertEqual(SUMMARY.describe({'sdk': SDK}, server={'start': {'status': 'passed'}}, mode='fixture')['state'], 'fixture')
        receipt['installation']['sdk_version'] = 'wrong'
        with self.assertRaisesRegex(ValueError, 'version disagrees'):
            SUMMARY.describe({'sdk': SDK}, installation=receipt)

    def test_partial_or_absent_identity_never_uses_other_sources(self):
        for value in (None, [], {'sdk': 'bad'}, {'sdk_identity': None}):
            self.assertEqual(SUMMARY.describe(value)['state'], 'unavailable')
        partial = SUMMARY.describe({'sdk': {'version': '0.1.0'}})
        self.assertIsNone(partial['source_commit'])
        self.assertIn('Unavailable', '\n'.join(SUMMARY.markdown(partial)))

    def test_resolved_actions_same_and_behind_head(self):
        for relation in ('equal', 'behind'):
            source = {'kind': 'github_actions', 'repository': 'ROCm/hrx-system', 'tested_sha': SHA,
                      'requested_head_sha': SHA if relation == 'equal' else 'a' * 40,
                      'relation_to_head': relation, 'run_id': 123, 'run_attempt': 2, 'build_age_seconds': 400}
            runtime = {'resolved_runtime': {'source': source, 'wheel': {'name': 'hrx-loom', 'version': '0.2.0'}}}
            text = '\n'.join(SUMMARY.markdown(SUMMARY.describe(runtime)))
            self.assertIn('hrx-loom 0.2.0', text)
            self.assertIn('Run 123 / attempt 2', text)
            self.assertIn('https://github.com/ROCm/hrx-system/actions/runs/123', text)
            self.assertIn('Behind requested HEAD' if relation == 'behind' else 'Matches requested HEAD', text)
            self.assertIn('400 seconds', text)

    def test_conflicting_or_malformed_resolved_identity_rejected(self):
        for resolved in (None, [], {'source': None, 'wheel': {}}):
            with self.assertRaisesRegex(ValueError, 'Malformed'):
                SUMMARY.describe({'resolved_runtime': resolved})
        with self.assertRaisesRegex(ValueError, 'disagree: version'):
            SUMMARY.describe({'sdk': SDK, 'resolved_runtime': {'source': {'tested_sha': SHA}, 'wheel': {'version': '0.2.0'}}})

    def test_malformed_build_relation_cannot_abort_failure_fallback(self):
        manifest = {'counts': {}, 'cases': [
            {'display_name': 'Failure', 'phase': 'load', 'summary': 'Original cause'}]}
        for relation in ([], {}, True, 123):
            runtime = {'resolved_runtime': {'source': {
                'repository': 'ROCm/hrx-system', 'tested_sha': SHA,
                'requested_head_sha': 'a' * 40, 'relation_to_head': relation},
                'wheel': {'name': 'hrx-loom', 'version': '0.1.0'}}}
            text = RECORDS.fallback_summary(manifest, runtime)
            self.assertIn('Original cause', text)
            self.assertIn('**Build relation:** Unconfirmed', text)

    def test_safe_links_escaping_and_bounds(self):
        for repo, sha in [('evil/x) [bad](http://bad)', SHA), ('ROCm/..', SHA), ('ROCm/hrx-system', 'not-a-sha')]:
            identity = SUMMARY.describe({'sdk': {**SDK, 'repository': repo, 'source_commit': sha}})
            self.assertNotIn('href=', SUMMARY.render_html(identity))
        identity = SUMMARY.describe({'sdk': {**SDK, 'name': '<script>alert(1)</script>' * 100}})
        output = SUMMARY.render_html(identity)
        self.assertNotIn('<script>', output)
        self.assertLess(len(identity['name']), 513)

    def test_report_identity_before_results_and_distinct_harness(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            results = root / 'results'
            shutil.copytree(ROOT / 'tests/fixtures/model-report/passing', results)
            path = results / 'suite-result.json'
            raw = json.loads(path.read_text())
            raw['runtime'] = {'sdk': SDK}
            raw['source']['commit'] = 'b' * 40
            path.write_text(json.dumps(raw))
            result = REPORT.generate(results, root / 'report')
            md = (root / 'report/summary.md').read_text()
            html = (root / 'report/index.html').read_text()
            self.assertLess(md.index('### HRX build'), md.index('<summary>Passed'))
            self.assertLess(html.index('aria-label="HRX build"'), html.index('id="passed"'))
            self.assertIn('**Harness source:** bbbbbbbbbbbb', md)
            self.assertIn(SHA, (root / 'report/report.json').read_text())
            self.assertEqual(result['counts']['passed'], 1)

    def test_fallback_early_prepared_identity_and_missing_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'prepared').mkdir()
            (root / 'prepared/prepared-suite.json').write_text(json.dumps({'runtime': {'sdk': SDK}, 'entries': []}))
            PACKAGE.package(root, root / 'failures')
            text = (root / 'failures/summary.md').read_text()
            self.assertIn('hrx-loom 0.1.0', text)
            self.assertIn('Selected; execution not reached/unconfirmed', text)
            self.assertIn('HRX source', text)
        text = RECORDS.fallback_summary({'counts': {}, 'cases': []})
        self.assertIn('**HRX runtime:** Unavailable', text)

    def test_fallback_identity_conflict_retains_failure_diagnostics(self):
        runtime = {'sdk': SDK, 'resolved_runtime': {'wheel': {'version': 'wrong'}, 'source': {}}}
        text = RECORDS.fallback_summary({'counts': {}, 'cases': [{'display_name': 'Failure', 'phase': 'load', 'summary': 'Original cause'}]}, runtime)
        self.assertIn('HRX identity evidence error', text)
        self.assertIn('Original cause', text)
