import importlib.util
import difflib
import hashlib
import os
import shutil
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/upgrade-pipelines-0.1.0-to-0.2.0.py'
FIXTURES = ROOT / 'tests/fixtures/pipeline-upgrade-0.1.0-to-0.2.0'
spec = importlib.util.spec_from_file_location('fixup', SCRIPT)
fixup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixup)


class FixupTests(unittest.TestCase):
    def test_legacy_minicpm_honors_explicit_no_reasoning_request(self):
        text = (FIXTURES / 'legacy-minicpm-reasoning.loom').read_text()
        result, edits = fixup.migrate_program(text)
        self.assertIn('@fixup_no_thinking(%request)', result)
        self.assertIn('global.load @fixup_generation_plain', result)
        self.assertIn('%thinking = scf.select %disabled, %zero, %one', result)
        self.assertEqual(sum(e.get('kind') == 'reasoning_selection' for e in edits), 1)
        self.assertEqual(fixup.migrate_program(result), (result, []))
        with self.assertRaisesRegex(ValueError, 'reasoning'):
            fixup.migrate_program(text.replace('%thinking = scalar.constant 1', '%thinking = scalar.constant 2'))

    def test_audio_output_recognizes_open_reasoning_marker(self):
        text = (FIXTURES / 'legacy-audio-output.loom').read_text()
        result, edits = fixup.migrate_program(text)
        self.assertIn('@fixup_original_output_next(', result)
        self.assertIn('global.load @think_open', result)
        self.assertIn('scalar.minsi %open_end, %legacy_end', result)
        self.assertEqual(sum(e.get('kind') == 'reasoning_output' for e in edits), 1)
        self.assertEqual(fixup.migrate_program(result), (result, []))
        with self.assertRaisesRegex(ValueError, 'reasoning'):
            fixup.migrate_program(text.replace('%spacing = scalar.cmpi eq', '%spacing = scalar.cmpi ne'))
        other = text.replace('4c464d3320417564696f203242', '6f74686572')
        self.assertEqual(fixup.migrate_program(other), (other, []))

    def test_legacy_decision_batch_handle_keeps_math_and_plan(self):
        root = FIXTURES / 'legacy-decision'
        changes, report = fixup.plan(root)
        control = changes['control.loom'].decode()
        service = changes['service.loom'].decode()
        self.assertIn('func.call @fixup_batch_pack(%batch)', control)
        self.assertIn('func.call @fixup_batch_finish(%batch)', control)
        self.assertIn('@decision_collect_batch_v1(', service)
        self.assertIn('@decision_run_v1(%state: buffer,', service)
        self.assertIn('import("runner") @execute_2(%stage: i32, %input: hal.buffer, %output: hal.buffer)', control)
        self.assertIn('func.return %stage, %payload, %plan : i32, buffer, buffer', control)
        old = (root / 'control.loom').read_text()
        self.assertEqual(control[control.index('  %c0_i64 =', control.index('func.def public @prepare_batch(')):control.index('func.def public @run(')],
                         old[old.index('  %c0_i64 =', old.index('func.def public @prepare_batch(')):old.index('func.def public @run(')])
        self.assertEqual(report['decision_batch_abi_edits'], 2)
        import tempfile,shutil
        with tempfile.TemporaryDirectory() as tmp:
            clone = Path(tmp) / 'model'
            shutil.copytree(root, clone)
            for name,data in changes.items(): (clone / name).write_bytes(data)
            self.assertEqual(fixup.plan(clone)[0], {})

    def test_unknown_or_partial_decision_provider_is_rejected(self):
        import tempfile,shutil
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'model'
            shutil.copytree(FIXTURES / 'legacy-decision', root)
            f = root / 'control.loom'
            f.write_text(f.read_text() + '// changed provider\n')
            with self.assertRaisesRegex(ValueError, 'decision'):
                fixup.plan(root)

    def test_gdn_guard_preserves_history_and_source_bounds(self):
        for path in (FIXTURES / 'legacy-gdn-convolution').glob('*.loom'):
            source = path.read_text()
            result, edits = fixup.migrate_program(source)
            self.assertIn('%fixup_inside_token = index.cmp uge, %token, %back : index', result)
            self.assertIn('%inside = scalar.andi %fixup_inside_rel, %fixup_inside_token : i1', result)
            self.assertIn('%src_v = index.sub %token, %back : index', result)
            self.assertIn('[range(%src_v, 0, 8191), lt(%src_v, %tokens)]', result)
            self.assertEqual(result.split('    } else {', 1)[1], source.split('    } else {', 1)[1])
            self.assertEqual(sum(e.get('kind') == 'gdn_convolution_guard' for e in edits), 1)
            self.assertEqual(fixup.migrate_program(result), (result, []))

    def test_unknown_gdn_helper_requires_manual_review(self):
        source = (FIXTURES / 'legacy-gdn-convolution/minicpm.loom').read_text()
        with self.assertRaisesRegex(ValueError, 'convolution'):
            fixup.migrate_program(source.replace('scalar.siluf<afn>', 'scalar.negf'))

    def test_gdn_pattern_in_comments_and_other_functions_is_untouched(self):
        text = '// %inside = index.cmp uge, %rel, %back : index\n'
        self.assertEqual(fixup.migrate_program(text), (text, []))
        source = (FIXTURES / 'legacy-gdn-convolution/minicpm.loom').read_text()
        commented = '/*\n' + source + '\n*/\n'
        self.assertEqual(fixup.migrate_program(commented), (commented, []))
        source = source.replace('@g_gdn_conv_chan(', '@another_convolution(')
        self.assertEqual(fixup.migrate_program(source), (source, []))

    def test_legacy_weight_key_range_is_distinct_from_tensor_range(self):
        source = ('func.decl import("weights") @input(%source: i32, %key: buffer, %offset: i64, %length: i64, %alignment: i64) -> (i64)\n'
                  'func.def public @capture(%source: i32, %tensor_name: buffer, %file_offset: i64, %data_size: i64, %align: i64) -> (i64) {\n'
                  '  %result = func.call @input(%source, %tensor_name, %file_offset, %data_size, %align) : (i32, buffer, i64, i64, i64) -> (i64) // keep data range\n'
                  '  func.return %result : i64\n}\n')
        result, edits = fixup.migrate_program(source)
        self.assertIn('%key_offset: i64, %key_length: i64, %offset: i64', result)
        self.assertIn('buffer.length %tensor_name', result)
        self.assertIn('scalar.constant 0 : i64', result)
        self.assertIn('%fixup_weight_key_offset_0, %fixup_weight_key_length_0, %file_offset, %data_size, %align)', result)
        self.assertIn('// keep data range', result)
        self.assertTrue(any(e.get('kind') == 'weight_key_range' for e in edits))
        self.assertEqual(fixup.migrate_program(result), (result, []))

    def test_legacy_weight_parameter_range_and_result_are_preserved(self):
        source = ('func.decl import("weights") @parameter(%source: i32, %key: buffer) -> (buffer, i64)\n'
                  '%metadata, %size = func.call @parameter(%file, %name) : (i32, buffer) -> (buffer, i64)\n')
        result, _ = fixup.migrate_program(source)
        self.assertIn('@parameter(%file, %name, %fixup_weight_key_offset_0, %fixup_weight_key_length_0)', result)
        self.assertIn('-> (buffer, i64)', result)
        self.assertIn('%metadata, %size =', result)

    def test_weight_rewrite_requires_import_and_recognized_calls(self):
        local = '%a = func.call @input(%s, %k, %o, %n, %a) : (i32, buffer, i64, i64, i64) -> (i64)\n'
        self.assertEqual(fixup.migrate_program(local), (local, []))
        decl = 'func.decl import("weights") @input(%source: i32, %key: buffer, %offset: i64, %length: i64, %alignment: i64) -> (i64)\n'
        with self.assertRaisesRegex(ValueError, 'weight'):
            fixup.migrate_program(decl + '%r = func.call @input(%s, %k) : (i32, buffer) -> (i64)\n')
        other = decl.replace('"weights"', '"another-module"') + local
        self.assertEqual(fixup.migrate_program(other), (other, []))

    def test_flint_template_keeps_its_original_suffix_behavior(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'model'
            shutil.copytree(FIXTURES / 'legacy-text-service', root)
            p = root / 'control.loom'
            old = ('  %body = func.call @join(%prefix, %content) : (buffer, buffer) -> (buffer)\n'
                   '  %suffix = global.load @message_suffix : buffer\n'
                   '  %fragment = func.call @join(%body, %suffix) : (buffer, buffer) -> (buffer)')
            p.write_text(p.read_text().replace(old, '  %fragment = func.call @join(%prefix, %content) : (buffer, buffer) -> (buffer)'))
            changes, _ = fixup.plan(root)
            self.assertNotIn('@message_suffix', changes['control.loom'].decode())
            self.assertIn('%fixup_no_frames', changes['control.loom'].decode())

    def test_incomplete_wrapper_closure_fails_before_rewriting(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'model'
            shutil.copytree(FIXTURES / 'legacy-text-service', root)
            p = root / 'service.loom'
            p.write_text(p.read_text().replace('@text_chat_message_v1(', '@custom_chat_message_v1('))
            with self.assertRaisesRegex(ValueError, 'provider closure'):
                fixup.plan(root)

    def test_legacy_service_provider_and_wrappers_migrate_together(self):
        root = FIXTURES / 'legacy-text-service'
        changes, report = fixup.plan(root)
        provider = changes['control.loom'].decode()
        wrappers = changes['service.loom'].decode()
        self.assertIn('%parts: buffer, %frames: buffer, %reasoning: buffer', provider)
        self.assertIn('%fixup_no_frames', provider)
        self.assertIn('func.return %fragment, %state, %empty, %c0_i64 : buffer, i64, buffer, i64', provider)
        self.assertIn('@parse_tools(%schemas: buffer, %response: buffer, %truncated: i64)', provider)
        self.assertIn('@text_chat_message_v1(%raw: buffer, %role: buffer, %content: buffer, %parts: buffer, %frames: buffer', wrappers)
        self.assertIn('%result_3 = func.call @chat_message(', wrappers)
        self.assertIn('@text_parse_tools_v1(%schemas: buffer, %response: buffer, %truncated: i64)', wrappers)
        self.assertIn('@parse_tools(%schemas, %response, %truncated) : (buffer, buffer, i64)', wrappers)

    def test_unrecognized_media_provider_requires_manual_migration(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'model'
            shutil.copytree(FIXTURES / 'legacy-text-service', root)
            p = root / 'control.loom'
            p.write_text(p.read_text().replace('  func.call @check(%no_parts) : (i1)\n', ''))
            with self.assertRaisesRegex(ValueError, 'provider'):
                fixup.plan(root)

    def test_legacy_service_migration_is_idempotent(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'model'
            shutil.copytree(FIXTURES / 'legacy-text-service', root)
            changes, _ = fixup.plan(root)
            self.assertTrue(changes)
            for relative, data in changes.items():
                (root / relative).write_bytes(data)
            self.assertFalse(fixup.plan(root)[0])

    def test_source_pool_bound_is_preserved_and_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'model.json').write_text('{"schema_version":1}')
            source = 'config.decl @llm.pool : %value: index where [range(%value, 16, 1048576)]\n'
            (root / 'program.loom').write_text(source)
            changes, report = fixup.plan(root)
            self.assertFalse(changes)
            self.assertEqual(report['source_pool_constraints'][0]['maximum'], 1048576)
            self.assertEqual((root / 'program.loom').read_text(), source)

    def test_command_rename_preserves_abi_and_excludes_noncode(self):
        source = ('command.program.def public @run() launch(%x: buffer) {\n'
                  '  command.program.launch @child(%x) : (buffer)\n'
                  '  command.return\n}\n'
                  'command.program.decl @child() launch(%x: buffer)\n'
                  '// command.program.def ignored\n'
                  '/*\ncommand.program.launch ignored\n*/\n'
                  'func.def @command.program.def() {}\n'
                  '%command.program.def = scalar.constant 1 : i32\n')
        result, edits = fixup.migrate_program(source)
        expected = source.replace('command.program.def public', 'command.def public').replace(
            'command.program.launch @child', 'command.launch @child').replace(
            'command.program.decl @child', 'command.decl @child')
        self.assertEqual(result, expected)
        self.assertEqual(len(edits), 3)
        self.assertEqual(fixup.migrate_program(result), (result, []))

    def test_unknown_command_operation_requires_review(self):
        with self.assertRaises(ValueError):
            fixup.migrate_program('command.program.unknown @run()')

    def test_typed_literals_preserve_values_types_and_comments(self):
        source = ('// vector.constant must remain in this comment\n'
                  '/* vector.constant 2 : vector<4xi32> */\n'
                  '  %mask = vector.constant 252645135 : vector<8xi32> // mask\n'
                  '  %zero = vector.constant -0.0 : vector<16xbf16>\n'
                  '  %ix = vector.constant 16 : vector<4xindex>\n'
                  '  %runtime = vector.splat %value : vector<8xi32>\n')
        result, edits = fixup.migrate_program(source)
        self.assertIn('%mask = vector.splati 252645135 : vector<8xi32> // mask', result)
        self.assertIn('%zero = vector.splatf -0.0 : vector<16xbf16>', result)
        self.assertIn('%ix = vector.splati 16 : vector<4xindex>', result)
        self.assertEqual(result.splitlines()[:2], source.splitlines()[:2])
        self.assertEqual(result.splitlines()[-1], source.splitlines()[-1])
        self.assertEqual(len(edits), 3)
        self.assertEqual(fixup.migrate_program(result), (result, []))

    def test_strings_and_multiline_comments_are_not_code(self):
        source = ('/*\n%a = vector.constant 0 : vector<4xi32>\n*/\n'
                  '%s = some.op "vector.constant \\" text"\n')
        self.assertEqual(fixup.migrate_program(source), (source, []))

    def test_ambiguous_or_unsupported_syntax_fails(self):
        for source in ('%a = vector.constant %value : vector<4xi32>',
                       '%a = vector.constant 0 : vector<4xcomplex>',
                       '%a = vector.constant [1, 2] : vector<2xi32>',
                       '%a = vector.constant 0.0 : vector<4xi32>'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                fixup.migrate_program(source)

    def test_card_changes_only_public_preset_field(self):
        card = {'schema_version': 1, 'presets': {'chat': {'execution': {
            'pool_capacity': 98304, 'context_capacity': 32768, 'rows': 4}}},
            'config': {'llm.pool_capacity': 42}}
        result, edits = fixup.migrate_card(json.dumps(card))
        expected = json.loads(json.dumps(card))
        del expected['presets']['chat']['execution']['pool_capacity']
        self.assertEqual(json.loads(result), expected)
        self.assertEqual(len(edits), 1)
        self.assertEqual(card['presets']['chat']['execution']['pool_capacity'], 98304)

    def test_output_is_separate_audited_and_does_not_copy_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            source.mkdir()
            card = source / 'model.json'
            card.write_text('{"schema_version":1,"presets":{"chat":{"execution":{"pool_capacity":64}}}}')
            code = source / 'program.loom'
            code.write_text('%a = vector.constant 15 : vector<8xi32>\n')
            (source / 'asset.gguf').symlink_to('/model-cache/immutable-model.gguf')
            before = {p.name: p.read_bytes() for p in (card, code)}
            output = root / 'output'
            run = subprocess.run([sys.executable, str(SCRIPT), str(source), '--output', str(output)], capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(before, {p.name: p.read_bytes() for p in (card, code)})
            self.assertEqual((output / 'asset.gguf').readlink(), (source / 'asset.gguf').readlink())
            report = json.loads((root / 'output.upgrade.json').read_text())
            self.assertEqual(report['vector_operations_rewritten'], 1)
            self.assertEqual(report['pool_fields_removed'], 1)
            self.assertFalse(report['publication_ready'])
            retry = subprocess.run([sys.executable, str(SCRIPT), str(source), '--output', str(output)], capture_output=True)
            self.assertNotEqual(retry.returncode, 0)

    def test_failure_leaves_no_output_or_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            source.mkdir()
            (source / 'model.json').write_text('{"schema_version":1}')
            (source / 'bad.loom').write_text('%a = vector.constant %value : vector<4xi32>')
            run = subprocess.run([sys.executable, str(SCRIPT), str(source), '--output', str(root / 'output')], capture_output=True)
            self.assertNotEqual(run.returncode, 0)
            self.assertFalse((root / 'output').exists())
            self.assertFalse((root / 'output.upgrade.json').exists())


class UpgradeReceiptTests(unittest.TestCase):
    def model(self, root):
        root.mkdir()
        (root / 'model.json').write_text('{"schema_version":1}')
        (root / 'model.json.lock').write_text('{"original_published_lock":true}\n')
        (root / 'program.loom').write_text('%mask = vector.constant 1 : vector<2xi32>\n')
        return root

    def test_receipt_identifies_migration_implementation_and_preserved_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.model(Path(directory) / 'source')
            before = {p.name:p.read_bytes() for p in root.iterdir()}
            changes, report = fixup.plan(root)
            self.assertEqual(report['migration_id'], 'hrx-pipelines-0.1.0-to-0.2.0')
            self.assertEqual(report['implementation_revision'], 1)
            self.assertEqual(report['implementation_sha256'], hashlib.sha256(SCRIPT.read_bytes()).hexdigest())
            self.assertEqual(report['status'], 'applied')
            self.assertEqual(report['operation_counts']['vector_literal'], 1)
            original = {p['path']:p for p in report['source_files']}
            output = {p['path']:p for p in report['output_files']}
            self.assertTrue(all(not Path(name).is_absolute() for name in original))
            for name,data in before.items():
                self.assertEqual(original[name]['sha256'], hashlib.sha256(data).hexdigest())
                self.assertEqual(output[name]['sha256'], hashlib.sha256(changes.get(name,data)).hexdigest())
            self.assertEqual(original['model.json.lock'], output['model.json.lock'])
            self.assertFalse(report['locks_modified'])
            self.assertFalse(report['publication_ready'])

    def test_check_is_write_free_and_already_compatible_source_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.model(Path(directory) / 'source')
            (root / 'program.loom').write_text('%mask = vector.splati 1 : vector<2xi32>\n')
            before = {p.name:p.read_bytes() for p in root.iterdir()}
            command = subprocess.run([sys.executable,str(SCRIPT),str(root),'--check'],capture_output=True,text=True)
            self.assertEqual(command.returncode,0,command.stderr)
            report = json.loads(command.stdout)
            self.assertEqual(report['status'],'unchanged')
            self.assertEqual(report['source_files'],report['output_files'])
            self.assertEqual(before,{p.name:p.read_bytes() for p in root.iterdir()})
            self.assertEqual(list(Path(directory).iterdir()),[root])

    def test_regular_weight_payload_cannot_be_copied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.model(Path(directory) / 'source')
            (root / 'weights.GGUF').write_bytes(b'model asset fixture')
            with self.assertRaisesRegex(ValueError,'refusing to copy'):
                fixup.write_derivative(root,Path(directory)/'output')
            self.assertFalse((Path(directory)/'output').exists())
            self.assertFalse((Path(directory)/'output.upgrade.json').exists())

    def test_source_symlinks_and_overlapping_output_require_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.model(Path(directory)/'source')
            with self.assertRaisesRegex(ValueError,'overlap'):
                fixup.write_derivative(root,root/'nested-output')
            other = Path(directory)/'linked.loom'
            other.write_text('%mask = vector.constant 1 : vector<2xi32>\n')
            (root/'program.loom').unlink()
            (root/'program.loom').symlink_to(other)
            with self.assertRaisesRegex(ValueError,'linked'):
                fixup.plan(root)

    def test_declared_source_directory_links_require_review_even_when_unresolved(self):
        for missing in (False,True):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                root=Path(directory)/'source';root.mkdir()
                (root/'model.json').write_text(json.dumps({'schema_version':1,'programs':{'source':{'inputs':[{'path':'program/a.loom'}]}}}))
                outside=Path(directory)/'outside'
                if not missing:
                    outside.mkdir();(outside/'a.loom').write_text('%value = vector.constant 1 : vector<2xi32>\n')
                (root/'program').symlink_to(outside,target_is_directory=True)
                output=Path(directory)/'output'
                with self.assertRaisesRegex(ValueError,'linked'):
                    fixup.write_derivative(root,output)
                self.assertFalse(output.exists())
                self.assertFalse((Path(directory)/'output.upgrade.json').exists())

    def test_existing_report_is_preserved_and_failed_publish_leaves_no_derivative(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.model(Path(directory)/'source');output=Path(directory)/'output'
            report=Path(directory)/'output.upgrade.json';report.write_text('existing evidence')
            with self.assertRaisesRegex(ValueError,'already exist'):
                fixup.write_derivative(root,output)
            self.assertEqual(report.read_text(),'existing evidence')
            self.assertFalse(output.exists());report.unlink()
            with patch.object(fixup.os,'replace',side_effect=OSError('fixture publication failure')):
                with self.assertRaisesRegex(OSError,'publication failure'):
                    fixup.write_derivative(root,output)
            self.assertFalse(output.exists());self.assertFalse(report.exists())
            self.assertEqual(list(Path(directory).iterdir()),[root])

    def test_copied_source_must_match_its_recorded_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root=self.model(Path(directory)/'source');output=Path(directory)/'output'
            real_copy=shutil.copytree
            def altered_copy(source,destination,**kwargs):
                result=real_copy(source,destination,**kwargs)
                (Path(destination)/'model.json.lock').write_text('changed after planning')
                return result
            with patch.object(fixup.shutil,'copytree',side_effect=altered_copy):
                with self.assertRaisesRegex(ValueError,'source changed'):
                    fixup.write_derivative(root,output)
            self.assertFalse(output.exists());self.assertFalse((Path(directory)/'output.upgrade.json').exists())

    def test_large_diff_and_edit_list_are_bounded_without_losing_full_identities(self):
        with tempfile.TemporaryDirectory() as directory:
            root=self.model(Path(directory)/'source')
            before=''.join(f'%mask_{n} = vector.constant 1 : vector<2xi32>\n' for n in range(3000))
            (root/'program.loom').write_text(before)
            changes,report=fixup.plan(root)
            record=report['files'][0]
            self.assertTrue(record['diff_truncated'])
            self.assertLessEqual(len(record['diff'].encode()),fixup.MAX_DIFF_BYTES)
            self.assertTrue(record['edits_truncated'])
            self.assertEqual(record['edit_count'],3000)
            self.assertEqual(len(record['edits']),fixup.MAX_RECORDED_EDITS)
            self.assertEqual(report['operation_counts']['vector_literal'],3000)
            self.assertEqual(record['after_sha256'],hashlib.sha256(changes['program.loom']).hexdigest())
            full=''.join(difflib.unified_diff(before.splitlines(True),changes['program.loom'].decode().splitlines(True),'a/program.loom','b/program.loom'))
            self.assertEqual(record['diff_sha256'],hashlib.sha256(full.encode()).hexdigest())


@unittest.skipUnless(os.environ.get('LOOM_TEST'), 'set LOOM_TEST to a selected SDK loom-test executable')
class NativeUpgradeTests(unittest.TestCase):
    def run_native(self, text, *, succeeds=True):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'case.loom';source.write_text(text)
            result=subprocess.run([os.environ['LOOM_TEST'],str(source)],capture_output=True,text=True)
        if succeeds:
            self.assertEqual(result.returncode,0,result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode,0,'legacy fixture unexpectedly passed')
        report=json.loads(result.stdout)
        self.assertEqual(report['planning_issue_count'],0)
        if succeeds:self.assertEqual(report['failed_sample_count'],0)
        else:self.assertGreater(report['failed_sample_count'],0)
        return report

    def parser_cases(self, base):
        cases=[(prefix,0,0,0,(1,0,0,0)) for prefix in ('','<','<t','<th','<thi','<thin','<think')]
        cases += [('<think>',0,0,0,(0,0,7,1)),('<think>x</think>\n4',0,0,1,(0,0,7,1)),
                  ('4',0,0,1,(1,0,1,0)),('hello <think>x',0,0,0,(1,0,6,0)),
                  ('hello <think>x',6,0,0,(0,6,13,1)),('x</think>\n4',0,1,0,(2,0,1,1)),
                  ('x</think>\n4',1,1,0,(0,1,9,2)),('x</think>\n4',9,2,1,(1,10,11,0)),
                  ('<think>x</thi',7,1,0,(2,7,8,1)),('<think>x</thi',8,1,0,(2,8,8,1)),
                  ('<think>x</thi',8,1,1,(2,8,13,1)),('<thi',0,0,1,(1,0,4,0)),
                  ('answer<tool_call>',0,0,0,(1,0,6,0)),('\n\t',0,2,0,(1,2,2,2))]
        parts=[base]
        for n,(text,previous,state,phase,expected) in enumerate(cases):
            parts.append(f'''func.def @probe_{n}() -> (i64, i64, i64, i64) {{
  %text = buffer.constant utf8({json.dumps(text)}) : buffer
  %previous = scalar.constant {previous} : i64
  %state = scalar.constant {state} : i64
  %phase = scalar.constant {phase} : i32
  %a, %b, %c, %d = func.call @output_next(%text, %previous, %state, %phase) : (buffer, i64, i64, i32) -> (i64, i64, i64, i64)
  func.return %a, %b, %c, %d : i64, i64, i64, i64
}}
check.case public @boundary_{n} {{
  %a, %b, %c, %d = func.call @probe_{n}() : () -> (i64, i64, i64, i64)''')
            for name,value in zip('abcd',expected):
                parts.append(f'  %expected_{name} = check.literal value({value}) : i64\n  check.expect.equal actual(%{name}) expected(%expected_{name}) : i64')
            parts.append('  check.return\n}')
        return '\n'.join(parts)+'\n'

    def test_audio_parser_boundaries_fail_before_and_pass_after_upgrade(self):
        legacy=(FIXTURES/'legacy-audio-output.loom').read_text()
        self.run_native(self.parser_cases(legacy),succeeds=False)
        upgraded=fixup.migrate_program(legacy)[0]
        report=self.run_native(self.parser_cases(upgraded))
        self.assertEqual(report['case_count'],21)

    def prompt_cases(self, base):
        parts=[line for line in base.splitlines() if line.startswith('global.rodata.def')]
        parts += [fixup.function_block(base,'chat_end')]
        for n,(state,thinking,prompt) in enumerate([(0,1,'<|im_start|>assistant\n<think>\n'),
                                                 (1,0,'<|im_start|>assistant\n<think>\n\n</think>\n\n')]):
            parts.append(f'''func.def @probe_{n}() -> (i1, i64) {{
  %state = scalar.constant {state} : i64
  %prompt, %thinking = func.call @chat_end(%state) : (i64) -> (buffer, i64)
  %expected = buffer.constant utf8({json.dumps(prompt)}) : buffer
  %zero = index.constant 0 : offset
  %length = buffer.length %prompt
  %wanted = buffer.length %expected
  %same_length = index.cmp eq, %length, %wanted : offset
  %same = scf.if %same_length -> (i1) {{
    %order = buffer.compare %prompt[%zero], %expected[%zero], %wanted
    %zero32 = scalar.constant 0 : i32
    %same_bytes = scalar.cmpi eq, %order, %zero32 : i32
    scf.yield %same_bytes : i1
  }} else {{
    %false = scalar.constant false : i1
    scf.yield %false : i1
  }}
  func.return %same, %thinking : i1, i64
}}
check.case public @prompt_{n} {{
  %same, %thinking = func.call @probe_{n}() : () -> (i1, i64)
  %yes = check.literal value(true) : i1
  %expected = check.literal value({thinking}) : i64
  check.expect.equal actual(%same) expected(%yes) : i1
  check.expect.equal actual(%thinking) expected(%expected) : i64
  check.return
}}''')
        return '\n\n'.join(parts)+'\n'

    def test_minicpm_default_and_explicit_none_prompts_fail_before_and_pass_after_upgrade(self):
        legacy=(FIXTURES/'legacy-minicpm-reasoning.loom').read_text()
        self.run_native(self.prompt_cases(legacy),succeeds=False)
        report=self.run_native(self.prompt_cases(fixup.migrate_program(legacy)[0]))
        self.assertEqual(report['case_count'],2)


if __name__ == '__main__':
    unittest.main()
