"""Stat-bound immutable asset receipts; persistent writes belong to cache writers."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading

INDEX_NAME = 'asset-verification.json'
STAT_FIELDS = ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns')


def metadata(information):
    if not stat.S_ISREG(information.st_mode):
        raise ValueError('asset must resolve to a regular file')
    return {key: getattr(information, 'st_' + key) for key in STAT_FIELDS}


def validate(receipt):
    if (not isinstance(receipt, dict) or receipt.get('schema_version') != 1 or
            not isinstance(receipt.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', receipt['sha256']) or
            not isinstance(receipt.get('stat'), dict) or set(receipt['stat']) != set(STAT_FIELDS) or
            any(type(v) is not int for v in receipt['stat'].values()) or receipt['stat']['size'] < 0):
        raise ValueError('malformed asset verification receipt')
    return receipt


def key(receipt):
    validate(receipt)
    return f"{receipt['sha256']}:{receipt['stat']['dev']}:{receipt['stat']['ino']}"


def load_index(path):
    try:
        information = Path(path).lstat()
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(information.st_mode) or information.st_size > 16 * 1024 * 1024:
        raise ValueError('asset receipt index must be a bounded regular file')
    value = json.loads(Path(path).read_text())
    if (not isinstance(value, dict) or value.get('schema_version') != 1 or
            value.get('kind') != 'asset_verification_index' or not isinstance(value.get('receipts'), dict)):
        raise ValueError('malformed asset verification index')
    for identity, receipt in value['receipts'].items():
        if key(receipt) != identity:
            raise ValueError('asset receipt index key disagrees with physical identity')
    return value['receipts']


def write_index(path, receipts):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.asset-verification-', dir=path.parent)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump({'schema_version': 1, 'kind': 'asset_verification_index', 'receipts': receipts}, stream, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def asset_path(cache, item):
    if item['cache_root'] not in ('huggingface', 'loom-assets'):
        raise ValueError('invalid asset cache root')
    relative = Path(item['cache_path'])
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('asset cache path escapes its root')
    root = (Path(cache) / item['cache_root']).resolve(strict=True)
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root):
        raise ValueError('asset cache path escapes its root')
    return path


class Verifier:
    def __init__(self, receipts=None, *, full=False):
        self.receipts, self.full, self.memo = receipts or {}, full, {}
        self.lock = threading.RLock()
        self.stats = {'sha256_files': 0, 'sha256_bytes': 0, 'receipt_hits': 0, 'stat_checks': 0}

    def snapshot(self, path):
        self.stats['stat_checks'] += 1
        return metadata(Path(path).stat())

    def verify(self, path, size, sha256):
        if type(size) is not int or size < 0 or not isinstance(sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', sha256):
            raise ValueError('invalid expected asset identity')
        with self.lock:
            before = self.snapshot(path)
            if before['size'] != size:
                raise ValueError('SHA-256 integrity mismatch: asset size changed')
            candidate = {'schema_version': 1, 'sha256': sha256, 'stat': before}
            identity = key(candidate)
            prior = self.memo.get(identity) or (None if self.full else self.receipts.get(identity))
            if prior is not None:
                validate(prior)
                if prior == candidate:
                    if self.snapshot(path) != before:
                        raise ValueError('asset changed during receipt validation')
                    self.stats['receipt_hits'] += 1
                    self.memo[identity] = candidate
                    return candidate
            with Path(path).open('rb') as stream:
                if metadata(os.fstat(stream.fileno())) != before:
                    raise ValueError('asset replaced before hashing')
                observed = hashlib.file_digest(stream, 'sha256').hexdigest()
                self.stats['sha256_files'] += 1
                self.stats['sha256_bytes'] += size
                if metadata(os.fstat(stream.fileno())) != before or self.snapshot(path) != before:
                    raise ValueError('asset changed or replaced during hashing')
            if observed != sha256:
                raise ValueError('SHA-256 integrity mismatch for cached asset')
            self.memo[identity] = candidate
            return candidate

    def unchanged(self, path, size, sha256, receipt):
        validate(receipt)
        with self.lock:
            current = self.snapshot(path)
            if receipt['sha256'] != sha256 or current['size'] != size or current != receipt['stat']:
                raise ValueError('cached asset changed since preparation; prepare again')
            self.stats['receipt_hits'] += 1
        return receipt


def adopt(root, output=None):
    cache = Path(root).expanduser().resolve(strict=True) / 'cache'
    from ..selection import inventory
    with (cache / '.seed.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        snapshot, candidates = inventory.discover(cache)
        if snapshot['errors'] or not candidates or any(e['conflict'] for e in candidates):
            raise ValueError('inventory has errors, conflicts or no published selections; no receipts changed')
        verifier = Verifier(full=True)
        receipts = load_index(cache / INDEX_NAME)
        for number, entry in enumerate(candidates, 1):
            print(f'[{number}/{len(candidates)}] Verify {entry["selection"]["model_id"]}', flush=True)
            for item in entry['_seed']['files']:
                receipt = verifier.verify(asset_path(cache, item), item['size'], item['sha256'])
                receipts[key(receipt)] = receipt
        write_index(cache / INDEX_NAME, receipts)
        result = {'schema_version': 1, 'status': 'passed', 'selections': len(candidates),
                  'index': str(cache / INDEX_NAME), 'asset_verification': verifier.stats}
        if output:
            Path(output).write_text(json.dumps(result, indent=2) + '\n')
        print(f"Verified {verifier.stats['sha256_files']} physical files / {verifier.stats['sha256_bytes'] / 1e9:.2f} GB; index: {cache / INDEX_NAME}")
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='Verify existing seeded assets once and publish stat-bound receipts; no SDK or downloads.')
    parser.add_argument('--root', type=Path, default=Path(os.environ.get('HRX_SYSTEMS_ROOT', Path.home() / '.local/share/hrx-systems-test')))
    parser.add_argument('--output', type=Path, help='optional new compact result file')
    args = parser.parse_args(argv)
    try:
        if args.output and args.output.exists():
            raise ValueError('--output must be new')
        adopt(args.root, args.output)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'Asset receipt adoption failed: {error}\n')


if __name__ == "__main__":
    raise SystemExit(main())
