"""SDK-neutral original pipeline storage; readers never create or repair cache files."""
import dataclasses
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

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
