"""Native SDK read-only cache protocol; writes only disposable staged copies."""
import os
from pathlib import Path
import re
import tempfile
import threading
from .pipeline_reader import fail

def _stage_atomic(path,data):
    fd,name=tempfile.mkstemp(prefix='.seed-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            os.fchmod(stream.fileno(),0o644); stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)

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
            _stage_atomic(target,data)
            return target
        def prepare_source(self,source,identity,*,offline):
            item=self.item(source,identity)
            return Source(source.origin,{'kind':'local','path':str(self.stage(item))},source.resolved)
        def acquire(self,source,identity,*,offline,progress=None):
            item=self.item(source,identity)
            return AcquiredFile(self.stage(item),FileIdentity(source.origin,item['size'],item['sha256'],source.resolved))
        def acquire_transformed(self,*args,**kwargs): fail('unknown_source','pipeline transforms are unsupported')
    return Adapter()
