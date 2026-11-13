import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
import runpy
runpy.run_path(str(Path(__file__).resolve().parents[1] / "python/run.py"))["bootstrap"]()
from hrx_systems.cache import seed_suite as BATCH


class SuiteSeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.suite = self.root / 'suite.json'
        sample = BATCH.SEED.read_json(ROOT / 'config/cached-model-tests.json')['entries'][0]
        self.entries = [{**sample, 'id': name,
                         'seed_manifest': f'models/{name}/seed-manifest.json'}
                        for name in ('one', 'two', 'three')]
        self.write_suite(self.entries)

    def write_suite(self, entries):
        self.suite.write_text(json.dumps({'schema_version': 1, 'entries': entries}))

    def invoke(self, *extra):
        return BATCH.main(['--suite', str(self.suite), '--root', str(self.root),
                           '--output', str(self.root / 'output'), *extra])

    def report(self):
        return json.loads((self.root / 'output/seed-suite.json').read_text())

    def test_no_argument_command_uses_existing_suite_and_root_environment(self):
        entries = BATCH.SEED.read_json(ROOT / 'config/cached-model-tests.json')['entries']
        with patch.dict(os.environ, {'HRX_SYSTEMS_ROOT': str(self.root)}), \
                patch.object(BATCH, 'run_child', return_value=0) as child, \
                patch.object(BATCH, 'grant_read') as acl:
            self.assertEqual(BATCH.main([]), 0)
        self.assertEqual(child.call_count, len(entries))
        for call, entry in zip(child.call_args_list, entries):
            command = call.args[0]
            self.assertEqual(command[:2], ['bash', str(ROOT / 'scripts/cache/seed-models.sh')])
            baseline = Path(command[command.index('--baseline') + 1])
            self.assertEqual(json.loads(baseline.read_text())['model_id'], entry['selection']['model_id'])
            self.assertEqual(command[command.index('--seed-manifest') + 1],
                             str(self.root / 'cache' / entry['seed_manifest']))
        acl.assert_not_called()

    def test_failed_child_keeps_all_expected_results_and_continues(self):
        with patch.object(BATCH, 'run_child', side_effect=[0, 7, 0]) as child:
            self.assertEqual(self.invoke(), 1)
        self.assertEqual(child.call_count, 3)
        report = self.report()
        self.assertEqual([e['id'] for e in report['entries']], ['one', 'two', 'three'])
        self.assertEqual([e['seeding']['exit_code'] for e in report['entries']], [0, 7, 0])
        self.assertEqual(report['status'], 'failed')

    def test_rejects_duplicates_and_escaping_references_before_children(self):
        cases = [[], [self.entries[0], self.entries[0]],
                 [self.entries[0], {**self.entries[1], 'seed_manifest': self.entries[0]['seed_manifest']}],
                 [{**self.entries[0], 'seed_manifest': '../outside.json'}],
                 [{**self.entries[0], 'seed_manifest': str(self.root / 'outside.json')}],
                 [{**self.entries[0], 'seed_manifest': '.'}],
                 [{**self.entries[0], 'baseline': '../outside.json'}]]
        for entries in cases:
            with self.subTest(entries=entries):
                self.write_suite(entries)
                with patch.object(BATCH, 'run_child') as child:
                    self.assertEqual(self.invoke(), 1)
                child.assert_not_called()
                self.assertFalse((self.root / 'output').exists())

    def test_manifest_symlink_escape_is_rejected_before_children(self):
        (self.root / 'cache').mkdir()
        (self.root / 'cache/link').symlink_to(self.root / 'outside')
        self.write_suite([{**self.entries[0], 'seed_manifest': 'link/new-manifest.json'}])
        with patch.object(BATCH, 'run_child') as child:
            self.assertEqual(self.invoke(), 1)
        child.assert_not_called()

    def test_offline_and_root_output_overrides_reach_each_single_model_command(self):
        with patch.object(BATCH, 'run_child', return_value=0) as child:
            self.assertEqual(self.invoke('--asset-network', 'offline'), 0)
        for call, entry in zip(child.call_args_list, self.entries):
            command = call.args[0]
            self.assertEqual(command[command.index('--asset-network') + 1], 'offline')
            self.assertEqual(command[command.index('--root') + 1], str(self.root))
            self.assertEqual(command[command.index('--output') + 1], str(self.root / 'output' / entry['id']))

    def test_reader_preflight_rejects_unknown_user_or_missing_setfacl(self):
        with patch.object(BATCH.pwd, 'getpwnam', side_effect=KeyError('unknown')), \
                patch.object(BATCH, 'run_child') as child:
            self.assertEqual(self.invoke('--reader-user', 'unknown'), 1)
        child.assert_not_called()
        with patch.object(BATCH.pwd, 'getpwnam', return_value=type('User', (), {'pw_uid': 12345})()), \
                patch.object(BATCH.shutil, 'which', return_value=None), \
                patch.object(BATCH, 'run_child') as child:
            self.assertEqual(self.invoke('--reader-user', 'fixture-reader'), 1)
        child.assert_not_called()

    def test_reader_acl_only_applied_after_success_and_never_grants_write_execute(self):
        order = []
        def child(command, log):
            path = Path(command[command.index('--seed-manifest') + 1])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}')
            path.chmod(0o600)
            order.append(('seed', str(path)))
            return 0
        def acl(command, **kwargs):
            order.append(('acl', command[-1]))
            self.assertEqual(command, ['/fixture/setfacl', '--no-mask', '-m',
                                       'u:12345:r--,m::r--', '--', command[-1]])
        with patch.object(BATCH.pwd, 'getpwnam', return_value=type('User', (), {'pw_uid': 12345})()), \
                patch.object(BATCH.shutil, 'which', return_value='/fixture/setfacl'), \
                patch.object(BATCH, 'run_child', side_effect=child), \
                patch.object(BATCH.subprocess, 'run', side_effect=acl):
            self.assertEqual(self.invoke('--reader-user', 'fixture-reader'), 0)
        self.assertEqual([kind for kind, _ in order], ['seed', 'acl'] * 3)

    def test_acl_failure_is_recorded_and_does_not_hide_later_entries(self):
        with patch.object(BATCH.pwd, 'getpwnam', return_value=type('User', (), {'pw_uid': 12345})()), \
                patch.object(BATCH.shutil, 'which', return_value='/fixture/setfacl'), \
                patch.object(BATCH, 'run_child', return_value=0), \
                patch.object(BATCH, 'grant_read', side_effect=PermissionError('not owned')):
            self.assertEqual(self.invoke('--reader-user', 'fixture-reader'), 1)
        self.assertTrue(all(e['seeding']['status'] == 'failed' for e in self.report()['entries']))

    def test_interruption_keeps_current_and_remaining_expected_entries(self):
        with patch.object(BATCH, 'run_child', side_effect=[0, KeyboardInterrupt]):
            self.assertEqual(self.invoke(), 130)
        report = self.report()
        self.assertEqual(report['status'], 'interrupted')
        self.assertEqual([e['seeding']['status'] for e in report['entries']], ['ready', 'failed', 'not_run'])

    def test_reader_acl_rejects_symlinks_and_nonowned_manifests(self):
        file = self.root / 'manifest.json'
        file.write_text('{}')
        link = self.root / 'link.json'
        link.symlink_to(file)
        with patch.object(BATCH.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                BATCH.grant_read(link, 12345, '/fixture/setfacl')
            with patch.object(BATCH.os, 'geteuid', return_value=os.geteuid() + 1), self.assertRaises(ValueError):
                BATCH.grant_read(file, 12345, '/fixture/setfacl')
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
