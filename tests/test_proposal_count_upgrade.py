"""Regression coverage of the checked source adaptation and emitted arithmetic."""
import importlib.util
import json
from pathlib import Path
import re
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tools/migrations/upgrade-pipelines-publication-13-to-14.py'
FIXTURE = ROOT / 'tests/fixtures/publication-proposal-counts'
spec = importlib.util.spec_from_file_location('proposal_upgrade', SCRIPT)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


def programs(kind='spec'):
    result = {'control.loom': (FIXTURE / (kind + '.loom')).read_text(),
              'service.loom': (FIXTURE / 'service.loom').read_text()}
    if kind == 'spec':
        result.update({p: (FIXTURE / p).read_text() for p in ('verifier.loom', 'allocation.loom')})
    return result


def evaluate_capture(block, length, speculative):
    """Execute the emitted scalar SSA capture, with LE view.store semantics.

    Inputs here are device metadata, rather than accepted/output counts. This
    deliberately tiny evaluator rejects unknown instructions instead of guessing.
    """
    env = {'%length32': length, '%c1_i32': 1, '%c0_i32': 0, '%spec': speculative}
    memory = bytearray([255] * 44)
    lines = [l.strip() for l in block.splitlines() if '%fixup_' in l]
    for line in lines:
        if line.startswith('view.store'):
            value = env[line.split()[1].rstrip(',')]
            memory[40:44] = struct.pack('<I', value)
            continue
        name, op = line.split(' = ', 1)
        args = re.findall(r'%\w+', op)
        if op.startswith('scalar.subi'):
            env[name] = env[args[0]] - env[args[1]]
        elif op.startswith('scalar.maxsi'):
            env[name] = max(env[args[0]], env[args[1]])
        elif op.startswith('scf.select'):
            env[name] = env[args[1] if env[args[0]] else args[2]]
        else:
            raise AssertionError('unexpected emitted instruction: ' + line)
    return memory[40:44]


class ProposalCountUpgradeTests(unittest.TestCase):
    def test_plain_writes_zero_in_live_span_loop_and_is_idempotent(self):
        original = programs('plain')
        updates, _ = adapter.transform(original)
        body = updates['control.loom']
        self.assertIn('scf.for %i = [%c0 to %length step %c1] {\n    view.store %c0_i32, %fixup_proposals[%i]', body)
        self.assertIn('buffer.view %proposal_counts[%origin] : buffer -> view<[%length]xi32>', body)
        self.assertEqual(adapter.transform({p: updates.get(p, t) for p, t in original.items()})[0], {})
        # No resident-row indexing may replace first-plan ordinal writes.
        self.assertNotIn('%fixup_proposals[%request]', body)

    def test_all_text_sequence_forwarding_sites_receive_final_buffer(self):
        updates, _ = adapter.transform(programs('plain'))
        service = updates['service.loom']
        self.assertEqual(service.count('%proposal_counts: buffer'), 4)
        self.assertEqual(service.count('%progress, %proposal_counts) : ('), 3)
        for _, _, _, b in adapter.functions(service):
            self.assertIn('%progress, %proposal_counts)', b)

    def test_spec_capture_precedes_acceptance_and_ignores_rejection_eos_credit(self):
        updates, _ = adapter.transform(programs())
        kernel = updates['verifier.loom']
        self.assertLess(kernel.index('%fixup_candidates'), kernel.index('%consumed, %emitted, %used'))
        cases = [(4, True, 3, 'rejected immediately'), (4, True, 3, 'EOS first'),
                 (4, True, 3, 'output credit one'), (8, True, 7, 'lookup candidates'),
                 (1, False, 0, 'gated anchor with host credit'),
                 (6, False, 0, 'ordinary prefill'), (1, True, 0, 'anchor only')]
        for length, speculative, expected, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(evaluate_capture(kernel, length, speculative), struct.pack('<I', expected))
        self.assertIn('%fixup_actual = view.load %records[%i, 10]', updates['control.loom'])
        # Plain executes first and overwrites every entry, including when the
        # runtime reuses a buffer after a preceding speculative publication.
        self.assertIn('view.store %c0_i32, %fixup_proposals[%i]', updates['control.loom'])
        self.assertNotIn('%credit', '\n'.join(l for l in kernel.splitlines() if '%fixup_' in l))

    def test_feedback_allocation_and_readback_close_over_same_stride(self):
        original = programs()
        original['verifier.loom'] += '\n%unrelated = index.constant 40 : offset\n'
        updates, _ = adapter.transform(original)
        self.assertIn('%result_stride = scalar.constant 92 : i64', updates['allocation.loom'])
        self.assertIn('%first_bank_stride = scalar.constant 44 : i64', updates['allocation.loom'])
        self.assertIn('%record = scalar.constant 44 : i64', updates['control.loom'])
        self.assertIn('%unrelated = index.constant 40 : offset', updates['verifier.loom'])
        self.assertNotIn('x10xi32>', ''.join(updates.values()))
        self.assertEqual(adapter.transform({p: updates.get(p, t) for p, t in original.items()})[0], {})

    def test_unknown_completed_continuations_are_rejected(self):
        original = programs()
        original['control.loom'] = original['control.loom'].replace('  %advance_at =', '  %unknown = scalar.addi %epoch_count, %epoch_count : i32\n  %advance_at =')
        with self.assertRaisesRegex(ValueError, 'continuation'):
            adapter.transform(original)
        original = programs()
        original['control.loom'] = original['control.loom'].replace('%warm = func.call', '%duplicate = func.call')
        with self.assertRaisesRegex(ValueError, 'continuation'):
            adapter.transform(original)

    def test_unknown_kernel_packet_or_publisher_is_rejected(self):
        for path, old, new in [('verifier.loom', '%length32, %c1_i32', '%length32, %c0_i32'),
                               ('control.loom', '%step = scalar.constant 2', '%step = scalar.constant 3'),
                               ('allocation.loom', 'constant 84', 'constant 80')]:
            original = programs()
            if old not in original[path]:
                old, new = ('%c8_i32 = scalar.constant 8', '%c8_i32 = scalar.constant 7')
            original[path] = original[path].replace(old, new)
            with self.assertRaises(ValueError):
                adapter.transform(original)

    def test_unused_parameter_and_dummy_zero14_are_rejected(self):
        original = programs('plain')
        dummy = {p: adapter.signatures(t) for p, t in original.items()}
        with self.assertRaisesRegex(ValueError, 'unverified'):
            adapter.transform(dummy)
        real, _ = adapter.transform(original)
        real['control.loom'] = real['control.loom'].replace('view.store %c0_i32, %fixup_proposals', 'view.store %c1_i32, %fixup_proposals')
        with self.assertRaisesRegex(ValueError, 'unverified'):
            adapter.transform(real)

    def test_legacy_inserted_control_state_name_is_supported(self):
        original = {p: t.replace('%control_state', '%fixup_epoch_control_state') for p, t in programs('plain').items()}
        changes, _ = adapter.transform(original)
        self.assertIn('%fixup_epoch_control_state: buffer', changes['control.loom'])

    def make_tree(self, root, kind='plain'):
        root.mkdir()
        (root / 'model.json').write_text(json.dumps({'services': {'chat': {'kind': 'text'}}}))
        for p, t in programs(kind).items():
            (root / p).write_text(t)
        return root

    def test_portable_writer_preserves_input_and_rejects_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_tree(root / 'source')
            before = {p.name: p.read_bytes() for p in source.iterdir()}
            report, receipt = adapter.write_derivative(source, root / 'output')
            self.assertEqual(report['status'], 'applied')
            self.assertTrue(receipt.exists())
            self.assertEqual({p.name: p.read_bytes() for p in source.iterdir()}, before)
            self.assertEqual(adapter.plan(root / 'output')[0], {})
            with self.assertRaisesRegex(ValueError, 'overlap'):
                adapter.write_derivative(source, source / 'nested')
            with self.assertRaisesRegex(ValueError, 'already exist'):
                adapter.write_derivative(source, root / 'output')

    def test_bytecode_auxiliary_source_missing_provider_and_weights_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_tree(Path(tmp) / 'source')
            (source / 'model.gguf').write_bytes(b'not weights')
            with self.assertRaisesRegex(ValueError, 'weights'):
                adapter.plan(source)
            (source / 'model.gguf').unlink()
            (source / 'control.loom').unlink()
            (source / 'service.loom').unlink()
            (source / 'weights.loom').write_text('')
            with self.assertRaisesRegex(ValueError, 'provider'):
                adapter.plan(source)
            (source / 'model.json').write_text('{"programs":{"bytecode":{"path":"model.vmfb"}}}')
            with self.assertRaisesRegex(ValueError, 'bytecode-only'):
                adapter.plan(source)

    def test_malformed_forwarding_or_missing_plain_provider_refused(self):
        original = programs('plain')
        original['service.loom'] = original['service.loom'].replace('  func.return', '  %extra = scalar.constant 0 : i32\n  func.return', 1)
        with self.assertRaisesRegex(ValueError, 'forwarding'):
            adapter.transform(original)
        original = programs()
        plain = next((s, e) for n, s, e, _ in adapter.functions(original['control.loom']) if n == 'plain_publish_epoch')
        start, end = plain
        original['control.loom'] = original['control.loom'][:start] + original['control.loom'][end:]
        with self.assertRaisesRegex(ValueError, 'provider closure'):
            adapter.transform(original)

    def test_declared_source_paths_are_bounded_and_provider_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_tree(Path(tmp) / 'source')
            for path, expected in [('../escape.loom', 'inside source'),
                                   ('missing.loom', 'missing declared'),
                                   ('service.loom', 'outside declared')]:
                (source / 'model.json').write_text(json.dumps({'programs': {'source': {'inputs': [{'path': path}]}}}))
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, expected):
                    adapter.plan(source)

    def test_writer_removes_only_owned_receipt_on_publish_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_tree(Path(tmp) / 'source')
            output = Path(tmp) / 'output'
            with patch.object(adapter.os, 'replace', side_effect=OSError('test failure')):
                with self.assertRaises(OSError):
                    adapter.write_derivative(source, output)
            self.assertFalse(output.exists())
            self.assertFalse(output.with_name('output.upgrade.json').exists())


if __name__ == '__main__':
    unittest.main()
