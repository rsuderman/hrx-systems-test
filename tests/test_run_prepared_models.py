"""Real subprocess/HTTP fixtures for sequential native-model lifecycle control."""
import argparse
import copy
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('run_prepared_models', ROOT / 'scripts/run-prepared-models.py')
RUN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUN)

SERVER = r'''
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json,sys
mode=sys.argv[2]
rows={}
counter=0
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def reply(self,status,data):
  encoded=json.dumps(data).encode();self.send_response(status)
  self.send_header('Content-Length',str(len(encoded)));self.end_headers();self.wfile.write(encoded)
 def do_GET(self):
  if self.path=='/readyz':
   if mode=='final_unhealthy' and counter==2:return self.reply(503,{'status':'failed'})
   return self.reply(200,{'status':'ready'})
  for name in list(rows):
   row=rows[name];row['ticks']+=1
   if row.get('removing') and mode not in ('stuck','release_failed') and row['ticks']>=2:
    del rows[name]
  data=[]
  for name,row in rows.items():
   state='preparing' if row['ticks']<2 or mode=='timeout' else 'ready'
   if mode=='load_failed' and name=='first':state='failed'
   if row.get('removing'):state='release_failed' if mode=='release_failed' else 'retiring'
   loom={'incarnation':row['incarnation'],'registration':state,'status':{'admission':'open'}}
   if state=='failed':loom['error']='fixture preparation failure'
   data.append({'id':name,'loom':loom})
  if self.path=='/v1/status':return self.reply(200,{'data':data})
  if self.path=='/v1/models':return self.reply(200,{'data':[x for x in data if x['loom']['registration']=='ready']})
  self.reply(404,{})
 def do_POST(self):
  global counter
  payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  if self.path.endswith('/register'):
   counter+=1;rows[payload['name']]={'incarnation':counter,'ticks':0}
   if mode=='lost' and payload['name']=='first':
    self.close_connection=True;return
   return self.reply(202,{})
  if self.path.endswith('/unregister'):
   if payload['model'] in rows:rows[payload['model']].update(removing=True,ticks=0)
   return self.reply(202,{})
  if self.path=='/v1/chat/completions':
   if mode=='malformed':return self.reply(200,{})
   answer='wrong' if mode=='wrong' and payload['model']=='first' else '4'
   return self.reply(200,{'choices':[{'message':{'content':answer}}]})
  self.reply(404,{})
ThreadingHTTPServer(('127.0.0.1',int(sys.argv[1])),Handler).serve_forever()
'''


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.server = self.root / 'server.py'
        self.server.write_text(SERVER)
        with socket.socket() as port:
            port.bind(('127.0.0.1', 0))
            self.port = port.getsockname()[1]
        self.limits = argparse.Namespace(startup_timeout=2, readiness_timeout=0.3,
            query_timeout=0.3, unload_timeout=0.15, abort_timeout=0.15,
            shutdown_timeout=1, cycles=1)
        self.suite = {'schema_version': 1, 'attempt_id': 'fixture', 'source': {'commit': 'fixture'},
            'runtime': {'image': 'fixture'}, 'entries': []}
        for name in ('first', 'second'):
            self.suite['entries'].append({'id': name, 'selection': {'model_id': name,
                'preset': 'chat', 'variant': 'gguf', 'programs': 'source'},
                'test_kind': 'text_smoke', 'smoke_request': {'messages': [{'role': 'user', 'content': '2+2?'}],
                'max_tokens': 8, 'temperature': 0, 'stream': False}, 'smoke_expected_text': '4',
                'preparation': {'status': 'ready'}, 'evidence': []})

    def run_fixture(self, mode='normal', command=None):
        output = self.root / 'results'
        output.mkdir()
        result = RUN.initial_results(self.suite, {'name': 'Fixture CPU'}, mode='fixture')
        command = command or [sys.executable, '-I', str(self.server), str(self.port), mode]
        code = RUN.execute_lifecycle(self.suite, result, output, command,
                                     f'http://127.0.0.1:{self.port}', self.limits)
        self.assertEqual(result, json.loads((output / 'suite-result.json').read_text()))
        self.assertEqual(result['server']['cleanup']['status'], 'passed')
        return code, result, output

    def test_two_models_repeat_in_same_owned_process_and_collect_evidence(self):
        self.limits.cycles = 2
        code, result, output = self.run_fixture()
        self.assertEqual(code, 0)
        self.assertEqual(len(result['expected_entries']), 2)
        incarnations = [cycle['incarnation'] for model in result['models'] for cycle in model['cycles']]
        self.assertEqual(incarnations, [1, 2, 3, 4])
        for model in result['models']:
            for cycle in model['cycles']:
                self.assertTrue(all(cycle[key]['status'] == 'passed' for key in RUN.PHASES))
                self.assertEqual(json.loads((output / cycle['response_path']).read_text())['choices'][0]['message']['content'], '4')
        self.assertEqual(result['server']['shutdown']['status'], 'passed')

    def test_wrong_answer_fails_but_unloads_and_continues(self):
        code, result, _ = self.run_fixture('wrong')
        self.assertEqual(code, 1)
        self.assertEqual([m['status'] for m in result['models']], ['failed', 'passed'])
        self.assertEqual(result['models'][0]['cycles'][0]['unload']['status'], 'passed')

    def test_accepted_registration_with_lost_response_is_removed(self):
        code, result, _ = self.run_fixture('lost')
        self.assertEqual(code, 1)
        first = result['models'][0]['cycles'][0]
        self.assertEqual(first['load']['status'], 'failed')
        self.assertEqual(first['unload']['status'], 'passed')
        self.assertEqual(result['models'][1]['status'], 'passed')

    def test_terminal_preparation_failure_does_not_block_next_after_removal(self):
        code, result, _ = self.run_fixture('load_failed')
        self.assertEqual(code, 1)
        self.assertEqual(result['models'][0]['cycles'][0]['readiness']['status'], 'failed')
        self.assertEqual(result['models'][1]['status'], 'passed')

    def test_unconfirmed_removal_stops_loop_and_shuts_server(self):
        code, result, _ = self.run_fixture('stuck')
        self.assertEqual(code, 1)
        self.assertEqual(result['models'][0]['cycles'][0]['unload']['status'], 'failed')
        self.assertEqual(result['models'][1]['status'], 'not_run')
        self.assertEqual(result['server']['shutdown']['status'], 'passed')

    def test_release_failure_stops_remaining_entries(self):
        code, result, _ = self.run_fixture('release_failed')
        self.assertEqual(code, 1)
        self.assertIn('release failed', result['models'][0]['cycles'][0]['unload']['reason'])
        self.assertEqual(result['models'][1]['cycles'], [])

    def test_readiness_timeout_cannot_pass(self):
        code, result, _ = self.run_fixture('timeout')
        self.assertEqual(code, 1)
        self.assertTrue(all(m['status'] == 'failed' for m in result['models']))
        self.assertTrue(all(m['cycles'][0]['unload']['status'] == 'passed' for m in result['models']))

    def test_malformed_response_cannot_pass(self):
        code, result, _ = self.run_fixture('malformed')
        self.assertEqual(code, 1)
        self.assertTrue(all(m['cycles'][0]['assertion']['status'] == 'failed' for m in result['models']))
        self.assertTrue(all(m['cycles'][0]['unload']['status'] == 'passed' for m in result['models']))

    def test_missing_preparation_selection_stays_visible(self):
        self.suite['entries'][0] = {'id': 'first', 'preparation': {'status': 'failed', 'reason': 'no baseline'}}
        code, result, _ = self.run_fixture()
        self.assertEqual(code, 1)
        self.assertEqual(result['expected_entries'][0]['selection'], {})
        self.assertEqual(result['models'][0]['preparation']['reason'], 'no baseline')
        self.assertEqual(result['models'][1]['status'], 'passed')

    def test_server_exit_cannot_pass_and_no_unrelated_server_is_used(self):
        code, result, _ = self.run_fixture(command=[sys.executable, '-I', '-c', 'raise SystemExit(7)'])
        self.assertEqual(code, 1)
        self.assertEqual(result['server']['start']['status'], 'failed')
        self.assertTrue(all(m['status'] == 'not_run' for m in result['models']))

    def test_failed_final_server_health_cannot_be_overwritten_by_good_cleanup(self):
        code, result, _ = self.run_fixture('final_unhealthy')
        self.assertEqual(code, 1)
        self.assertTrue(all(m['status'] == 'passed' for m in result['models']))
        self.assertEqual(result['server']['shutdown']['status'], 'passed')
        self.assertEqual(result['status'], 'failed')

    def test_nonzero_container_exit_overrides_saved_passing_raw_result(self):
        prepared = self.root / 'prepared'
        prepared.mkdir()
        self.suite['runtime'].update(platform='linux/amd64')
        self.suite['cache_roots'] = {name: {'host_path': str(self.root)} for name in ('huggingface', 'loom-assets')}
        RUN.write_json(prepared / 'prepared-suite.json', self.suite)
        args = copy.copy(self.limits)
        args.prepared, args.output, args.device_uri, args.workflow = prepared, self.root / 'raw', 'amdgpu://0', None
        def execute(command, **kwargs):
            if command[:2] == ['docker', 'run']:
                time.sleep(0.03)
                saved = RUN.read_json(args.output / 'suite-result.json')
                saved['status'] = 'passed'
                saved['elapsed_seconds'] = 0.001
                RUN.write_json(args.output / 'suite-result.json', saved)
                return argparse.Namespace(returncode=17)
            return argparse.Namespace(returncode=0)
        with mock.patch.object(RUN, 'copy_evidence'), mock.patch.object(RUN, 'verify_prepared', return_value=(None, None)), \
                mock.patch.object(RUN, 'gpu_options', return_value=([], {})), \
                mock.patch.object(RUN, 'cleanup_container'), mock.patch.object(RUN.subprocess, 'run', side_effect=execute):
            self.assertEqual(RUN.run_host(args), 1)
        saved = RUN.read_json(args.output / 'suite-result.json')
        self.assertEqual(saved['status'], 'failed')
        self.assertEqual(saved['container_exit_code'], 17)
        self.assertEqual(saved['lifecycle_elapsed_seconds'], 0.001)
        self.assertGreaterEqual(saved['elapsed_seconds'], 0.03)
        observed = (datetime.fromisoformat(saved['ended_at']) - datetime.fromisoformat(saved['started_at'])).total_seconds()
        self.assertAlmostEqual(saved['elapsed_seconds'], observed, delta=0.02)

    def test_served_asset_links_and_native_copy_fallback_are_verified(self):
        model = self.root / 'model'
        model.mkdir()
        cache = self.root / 'cache.gguf'
        cache.write_bytes(b'correct locked bytes')
        identity = {'size': cache.stat().st_size, 'sha256': RUN.digest(cache)}
        installed = model / 'asset.gguf'
        installed.symlink_to(cache)
        RUN.verify_asset_reference(model, 'asset.gguf', cache, identity)
        installed.unlink()
        installed.write_bytes(cache.read_bytes())
        RUN.verify_asset_reference(model, 'asset.gguf', cache, identity)
        installed.write_bytes(b'wrong')
        with self.assertRaisesRegex(ValueError, 'bytes differ'):
            RUN.verify_asset_reference(model, 'asset.gguf', cache, identity)
        installed.unlink()
        outside = self.root / 'outside.gguf'
        outside.write_bytes(cache.read_bytes())
        installed.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'escapes'):
            RUN.verify_asset_reference(model, 'asset.gguf', cache, identity)
        outside.unlink()
        with self.assertRaises(FileNotFoundError):
            RUN.verify_asset_reference(model, 'asset.gguf', cache, identity)

    def test_bundle_path_escape_rejected(self):
        with self.assertRaises(ValueError):
            RUN.contained(self.root, '../outside')
        (self.root / 'escape').symlink_to('/etc/passwd')
        with self.assertRaises(ValueError):
            RUN.contained(self.root, 'escape')

    def test_all_failed_preparation_retains_results_without_docker(self):
        prepared = self.root / 'prepared'
        prepared.mkdir()
        for entry in self.suite['entries']:
            entry['preparation'] = {'status': 'failed', 'reason': 'cache miss'}
        RUN.write_json(prepared / 'prepared-suite.json', self.suite)
        args = argparse.Namespace(prepared=prepared, output=self.root / 'raw', device_uri='amdgpu://0', workflow=None)
        with mock.patch.object(RUN.subprocess, 'run', side_effect=AssertionError('must not invoke Docker')):
            self.assertEqual(RUN.run_host(args), 1)
        result = RUN.read_json(args.output / 'suite-result.json')
        self.assertEqual(len(result['expected_entries']), 2)
        self.assertEqual(result['server']['start']['status'], 'not_run')
        self.assertEqual(result['server']['cleanup']['status'], 'passed')

    def test_versioned_workflow_metadata_survives_in_raw_results(self):
        prepared = self.root / 'prepared'
        prepared.mkdir()
        for entry in self.suite['entries']:
            entry['preparation'] = {'status': 'failed', 'reason': 'cache miss'}
        RUN.write_json(prepared / 'prepared-suite.json', self.suite)
        workflow = {'schema_version': 1, 'repository': 'rsuderman/hrx-systems-test',
            'run_id': '123', 'run_attempt': '1', 'ref': 'refs/heads/main',
            'sha': 'abc123', 'runner': 'fixture-runner',
            'run_url': 'https://github.com/rsuderman/hrx-systems-test/actions/runs/123'}
        RUN.write_json(self.root / 'workflow.json', workflow)
        args = argparse.Namespace(prepared=prepared, output=self.root / 'raw', device_uri='amdgpu://0',
                                  workflow=self.root / 'workflow.json')
        with mock.patch.object(RUN.subprocess, 'run', side_effect=AssertionError('must not invoke Docker')):
            self.assertEqual(RUN.run_host(args), 1)
        saved = RUN.read_json(args.output / 'suite-result.json')
        self.assertEqual(saved['workflow'], workflow)
        self.assertIn('workflow.json', saved['evidence'])
        self.assertEqual(RUN.read_json(args.output / 'workflow.json'), workflow)

    def test_prepared_manifest_hash_is_checked_before_trusting_file_digests(self):
        path = self.root / 'prepared-inputs.json'
        RUN.write_json(path, {'programs': [{'sha256': 'original'}]})
        entry = {'inputs_manifest': path.name, 'inputs_manifest_sha256': RUN.digest(path)}
        self.assertEqual(RUN.input_manifest(self.root, entry)['programs'][0]['sha256'], 'original')
        RUN.write_json(path, {'programs': [{'sha256': 'changed'}]})
        with self.assertRaisesRegex(ValueError, 'manifest digest changed'):
            RUN.input_manifest(self.root, entry)

    def test_cleanup_refuses_container_without_matching_ownership(self):
        name, _ = RUN.owned_container(self.root)
        responses = [argparse.Namespace(stdout=name + '\n'), argparse.Namespace(stdout='container-id wrong-owner')]
        with mock.patch.object(RUN.subprocess, 'run', side_effect=responses) as execute:
            with self.assertRaisesRegex(RuntimeError, 'ownership'):
                RUN.cleanup_container(self.root)
        self.assertFalse(any(call.args[0][1] == 'rm' for call in execute.call_args_list))

    def test_cleanup_uses_verified_exact_id_and_confirms_absence(self):
        name, token = RUN.owned_container(self.root)
        responses = [argparse.Namespace(stdout=name + '\n'),
                     argparse.Namespace(stdout='verified-container-id ' + token),
                     argparse.Namespace(stdout=''), argparse.Namespace(stdout='')]
        with mock.patch.object(RUN.subprocess, 'run', side_effect=responses) as execute:
            RUN.cleanup_container(self.root)
        self.assertEqual(execute.call_args_list[2].args[0], ['docker', 'rm', '-f', 'verified-container-id'])

    def test_runtime_scratch_is_retained_when_container_cleanup_is_unconfirmed(self):
        with mock.patch.object(RUN, 'cleanup_container', side_effect=RuntimeError('daemon unreachable')):
            with self.assertRaisesRegex(RuntimeError, 'daemon unreachable'):
                with RUN.runtime_scratch(self.root) as scratch:
                    (scratch / 'owned-runtime-state').write_text('retained')
        self.assertTrue((scratch / 'owned-runtime-state').exists())


if __name__ == '__main__':
    unittest.main()
