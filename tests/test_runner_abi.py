# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
"""Offline runner ownership recognition, frozen evidence and composition checks."""
import base64
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from _publication_fixture import ROOT
from hrx_systems.sdk import runner_abi as ABI
from hrx_systems.preparation import runtime_policy as POLICY
from hrx_systems.pipelines import seals as PIPE
from test_runtime_policy import snapshot

STAGE = ABI.stage
FIXTURES = ROOT / 'tests/fixtures/runner-abi'


class Client:
    def __init__(self, profile='borrowed'):
        self.profile = profile

    def json(self, url):
        path = url.split('/contents/')[1].split('?')[0]
        raw = (FIXTURES / (self.profile + '-' + Path(path).name)).read_bytes()
        return {'type': 'file', 'path': path, 'encoding': 'base64', 'content': base64.b64encode(raw).decode()}


def runner_descriptor(snap, root, profile='borrowed'):
    return STAGE(root, snap, Client(profile))


class RunnerAbiTests(unittest.TestCase):
    def test_signature_selected_ownership_and_publication(self):
        from hrx_systems.sdk import publication_abi
        module, control, model = [(FIXTURES / ('selected-' + n + '.c')).read_bytes()
                                  for n in ('module', 'control', 'model')]
        self.assertEqual(ABI.recognize(module, control)['profile'], 'borrowed-ptr')
        self.assertEqual(publication_abi.recognize(control, model)['profile'], 'proposal-counts-14')
        with tempfile.TemporaryDirectory() as tmp:
            snap = snapshot('0.3.0.dev0')
            runner = ABI.stage(tmp, snap, Client('selected'))
            pub = publication_abi.stage(tmp, snap, Client('selected'))
            policy = POLICY.resolve(snap, publication_abi=pub, runner_abi=runner)
            ABI.validate(tmp, runner, snap)
            publication_abi.validate(tmp, pub, snap)
            POLICY.validate(policy, snap)

    def test_signature_selection_rejects_changed_routes(self):
        from hrx_systems.sdk import publication_abi
        raw = {n: (FIXTURES / ('selected-' + n + '.c')).read_bytes()
               for n in ('module', 'control', 'model')}
        for name, old, new, recognizer, inputs in (
            ('module', b'options.owned_hal_arguments ?', b'!options.owned_hal_arguments ?', ABI, ('module', 'control')),
            ('control', b'device_buffer.kind == IREE_VM_SIGNATURE_TYPE_KIND_REF', b'device_buffer.kind != IREE_VM_SIGNATURE_TYPE_KIND_REF', ABI, ('module', 'control')),
            ('control', b'.owned_hal_arguments = owned_hal_arguments', b'.owned_hal_arguments = true', ABI, ('module', 'control')),
            ('control', b'source->types.buffer, &device_buffer', b'source->vm_types.buffer, &device_buffer', ABI, ('module', 'control')),
            ('control', b'const iree_vm_signature_type_t step_arguments[]', b'device_buffer.kind = IREE_VM_SIGNATURE_TYPE_KIND_REF; const iree_vm_signature_type_t step_arguments[]', ABI, ('module', 'control')),
            ('control', b'status = loom_runtime_module_create', b'options.owned_hal_arguments = true; status = loom_runtime_module_create', ABI, ('module', 'control')),
            ('control', b'--control->publish_argument_count', b'++control->publish_argument_count', publication_abi, ('control', 'model')),
            ('control', b'IREE_ARRAYSIZE(publish_arguments) - 1', b'IREE_ARRAYSIZE(publish_arguments) - 2', publication_abi, ('control', 'model')),
            ('model', b'model->control.publish_argument_count),', b'13),', publication_abi, ('control', 'model')),
            ('model', b'model->control.publish_argument_count ==', b'model->control.publish_argument_count =', publication_abi, ('control', 'model')),
            ('model', b'const uint8_t* records =', b'model->control.publish_argument_count = 13; const uint8_t* records =', publication_abi, ('control', 'model')),
            ('model', b'TEXT_HOST_PROPOSAL_COUNTS', b'TEXT_HOST_PROGRESS', publication_abi, ('control', 'model')),
        ):
            with self.subTest(name=name, old=old):
                self.assertIn(old, raw[name])
                changed = dict(raw); changed[name] = raw[name].replace(old, new)
                with self.assertRaises(ValueError):
                    recognizer.recognize(*(changed[n] for n in inputs))

    def test_reviewed_profiles_and_unrelated_source_edits(self):
        for name, profile in [('owning', 'owning-ref'), ('borrowed', 'borrowed-ptr')]:
            raw = [(FIXTURES / (name + '-' + Path(p).name)).read_bytes() for p in ABI.PATHS]
            self.assertEqual(ABI.recognize(*raw)['profile'], profile)
            self.assertEqual(ABI.recognize(*[b'// unrelated comment\n' + b for b in raw])['profile'], profile)
            with self.assertRaises(ValueError):
                ABI.recognize(raw[0].replace(b'module->types[0] = types->buffer;', b'module->types[0] = types->fence;'), raw[1])
            with self.assertRaises(ValueError):
                ABI.recognize(raw[0], raw[1].replace(b'.value.ref = source->types.buffer', b'.value.ref = source->vm_types.buffer'))
            for index in (0, 1):
                changed = list(raw)
                changed[index] = changed[index].replace(b'TYPE_KIND_PTR' if name == 'borrowed' else b'TYPE_KIND_REF', b'TYPE_KIND_SCALAR', 1)
                with self.assertRaises(ValueError):
                    ABI.recognize(*changed)

    def test_evidence_is_offline_and_tamper_evident(self):
        snap = snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            desc = runner_descriptor(snap, tmp)
            with patch('urllib.request.urlopen', side_effect=AssertionError('offline')):
                ABI.validate(tmp, desc, snap)
            source = Path(tmp) / desc['receipt']['sources'][0]['path']
            source.write_bytes(source.read_bytes() + b' ')
            with self.assertRaises(ValueError): ABI.validate(tmp, desc, snap)
            changed = copy.deepcopy(desc)
            changed['receipt']['profile'] = 'owning-ref'
            with self.assertRaises(ValueError): ABI.validate_descriptor(changed, snap)

    def test_schema3_policy_preserves_old_policy_and_rejects_unknown(self):
        from _publication_fixture import publication_abi
        snap = snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            pub = publication_abi(snap, tmp, 'state')
            old = POLICY.resolve(snap, publication_abi=pub)
            self.assertEqual(old['schema_version'], 2)
            desc = runner_descriptor(snap, tmp)
            new = POLICY.resolve(snap, publication_abi=pub, runner_abi=desc)
            self.assertEqual(new['schema_version'], 3)
            POLICY.validate(new, snap); POLICY.validate(old, snap)
            changed = copy.deepcopy(new)
            changed['runner_abi']['receipt']['tested_sha'] = '0' * 40
            changed['policy_sha256'] = POLICY.digest({k:v for k,v in changed.items() if k != 'policy_sha256'})
            with self.assertRaises(ValueError): POLICY.validate(changed, snap)

    def test_composition_and_stage_tamper(self):
        from test_pipeline_preparation import PipelinePreparationTests
        from hrx_systems import layout
        for native in (False, True):
            for target in ('state', 'proposals'):
                with self.subTest(native=native, target=target), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    dest, policy, manifest, baseline, entry, suite = PipelinePreparationTests().fixture(
                        root, native=native, abi_profile=target, runner_profile='borrowed', decision=target=='proposals')
                    PIPE.validate_policy(root, suite)
                    PIPE.verify_upgrade(root, entry, manifest, baseline, policy)
                    report = PIPE.read_json(dest / 'pipeline-upgrade.json')
                    self.assertIn('ptr<@HalBuffer>', (dest / 'model/program/runner.loom').read_text())
                    self.assertIn('hal.buffer', (dest / 'model-original/program/runner.loom').read_text())
                    source = {'layout_version': layout.LAYOUT_VERSION, 'migration_implementation_path': layout.MIGRATION_RELATIVE,
                              'files_sha256': {p:PIPE.digest(ROOT/p) for p in layout.implementation_files()}}
                    PIPE.verify_migration_implementations(report, source)
                    changed = copy.deepcopy(report)
                    changed['stages'][-1]['implementation_sha256'] = '0'*64
                    with self.assertRaises(ValueError): PIPE.verify_migration_implementations(changed, source)
                    changed = copy.deepcopy(report)
                    changed['stages'][-1]['source_files'] = []
                    with self.assertRaises(ValueError): PIPE._verify_runner_stages(changed, policy, report.get('native_publication'), policy['publication_abi']['receipt']['profile'])

    def test_actual_03_contract_and_source_verified_family(self):
        from hrx_systems.sdk import publication_abi
        snap = snapshot('0.3.0.dev0')
        self.assertEqual(ABI.recognize((FIXTURES/'0.3.0-module.c').read_bytes(), (FIXTURES/'0.3.0-control.c').read_bytes())['profile'], 'borrowed-ptr')
        self.assertEqual(publication_abi.recognize((FIXTURES/'0.3.0-control.c').read_bytes(), (FIXTURES/'0.3.0-model.c').read_bytes())['profile'], 'proposal-counts-14')
        from _publication_fixture import publication_abi as descriptor
        with tempfile.TemporaryDirectory() as tmp:
            pub = descriptor(snap, tmp, 'proposals')
            runner = runner_descriptor(snap, tmp)
            policy = POLICY.resolve(snap, publication_abi=pub, runner_abi=runner)
            self.assertEqual(policy['runtime_family'], '0.2.0')
            POLICY.validate(policy, snap)
            with self.assertRaises(ValueError): POLICY.resolve(snap, publication_abi=pub)

    def test_runner_composition_overlap_and_receipt_failure(self):
        import json
        from hrx_systems.pipelines import upgrader
        from test_runner_ownership_upgrade import programs
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'source'; source.mkdir()
            (source/'model.json').write_text(json.dumps({'schema_version':1}))
            (source/'control.loom').write_text(programs()['control.loom'])
            with self.assertRaisesRegex(ValueError, 'overlap'):
                upgrader.write_runner_derivative(source, source/'nested', legacy=False, target='control-state-13')
            output = Path(tmp)/'output'
            with patch.object(upgrader.json, 'dump', side_effect=OSError('receipt failure')):
                with self.assertRaisesRegex(OSError, 'receipt failure'):
                    upgrader.write_runner_derivative(source, output, legacy=False, target='control-state-13')
            self.assertFalse(output.exists())
            self.assertFalse(output.with_name('output.upgrade.json').exists())
