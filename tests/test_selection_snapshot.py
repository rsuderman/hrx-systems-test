import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
import runpy
runpy.run_path(str(Path(__file__).resolve().parents[1] / "python/run.py"))["bootstrap"]()
from hrx_systems.selection import snapshot as M

class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.cache=self.root/'cache';self.repo=self.root/'repo'
        self.cache.mkdir();(self.repo/'config').mkdir(parents=True)
        (self.repo/'config/model-test-profiles.json').write_text('{}')
        self.seed={'schema_version':1,'selection':{'repository':'loom-fun/example','publication_revision':'a'*40,'model_id':'example/model','preset':'chat','variant':'gguf','programs':'source'},'card_sha256':'b'*64,'lock_sha256':'c'*64,'files':[{'installation_path':'parameter/model.gguf','cache_root':'loom-assets','cache_path':'blob/'+'d'*64,'verified':True,'roles':['parameter'],'origin':'@checkpoint/model.gguf','resolved':None,'size':4,'sha256':'d'*64}]}
        (self.cache/'seed-manifest.json').write_text(json.dumps(self.seed))
    def save(self,doc):
        p=self.root/'snapshot.json';p.write_text(json.dumps(doc));return p
    def test_cached_receipt_changes_do_not_change_frozen_selection(self):
        doc=M.freeze('cached',self.cache,self.repo)
        original=(self.cache/'seed-manifest.json').read_bytes()
        (self.cache/'seed-manifest.json').unlink()
        mode,inventory,entries=M.load(self.save(doc),self.cache,self.repo)
        self.assertEqual(mode,'cached');self.assertEqual(entries[0]['_raw'],original)
        self.assertEqual(len(inventory['entries']),1)
    def test_suite_uses_frozen_seed_and_authored_entry(self):
        suite=self.repo/'config/suite.json';suite.write_text(json.dumps({'schema_version':1,'entries':[{'id':'example','selection':self.seed['selection'],'seed_manifest':'seed-manifest.json'}]}))
        doc=M.freeze('suite',self.cache,self.repo,suite)
        (self.cache/'seed-manifest.json').write_text('{}')
        mode,_,entries=M.load(self.save(doc),self.cache,self.repo)
        self.assertEqual(mode,'suite');self.assertEqual(entries[0]['_seed'],self.seed)
    def test_config_and_seed_tampering_are_rejected(self):
        doc=M.freeze('cached',self.cache,self.repo)
        changed=copy.deepcopy(doc);changed['entries'][0]['seed_sha256']='e'*64
        with self.assertRaisesRegex(ValueError,'digest'):M.load(self.save(changed),self.cache,self.repo)
        (self.repo/'config/model-test-profiles.json').write_text('{"changed":true}')
        with self.assertRaisesRegex(ValueError,'configuration changed'):M.load(self.save(doc),self.cache,self.repo)
    def test_duplicate_entry_and_snapshot_symlink_are_rejected(self):
        doc=M.freeze('cached',self.cache,self.repo);doc['entries'].append(copy.deepcopy(doc['entries'][0]))
        with self.assertRaisesRegex(ValueError,'duplicate'):M.load(self.save(doc),self.cache,self.repo)
        p=self.save(M.freeze('cached',self.cache,self.repo));link=self.root/'linked';link.symlink_to(p)
        with self.assertRaisesRegex(ValueError,'regular'):M.load(link,self.cache,self.repo)
