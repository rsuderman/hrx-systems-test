"""Exercise both offline callers against the reviewed SDK constructor shapes."""
from dataclasses import make_dataclass
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import runpy

ROOT = Path(__file__).resolve().parents[1]
runpy.run_path(str(ROOT / 'python/run.py'))['bootstrap']()
from hrx_systems.preparation.native import NativePreparation
from hrx_systems.cache import seed_assets


class Installed(Exception):
    pass


class FetchPlanCallerTests(unittest.TestCase):
    def exercise(self, caller, modern, offline=True, malformed=False):
        names = ('document', 'selection', 'sources', 'identities', 'origins',
                 'offline', 'lock_mode', 'lock_scope', 'update_path')
        plan_type = make_dataclass('FetchPlan', [(n, object) for n in names +
                                  (('previous_lock',) if modern else ())])
        document_type = make_dataclass('Document', [('location', str), ('text', str)])
        lock_text = '{"annotations": {"author": "preserve"}}'
        parsed = []
        def parse(text):
            parsed.append(text)
            return json.loads(text)
        def install(plan, cache, output):
            raise Installed(plan)
        modules = {n: ModuleType(n) for n in
                   ('loom', 'loom.model', 'loom.model.fetch', 'loom.model.fetch_source', 'loom.model.lock')}
        modules['loom.model.fetch'].__dict__.update(FetchPlan=plan_type, install=install)
        modules['loom.model.fetch_source'].__dict__.update(Document=document_type, Source=lambda **kw: kw)
        modules['loom.model.lock'].__dict__.update(FileIdentity=lambda **kw: kw,
                                                 ModelLock=SimpleNamespace(parse=parse), file_key=str)
        saved = {'document': {'location': 'https://example.invalid/model.json', 'text': '{}'},
                 'sources': {}, 'identities': {}, 'origins': {'weights': {'requested': 'immutable'}},
                 'lock_mode': 'verify', 'lock_scope': 'all'}
        selection = SimpleNamespace(files={})
        with tempfile.TemporaryDirectory() as tmp, patch.dict('sys.modules', modules):
            work = Path(tmp)
            (work / 'model.json.lock').write_text('invalid JSON' if malformed else lock_text)
            if caller == 'preparation':
                seed = SimpleNamespace(selection_for=lambda *a: selection)
                native = NativePreparation(seed=seed, assets=None, cache=None, policy=None,
                    inventory=None, contained=None, read_json=lambda p: saved, write_json=None, digest=None)
                native.indexed_cache = lambda *a: object()
                invoke = lambda: native.offline({}, {}, work, work, work, 'example', {})
            else:
                self.enterContext(patch.object(seed_assets, 'read_json', return_value=saved))
                self.enterContext(patch.object(seed_assets, 'selection_for', return_value=selection))
                self.enterContext(patch.object(seed_assets, 'routed_cache', return_value=(object(), None)))
                invoke = lambda: seed_assets.fill({}, work, work, work, offline)
            if malformed:
                with self.assertRaises(json.JSONDecodeError):
                    invoke()
                return
            with self.assertRaises(Installed) as caught:
                invoke()
            plan = caught.exception.args[0]
            self.assertIs(plan.selection, selection)
            self.assertEqual(plan.origins, saved['origins'])
            self.assertEqual((plan.lock_mode, plan.lock_scope), ('verify', 'all'))
            self.assertEqual(plan.offline, True if caller == 'preparation' else offline)
            self.assertIsNone(plan.update_path)
            if modern:
                self.assertEqual(plan.previous_lock, json.loads(lock_text))
                self.assertEqual(parsed, [lock_text])
            else:
                self.assertFalse(hasattr(plan, 'previous_lock'))
                self.assertEqual(parsed, [])

    def test_both_offline_callers_preserve_frozen_lock_on_new_sdk(self):
        for caller in ('preparation', 'seed'):
            with self.subTest(caller=caller): self.exercise(caller, True)

    def test_both_offline_callers_support_release_sdk(self):
        for caller in ('preparation', 'seed'):
            with self.subTest(caller=caller): self.exercise(caller, False)

    def test_manual_seeding_preserves_online_mode(self):
        self.exercise('seed', True, offline=False)

    def test_invalid_retained_lock_is_not_silently_discarded(self):
        for caller in ('preparation', 'seed'):
            with self.subTest(caller=caller): self.exercise(caller, True, malformed=True)
