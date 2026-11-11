"""Real file mutation, inode replacement and writer/reader receipt boundaries."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('asset_verification', ROOT / 'scripts/asset-verification.py')
ASSETS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ASSETS)


class AssetVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'weights.gguf'
        self.path.write_bytes(b'bounded immutable test weights')
        self.sha = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.size = self.path.stat().st_size

    def test_published_receipt_avoids_hashing_and_preflight_reads(self):
        receipt = ASSETS.Verifier(full=True).verify(self.path, self.size, self.sha)
        verifier = ASSETS.Verifier({ASSETS.key(receipt): receipt})
        with patch.object(ASSETS.hashlib, 'file_digest', side_effect=AssertionError('unexpected weight read')):
            self.assertEqual(verifier.verify(self.path, self.size, self.sha), receipt)
            verifier.unchanged(self.path, self.size, self.sha, receipt)
        self.assertEqual(verifier.stats['sha256_bytes'], 0)
        self.assertEqual(verifier.stats['receipt_hits'], 2)

    def test_legacy_and_full_mode_hash_once_per_physical_identity(self):
        alias = self.root / 'alias.gguf'
        os.link(self.path, alias)
        receipt = ASSETS.Verifier().verify(self.path, self.size, self.sha)
        for full in (False, True):
            verifier = ASSETS.Verifier({ASSETS.key(receipt): receipt} if full else {}, full=full)
            verifier.verify(self.path, self.size, self.sha)
            verifier.verify(alias, self.size, self.sha)
            self.assertEqual(verifier.stats['sha256_files'], 1)
            self.assertEqual(verifier.stats['sha256_bytes'], self.size)

    def test_same_digest_on_two_distinct_inodes_needs_two_receipts(self):
        other = self.root / 'copy.gguf'
        other.write_bytes(self.path.read_bytes())
        verifier = ASSETS.Verifier()
        first = verifier.verify(self.path, self.size, self.sha)
        second = verifier.verify(other, self.size, self.sha)
        self.assertNotEqual(ASSETS.key(first), ASSETS.key(second))
        self.assertEqual(verifier.stats['sha256_files'], 2)

    def test_same_size_mutation_with_restored_mtime_invalidates_receipt(self):
        receipt = ASSETS.Verifier().verify(self.path, self.size, self.sha)
        before = self.path.stat()
        self.path.write_bytes(b'x' * self.size)
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        verifier = ASSETS.Verifier({ASSETS.key(receipt): receipt})
        with self.assertRaisesRegex(ValueError, 'integrity mismatch'):
            verifier.verify(self.path, self.size, self.sha)
        with self.assertRaisesRegex(ValueError, 'cached asset changed'):
            verifier.unchanged(self.path, self.size, self.sha, receipt)

    def test_replacement_or_mutation_while_hashing_is_rejected(self):
        real_digest = ASSETS.hashlib.file_digest
        for replacement in (False, True):
            self.path.write_bytes(b'w' * self.size)
            expected = hashlib.sha256(self.path.read_bytes()).hexdigest()
            def race(stream, algorithm):
                observed = real_digest(stream, algorithm)
                if replacement:
                    other = self.root / 'replacement'
                    other.write_bytes(b'w' * self.size)
                    os.replace(other, self.path)
                else:
                    self.path.write_bytes(b'z' * self.size)
                return observed
            with patch.object(ASSETS.hashlib, 'file_digest', race), self.assertRaisesRegex(ValueError, 'changed or replaced'):
                ASSETS.Verifier().verify(self.path, self.size, expected)

    def test_malformed_receipt_cannot_enable_fast_path(self):
        receipt = ASSETS.Verifier().verify(self.path, self.size, self.sha)
        malformed = {**receipt, 'stat': {**receipt['stat'], 'mtime_ns': 'not a timestamp'}}
        with self.assertRaisesRegex(ValueError, 'malformed'):
            ASSETS.Verifier({ASSETS.key(receipt): malformed}).verify(self.path, self.size, self.sha)

    def test_adoption_only_publishes_after_all_assets_verify(self):
        cache = self.root / 'cache'
        (cache / 'loom-assets').mkdir(parents=True)
        target = cache / 'loom-assets/model.gguf'
        target.write_bytes(self.path.read_bytes())
        manifest = {'schema_version': 1, 'selection': {'repository': 'loom-fun/fixture',
            'publication_revision': 'a' * 40, 'model_id': 'fixture', 'preset': 'chat', 'variant': 'gguf', 'programs': 'source'},
            'card_sha256': 'b' * 64, 'lock_sha256': 'c' * 64, 'files': [{
                'installation_path': 'model.gguf', 'cache_root': 'loom-assets', 'cache_path': 'model.gguf',
                'roles': ['parameter'], 'origin': 'model.gguf', 'resolved': None, 'verified': True,
                'sha256': self.sha, 'size': self.size}]}
        (cache / 'seed-manifest.json').write_text(json.dumps(manifest))
        result = ASSETS.adopt(self.root)
        self.assertEqual(result['asset_verification']['sha256_files'], 1)
        old = (cache / ASSETS.INDEX_NAME).read_bytes()
        target.write_bytes(b'bad weights')
        with self.assertRaisesRegex(ValueError, 'integrity mismatch'):
            ASSETS.adopt(self.root)
        self.assertEqual((cache / ASSETS.INDEX_NAME).read_bytes(), old)


if __name__ == '__main__':
    unittest.main()
