"""Refresh producer/consumer and failure behavior without model payloads."""
import copy
import fcntl
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
runpy.run_path(str(ROOT / 'python/run.py'))['bootstrap']()
from hrx_systems.cache import refresh_publications as R, seed_suite as SUITE
try:
    import loom
except ImportError:
    loom = None


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / 'repo'; self.repo.mkdir()
        self.documents = {name: json.loads((ROOT / name).read_text()) for name in R.INPUT_PATHS}
        sample = self.documents[R.CONFIG_PATHS[0]]
        sample['entries'] = sample['entries'][:2]
        self.old = [x['selection'] for x in sample['entries']]
        names = {x['repository'] for x in self.old}
        self.documents[R.CONFIG_PATHS[1]]['entries'] = [x for x in self.documents[R.CONFIG_PATHS[1]]['entries'] if x['model_repository'] in names]
        self.documents[R.CONFIG_PATHS[2]]['overrides'] = [x for x in self.documents[R.CONFIG_PATHS[2]]['overrides'] if x['selection'] in self.old]
        self.documents[R.CONFIG_PATHS[3]]['request_profiles']['overrides'] = [
            {'selection': copy.deepcopy(self.old[0]), 'request_fields': {'reasoning_effort': 'none'}}]
        for name, doc in self.documents.items():
            path = self.repo / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(R.formatted(doc))
        self.existing = R.existing_selections(self.documents, self.root / 'missing-cache', 'loom-fun')
        self.metadata, self.inspections = {}, {}
        for n, old in enumerate(self.old):
            self.add_metadata(old['repository'], str(n + 1) * 40)
            selected = self.selected(old['model_id'], old['preset'], old['variant'])
            self.inspections[old['repository']] = self.inspection(old['repository'], {R.key(old): selected})

    def add_metadata(self, name, revision='c' * 40):
        card, lock = '{"fixture":"card"}', '{"fixture":"lock"}'
        self.metadata[name] = {'repository': name, 'revision': revision, 'status': 'available',
            'card': card, 'lock': lock, 'card_sha256': R.sha(card.encode()), 'lock_sha256': R.sha(lock.encode())}

    def selected(self, model='New/model', preset='chat', variant='gguf', size=100):
        return {'model_id': model, 'preset': preset, 'variant': variant, 'programs': 'source',
            'service_kind': 'text', 'gguf_bytes': size,
            'published_execution': {'rows': 4, 'context_capacity': 128, 'prefill_capacity': 64, 'max_tokens': 32},
            'assets': [{'path': 'parameter/model.gguf', 'roles': ['parameter'], 'size': size,
                        'sha256': 'd' * 64, 'origin': 'weights.gguf', 'resolved': None, 'transform': None}]}

    def inspection(self, name, selections):
        return {'status': 'inspected', 'selections': selections, 'errors': {},
            **{k: self.metadata[name][k] for k in ('card_sha256', 'lock_sha256')}}

    def add_new(self, choices=None):
        name = 'loom-fun/new-model'; self.add_metadata(name)
        self.inspections[name] = self.inspection(name, {'new': choices if choices is not None else [self.selected()]})
        return name

    def plan(self, include_new=False):
        return R.make_plan(self.repo, self.existing, self.metadata, self.inspections,
                           {'sdk': {'version': 'fixture'}}, include_new, 'loom-fun', {})

    def snapshot(self):
        return {name: (self.repo / name).read_bytes() for name in R.INPUT_PATHS}

    def reseal(self, plan):
        plan['plan_sha256'] = R.sha(R.canonical({k: v for k, v in plan.items() if k != 'plan_sha256'}))

    def test_plan_apply_offline_and_idempotence_preserve_profiles_and_exclusions(self):
        before = self.snapshot(); plan = self.plan()
        self.assertEqual([x['status'] for x in plan['models']], ['updated', 'updated'])
        with patch.object(R.Hub, 'get', side_effect=AssertionError('network during apply')):
            self.assertEqual(R.apply_plan(plan, self.repo)['status'], 'applied')
            self.assertEqual(R.apply_plan(plan, self.repo)['status'], 'unchanged')
        self.assertEqual((self.repo / R.INPUT_PATHS[-1]).read_bytes(), before[R.INPUT_PATHS[-1]])
        profiles = json.loads((self.repo / R.CONFIG_PATHS[2]).read_text())['overrides']
        for original in self.documents[R.CONFIG_PATHS[2]]['overrides']:
            self.assertIn(original, profiles)
            target = next(r['selection'] for r in plan['models'] if r['old_selection'] == original['selection'])
            self.assertIn({**original, 'selection': target}, profiles)
        policy = json.loads((self.repo / R.CONFIG_PATHS[3]).read_text())
        self.assertEqual(policy['seccomp'], self.documents[R.CONFIG_PATHS[3]]['seccomp'])
        self.assertEqual(policy['default_launch'], self.documents[R.CONFIG_PATHS[3]]['default_launch'])
        self.assertEqual(len(policy['request_profiles']['overrides']), 2)

    def test_saved_evidence_produces_deterministic_configuration(self):
        a, b = self.plan(), self.plan()
        self.assertEqual(a, b)
        self.assertEqual(R.validate_plan(a), a)

    def test_unavailable_and_unchanged_models_remain_in_catalog(self):
        first, second = self.old
        self.metadata[first['repository']] = {'repository': first['repository'], 'status': 'unavailable', 'reason': 'HTTP 401'}
        self.metadata[second['repository']]['revision'] = second['publication_revision']
        plan = self.plan()
        self.assertEqual({r['status'] for r in plan['models']}, {'unavailable', 'unchanged'})
        after = json.loads(plan['proposed'][R.CONFIG_PATHS[1]]['text'])
        self.assertEqual(after, self.documents[R.CONFIG_PATHS[1]])
        self.assertEqual(plan['suite']['entries'], [])

    def test_incompatible_selection_does_not_hide_independent_update(self):
        self.inspections[self.old[0]['repository']] = {'status': 'incompatible', 'reason': 'unsupported schema'}
        plan = self.plan()
        self.assertEqual({r['status'] for r in plan['models']}, {'incompatible', 'updated'})
        self.assertEqual(len(plan['suite']['entries']), 1)

    def test_new_models_require_include_new_and_do_not_expand_default_sample(self):
        self.add_new()
        self.assertNotIn('added', {r['status'] for r in self.plan()['models']})
        plan = self.plan(True)
        self.assertEqual(len(json.loads(plan['proposed'][R.CONFIG_PATHS[0]]['text'])['entries']), 2)
        self.assertEqual(len(json.loads(plan['proposed'][R.CONFIG_PATHS[1]]['text'])['entries']), 3)
        self.assertEqual(len(plan['suite']['entries']), 3)

    def test_ambiguity_size_filter_and_card_declared_default(self):
        name = self.add_new([self.selected(variant='small'), self.selected(variant='large')])
        row = next(r for r in self.plan(True)['models'] if r['repository'] == name)
        self.assertEqual(row['status'], 'needs_selection')
        limit = self.documents[R.CONFIG_PATHS[1]]['max_gguf_bytes']
        self.inspections[name]['selections']['new'][1]['gguf_bytes'] = limit + 1
        row = next(r for r in self.plan(True)['models'] if r['repository'] == name)
        self.assertEqual(row['status'], 'added'); self.assertEqual(row['selection']['variant'], 'small')
        self.inspections[name]['selections']['new'][1]['gguf_bytes'] = 101
        self.inspections[name]['selections']['new'][1]['card_default'] = True
        row = next(r for r in self.plan(True)['models'] if r['repository'] == name)
        self.assertEqual(row['selection']['variant'], 'large')

    def test_unknown_zero_or_oversize_gguf_cannot_be_added(self):
        name = self.add_new()
        for size in (None, 0, 20_000_000_001):
            with self.subTest(size=size):
                self.inspections[name]['selections']['new'][0]['gguf_bytes'] = size
                row = next(r for r in self.plan(True)['models'] if r['repository'] == name)
                self.assertEqual(row['status'], 'outside_asset_policy')

    def test_oversize_existing_catalog_is_held_instead_of_corrupting_other_updates(self):
        old = self.old[0]
        self.inspections[old['repository']]['selections'][R.key(old)]['gguf_bytes'] = 20_000_000_001
        plan = self.plan()
        self.assertEqual({r['status'] for r in plan['models']}, {'outside_asset_policy', 'updated'})

    def test_tampered_plan_or_inspection_is_rejected_before_any_write(self):
        before = self.snapshot()
        plan = self.plan(); plan['models'][0]['selection']['publication_revision'] = 'f' * 40
        with self.assertRaisesRegex(ValueError, 'digest'): R.apply_plan(plan, self.repo)
        plan = self.plan(); plan['inspections'][self.old[0]['repository']]['card_sha256'] = '0' * 64
        self.reseal(plan)
        with self.assertRaisesRegex(ValueError, 'different card'): R.apply_plan(plan, self.repo)
        self.assertEqual(before, self.snapshot())

    def test_output_path_injection_rejected_even_with_updated_plan_digest(self):
        before = self.snapshot(); plan = self.plan()
        plan['proposed']['../outside.json'] = plan['proposed'].pop(R.CONFIG_PATHS[0]); self.reseal(plan)
        with self.assertRaisesRegex(ValueError, 'undeclared'): R.apply_plan(plan, self.repo)
        self.assertEqual(before, self.snapshot()); self.assertFalse((self.root / 'outside.json').exists())

    def test_input_drift_does_not_partially_apply_other_files(self):
        plan = self.plan(); path = self.repo / R.CONFIG_PATHS[2]; path.write_text(path.read_text() + '\n')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'changed since'): R.apply_plan(plan, self.repo)
        self.assertEqual(before, self.snapshot())

    def test_mid_publication_failure_restores_all_original_config_bytes(self):
        plan = self.plan(); before = self.snapshot(); original = os.replace; count = 0
        def replace(source, target):
            nonlocal count
            count += 1
            if count == 2: raise OSError('injected publication failure')
            return original(source, target)
        with patch.object(R.os, 'replace', side_effect=replace):
            with self.assertRaisesRegex(OSError, 'injected'): R.apply_plan(plan, self.repo)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(list(self.repo.rglob('.refresh-*')), [])

    def test_interruption_rolls_back_and_concurrent_apply_cannot_enter(self):
        plan = self.plan(); before = self.snapshot(); original = os.replace; count = 0
        def replace(source, target):
            nonlocal count
            count += 1
            if count == 2: raise R.Interrupted(15)
            return original(source, target)
        with patch.object(R.os, 'replace', side_effect=replace):
            with self.assertRaises(R.Interrupted): R.apply_plan(plan, self.repo)
        self.assertEqual(before, self.snapshot())
        fd = os.open(self.repo/'config', os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, 'configuration lock'): R.apply_plan(plan, self.repo)
        finally: os.close(fd)
        self.assertEqual(before, self.snapshot())

    def test_config_symlink_rejected(self):
        plan = self.plan(); path = self.repo / R.CONFIG_PATHS[0]
        outside = self.root / 'outside.json'; shutil.copyfile(path, outside); path.unlink(); path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'symlink'): R.apply_plan(plan, self.repo)

    def test_ready_subset_reconciles_previously_applied_candidate_without_manual_revision_edits(self):
        plan = self.plan(); R.apply_plan(plan, self.repo)
        chosen = plan['models'][0]['selection_id']; result = R.apply_plan(plan, self.repo, [chosen])
        self.assertEqual(result['selected_ids'], [chosen])
        sample = json.loads((self.repo / R.CONFIG_PATHS[0]).read_text())
        selected = {R.key(e['selection']) for e in sample['entries']}
        held = plan['models'][1]
        self.assertIn(chosen, selected); self.assertIn(R.key(held['old_selection']), selected)
        self.assertEqual(R.apply_plan(plan, self.repo, [chosen])['status'], 'unchanged')
        with self.assertRaises(ValueError): R.apply_plan(plan, self.repo, ['f' * 64])

    def test_generated_suite_is_consumed_by_existing_seed_suite_contract(self):
        plan = self.plan(); path = self.root / 'suite.json'; path.write_text(R.formatted(plan['suite']))
        entries = SUITE.load_entries(path, self.root / 'cache')
        self.assertEqual(len(entries), 2)
        for entry, baseline, manifest in entries:
            self.assertIsNone(baseline)
            self.assertIn('/publication-refresh/', str(manifest))
            self.assertNotIn('/models/', str(manifest))

    def test_staged_receipts_freeze_to_real_cached_snapshot_without_entering_live_discovery(self):
        plan = self.plan(); R.apply_plan(plan, self.repo); cache = self.root/'cache'
        for item in plan['suite']['entries']:
            row = next(r for r in plan['models'] if r['selection'] == item['selection'])
            seed = {'schema_version': 1, 'selection': item['selection'],
                    'card_sha256': row['card_sha256'], 'lock_sha256': row['lock_sha256'],
                    'files': [{'installation_path': 'parameter/model.gguf', 'roles': ['parameter'], 'origin': 'weights.gguf',
                      'resolved': None, 'size': 100, 'sha256': 'd'*64, 'verified': True,
                      'cache_root': 'loom-assets', 'cache_path': 'blob/sha256/'+'d'*64}]}
            path=cache/item['seed_manifest'];path.parent.mkdir(parents=True,exist_ok=True);path.write_text(R.formatted(seed))
        self.assertEqual(R.INVENTORY.discover(cache)[0]['entries'], [])
        frozen=R.candidate_snapshot(plan,cache,self.repo)
        mode,inventory,candidates=R.SNAPSHOT.validate(frozen,cache,self.repo)
        self.assertEqual(mode,'cached');self.assertEqual(len(candidates),2)
        self.assertTrue(all(c['id'].startswith('cached-') for c in candidates))
        first=plan['models'][0]['selection_id']
        self.assertEqual(len(R.candidate_snapshot(plan,cache,self.repo,[first])['entries']),1)
        # Receipts that carry the right selection but different publications fail.
        path=cache/plan['suite']['entries'][0]['seed_manifest'];seed=json.loads(path.read_text());seed['card_sha256']='0'*64;path.write_text(R.formatted(seed))
        with self.assertRaisesRegex(ValueError,'card/lock'):R.candidate_snapshot(plan,cache,self.repo)

    def test_actual_plan_host_produces_saved_diff_and_consumable_plan(self):
        class Client:
            def repositories(inner, author):
                return {n: {'repository': n, 'revision': v['revision']} for n, v in self.metadata.items()}
            def metadata(inner, name, listed=None): return self.metadata[name]
        args = SimpleNamespace(author='loom-fun', root=self.root/'storage', include_new=False,
            model=[], select=[], runtime=None, output=self.root/'output')
        observed = []
        def inspect(batch, output, runtime):
            observed.extend(batch); return self.inspections, {'sdk': {'version': 'fixture'}}
        plan = R.plan_host(args, repo=self.repo, client=Client(), inspector=inspect)
        self.assertEqual(len(observed), 2)
        self.assertTrue((args.output/'configuration.diff').read_text())
        self.assertTrue((args.output/'summary.md').read_text())
        saved = R.parse((args.output/'plan.json').read_text()); self.assertEqual(saved, plan)
        R.apply_plan(saved, self.repo)
        self.assertFalse((args.root/'cache').exists())

    def test_explicit_choice_syntax_and_existing_model_protection(self):
        self.assertEqual(R.request_choices(['loom-fun/new=chat,gguf']), {'loom-fun/new': {'preset': 'chat', 'variant': 'gguf'}})
        for values in (['bad'], ['../outside=chat,x'], ['loom-fun/new=chat,x,y'], ['loom-fun/new=chat,x'] * 2):
            with self.assertRaises(ValueError): R.request_choices(values)

    def test_command_help_from_another_cwd_needs_no_sdk(self):
        result = subprocess.run(['bash', str(ROOT/'scripts/cache/refresh-publications.sh'), 'plan', '--help'],
            cwd=self.root, env={**os.environ, 'PYTHONPATH': str(self.root)}, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--include-new', result.stdout)


class HubTests(unittest.TestCase):
    def test_paginated_listing_and_card_urls_are_immutable_without_payload_requests(self):
        client = R.Hub(); calls = []; rev = 'a'*40
        first = 'https://huggingface.co/api/models?author=loom-fun&limit=1000&full=true'
        second = first + '&cursor=next'
        card_url = f'https://huggingface.co/loom-fun/new/resolve/{rev}/model.json'
        replies = {first: (json.dumps([{'id':'loom-fun/new','sha':rev}]), f'<{second}>; rel="next"'),
            second: ('[]',''), card_url: ('{"schema_version":1}',''), card_url+'.lock': ('{"schema_version":1}','')}
        def get(url): calls.append(url); return replies[url]
        with patch.object(client,'get',side_effect=get):
            listing = client.repositories('loom-fun')
            value = client.metadata('loom-fun/new', listing['loom-fun/new'])
        self.assertEqual(value['status'],'available')
        self.assertEqual(calls,[first,second,card_url,card_url+'.lock'])
        self.assertFalse(any('.gguf' in x for x in calls))

    def test_unavailable_repository_and_bad_listing_are_explicit(self):
        client=R.Hub()
        with patch.object(client,'get',side_effect=R.RemoteError('HTTP 401')):
            self.assertEqual(client.metadata('loom-fun/private')['reason'],'HTTP 401')
        for value in ({},[{'id':'other/model','sha':'a'*40}],[{'id':'loom-fun/model','sha':'main'}]):
            with self.subTest(value=value),patch.object(client,'get',return_value=(json.dumps(value),'')):
                with self.assertRaises(R.RemoteError):client.repositories('loom-fun')

    def test_foreign_metadata_and_credential_redirect_are_rejected(self):
        client=R.Hub()
        for url in ('http://huggingface.co/api/models','https://example.com/api/models','https://user:password@huggingface.co/api/models'):
            with self.assertRaises(ValueError):client.get(url)
        import urllib.request
        request=urllib.request.Request('https://huggingface.co/model',headers={'Authorization':'Bearer private'})
        redirected=R._Redirect().redirect_request(request,None,302,'Found',{},'https://cdn.example.com/card')
        self.assertIsNone(redirected.get_header('Authorization'))


@unittest.skipUnless(loom, 'requires the pinned installed SDK; run the tiny native refresh fixture')
class NativeRefreshTests(unittest.TestCase):
    def exercise(self, kind):
        from test_prepare_cached_models import NativePreparationTests, PREP
        fixture = NativePreparationTests('test_seeded_code_and_offline_verified_assets_keep_cache_unchanged')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        repo = fixture.root/'repo'; shutil.copytree(ROOT/'config',repo/'config')
        old = {k:fixture.baseline[k] for k in R.FIELDS}
        card_path=fixture.fixture.remote/'model.json';card=json.loads(card_path.read_text())
        card['services']['chat']['kind']=kind
        card['presets']['chat']['execution']={'pending_requests':64} if kind=='decision' else {'rows':2,'context_capacity':128}
        card_path.write_text(json.dumps(card))
        fixture.fixture.baseline['publication_revision']='f'*40
        seed_work,_,seed=fixture.fixture.attempt('refresh-seed',offline=True)
        fixture.seed_pipeline(seed_work,seed)
        name=old['repository'];text=card_path.read_text();lock=(fixture.fixture.remote/'model.json.lock').read_text()
        metadata={name:{'repository':name,'revision':'f'*40,'status':'available','card':text,'lock':lock,
                        'card_sha256':R.sha(text.encode()),'lock_sha256':R.sha(lock.encode())}}
        inspected=R.inspect_native([{**metadata[name],'existing':[old],'choice':None}])
        plan=R.make_plan(repo,[{'selection':old,'aliases':[],'assets':[],'cataloged':True}],metadata,inspected,
                         {'fixture':True},False,'loom-fun',{})
        self.assertEqual(plan['models'][0]['status'],'updated')
        R.apply_plan(plan,repo)
        receipt=fixture.cache/plan['suite']['entries'][0]['seed_manifest']
        receipt.parent.mkdir(parents=True);receipt.write_text(R.formatted(seed))
        frozen=R.candidate_snapshot(plan,fixture.cache,repo)
        snapshot=fixture.root/'candidate-snapshot.json';snapshot.write_text(R.formatted(frozen))
        mode,_,records=R.SNAPSHOT.load(snapshot,fixture.cache,repo)
        self.assertEqual(mode,'cached')
        # Run the actual host composition consumer. External SDK/container work
        # is suppressed here; below the real native preparation consumes the
        # baseline that this path produced, using only tiny seeded fixtures.
        args=SimpleNamespace(root=fixture.root,suite=None,selection_snapshot=snapshot,output=fixture.root/'host-prepared')
        with patch.object(PREP,'REPO',repo),patch.object(PREP,'source_identity',return_value={'commit':'fixture'}), \
                patch.object(PREP.subprocess,'run'),patch.object(PREP.PROGRESS,'run'):
            PREP.run_host(args)
        entry_id=records[0]['id']
        baseline=json.loads((args.output/'selection-config'/f'{entry_id}.json').read_text())
        self.assertEqual(baseline['selection_mode'],'cached')
        self.assertEqual(baseline['test_defaults']['decision']['smoke_expected_text'],'support')
        work,output=fixture.root/'refresh-prepare-work',fixture.root/'refresh-prepare-output'
        work.mkdir();output.mkdir();request_count=len(fixture.fixture.requests)
        normalized=PREP.online(baseline,seed,fixture.cache,work,location=fixture.fixture.url)
        manifest=PREP.offline(normalized,seed,fixture.cache,work,output,entry_id,{'fixture':True})
        self.assertEqual(manifest['test_kind'],kind+'_smoke')
        self.assertEqual(manifest['smoke_expected_text'],'support' if kind=='decision' else '4')
        self.assertEqual(manifest['effective_execution'],card['presets']['chat']['execution'])
        self.assertEqual(fixture.fixture.requests[request_count:],[])

    def test_text_refresh_plan_seed_snapshot_and_native_preparation(self):
        self.exercise('text')

    def test_decision_refresh_plan_seed_snapshot_and_native_preparation(self):
        self.exercise('decision')


if __name__ == '__main__':
    unittest.main()
