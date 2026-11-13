"""Bulk publication preserves reader access without doing real acquisition."""
import argparse
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import runpy
runpy.run_path(str(Path(__file__).resolve().parents[1] / "python/run.py"))["bootstrap"]()
from hrx_systems.cache import download_catalog as DOWNLOAD


class DownloadReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.models = [e['model_repository'] for e in
                       json.loads((ROOT / 'config/download-models-under-20gb.json').read_text())['entries'][:2]]
        self.args = argparse.Namespace(root=self.directory / 'root', output=self.directory / 'attempt',
                                       model=self.models, dry_run=False, asset_network='offline', reader_user='reader')
        self.child = self.enterContext(patch.object(DOWNLOAD.SUITE, 'run_child', side_effect=self.publish))
        self.grant = self.enterContext(patch.object(DOWNLOAD.SUITE, 'grant_read'))
        self.account = self.enterContext(patch.object(DOWNLOAD.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=12345)))
        self.which = self.enterContext(patch.object(DOWNLOAD.shutil, 'which', return_value='/fixture/setfacl'))

    def publish(self, command, log):
        manifest = Path(command[command.index('--seed-manifest') + 1])
        manifest.parent.mkdir(parents=True, exist_ok=True)
        replacement = manifest.with_suffix('.replacement')
        replacement.write_text('{}\n')
        replacement.chmod(0o600)
        replacement.replace(manifest)
        return 0

    def progress(self):
        return json.loads((self.args.output / 'progress.json').read_text())

    def test_reader_acl_is_reapplied_after_every_successful_publication(self):
        self.assertEqual(DOWNLOAD.run(self.args), 0)
        first = self.progress()
        self.args.output = self.directory / 'reseed'
        self.assertEqual(DOWNLOAD.run(self.args), 0)
        self.assertEqual(self.child.call_count, 4)
        self.assertEqual(self.grant.call_count, 4)
        for call in self.grant.call_args_list:
            manifest, uid, tool = call.args
            self.assertTrue(manifest.is_file())
            self.assertEqual(uid, 12345)
            self.assertEqual(tool, '/fixture/setfacl')
        self.assertEqual(first['reader_user'], 'reader')
        self.assertTrue(all(e['reader_acl']['status'] == 'passed' for e in self.progress()['entries']))

    def test_no_reader_preserves_existing_behavior(self):
        self.args.reader_user = None
        self.assertEqual(DOWNLOAD.run(self.args), 0)
        self.account.assert_not_called()
        self.which.assert_not_called()
        self.grant.assert_not_called()
        self.assertTrue(all('reader_acl' not in e for e in self.progress()['entries']))

    def test_unknown_account_fails_before_downloading_or_creating_output(self):
        self.account.side_effect = KeyError('reader')
        with self.assertRaisesRegex(ValueError, 'unknown reader account'):
            DOWNLOAD.run(self.args)
        self.child.assert_not_called()
        self.assertFalse(self.args.output.exists())

    def test_missing_acl_tool_fails_before_downloading(self):
        self.which.return_value = None
        with self.assertRaisesRegex(ValueError, 'requires setfacl'):
            DOWNLOAD.run(self.args)
        self.child.assert_not_called()
        self.assertFalse(self.args.output.exists())

    def test_acl_failure_is_reported_and_later_models_are_attempted(self):
        self.grant.side_effect = [PermissionError('fixture denial'), None]
        self.assertEqual(DOWNLOAD.run(self.args), 1)
        report = self.progress()
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(self.child.call_count, 2)
        self.assertEqual(report['entries'][0]['status'], 'failed')
        self.assertEqual(report['entries'][0]['exit_code'], 0)
        self.assertEqual(report['entries'][0]['reader_acl']['status'], 'failed')
        self.assertIn('fixture denial', report['entries'][0]['reader_acl']['reason'])
        self.assertEqual(report['entries'][1]['status'], 'passed')

    def test_failed_seed_never_grants_reader_access(self):
        self.child.side_effect = None
        self.child.return_value = 9
        self.assertEqual(DOWNLOAD.run(self.args), 1)
        self.grant.assert_not_called()
        self.assertTrue(all(e['exit_code'] == 9 for e in self.progress()['entries']))

    def test_reader_dry_run_validates_without_publishing(self):
        self.args.dry_run = True
        self.assertEqual(DOWNLOAD.run(self.args), 0)
        self.account.assert_called_once_with('reader')
        self.which.assert_called_once_with('setfacl')
        self.child.assert_not_called()
        self.grant.assert_not_called()
        self.assertFalse(self.args.output.exists())

    def test_interrupted_acl_does_not_leave_a_passed_model(self):
        self.grant.side_effect = KeyboardInterrupt
        self.assertEqual(DOWNLOAD.run(self.args), 130)
        report = self.progress()
        self.assertEqual(report['status'], 'interrupted')
        self.assertEqual(report['entries'][0]['status'], 'interrupted')
        self.assertEqual(report['entries'][1]['status'], 'not_run')


if __name__ == '__main__':
    unittest.main()
