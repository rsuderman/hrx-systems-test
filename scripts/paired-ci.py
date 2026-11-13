#!/usr/bin/env python3
"""Frozen paired-runtime planning, worker attribution and saved-results aggregation."""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
MAX_JSON = 32 * 1024 * 1024
DOWNLOAD_ACTION = 'd3f86a106a0bac45b974a628896c90dbdf5c8093'


def helper(name):
    s = importlib.util.spec_from_file_location('paired_' + name.replace('-', '_'), HERE / (name + '.py'))
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m


def read(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON:
        raise ValueError('expected bounded regular JSON file')
    return json.loads(path.read_text())


def write(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def contained(root, relative):
    value = Path(relative)
    if value.is_absolute() or '..' in value.parts: raise ValueError('plan reference escapes bundle')
    result = Path(root)
    for part in value.parts:
        result = result / part
        if result.is_symlink(): raise ValueError('plan reference is a symlink')
    return result


def normalize(event, raw):
    defaults = {'selection': 'cached', 'runtime_source': 'both', 'device_uri': 'amdgpu://0',
                'cycles': 2, 'full_integrity': False, 'runtime_run_id': '',
                'runtime_run_attempt': '', 'runtime_artifact_id': '', 'runtime_wheel_sha256': ''}
    if event not in ('push', 'schedule', 'workflow_dispatch'): raise ValueError('unsupported CI event')
    if not isinstance(raw, dict) or set(raw) - set(defaults): raise ValueError('unknown dispatch inputs')
    values = {**defaults, **raw} if event == 'workflow_dispatch' else defaults
    if values['selection'] not in ('cached', 'suite'): raise ValueError('invalid model selection')
    if values['runtime_source'] not in ('both', 'release-latest', 'actions-main', 'release', 'actions-replay'):
        raise ValueError('invalid runtime selection')
    if not isinstance(values['device_uri'], str) or not re.fullmatch(r'amdgpu://[0-9]+', values['device_uri']):
        raise ValueError('invalid device URI')
    if type(values['cycles']) is int and values['cycles'] > 0: pass
    elif isinstance(values['cycles'], str) and re.fullmatch('[1-9][0-9]*', values['cycles']): values['cycles'] = int(values['cycles'])
    else: raise ValueError('cycles must be positive')
    if values['full_integrity'] in ('true', 'false'): values['full_integrity'] = values['full_integrity'] == 'true'
    if type(values['full_integrity']) is not bool: raise ValueError('full_integrity must be boolean')
    A = helper('actions-runtime')
    replay = ('run_id', 'run_attempt', 'artifact_id', 'wheel_sha256')
    if values['runtime_source'] == 'actions-replay':
        for key in replay[:-1]: values['runtime_' + key] = str(A.positive(values['runtime_' + key], key))
        A.sha(values['runtime_wheel_sha256'], 64)
    elif any(values['runtime_' + k] not in ('', None) for k in replay):
        raise ValueError('replay identifiers require actions-replay')
    return values


def outputs(path, values):
    if path:
        with Path(path).open('a') as stream:
            for key, value in values.items():
                value = str(value)
                if '\n' in value or '\r' in value: raise ValueError('unsafe CI output')
                stream.write(key + '=' + value + '\n')


def resolution_failure(selector, error):
    # SDK clients emit safe errors, but strip URL queries and known credential forms
    # before allowing any unexpected exception into a persisted diagnostic.
    message = re.sub(r'https?://[^\s?]+\?[^\s]+', '<URL query redacted>', str(error))
    message = re.sub(r'(?i)(token|authorization|signature|credential)([=: ]+)[^\s,;]+', r'\1\2<redacted>', message)
    return {'schema_version': 1, 'status': 'failed', 'selector': copy.deepcopy(selector),
            'error': {'kind': 'resolution', 'message': message.encode()[:4000].decode(errors='ignore')}}


def exclusion_policy(value):
    """Only exact published selections on one explicitly affected stable release."""
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'lane', 'release', 'evidence_url', 'entries'}
            or value['schema_version'] != 1 or value['lane'] != 'latest-release'
            or not isinstance(value['release'], str) or not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', value['release'])
            or not isinstance(value['evidence_url'], str)
            or not re.fullmatch(r'https://github.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+/attempts/[0-9]+', value['evidence_url'])
            or not isinstance(value['entries'], list) or len(value['entries']) > 4096):
        raise ValueError('invalid temporary CI exclusion policy')
    inventory = helper('cached-inventory'); seen = set()
    for row in value['entries']:
        if (not isinstance(row, dict) or set(row) != {'selection', 'reason'}
                or not isinstance(row['selection'], dict) or set(row['selection']) != set(inventory.FIELDS)
                or not isinstance(row['reason'], str) or not row['reason'].strip() or len(row['reason']) > 4096):
            raise ValueError('temporary CI exclusions require exact selections and reasons')
        identity = inventory.selection({'schema_version': 1, 'selection': row['selection']})
        key = inventory.fingerprint(identity)
        if key in seen: raise ValueError('duplicate temporary CI exclusion')
        seen.add(key)
    return value


def lane_snapshot(common, lane_id, runtime, policy):
    """Partition the frozen inventory, preserving all metadata and inventory errors."""
    source = runtime.get('source', {})
    rules = policy['entries'] if (lane_id == policy['lane'] and source.get('kind') == 'release'
                                 and source.get('release') == policy['release']) else []
    snapshot, kept, excluded = copy.deepcopy(common), [], []
    for record in snapshot['entries']:
        rule = next((rule for rule in rules if record['selection'] == rule['selection']), None)
        if rule is None:
            kept.append(record)
        else:
            excluded.append({'id': record['entry']['id'], 'selection': record['selection'],
                             'reason': rule['reason'], 'release': policy['release'],
                             'evidence_url': policy['evidence_url']})
    snapshot['entries'] = kept
    if snapshot.get('inventory') is not None:
        snapshot['inventory']['entries'] = [record['entry'] for record in kept]
    if not kept and excluded: raise ValueError('temporary CI exclusions leave the lane empty')
    return snapshot, excluded


def expected_entries(snapshot):
    return [{'id': row['entry']['id'], 'selection': row['selection']} for row in snapshot['entries']]


def plan(root, output, values, *, repo=REPO, freezer=None):
    root, output, repo = Path(root), Path(output), Path(repo)
    output.mkdir(parents=True, exist_ok=False)
    S, A, R, SDK = helper('selection-snapshot'), helper('actions-runtime'), helper('release-runtime'), helper('sdk-environment')
    selection = S.freeze(values['selection'], root / 'cache', repo)
    write(output / 'selection-snapshot.json', selection)
    policy_path = repo / 'config/ci-model-exclusions.json'
    policy = exclusion_policy(read(policy_path)) if policy_path.exists() else None
    if policy is not None: write(output / 'ci-model-exclusions.json', policy)
    settings_path = repo / 'config/runtime.json'
    settings, _, _, _ = SDK.load_runtime(settings_path)
    modes = ['release-latest', 'actions-main'] if values['runtime_source'] == 'both' else [values['runtime_source']]
    lanes = []
    for mode in modes:
        lane_id = {'release-latest': 'latest-release', 'actions-main': 'latest-ci', 'release': 'pinned-release', 'actions-replay': 'ci-replay'}[mode]
        item = {'id': lane_id, 'runtime_source': mode}
        if mode == 'release':
            directory = output / 'pinned-runtime'; directory.mkdir()
            shutil.copyfile(settings_path, directory / 'runtime.json')
            lock = settings['sdk']['dependency_lock']
            shutil.copyfile(settings_path.parent / lock, directory / lock)
            item.update(runtime_config='pinned-runtime/runtime.json', runtime_config_sha256=sha(directory / 'runtime.json'),
                        dependency_lock=lock, dependency_lock_sha256=sha(directory / lock), resolution_status='selected')
        else:
            args = SimpleNamespace(runtime_source=mode, **{'runtime_' + k: values.get('runtime_' + k) or None for k in ('run_id', 'run_attempt', 'artifact_id', 'wheel_sha256')})
            selector = A.selector_from_args(args, settings['sdk']['source'])
            try:
                frozen = (freezer or R.freeze_selection)(selector)
                item['resolution_status'] = 'selected'
            except (ValueError, OSError, KeyError, TypeError) as error:
                frozen = resolution_failure(selector, error)
                item['resolution_status'] = 'failed'
            name = 'runtime-selection-' + lane_id + '.json'; write(output / name, frozen)
            item.update(runtime_selection=name, runtime_selection_sha256=sha(output / name))
        if policy is not None:
            selected = frozen if mode != 'release' else {}
            snapshot, excluded = lane_snapshot(selection, lane_id, selected, policy)
            name = 'selection-snapshot-' + lane_id + '.json'; write(output / name, snapshot)
            item.update(selection_snapshot=name, selection_snapshot_sha256=sha(output / name),
                        expected_entries=expected_entries(snapshot), excluded_entries=excluded)
        lanes.append(item)
    document = {'schema_version': 1, 'selected_at': datetime.now(timezone.utc).isoformat(),
                'inputs': values, 'selection_snapshot': 'selection-snapshot.json',
                'selection_snapshot_sha256': sha(output / 'selection-snapshot.json'),
                'expected_entries': expected_entries(selection),
                'lanes': lanes}
    if policy is not None:
        document.update(exclusion_policy='ci-model-exclusions.json',
                        exclusion_policy_sha256=sha(output / 'ci-model-exclusions.json'))
    write(output / 'ci-plan.json', document)
    return document


def load_plan(path):
    path = Path(path); value = read(path)
    if value.get('schema_version') != 1 or not isinstance(value.get('lanes'), list) or not value['lanes'] or len(value['lanes']) > 2:
        raise ValueError('invalid paired CI plan')
    references = {value['selection_snapshot']: value['selection_snapshot_sha256']}
    if 'exclusion_policy' in value: references[value['exclusion_policy']] = value['exclusion_policy_sha256']
    ids = set()
    for lane in value['lanes']:
        if lane['id'] not in ('latest-release', 'latest-ci', 'pinned-release', 'ci-replay') or lane['id'] in ids:
            raise ValueError('invalid/duplicate lane identity')
        ids.add(lane['id'])
        fields = ('selection_snapshot', 'selection_snapshot_sha256', 'expected_entries', 'excluded_entries')
        if ('exclusion_policy' in value) != all(field in lane for field in fields) or (
                'exclusion_policy' not in value and any(field in lane for field in fields)):
            raise ValueError('lane selection requires a frozen exclusion policy')
        if 'exclusion_policy' in value:
            references[lane['selection_snapshot']] = lane['selection_snapshot_sha256']
        if 'runtime_selection' in lane: references[lane['runtime_selection']] = lane['runtime_selection_sha256']
        else:
            references[lane['runtime_config']] = lane['runtime_config_sha256']
            references['pinned-runtime/' + lane['dependency_lock']] = lane['dependency_lock_sha256']
    for name, expected in references.items():
        if sha(contained(path.parent, name)) != expected: raise ValueError('frozen plan input digest changed: ' + name)
    snapshot = read(contained(path.parent, value['selection_snapshot']))
    expected = expected_entries(snapshot)
    if value['expected_entries'] != expected: raise ValueError('plan denominator differs from frozen selection')
    if 'exclusion_policy' in value:
        policy = exclusion_policy(read(contained(path.parent, value['exclusion_policy'])))
        for lane in value['lanes']:
            runtime = read(contained(path.parent, lane['runtime_selection'])) if 'runtime_selection' in lane else {}
            selected, excluded = lane_snapshot(snapshot, lane['id'], runtime, policy)
            if (read(contained(path.parent, lane['selection_snapshot'])) != selected
                    or lane['expected_entries'] != expected_entries(selected) or lane['excluded_entries'] != excluded):
                raise ValueError('lane selection or exclusions differ from frozen policy')
    return value


def initialize(plan_path, lane_id, output, previous_safe=True):
    document = load_plan(plan_path); lane = next(x for x in document['lanes'] if x['id'] == lane_id)
    output = Path(output); output.mkdir(parents=True, exist_ok=False)
    values = document['inputs']
    enabled = lane.get('expected_entries', document['expected_entries'])
    excluded = lane.get('excluded_entries', [])
    workflow = {'schema_version': 1, 'lane_id': lane_id, 'plan_sha256': sha(plan_path)}
    for key, env in {'repository':'GITHUB_REPOSITORY','run_id':'GITHUB_RUN_ID','run_attempt':'GITHUB_RUN_ATTEMPT','ref':'GITHUB_REF','sha':'GITHUB_SHA','runner':'RUNNER_NAME'}.items():
        if os.environ.get(env): workflow[key] = os.environ[env]
    if workflow.get('repository') and workflow.get('run_id'):
        workflow['run_url'] = 'https://github.com/' + workflow['repository'] + '/actions/runs/' + workflow['run_id']
    workflow['normalized_inputs'] = {**values, 'runtime_source': lane['runtime_source']}
    workflow['event_name'] = os.environ.get('GITHUB_EVENT_NAME', 'workflow_dispatch')
    workflow['observed_at'] = datetime.now(timezone.utc).isoformat()
    workflow['observed_time_source'] = 'runner initialization clock'
    if workflow['event_name'] == 'schedule':
        workflow.update(schedule='0 4 * * *', schedule_timezone='America/Los_Angeles')
    write(output / 'workflow.json', workflow); write(output / 'ci-plan.json', document)
    selected = read(contained(Path(plan_path).parent, lane['runtime_selection'])) if 'runtime_selection' in lane else read(contained(Path(plan_path).parent, lane['runtime_config']))
    write(output / 'lane-selection.json', {'schema_version':1, 'lane_id':lane_id, 'selection':selected,
                                         'selection_snapshot_sha256':lane.get('selection_snapshot_sha256', document['selection_snapshot_sha256']),
                                         'expected_entries': enabled, 'excluded_entries': excluded})
    write(output / 'worker-state.json', {'schema_version':1, 'lane_id':lane_id, 'run_invoked':False, 'blocked':not previous_safe})
    if not previous_safe:
        reason = 'Previous lane GPU cleanup was not confirmed; no second GPU server started.'
        models = [{'id':e['id'], 'selection':e['selection'], 'status':'not_run', 'reason':reason,
                   'preparation':{'status':'not_run','reason':reason}, 'cycles':[], 'evidence':[]} for e in enabled]
        raw = {'schema_version':1,'attempt_id':output.name,'mode':'real','status':'failed','reason':reason,
               'source':{'commit':workflow.get('sha')},'runtime':{},'hardware':{},'expected_entries':enabled,
               'models':models,'server':{k:{'status':'not_run','elapsed_seconds':None,'reason':reason} for k in ('start','shutdown','cleanup')},
               'workflow':workflow,'evidence':['workflow.json']}
        write(output / 'results/workflow.json', workflow)
        write(output / 'results/suite-result.json',raw)
    return {'E2E_OUTPUT':str(output),'MODEL_SELECTION':values['selection'],'DEVICE_URI':values['device_uri'],
            'TEST_CYCLES':str(values['cycles']),'FULL_INTEGRITY':str(values['full_integrity']).lower(),
            'BLOCKED':str(not previous_safe).lower(),'RUNTIME_SOURCE':lane['runtime_source'],
            **{key.upper():str(values[key]) for key in ('runtime_run_id','runtime_run_attempt','runtime_artifact_id','runtime_wheel_sha256')}}


def cleanup_safe(attempt, stages):
    attempt = Path(attempt)
    if stages.get('cleanup') != 'success': return False, 'owned-container cleanup command did not succeed'
    state = read(attempt / 'worker-state.json')
    if not state['run_invoked']: return True, 'no GPU execution invoked; owned-container absence confirmed'
    try: raw = read(attempt / 'results/suite-result.json')
    except (ValueError, OSError): return False, 'execution result missing; GPU teardown unknown'
    server = raw.get('server', {})
    if server.get('start', {}).get('status') == 'not_run' and not any(m.get('cycles') for m in raw.get('models', [])) and server.get('cleanup', {}).get('status') == 'passed':
        return True, 'no model/server started; owned-container absence confirmed'
    if any(server.get(k, {}).get('status') != 'passed' for k in ('start','shutdown','cleanup')):
        return False, 'server teardown was not fully confirmed'
    if server.get('shutdown', {}).get('forced'): return False, 'server shutdown required force'
    for model in raw.get('models', []):
        for cycle in model.get('cycles', []):
            if cycle.get('unload', {}).get('status') != 'passed' or cycle.get('unload', {}).get('forced'):
                return False, 'model removal was not safely confirmed'
    return True, 'model removal, server teardown and owned-container absence confirmed'


def finish(attempt, stages, report_url=None, failure_url=None):
    attempt = Path(attempt); state = read(attempt / 'worker-state.json')
    safe, reason = cleanup_safe(attempt, stages)
    raw_path = attempt / 'results/suite-result.json'
    raw = read(raw_path) if raw_path.exists() else {}
    selection = read(attempt / 'lane-selection.json')
    counts = raw.get('counts')
    if not counts:
        models = raw.get('models', []); counts = {s:sum(m.get('status')==s for m in models) for s in ('passed','failed','not_run')}
        counts['expected'] = len(selection['expected_entries'])
        if not raw: counts['not_run'] = counts['expected']
    required = ('prepare','run','cleanup','report','collect','package','upload','summary')
    passed = not state['blocked'] and raw.get('status') == 'passed' and all(stages.get(k)=='success' for k in required)
    def url(value): return value if isinstance(value,str) and re.fullmatch(r'https://[A-Za-z0-9./_%?=&:+~-]+',value) else None
    value = {'schema_version':1,'lane_id':state['lane_id'],'status':'passed' if passed else ('blocked' if state['blocked'] else 'failed'),
             'cleanup_safe':safe,'cleanup_reason':reason,'counts':counts,'runtime':raw.get('runtime',{}),
             'stages':stages,'report_url':url(report_url),'failure_url':url(failure_url),
             'workflow':read(attempt/'workflow.json'),'selected_runtime':read(attempt/'lane-selection.json')}
    value['excluded_entries'] = selection.get('excluded_entries', [])
    observed = [{'id': entry.get('id'), 'selection': entry.get('selection')}
                for entry in raw.get('expected_entries', [])]
    if raw and observed != selection.get('expected_entries'):
        value.update(status='failed', reason='execution denominator differs from the frozen lane selection')
    if stages.get('upload')=='success' and not value['report_url']: value['status']='failed'
    write(attempt/'lane-result.json',value)
    return value


def aggregate(plan_path, results_root, output):
    plan = load_plan(plan_path); results_root, output = Path(results_root),Path(output); output.mkdir(parents=True,exist_ok=True)
    expected = {x['id'] for x in plan['lanes']}; rows={}
    for path in results_root.rglob('lane-result.json'):
        row=read(path); identity=row.get('lane_id')
        if identity not in expected or identity in rows: raise ValueError('unexpected/duplicate lane result')
        if row.get('workflow',{}).get('plan_sha256')!=sha(plan_path): raise ValueError('lane result belongs to another frozen plan')
        if row.get('status') not in ('passed','failed','blocked') or type(row.get('cleanup_safe')) is not bool:
            raise ValueError('invalid lane outcome')
        if any(type(n) is not int or n < 0 for n in row.get('counts',{}).values()): raise ValueError('invalid lane counts')
        lane = next(lane for lane in plan['lanes'] if lane['id'] == identity)
        if 'expected_entries' in lane:
            if row.get('excluded_entries') != lane['excluded_entries']:
                raise ValueError('lane result exclusions differ from frozen plan')
            if row.get('counts', {}).get('expected') != len(lane['expected_entries']):
                raise ValueError('lane result denominator differs from frozen plan')
        if row.get('report_url') and not re.fullmatch(r'https://[A-Za-z0-9./_%?=&:+~-]+',row['report_url']): raise ValueError('invalid report URL')
        rows[identity]=row
    ordered=[]
    for lane in plan['lanes']:
        ordered.append(rows.get(lane['id'],{'lane_id':lane['id'],'status':'not_run','counts':{'expected':len(lane.get('expected_entries', plan['expected_entries']))},'excluded_entries':lane.get('excluded_entries', []),'cleanup_safe':False,'cleanup_reason':'No lane result artifact was retained'}))
    passed=all(x['status']=='passed' and x.get('cleanup_safe') for x in ordered)
    value={'schema_version':1,'status':'passed' if passed else 'failed','plan_sha256':sha(plan_path),'selected_at':plan['selected_at'],'lanes':ordered}
    write(output/'paired-result.json',value)
    lines=['# Release and CI model verification','',f"Overall: **{value['status']}**. Runtime selections frozen at {plan['selected_at']}.",'','| Lane | SDK version / source | Result | Enabled | Excluded | Passed | Failed | Not run | Cleanup | Report |','| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |']
    for row in ordered:
        c=row.get('counts',{});link='[Download report]('+row['report_url']+')' if row.get('report_url') else 'Unavailable'
        selected = row.get('selected_runtime',{}).get('selection',{})
        source = selected.get('source',{})
        identity = row.get('runtime',{}).get('sdk_identity',{}).get('version') or row.get('runtime',{}).get('resolved_runtime',{}).get('wheel',{}).get('version') or source.get('version') or source.get('release') or source.get('release_tag') or source.get('tag') or source.get('tested_sha') or source.get('commit') or source.get('run_id') or 'Unresolved'
        provenance = ' '.join(str(source[key]) for key in ('tested_sha','run_id','run_attempt','artifact_id') if source.get(key) is not None)
        identity = (str(identity) + (' / ' + provenance if provenance else '')).replace('|','&#124;').replace('\n',' ')
        lines.append(f"| {row['lane_id']} | {identity} | {row['status']} | {c.get('expected',0)} | {len(row.get('excluded_entries',[]))} | {c.get('passed',0)} | {c.get('failed',0)} | {c.get('not_run',0)} | {'confirmed' if row.get('cleanup_safe') else 'unconfirmed'} | {link} |")
    excluded = [(row['lane_id'], entry) for row in ordered for entry in row.get('excluded_entries', [])]
    if excluded:
        lines += ['', '## Temporary exclusions', '', 'Excluded entries were not prepared or executed and are not counted as passes.', '',
                  '| Lane | Pipeline | Release | Reason | Evidence |', '| --- | --- | --- | --- | --- |']
        def cell(text): return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('|', '&#124;').replace('\r', ' ').replace('\n', ' ')
        for lane, entry in excluded:
            selected = entry['selection']
            label = selected['repository'] + '@' + selected['publication_revision'][:12] + ' / ' + selected['variant']
            lines.append(f"| {lane} | {cell(label)} | {entry['release']} | {cell(entry['reason'])} | [Source run]({entry['evidence_url']}) |")
    (output/'summary.md').write_text('\n'.join(lines)+'\n')
    return value


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('plan');p.add_argument('--root',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--inputs',type=Path,required=True);p.add_argument('--event',required=True);p.add_argument('--github-output')
    p=sub.add_parser('initialize');p.add_argument('--plan',type=Path,required=True);p.add_argument('--lane',required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--previous-safe',choices=('true','false'),default='true');p.add_argument('--github-env')
    p=sub.add_parser('prepare-args');p.add_argument('--plan',type=Path,required=True);p.add_argument('--lane',required=True)
    p=sub.add_parser('mark-run');p.add_argument('--output',type=Path,required=True)
    p=sub.add_parser('finish');p.add_argument('--output',type=Path,required=True);p.add_argument('--stages',required=True);p.add_argument('--github-output');p.add_argument('--report-url');p.add_argument('--failure-url')
    p=sub.add_parser('aggregate');p.add_argument('--plan',type=Path,required=True);p.add_argument('--results',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.command=='plan':
        value=plan(args.root,args.output,normalize(args.event,read(args.inputs)));lanes=[x['id'] for x in value['lanes']]
        outputs(args.github_output,{'ready':'true','first_lane':lanes[0],'second_lane':lanes[1] if len(lanes)>1 else ''});return 0
    if args.command=='initialize':
        outputs(args.github_env,initialize(args.plan,args.lane,args.output,args.previous_safe=='true'));return 0
    if args.command=='prepare-args':
        value=load_plan(args.plan);lane=next(x for x in value['lanes'] if x['id']==args.lane);base=args.plan.parent
        values=['--selection-snapshot',str(contained(base,lane.get('selection_snapshot',value['selection_snapshot'])))]
        if 'runtime_selection' in lane:values+=['--runtime-selection',str(contained(base,lane['runtime_selection']))]
        else:values+=['--runtime',str(contained(base,lane['runtime_config'])),'--runtime-source','release']
        for value in values:sys.stdout.buffer.write(value.encode()+b'\0')
        return 0
    if args.command=='mark-run':
        value=read(args.output/'worker-state.json');value['run_invoked']=True;write(args.output/'worker-state.json',value);return 0
    if args.command=='finish':
        value=finish(args.output,json.loads(args.stages),args.report_url,args.failure_url);outputs(args.github_output,{'cleanup_safe':str(value['cleanup_safe']).lower(),'status':value['status']});return 0
    try:
        value=aggregate(args.plan,args.results,args.output)
    except (OSError,ValueError,KeyError,TypeError) as error:
        reason=resolution_failure({},error)['error']['message']
        write(args.output/'paired-result.json',{'schema_version':1,'status':'failed','reason':reason})
        (args.output/'summary.md').write_text('# Verification incomplete\n\nSaved comparison inputs or lane results could not be validated. See paired-result.json for the diagnostic.\n')
        return 1
    return 0 if value['status']=='passed' else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except (OSError,ValueError,KeyError,TypeError,StopIteration) as error:
        print('Paired CI error: '+str(error),file=sys.stderr);raise SystemExit(1)
