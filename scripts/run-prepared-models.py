#!/usr/bin/env python3
"""Execute an already prepared model suite offline; save raw evidence only."""
import argparse
from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request


SCRIPT_DIR = Path(__file__).resolve().parent
PHASES = ('load', 'readiness', 'query', 'assertion', 'unload')
MAX_EVIDENCE_BYTES = 4 * 1024 * 1024
_ASSET_SPEC = importlib.util.spec_from_file_location('asset_verification', SCRIPT_DIR / 'asset-verification.py')
ASSETS = importlib.util.module_from_spec(_ASSET_SPEC)
_ASSET_SPEC.loader.exec_module(ASSETS)
_PROGRESS_SPEC = importlib.util.spec_from_file_location('ci_progress', SCRIPT_DIR / 'ci-progress.py')
PROGRESS = importlib.util.module_from_spec(_PROGRESS_SPEC)
_PROGRESS_SPEC.loader.exec_module(PROGRESS)
_CAPTURE_SPEC = importlib.util.spec_from_file_location('evidence_capture', SCRIPT_DIR / 'evidence-capture.py')
CAPTURE = importlib.util.module_from_spec(_CAPTURE_SPEC)
_CAPTURE_SPEC.loader.exec_module(CAPTURE)
_PIPELINE_SPEC = importlib.util.spec_from_file_location('pipeline_preparation', SCRIPT_DIR / 'pipeline-preparation.py')
PIPELINE = importlib.util.module_from_spec(_PIPELINE_SPEC)
_PIPELINE_SPEC.loader.exec_module(PIPELINE)


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.result-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def contained(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('bundle paths must be relative and remain inside the bundle')
    target = (root / relative).resolve(strict=True)
    if not target.is_relative_to(root.resolve(strict=True)):
        raise ValueError('bundle path escapes its root')
    return target


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def phase(status='not_run', reason='not attempted', started=None, **observations):
    value = {'status': status, 'elapsed_seconds': None if started is None else time.monotonic() - started}
    if reason:
        value['reason'] = reason
    value.update(observations)
    return value


def initial_results(suite, hardware, mode='real'):
    expected, models = [], []
    ids = [entry['id'] for entry in suite['entries']]
    if (not ids and not suite.get('inventory_errors')) or len(set(ids)) != len(ids) or any(
            not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', value) for value in ids):
        raise ValueError('prepared entries must have unique path-safe IDs and be nonempty')
    for entry in suite['entries']:
        selection = copy.deepcopy(entry.get('selection', {}))
        expected.append({'id': entry['id'], 'selection': selection,
            'test_kind': entry.get('test_kind', 'text_smoke'),
            'expected_text': entry.get('smoke_expected_text')})
        preparation = copy.deepcopy(entry['preparation'])
        models.append({'id': entry['id'], 'selection': selection, 'preparation': preparation,
            'status': 'not_run' if preparation['status'] == 'ready' else 'failed',
            'reason': 'execution not attempted' if preparation['status'] == 'ready' else preparation.get('reason'),
            'cycles': [], 'evidence': []})
    return {'schema_version': 1, 'attempt_id': suite['attempt_id'], 'mode': mode,
        'source': copy.deepcopy(suite.get('source', {})), 'runtime': copy.deepcopy(suite.get('runtime') or {}),
        'selection_mode': suite.get('selection_mode', 'suite'),
        'preparation_contract': copy.deepcopy(suite.get('preparation_contract', {})),
        'inventory_sha256': suite.get('inventory_sha256'),
        'inventory_errors': copy.deepcopy(suite.get('inventory_errors', [])),
        'asset_verification': {'preparation': copy.deepcopy(suite.get('asset_verification', {}))},
        'hardware': hardware, 'expected_entries': expected, 'status': 'incomplete',
        'reason': 'execution not complete', 'started_at': now(), 'ended_at': None,
        'elapsed_seconds': None, 'server': {'identity': {}, 'start': phase(),
        'shutdown': phase(), 'cleanup': phase()}, 'models': models, 'evidence': []}


def copy_evidence(prepared, output, suite, result):
    """Copy only bounded files referenced by preparation, never payload trees."""
    if suite.get('inventory_path'):
        source = contained(prepared, suite['inventory_path'])
        if source.stat().st_size > MAX_EVIDENCE_BYTES or digest(source) != suite['inventory_sha256']:
            raise ValueError('frozen cache inventory changed')
        shutil.copyfile(source, output / 'inventory.json')
        result['evidence'].append('inventory.json')
    for record in PIPELINE.validate_frozen_inputs(prepared, suite):
        destination = output / record['path']
        shutil.copyfile(contained(prepared, record['path']), destination)
        result['evidence'].append(record['path'])
    if suite.get('frozen_inputs'):
        result['frozen_inputs'] = copy.deepcopy(suite['frozen_inputs'])
    for record in suite.get('policy_files', []):
        relative = record['path']
        source = contained(prepared, relative)
        if source.stat().st_size > MAX_EVIDENCE_BYTES or digest(source) != record['sha256']:
            raise ValueError('frozen runtime policy evidence changed')
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        result['evidence'].append(relative)
    if suite.get('runtime_policy') is not None:
        result['runtime_policy'] = copy.deepcopy(suite['runtime_policy'])
    for entry, model in zip(suite['entries'], result['models']):
        for key in ('pipeline_upgrade', 'pipeline_cache', 'published_execution', 'effective_execution', 'effective_test_profile'):
            if key in entry:
                model[key] = copy.deepcopy(entry[key])
        references = list(entry.get('evidence', []))
        if entry.get('inputs_manifest'):
            references.append(entry['inputs_manifest'])
        for relative in dict.fromkeys(references):
            source = contained(prepared, relative)
            if not source.is_file() or source.stat().st_size > MAX_EVIDENCE_BYTES:
                raise ValueError(f'preparation evidence is not a bounded regular file: {relative}')
            if source.suffix.lower() not in ('.json', '.log', '.txt'):
                raise ValueError('unexpected preparation evidence type')
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            model['evidence'].append(relative)


class Api:
    def __init__(self, address, process, http_timeout=5):
        self.address, self.process, self.http_timeout = address, process, http_timeout
        self.deadline = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, route, payload=None, timeout=None, accepted=(200,)):
        if self.process.poll() is not None:
            raise RuntimeError(f'owned server exited with status {self.process.returncode}')
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.address + route, data=data,
            headers={'Content-Type': 'application/json'} if data is not None else {})
        timeout = timeout or self.http_timeout
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('model cleanup deadline exceeded')
            timeout = min(timeout, remaining)
        try:
            response = self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            detail = error.read(8192).decode('utf-8', errors='replace')
            raise HttpFailure(error.code, detail) from error
        with response:
            if response.status not in accepted:
                raise RuntimeError(f'{route} returned unexpected HTTP {response.status}')
            body = response.read(MAX_EVIDENCE_BYTES + 1)
            if len(body) > MAX_EVIDENCE_BYTES:
                raise ValueError('HTTP response exceeded evidence limit')
            return json.loads(body) if body else {}

    def status(self, name=None):
        data = self.request('/v1/status')['data']
        if not isinstance(data, list):
            raise ValueError('status data is not a list')
        matches = data if name is None else [item for item in data if item['id'] == name]
        if name is not None and len(matches) > 1:
            raise RuntimeError('duplicate registration names in status')
        return matches if name is None else (matches[0] if matches else None)


class HttpFailure(RuntimeError):
    def __init__(self, status, body):
        self.status = status
        super().__init__(f'HTTP {status}: {body}')


def poll(check, timeout, process, interval=0.05):
    deadline = time.monotonic() + timeout
    while True:
        if process.poll() is not None:
            raise RuntimeError(f'owned server exited with status {process.returncode}')
        value = check()
        if value:
            return value
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('lifecycle deadline exceeded')
        time.sleep(min(interval, remaining))


def wait_ready(api, name, cycle, timeout):
    def check():
        row = api.status(name)
        if row is None:
            return False
        state = row['loom']
        incarnation = state['incarnation']
        if cycle['incarnation'] is not None and incarnation != cycle['incarnation']:
            raise RuntimeError('model incarnation changed unexpectedly')
        cycle['incarnation'] = incarnation
        if state.get('error') or state['registration'] in ('failed', 'release_failed', 'retiring'):
            raise RuntimeError(f"model failed: {state.get('error') or state['registration']}")
        if state['registration'] != 'ready' or state.get('status', {}).get('admission') != 'open':
            return False
        return any(item['id'] == name for item in api.request('/v1/models')['data'])
    return poll(check, timeout, api.process)


def unregister(api, name, cycle, timeout, abort_timeout):
    started = time.monotonic()
    row = api.status(name)
    if row is None:
        if cycle['load']['status'] != 'passed':
            raise RuntimeError('registration outcome uncertain and no owned incarnation was observed')
        return phase('passed', None, started, already_absent=True)
    incarnation = row['loom']['incarnation']
    if cycle['incarnation'] is not None and incarnation != cycle['incarnation']:
        raise RuntimeError('refusing removal after model incarnation changed')
    cycle['incarnation'] = incarnation

    def absent():
        current = api.status(name)
        if current is None:
            return True
        state = current['loom']
        if state['incarnation'] != incarnation:
            raise RuntimeError('model incarnation changed during removal')
        if state['registration'] == 'release_failed':
            raise RuntimeError('native model resource release failed')
        return False

    forced = False
    api.request('/v1/models/unregister', {'model': name, 'mode': 'drain'}, accepted=(202,))
    try:
        poll(absent, timeout, api.process)
    except TimeoutError:
        forced = True
        api.request('/v1/models/unregister', {'model': name, 'mode': 'abort'}, accepted=(202,))
        poll(absent, abort_timeout, api.process)
    return phase('passed', None, started, forced=forced)


def idle_pool_snapshot(api):
    status = api.request('/v1/status')
    if status['data'] != []:
        raise RuntimeError('idle pool reclamation requires zero registrations')
    pools = status['server']['resources']['pools']
    if not isinstance(pools, list) or not pools:
        raise ValueError('server does not publish owned serving pools')
    keys = [pool['key'] for pool in pools]
    if any(not isinstance(key, str) or not key for key in keys) or len(set(keys)) != len(keys):
        raise ValueError('server pool keys must be nonempty and unique')
    quiet = True
    for pool in pools:
        memory = pool['memory']
        retained = memory['retained']
        if memory['backing'] not in ('fixed', 'elastic') or (retained is None) != (memory['backing'] == 'fixed'):
            raise ValueError('unsupported pool backing accounting')
        pending = [memory['workspace']['quarantined_bytes']]
        if retained is not None:
            pending += [retained['pending_bytes'], retained['releasing_bytes']]
        if any(type(value) is not int or value < 0 for value in pending):
            raise ValueError('pool retirement counters must be nonnegative integers')
        quiet &= not any(pending)
    return {'registration_count': 0, 'pools': pools}, quiet


def reclaim_idle_pools(api, output_path):
    evidence = {'before': None, 'trims': [], 'after': None}
    try:
        evidence['before'], _ = idle_pool_snapshot(api)
        keys = {pool['key'] for pool in evidence['before']['pools']}
        for key in sorted(keys):
            response = api.request('/v1/pools/trim', {'pool': key, 'target_bytes': 0})
            evidence['trims'].append(response)
            if type(response['target_bytes']) is not int or response['target_bytes'] != 0 or response['pool']['key'] != key:
                raise ValueError('unsupported pool reclamation response')

        def quiescent():
            evidence['after'], quiet = idle_pool_snapshot(api)
            if {pool['key'] for pool in evidence['after']['pools']} != keys:
                raise RuntimeError('owned serving pools changed during reclamation')
            return quiet

        poll(quiescent, max(0, api.deadline - time.monotonic()), api.process)
    except Exception as error:
        evidence['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        if len(json.dumps(evidence, indent=2).encode()) + 1 > MAX_EVIDENCE_BYTES // 2:
            write_json(output_path, {'error': 'pool reclamation evidence exceeded its size limit'})
            raise ValueError('pool reclamation evidence exceeded its size limit')
        write_json(output_path, evidence)


def assert_response(entry, response):
    kind = entry.get('test_kind', 'text_smoke')
    if kind == 'text_smoke':
        actual = response['choices'][0]['message']['content']
        if not isinstance(actual, str) or actual.strip() != entry['smoke_expected_text']:
            raise ValueError('model response did not satisfy configured exact-text assertion')
    elif kind == 'decision_smoke':
        answer = response['answers']['route']
        expected = entry['smoke_expected_text']
        probabilities = answer['probabilities']
        def one(value):
            return type(value) in (int, float) and math.isfinite(value) and abs(value - 1) <= 1e-6
        usage = response['usage']
        if (response.get('model') != entry['selection']['model_id'] or answer.get('type') != 'choice' or
                answer.get('choice') != expected or set(probabilities) != {expected} or
                not one(probabilities[expected]) or not one(answer.get('confidence')) or
                type(usage.get('input_tokens')) is not int or usage['input_tokens'] <= 0 or
                type(usage.get('output_tokens')) is not int or usage['output_tokens'] != 0):
            raise ValueError('decision response did not satisfy configured single-choice service smoke assertion')
    else:
        raise ValueError(f'unsupported_test_kind: {kind}')


def run_cycle(api, entry, model, output, cycle_number, limits, position=None):
    name = entry['selection']['model_id']
    prefix = f"models/{entry['id']}/cycle-{cycle_number}"
    cycle = {'model_id': name, 'incarnation': None,
             **{key: phase() for key in PHASES}}
    model['cycles'].append(cycle)
    label = f"run {position or '?'} cycle {cycle_number}: {name}"
    stage, accepted = 'load', False
    started = time.monotonic()
    try:
        if api.status(name) is not None:
            raise RuntimeError('model name already registered before this cycle')
        test_kind = entry.get('test_kind', 'text_smoke')
        if test_kind not in ('text_smoke', 'decision_smoke'):
            raise ValueError(f'unsupported_test_kind: {test_kind}')
        deployment = {'name': name, 'base_directory': f"/inputs/{entry['id']}/model",
            'model': {'path': 'model.json', 'preset': entry['selection']['preset'],
                      'programs': entry['selection']['programs']},
            'definition': {'kind': 'decision' if test_kind == 'decision_smoke' else 'text'}}
        # Registration can be accepted even if the client loses its response.
        accepted = True
        with PROGRESS.operation(label + ': load'):
            api.request('/v1/models/register', deployment, accepted=(202,))
        cycle['load'] = phase('passed', None, started)
        stage, started = 'readiness', time.monotonic()
        with PROGRESS.operation(label + ': readiness', timeout=limits.readiness_timeout):
            wait_ready(api, name, cycle, limits.readiness_timeout)
        cycle['readiness'] = phase('passed', None, started)
        stage, started = 'query', time.monotonic()
        request = {**entry['smoke_request'], 'model': name}
        cycle['request_path'] = prefix + '/request.json'
        write_json(output / cycle['request_path'], request)
        with PROGRESS.operation(label + ': query', timeout=limits.query_timeout):
            response = api.request('/v1/systemone' if test_kind == 'decision_smoke' else '/v1/chat/completions',
                                   request, timeout=limits.query_timeout)
        cycle['response_path'] = prefix + '/response.json'
        write_json(output / cycle['response_path'], response)
        cycle['query'] = phase('passed', None, started)
        stage, started = 'assertion', time.monotonic()
        with PROGRESS.operation(label + ': answer check'):
            assert_response(entry, response)
        cycle['assertion'] = phase('passed', None, started)
    except Exception as error:
        cycle[stage] = phase('failed', f'{type(error).__name__}: {error}', started)
        if stage == 'load' and isinstance(error, HttpFailure) and error.status in (400, 422):
            accepted = False
            if api.status(name) is not None:
                accepted = True
            else:
                cycle['unload'] = phase('passed', None, registration_rejected=True)
    finally:
        if accepted:
            started = time.monotonic()
            previous_deadline = api.deadline
            api.deadline = started + limits.unload_timeout + limits.abort_timeout
            reclamation_path = prefix + '/pool-reclamation.json'
            try:
                with PROGRESS.operation(label + ': unload', timeout=limits.unload_timeout + limits.abort_timeout):
                    cycle['unload'] = unregister(api, name, cycle, limits.unload_timeout, limits.abort_timeout)
                    reclaim_idle_pools(api, output / reclamation_path)
                cycle['unload'].update(elapsed_seconds=time.monotonic() - started, evidence=[reclamation_path])
            except Exception as error:
                cycle['unload'] = phase('failed', f'{type(error).__name__}: {error}', started)
                if (output / reclamation_path).is_file():
                    cycle['unload']['evidence'] = [reclamation_path]
            finally:
                api.deadline = previous_deadline
    passed = all(cycle[key]['status'] == 'passed' for key in PHASES)
    model['status'] = 'passed' if passed and model['status'] != 'failed' else 'failed'
    model['reason'] = None if model['status'] == 'passed' else next(
        (cycle[key].get('reason') for key in PHASES if cycle[key]['status'] != 'passed'), model.get('reason'))
    write_json(output / f"models/{entry['id']}/serve-result.json", model)
    PROGRESS.emit(label + (': PASS' if passed else ': FAIL ' + PROGRESS.summarize(model['reason'] or 'model phase failed')))
    return cycle['unload']['status'] == 'passed'


def drain_log(pipe, path):
    CAPTURE.drain_log(pipe, path, MAX_EVIDENCE_BYTES)


def stop_process(process, timeout):
    started = time.monotonic()
    forced = False
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            forced = True
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
    else:
        raise RuntimeError(f'server exited before owned shutdown: {process.returncode}')
    # Joining the process group catches descendants, not just the listener PID.
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return phase('passed', None, started, forced=forced, exit_code=process.returncode)
    os.killpg(process.pid, signal.SIGKILL)
    raise RuntimeError('owned process descendants remained after server shutdown')


def execute_lifecycle(suite, result, output, command, address, limits):
    began = time.monotonic()
    process, thread = None, None
    lifecycle_failed = False
    server = result['server']
    started = time.monotonic()
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True, env={**os.environ, 'PYTHONNOUSERSITE': '1'})
        server['identity'].update(pid=process.pid)
        thread = threading.Thread(target=drain_log, args=(process.stdout, output / 'server.log'), daemon=True)
        thread.start()
        result['evidence'].extend(['server.log', 'server.log.capture.json'])
        api = Api(address, process)
        def ready():
            try:
                return api.request('/readyz').get('status') == 'ready'
            except (OSError, urllib.error.URLError):
                return False
            except HttpFailure as error:
                if error.status == 503:
                    return False
                raise
        with PROGRESS.operation('run shared server startup', timeout=limits.startup_timeout):
            poll(ready, limits.startup_timeout, process)
        if api.status():
            raise RuntimeError('new empty server unexpectedly has model registrations')
        server['start'] = phase('passed', None, started)
        PROGRESS.emit('run: shared server ready')
        for position, (entry, model) in enumerate(zip(suite['entries'], result['models']), 1):
            if entry['preparation']['status'] != 'ready':
                PROGRESS.emit(f"run {position}/{len(suite['entries'])}: {entry.get('selection', {}).get('model_id', entry['id'])}: not run; preparation failed")
                continue
            for number in range(1, limits.cycles + 1):
                safe = run_cycle(api, entry, model, output, number, limits, f"{position}/{len(suite['entries'])}")
                write_json(output / 'suite-result.json', result)
                if not safe:
                    raise RuntimeError('model removal could not be confirmed; remaining entries not run')
                if api.request('/readyz').get('status') != 'ready':
                    raise RuntimeError('server unhealthy after model removal')
                if model['status'] != 'passed':
                    break
    except (Exception, KeyboardInterrupt) as error:
        lifecycle_failed = True
        result['reason'] = f'{type(error).__name__}: {error}'
        PROGRESS.emit('run: FAIL ' + result['reason'])
        if server['start']['status'] != 'passed':
            server['start'] = phase('failed', result['reason'], started)
    finally:
        if process is not None:
            started = time.monotonic()
            try:
                with PROGRESS.operation('run shared server shutdown', timeout=limits.shutdown_timeout):
                    server['shutdown'] = stop_process(process, limits.shutdown_timeout)
            except Exception as error:
                server['shutdown'] = phase('failed', f'{type(error).__name__}: {error}', started)
            if thread is not None:
                thread.join(timeout=5)
            if process.stdout is not None and (thread is None or not thread.is_alive()):
                process.stdout.close()
            server['cleanup'] = phase('passed' if process.poll() is not None and not thread.is_alive() else 'failed',
                None if process.poll() is not None and not thread.is_alive() else 'server/log collection not stopped')
        else:
            server['shutdown'] = phase('not_run', 'server was not started')
            server['cleanup'] = phase('passed', None)
        passed = (not lifecycle_failed and not result.get('inventory_errors') and
                  all(model['status'] == 'passed' for model in result['models']) and
                  all(server[key]['status'] == 'passed' for key in ('start', 'shutdown', 'cleanup')))
        result.update(status='passed' if passed else 'failed', ended_at=now(), elapsed_seconds=time.monotonic() - began)
        if passed:
            result['reason'] = None
        elif result.get('reason') == 'execution not complete':
            result['reason'] = 'one or more model phases or server cleanup did not pass'
        for model in result['models']:
            write_json(output / f"models/{model['id']}/serve-result.json", model)
        write_json(output / 'suite-result.json', result)
    PROGRESS.emit(f"run: {sum(model['status'] == 'passed' for model in result['models'])} passed; "
                  f"{sum(model['status'] == 'failed' for model in result['models'])} failed; "
                  f"{sum(model['status'] in ('not_run', 'incomplete') for model in result['models'])} not run/incomplete; "
                  f"server cleanup {server['cleanup']['status']}")
    return 0 if passed else 1


def sdk_helper():
    spec = importlib.util.spec_from_file_location('sdk_environment', SCRIPT_DIR / 'sdk-environment.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def runtime_identity(identity):
    return {key: identity[key] for key in
            ('dependency_lock_sha256', 'base_image', 'sdk', 'wheel_sha256')}


def input_manifest(prepared, entry):
    path = contained(prepared, entry['inputs_manifest'])
    if digest(path) != entry['inputs_manifest_sha256']:
        raise ValueError('prepared input manifest digest changed')
    return read_json(path)


def verify_asset_reference(model_root, relative, cache_path, identity, verifier=None):
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('invalid installed asset path')
    installed = model_root / relative
    resolved = installed.resolve(strict=True)
    if installed.samefile(cache_path):
        return
    if not resolved.is_relative_to(model_root.resolve(strict=True)):
        raise ValueError('served asset reference escapes the verified model/cache target')
    (verifier or ASSETS.Verifier()).unchanged(resolved, identity['size'], identity['sha256'],
                                             identity.get('installed_verification'))


def verify_prepared(prepared, suite, inside=False):
    if suite.get('schema_version') != 1:
        raise ValueError('unsupported prepared-suite schema')
    runtime = suite['runtime']
    baseline_path = contained(prepared, runtime['sdk_baseline'])
    baseline, _, payloads, identity = sdk_helper().load_baseline(baseline_path)
    if runtime.get('resolved_runtime') != baseline.get('resolved_runtime'):
        raise ValueError('frozen runtime snapshot changed')
    if identity != runtime['sdk_identity'] or identity['base_image'] != runtime['image']:
        raise ValueError('prepared runtime identity changed')
    PIPELINE.validate_frozen_inputs(prepared, suite)
    policy = PIPELINE.validate_policy(prepared, suite)
    payload_root = contained(prepared, runtime['sdk_payloads'])
    helper = sdk_helper()
    verifier = ASSETS.Verifier()
    suite['asset_preflight_verification'] = verifier.stats
    for kind, payload in payloads:
        helper.verify(helper.payload_path(payload_root, kind, payload), payload)
    for name in ('huggingface', 'loom-assets'):
        cache = suite['cache_roots'][name]
        path = Path(cache['container_path'] if inside else cache['host_path'])
        if not path.is_absolute() or not path.is_dir() or cache['container_path'] != '/model-cache/' + name:
            raise ValueError('invalid cache mount mapping')
    for entry in suite['entries']:
        if entry['preparation']['status'] != 'ready':
            continue
        manifest = input_manifest(prepared, entry)
        entry_baseline, _, _, entry_identity = helper.load_baseline(contained(prepared, entry['baseline']))
        test_kind = entry.get('test_kind', 'text_smoke')
        if (test_kind not in ('text_smoke', 'decision_smoke') or
                manifest.get('test_kind', 'text_smoke') != test_kind or
                entry_baseline.get('test_kind', 'text_smoke') != test_kind):
            raise ValueError('prepared service/test kind changed or unsupported')
        if (entry_identity['baseline_sha256'] != entry['baseline_sha256'] or
                entry_identity['dependency_lock_sha256'] != entry['dependency_lock_sha256']):
            raise ValueError('frozen model baseline/dependency identity changed')
        # Model configs differ; their manifests record the shared SDK bootstrap.
        if (manifest['sdk_identity'] != identity or
                runtime_identity(entry_identity) != runtime_identity(identity) or
                manifest['selection'] != entry['selection']):
            raise ValueError('prepared model identity changed')
        if manifest['model_card_path'] != f"/inputs/{entry['id']}/model/model.json":
            raise ValueError('prepared model path does not match serving mount')
        card = contained(prepared, f"models/{entry['id']}/model/model.json")
        if digest(card) != manifest['installed_card_sha256']:
            raise ValueError('prepared installed card changed')
        if manifest['smoke_request'] != entry['smoke_request'] or manifest['smoke_expected_text'] != entry['smoke_expected_text']:
            raise ValueError('prepared request/assertion changed')
        if (entry_baseline['smoke_request'] != entry['smoke_request'] or
                entry_baseline['smoke_expected_text'] != entry['smoke_expected_text'] or
                entry_baseline.get('effective_execution', entry_baseline['published_execution']) != manifest['effective_execution']):
            raise ValueError('prepared workload differs from frozen baseline')
        PIPELINE.verify_upgrade(prepared, entry, manifest, entry_baseline, policy)
        PIPELINE.verify_pipeline_cache(prepared, entry, manifest, entry_baseline,
            required=suite.get('preparation_contract', {}).get('pipeline_source') == 'host_seeded')
        if policy is not None and policy['pipeline_upgrade']['enabled']:
            report = PIPELINE.read_json(PIPELINE.contained(prepared, manifest['pipeline_upgrade']['report_path']))
            expected_implementation = suite['source']['files_sha256']['scripts/upgrade-pipelines-0.1.0-to-0.2.0.py']
            if report['implementation_sha256'] != expected_implementation:
                raise ValueError('pipeline report implementation differs from preparation source')
        if policy is not None and entry.get('effective_execution') != manifest['effective_execution']:
            raise ValueError('prepared executable settings differ from frozen entry')
        for program in manifest['programs']:
            path = contained(prepared, f"models/{entry['id']}/model/{program['path']}")
            if digest(path) != program['sha256']:
                raise ValueError('prepared program bytes changed')
        for asset in manifest['assets']:
            cache = suite['cache_roots'][asset['cache_root']]
            root = Path(cache['container_path'] if inside else cache['host_path'])
            path = contained(root, asset['cache_path'])
            verifier.unchanged(path, asset['size'], asset['sha256'], asset.get('verification'))
            if inside:
                verify_asset_reference(Path(f"/inputs/{entry['id']}/model"), asset['installation_path'], path, asset, verifier)
    return baseline_path, payload_root


def gpu_options(device_uri):
    match = re.fullmatch(r'amdgpu://([0-9]+)', device_uri)
    if match is None:
        raise ValueError('this GPU runner requires an explicit amdgpu://INDEX device URI')
    nodes = sorted(Path('/dev/dri').glob('renderD*'))
    index = int(match.group(1))
    if index >= len(nodes):
        raise ValueError('requested AMD GPU render node is not present')
    devices = [Path('/dev/kfd'), nodes[index]]
    options = []
    for node in devices:
        if not os.access(node, os.R_OK | os.W_OK):
            raise PermissionError(f'GPU device is not readable/writable: {node}')
        options += ['--device', str(node)]
    for gid in sorted({node.stat().st_gid for node in devices}):
        options += ['--group-add', str(gid)]
    hardware = {'device_uri': device_uri, 'render_node': str(nodes[index])}
    properties = Path('/sys/class/drm') / nodes[index].name / 'device/uevent'
    if properties.is_file():
        observed = dict(line.split('=', 1) for line in properties.read_text().splitlines() if '=' in line)
        hardware.update(driver=observed.get('DRIVER'), pci_id=observed.get('PCI_ID'), pci_slot=observed.get('PCI_SLOT_NAME'))
        hardware['name'] = 'AMD GPU (PCI ' + observed.get('PCI_ID', 'unknown') + ')'
    return options, hardware


def owned_container(output):
    token = hashlib.sha256(str(output.resolve()).encode()).hexdigest()
    return 'hrx-test-' + token[:24], token


def cleanup_container(output):
    name, token = owned_container(output)
    listed = subprocess.run(['docker', 'container', 'ls', '--all', '--filter', 'name=' + name,
                             '--format', '{{.Names}}'], capture_output=True, text=True,
                            check=True, timeout=10)
    if name not in listed.stdout.splitlines():
        return
    details = subprocess.run(['docker', 'container', 'inspect', name, '--format',
        '{{.Id}} {{index .Config.Labels "hrx-systems-test.output"}}'], capture_output=True,
        text=True, check=True, timeout=10).stdout.strip().split()
    if len(details) != 2 or details[1] != token:
        raise RuntimeError('refusing cleanup of container without matching output ownership label')
    subprocess.run(['docker', 'rm', '-f', details[0]], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15)
    listed = subprocess.run(['docker', 'container', 'ls', '--all', '--filter', 'name=' + name,
                             '--format', '{{.Names}}'], capture_output=True, text=True,
                            check=True, timeout=10)
    if name in listed.stdout.splitlines():
        raise RuntimeError('owned execution container still exists')


@contextmanager
def runtime_scratch(output):
    work = Path(tempfile.mkdtemp(prefix='.runtime-', dir=output))
    try:
        yield work
    finally:
        # Keep scratch intact if the container may still be borrowing it.
        cleanup_container(output)
        shutil.rmtree(work)


def run_host(args):
    host_began, host_started_at = time.monotonic(), now()
    prepared = args.prepared.resolve(strict=True)
    suite = read_json(prepared / 'prepared-suite.json')
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    result = initial_results(suite, {'device_uri': args.device_uri})
    result['started_at'] = host_started_at
    result['source']['execution_runner_sha256'] = digest(Path(__file__))
    result['source']['execution_helpers_sha256'] = {name: digest(SCRIPT_DIR / name) for name in
        ('asset-verification.py', 'ci-progress.py', 'evidence-capture.py', 'failure-records.py', 'pipeline-preparation.py', 'runtime-policy.py', 'pipeline-cache.py')}
    if args.workflow:
        if args.workflow.stat().st_size > 16384:
            raise ValueError('workflow identity file exceeds its size limit')
        workflow = read_json(args.workflow)
        allowed = {'schema_version', 'repository', 'run_id', 'run_attempt', 'ref', 'sha', 'runner', 'run_url',
                   'event_name', 'schedule', 'schedule_timezone', 'observed_at', 'observed_time_source', 'normalized_inputs', 'lane_id', 'plan_sha256'}
        normalized = workflow.get('normalized_inputs') if isinstance(workflow, dict) else None
        input_keys = {'selection', 'runtime_source', 'device_uri', 'cycles', 'full_integrity',
                      'runtime_run_id', 'runtime_run_attempt', 'runtime_artifact_id', 'runtime_wheel_sha256'}
        if isinstance(workflow, dict) and 'normalized_inputs' in workflow and (not isinstance(normalized, dict) or set(normalized) - input_keys or
                any(not isinstance(value, (str, int, bool)) or len(str(value)) > 2048 for value in normalized.values())):
            raise ValueError('workflow normalized inputs must be bounded scalar execution inputs')
        if (not isinstance(workflow, dict) or set(workflow) - allowed or
                any(not isinstance(value, (str, int)) or len(str(value)) > 2048
                    for key, value in workflow.items() if key != 'normalized_inputs')):
            raise ValueError('workflow metadata must be a bounded identity-only object')
        if 'schema_version' in workflow and (type(workflow['schema_version']) is not int or workflow['schema_version'] != 1):
            raise ValueError('unsupported workflow metadata schema')
        if 'lane_id' in workflow and (not isinstance(workflow['lane_id'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', workflow['lane_id'])):
            raise ValueError('workflow lane ID must be a bounded path-safe identity')
        if 'plan_sha256' in workflow and (not isinstance(workflow['plan_sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', workflow['plan_sha256'])):
            raise ValueError('workflow paired plan identity must be a SHA256')
        write_json(output / 'workflow.json', workflow)
        result['workflow'] = workflow
        result['evidence'].append('workflow.json')
    write_json(output / 'suite-result.json', result)
    name, ownership = owned_container(output)
    result['server']['identity']['container'] = name
    container_started = False
    old = {sig: signal.signal(sig, lambda signum, frame: (_ for _ in ()).throw(KeyboardInterrupt()))
           for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        copy_evidence(prepared, output, suite, result)
        write_json(output / 'suite-result.json', result)
        if not any(entry['preparation']['status'] == 'ready' for entry in suite['entries']):
            result.update(status='failed', reason='no prepared model entries are ready', ended_at=now())
            result['server']['cleanup'] = phase('passed', None)
            return 1
        baseline, payloads = verify_prepared(prepared, suite)
        result['asset_verification']['host_preflight'] = dict(suite.get('asset_preflight_verification', {}))
        device_options, hardware = gpu_options(args.device_uri)
        result['hardware'].update(hardware)
        subprocess.run(['docker', 'image', 'inspect', suite['runtime']['image']],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        with runtime_scratch(output) as work:
            for child in ('home', 'cache', 'tmp'):
                (work / child).mkdir()
            write_json(output / 'suite-result.json', result)
            command = ['docker', 'run', '--rm', '--pull=never', '--name', name, '--read-only',
                '--label', 'hrx-systems-test.output=' + ownership,
                '--platform', suite['runtime']['platform'], '--network', 'none',
                '--user', f'{os.getuid()}:{os.getgid()}', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges', '--env', 'HOME=/run/home',
                '--env', 'XDG_CACHE_HOME=/run/cache', '--env', 'TMPDIR=/run/tmp',
                '--env', 'LOOM_SERVE_DIR=/run/loom',
                '--env', 'PYTHONNOUSERSITE=1', *device_options,
                '--mount', f'type=bind,src={SCRIPT_DIR},dst=/source/scripts,readonly',
                '--mount', f'type=bind,src={prepared},dst=/prepared,readonly',
                '--mount', f"type=bind,src={prepared / 'models'},dst=/inputs,readonly",
                '--mount', f'type=bind,src={work},dst=/run',
                '--mount', f'type=bind,src={output},dst=/results']
            if suite.get('runtime_policy', {}).get('seccomp') is not None:
                profile = next(record for record in suite['policy_files'] if record['kind'] == 'seccomp')
                command += ['--security-opt', 'seccomp=' + str(contained(prepared, profile['path']))]
            for key in ('huggingface', 'loom-assets'):
                command += ['--mount', f"type=bind,src={suite['cache_roots'][key]['host_path']},dst=/model-cache/{key},readonly"]
            command += [suite['runtime']['image'], 'python', '-u', '-I', '/source/scripts/run-prepared-models.py',
                        '--inside-container', '--device-uri', args.device_uri, '--cycles', str(args.cycles)]
            for field in ('startup_timeout', 'readiness_timeout', 'query_timeout', 'unload_timeout', 'abort_timeout', 'shutdown_timeout'):
                command += ['--' + field.replace('_', '-'), str(getattr(args, field))]
            try:
                budget = (300 + args.startup_timeout + args.shutdown_timeout + len(suite['entries']) *
                          args.cycles * (args.readiness_timeout + args.query_timeout +
                                         args.unload_timeout + args.abort_timeout + 30))
                container_started = True
                execution = PROGRESS.run(command, output / 'container.log', label='run execution container', timeout=budget)
            finally:
                cleanup_container(output)
        result = read_json(output / 'suite-result.json')
        result['container_exit_code'] = execution.returncode
        if execution.returncode != 0:
            result.update(status='failed', reason=f'execution container exited with status {execution.returncode}')
    except (Exception, KeyboardInterrupt) as error:
        result = read_json(output / 'suite-result.json')
        result.update(status='failed', reason=f'{type(error).__name__}: {error}', ended_at=now())
        PROGRESS.emit('run: FAIL ' + result['reason'])
        if result['server']['start']['status'] != 'passed':
            result['server']['start'] = phase('failed', result['reason'])
    finally:
        try:
            if container_started:
                cleanup_container(output)
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            result['server']['cleanup'] = phase('failed', f'container cleanup unconfirmed: {error}')
            result['status'] = 'failed'
        result['server']['identity']['container'] = name
        if result.get('elapsed_seconds') is not None:
            result['lifecycle_elapsed_seconds'] = result['elapsed_seconds']
        try:
            CAPTURE.failed_sources(prepared, output, suite, result)
        except (ValueError, OSError, TypeError, KeyError) as error:
            result['failure_capture_error'] = f'{type(error).__name__}: {error}'
            result['status'] = 'failed'
            result['reason'] = result.get('reason') or 'Failure evidence capture failed: ' + str(error)
            PROGRESS.emit('run: FAIL evidence capture: ' + str(error))
        result['ended_at'] = now()
        result['elapsed_seconds'] = time.monotonic() - host_began
        write_json(output / 'suite-result.json', result)
        for sig, previous in old.items():
            signal.signal(sig, previous)
    print(f"Execution {result['status']}: {output / 'suite-result.json'}")
    return int(result['status'] != 'passed')


def inside_container(args):
    suite, prepared, output = read_json('/prepared/prepared-suite.json'), Path('/prepared'), Path('/results')
    result = read_json(output / 'suite-result.json')
    try:
        baseline, payloads = verify_prepared(prepared, suite, inside=True)
        result['asset_verification']['container_preflight'] = dict(suite.get('asset_preflight_verification', {}))
        for path in (Path('/inputs'), Path('/prepared'), Path('/model-cache/huggingface'), Path('/model-cache/loom-assets')):
            if not os.statvfs(path).f_flag & os.ST_RDONLY:
                raise ValueError('immutable execution input mount is writable')
        # Host preflight validates host-side cache paths; container sees fixed mounts.
        with PROGRESS.operation('run SDK installation'):
            subprocess.run([sys.executable, '-u', '-I', '/source/scripts/sdk-environment.py', 'install',
                '--baseline', str(baseline), '--payload-root', str(payloads),
                '--output', '/results/sdk-runtime'], check=True)
        receipt = read_json('/results/sdk-runtime/sdk.json')
        if receipt['identity'] != suite['runtime']['sdk_identity']:
            raise ValueError('serving SDK identity differs from prepared SDK')
        result['evidence'].append('sdk-runtime/sdk.json')
        result['execution_contract'] = {'cache_read_only': True, 'external_network': False}
        if suite.get('preparation_contract', {}).get('pipeline_source') == 'host_seeded':
            result['execution_contract']['pipeline_source'] = 'host_seeded'
        if suite.get('preparation_contract', {}).get('pipeline_fresh') is True:
            result['execution_contract']['pipeline_fresh'] = True
        with socket.socket() as check:
            check.bind(('127.0.0.1', 18080))
        server = ['/run/venv/bin/loom-serve', '--device=' + args.device_uri, '--listen=127.0.0.1:18080']
        if suite.get('runtime_policy') is not None:
            server += PIPELINE.helper('runtime-policy').server_args(suite['runtime_policy'])
            result['execution_contract']['runtime_policy_sha256'] = suite['runtime_policy']['policy_sha256']
        result['server_command'] = server
        return execute_lifecycle(suite, result, output, server,
            'http://127.0.0.1:18080', args)
    except (Exception, KeyboardInterrupt) as error:
        result.update(status='failed', reason=f'{type(error).__name__}: {error}', ended_at=now())
        write_json(output / 'suite-result.json', result)
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepared', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--device-uri', default='amdgpu://0')
    parser.add_argument('--workflow', type=Path, help='optional compact GitHub identity JSON; not required locally')
    parser.add_argument('--cycles', type=int, default=1)
    parser.add_argument('--inside-container', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--cleanup', action='store_true', help='remove only this output directory\'s owned execution container')
    for key, default in (('startup', 180), ('readiness', 180), ('query', 60), ('unload', 30), ('abort', 10), ('shutdown', 10)):
        parser.add_argument('--' + key + '-timeout', type=float, default=default)
    args = parser.parse_args()
    if args.cycles < 1 or any(not math.isfinite(getattr(args, key + '_timeout')) or
            getattr(args, key + '_timeout') <= 0 for key in ('startup', 'readiness', 'query', 'unload', 'abort', 'shutdown')):
        parser.error('cycles and timeouts must be positive')
    if args.inside_container:
        return inside_container(args)
    if args.cleanup:
        if args.output is None:
            parser.error('--cleanup requires --output')
        try:
            cleanup_container(args.output.absolute())
            return 0
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            PROGRESS.emit(f'cleanup: FAIL {type(error).__name__}: {error}')
            print(f'Execution container cleanup unconfirmed: {error}', file=sys.stderr)
            return 1
    if args.prepared is None or args.output is None:
        parser.error('--prepared and --output are required')
    try:
        return run_host(args)
    except (ValueError, OSError, KeyError, TypeError) as error:
        PROGRESS.emit(f'run: FAIL {type(error).__name__}: {error}')
        print(f'Cannot execute prepared suite: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
