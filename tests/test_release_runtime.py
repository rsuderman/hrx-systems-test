"""Latest release selection, frozen references and exact payload acquisition."""
import argparse
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error
import zipfile

ROOT = Path(__file__).resolve().parents[1]
def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module
import runpy
runpy.run_path(str(Path(__file__).resolve().parents[1] / "python/run.py"))["bootstrap"]()
from hrx_systems.sdk import releases as R
F = load(ROOT/'tests/test_actions_runtime.py', 'release_actions_fixture')
A = R.A
NOW = F.NOW
COMMIT, TAG = 'd'*40, 'e'*40
NAME = 'hrx_loom-0.2.0-py3-none-manylinux_2_39_x86_64.whl'


def release(**changes):
    return {'id': 7, 'tag_name': 'v0.2.0', 'draft': False, 'prerelease': False,
        'target_commitish': 'main', 'published_at': '2026-11-10T00:00:00Z',
        'assets': [{'id': 8, 'name': NAME, 'size': 100, 'state': 'uploaded',
            'digest': 'sha256:'+'c'*64, 'browser_download_url': R._url('v0.2.0', NAME)}], **changes}


class Client:
    def __init__(self, latest=None, annotated=False):
        self.latest=release() if latest is None else latest
        self.annotated=annotated;self.calls=[];self.body=b''
    def json(self,path):
        self.calls.append(path)
        if path.endswith('/releases/latest'):return copy.deepcopy(self.latest)
        if '/git/ref/tags/' in path:
            return {'ref':'refs/tags/'+self.latest['tag_name'], 'object':{'sha':TAG if self.annotated else COMMIT, 'type':'tag' if self.annotated else 'commit'}}
        if path.endswith('/git/tags/'+TAG):return {'sha':TAG,'object':{'sha':COMMIT,'type':'commit'}}
        raise AssertionError(path)
    def download_release(self,identity,destination,expected_size):
        self.calls.append(('download',identity));Path(destination).write_bytes(self.body)


class ResolutionTests(unittest.TestCase):
    def test_latest_stable_uses_actual_tag_commit_not_main(self):
        for annotated in (False,True):
            client=Client(annotated=annotated)
            source=R.resolve_source(client=client,now=NOW)
            self.assertEqual(source['tested_sha'],COMMIT)
            self.assertEqual(source['tag_sha'],TAG if annotated else COMMIT)
            self.assertEqual(source['release_id'],7)
            self.assertEqual(source['asset_id'],8)
            self.assertEqual(source['version'],'0.2.0')
            self.assertEqual(sum('/releases/latest' in x for x in client.calls),1)
            self.assertFalse(any(isinstance(x,tuple) for x in client.calls))

    def test_draft_prerelease_future_publication_rejected_without_fallback(self):
        for changes in ({'draft':True},{'prerelease':True},{'tag_name':'v0.2.0rc1'},
                        {'published_at':'2026-11-12T00:00:00Z'}):
            with self.subTest(changes=changes):
                client=Client(release(**changes))
                with self.assertRaises((R.ReleaseError,A.ActionsError)):
                    R.resolve_source(client=client,now=NOW)
                self.assertEqual(len(client.calls),1)

    def test_instrumented_and_other_platform_assets_are_excluded(self):
        value=release()
        for name in (NAME.replace('0.2.0','0.2.0+asan'),NAME.replace('0.2.0','0.2.0+debug'),
                     NAME.replace('0.2.0','0.2.0+tracy'),NAME.replace('manylinux_2_39_x86_64','win_amd64')):
            value['assets'].append({**value['assets'][0],'name':name,'id':99})
        self.assertEqual(R.resolve_source(client=Client(value),now=NOW)['asset_id'],8)

    def test_missing_ambiguous_inconsistent_or_untrusted_assets_fail(self):
        base=release()
        values=[release(assets=[]),release(assets=base['assets']*2)]
        for change in ({'digest':None},{'state':'new'},{'name':NAME.replace('0.2.0','0.1.0')},
                       {'browser_download_url':'https://evil.example/sdk.whl'},{'size':A.MAX_WHEEL_BYTES+1}):
            values.append(release(assets=[{**base['assets'][0],**change}]))
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises((R.ReleaseError,A.ActionsError)):
                    R.resolve_source(client=Client(value),now=NOW)

    def test_tag_cycle_and_wrong_reference_are_rejected(self):
        for problem in ('cycle','reference','object','changed'):
            class Broken(Client):
                def json(self,path):
                    value=super().json(path)
                    if '/git/ref/' in path:
                        if problem=='reference':value['ref']='refs/tags/v9.0.0'
                        if problem=='object':value['object']['type']='tree'
                    if '/git/tags/' in path:
                        if problem=='cycle':value['object']={'type':'tag','sha':TAG}
                        if problem=='changed':value['sha']='f'*40
                    return value
            with self.subTest(problem=problem),self.assertRaises((R.ReleaseError,A.ActionsError)):
                R.resolve_source(client=Broken(annotated=True),now=NOW)

    def test_api_failure_propagates_without_other_source_queries(self):
        class Denied(Client):
            def json(self,path):raise R.ReleaseError('permission denied','authentication')
        with self.assertRaisesRegex(R.ReleaseError,'permission'):
            R.resolve_source(client=Denied(),now=NOW)

    def test_release_later_changes_do_not_change_frozen_reference(self):
        client=Client();frozen=R.freeze_selection(R.SELECTOR,client=client,now=NOW)
        before=len(client.calls);client.latest=release(tag_name='v9.0.0')
        loaded=R.load_selection(frozen)
        self.assertEqual(loaded['source']['release'],'v0.2.0')
        loaded['source']['asset']['id']=999
        self.assertEqual(frozen['source']['asset']['id'],8)
        self.assertEqual(len(client.calls),before)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'selection.json';path.write_text(json.dumps(frozen))
            self.assertEqual(R.load_selection(path),frozen)

    def test_changed_frozen_fields_are_rejected(self):
        frozen=R.freeze_selection(R.SELECTOR,client=Client(),now=NOW)
        for name,value in [('asset_id',99),('version','9.0.0'),('tested_sha','wrong'),('tag_sha',None),('secret','token')]:
            item=copy.deepcopy(frozen);item['source'][name]=value
            with self.subTest(name=name),self.assertRaises((R.ReleaseError,A.ActionsError)):
                R.load_selection(item)
        for name,value in [('url','https://evil.example'),('name',None),('upstream_digest',None),('sha256','b'*64),('digest_verified',False)]:
            item=copy.deepcopy(frozen);item['source']['asset'][name]=value
            with self.subTest(name=name),self.assertRaises((R.ReleaseError,A.ActionsError)):
                R.load_selection(item)

    def test_actions_freeze_load_and_replay_share_contract(self):
        for replay in (False,True):
            selector=copy.deepcopy(F.SELECTOR)
            if replay:selector.update(mode='replay',run_id=1,run_attempt=1,artifact_id=10,wheel_sha256='d'*64)
            client=F.Client([F.run(1)])
            frozen=R.freeze_selection(selector,client=client,now=NOW);before=len(client.calls)
            self.assertEqual(R.load_selection(frozen),frozen)
            self.assertEqual(len(client.calls),before)
            if replay:
                changed=copy.deepcopy(frozen);changed['selector']['artifact_id']=99
                with self.assertRaisesRegex(R.ReleaseError,'replay'):R.load_selection(changed)

    def test_changed_actions_fields_and_selectors_are_rejected(self):
        frozen=R.freeze_selection(F.SELECTOR,client=F.Client([F.run(1)]),now=NOW)
        for key,value in [('run_url','https://evil.example/?token=private'),('relation_to_head','equal'),
                          ('build_age_seconds',0),('release','v0.2.0'),('extra','secret')]:
            changed=copy.deepcopy(frozen);changed['source'][key]=value
            with self.subTest(key=key),self.assertRaises((R.ReleaseError,A.ActionsError)):
                R.load_selection(changed)
        changed=copy.deepcopy(frozen);changed['source']['artifact']['archive_sha256']='b'*64
        with self.assertRaises(R.ReleaseError):R.load_selection(changed)
        changed=copy.deepcopy(frozen);changed['selector']['run_id']=1
        with self.assertRaises(R.ReleaseError):R.load_selection(changed)

    def test_request_observer_retains_context_when_release_resolution_fails(self):
        events=[]
        with self.assertRaises(R.ReleaseError):
            R.freeze_selection(R.SELECTOR,client=Client(release(draft=True)),now=NOW,observe_request=events.append)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['kind'],'release')
        self.assertEqual(A.stamp(events[0]['requested_at']),A.stamp(NOW))

    def test_cli_refuses_existing_or_linked_outputs_before_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);existing=root/'existing.json';existing.write_text('original')
            link=root/'link';link.symlink_to(root,target_is_directory=True)
            for path in [existing,link/'new.json']:
                with mock.patch.object(R,'freeze_selection') as resolve,mock.patch('sys.stderr',new_callable=io.StringIO),self.assertRaises(SystemExit):
                    R.main(['freeze','--output',str(path)])
                resolve.assert_not_called()
            self.assertEqual(existing.read_text(),'original')

    def test_new_selector_does_not_change_release_pin_or_actions_replay(self):
        parser=argparse.ArgumentParser();A.add_source_arguments(parser)
        pin={'kind':'release','release':'v0.1.0'}
        self.assertEqual(A.selector_from_args(parser.parse_args(['--runtime-source','release']),pin),pin)
        self.assertEqual(A.selector_from_args(parser.parse_args([]),pin),pin)
        self.assertEqual(A.selector_from_args(parser.parse_args(['--runtime-source','release-latest'])),R.SELECTOR)
        with self.assertRaises(A.ActionsError):
            A.selector_from_args(parser.parse_args(['--runtime-source','release-latest','--runtime-run-id','1']))


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.client=Client();self.client.body=F.wheel_bytes(['dep>=1'])
        self.client.latest['assets'][0].update(size=len(self.client.body),digest='sha256:'+hashlib.sha256(self.client.body).hexdigest())
        self.source=R.resolve_source(client=self.client,now=NOW)
        self.lock={'python':'3.12','platform':'linux_x86_64','wheels':[{'name':'dep','version':'2.0'}]}
    def acquire(self,**kwargs):
        return R.acquire_wheel(self.source,self.root/'wheel',dependency_lock=self.lock,environment=F.ENV,client=self.client,**kwargs)

    def test_exact_frozen_asset_downloads_once_and_qualifies_without_latest_query(self):
        initial=len(self.client.calls)
        self.client.latest=release(tag_name='v9.0.0')
        result=self.acquire()
        self.assertEqual(self.client.calls[initial:],[('download',8)])
        self.assertEqual(result['wheel']['version'],'0.2.0')
        self.assertTrue(result['source']['asset']['digest_verified'])
        self.assertEqual(result['wheel']['sha256'],hashlib.sha256(self.client.body).hexdigest())
        self.assertNotIn('digest_verified',self.source['asset'])
        self.assertTrue(Path(result['staged_path']).is_file())

    def test_hash_size_metadata_tag_and_dependency_failure_do_not_fallback(self):
        for problem in ('hash','size','metadata','tag','dependency'):
            with self.subTest(problem=problem),tempfile.TemporaryDirectory() as directory:
                source=copy.deepcopy(self.source);client=Client();client.body=self.client.body
                lock=copy.deepcopy(self.lock);environment=dict(F.ENV)
                if problem=='hash':client.body=client.body[:-1]+b'X'
                if problem=='size':client.body+=b'X'
                if problem=='metadata':client.body=F.wheel_bytes(version='0.1.0')
                if problem=='tag':environment['glibc_version']='2.31'
                if problem=='dependency':lock['wheels']=[]
                if problem=='metadata':source['asset'].update(size_bytes=len(client.body),upstream_digest='sha256:'+hashlib.sha256(client.body).hexdigest())
                output=Path(directory)/'wheel'
                with self.assertRaises((R.ReleaseError,A.ActionsError)):
                    R.acquire_wheel(source,output,dependency_lock=lock,environment=environment,client=client)
                self.assertEqual(client.calls,[('download',8)])
                self.assertEqual(list(output.iterdir()),[])

    def test_target_qualifier_runs_after_digest_and_version_must_agree(self):
        events=[]
        def qualifier(wheel,lock,environment):
            events.append((wheel.name,lock,environment))
            return {'name':'hrx-loom','version':'0.2.0'},'locked_metadata_compatible'
        result=self.acquire(qualifier=qualifier)
        self.assertEqual(len(events),1)
        self.assertEqual(result['compatibility_status'],'locked_metadata_compatible')

    def test_qualifier_cannot_override_bad_bytes_or_release_version(self):
        for problem in ('bytes','version'):
            with self.subTest(problem=problem),tempfile.TemporaryDirectory() as directory:
                client=Client();client.body=self.client.body
                qualifier=mock.Mock(return_value=({'name':'hrx-loom','version':'9.0.0'},'compatible'))
                if problem=='bytes':client.body=client.body[:-1]+b'X'
                output=Path(directory)/'wheel'
                with self.assertRaises(R.ReleaseError):
                    R.acquire_wheel(self.source,output,dependency_lock=self.lock,environment=F.ENV,client=client,qualifier=qualifier)
                if problem=='bytes':qualifier.assert_not_called()
                else:self.assertEqual(qualifier.call_count,1)
                self.assertEqual(list(output.iterdir()),[])

    def test_existing_or_linked_destination_rejected_before_transfer(self):
        existing=self.root/'wheel';existing.mkdir()
        before=len(self.client.calls)
        with self.assertRaisesRegex(R.ReleaseError,'destination'):self.acquire()
        self.assertEqual(len(self.client.calls),before)
        linked=self.root/'linked';linked.symlink_to(existing,target_is_directory=True)
        with self.assertRaisesRegex(R.ReleaseError,'destination'):
            R.acquire_wheel(self.source,linked/'child',dependency_lock=self.lock,environment=F.ENV,client=self.client)

    def test_credential_not_forwarded_to_release_storage(self):
        body=self.client.body
        api=mock.MagicMock();api.open.side_effect=urllib.error.HTTPError('https://api.github.com',302,'redirect',{'Location':'https://release-assets.githubusercontent.com/file?signature=private'},None)
        response=io.BytesIO(body)
        with mock.patch.object(R.urllib.request,'build_opener',return_value=api),mock.patch.object(R.urllib.request,'urlopen',return_value=response) as transfer:
            R.GitHubClient(token='secret').download_release(8,self.root/'download',len(body))
            self.assertEqual(api.open.call_args[0][0].get_header('Authorization'),'Bearer secret')
            self.assertIsNone(transfer.call_args[0][0].get_header('Authorization'))

    def test_redirects_outside_allowlist_rejected(self):
        for url in ('http://release-assets.githubusercontent.com/x','https://evil.example/x','https://user@release-assets.githubusercontent.com/x'):
            api=mock.MagicMock();api.open.side_effect=urllib.error.HTTPError('https://api.github.com',302,'redirect',{'Location':url},None)
            with self.subTest(url=url),mock.patch.object(R.urllib.request,'build_opener',return_value=api),self.assertRaisesRegex(R.ReleaseError,'redirect'):
                R.GitHubClient(token='secret').download_release(8,self.root/'download',1)


if __name__=='__main__':unittest.main()
