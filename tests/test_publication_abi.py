"""Pinned native ABI recognition and real policy/rewrite evidence handoff."""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from _publication_fixture import ABI, FIXTURES, SourceClient, publication_abi
from test_runtime_policy import POLICY, SDK, snapshot
from test_pipeline_preparation import PIPE

ROOT = Path(__file__).resolve().parents[1]
S = importlib.util.spec_from_file_location('epoch_upgrader', ROOT / 'scripts/upgrade-pipelines-0.1.0-to-0.2.0.py')
UP = importlib.util.module_from_spec(S); S.loader.exec_module(UP)
CLOSURE = ROOT / 'tests/fixtures/pipeline-upgrade-0.1.0-to-0.2.0/legacy-epoch-publication'


class RecognitionTests(unittest.TestCase):
    def test_old_and_new_same_version_classify_from_both_source_shapes(self):
        for profile, commit, expected in (
                ('legacy', '2998bccca38cb1ae06ea62f5d74743b5817f2441', 'legacy-12'),
                ('legacy', '7bf1bd93a235aa7c74766498e3b146a4d0dfbcd7', 'legacy-12'),
                ('state', '31cf5242e606c6cb654eabf063600e38f9e6f1f8', 'control-state-13')):
            with self.subTest(commit=commit), tempfile.TemporaryDirectory() as tmp:
                snap = snapshot(); snap['source']['tested_sha'] = commit
                snap['runtime_identity_sha256'] = SDK.runtime_identity_sha256(snap)
                client = SourceClient(profile)
                desc = publication_abi(snap, tmp, client=client)
                self.assertEqual(desc['receipt']['profile'], expected)
                self.assertEqual(ABI.validate(tmp, desc, snap), desc)
                self.assertEqual(len(client.calls), 2)
                self.assertTrue(all('?ref=' + commit in call for call in client.calls))
                self.assertEqual(desc['receipt']['runtime_identity_sha256'], snap['runtime_identity_sha256'])

    def test_whitespace_and_comments_do_not_change_contract(self):
        def mutate(name, raw):
            return raw.replace(b',', b', /* contract comment */\n').replace(b' ', b'  ')
        result = publication_abi(snapshot(), client=SourceClient('state', mutate))
        self.assertEqual(result['receipt']['profile'], 'control-state-13')

    def test_unknown_shapes_and_mixed_callers_are_not_count_guesses(self):
        for name, old, new in (
            ('control.c', b'IREE_VM_SCALAR_TYPE_I32', b'IREE_VM_SCALAR_TYPE_I64'),
            ('control.c', b'{NULL, 0}', b'{NULL, 1}'),
            ('control.c', b'buffer, buffer, buffer, i32', b'buffer, buffer, i32, buffer'),
            ('model.c', b'model->source.control_state', b'model->source.parameters'),
            ('model.c', b'from_ptr_borrowed', b'from_ptr_move'),
            ('model.c', b'iree_vm_variant_span_empty()', b'iree_vm_variant_span_from_array(other)'),
        ):
            with self.subTest(name=name, old=old):
                def mutate(path, raw): return raw.replace(old, new) if path == name else raw
                desc = publication_abi(snapshot(), client=SourceClient('state', mutate))
                self.assertEqual(desc['receipt']['status'], 'unsupported')
                self.assertIsNone(desc['receipt']['profile'])
        old = (FIXTURES / 'legacy-control.c').read_bytes()
        new = (FIXTURES / 'state-model.c').read_bytes()
        with self.assertRaisesRegex(ValueError, 'disagree'): ABI.recognize(old, new)

    def test_duplicate_and_comment_string_lookalikes_cannot_supply_anchors(self):
        control = (FIXTURES / 'state-control.c').read_bytes()
        model = (FIXTURES / 'state-model.c').read_bytes()
        with self.assertRaisesRegex(ValueError, 'duplicated'): ABI.recognize(control + control, model)
        with self.assertRaisesRegex(ValueError, 'missing'): ABI.recognize(b'/*' + control + b'*/', model)
        with self.assertRaisesRegex(ValueError, 'missing'):
            ABI.recognize(('"' + control.decode().replace('"', '\\"').replace('\n', '\\n') + '"').encode(), model)

    def test_unavailable_malformed_and_oversized_responses_are_explicit(self):
        for response in (None, [], {}, {'type':'file', 'path':ABI.PATHS[0], 'encoding':'base64', 'content':'x'*(2*ABI.MAX_SOURCE_BYTES+1)}):
            with self.subTest(response_type=type(response).__name__), tempfile.TemporaryDirectory() as tmp:
                client = SourceClient()
                with patch.object(client, 'json', return_value=response): desc = ABI.stage(tmp, snapshot(), client)
                self.assertEqual(desc['receipt']['status'], 'unavailable')
                ABI.validate(tmp, desc, snapshot())
        client = SourceClient()
        with patch.object(client, 'json', side_effect=OSError('token=must-not-leak')):
            desc = publication_abi(snapshot(), client=client)
        self.assertNotIn('must-not-leak', json.dumps(desc))
        with self.assertRaises(ValueError): ABI.recognize(b'x'*(ABI.MAX_SOURCE_BYTES+1), b'')

    def test_partial_unavailable_receipt_and_tampered_source_are_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = SourceClient(); get = client.json
            def fetch(path):
                if '/model.c?' in path: raise OSError('offline')
                return get(path)
            with patch.object(client, 'json', side_effect=fetch): desc = ABI.stage(tmp, snapshot(), client)
            self.assertEqual(len(desc['receipt']['sources']), 1)
            ABI.validate(tmp, desc, snapshot())
            (Path(tmp) / desc['receipt']['sources'][0]['path']).write_text('changed')
            with self.assertRaisesRegex(ValueError, 'bytes changed'): ABI.validate(tmp, desc, snapshot())

    def test_sdk_identity_receipt_and_source_tampering_are_rejected_offline(self):
        for field in ('tested_sha', 'wheel_sha256', 'runtime_identity_sha256'):
            desc = publication_abi(snapshot()); desc['receipt'][field] = '0'*len(desc['receipt'][field])
            desc['sha256'] = ABI.sha(ABI.encoded(desc['receipt']))
            with self.assertRaises(ValueError): ABI.validate_descriptor(desc, snapshot())
        with tempfile.TemporaryDirectory() as tmp:
            desc = publication_abi(snapshot(), tmp, 'state')
            path = Path(tmp) / desc['receipt']['sources'][0]['path']; path.unlink(); path.symlink_to(FIXTURES / 'state-control.c')
            with self.assertRaisesRegex(ValueError, 'symlink'): ABI.validate(tmp, desc, snapshot())
        with self.assertRaises(ValueError): POLICY.resolve(snapshot())


class MigrationTests(unittest.TestCase):
    def fixture(self, root):
        source = Path(root) / 'source'; shutil.copytree(CLOSURE, source)
        return source

    def test_exact_eight_sites_forward_state_and_keep_body_and_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.fixture(tmp); (source / 'model.json.lock').write_text('{"locked":true}\n')
            changes, report = UP.plan(source, epoch_publication_abi='control-state-13')
            self.assertEqual(report['epoch_publication_abi_edits'], 8)
            self.assertEqual(set(changes), {'control.loom', 'service.loom'})
            before = (source / 'control.loom').read_text()
            self.assertEqual(changes['control.loom'].decode().split('{',1)[1], before.split('{',1)[1])
            self.assertEqual(changes['service.loom'].decode().count('func.call @publish_epoch('+UP.EPOCH_STATE+', '), 3)
            out = Path(tmp) / 'output'; UP.write_derivative(source, out, epoch_publication_abi='control-state-13')
            self.assertEqual(UP.plan(out, epoch_publication_abi='control-state-13')[0], {})
            self.assertEqual((out / 'model.json.lock').read_bytes(), (source / 'model.json.lock').read_bytes())
            with self.assertRaisesRegex(ValueError, 'mismatch'): UP.plan(out)

    def test_legacy_default_and_cli_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.fixture(tmp)
            self.assertEqual(UP.plan(source)[0], {})
            run = subprocess.run([sys.executable, '-I', str(ROOT/'scripts/upgrade-pipelines-0.1.0-to-0.2.0.py'), str(source), '--check', '--epoch-publication-abi', 'control-state-13'],capture_output=True,text=True,check=True)
            self.assertEqual(json.loads(run.stdout)['epoch_publication_abi_edits'], 8)

    def test_partial_unknown_and_mixed_closure_fail_before_output(self):
        for mutation in ('partial', 'body', 'name', 'mixed'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                source = self.fixture(tmp); path = source/'service.loom'; text=path.read_text()
                if mutation=='partial':text=text.replace('func.decl @publish_epoch(', 'func.decl @missing_publish(')
                elif mutation=='body':text=text.replace('  func.return', '  func.call @unreviewed()\n  func.return',1)
                elif mutation=='name':text=text.replace('%plan: buffer','%another: buffer',1)
                else:text=text.replace('func.decl @publish_epoch(', 'func.decl @publish_epoch('+UP.EPOCH_STATE+': buffer, ',1)
                path.write_text(text); before=path.read_bytes(); out=Path(tmp)/'output'
                with self.assertRaisesRegex(ValueError, 'publication'):UP.write_derivative(source,out,epoch_publication_abi='control-state-13')
                self.assertFalse(out.exists()); self.assertEqual(path.read_bytes(), before)
        with tempfile.TemporaryDirectory() as tmp:
            source=self.fixture(tmp)
            with self.assertRaisesRegex(ValueError,'unavailable'):UP.plan(source,epoch_publication_abi=None)

    def test_comments_do_not_receive_edits_and_decision_has_no_closure(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=self.fixture(tmp); p=source/'service.loom'
            comment='/* '+p.read_text()+' */\n';p.write_text(comment+p.read_text())
            changes,_=UP.plan(source,epoch_publication_abi='control-state-13')
            self.assertTrue(changes['service.loom'].decode().startswith(comment))
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source';source.mkdir();(source/'model.json').write_text('{"schema_version":1}')
            (source/'decision.loom').write_text('func.def public @decision_run() {\n  func.return\n}\n')
            self.assertEqual(UP.plan(source,epoch_publication_abi=None)[0],{})


class FrozenHandoffTests(unittest.TestCase):
    def fixture(self, root, **kwargs):
        from test_pipeline_preparation import PipelinePreparationTests
        return PipelinePreparationTests().fixture(root, **kwargs)

    def test_schema2_freeze_rewrite_and_offline_verification_with_real_seam(self):
        for profile, expected in (('legacy','legacy-12'), ('state','control-state-13')):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                dest, policy, manifest, baseline, entry, suite=self.fixture(root,abi_profile=profile,epoch=True)
                self.assertEqual(policy['schema_version'],2)
                self.assertEqual(policy['publication_abi']['receipt']['profile'],expected)
                with patch('urllib.request.urlopen',side_effect=AssertionError('offline must not use network')):
                    checked=PIPE.validate_policy(root,suite)
                    PIPE.verify_upgrade(root,entry,manifest,baseline,checked)
                text=(dest/'model/program/control.loom').read_text()
                self.assertEqual(UP.EPOCH_STATE in text,profile=='state')
                report=json.loads((dest/'pipeline-upgrade.json').read_text())
                self.assertEqual(report['epoch_publication_abi'],expected)
                self.assertEqual(report['epoch_publication_abi_edits'],8 if profile=='state' else 0)
                expected_kinds=['runtime_policy','seccomp','publication_abi','publication_abi_source','publication_abi_source']
                self.assertEqual([r['kind']for r in suite['policy_files']],expected_kinds)

    def test_schema2_report_missing_and_mismatched_target_rejected(self):
        for profile in ('legacy','state'):
            for target in ('missing','legacy-12' if profile=='state' else 'control-state-13'):
                with self.subTest(profile=profile,target=target), tempfile.TemporaryDirectory() as tmp:
                    root=Path(tmp);dest,policy,manifest,baseline,entry,suite=self.fixture(root,abi_profile=profile,epoch=True)
                    report=json.loads((dest/'pipeline-upgrade.json').read_text())
                    if target=='missing':report.pop('epoch_publication_abi')
                    else:report['epoch_publication_abi']=target
                    PIPE.write_json(dest/'pipeline-upgrade.json',report)
                    entry['pipeline_upgrade']['report_sha256']=PIPE.digest(dest/'pipeline-upgrade.json')
                    with self.assertRaisesRegex(ValueError,'ABI target'):PIPE.verify_upgrade(root,entry,manifest,baseline,policy)

    def test_unknown_contract_decision_continues_but_text_fails_and_corruption_is_fatal(self):
        for kind in ('unavailable','unsupported'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                client=SourceClient('state')
                if kind=='unavailable':client.json=lambda path:None
                else:client.mutate=lambda path,raw: raw.replace(b'control_state',b'unknown_state')
                root=Path(tmp);dest,policy,manifest,baseline,entry,suite=self.fixture(root,abi_client=client)
                self.assertEqual(policy['publication_abi']['receipt']['status'],kind)
                self.assertIsNone(json.loads((dest/'pipeline-upgrade.json').read_text())['epoch_publication_abi'])
                PIPE.validate_policy(root,suite);PIPE.verify_upgrade(root,entry,manifest,baseline,policy)
                text=root/'text';text.mkdir();shutil.copytree(CLOSURE,text/'source')
                with self.assertRaisesRegex(ValueError,'unavailable'):UP.plan(text/'source',epoch_publication_abi=None)
                # Even a no-publication decision report must explicitly record None.
                report=json.loads((dest/'pipeline-upgrade.json').read_text());report.pop('epoch_publication_abi')
                PIPE.write_json(dest/'pipeline-upgrade.json',report)
                entry['pipeline_upgrade']['report_sha256']=PIPE.digest(dest/'pipeline-upgrade.json')
                with self.assertRaisesRegex(ValueError,'ABI target'):PIPE.verify_upgrade(root,entry,manifest,baseline,policy)
                (root/'policies/publication-abi/receipt.json').write_text('{}')
                with self.assertRaises(ValueError):PIPE.validate_policy(root,suite)

    def test_schema1_sealed_policy_and_raw_modes_keep_historical_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);dest,policy,manifest,baseline,entry,suite=self.fixture(root)
            # A sealed schema1 policy retains its historical recipe, without ABI evidence.
            policy.pop('publication_abi');policy['schema_version']=1
            policy['policy_sha256']=POLICY.digest({k:v for k,v in policy.items()if k!='policy_sha256'})
            self.assertEqual(POLICY.validate(policy,suite['runtime']['resolved_runtime']),policy)
            shutil.rmtree(root/'policies/publication-abi')
            PIPE.write_json(root/'policies/runtime-policy.json',policy)
            suite['policy_files']=suite['policy_files'][:2]
            suite['policy_files'][0]['sha256']=PIPE.digest(root/'policies/runtime-policy.json')
            baseline['runtime_policy_sha256']=policy['policy_sha256']
            report=json.loads((dest/'pipeline-upgrade.json').read_text())
            report.pop('epoch_publication_abi');report.pop('epoch_publication_abi_edits')
            report['operation_counts'].pop('epoch_publication_abi')
            report['implementation_revision']=1
            report['policy_sha256']=policy['policy_sha256']
            PIPE.write_json(dest/'pipeline-upgrade.json',report)
            entry['pipeline_upgrade'].update(policy_sha256=policy['policy_sha256'],report_sha256=PIPE.digest(dest/'pipeline-upgrade.json'))
            with patch('urllib.request.urlopen',side_effect=AssertionError('old sealed recipe stays offline')):
                checked=PIPE.validate_policy(root,suite)
                PIPE.verify_upgrade(root,entry,manifest,baseline,checked)
        for version,mode in (('0.1.0','auto'),('0.2.0.dev0','none')):
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);_,policy,_,_,_,suite=self.fixture(root,version=version,upgrade=mode)
                self.assertEqual(policy['schema_version'],1)
                self.assertNotIn('publication_abi',policy)
                self.assertFalse((root/'policies/publication-abi').exists())

    def test_real_capture_copy_and_report_preserve_source_evidence(self):
        from test_pipeline_preparation import RUN, PREP
        spec=importlib.util.spec_from_file_location('epoch_report',ROOT/'scripts/generate-model-report.py')
        report_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(report_module)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);prepared=root/'prepared';prepared.mkdir()
            _,policy,_,_,_,suite=self.fixture(prepared,abi_profile='state')
            suite.update(attempt_id='fixture',entries=[],inventory_errors=[{'path':'fixture','reason':'test has no execution inputs'}])
            raw=RUN.initial_results(suite,{})
            results=root/'results';results.mkdir()
            RUN.copy_evidence(prepared,results,suite,raw)
            raw.update(status='failed',reason='no execution performed')
            RUN.write_json(results/'suite-result.json',raw)
            PREP.CAPTURE.preparation_snapshot(prepared,suite)
            with patch('urllib.request.urlopen',side_effect=AssertionError('report must be offline')):
                report_module.generate(results,root/'report')
            for name in ('receipt.json','control.c.txt','model.c.txt'):
                rel=Path('policies/publication-abi')/name
                self.assertEqual((prepared/rel).read_bytes(),(results/rel).read_bytes())
                self.assertEqual((prepared/rel).read_bytes(),(prepared/'failure-evidence'/rel).read_bytes())
                self.assertTrue((root/'report'/rel).exists())
                self.assertIn(str(rel),raw['evidence'])
                self.assertIn('results/'+str(rel),(ROOT/'.github/workflows/_model-verification-worker.yml').read_text())


class PreparationSelectorTests(unittest.TestCase):
    def test_shared_preparation_hook_covers_latest_replay_and_configured_pinned_sdk(self):
        from test_host_pipeline_preparation import HostPipelinePreparationTests, P, C
        from types import SimpleNamespace
        for route in ('actions-main','actions-replay','release'):
            with self.subTest(route=route), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);args,_,manifest,payloads=HostPipelinePreparationTests().suite(root)
                C.publish(root/'cache/pipelines',manifest,payloads)
                args.runtime_source=route
                if route=='actions-replay':
                    args.runtime_run_id='123';args.runtime_run_attempt='1';args.runtime_artifact_id='456';args.runtime_wheel_sha256='a'*64
                snap=snapshot('0.2.0' if route=='release' else '0.2.0.dev0', 'release' if route=='release' else 'github_actions')
                settings,_,_,_=P.SDK.load_runtime(ROOT/'config/runtime.json')
                template=P.SDK.runtime_baseline(settings,snap)
                lock=ROOT/'config'/settings['sdk']['dependency_lock']
                client=SourceClient('state')
                with patch.object(P.subprocess,'run'),patch.object(P.PROGRESS,'run'), \
                     patch.object(P,'source_identity',return_value={'commit':'fixture'}), \
                     patch.object(P.CONFIG,'runtime',return_value=(template,snap,lock)), \
                     patch.object(P,'actions_runtime',return_value=(template,snap)) as acquired, \
                     patch.object(P.ACTIONS,'GitHubClient',return_value=client):
                    P.run_host(args)
                suite=P.read_json(root/'prepared/prepared-suite.json')
                self.assertEqual(suite['runtime_policy']['schema_version'],2)
                self.assertEqual(suite['runtime_policy']['publication_abi']['receipt']['profile'],'control-state-13')
                self.assertEqual(len(client.calls),2)
                self.assertEqual(acquired.call_count,0 if route=='release' else 1)
                for call in client.calls:self.assertTrue(call.endswith('?ref='+snap['source']['tested_sha']))
                PIPE.validate_policy(root/'prepared',suite)

    def test_unchanged_release_and_explicit_none_never_acquire_sources(self):
        from test_host_pipeline_preparation import HostPipelinePreparationTests, P, C
        for version,mode in (('0.1.0','auto'),('0.2.0.dev0','none')):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);args,_,manifest,payloads=HostPipelinePreparationTests().suite(root)
                C.publish(root/'cache/pipelines',manifest,payloads);args.pipeline_upgrade=mode
                snap=snapshot(version,'release')
                settings,_,_,_=P.SDK.load_runtime(ROOT/'config/runtime.json')
                template=P.SDK.runtime_baseline(settings,snap);lock=ROOT/'config'/settings['sdk']['dependency_lock']
                with patch.object(P.subprocess,'run'),patch.object(P.PROGRESS,'run'), \
                     patch.object(P,'source_identity',return_value={'commit':'fixture'}), \
                     patch.object(P.CONFIG,'runtime',return_value=(template,snap,lock)), \
                     patch.object(P.ACTIONS,'GitHubClient') as client:
                    P.run_host(args)
                client.assert_not_called()
                policy=P.read_json(root/'prepared/policies/runtime-policy.json')
                self.assertEqual(policy['schema_version'],1)
