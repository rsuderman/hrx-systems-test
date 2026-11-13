"""Compatibility API for pipeline readers, explicit host writers and SDK staging."""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location('hrx_bootstrap', Path(__file__).resolve().parent / '_bootstrap.py')
BOOTSTRAP = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(BOOTSTRAP)

READER = BOOTSTRAP.load_internal('pipelines/cache_reader.py')
WRITER = BOOTSTRAP.load_internal('host/cache_writer.py')
NATIVE = BOOTSTRAP.load_internal('pipelines/native_cache.py')

FIELDS = READER.FIELDS
MAX_MANIFEST_BYTES = READER.MAX_MANIFEST_BYTES
CacheError = READER.CacheError
canonical = READER.canonical
sha = READER.sha
digest = READER.digest
key_for = READER.key_for
fail = READER.fail
relative = READER.relative
validate_coordinates = READER.validate_coordinates
validate_manifest = READER.validate_manifest
read_regular = READER.read_regular
VerifiedEntry = READER.VerifiedEntry
read_entry = READER.read_entry

atomic = WRITER.atomic
make_directory = WRITER.make_directory
writer_lock = WRITER.writer_lock
native_cache = NATIVE.native_cache


def publish(root, manifest, payloads):
    return WRITER.publish(root, manifest, payloads, atomic_write=atomic)
