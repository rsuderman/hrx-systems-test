"""Ownership migration rejects producers and preserves the forwarding graph."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tools/migrations/upgrade-pipelines-runner-owned-to-borrowed.py'
spec = importlib.util.spec_from_file_location('runner_ownership_upgrade', SCRIPT)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


def programs(arity=2):
    params = ', '.join(f'%b{i}: hal.buffer' for i in range(arity))
    values = ', '.join(f'%b{i}' for i in range(arity))
    types = ', '.join(['hal.buffer'] * arity)
    return {'control.loom': f'''func.decl import("runner") @execute_{arity}(%stage: i32, {params}) -> (i64)
func.decl import("runner") @feedback(%slot: i32, %source: hal.buffer, %offset: i64, %length: i64) -> (i64)
func.def public @step(%stage: i32, {params}) -> (i64) {{
  %work = func.call @execute_{arity}(%stage, {values}) : (i32, {types}) -> (i64)
  func.return %work : i64
}}
'''}


class RunnerOwnershipUpgradeTests(unittest.TestCase):
    def test_all_observed_arities_migrate_import_and_public_entry(self):
        for arity in (2, 4, 5, 6, 7):
            with self.subTest(arity=arity):
                source = programs(arity)
                changes, counts = adapter.transform(source)
                self.assertEqual(counts['hal_input_types'], 3 * arity + 1)
                self.assertTrue(changes['control.loom'].startswith(adapter.ALIAS + '\n\n'))
                self.assertNotIn('hal.buffer', changes['control.loom'])
                self.assertIn('-> (i64)', changes['control.loom'])
                self.assertEqual(adapter.transform(changes)[0], {})

    def test_forwarding_service_and_local_branch_keep_values_and_flow(self):
        source = programs()
        source['service.loom'] = '''func.decl @step(%stage: i32, %b0: hal.buffer, %b1: hal.buffer) -> (i64)
func.def public @text_step(%stage: i32, %b0: hal.buffer, %b1: hal.buffer) -> (i64) {
  %work = func.call @step(%stage, %b0, %b1) : (i32, hal.buffer, hal.buffer) -> (i64)
  func.return %work : i64
}
'''
        source['control.loom'] = source['control.loom'].replace('  %work =', '''  %choose = scalar.constant true : i1
  %accepted = scf.if %choose -> (hal.buffer) {
    scf.yield %b0 : hal.buffer
  } else {
    scf.yield %b1 : hal.buffer
  }
  %work =''').replace('@execute_2(%stage, %b0, %b1) :', '@execute_2(%stage, %accepted, %b1) :')
        changes, _ = adapter.transform(source)
        self.assertIn('scf.yield %b0 : ptr<@HalBuffer>', changes['control.loom'])
        for path, text in changes.items():
            self.assertEqual(text.removeprefix(adapter.ALIAS + '\n\n').replace(adapter.BORROWED, adapter.HAL), source[path])

    def test_rejects_owned_producers_and_returns(self):
        bad = [
            '\nfunc.decl import("storage") @create() -> (hal.buffer)\n',
            '\nfunc.def @forward(%b: hal.buffer) -> (hal.buffer) {\n  func.return %b : hal.buffer\n}\n',
            '\nfunc.decl import("foreign") @consume(%b: hal.buffer)\n',
        ]
        for extra in bad:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                adapter.transform({'control.loom': programs()['control.loom'] + extra})

    def test_rejects_inline_ownership_body_and_declaration_tail(self):
        for extra in ('func.def @drop(%b: hal.buffer) { object.release %b }',
                      'func.decl @drop(%b: hal.buffer) unexpected'):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, 'inline body'):
                adapter.transform({'control.loom': programs()['control.loom'] + '\n' + extra + '\n'})

    def test_rejects_nested_owning_handle_containers(self):
        for typ in ('view<hal.buffer>', 'ref<hal.buffer>', 'ptr<hal.buffer>', 'tuple<hal.buffer>'):
            source = programs()
            source['control.loom'] += '\nfunc.def @unused(%xs: ' + typ + ') {\n  func.return\n}\n'
            with self.subTest(typ=typ), self.assertRaisesRegex(ValueError, 'nested HAL'):
                adapter.transform(source)

    def test_branch_result_cannot_be_its_own_borrowed_root(self):
        source = programs()
        source['control.loom'] = source['control.loom'].replace('  %work =',
            '  %cond = scalar.constant true : i1\n'
            '  %accepted = scf.if %cond -> (hal.buffer) {\n'
            '    scf.yield %accepted : hal.buffer\n'
            '  } else {\n'
            '    scf.yield %b1 : hal.buffer\n  }\n  %work =')
        with self.assertRaisesRegex(ValueError, 'borrowed root'):
            adapter.transform(source)

    def test_rejects_owned_identity_use_in_already_migrated_source(self):
        source, _ = adapter.transform(programs())
        source['control.loom'] += '\nfunc.decl import("foreign") @own(%x: ref<@HalBuffer>)\n'
        with self.assertRaisesRegex(ValueError, 'non-borrowed canonical'):
            adapter.transform(source)

    def test_rejects_unknown_uses_even_without_type_annotation(self):
        for op in ('object.release %b0', 'global.store %b0 @saved', 'unknown.consume %b0'):
            source = programs()
            source['control.loom'] = source['control.loom'].replace('  func.return', '  ' + op + '\n  func.return')
            with self.subTest(op=op), self.assertRaisesRegex(ValueError, 'ownership operation'):
                adapter.transform(source)

    def test_rejects_unclosed_wrong_arity_and_ownership_forwarding(self):
        for old, new in (('@execute_2(%stage, %b0, %b1) :', '@unknown(%stage, %b0, %b1) :'),
                         ('(i32, hal.buffer, hal.buffer) ->', '(i32, buffer, hal.buffer) ->'),
                         ('@execute_2(%stage, %b0, %b1) :', '@execute_2(%stage, %work, %b1) :')):
            source = programs()
            source['control.loom'] = source['control.loom'].replace(old, new)
            with self.subTest(new=new), self.assertRaises(ValueError):
                adapter.transform(source)

    def test_rejects_hidden_operations_before_and_after_forwarding_call(self):
        for old, new in (('  %work = func.call', '  object.release %b0; %work = func.call'),
                         ('-> (i64)\n  func.return', '-> (i64) object.release %b0\n  func.return')):
            source = programs()
            source['control.loom'] = source['control.loom'].replace(old, new)
            with self.subTest(new=new), self.assertRaises(ValueError):
                adapter.transform(source)

    def test_indented_declarations_receive_same_foreign_and_public_checks(self):
        for extra in ('  func.decl import("foreign") @consume(%b: hal.buffer)',
                      '  func.def public @unknown(%b: hal.buffer) {\n    func.return\n  }'):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                adapter.transform({'control.loom': programs()['control.loom'] + '\n' + extra + '\n'})

    def test_comment_prefix_cannot_hide_foreign_import_or_public_entry(self):
        for extra in ('/* comment */ func.decl import("foreign") @consume(%b: hal.buffer)',
                      '/* comment */ func.def public @unknown(%b: hal.buffer) {\n  func.return\n}'):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                adapter.transform({'control.loom': programs()['control.loom'] + '\n' + extra + '\n'})

    def test_multiline_comments_are_not_executable_ownership_uses(self):
        source = programs()
        source['control.loom'] += '\n/* historical spelling\n hal.buffer and object.release %b0\n */\n'
        changes, _ = adapter.transform(source)
        self.assertIn(' hal.buffer and object.release %b0', changes['control.loom'])
        self.assertEqual(adapter.transform(changes)[0], {})

    def test_rejects_alias_collisions_and_mixed_ownership(self):
        for extra in ('\nfunc.decl @HalBuffer()\n', '\n' + adapter.ALIAS + '\n', '\nfunc.decl @other(%b: ptr<@HalBuffer>)\n'):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                adapter.transform({'control.loom': programs()['control.loom'] + extra})

    def test_preserves_comments_strings_host_buffers_and_unrelated_sources(self):
        source = programs()
        source['control.loom'] += '\n// hal.buffer is the historical spelling\n'
        source['unrelated.loom'] = 'func.def @host(%b: buffer) {\n  func.return\n}\n'
        changes, _ = adapter.transform(source)
        self.assertIn('// hal.buffer is the historical spelling', changes['control.loom'])
        self.assertNotIn('unrelated.loom', changes)

    def test_derivative_keeps_card_lock_weights_and_source_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'source'; root.mkdir()
            (root / 'model.json').write_text(json.dumps({'programs': {'source': [{'path': 'control.loom'}]}}))
            (root / 'control.loom').write_text(programs()['control.loom'])
            (root / 'model.json.lock').write_text('immutable lock')
            weight = Path(tmp) / 'immutable.gguf'; weight.write_bytes(b'weights')
            (root / 'model.gguf').symlink_to(weight)
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            out = Path(tmp) / 'out'
            report, receipt = adapter.write_derivative(root, out)
            self.assertEqual(report['status'], 'applied')
            self.assertEqual(report['changed_file_count'], 1)
            self.assertTrue(receipt.is_file())
            self.assertEqual(adapter.plan(out)[0], {})
            self.assertTrue((out / 'model.gguf').is_symlink())
            for name, raw in before.items():
                self.assertEqual((root / name).read_bytes(), raw)
                if name != 'control.loom':
                    self.assertEqual((out / name).read_bytes(), raw)
            (root / 'payload.gguf').write_bytes(b'no copies')
            with self.assertRaisesRegex(ValueError, 'weights'):
                adapter.plan(root)

    def test_rejects_bytecode_symlink_source_and_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'source'; root.mkdir()
            (root / 'model.json').write_text('{"programs":{"bytecode":{"path":"program.vm"}}}')
            with self.assertRaisesRegex(ValueError, 'bytecode'):
                adapter.plan(root)
            (root / 'model.json').write_text('{}')
            (root / 'control.loom').write_text(programs()['control.loom'])
            with self.assertRaisesRegex(ValueError, 'overlap'):
                adapter.write_derivative(root, root / 'nested')
            (root / 'alias.loom').symlink_to(root / 'control.loom')
            with self.assertRaisesRegex(ValueError, 'linked'):
                adapter.plan(root)


if __name__ == '__main__':
    unittest.main()
