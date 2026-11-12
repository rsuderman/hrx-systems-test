import copy
import importlib.util
import json
from pathlib import Path
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

if __name__=='__main__':unittest.main()
