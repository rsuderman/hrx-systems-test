#!/usr/bin/env python3
"""Explicit host writer for original pipeline inputs; never acquires model assets."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid

REPO=Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location('hrx_bootstrap', Path(__file__).resolve().parent / '_bootstrap.py')
BOOTSTRAP = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(BOOTSTRAP)


def helper(name):
    return BOOTSTRAP.load_public(name)
CACHE=helper('pipeline-cache'); SEED=helper('seed-models'); CONFIG=helper('model-config'); INVENTORY=helper('cached-inventory')

def selected(cache_root, suite=None, cached=False, runtime=None):
    if cached:
        metadata, candidates=INVENTORY.discover(cache_root)
        if metadata['errors'] or any(c['conflict'] for c in candidates): raise ValueError('cached seed inventory is invalid or conflicting')
        if not candidates: raise ValueError('cached seed inventory is empty')
        entries=[({'id':c['id'],'selection':c['selection']},c['_seed']) for c in candidates]
    else:
        suite_helper=helper('seed-cached-models')
        entries=[(entry,SEED.read_json(path)) for entry,_,path in suite_helper.load_entries(suite or REPO/'config/cached-model-tests.json',cache_root)]
    result=[]
    for entry,seed in entries:
        baseline,lock=CONFIG.compose(entry,runtime)
        identity=INVENTORY.selection(seed)
        if identity!={k:baseline[k] for k in CACHE.FIELDS}: raise ValueError('suite selection differs from asset seed')
        INVENTORY.asset_identity(seed)
        result.append((entry['id'],baseline,lock,seed))
    return result

def native(work, output, cache_root=Path('/pipeline-cache')):
    from loom.model.fetch import plan_fetch
    from loom.model.fetch_source import Document
    baseline=SEED.read_json(work/'config/baseline.json'); seed=SEED.read_json(work/'seed.json')
    identity=INVENTORY.selection(seed)
    location=f"https://huggingface.co/{identity['repository']}/resolve/{identity['publication_revision']}/model.json"
    downloader=SEED.native_cache(work/'native-content',work/'native-hf',threading.Event())
    payloads={}
    def document_record(location):
        doc=downloader.read_document(location,offline=False)
        data=doc.text.encode('utf-8'); payloads[CACHE.sha(data)]=data
        return doc,{'location':location,'size':len(data),'sha256':CACHE.sha(data)}
    doc,card=document_record(location); lockdoc,lock=document_record(location+'.lock')
    if card['sha256']!=seed['card_sha256'] or lock['sha256']!=seed['lock_sha256']: raise ValueError('fresh documents differ from asset seed hashes')
    selection=SEED.selection_for(baseline,doc.text)
    class NoFloating:
        def resolve_revision(self,root,*,offline):
            revision=root.get('revision','')
            if not re.fullmatch('[0-9a-f]{40}',revision): raise ValueError('moving external source alias requires immutable published lock coordinates')
            return revision
    class Planner:
        huggingface=NoFloating()
        def read_document(self,value,*,offline):
            if value!=location+'.lock': raise ValueError('unknown planning document')
            return lockdoc
        def prepare_source(self,source,identity,*,offline): return source
    plan=plan_fetch(Document(location,doc.text),selection,Planner(),lock_location=location+'.lock')
    sources=[]
    for name,file in selection.files.items():
        if not SEED.code_file(file): continue
        if file.roles-{'program','auxiliary'} or file.transform is not None: raise ValueError('pipeline source must be untransformed code only')
        source=plan.sources[name]; expected=plan.identities.get(name)
        transport=source.transport
        kind=transport['kind']
        immutable=(kind=='inline' or (kind=='hf' and re.fullmatch('[0-9a-f]{40}',transport.get('revision',''))) or
                   (kind=='url' and transport['url'].startswith('https://huggingface.co/') and re.search('/resolve/[0-9a-f]{40}/',transport['url'])))
        if not immutable and expected is None: raise ValueError('external source requires a published digest and immutable coordinates')
        if kind=='hf' and not re.fullmatch('[0-9a-f]{40}',transport.get('revision','')): raise ValueError('moving external source')
        if kind=='url' and 'huggingface.co/' in transport['url'] and not re.search('/resolve/[0-9a-f]{40}/',transport['url']): raise ValueError('moving external Hugging Face URL')
        prepared=downloader.prepare_source(source,expected,offline=False)
        acquired=downloader.acquire(prepared,expected,offline=False)
        data=acquired.path.read_bytes(); payloads[CACHE.sha(data)]=data
        sources.append({'installation_path':name,'roles':sorted(file.roles),'origin':source.origin,'resolved':source.resolved,
                        'size':len(data),'sha256':CACHE.sha(data),'identity_authority':'published_lock' if expected else 'host_seed'})
    sdk=SEED.read_json(output/'sdk'/'sdk.json')
    manifest={'schema_version':1,'selection':identity,'card':card,'lock':lock,'sources':sorted(sources,key=lambda x:x['installation_path']),
              'origin_roots':plan.origins,'sdk_identity':sdk['identity']}
    entry=CACHE.publish(cache_root,manifest,payloads)
    SEED.write_json(output/'pipeline-seed.json',{'schema_version':1,'status':'ready','key':entry.key,'manifest_sha256':entry.manifest_sha256,'model_executed':False})

def seed_one(name, baseline, lock, seed, report, persistent):
    report.mkdir()
    with tempfile.TemporaryDirectory(prefix='.work-',dir=report) as tmp:
        tmp=Path(tmp); work=tmp/'run'; (work/'config').mkdir(parents=True)
        SEED.write_json(work/'config/baseline.json',baseline); SEED.write_json(work/'seed.json',seed)
        shutil.copyfile(lock,work/'config'/baseline['sdk']['dependency_lock'])
        payloads=tmp/'sdk-payloads'
        subprocess.run([sys.executable,'-I',str(REPO/'scripts/sdk-environment.py'),'acquire','--baseline',str(work/'config/baseline.json'),'--payload-root',str(payloads),'--output',str(report/'sdk-download')],check=True)
        container='hrx-pipeline-seed-'+uuid.uuid4().hex
        command=['docker','run','--rm','--name',container,'--read-only','--platform',baseline['container']['platform'],'--user',f'{os.getuid()}:{os.getgid()}',
                 '--cap-drop','ALL','--security-opt','no-new-privileges','--tmpfs','/tmp:rw,size=128m,mode=1777','--env','HOME=/tmp','--env','PYTHONNOUSERSITE=1',
                 '--mount',f'type=bind,src={REPO / "scripts"},dst=/source/scripts,readonly',
                 '--mount',f'type=bind,src={payloads},dst=/sdk-payloads,readonly','--mount',f'type=bind,src={work},dst=/run',
                 '--mount',f'type=bind,src={report},dst=/results','--mount',f'type=bind,src={persistent},dst=/pipeline-cache','--network','bridge']
        if os.environ.get('HF_TOKEN'): command+=['--env','HF_TOKEN']
        command += [baseline['container']['image'],'python','-I','/source/scripts/seed-pipelines.py','--stage']
        try:
            with (report/'seed.log').open('w') as log: subprocess.run(command,check=True,stdout=log,stderr=subprocess.STDOUT)
        finally: subprocess.run(['docker','rm','-f',container],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    return {'id':name,**SEED.read_json(report/'pipeline-seed.json')}

def run(args):
    root=Path(args.root or os.environ.get('HRX_SYSTEMS_ROOT',Path.home()/'.local/share/hrx-systems-test')).expanduser()
    if not root.is_absolute() or root==Path('/'): raise ValueError('root must be an absolute directory other than /')
    entries=selected(root/'cache',args.suite,args.selection=='cached',args.runtime)
    output=(args.output or root/'runs'/('pipeline-seed-'+uuid.uuid4().hex)).absolute()
    output.mkdir(parents=True,exist_ok=False)
    persistent=root/'cache/pipelines'
    CACHE.make_directory(persistent)
    reports=[]
    def save(status):
        SEED.write_json(output/'pipeline-seed-report.json',{'schema_version':1,'status':status,'entries':reports,'model_executed':False})
    try:
        # Separate publication locking lets the child publish atomically while
        # this host lock serializes complete public seed batches.
        with CACHE.writer_lock(persistent/'.seed.lock'):
            for name,baseline,lock,seed in entries:
                report=output/name
                try:
                    result=seed_one(name,baseline,lock,seed,report,persistent)
                except KeyboardInterrupt:
                    reports.append({'id':name,'status':'failed','reason':'entry interrupted','model_executed':False})
                    raise
                except Exception as error:
                    result={'id':name,'schema_version':1,'status':'failed','error':f'{type(error).__name__}: {error}','model_executed':False}
                    report.mkdir(parents=True,exist_ok=True)
                    SEED.write_json(report/'pipeline-seed.json',result)
                reports.append(result)
                save('in_progress')
        status='ready' if all(r['status']=='ready' for r in reports) else 'failed'
        save(status)
        print(f'Pipeline seed {status}. Report: {output / "pipeline-seed-report.json"}')
        return 0 if status=='ready' else 1
    except BaseException:
        completed={r['id'] for r in reports}
        reports.extend({'id':name,'status':'not_run','reason':'batch interrupted','model_executed':False}
                       for name,_,_,_ in entries if name not in completed)
        save('failed')
        raise

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path); parser.add_argument('--runtime',type=Path); parser.add_argument('--output',type=Path)
    group=parser.add_mutually_exclusive_group(); group.add_argument('--selection',choices=['cached']); group.add_argument('--suite',type=Path)
    parser.add_argument('--stage',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--native',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.native: native(Path('/run'),Path('/results')); return 0
    if args.stage:
        subprocess.run([sys.executable,'-I','/source/scripts/sdk-environment.py','install','--baseline','/run/config/baseline.json','--payload-root','/sdk-payloads','--output','/results/sdk'],check=True)
        return subprocess.run(['/run/venv/bin/python','-I','/source/scripts/seed-pipelines.py','--native']).returncode
    try: return run(args)
    except (ValueError,OSError,subprocess.SubprocessError) as error:
        print(f'Cannot seed pipelines: {error}',file=sys.stderr); return 1
if __name__=='__main__': raise SystemExit(main())
