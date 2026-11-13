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
import runpy
runpy.run_path(str(ROOT / 'python/run.py'))['bootstrap']()
from hrx_systems.reporting import render as REPORT
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
    def test_host_seeded_provenance_replaces_fresh_download_claim_for_new_results(self):
        self.raw['preparation_contract'] = {'pipeline_source': 'host_seeded'}
        self.raw['execution_contract'] = {'pipeline_source': 'host_seeded', 'cache_read_only': True, 'external_network': False}
        _, html, _ = self.generate()
        self.assertIn('Host-seeded pinned pipelines', html)
        self.assertNotIn('Fresh pinned pipeline staging', html)

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
    def test_migration_policy_and_publication_evidence_survive_rendering(self):
        self.raw['runtime_policy']={'policy_id':'hrx-runtime-0.2.0-v1','runtime_family':'0.2.0'}
        model=self.raw['models'][0]
        model['pipeline_upgrade']={'status':'applied','migration_id':'0.1.0-to-0.2.0','policy_sha256':'a'*64}
        model['published_execution']={'pool_capacity':100}
        model['effective_execution']={'pool_capacity':200}
        evidence='models/'+model['id']+'/publication-card.json'
        path=self.results/evidence;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('{"published":true}')
        model.setdefault('evidence',[]).append(evidence)
        report,html,md=self.generate()
        for summary in (md, html):
            self.assertNotIn('Pipeline compatibility', summary)
            self.assertNotIn('| Entry | Pipeline upgrade | Migration |', summary)
        saved=json.loads((self.root/'report/report.json').read_text())
        self.assertEqual(saved['runtime_policy'],self.raw['runtime_policy'])
        self.assertEqual(saved['models'][0]['pipeline_upgrade'],model['pipeline_upgrade'])
        self.assertEqual(report['models'][0]['published_execution']['pool_capacity'],100)
        self.assertEqual(report['models'][0]['effective_execution']['pool_capacity'],200)
        self.assertTrue((self.root/'report'/evidence).is_file())
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
        # Failures are visible first, with passing and unreached selections separated.
        self.assertLess(md.index('## Failures'), md.index('<summary>Passed (1)'))
        self.assertLess(md.index('<summary>Passed (1)'), md.index('## Not run / incomplete'))
        self.assertNotIn('**failed-model:**', md)
        self.assertIn('expected 4, got 5', md)
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
        (self.results/'huge.log').write_bytes(b'x'*(REPORT.MAX_LOG+1))
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
        for heading in ('Coverage','Runtime and hardware','Source and run','Server lifecycle','Artifacts'):
            self.assertIn('## '+heading,md)
        self.assertIn('<details><summary>Passed (1)</summary>',md)
        self.assertNotIn('## Failures',md)
        self.assertIn('**HRX runtime:** hrx-loom 0.1.0',md)
        self.assertIn('**Release:** v0.1.0',md)
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
    def test_failure_manifest_is_shared_with_renderers_and_escapes_diagnostics(self):
        self.raw['models'][0]['cycles'][0]['assertion']={'status':'failed','reason':'opaque wrapper'}
        self.raw['models'][0]['status']='failed';self.raw['status']='failed';self.save()
        manifest=REPORT.load_failures(REPORT.normalize(self.results),self.results)
        manifest['cases'][0]['display_name']='Publisher / Friendly model | <script>'
        manifest['cases'][0]['summary']='Specific observed failure | <svg>'
        manifest['cases'][0]['diagnostic']='Full original diagnostic </pre><script>unsafe</script> [untrusted](javascript:alert(1))'
        path=self.root/'failures.json';path.write_text(json.dumps(manifest))
        report=REPORT.generate(self.results,self.root/'report',path)
        html=(self.root/'report/index.html').read_text();md=(self.root/'report/summary.md').read_text()
        self.assertEqual(report['failures'],manifest)
        self.assertIn(r'Specific observed failure \| &lt;svg&gt;',md)
        self.assertIn('Full original diagnostic &lt;/pre&gt;&lt;script&gt;',html)
        self.assertNotIn('<script>',html)
        self.assertNotIn('](../failures/',md)
        self.assertNotIn('[untrusted](javascript:',md)
        self.assertIn('&#91;untrusted&#93;',md)
        self.assertIn('1 failed selections · 1 failure cases',md)

    def test_manifest_attempt_counts_and_passing_case_conflicts_rejected(self):
        self.save();normalized=REPORT.normalize(self.results)
        original=REPORT.load_failures(normalized,self.results)
        for field,value in [('attempt_id','other-attempt'),('counts',{**original['counts'],'passed':99})]:
            bad=copy.deepcopy(original);bad[field]=value
            path=self.root/'bad.json';path.write_text(json.dumps(bad))
            with self.assertRaises(REPORT.ReportError): REPORT.load_failures(normalized,self.results,path)
        bad=copy.deepcopy(original)
        bad['cases']=[{'case_id':'false','scope':'model','selection_id':'qwen-08b','display_name':'model',
                       'phase':'query','summary':'false','diagnostic':'false'}]
        path=self.root/'bad.json';path.write_text(json.dumps(bad))
        with self.assertRaisesRegex(REPORT.ReportError,'passing or unknown'): REPORT.load_failures(normalized,self.results,path)

    def test_manifest_cannot_omit_failed_selection_while_retaining_counts(self):
        self.raw['models'][0]['cycles'][0]['readiness']={'status':'failed','reason':'readiness failed'}
        self.raw['models'][0]['status']='failed';self.raw['status']='failed'
        self.save();normalized=REPORT.normalize(self.results)
        manifest=REPORT.load_failures(normalized,self.results)
        self.assertEqual(manifest['counts']['failed'],1)
        manifest['cases']=[]
        path=self.root/'missing-cases.json';path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(REPORT.ReportError,'omit a failed selection'):
            REPORT.load_failures(normalized,self.results,path)

    def test_multiple_cycles_and_independent_unload_failure_keep_case_counts(self):
        cycle=self.raw['models'][0]['cycles'][0]
        cycle['readiness']={'status':'failed','reason':'expects 11 arguments, got 10'}
        cycle['query']={'status':'not_run','reason':'readiness failed'}
        cycle['assertion']={'status':'not_run','reason':'readiness failed'}
        cycle['unload']={'status':'failed','reason':'removal deadline exceeded'}
        self.raw['models'][0]['status']='failed';self.raw['status']='failed'
        self.raw['models'][0]['cycles'].append(copy.deepcopy(cycle))
        report,html,md=self.generate()
        self.assertEqual(report['counts']['failed'],1)
        self.assertEqual(len(report['failures']['cases']),2)
        self.assertIn('1 failed selections · 2 failure cases',md)
        self.assertIn('readiness · cycle 1',md)
        self.assertIn('additional failure: unload',md)
        self.assertIn('removal deadline exceeded',html)
        self.assertNotIn('## Passed',md)
        self.assertNotIn('<summary>Passed (',html)

    def test_failure_first_even_when_passing_selection_is_first_in_inventory(self):
        expected=copy.deepcopy(self.raw['expected_entries'][0]);expected['id']='broken'
        expected['selection']['model_id']='Broken model';self.raw['expected_entries'].append(expected)
        model=copy.deepcopy(self.raw['models'][0]);model['id']='broken';model['selection']=copy.deepcopy(expected['selection'])
        model['cycles'][0]['model_id']='Broken model'
        model['cycles'][0]['query']={'status':'failed','reason':'query deadline exceeded'}
        model['cycles'][0]['assertion']={'status':'not_run','reason':'query did not finish'}
        model['status']='failed';self.raw['models'].append(model);self.raw['status']='failed'
        report,html,md=self.generate()
        self.assertLess(md.index('Broken model'),md.index('<summary>Passed (1)'))
        self.assertLess(html.index('<h2>Failures</h2>'),html.index('<summary>Passed (1)'))
        self.assertIn('query deadline exceeded',md)
        self.assertNotIn('Support matrix',html)
        self.assertEqual(report['counts']['expected'],2)

    def test_empty_inventory_is_not_verified_and_explains_missing_coverage(self):
        self.raw['expected_entries']=[];self.raw['models']=[];self.raw['status']='failed'
        report,html,md=self.generate()
        self.assertEqual(report['counts']['expected'],0)
        self.assertIn('## Failures',md)
        self.assertNotIn('>Verified<',html)
        self.assertNotIn('<summary>Passed (0)',md)

    def test_bounded_four_mib_logs_allowed_but_other_evidence_remains_two_mib(self):
        log=self.results/'late.log';log.write_bytes(b'x'*(REPORT.MAX_FILE+1))
        self.assertEqual(REPORT.evidence_path(self.results,'late.log'),log)
        data=self.results/'late.txt';data.write_bytes(log.read_bytes())
        with self.assertRaises(REPORT.ReportError): REPORT.evidence_path(self.results,'late.txt')

    def test_forty_pass_five_failure_saved_fixture_has_clear_consistent_sections(self):
        expected=copy.deepcopy(self.raw['expected_entries'][0]);model=copy.deepcopy(self.raw['models'][0])
        self.raw['expected_entries']=[];self.raw['models']=[]
        reasons=['Server expects 11 arguments; pipeline declares 10',
                 'Server expects 11 arguments; pipeline declares 10',
                 'Compiler rejection: g_gdn_conv2 index.assume [-3,-1] llm.loom:3199',
                 'Compiler rejection: g_gdn_conv index.assume [-3,-1] llm.loom:10650',
                 'expected 4 but final answer was empty']
        names=['Qwen3-Coder 30B','LFM3 Audio 2B','MiniCPM OCR','Qwen3.8 27B','Qwen3 14B']
        for number in range(45):
            entry=copy.deepcopy(expected);entry['id']=f'cached-{number:064x}'
            entry['selection']['model_id']=f'Passing model {number}' if number<40 else names[number-40]
            result=copy.deepcopy(model);result['id']=entry['id'];result['selection']=copy.deepcopy(entry['selection'])
            result['cycles'][0]['model_id']=entry['selection']['model_id']
            if number>=40:
                phase='readiness' if number<44 else 'assertion'
                result['cycles'][0][phase]={'status':'failed','reason':reasons[number-40]}
                result['status']='failed'
            if number==44:
                response=self.results/'last-response.json'
                response.write_text(json.dumps({'choices':[{'message':{'content':''},'finish_reason':'length'}],
                                                'usage':{'completion_tokens':256}}))
                result['cycles'][0]['response_path']='last-response.json'
            self.raw['expected_entries'].append(entry);self.raw['models'].append(result)
        self.raw['status']='failed'
        report,html,md=self.generate()
        self.assertEqual(report['counts']['expected'],45)
        self.assertEqual(report['counts']['passed'],40)
        self.assertEqual(report['counts']['failed'],5)
        self.assertEqual(len(report['failures']['cases']),5)
        self.assertIn('<summary>Passed (40)',md)
        self.assertIn('Server expects 11 arguments; pipeline declares 10',md)
        self.assertIn('256 completion tokens; expected 4',md)
        self.assertLess(md.index('Qwen3-Coder 30B'),md.index('<summary>Passed (40)'))
        self.assertNotIn('**cached-',md)
        links=Links();links.feed(html)
        for link in links.links:
            if not urlsplit(link).scheme and not link.startswith('#'):
                self.assertTrue((self.root/'report'/unquote(link)).is_file(),link)

    def test_long_diagnostic_is_explicitly_shortened_only_in_summary(self):
        reason='detailed compiler failure '+('retained detail '*800)
        self.raw['models'][0]['cycles'][0]['readiness']={'status':'failed','reason':reason}
        self.raw['models'][0]['status']='failed';self.raw['status']='failed'
        report,html,md=self.generate()
        self.assertIn(reason,html)
        self.assertNotIn(reason,md)
        self.assertIn('Diagnostic shortened here',md)
        self.assertEqual(report['failures']['cases'][0]['diagnostic'],reason)
        self.assertLess(len(md),10000)

    def test_malformed_schema_and_duplicate_entries_rejected(self):
        for edit in ['schema','duplicates']:
            self.raw=json.loads((FIXTURE/'suite-result.json').read_text())
            if edit=='schema':self.raw['schema_version']=2
            else:self.raw['expected_entries']*=2
            self.save()
            with self.assertRaises(REPORT.ReportError):REPORT.normalize(self.results)


if __name__=='__main__':unittest.main()
