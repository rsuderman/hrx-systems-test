import copy
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('paired_test',ROOT/'scripts/paired-ci.py')
M=importlib.util.module_from_spec(spec);spec.loader.exec_module(M)

class PairedTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.plan=self.root/'plan';self.plan.mkdir()
        M.write(self.plan/'selection.json',{'entries':[{'entry':{'id':'example'},'selection':{'model_id':'example'}}]})
        for lane in ('latest-release','latest-ci'):M.write(self.plan/(lane+'.json'),{'schema_version':1,'selector':{},'source':{'tag':'0.2.0'}})
        self.doc={'schema_version':1,'selected_at':'2026-11-12T00:00:00Z','inputs':M.normalize('push',{}),
          'selection_snapshot':'selection.json','selection_snapshot_sha256':M.sha(self.plan/'selection.json'),
          'expected_entries':[{'id':'example','selection':{'model_id':'example'}}],
          'lanes':[{'id':lane,'runtime_source':source,'runtime_selection':lane+'.json','runtime_selection_sha256':M.sha(self.plan/(lane+'.json'))} for lane,source in [('latest-release','release-latest'),('latest-ci','actions-main')]]}
        self.path=self.plan/'ci-plan.json';M.write(self.path,self.doc)
        self.stages={key:'success' for key in ('prepare','run','cleanup','report','collect','package','upload','summary')}
    def init(self,lane='latest-release',safe=True):
        out=self.root/lane;M.initialize(self.path,lane,out,safe);return out
    def raw(self,out,forced=False):
        state=M.read(out/'worker-state.json');state['run_invoked']=True;M.write(out/'worker-state.json',state)
        raw={'status':'passed','expected_entries':self.doc['expected_entries'],'models':[{'id':'example','status':'passed','cycles':[{'unload':{'status':'passed'}}]}],
             'server':{key:{'status':'passed'} for key in ('start','shutdown','cleanup')}}
        raw['server']['shutdown']['forced']=forced;M.write(out/'results/suite-result.json',raw);return raw
    def test_default_pair_and_replay_validation(self):
        self.assertEqual(M.normalize('push',{'runtime_source':'release'})['runtime_source'],'both')
        with self.assertRaises(ValueError):M.normalize('workflow_dispatch',{'runtime_source':'actions-replay'})
        with self.assertRaises(ValueError):M.normalize('workflow_dispatch',{'cycles':False})
    def test_metadata_failure_is_frozen_without_losing_second_lane(self):
        from types import SimpleNamespace
        real_helper=M.helper
        frozen={'entries':[{'entry':{'id':'example'},'selection':{'model_id':'example'}}]}
        def helpers(name):
            return SimpleNamespace(freeze=lambda *args: frozen) if name=='selection-snapshot' else real_helper(name)
        calls=[]
        def freezer(selector):
            calls.append(selector)
            if len(calls)==1: raise ValueError('release metadata unavailable')
            return {'schema_version':1,'selector':selector,'source':{'run_id':123}}
        with patch.object(M,'helper',helpers):
            doc=M.plan(self.root,self.root/'resolved-plan',M.normalize('push',{}),freezer=freezer)
        self.assertEqual(len(calls),2)
        self.assertEqual([x['resolution_status'] for x in doc['lanes']],['failed','selected'])
        envelope=M.read(self.root/'resolved-plan/runtime-selection-latest-release.json')
        self.assertEqual(envelope['status'],'failed');self.assertNotIn('source',envelope)
        self.assertEqual(M.load_plan(self.root/'resolved-plan/ci-plan.json')['expected_entries'],self.doc['expected_entries'])
    def test_snapshot_and_denominator_tamper_rejected(self):
        changed=copy.deepcopy(self.doc);changed['expected_entries']=[];M.write(self.path,changed)
        with self.assertRaisesRegex(ValueError,'denominator'):M.load_plan(self.path)
        M.write(self.path,self.doc);(self.plan/'latest-ci.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'digest'):M.load_plan(self.path)
    def test_cleanup_ignores_model_report_upload_failures(self):
        out=self.init();raw=self.raw(out);raw['status']='failed';raw['models'][0]['status']='failed';M.write(out/'results/suite-result.json',raw)
        for key in ('prepare','run','report','collect','package','upload','summary'):
            stages={**self.stages,key:'failure'};self.assertTrue(M.cleanup_safe(out,stages)[0],key)
        self.assertFalse(M.finish(out,{**self.stages,'report':'failure'})['status']=='passed')
    def test_no_gpu_preparation_failure_safe_but_unknown_forced_execution_unsafe(self):
        out=self.init();self.assertTrue(M.cleanup_safe(out,{**self.stages,'prepare':'failure'})[0])
        self.raw(out,True);self.assertFalse(M.cleanup_safe(out,self.stages)[0])
        (out/'results/suite-result.json').unlink();self.assertFalse(M.cleanup_safe(out,self.stages)[0])
        self.assertFalse(M.cleanup_safe(out,{**self.stages,'cleanup':'failure'})[0])
    def test_blocked_lane_retains_full_denominator_without_execution(self):
        out=self.init(safe=False);raw=M.read(out/'results/suite-result.json')
        self.assertEqual(raw['models'][0]['status'],'not_run');self.assertFalse(M.read(out/'worker-state.json')['run_invoked'])
        row=M.finish(out,self.stages,'https://github.com/a/b/actions/artifacts/123')
        self.assertEqual(row['status'],'blocked');self.assertTrue(row['cleanup_safe'])
    def test_pair_requires_both_results_and_exact_plan_attribution(self):
        out=self.init();self.raw(out);M.finish(out,self.stages,'https://github.com/a/b/actions/artifacts/123')
        result=M.aggregate(self.path,self.root,self.root/'combined');self.assertEqual(result['status'],'failed')
        self.assertEqual(result['lanes'][1]['status'],'not_run')
        out2=self.init('latest-ci');self.raw(out2);M.finish(out2,self.stages,'https://github.com/a/b/actions/artifacts/456')
        self.assertEqual(M.aggregate(self.path,self.root,self.root/'combined')['status'],'passed')
        row=M.read(out2/'lane-result.json');row['workflow']['plan_sha256']='0'*64;M.write(out2/'lane-result.json',row)
        with self.assertRaisesRegex(ValueError,'another frozen plan'):M.aggregate(self.path,self.root,self.root/'combined')
    def test_resolution_errors_are_sanitized_and_bounded(self):
        row=M.resolution_failure({'kind':'release_latest'},ValueError('token=secret https://example.com/x?token=secret '+('x'*5000)))
        self.assertNotIn('secret',json.dumps(row));self.assertLessEqual(len(row['error']['message'].encode()),4096)
    def test_workflow_serializes_gpu_lanes_with_outer_lock_only(self):
        outer=(ROOT/'.github/workflows/single-model-e2e.yml').read_text();worker=(ROOT/'.github/workflows/_model-verification-worker.yml').read_text()
        self.assertIn('needs: [plan, first]',outer);self.assertIn('needs.first.outputs.cleanup_safe',outer)
        self.assertIn('cancel-in-progress: false',outer);self.assertNotIn('concurrency:',worker)
        self.assertIn("env.BLOCKED != 'true'",worker);self.assertIn('LANE_UPLOAD',worker)

class TemporaryExclusionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.repo = self.root / 'repo'
        (self.repo / 'config').mkdir(parents=True)
        for name in ('runtime.json', 'sdk-requirements.lock.json', 'ci-model-exclusions.json'):
            (self.repo / 'config' / name).write_bytes((ROOT / 'config' / name).read_bytes())
        self.policy = M.read(self.repo / 'config/ci-model-exclusions.json')
        self.assertEqual(len(self.policy['entries']), 9)
        self.selections = [entry['selection'] for entry in self.policy['entries']]
        self.selections += [{'repository': 'loom-fun/example', 'publication_revision': 'a'*40,
                             'model_id': 'enabled-' + str(n), 'preset': 'chat', 'variant': 'gguf',
                             'programs': 'source'} for n in range(51)]
        for n, selection in enumerate(self.selections):
            directory = self.root / 'cache/models' / str(n); directory.mkdir(parents=True)
            M.write(directory / 'seed-manifest.json', {
                'schema_version': 1, 'selection': selection, 'card_sha256': 'b'*64, 'lock_sha256': 'c'*64,
                'files': [{'installation_path': 'parameter/model.gguf', 'cache_root': 'loom-assets',
                           'cache_path': 'blob/' + 'd'*64, 'verified': True, 'roles': ['parameter'],
                           'origin': '@checkpoint/model.gguf', 'resolved': None, 'size': 4, 'sha256': 'd'*64}]})
        self.release = 'v0.1.0'
        self.stages = {key: 'success' for key in ('prepare','run','cleanup','report','collect','package','upload','summary')}

    def make_plan(self, name='plan', runtime_source='both', fail_release=False):
        def freezer(selector):
            if selector['kind'] == 'release_latest':
                if fail_release: raise ValueError('release metadata unavailable')
                return {'schema_version': 1, 'source': {'kind': 'release', 'release': self.release}}
            return {'schema_version': 1, 'source': {'kind': 'github_actions', 'tested_sha': 'e'*40}}
        values = M.normalize('workflow_dispatch', {'runtime_source': runtime_source})
        path = self.root / name / 'ci-plan.json'
        M.plan(self.root, path.parent, values, repo=self.repo, freezer=freezer)
        return path, M.load_plan(path)

    def result(self, plan, lane, *, safe=True, missing=False, stages=None):
        out = self.root / ('worker-' + lane)
        M.initialize(plan, lane, out, safe)
        if safe and not missing:
            enabled = M.read(out / 'lane-selection.json')['expected_entries']
            suite = {'attempt_id': 'metadata-test', 'entries': [dict(e, preparation={'status': 'ready', 'reason': None},
                     smoke_expected_text='4') for e in enabled]}
            raw = M.helper('run-prepared-models').initial_results(suite, {})
            raw['status'] = 'passed'
            for model in raw['models']: model['status'] = 'passed'
            M.write(out / 'results/suite-result.json', raw)
        return out, M.finish(out, stages or self.stages, 'https://github.com/a/b/actions/artifacts/123')

    def test_exact_nine_release_exclusions_and_preparation_snapshots(self):
        path, plan = self.make_plan()
        self.assertEqual(len(plan['expected_entries']), 60)
        release, ci = plan['lanes']
        self.assertEqual([len(row['expected_entries']) for row in plan['lanes']], [51, 60])
        self.assertEqual([len(row['excluded_entries']) for row in plan['lanes']], [9, 0])
        self.assertEqual({row['selection']['repository'] for row in release['excluded_entries']},
                         {row['selection']['repository'] for row in self.policy['entries']})
        snapshot_module = M.helper('selection-snapshot')
        for lane in (release, ci):
            # Exercise the actual CLI argument boundary consumed by the worker.
            import subprocess, sys
            command = subprocess.check_output([sys.executable, '-I', str(ROOT / 'scripts/paired-ci.py'),
                'prepare-args', '--plan', str(path), '--lane', lane['id']]).split(b'\0')
            snapshot = Path(command[1].decode())
            self.assertEqual(snapshot.name, lane['selection_snapshot'])
            _, inventory, candidates = snapshot_module.load(snapshot, self.root / 'cache', self.repo)
            self.assertEqual(len(candidates), len(lane['expected_entries']))
            self.assertEqual(len(inventory['entries']), len(candidates))

    def test_new_release_pinned_release_and_replay_remain_enabled(self):
        self.release = 'v0.2.0'
        _, plan = self.make_plan('new-release')
        self.assertEqual([len(row['expected_entries']) for row in plan['lanes']], [60, 60])
        _, pinned = self.make_plan('pinned', 'release')
        self.assertEqual(pinned['lanes'][0]['excluded_entries'], [])
        common = M.read(self.root / 'new-release/selection-snapshot.json')
        for lane in ('latest-ci', 'ci-replay', 'pinned-release'):
            selected, excluded = M.lane_snapshot(common, lane, {'source': {'kind': 'release', 'release': 'v0.1.0'}}, self.policy)
            self.assertEqual(len(selected['entries']), 60); self.assertEqual(excluded, [])
        changed = copy.deepcopy(common)
        changed['entries'][0]['selection']['publication_revision'] = 'f'*40
        _, excluded = M.lane_snapshot(changed, 'latest-release', {'source': {'kind': 'release', 'release': 'v0.1.0'}}, self.policy)
        self.assertEqual(len(excluded), 8)

    def test_resolution_failure_does_not_activate_exclusions(self):
        _, plan = self.make_plan(fail_release=True)
        self.assertEqual(plan['lanes'][0]['resolution_status'], 'failed')
        self.assertEqual(len(plan['lanes'][0]['expected_entries']), 60)
        self.assertEqual(plan['lanes'][0]['excluded_entries'], [])

    def test_frozen_policy_subset_and_metadata_tampering_are_rejected(self):
        path, plan = self.make_plan()
        lane = plan['lanes'][0]; snapshot = path.parent / lane['selection_snapshot']
        original = snapshot.read_bytes(); changed = M.read(snapshot); changed['entries'].pop()
        M.write(snapshot, changed)
        with self.assertRaisesRegex(ValueError, 'digest'): M.load_plan(path)
        lane['selection_snapshot_sha256'] = M.sha(snapshot); M.write(path, plan)
        with self.assertRaisesRegex(ValueError, 'frozen policy'): M.load_plan(path)
        snapshot.write_bytes(original); lane['selection_snapshot_sha256'] = M.sha(snapshot)
        lane['excluded_entries'][0]['reason'] = 'tampered'; M.write(path, plan)
        with self.assertRaisesRegex(ValueError, 'frozen policy'): M.load_plan(path)
        path, plan = self.make_plan('policy-tamper')
        M.write(path.parent / plan['exclusion_policy'], {})
        with self.assertRaisesRegex(ValueError, 'digest'): M.load_plan(path)
        path, plan = self.make_plan('repo-tamper')
        (self.repo / 'config/ci-model-exclusions.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'configuration changed'):
            M.helper('selection-snapshot').load(path.parent / plan['lanes'][0]['selection_snapshot'], self.root / 'cache', self.repo)

    def test_blocked_and_missing_execution_keep_only_enabled_denominator(self):
        path, _ = self.make_plan()
        out, row = self.result(path, 'latest-release', safe=False)
        self.assertEqual(row['status'], 'blocked'); self.assertEqual(row['counts'], {'passed': 0, 'failed': 0, 'not_run': 51, 'expected': 51})
        self.assertEqual(len(row['excluded_entries']), 9)
        raw = M.read(out / 'results/suite-result.json')
        self.assertEqual(len(raw['models']), 51)
        out, row = self.result(path, 'latest-ci', missing=True)
        self.assertEqual(row['status'], 'failed'); self.assertEqual(row['counts']['expected'], 60)
        self.assertEqual(row['counts']['not_run'], 60); self.assertEqual(row['excluded_entries'], [])

    def test_results_and_summary_separate_enabled_excluded_and_passed(self):
        path, plan = self.make_plan()
        _, release = self.result(path, 'latest-release')
        _, ci = self.result(path, 'latest-ci')
        self.assertEqual(release['counts']['passed'], 51); self.assertEqual(ci['counts']['passed'], 60)
        result = M.aggregate(path, self.root, self.root / 'comparison')
        self.assertEqual(result['status'], 'passed')
        summary = (self.root / 'comparison/summary.md').read_text()
        self.assertIn('| Enabled | Excluded |', summary)
        self.assertIn('| passed | 51 | 9 | 51 |', summary)
        self.assertIn('| passed | 60 | 0 | 60 |', summary)
        self.assertIn('not counted as passes', summary)
        for entry in self.policy['entries']: self.assertIn(entry['reason'], summary)
        release['excluded_entries'] = []
        M.write(self.root / 'worker-latest-release/lane-result.json', release)
        with self.assertRaisesRegex(ValueError, 'exclusions'): M.aggregate(path, self.root, self.root / 'comparison')

    def test_exclusions_do_not_hide_stage_or_execution_failures(self):
        path, _ = self.make_plan()
        out, row = self.result(path, 'latest-release', stages={**self.stages, 'prepare': 'failure'})
        self.assertEqual(row['status'], 'failed'); self.assertEqual(row['counts']['expected'], 51)
        row = M.finish(out, {**self.stages, 'cleanup': 'failure'}, 'https://github.com/a/b/actions/artifacts/123')
        self.assertEqual(row['status'], 'failed'); self.assertFalse(row['cleanup_safe'])
        raw = M.read(out / 'results/suite-result.json'); raw['expected_entries'].pop()
        M.write(out / 'results/suite-result.json', raw)
        row = M.finish(out, self.stages, 'https://github.com/a/b/actions/artifacts/123')
        self.assertEqual(row['status'], 'failed'); self.assertIn('denominator', row['reason'])

    def test_policy_rejects_broad_duplicate_and_unexplained_rules(self):
        for field, value in [('selection', {'model_id': 'qwen*'}), ('reason', '')]:
            policy = copy.deepcopy(self.policy); policy['entries'][0][field] = value
            with self.assertRaises(ValueError): M.exclusion_policy(policy)
        policy = copy.deepcopy(self.policy); policy['entries'].append(policy['entries'][0])
        with self.assertRaisesRegex(ValueError, 'duplicate'): M.exclusion_policy(policy)

    def initialize_and_publish(self, lane, *, safe):
        path, plan = self.make_plan('plan-' + lane)
        out = self.root / ('actual-worker-' + lane)
        identity = {'GITHUB_REPOSITORY': 'rsuderman/hrx-systems-test', 'GITHUB_RUN_ID': '43379530677',
                    'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'a'*40, 'GITHUB_REF': 'refs/heads/main',
                    'RUNNER_NAME': 'fixture-runner', 'GITHUB_EVENT_NAME': 'push'}
        with patch.dict(os.environ, identity): M.initialize(path, lane, out, safe)
        if safe:
            # Exercise real host validation without SDK, Docker or model execution.
            enabled = M.read(out / 'lane-selection.json')['expected_entries']
            prepared = out / 'prepared'
            M.write(prepared / 'prepared-suite.json', {'attempt_id': 'metadata-regression', 'source': {},
                    'entries': [dict(e, preparation={'status': 'failed', 'reason': 'intentional fixture cache miss'})
                                for e in enabled]})
            runner = M.helper('run-prepared-models')
            args = argparse.Namespace(prepared=prepared, output=out / 'results', device_uri='amdgpu://0',
                                      workflow=out / 'workflow.json')
            with patch.object(runner.subprocess, 'run', side_effect=AssertionError('must not invoke Docker')):
                self.assertEqual(runner.run_host(args), 1)
        raw = M.read(out / 'results/suite-result.json')
        self.assertEqual(len(raw['expected_entries']), 51 if lane == 'latest-release' else 60)
        M.helper('generate-model-report').generate(out / 'results', out / 'report')
        source = (ROOT / '.github/workflows/_model-verification-worker.yml').read_text()
        section = source.split('      - name: Publish readable summary and report download\n', 1)[1]
        lines = []
        for line in section.split('        run: |\n', 1)[1].splitlines():
            if line and not line.startswith('          '): break
            lines.append(line[10:])
        summary = out / 'github-summary.md'
        env = {**os.environ, 'E2E_OUTPUT': str(out), 'GITHUB_STEP_SUMMARY': str(summary),
               'ARTIFACT_URL': 'https://github.com/rsuderman/hrx-systems-test/actions/runs/43379530677/artifacts/123',
               'UPLOAD_OUTCOME': 'success', 'COLLECT_OUTCOME': 'success', 'PACKAGE_OUTCOME': 'success',
               'HAS_FAILURES': 'false', 'PREPARE_OUTCOME': 'failure' if safe else 'skipped',
               'RUN_OUTCOME': 'failure' if safe else 'skipped', 'CLEANUP_OUTCOME': 'success', 'REPORT_OUTCOME': 'success'}
        published = subprocess.run(['bash', '-c', '\n'.join(lines) + '\n'], env=env,
                                   capture_output=True, text=True, timeout=10)
        self.assertEqual(published.returncode, 0, published.stderr)
        self.assertEqual(raw['workflow'], M.read(out / 'workflow.json'))
        self.assertNotIn('model_selection', raw['workflow'])
        self.assertIn(raw['workflow']['run_url'], summary.read_text())
        self.assertNotIn('](index.html)', summary.read_text())
        self.assertEqual(len(M.read(out / 'lane-selection.json')['excluded_entries']), 9 if lane == 'latest-release' else 0)

    def test_initialized_lane_workflows_reach_runner_and_github_publisher(self):
        for lane in ('latest-release', 'latest-ci'):
            with self.subTest(lane=lane): self.initialize_and_publish(lane, safe=True)

    def test_blocked_lane_saved_report_publishes_github_links(self):
        self.initialize_and_publish('latest-ci', safe=False)

if __name__=='__main__':unittest.main()
