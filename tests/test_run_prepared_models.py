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
import json,sys,time
mode=sys.argv[2]
rows={}
counter=0
pending_reads=0
pool_dirty=False
pool_key='owned-fixture-pool'
def pool_record():
 retained={'pending_bytes':1 if pending_reads else 0,'releasing_bytes':128 if pending_reads else 0,
  'reserved_bytes':4096,'committed_bytes':8192,'released_bytes':99999}
 if mode=='quarantined':retained.update(pending_bytes=0,releasing_bytes=0)
 return {'key':pool_key,'devices':[0],'memory':{'backing':'elastic','retained':retained,
  'storage':{'reserved_bytes':0,'committed_bytes':256},
  'workspace':{'quarantined_bytes':32 if pending_reads else 0,'committed_bytes':1024,'reserve_count':25}}}
def pool_records():
 pools=[pool_record()]
 if mode=='multiple_pools':
  other=pool_record();other['key']='second-owned-pool';pools.append(other)
 return pools
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def reply(self,status,data):
  encoded=json.dumps(data).encode();self.send_response(status)
  self.send_header('Content-Length',str(len(encoded)));self.end_headers();self.wfile.write(encoded)
 def do_GET(self):
  global pending_reads,pool_dirty
  if self.path=='/readyz':
   if mode=='final_unhealthy' and counter==2:return self.reply(503,{'status':'failed'})
   return self.reply(200,{'status':'ready'})
  if self.path=='/v1/status' and pending_reads:
   if mode not in ('trim_pending','quarantined'):pending_reads-=1
   if not pending_reads:pool_dirty=False
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
  if mode=='foreign_registration' and counter and not rows:
   data.append({'id':'foreign','loom':{'incarnation':99,'registration':'ready','status':{'admission':'open'}}})
  if self.path=='/v1/status':
   if mode=='missing_resources':return self.reply(200,{'data':data})
   return self.reply(200,{'data':data,'server':{'resources':{'pools':pool_records()}}})
  if self.path=='/v1/models':return self.reply(200,{'data':[x for x in data if x['loom']['registration']=='ready']})
  self.reply(404,{})
 def do_POST(self):
  global counter,pending_reads,pool_dirty
  payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  if self.path.endswith('/register'):
   if pool_dirty:return self.reply(409,{'error':'pool still needs reclamation'})
   counter+=1;rows[payload['name']]={'incarnation':counter,'ticks':0}
   print('REGISTER '+payload['name'],flush=True)
   if mode=='lost' and payload['name']=='first':
    self.close_connection=True;return
   return self.reply(202,{})
  if self.path.endswith('/unregister'):
   if payload['model'] in rows:rows[payload['model']].update(removing=True,ticks=0)
   pool_dirty=True
   return self.reply(202,{})
  if self.path=='/v1/pools/trim':
   if rows:return self.reply(409,{'error':'registrations remain'})
   if payload.get('pool') not in {p['key'] for p in pool_records()} or payload.get('target_bytes')!=0:
    return self.reply(400,{'error':'wrong pool selector'})
   print('TRIM '+payload['pool'],flush=True)
   if mode=='trim_failure':return self.reply(500,{'error':'fixture trim failed'})
   if mode=='slow_trim':time.sleep(1)
   pending_reads=3
   reply_pool=pool_record();reply_pool['key']=payload['pool']
   if mode=='bad_trim_response':reply_pool['key']='unexpected-pool'
   return self.reply(200,{'target_bytes':0,'pool':reply_pool})
  if self.path=='/v1/chat/completions':
   if mode=='malformed':return self.reply(200,{})
   answer='wrong' if mode=='wrong' and payload['model']=='first' else '4'
   return self.reply(200,{'choices':[{'message':{'content':answer}}]})
  self.reply(404,{})
ThreadingHTTPServer(('127.0.0.1',int(sys.argv[1])),Handler).serve_forever()
'''


class PreparedHandoffTests(unittest.TestCase):
    """Exercise real SDK identity parsing with distinct model config snapshots."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sdk = RUN.sdk_helper()
        self.payloads = self.root / 'sdk-payloads'
        wheel = self.payloads / 'hrx/fixture-sdk.whl'
        wheel.parent.mkdir(parents=True)
        wheel.write_bytes(b'tiny verified SDK payload')
        self.suite = {'schema_version': 1, 'entries': [], 'cache_roots': {}}
        for name in ('huggingface', 'loom-assets'):
            cache = self.root / 'cache' / name
            cache.mkdir(parents=True)
            self.suite['cache_roots'][name] = {
                'host_path': str(cache), 'container_path': '/model-cache/' + name}
        sdk_config = {'name': 'hrx-loom', 'version': 'fixture', 'repository': 'fixture/sdk',
            'release': 'fixture', 'source_commit': 'b' * 40, 'asset_id': 1,
            'dependency_lock': 'dependencies.json', 'wheel': {
                'filename': wheel.name, 'url': 'https://example.invalid/fixture-sdk.whl',
                'sha256': RUN.digest(wheel), 'size_bytes': wheel.stat().st_size}}
        for name in ('first', 'second'):
            selection = {'model_id': 'fixture/' + name, 'repository': 'fixture/' + name,
                'publication_revision': 'c' * 40, 'preset': 'chat', 'variant': 'gguf', 'programs': 'source'}
            baseline = {'schema_version': 1, **selection,
                'container': {'image': 'example.invalid/python@sha256:' + 'a' * 64},
                'sdk': copy.deepcopy(sdk_config),
                'smoke_request': {'messages': [{'role': 'user', 'content': 'two plus two'}]},
                'smoke_expected_text': '4', 'published_execution': {'rows': 1}}
            config_dir = self.root / 'config' / name
            RUN.write_json(config_dir / 'baseline.json', baseline)
            RUN.write_json(config_dir / 'dependencies.json', {'schema_version': 1, 'wheels': []})
            _, _, _, entry_identity = self.sdk.load_baseline(config_dir / 'baseline.json')
            if name == 'first':
                common = self.root / 'config/runtime'
                RUN.write_json(common / 'baseline.json', baseline)
                RUN.write_json(common / 'dependencies.json', {'schema_version': 1, 'wheels': []})
                _, _, _, common_identity = self.sdk.load_baseline(common / 'baseline.json')
                self.suite['runtime'] = {'image': baseline['container']['image'],
                    'sdk_baseline': 'config/runtime/baseline.json', 'sdk_payloads': 'sdk-payloads',
                    'sdk_identity': common_identity}
            model_root = self.root / 'models' / name / 'model'
            model_root.mkdir(parents=True)
            RUN.write_json(model_root / 'model.json', {'fixture': name})
            (model_root / 'control.loom').write_text('fixture program for ' + name)
            asset = self.root / 'cache/huggingface' / (name + '.gguf')
            asset.write_bytes(('fixture weight bytes for ' + name).encode())
            manifest = {'sdk_identity': copy.deepcopy(self.suite['runtime']['sdk_identity']),
                'selection': selection, 'model_card_path': '/inputs/' + name + '/model/model.json',
                'installed_card_sha256': RUN.digest(model_root / 'model.json'),
                'smoke_request': baseline['smoke_request'], 'smoke_expected_text': '4',
                'effective_execution': baseline['published_execution'],
                'programs': [{'path': 'control.loom', 'sha256': RUN.digest(model_root / 'control.loom')}],
                'assets': [{'cache_root': 'huggingface', 'cache_path': asset.name,
                    'installation_path': 'model.gguf', 'size': asset.stat().st_size, 'sha256': RUN.digest(asset)}]}
            manifest_path = self.root / 'models' / name / 'prepared-inputs.json'
            RUN.write_json(manifest_path, manifest)
            self.suite['entries'].append({'id': name, 'preparation': {'status': 'ready'},
                'inputs_manifest': str(manifest_path.relative_to(self.root)),
                'inputs_manifest_sha256': RUN.digest(manifest_path),
                'baseline': str((config_dir / 'baseline.json').relative_to(self.root)),
                'baseline_sha256': entry_identity['baseline_sha256'],
                'dependency_lock_sha256': entry_identity['dependency_lock_sha256'],
                'selection': selection, 'smoke_request': baseline['smoke_request'], 'smoke_expected_text': '4'})

    def verify(self):
        return RUN.verify_prepared(self.root, self.suite)

    def rewrite_manifest(self, entry, change):
        path = self.root / entry['inputs_manifest']
        manifest = RUN.read_json(path)
        change(manifest)
        RUN.write_json(path, manifest)
        entry['inputs_manifest_sha256'] = RUN.digest(path)

    def test_distinct_model_configs_share_one_exact_runtime_receipt(self):
        first, second = self.suite['entries']
        common_identity = self.suite['runtime']['sdk_identity']
        self.assertNotEqual(first['baseline_sha256'], second['baseline_sha256'])
        self.assertEqual(first['baseline_sha256'], common_identity['baseline_sha256'])
        self.assertNotEqual(second['baseline_sha256'], common_identity['baseline_sha256'])
        self.assertEqual(self.verify(), (self.root / 'config/runtime/baseline.json', self.payloads))

    def test_per_model_baseline_hash_cannot_replace_common_sdk_receipt(self):
        second = self.suite['entries'][1]
        self.rewrite_manifest(second, lambda manifest: manifest['sdk_identity'].update(
            baseline_sha256=second['baseline_sha256']))
        with self.assertRaisesRegex(ValueError, 'prepared model identity changed'):
            self.verify()

    def test_distinct_entry_runtime_is_rejected_even_with_valid_model_config_hash(self):
        second = self.suite['entries'][1]
        path = self.root / second['baseline']
        baseline = RUN.read_json(path)
        baseline['sdk']['source_commit'] = 'd' * 40
        RUN.write_json(path, baseline)
        second['baseline_sha256'] = RUN.digest(path)
        with self.assertRaisesRegex(ValueError, 'prepared model identity changed'):
            self.verify()

    def test_changed_frozen_model_config_is_rejected(self):
        path = self.root / self.suite['entries'][1]['baseline']
        baseline = RUN.read_json(path)
        baseline['published_execution']['rows'] = 2
        RUN.write_json(path, baseline)
        with self.assertRaisesRegex(ValueError, 'frozen model baseline/dependency identity changed'):
            self.verify()

    def test_changed_model_card_and_program_bytes_are_rejected(self):
        card = self.root / 'models/second/model/model.json'
        original = card.read_bytes()
        card.write_bytes(b'changed card')
        with self.assertRaisesRegex(ValueError, 'prepared installed card changed'):
            self.verify()
        card.write_bytes(original)
        (self.root / 'models/second/model/control.loom').write_text('changed program')
        with self.assertRaisesRegex(ValueError, 'prepared program bytes changed'):
            self.verify()

    def test_changed_cache_bytes_and_manifest_digests_are_rejected(self):
        asset = self.root / 'cache/huggingface/second.gguf'
        original = asset.read_bytes()
        asset.write_bytes(b'changed weights')
        with self.assertRaisesRegex(ValueError, 'cached asset changed'):
            self.verify()
        asset.write_bytes(original)
        manifest = self.root / self.suite['entries'][1]['inputs_manifest']
        manifest.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'prepared input manifest digest changed'):
            self.verify()

    def test_changed_selection_and_request_remain_rejected(self):
        second = self.suite['entries'][1]
        selection = copy.deepcopy(second['selection'])
        second['selection'] = {**selection, 'model_id': 'fixture/changed'}
        with self.assertRaisesRegex(ValueError, 'prepared model identity changed'):
            self.verify()
        second['selection'] = selection
        second['smoke_expected_text'] = 'wrong'
        with self.assertRaisesRegex(ValueError, 'prepared request/assertion changed'):
            self.verify()


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
                reclamation = RUN.read_json(output / cycle['unload']['evidence'][0])
                self.assertEqual(reclamation['trims'][0]['pool']['key'], 'owned-fixture-pool')
                self.assertGreater(reclamation['trims'][0]['pool']['memory']['retained']['pending_bytes'], 0)
                memory = reclamation['after']['pools'][0]['memory']
                self.assertEqual(memory['retained']['pending_bytes'], 0)
                self.assertEqual(memory['retained']['releasing_bytes'], 0)
                self.assertEqual(memory['workspace']['quarantined_bytes'], 0)
                self.assertGreater(memory['retained']['reserved_bytes'], 0)
                self.assertGreater(memory['retained']['committed_bytes'], 0)
                self.assertGreater(memory['workspace']['committed_bytes'], 0)
        self.assertEqual(result['server']['shutdown']['status'], 'passed')

    def test_every_published_owned_pool_is_trimmed_before_next_registration(self):
        code, result, output = self.run_fixture('multiple_pools')
        self.assertEqual(code, 0)
        for model in result['models']:
            evidence = RUN.read_json(output / model['cycles'][0]['unload']['evidence'][0])
            self.assertEqual({item['pool']['key'] for item in evidence['trims']},
                             {'owned-fixture-pool', 'second-owned-pool'})
        self.assertEqual((output / 'server.log').read_text().splitlines(), [
            'REGISTER first', 'TRIM owned-fixture-pool', 'TRIM second-owned-pool',
            'REGISTER second', 'TRIM owned-fixture-pool', 'TRIM second-owned-pool'])

    def test_pool_trim_failure_stops_before_next_registration_and_keeps_evidence(self):
        code, result, output = self.run_fixture('trim_failure')
        self.assertEqual(code, 1)
        unload = result['models'][0]['cycles'][0]['unload']
        self.assertEqual(unload['status'], 'failed')
        evidence = RUN.read_json(output / unload['evidence'][0])
        self.assertIn('HTTP 500', evidence['error'])
        self.assertEqual(result['models'][1]['status'], 'not_run')
        self.assertNotIn('REGISTER second', (output / 'server.log').read_text())

    def test_missing_pool_accounting_is_failed_cleanup(self):
        code, result, output = self.run_fixture('missing_resources')
        self.assertEqual(code, 1)
        unload = result['models'][0]['cycles'][0]['unload']
        self.assertEqual(unload['status'], 'failed')
        self.assertIn('error', RUN.read_json(output / unload['evidence'][0]))
        self.assertEqual(result['models'][1]['status'], 'not_run')

    def test_pending_pool_release_uses_remaining_unload_budget_and_stops_loop(self):
        code, result, output = self.run_fixture('trim_pending')
        self.assertEqual(code, 1)
        unload = result['models'][0]['cycles'][0]['unload']
        self.assertEqual(unload['status'], 'failed')
        self.assertLess(unload['elapsed_seconds'], self.limits.unload_timeout + self.limits.abort_timeout + 0.25)
        evidence = RUN.read_json(output / unload['evidence'][0])
        self.assertGreater(evidence['after']['pools'][0]['memory']['retained']['releasing_bytes'], 0)
        self.assertEqual(result['models'][1]['status'], 'not_run')

    def test_workspace_quarantine_blocks_next_model_even_with_retained_quiescent(self):
        code, result, output = self.run_fixture('quarantined')
        self.assertEqual(code, 1)
        unload = result['models'][0]['cycles'][0]['unload']
        memory = RUN.read_json(output / unload['evidence'][0])['after']['pools'][0]['memory']
        self.assertEqual(memory['retained']['pending_bytes'], 0)
        self.assertEqual(memory['retained']['releasing_bytes'], 0)
        self.assertGreater(memory['workspace']['quarantined_bytes'], 0)
        self.assertEqual(result['models'][1]['status'], 'not_run')

    def test_unsupported_trim_response_cannot_be_cleanup_success(self):
        code, result, output = self.run_fixture('bad_trim_response')
        self.assertEqual(code, 1)
        unload = result['models'][0]['cycles'][0]['unload']
        self.assertIn('unsupported pool reclamation response', unload['reason'])
        self.assertEqual(result['models'][1]['status'], 'not_run')

    def test_slow_trim_http_request_cannot_exceed_cleanup_budget(self):
        self.limits.unload_timeout = self.limits.abort_timeout = 0.1
        code, result, _ = self.run_fixture('slow_trim')
        self.assertEqual(code, 1)
        unload = result['models'][0]['cycles'][0]['unload']
        self.assertEqual(unload['status'], 'failed')
        self.assertLess(unload['elapsed_seconds'], 0.45)
        self.assertEqual(result['models'][1]['status'], 'not_run')

    def test_pool_reclamation_refuses_nonempty_registration_inventory(self):
        code, result, output = self.run_fixture('foreign_registration')
        self.assertEqual(code, 1)
        unload = result['models'][0]['cycles'][0]['unload']
        self.assertIn('requires zero registrations', unload['reason'])
        self.assertNotIn('TRIM', (output / 'server.log').read_text())
        self.assertEqual(result['models'][1]['status'], 'not_run')

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
