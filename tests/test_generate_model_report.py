"""Reporting consumes saved data only; none of these checks loads a model."""
import copy
import hashlib
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from urllib.parse import unquote, urlsplit


ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('model_report',ROOT/'scripts/generate-model-report.py')
REPORT=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)
FIXTURE=ROOT/'tests/fixtures/model-report/passing'


class Links(HTMLParser):
    def __init__(self):
        super().__init__();self.links=[];self.scripts=[]
    def handle_starttag(self,tag,attrs):
        values=dict(attrs)
        if tag=='a':self.links.append(values.get('href',''))
        if tag=='script':self.scripts.append(values)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.results=self.root/'results'
        shutil.copytree(FIXTURE,self.results)
        self.raw=json.loads((self.results/'suite-result.json').read_text())
    def save(self):
        (self.results/'suite-result.json').write_text(json.dumps(self.raw))
    def generate(self):
        self.save()
        report=REPORT.generate(self.results,self.root/'report')
        return report,(self.root/'report/index.html').read_text(),(self.root/'report/summary.md').read_text()
    def test_fixture_report_links_and_shared_counts(self):
        report,html,md=self.generate()
        self.assertEqual(report['counts']['passed'],1)
        self.assertIn('Fixture passed',html)
        self.assertIn('not proof of real model support',md)
        self.assertIn('1/1 test entries passed',md)
        links=Links();links.feed(html);self.assertEqual(links.scripts,[])
        for link in links.links:
            if not urlsplit(link).scheme and not link.startswith('#'):
                self.assertTrue((self.root/'report'/unquote(link)).is_file(),link)
        self.assertNotIn('<link ',html)
    def test_two_cycles_only_count_one_model(self):
        cycle=copy.deepcopy(self.raw['models'][0]['cycles'][0]);cycle['incarnation']=2
        self.raw['models'][0]['cycles'].append(cycle)
        report,html,md=self.generate()
        self.assertEqual(report['counts']['expected'],1)
        self.assertEqual(report['counts']['passed'],1)
        self.assertEqual(report['counts']['cycles'],2)
        self.assertIn('Cycle 2',html)
        self.assertIn('2 lifecycle cycles',md)

    def test_cache_inventory_errors_remain_failed_and_human_readable(self):
        self.raw['selection_mode'] = 'cached'
        self.raw['inventory_errors'] = [{'path': 'models/private', 'reason': 'Permission denied'}]
        report, html, md = self.generate()
        self.assertEqual(report['status'], 'failed')
        self.assertIn('Cache inventory errors', md)
        self.assertIn('Permission denied', html)
        self.assertIn('all published runner-cache selections', md)
    def test_mixed_failure_and_not_run_keep_all_expected_entries(self):
        for identifier,status in [('failed-model','failed'),('unreached','not_run')]:
            expected=copy.deepcopy(self.raw['expected_entries'][0]);expected['id']=identifier
            self.raw['expected_entries'].append(expected)
            model=copy.deepcopy(self.raw['models'][0]);model['id']=identifier;model['status']=status
            model['reason']='wrong answer' if status=='failed' else 'prior cleanup failed'
            if status=='failed':model['cycles'][0]['assertion']={'status':'failed','reason':'expected 4, got 5'}
            else:model['cycles']=[]
            self.raw['models'].append(model)
        self.raw['status']='failed'
        report,html,md=self.generate()
        self.assertEqual(report['counts'],{'expected':3,'passed':1,'failed':1,'not_run':1,'incomplete':0,'cycles':2,'distinct_models':1})
        self.assertEqual(report['status'],'failed')
        self.assertIn('wrong answer',html);self.assertIn('prior cleanup failed',md)
        # Failure notes belong below the uninterrupted table.
        self.assertEqual(md.count('| Qwen/Qwen3.5-0.8B'),3)
    def test_missing_phase_and_model_never_imply_success(self):
        del self.raw['models'][0]['cycles'][0]['unload']
        other=copy.deepcopy(self.raw['expected_entries'][0]);other['id']='no-result'
        self.raw['expected_entries'].append(other)
        report,_,_=self.generate()
        self.assertEqual(report['counts']['incomplete'],2)
        self.assertNotEqual(report['status'],'passed')
    def test_failed_cleanup_overrides_passing_queries(self):
        self.raw['mode']='real';self.raw['server']['cleanup']={'status':'failed','reason':'container did not exit'}
        report,html,md=self.generate()
        self.assertEqual(report['status'],'failed')
        self.assertIn('container did not exit',html)
        self.assertIn('cleanup: Failed',md)
        self.assertNotIn('>Verified<',html)
    def test_earlier_failed_cycle_cannot_be_masked(self):
        bad=copy.deepcopy(self.raw['models'][0]['cycles'][0]);bad['query']={'status':'failed','reason':'timeout'}
        self.raw['models'][0]['cycles'].insert(0,bad)
        report,_,_=self.generate();self.assertEqual(report['status'],'failed')
    def test_identity_mismatch_and_missing_query_evidence_rejected(self):
        self.raw['models'][0]['cycles'][0]['model_id']='wrong-model';self.save()
        with self.assertRaisesRegex(REPORT.ReportError,'identity'):REPORT.normalize(self.results)
        self.raw['models'][0]['cycles'][0]['model_id']=self.raw['expected_entries'][0]['selection']['model_id']
        self.raw['models'][0]['cycles'][0].pop('response_path');self.save()
        with self.assertRaisesRegex(REPORT.ReportError,'response_path'):REPORT.normalize(self.results)
    def test_publisher_and_response_text_are_escaped(self):
        injection='<img src=x onerror=alert(1)> | [click](javascript:alert(1))'
        for where in [self.raw['expected_entries'][0]['selection'],self.raw['models'][0]['selection']]:where['model_id']=injection
        self.raw['models'][0]['cycles'][0]['model_id']=injection
        response=self.results/'models/qwen-08b/response.json'
        response.write_text(json.dumps({'choices':[{'message':{'content':'</pre><script>alert(1)</script>'}}]}))
        _,html,md=self.generate()
        self.assertNotIn('<img ',html);self.assertNotIn('<script>',html)
        self.assertIn('&lt;script&gt;',html)
        self.assertIn('\\|',md);self.assertIn('\\[click\\]',md)
    def test_evidence_escape_symlink_payload_missing_and_oversized_rejected(self):
        outside=self.root/'secret.log';outside.write_text('private')
        for reference in ['../secret.log','/secret.log','models/qwen-08b/../../secret.log','models/qwen-08b/model.gguf','missing.log']:
            with self.subTest(reference=reference):
                with self.assertRaises(REPORT.ReportError):REPORT.evidence_path(self.results,reference)
        (self.results/'escape.log').symlink_to(outside)
        with self.assertRaises(REPORT.ReportError):REPORT.evidence_path(self.results,'escape.log')
        (self.results/'huge.log').write_bytes(b'x'*(REPORT.MAX_FILE+1))
        with self.assertRaises(REPORT.ReportError):REPORT.evidence_path(self.results,'huge.log')
    def test_re_render_is_deterministic_and_does_not_mutate_saved_results(self):
        before={str(p.relative_to(self.results)):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.results.rglob('*') if p.is_file()}
        REPORT.generate(self.results,self.root/'report-one')
        REPORT.generate(self.results,self.root/'report-two')
        for name in ('index.html','report.json','summary.md'):
            self.assertEqual((self.root/'report-one'/name).read_bytes(),(self.root/'report-two'/name).read_bytes())
        after={str(p.relative_to(self.results)):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.results.rglob('*') if p.is_file()}
        self.assertEqual(before,after)
        with self.assertRaises(REPORT.ReportError):REPORT.generate(self.results,self.results/'nested')
        with self.assertRaises(REPORT.ReportError):REPORT.generate(self.results,self.root/'report-one')
    def test_cli_failed_test_results_render_successfully(self):
        self.raw['status']='failed';self.raw['reason']='intentional failed run';self.save()
        result=subprocess.run(['bash',str(ROOT/'scripts/generate-model-report.sh'),'--results',str(self.results),'--output',str(self.root/'report')],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads((self.root/'report/report.json').read_text())['status'],'failed')
    def test_nested_phase_server_and_cycle_evidence_is_bundled(self):
        paths=['server-start.log','server-cleanup.log','models/qwen-08b/phase.log','models/qwen-08b/cycle.log','models/qwen-08b/preparation.log']
        for name in paths:(self.results/name).write_text('bounded evidence')
        self.raw['server']['evidence']=[paths[0]]
        self.raw['server']['cleanup']['evidence']=[paths[1]]
        self.raw['models'][0]['cycles'][0]['query']['evidence']=[paths[2]]
        self.raw['models'][0]['cycles'][0]['evidence']=[paths[3]]
        self.raw['models'][0]['preparation']['evidence']=[paths[4]]
        report,_,_=self.generate()
        for name in paths:
            self.assertIn(name,report['evidence_files'])
            self.assertTrue((self.root/'report'/name).is_file())
    def test_raw_suite_result_symlink_is_rejected(self):
        source=self.results/'suite-result.json'
        outside=self.root/'outside.json';outside.write_bytes(source.read_bytes())
        source.unlink();source.symlink_to(outside)
        with self.assertRaisesRegex(REPORT.ReportError,'Missing or oversized JSON'):REPORT.normalize(self.results)
    def test_optional_workflow_links_use_validated_metadata(self):
        workflow=json.loads((ROOT/'tests/fixtures/model-report/workflow.json').read_text())
        self.raw['workflow']=workflow
        (self.results/'workflow.json').write_text(json.dumps(workflow))
        self.raw['evidence'].append('workflow.json')
        report,html,md=self.generate()
        run=workflow['run_url'];source=f"https://github.com/{workflow['repository']}/commit/{workflow['sha']}"
        self.assertIn(f'href="{run}"',html);self.assertIn(f'href="{source}"',html)
        self.assertIn(f'[GitHub run #{workflow["run_id"]}]({run})',md)
        self.assertIn(source,md)
        self.assertEqual(report['workflow'],workflow)
        self.assertTrue((self.root/'report/workflow.json').is_file())
    def test_optional_workflow_rejects_unsafe_or_mismatched_urls(self):
        workflow=json.loads((ROOT/'tests/fixtures/model-report/workflow.json').read_text())
        for bad in ('javascript:alert(1)','http://github.com/x','https://github.com.evil.test/x',
                    workflow['run_url']+'?redirect=evil','https://github.com/other/repo/actions/runs/1'):
            with self.subTest(url=bad):
                metadata={**workflow,'run_url':bad,'sha':'bad'}
                self.assertEqual(REPORT.github_links({'workflow':metadata}),{})
        for bad in ('owner/../repo','//evil.test/repo','owner/repo<svg>'):
            self.assertEqual(REPORT.github_links({'workflow':{**workflow,'repository':bad}}),{})
        self.assertEqual(REPORT.github_links({}),{})
        # No supplied run URL is needed when repository and run identity are valid.
        workflow.pop('run_url')
        self.assertIn('GitHub run #42906729082',REPORT.github_links({'workflow':workflow}))
        workflow['run_url']='https://github.com/rsuderman/hrx-systems-test/actions/runs/42906729082/attempts/1'
        self.assertIn('GitHub run #42906729082',REPORT.github_links({'workflow':workflow}))
    def test_fifty_entries_with_full_compact_evidence_render_and_link(self):
        original_expected=copy.deepcopy(self.raw['expected_entries'][0])
        original_model=copy.deepcopy(self.raw['models'][0])
        self.raw['expected_entries']=[];self.raw['models']=[]
        for number in range(50):
            identifier=f'model-{number:02d}'
            expected=copy.deepcopy(original_expected);expected['id']=identifier
            expected['selection']['model_id']=f'Example/Model-{number:02d}'
            model=copy.deepcopy(original_model);model['id']=identifier
            model['selection']=copy.deepcopy(expected['selection'])
            cycle=model['cycles'][0];cycle['model_id']=expected['selection']['model_id']
            directory=self.results/'models'/identifier;directory.mkdir()
            request={'model':cycle['model_id'],'messages':[{'role':'user','content':'Two plus two?'}]}
            (directory/'request.json').write_text(json.dumps(request))
            (directory/'response.json').write_text(json.dumps({'choices':[{'message':{'content':'4'}}]}))
            (directory/'prepared-inputs.json').write_text(json.dumps({'selection':expected['selection']}))
            cycle['request_path']=f'models/{identifier}/request.json'
            cycle['response_path']=f'models/{identifier}/response.json'
            model['evidence']=[f'models/{identifier}/prepared-inputs.json']
            for n in range(9):
                path=f'models/{identifier}/stage-{n}.log'
                (self.results/path).write_text('compact fixture evidence')
                model['evidence'].append(path)
            self.raw['expected_entries'].append(expected);self.raw['models'].append(model)
        report,html,md=self.generate()
        self.assertEqual(report['counts']['expected'],50)
        self.assertEqual(report['counts']['passed'],50)
        self.assertEqual(report['counts']['distinct_models'],50)
        self.assertEqual(report['counts']['cycles'],50)
        self.assertGreaterEqual(len(report['evidence_files']),600)
        self.assertIn('50/50 test entries passed',md)
        links=Links();links.feed(html)
        for link in links.links:
            if not urlsplit(link).scheme and not link.startswith('#'):
                self.assertTrue((self.root/'report'/unquote(link)).is_file(),link)
    def test_summary_is_readable_without_flattened_identity_json(self):
        self.raw['runtime']={'platform':'linux/amd64','image':'python@sha256:full-image-digest',
            'sdk':{'name':'hrx-loom','version':'0.1.0','release':'v0.1.0',
                   'wheel':{'url':'full-identity-payload-url','sha256':'full-wheel-sha'}},
            'sdk_identity':{'nested':'retained-runtime-identity'}}
        self.raw['hardware']={'name':'AMD GPU (PCI 1002:1586)','device_uri':'amdgpu://0','driver':'amdgpu',
                              'extra':{'nested':'retained-hardware-identity'}}
        self.raw['source']={'commit':'faaa58382503bfa38b50e8c2a4c6d270f37e3bc6','dirty':False,
                            'files_sha256':{'script.py':'retained-source-identity'}}
        report,html,md=self.generate()
        for heading in ('Coverage','Model results','Runtime and hardware','Source and run','Server lifecycle','Artifacts'):
            self.assertIn('## '+heading,md)
        self.assertIn('**HRX runtime:** hrx-loom 0.1.0',md)
        self.assertIn('release v0.1.0',md)
        self.assertIn('**GPU:** AMD GPU',md)
        self.assertIn('amdgpu://0',md);self.assertIn('linux/amd64',md)
        self.assertIn('faaa58382503 · clean checkout',md)
        self.assertIn('Server cleanup: Passed',md)
        for marker in ('full-image-digest','full-identity-payload-url','retained-runtime-identity',
                       'retained-hardware-identity','retained-source-identity','"files_sha256"','"sdk_identity"'):
            self.assertNotIn(marker,md)
        self.assertLess(len(md),4000)
        self.assertEqual(report['runtime'],self.raw['runtime'])
        self.assertIn('retained-runtime-identity',html)
    def test_github_summary_links_only_to_validated_run_and_source(self):
        self.raw['workflow']=json.loads((ROOT/'tests/fixtures/model-report/workflow.json').read_text())
        _,_,md=self.generate()
        self.assertIn('from this run’s artifacts',md)
        self.assertIn(self.raw['workflow']['run_url'],md)
        for filename in ('index.html','report.json','suite-result.json'):
            self.assertNotIn(']('+filename+')',md)
        self.assertIn('Workflow source',md)
    def test_local_summary_keeps_working_relative_artifact_links(self):
        _,_,md=self.generate()
        for filename in ('index.html','report.json','suite-result.json'):
            self.assertIn(']('+filename+')',md)
            self.assertTrue((self.root/'report'/filename).is_file())
    def test_local_sha_only_workflow_metadata_keeps_relative_artifact_links(self):
        self.raw['workflow']={'schema_version':1,'sha':'faaa58382503bfa38b50e8c2a4c6d270f37e3bc6'}
        _,_,md=self.generate()
        for filename in ('index.html','report.json','suite-result.json'):
            self.assertIn(']('+filename+')',md)
            self.assertTrue((self.root/'report'/filename).is_file())
        self.assertNotIn('workflow’s artifact bundle',md)
    def test_invalid_workflow_run_has_plain_artifact_instruction_not_broken_links(self):
        self.raw['workflow']={'repository':'org/repo','run_id':'1','run_url':'javascript:unsafe'}
        _,_,md=self.generate()
        self.assertIn('workflow’s artifact bundle',md)
        self.assertNotIn('javascript:',md)
        self.assertNotIn('](index.html)',md)
    def test_malformed_schema_and_duplicate_entries_rejected(self):
        for edit in ['schema','duplicates']:
            self.raw=json.loads((FIXTURE/'suite-result.json').read_text())
            if edit=='schema':self.raw['schema_version']=2
            else:self.raw['expected_entries']*=2
            self.save()
            with self.assertRaises(REPORT.ReportError):REPORT.normalize(self.results)


if __name__=='__main__':unittest.main()
