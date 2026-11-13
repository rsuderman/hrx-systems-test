"""SDK-neutral original pipeline storage; readers never create or repair cache files."""
import dataclasses
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import threading

FIELDS = ('repository', 'publication_revision', 'model_id', 'preset', 'variant', 'programs')
MAX_MANIFEST_BYTES = 4 * 1024 * 1024

class CacheError(ValueError):
    def __init__(self, kind, message):
        self.kind = kind
        super().__init__(message)

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()

def sha(data):
    return hashlib.sha256(data).hexdigest()

def digest(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value)

def key_for(selection, card_sha256, lock_sha256):
    return sha(canonical({'selection': {k: selection[k] for k in FIELDS},
                          'card_sha256': card_sha256, 'lock_sha256': lock_sha256}))

def fail(kind, message):
    raise CacheError(kind, message)

def relative(value):
    if (not isinstance(value, str) or not value or '\\' in value or
            value.startswith('/') or any(p in ('', '.', '..') for p in value.split('/'))):
        fail('malformed_manifest', 'invalid relative installation path')
    return value

def validate_coordinates(source, manifest):
    origin=source['origin']
    authority=source['identity_authority']
    def descriptor(value):
        if not isinstance(value,dict): raise ValueError('invalid source descriptor')
        kind=value.get('kind')
        if kind=='inline':
            if not isinstance(value.get('base64'),str): raise ValueError('invalid inline source')
        elif kind=='hf':
            revision=value.get('revision','')
            resolved=source['resolved'] or {}
            if not re.fullmatch('[0-9a-f]{40}',resolved.get('revision',revision)):
                raise ValueError('unresolved Hugging Face source')
            relative(value['path'])
        elif kind=='url':
            url=value.get('url','')
            if not url.startswith('https://') or '?' in url or '#' in url or '@' in url.split('/')[2]:
                raise ValueError('invalid remote source URL')
            if url.startswith('https://huggingface.co/'):
                if not re.search('/resolve/[0-9a-f]{40}/',url): raise ValueError('mutable Hugging Face URL')
            elif authority!='published_lock': raise ValueError('external URL lacks published identity')
        else: raise ValueError('unsupported source kind')
    if isinstance(origin,str):
        if origin.startswith('@'):
            name,path=origin[1:].split('/',1); relative(path)
            record=manifest['origin_roots'][name]
            requested=record['requested']
            if requested.get('kind')=='hf':
                revision=record.get('resolved',{}).get('revision',requested.get('revision',''))
                if not re.fullmatch('[0-9a-f]{40}',revision): raise ValueError('unresolved origin root')
            else: descriptor({**requested,'url':requested.get('url','').rstrip('/')+'/'+path})
        elif '://' in origin: descriptor({'kind':'url','url':origin})
        else: relative(origin)
    else: descriptor(origin)

def validate_manifest(raw, selection=None, card_sha256=None, lock_sha256=None):
    """Validate captured manifest bytes without consulting any cache or SDK."""
    try:
        if len(raw) > MAX_MANIFEST_BYTES:
            fail('malformed_manifest', 'entry exceeds size limit')
        def unique(pairs):
            result = {}
            for k,v in pairs:
                if k in result: fail('malformed_manifest', 'duplicate JSON key')
                result[k] = v
            return result
        m = json.loads(raw, object_pairs_hook=unique)
        if m['schema_version'] != 1: raise ValueError('unsupported schema')
        s = m['selection']
        if set(s) != set(FIELDS) or any(not isinstance(s[k],str) or not s[k] for k in FIELDS):
            raise ValueError('invalid selection')
        if not re.fullmatch('[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', s['repository']): raise ValueError('invalid repository')
        if not re.fullmatch('[0-9a-f]{40}', s['publication_revision']): raise ValueError('mutable publication')
        base = f"https://huggingface.co/{s['repository']}/resolve/{s['publication_revision']}/model.json"
        for name, location in [('card',base),('lock',base+'.lock')]:
            d=m[name]
            if d['location'] != location or not digest(d['sha256']) or type(d['size']) is not int or d['size'] < 0:
                raise ValueError('invalid document identity')
        if selection is not None and s != {k:selection[k] for k in FIELDS}: fail('seed_mismatch','entry selection differs from seed')
        for name,expected in [('card',card_sha256),('lock',lock_sha256)]:
            if expected is not None and m[name]['sha256'] != expected: fail('seed_mismatch','entry document differs from seed')
        if not isinstance(m['sources'],list) or not m['sources']: raise ValueError('empty source closure')
        seen=set()
        for source in m['sources']:
            p=relative(source['installation_path'])
            if p in seen: raise ValueError('duplicate installation path')
            seen.add(p)
            if not isinstance(source['roles'],list) or not source['roles'] or not set(source['roles']) <= {'program','auxiliary'}: raise ValueError('non-code payload')
            if not digest(source['sha256']) or type(source['size']) is not int or source['size'] < 0: raise ValueError('invalid source identity')
            if source['identity_authority'] not in ('published_lock','host_seed'): raise ValueError('unknown source authority')
            if not isinstance(source['origin'],(str,dict)) or not isinstance(source['resolved'],(dict,type(None))): raise ValueError('invalid source coordinates')
            validate_coordinates(source,m)
        if not isinstance(m['origin_roots'],dict) or not isinstance(m['sdk_identity'],dict): raise ValueError('invalid provenance')
        return m
    except CacheError: raise
    except (ValueError,KeyError,TypeError,UnicodeError) as e:
        fail('malformed_manifest',str(e))

def read_regular(root, relative_path, limit=None):
    """Open every path component without following symlinks, including cache root."""
    path=Path(root)
    try:
        # Reject links in ancestor components as well as the final root.
        for component in [*reversed(path.absolute().parents),path.absolute()]:
            if component.is_symlink(): fail('malformed_manifest','symlinked cache path')
        fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            parts=PurePosixPath(relative_path).parts
            for part in parts[:-1]:
                nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                os.close(fd); fd=nxt
            filefd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
            with os.fdopen(filefd,'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): fail('malformed_manifest','cache object is not regular')
                data=stream.read() if limit is None else stream.read(limit+1)
                if limit is not None and len(data)>limit: fail('hash_mismatch' if relative_path.startswith('blobs/') else 'malformed_manifest','cache object exceeds bound')
                return data
        finally: os.close(fd)
    except CacheError: raise
    except FileNotFoundError: fail('missing_blob' if relative_path.startswith('blobs/') else 'missing_entry','required pipeline cache object is absent')
    except OSError as e: fail('unreadable_entry',f'cannot read pipeline cache object: {e.strerror}')

@dataclasses.dataclass(frozen=True)
class VerifiedEntry:
    root: Path
    key: str
    manifest: dict
    raw_manifest: bytes
    manifest_sha256: str
    def bytes(self,item):
        data=read_regular(self.root,'blobs/sha256/'+item['sha256'],item['size'])
        if len(data)!=item['size'] or sha(data)!=item['sha256']: fail('hash_mismatch','pipeline blob identity mismatch')
        return data

def read_entry(root, selection, card_sha256, lock_sha256):
    root=Path(root)
    if not root.exists(): fail('missing_root','pipeline cache root is absent; run host seed-pipelines.sh')
    key=key_for(selection,card_sha256,lock_sha256)
    raw=read_regular(root,'entries/'+key+'.json',MAX_MANIFEST_BYTES)
    m=validate_manifest(raw,selection,card_sha256,lock_sha256)
    entry=VerifiedEntry(root,key,m,raw,sha(raw))
    for item in [m['card'],m['lock'],*m['sources']]: entry.bytes(item)
    return entry

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
    if path.is_symlink(): fail('malformed_manifest','linked writer directory')
    if not path.exists():
        make_directory(path.parent)
        try: path.mkdir(mode=0o755)
        except FileExistsError: pass
        else: path.chmod(0o755)
    if not path.is_dir(): fail('malformed_manifest','writer directory is not a directory')

@contextmanager
def writer_lock(path):
    """Writer-only lock; public commands use .seed.lock for their whole batch."""
    path=Path(path)
    if path.is_symlink(): fail('malformed_manifest','linked writer lock')
    try:
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW,0o644)
    except FileExistsError:
        fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK)
    else: os.fchmod(fd,0o644)
    with os.fdopen(fd,'a') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): fail('malformed_manifest','writer lock is not regular')
        fcntl.flock(stream,fcntl.LOCK_EX)
        yield

def publish(root, manifest, payloads):
    """Explicit writer only: repair missing/corrupt blobs, publish manifest last."""
    raw=canonical(manifest)+b'\n'; m=validate_manifest(raw)
    root=Path(root)
    for ancestor in [root,*root.absolute().parents]:
        if ancestor.is_symlink(): fail('malformed_manifest','writer refuses linked cache ancestors')
    for path in (root,root/'entries',root/'blobs',root/'blobs/sha256'):
        if path.is_symlink(): fail('malformed_manifest','writer refuses symlinked cache directories')
        make_directory(path)
    with writer_lock(root / '.publish.lock'):
        key=key_for(m['selection'],m['card']['sha256'],m['lock']['sha256'])
        try:
            old=read_entry(root,m['selection'],m['card']['sha256'],m['lock']['sha256'])
        except CacheError as e:
            if e.kind not in ('missing_entry','missing_blob','hash_mismatch','malformed_manifest'): raise
        else:
            # SDK provenance may differ across explicit repeat seeds.
            previous=dict(old.manifest); current=dict(m)
            previous.pop('sdk_identity'); current.pop('sdk_identity')
            if previous!=current: fail('identity_conflict','valid entry conflicts with newly acquired source bytes')
            return old
        for item in [m['card'],m['lock'],*m['sources']]:
            data=payloads[item['sha256']]
            if len(data)!=item['size'] or sha(data)!=item['sha256']: fail('hash_mismatch','writer payload differs from manifest')
            destination=root/'blobs/sha256'/item['sha256']
            if destination.is_symlink(): fail('malformed_manifest','writer refuses linked blobs')
            atomic(destination,data)
        atomic(root/'entries'/f'{key}.json',raw)
        return read_entry(root,m['selection'],m['card']['sha256'],m['lock']['sha256'])

def native_cache(entry, stage):
    """Construct a native protocol adapter without any download provider."""
    from loom.model.fetch import Document, Source
    from loom.model.fetch_storage import AcquiredFile, ContentCache
    from loom.model.lock import FileIdentity
    stage=Path(stage)
    class Provider:
        cancelled = threading.Event()
        cache_directory = stage
        def resolve_revision(self,root,*,offline):
            if re.fullmatch('[0-9a-f]{40}',root.get('revision','')): return root['revision']
            for record in entry.manifest['origin_roots'].values():
                if record['requested']==root: return record['resolved']['revision']
            fail('unknown_source','unseeded origin resolution')
    class Adapter(ContentCache):
        def __init__(self):
            super().__init__(stage, huggingface=Provider())
        def read_document(self,location,*,offline):
            for name in ('card','lock'):
                item=entry.manifest[name]
                if item['location']==location: return Document(location,entry.bytes(item).decode('utf-8'))
            fail('unknown_source','unseeded document')
        def item(self,source,identity):
            matches=[s for s in entry.manifest['sources'] if s['origin']==source.origin]
            if not matches: fail('unknown_source','unseeded program or auxiliary origin')
            item=matches[0]
            if any((s['sha256'],s['size'],s['resolved'])!=(item['sha256'],item['size'],item['resolved']) for s in matches): fail('malformed_manifest','ambiguous source origin')
            if source.resolved != item['resolved']: fail('seed_mismatch','source resolution differs from seed')
            if identity is not None and (identity.sha256,identity.size)!=(item['sha256'],item['size']): fail('seed_mismatch','native lock differs from seeded source')
            return item
        def stage(self,item):
            data=entry.bytes(item)
            stage.mkdir(parents=True,exist_ok=True)
            target=stage/item['sha256']
            if target.is_symlink(): fail('malformed_manifest','linked staging path')
            atomic(target,data)
            return target
        def prepare_source(self,source,identity,*,offline):
            item=self.item(source,identity)
            return Source(source.origin,{'kind':'local','path':str(self.stage(item))},source.resolved)
        def acquire(self,source,identity,*,offline,progress=None):
            item=self.item(source,identity)
            return AcquiredFile(self.stage(item),FileIdentity(source.origin,item['size'],item['sha256'],source.resolved))
        def acquire_transformed(self,*args,**kwargs): fail('unknown_source','pipeline transforms are unsupported')
    return Adapter()
