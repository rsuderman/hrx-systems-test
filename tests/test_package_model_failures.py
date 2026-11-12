"""Saved-only failure records, safe packaging and bounded capture fixtures."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


RECORDS = module('failure-records')
PACKAGE = module('package-model-failures')
CAPTURE = module('evidence-capture')


class FailureBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / 'tests/fixtures/model-report/passing', self.root / 'results')
        self.raw_path = self.root / 'results/suite-result.json'
        self.raw = json.loads(self.raw_path.read_text())

    def save(self):
        self.raw_path.write_text(json.dumps(self.raw))

    def fail(self, phase='readiness', reason='RuntimeError: server expects 11 arguments; pipeline declares 10'):
        self.raw['models'][0]['status'] = 'failed'
        self.raw['models'][0]['cycles'][0][phase] = {'status': 'failed', 'reason': reason}
        self.raw['status'] = 'failed'
        self.save()

    def test_actual_host_source_argument_mismatch_is_concise(self):
        diagnostic = "RuntimeError: {'error': 'argument count mismatch: host expects 11, source declares 10'}"
        self.assertEqual(RECORDS.summarize(diagnostic),
                         'Server expects 11 arguments; pipeline declares 10')
        self.fail(reason=diagnostic)
        manifest, code = PACKAGE.package(self.root, self.root / 'failures')
        self.assertEqual(code, 0)
        self.assertEqual(manifest['cases'][0]['summary'], 'Server expects 11 arguments; pipeline declares 10')
        self.assertEqual(manifest['cases'][0]['diagnostic'], diagnostic)

    def test_all_pass_omits_zip(self):
        manifest, code = PACKAGE.package(self.root, self.root / 'failures', self.root / 'failures.zip')
        self.assertEqual(code, 0)
        self.assertFalse(manifest['cases'])
        self.assertFalse((self.root / 'failures.zip').exists())
        self.assertFalse((self.root / 'failures/README.md').exists())

    def test_one_readable_case_and_no_heavy_payloads(self):
        self.fail()
        (self.root / 'results/weights.gguf').write_bytes(b'immutable weights')
        manifest, code = PACKAGE.package(self.root, self.root / 'failures', self.root / 'failures.zip')
        self.assertEqual(code, 0)
        self.assertEqual(manifest['counts']['failed'], 1)
        case = manifest['cases'][0]
        self.assertEqual(case['phase'], 'readiness')
        self.assertIn('expects 11 arguments', case['summary'])
        with zipfile.ZipFile(self.root / 'failures.zip') as archive:
            names = archive.namelist()
            self.assertIn('README.md', names)
            self.assertFalse(any(name.endswith(('.gguf', '.zip', '.whl')) for name in names))
            self.assertTrue(any('request.json' in name for name in names))
        self.assertEqual((self.root / 'results/weights.gguf').read_bytes(), b'immutable weights')

    def test_mixed_45_counts_and_only_failed_payloads(self):
        template, expected = copy.deepcopy(self.raw['models'][0]), copy.deepcopy(self.raw['expected_entries'][0])
        self.raw['models'], self.raw['expected_entries'] = [], []
        for i in range(45):
            model, entry = copy.deepcopy(template), copy.deepcopy(expected)
            model['id'] = entry['id'] = f'model-{i}'
            if i >= 40:
                model['status'] = 'failed'
                model['cycles'][0]['assertion'] = {'status': 'failed', 'reason': 'answer differs'}
            self.raw['models'].append(model); self.raw['expected_entries'].append(entry)
        self.raw['status'] = 'failed'; self.save()
        manifest, code = PACKAGE.package(self.root, self.root / 'failures')
        self.assertEqual(manifest['counts'], {'expected': 45, 'passed': 40, 'failed': 5, 'not_run': 0, 'incomplete': 0})
        self.assertEqual(len(manifest['cases']), 5)
        self.assertEqual(code, 0)

    def test_first_failed_phase_preserves_independent_unload(self):
        self.fail()
        self.raw['models'][0]['cycles'][0]['query'] = {'status': 'not_run', 'reason': 'not attempted'}
        self.raw['models'][0]['cycles'][0]['unload'] = {'status': 'failed', 'reason': 'unregister timeout'}
        self.save()
        case = RECORDS.collect(self.root)['cases'][0]
        self.assertEqual(case['phase'], 'readiness')
        self.assertEqual([v['phase'] for v in case['observations']], ['readiness', 'unload'])

    def test_standalone_results_ignore_unrelated_parent_stage_state(self):
        before = RECORDS.collect_results(self.root / 'results')
        (self.root / 'stages.json').write_text(json.dumps({'stages': {'report': 'failure'}, 'interrupted': 143}))
        after = RECORDS.collect_results(self.root / 'results')
        self.assertEqual(before, after)
        self.assertFalse(after['cases'])

    def test_malformed_cycle_writes_minimal_safe_failure_bundle(self):
        for cycles in ([None], {'wrong': 'type'}, [{'query': None}]):
            with self.subTest(cycles=cycles):
                self.raw['models'][0]['cycles'] = cycles
                self.save()
                manifest, code = PACKAGE.package(self.root, self.root / 'failures')
                self.assertEqual(code, 1)
                self.assertTrue(manifest['evidence_errors'])
                self.assertTrue((self.root / 'failures/README.md').is_file())

    def test_fallback_summary_uses_safe_readable_saved_failure_facts(self):
        self.fail('load', 'Invalid pipeline | <script> [bad](javascript:alert(1))')
        manifest, code = PACKAGE.package(self.root, self.root / 'failures')
        summary = (self.root / 'failures/summary.md').read_text()
        self.assertEqual(code, 0)
        self.assertIn('Expected: **1**', summary)
        self.assertIn('## Failures', summary)
        self.assertIn('Invalid pipeline &#124; &lt;script&gt;', summary)
        self.assertNotIn('[bad](', summary)
        self.assertNotIn(str(manifest['counts']), summary)

    def test_raw_passed_cannot_hide_failed_or_missing_phase(self):
        self.raw['models'][0]['cycles'][0]['query'] = {'status': 'failed', 'reason': 'query timeout'}
        self.save()
        self.assertEqual(RECORDS.collect(self.root)['counts']['failed'], 1)
        del self.raw['models'][0]['cycles'][0]['query']; self.save()
        manifest = RECORDS.collect_results(self.root / 'results')
        self.assertEqual(manifest['counts']['incomplete'], 1)
        self.assertTrue(manifest['cases'])

    def test_missing_results_preserves_prepare_and_report_errors(self):
        self.raw_path.unlink()
        (self.root / 'stages.json').write_text(json.dumps({'stages': {'prepare': 'failure', 'run': 'skipped', 'report': 'failure'}, 'interrupted': 143}))
        (self.root / 'prepare.log').write_text('SDK failed')
        manifest, code = PACKAGE.package(self.root, self.root / 'failures', self.root / 'failure.zip')
        self.assertEqual(code, 0)
        self.assertIsNone(manifest['counts']['expected'])
        self.assertTrue({'prepare', 'report', 'interrupted', 'results'}.issubset({v['phase'] for v in manifest['cases']}))
        self.assertTrue((self.root / 'failure.zip').exists())

    def test_no_query_marked_unavailable_and_missing_evidence_reported(self):
        self.fail('load', 'registration failed')
        cycle = self.raw['models'][0]['cycles'][0]
        del cycle['request_path']; del cycle['response_path']
        self.raw['models'][0]['evidence'].append('models/qwen-08b/missing.log'); self.save()
        manifest, code = PACKAGE.package(self.root, self.root / 'failures')
        self.assertEqual(code, 0)
        self.assertTrue(manifest['cases'][0]['missing_evidence'])
        self.assertTrue(any(v['missing'] for v in manifest['cases'][0]['evidence']))

    def test_escaping_and_symlink_references_fail_safely(self):
        self.fail()
        outside = self.root / 'secret.json'; outside.write_text('secret')
        (self.root / 'results/link.json').symlink_to(outside)
        for reference in ['../secret.json', 'link.json']:
            self.raw['models'][0]['evidence'] = [reference]; self.save()
            manifest, code = PACKAGE.package(self.root, self.root / 'failures')
            self.assertEqual(code, 1)
            self.assertTrue(manifest['evidence_errors'])
            self.assertFalse(any(p.read_text() == 'secret' for p in (self.root / 'failures').rglob('*.json')))

    def test_idempotent_render_failure_second_pass(self):
        self.fail()
        first, _ = PACKAGE.package(self.root, self.root / 'failures')
        (self.root / 'stages.json').write_text(json.dumps({'stages': {'run': 'failure', 'report': 'failure'}}))
        second, _ = PACKAGE.package(self.root, self.root / 'failures')
        self.assertEqual(first['cases'][0]['case_id'], second['cases'][0]['case_id'])
        self.assertEqual(first['counts'], second['counts'])
        self.assertIn('report', [v['phase'] for v in second['cases']])

    def test_response_length_summary_only_when_saved_facts_exist(self):
        response = {'choices': [{'message': {'content': ''}, 'finish_reason': 'length'}], 'usage': {'completion_tokens': 256}}
        self.assertIn('256 completion tokens', RECORDS.summarize('Answer mismatch', response, '4'))
        self.assertNotIn('256', RECORDS.summarize('Answer mismatch'))

    def test_malformed_response_message_retains_literal_without_breaking_package(self):
        self.fail('assertion', 'Answer mismatch')
        path = self.root / 'results' / self.raw['models'][0]['cycles'][0]['response_path']
        path.write_text(json.dumps({'choices': [{'message': 'bad-shape', 'finish_reason': 'length'}]}))
        manifest, code = PACKAGE.package(self.root, self.root / 'failures')
        self.assertEqual(code, 0)
        self.assertEqual(manifest['cases'][0]['summary'], 'Answer mismatch')

    def test_malformed_response_usage_retains_literal_without_breaking_package(self):
        self.fail('assertion', 'Answer mismatch')
        path = self.root / 'results' / self.raw['models'][0]['cycles'][0]['response_path']
        path.write_text(json.dumps({'choices': [{'message': {'content': ''}, 'finish_reason': 'length'}],
            'usage': 'bad-shape'}))
        manifest, code = PACKAGE.package(self.root, self.root / 'failures')
        self.assertEqual(code, 0)
        self.assertEqual(manifest['cases'][0]['summary'], 'Answer mismatch')

    def test_bounded_log_retains_tail_and_original_size(self):
        path = self.root / 'server.log'
        CAPTURE.drain_log(io.BufferedReader(io.BytesIO(b'HEAD' + b'x' * 10000 + b'LATE FAILURE')), path, 1024)
        data = path.read_bytes()
        self.assertLessEqual(len(data), 1024)
        self.assertTrue(data.startswith(b'HEAD'))
        self.assertTrue(data.endswith(b'LATE FAILURE'))
        metadata = json.loads(path.with_name('server.log.capture.json').read_text())
        self.assertTrue(metadata['truncated']); self.assertEqual(metadata['original_size'], 10016)

    def test_sources_allowlist_digest_no_weights_or_symlinks(self):
        import hashlib
        prepared = self.root / 'prepared'
        model = prepared / 'models/first/model'
        model.mkdir(parents=True)
        (model / 'control.loom').write_text('func @main()')
        (model / 'weights.gguf').write_bytes(b'weights')
        good = {'path': 'control.loom', 'sha256': hashlib.sha256(b'func @main()').hexdigest()}
        manifest = prepared / 'models/first/prepared-inputs.json'
        manifest.write_text(json.dumps({'programs': [good, {'path': 'weights.gguf', 'sha256': 'unused'}]}))
        suite = {'entries': [{'id': 'first', 'inputs_manifest': 'models/first/prepared-inputs.json'}]}
        result = {'models': [{'cycles': [{'readiness': {'status': 'failed'}}], 'evidence': []}]}
        CAPTURE.failed_sources(prepared, self.root / 'results', suite, result)
        source = self.root / 'results/models/first/pipeline/control.loom'
        self.assertEqual(source.read_text(), 'func @main()')
        self.assertFalse(source.with_name('weights.gguf').exists())
        source.unlink()
        (model / 'control.loom').unlink(); (model / 'control.loom').symlink_to(self.root / 'secret.json')
        (self.root / 'secret.json').write_text('secret')
        CAPTURE.failed_sources(prepared, self.root / 'results', suite, result)
        self.assertFalse(source.exists())
        metadata = json.loads(source.with_name('capture.json').read_text())
        self.assertTrue(metadata['errors'])

    def test_capture_rejects_destination_symlink_before_writing(self):
        outside = self.root / 'outside'; outside.mkdir()
        output = self.root / 'capture'; output.mkdir()
        (output / 'models').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'destination is a symlink'):
            CAPTURE.safe_destination(output, 'models/one/pipeline/control.loom')
        self.assertFalse(list(outside.iterdir()))

    def test_bounded_copy_limits_are_explicit_and_sources_not_truncated(self):
        from unittest import mock
        self.fail()
        (self.root / 'results/container.log').write_bytes(b'HEAD' + b'x' * 1024 + b'TAIL')
        with mock.patch.object(PACKAGE, 'MAX_LOG', 128):
            manifest, code = PACKAGE.package(self.root, self.root / 'failures')
        record = next(item for item in manifest['shared_evidence'] if item['path'].endswith('container.log'))
        self.assertEqual(code, 0)
        self.assertTrue(record['truncated'])
        self.assertEqual(record['original_size'], 1032)
        self.assertLessEqual(record['retained_size'], 128)
        self.assertTrue((self.root / 'failures' / record['path']).read_bytes().endswith(b'TAIL'))

    def test_unsafe_extraction_still_writes_error_manifest_and_readme(self):
        self.raw['expected_entries'].append(copy.deepcopy(self.raw['expected_entries'][0])); self.save()
        manifest, code = PACKAGE.package(self.root, self.root / 'failures', self.root / 'failures.zip')
        self.assertEqual(code, 1)
        self.assertTrue(manifest['evidence_errors'])
        self.assertTrue((self.root / 'failures/README.md').is_file())

    def test_non_evidence_json_is_never_packaged(self):
        self.fail()
        (self.root / 'results/tokenizer.json').write_text('{"private":"asset"}')
        self.raw['models'][0]['evidence'] = ['tokenizer.json']; self.save()
        manifest, code = PACKAGE.package(self.root, self.root / 'failures')
        self.assertEqual(code, 1)
        self.assertTrue(manifest['cases'][0]['evidence'][0]['missing'])
        self.assertFalse(any(path.name == 'tokenizer.json' for path in (self.root / 'failures').rglob('*')))

    def test_preparation_snapshot_survives_cleanup(self):
        prepared = self.root / 'prepared'; prepared.mkdir()
        suite = {'schema_version': 1, 'entries': [{'id': 'one', 'selection': {'model_id': 'one'}, 'preparation': {'status': 'failed', 'reason': 'pipeline unavailable'}, 'evidence': []}]}
        (prepared / 'prepared-suite.json').write_text(json.dumps(suite))
        CAPTURE.preparation_snapshot(prepared, suite)
        self.raw_path.unlink()
        first, _ = PACKAGE.package(self.root, self.root / 'failures')
        shutil.rmtree(prepared)
        second, _ = PACKAGE.package(self.root, self.root / 'failures')
        self.assertEqual(first['counts'], second['counts'])
        self.assertEqual(second['counts']['failed'], 1)


if __name__ == '__main__':
    unittest.main()
