"""Explicit host-only persistent pipeline publication and writer locks."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import tempfile
from hrx_systems.pipelines import cache_reader as reader

def atomic(path,data):
    fd,name=tempfile.mkstemp(prefix='.seed-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            os.fchmod(stream.fileno(),0o644); stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)

def make_directory(path):
    path=Path(path)
    if path.is_symlink(): reader.fail('malformed_manifest','linked writer directory')
    if not path.exists():
        make_directory(path.parent)
        try: path.mkdir(mode=0o755)
        except FileExistsError: pass
        else: path.chmod(0o755)
    if not path.is_dir(): reader.fail('malformed_manifest','writer directory is not a directory')

@contextmanager
def writer_lock(path):
    """Writer-only lock; public commands use .seed.lock for their whole batch."""
    path=Path(path)
    if path.is_symlink(): reader.fail('malformed_manifest','linked writer lock')
    try:
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW,0o644)
    except FileExistsError:
        fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK)
    else: os.fchmod(fd,0o644)
    with os.fdopen(fd,'a') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): reader.fail('malformed_manifest','writer lock is not regular')
        fcntl.flock(stream,fcntl.LOCK_EX)
        yield

def publish(root, manifest, payloads, *, atomic_write=None):
    """Explicit writer only: repair missing/corrupt blobs, publish manifest last."""
    atomic_write = atomic_write or atomic
    raw=reader.canonical(manifest)+b'\n'; m=reader.validate_manifest(raw)
    root=Path(root)
    for ancestor in [root,*root.absolute().parents]:
        if ancestor.is_symlink(): reader.fail('malformed_manifest','writer refuses linked cache ancestors')
    for path in (root,root/'entries',root/'blobs',root/'blobs/sha256'):
        if path.is_symlink(): reader.fail('malformed_manifest','writer refuses symlinked cache directories')
        make_directory(path)
    with writer_lock(root / '.publish.lock'):
        key=reader.key_for(m['selection'],m['card']['sha256'],m['lock']['sha256'])
        try:
            old=reader.read_entry(root,m['selection'],m['card']['sha256'],m['lock']['sha256'])
        except reader.CacheError as e:
            if e.kind not in ('missing_entry','missing_blob','hash_mismatch','malformed_manifest'): raise
        else:
            # SDK provenance may differ across explicit repeat seeds.
            previous=dict(old.manifest); current=dict(m)
            previous.pop('sdk_identity'); current.pop('sdk_identity')
            if previous!=current: reader.fail('identity_conflict','valid entry conflicts with newly acquired source bytes')
            return old
        for item in [m['card'],m['lock'],*m['sources']]:
            data=payloads[item['sha256']]
            if len(data)!=item['size'] or reader.sha(data)!=item['sha256']: reader.fail('hash_mismatch','writer payload differs from manifest')
            destination=root/'blobs/sha256'/item['sha256']
            if destination.is_symlink(): reader.fail('malformed_manifest','writer refuses linked blobs')
            atomic_write(destination,data)
        atomic_write(root/'entries'/f'{key}.json',raw)
        return reader.read_entry(root,m['selection'],m['card']['sha256'],m['lock']['sha256'])
