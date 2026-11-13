import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
import runpy
runpy.run_path(str(Path(__file__).resolve().parents[1] / "python/run.py"))["bootstrap"]()
from hrx_systems.cache import pipeline_reader as C, pipeline_writer as W, native_adapter as N

class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'pipelines'
        self.selection=dict(zip(C.FIELDS,['owner/repo','a'*40,'model','chat','gguf','source']))
        base='https://huggingface.co/owner/repo/resolve/'+'a'*40+'/model.json'
        self.payloads={}
        def item(data):
            self.payloads[C.sha(data)]=data
            return {'size':len(data),'sha256':C.sha(data)}
        self.manifest={'schema_version':1,'selection':self.selection,
            'card':{'location':base,**item(b'card')},'lock':{'location':base+'.lock',**item(b'lock')},
            'sources':[{'installation_path':'program/model.loom','roles':['program'],'origin':'program/model.loom','resolved':None,
                        'identity_authority':'host_seed',**item(b'vector.constant')}], 'origin_roots':{},'sdk_identity':{'version':'test'}}
    def read(self): return C.read_entry(self.root,self.selection,self.manifest['card']['sha256'],self.manifest['lock']['sha256'])
    def publish(self): return W.publish(self.root,self.manifest,self.payloads)
    def test_publish_repeat_preserves_manifest_and_inode(self):
        entry=self.publish(); inode=(self.root/'entries'/f'{entry.key}.json').stat().st_ino
        self.manifest['sdk_identity']={'version':'another'}
        self.assertEqual(self.publish().raw_manifest,entry.raw_manifest)
        self.assertEqual((self.root/'entries'/f'{entry.key}.json').stat().st_ino,inode)
    def test_reader_missing_does_not_create(self):
        with self.assertRaises(C.CacheError) as error:self.read()
        self.assertEqual(error.exception.kind,'missing_root'); self.assertFalse(self.root.exists())
    def test_corrupt_blob_read_fails_explicit_seed_repairs(self):
        self.publish(); source=self.manifest['sources'][0]
        (self.root/'blobs/sha256'/source['sha256']).write_bytes(b'bad')
        with self.assertRaises(C.CacheError) as error:self.read()
        self.assertEqual(error.exception.kind,'hash_mismatch'); self.publish(); self.read()
    def test_identity_conflict_rejected(self):
        original=self.publish().raw_manifest
        self.manifest['sources'][0]['sha256']=C.sha(b'changed'); self.manifest['sources'][0]['size']=7
        self.payloads[C.sha(b'changed')]=b'changed'
        with self.assertRaises(C.CacheError) as error:self.publish()
        self.assertEqual(error.exception.kind,'identity_conflict'); self.assertEqual(self.read().raw_manifest,original)
    def test_selection_changes_miss(self):
        self.publish(); self.selection={**self.selection,'preset':'other'}
        with self.assertRaises(C.CacheError) as error:self.read()
        self.assertEqual(error.exception.kind,'missing_entry')
    def test_blob_symlink_rejected(self):
        self.publish(); path=self.root/'blobs/sha256'/self.manifest['sources'][0]['sha256']; path.unlink(); path.symlink_to('/etc/hosts')
        with self.assertRaises(C.CacheError):self.read()
        with self.assertRaises(C.CacheError):self.publish()
    def test_directory_symlink_rejected(self):
        self.publish(); alias=Path(self.tmp.name)/'alias'; alias.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(C.CacheError):C.read_entry(alias,self.selection,self.manifest['card']['sha256'],self.manifest['lock']['sha256'])
    def test_schema_rejects_paths_and_noncode(self):
        for path in ('../bad','/bad','a//b','a/./b','a\\b'):
            with self.subTest(path=path):
                self.manifest['sources'][0]['installation_path']=path
                with self.assertRaises(C.CacheError):C.validate_manifest(C.canonical(self.manifest))
        self.manifest['sources'][0]['installation_path']='good'
        self.manifest['sources'][0]['roles']=['parameter']
        with self.assertRaises(C.CacheError):C.validate_manifest(C.canonical(self.manifest))
    def test_failed_publication_keeps_previous_manifest(self):
        entry=self.publish(); (self.root/'blobs/sha256'/self.manifest['sources'][0]['sha256']).unlink()
        real=W.atomic
        def interrupt(path,data):
            if path.suffix=='.json':raise InterruptedError('test interruption')
            real(path,data)
        with patch.object(W,'atomic',side_effect=interrupt):
            with self.assertRaises(InterruptedError):self.publish()
        self.assertEqual((self.root/'entries'/f'{entry.key}.json').read_bytes(),entry.raw_manifest)
    def test_manifest_bound_to_expected_seed(self):
        entry=self.publish()
        with self.assertRaises(C.CacheError) as error:C.validate_manifest(entry.raw_manifest,self.selection,'0'*64,self.manifest['lock']['sha256'])
        self.assertEqual(error.exception.kind,'seed_mismatch')
    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(C.CacheError):C.validate_manifest(b'{"schema_version":1,"schema_version":1}')
    def test_reader_does_not_write(self):
        self.publish()
        with patch.object(W,'atomic',side_effect=AssertionError('write')),patch.object(Path,'mkdir',side_effect=AssertionError('mkdir')):
            self.read()


@unittest.skipUnless(os.environ.get('PIPELINE_CACHE_SDK_WHEEL'),'set PIPELINE_CACHE_SDK_WHEEL to test actual native SDK')
class NativeAdapterTests(unittest.TestCase):
    def setUp(self):
        CacheTests.setUp(self)
        sys.path.insert(0,os.environ['PIPELINE_CACHE_SDK_WHEEL'])
        self.addCleanup(lambda:sys.path.remove(os.environ['PIPELINE_CACHE_SDK_WHEEL']))
    def test_native_staging_is_independent_and_unknown_refuses(self):
        from loom.model.fetch_source import Source
        entry=CacheTests.publish(self); adapter=N.native_cache(entry,Path(self.tmp.name)/'staged')
        source=Source('program/model.loom',{'kind':'url','url':'https://invalid'})
        prepared=adapter.prepare_source(source,None,offline=True)
        acquired=adapter.acquire(prepared,None,offline=True)
        cachefile=self.root/'blobs/sha256'/self.manifest['sources'][0]['sha256']
        self.assertNotEqual(acquired.path.stat().st_ino,cachefile.stat().st_ino)
        self.assertFalse(acquired.path.is_symlink())
        acquired.path.write_bytes(b'edited'); self.assertEqual(cachefile.read_bytes(),b'vector.constant')
        self.assertEqual(adapter.read_document(self.manifest['card']['location'],offline=True).text,'card')
        with self.assertRaises(C.CacheError):adapter.read_document('https://unknown',offline=False)
        with self.assertRaises(C.CacheError):adapter.acquire(Source('unknown',{'kind':'url','url':'https://unknown'}),None,offline=False)
        with self.assertRaises(C.CacheError):adapter.huggingface.resolve_revision({'kind':'hf','repo':'other','revision':'main'},offline=False)
    def test_native_seeder_plans_code_only_without_asset_access(self):
        from hrx_systems.cache import seed_pipelines as seed
        from loom.model.fetch_source import Document,Source
        from loom.model.fetch_storage import ContentCache
        from loom.model.lock import FileIdentity,ModelLock
        base=self.manifest['card']['location']
        card={'schema_version':1,'model':{'id':'model','name':'fixture'},
              'programs':{'source':{'inputs':['control.loom'],'libraries':[]}},
              'services':{'chat':{'kind':'text','version':1}},
              'variants':{'gguf':{'services':{'chat':{'programs':['source'],'parameters':{'model':{'files':[{'path':'model.gguf','format':'gguf'}]}},'tokenizers':{'text':'tokenizer.json'}}}}},
              'presets':{'chat':{'service':'chat','variant':'gguf','programs':'source'}}}
        assets={'model.gguf':b'asset','tokenizer.json':b'{}'}
        lock=ModelLock.snapshot({},[FileIdentity(k,len(v),C.sha(v)) for k,v in assets.items()],scope='assets')
        docs={base:json.dumps(card),base+'.lock':json.dumps(lock)}
        work=Path(self.tmp.name)/'work';(work/'config').mkdir(parents=True)
        (work/'config/baseline.json').write_text(json.dumps(self.selection))
        receipt={'schema_version':1,'selection':self.selection,'card_sha256':C.sha(docs[base].encode()),'lock_sha256':C.sha(docs[base+'.lock'].encode())}
        (work/'seed.json').write_text(json.dumps(receipt))
        output=Path(self.tmp.name)/'out';(output/'sdk').mkdir(parents=True);(output/'sdk/sdk.json').write_text('{"identity":{"fixture":true}}')
        code=work/'control.loom';code.write_bytes(b'program')
        touched=[]
        class FixtureDownloader(ContentCache):
            def read_document(self,location,*,offline):return Document(location,docs[location])
            def prepare_source(self,source,identity,*,offline):
                touched.append(source.origin)
                if source.origin!='control.loom':raise AssertionError('asset acquisition')
                return Source(source.origin,{'kind':'local','path':str(code)},source.resolved)
        downloader=FixtureDownloader(work/'scratch')
        with patch.object(seed.SEED,'native_cache',return_value=downloader):seed.native(work,output,self.root)
        self.assertEqual(touched,['control.loom'])
        entry=C.read_entry(self.root,self.selection,receipt['card_sha256'],receipt['lock_sha256'])
        self.assertEqual(len(entry.manifest['sources']),1)
        self.assertEqual(entry.manifest['sources'][0]['identity_authority'],'host_seed')

class SeederOrchestrationTests(unittest.TestCase):
    def setUp(self):
        from hrx_systems.cache import seed_pipelines
        self.seed=seed_pipelines
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.args=type('Args',(),dict(root=self.root,suite=None,selection=None,runtime=None,output=self.root/'report'))()
        self.entries=[(name,{},None,{}) for name in ('first','second')]
    def test_failed_entry_does_not_omit_or_prevent_later_seed(self):
        calls=[]
        def one(name,*args):
            calls.append(name)
            if name=='first':raise RuntimeError('acquisition failed')
            return {'id':name,'status':'ready'}
        with patch.object(self.seed,'selected',return_value=self.entries),patch.object(self.seed,'seed_one',side_effect=one):
            self.assertEqual(self.seed.run(self.args),1)
        report=json.loads((self.args.output/'pipeline-seed-report.json').read_text())
        self.assertEqual(calls,['first','second']);self.assertEqual([r['status'] for r in report['entries']],['failed','ready'])
    def test_host_holds_batch_lock_while_child_publication_lock_is_available(self):
        import fcntl
        def one(name,*args):
            path=self.root/'cache/pipelines/.seed.lock'
            with path.open('a') as stream:
                with self.assertRaises(BlockingIOError):fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.seed.WRITER.writer_lock(path.with_name('.publish.lock')):pass
            return {'id':name,'status':'ready'}
        with patch.object(self.seed,'selected',return_value=self.entries),patch.object(self.seed,'seed_one',side_effect=one):
            self.assertEqual(self.seed.run(self.args),0)
    def test_interrupt_records_all_requested_selections(self):
        with patch.object(self.seed,'selected',return_value=self.entries),patch.object(self.seed,'seed_one',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.seed.run(self.args)
        report=json.loads((self.args.output/'pipeline-seed-report.json').read_text())
        self.assertEqual([r['id'] for r in report['entries']],['first','second'])
        self.assertEqual([r['status'] for r in report['entries']],['failed','not_run'])
