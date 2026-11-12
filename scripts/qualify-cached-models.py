#!/usr/bin/env python3
"""Produce an advisory hardware report without changing cache membership or running models."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import re
import shutil
import sys

FIELDS = ('repository', 'publication_revision', 'model_id', 'preset', 'variant', 'programs')
MAX_JSON = 4 * 1024 * 1024
OUTPUT_HEADROOM = 8 * 1024 * 1024


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def read_json(path):
    path = Path(path)
    require(not path.is_symlink() and path.is_file() and path.stat().st_size <= MAX_JSON,
            f'missing, symlink or oversized JSON: {path.name}')
    value = json.loads(path.read_text())
    require(isinstance(value, dict), f'expected JSON object: {path.name}')
    return value


def contained(root, relative):
    require(isinstance(relative, str) and relative and '\\' not in relative, 'invalid prepared reference')
    path = Path(relative)
    require(not path.is_absolute() and all(part not in ('.', '..') for part in relative.split('/')),
            'prepared reference escapes root')
    target = root
    for part in path.parts:
        target /= part
        require(not target.is_symlink(), 'prepared reference contains a symlink')
    require(target.resolve().is_relative_to(root.resolve()), 'prepared reference escapes root')
    return target


def runtime_identity(runtime):
    require(isinstance(runtime, dict), 'prepared runtime must be an object')
    if 'resolved_runtime' in runtime:
        resolved = runtime['resolved_runtime']
        require(isinstance(resolved, dict), 'resolved runtime identity must be an object')
        identity = resolved.get('runtime_identity_sha256')
        require(isinstance(identity, str) and re.fullmatch('[0-9a-f]{64}', identity),
                'resolved runtime identity is missing or invalid')
        return identity
    legacy = runtime.get('sdk_identity')
    require(isinstance(legacy, dict) and all(key in legacy for key in
            ('dependency_lock_sha256', 'base_image', 'sdk', 'wheel_sha256')),
            'prepared runtime identity unavailable')
    return digest({key: legacy[key] for key in
                   ('dependency_lock_sha256', 'base_image', 'sdk', 'wheel_sha256')})


def workload_identity(entry, cycles):
    return digest({key: entry.get(key) for key in
                   ('effective_execution', 'smoke_request', 'test_kind')} | {'cycles': cycles})


def provenance(value):
    return isinstance(value, dict) and all(isinstance(value.get(key), str) and value[key].strip()
                                         for key in ('method', 'reference', 'reviewer', 'observed_at'))


def integer(value):
    return type(value) is int and value >= 0


def observe_amd(device_uri, sysfs=Path('/sys/class/drm'), devices=Path('/dev/dri'),
                kfd=Path('/sys/class/kfd/kfd/topology/nodes')):
    """Observe bounded Linux AMD facts; sysfs ordering is not an SDK enumeration proof."""
    match = re.fullmatch(r'amdgpu://([0-9]+)', device_uri)
    require(match is not None, 'expected amdgpu://INDEX')
    result = {'device_uri': device_uri, 'observed_at': datetime.now(timezone.utc).isoformat(),
              'method': 'linux_drm_sysfs', 'render_node': None, 'pci_slot': None, 'driver': None,
              'mapping_qualified': False, 'canonical_target': None, 'memory_topology': 'unknown',
              'driver_version': platform.release(), 'kernel_device_mapping': None,
              'usable_total_bytes': None, 'usable_free_bytes': None, 'memory_qualified': False,
              'observed_counters': {}, 'evidence': [], 'gaps': [
                  'selected_sdk_device_mapping_unavailable', 'selected_sdk_target_catalog_unavailable',
                  'gpu_usable_memory_pool_unqualified']}
    nodes = sorted(devices.glob('renderD*'))
    index = int(match[1])
    if index >= len(nodes):
        result['gaps'].append('render_node_unavailable')
        return result
    node = nodes[index]
    result['render_node'] = str(node)
    device = sysfs / node.name / 'device'
    try:
        path = device / 'uevent'
        with path.open() as stream:
            content = stream.read(16385)
        require(len(content) <= 16384, 'oversized device uevent')
        properties = dict(line.split('=', 1) for line in content.splitlines() if '=' in line)
        result.update(driver=properties.get('DRIVER'), pci_slot=properties.get('PCI_SLOT_NAME'),
                      pci_id=properties.get('PCI_ID'))
        result['evidence'].append(str(path))
    except (OSError, ValueError) as error:
        result['gaps'].append(f'device_properties_unavailable: {error}')
    if result['driver'] != 'amdgpu':
        result['gaps'].append('selected_render_node_is_not_observed_amdgpu')
        return result
    for name in ('mem_info_vram_total', 'mem_info_vram_used', 'mem_info_gtt_total', 'mem_info_gtt_used'):
        try:
            path = device / name
            with path.open() as stream:
                value = stream.read(128).strip()
            if value.isdecimal():
                result['observed_counters'][name] = int(value)
                result['evidence'].append(str(path))
        except OSError:
            pass
    # Linux exports raw target version and render minor. Neither is an SDK
    # target normalization or runtime enumeration contract.
    matches = []
    for node in sorted(kfd.glob('[0-9]*'))[:64]:
        try:
            path = node / 'properties'
            with path.open() as stream:
                content = stream.read(16385)
            require(len(content) <= 16384, 'oversized KFD properties')
            fields = dict(line.split(None, 1) for line in content.splitlines() if len(line.split(None, 1)) == 2)
            if fields.get('drm_render_minor') != Path(result['render_node']).name.removeprefix('renderD'):
                continue
            with (node / 'gpu_id').open() as stream:
                gpu_id = stream.read(128).strip()
            if not gpu_id.isdecimal() or int(gpu_id) == 0:
                continue
            matches.append({'node': node.name, 'gpu_id': int(gpu_id),
                            'drm_render_minor': int(fields['drm_render_minor']),
                            'gfx_target_version': int(fields['gfx_target_version']) if fields.get('gfx_target_version', '').isdecimal() else None})
            result['evidence'].append(str(path))
        except (OSError, ValueError):
            pass
    if len(matches) == 1:
        result['kernel_device_mapping'] = matches[0]
    elif len(matches) > 1:
        result['gaps'].append('ambiguous_kernel_render_node_mapping')
    return result


def qualify_observation(observed, saved, identity, *, now=None, max_age_seconds=300):
    """Use only explicitly reviewed SDK-bound mapping/pool/catalog evidence."""
    if saved is None:
        return observed
    require(saved.get('schema_version') == 1 and saved.get('runtime_identity') == identity,
            'SDK observation belongs to a different runtime')
    for key in ('device_uri', 'pci_slot', 'driver', 'driver_version'):
        require(saved.get(key) and saved[key] == observed.get(key), f'SDK observation {key} differs from device')
    require(saved.get('mapping_qualified') is True and provenance(saved.get('provenance')),
            'SDK observation needs reviewed device mapping provenance')
    require(isinstance(saved.get('canonical_target'), str) and saved['canonical_target'],
            'SDK observation has no canonical target')
    require(saved.get('memory_topology') in ('dedicated', 'shared', 'unknown'), 'invalid memory topology')
    result = dict(observed)
    result.update({key: saved.get(key) for key in
                   ('canonical_target', 'memory_topology', 'memory_qualified', 'usable_total_bytes',
                    'usable_free_bytes', 'target_catalog', 'provenance')})
    result.update(mapping_qualified=True, gaps=[])
    result['evidence'] = [*observed.get('evidence', []), saved['provenance']['reference']]
    if result.get('memory_qualified') is True:
        total, free = result.get('usable_total_bytes'), result.get('usable_free_bytes')
        require(integer(total) and (free is None or integer(free) and free <= total), 'invalid qualified usable-memory pool')
        require(provenance(saved.get('memory_provenance')), 'usable-memory pool needs reviewed provenance')
        result['memory_provenance'] = saved['memory_provenance']
        try:
            recorded = datetime.fromisoformat(saved['memory_provenance']['observed_at'].replace('Z', '+00:00'))
            require(recorded.tzinfo is not None, 'memory observation needs a UTC offset')
            age = ((now or datetime.now(timezone.utc)) - recorded).total_seconds()
            require(0 <= age <= max_age_seconds, 'memory free observation is stale or future-dated')
        except ValueError:
            result['usable_free_bytes'] = None
            result['gaps'].append('memory_free_observation_stale_or_invalid')
    else:
        result.update(usable_total_bytes=None, usable_free_bytes=None)
        result['gaps'].append('gpu_usable_memory_pool_unqualified')
    return result


def check(status, reason, evidence=()):
    return {'status': status, 'reason': reason, 'evidence': list(evidence)}


def select_profile(entry, profiles, identity, hardware, cycles):
    matching = [profile for profile in profiles if profile.get('selection') == entry.get('selection')]
    exact = [p for p in matching if p.get('runtime_identity') == identity
             and p.get('workload_identity') == workload_identity(entry, cycles)
             and p.get('hardware') == {k: hardware.get(k) for k in
                                      ('canonical_target', 'driver', 'driver_version', 'memory_topology')}]
    require(len(exact) <= 1, 'duplicate exact qualification profiles')
    if not exact:
        return None, 'profile_stale_or_missing' if matching else 'profile_missing'
    require(provenance(exact[0].get('provenance')), 'profile lacks review/method/evidence/date')
    return exact[0], None


def pipeline_check(manifest, profile, hardware, identity):
    if not manifest:
        return check('unknown', 'prepared_program_manifest_unavailable')
    programs = manifest.get('programs')
    if not isinstance(programs, list) or not programs:
        return check('unknown', 'target_metadata_unavailable')
    if not profile:
        return check('unknown', 'reviewed_program_targets_missing')
    requirements = profile.get('programs')
    if not isinstance(requirements, list):
        return check('unknown', 'reviewed_program_targets_missing')
    require(all(isinstance(p, dict) and isinstance(p.get('path'), str) and p['path'] for p in programs),
            'invalid prepared program paths')
    require(all(isinstance(p, dict) and isinstance(p.get('path'), str) and p['path'] for p in requirements),
            'invalid reviewed program paths')
    paths = [p['path'] for p in programs]
    required = [p['path'] for p in requirements]
    require(len(set(paths)) == len(paths), 'duplicate prepared program paths')
    require(len(set(required)) == len(required), 'duplicate program requirements')
    if set(required) - set(paths):
        return check('fail', 'required_pipeline_program_missing', [profile['provenance']['reference']])
    if set(paths) - set(required):
        return check('unknown', 'program_target_coverage_incomplete')
    catalog = hardware.get('target_catalog')
    if (not hardware.get('mapping_qualified') or not isinstance(catalog, dict)
            or catalog.get('runtime_identity') != identity or not provenance(catalog.get('provenance'))
            or catalog.get('canonical_target') != hardware.get('canonical_target')):
        return check('unknown', 'selected_sdk_target_catalog_unavailable')
    supported = catalog.get('compatible_targets')
    if not isinstance(supported, list):
        return check('unknown', 'selected_sdk_target_catalog_unavailable')
    for program in requirements:
        if program.get('target_independent') is True:
            continue
        alternatives = program.get('targets')
        if not isinstance(alternatives, list) or not alternatives:
            return check('unknown', 'program_targets_unknown')
        matched = False
        for requirement in alternatives:
            require(isinstance(requirement, dict) and isinstance(requirement.get('target'), str)
                    and isinstance(requirement.get('features', []), list)
                    and all(isinstance(f, str) for f in requirement.get('features', [])), 'invalid target requirement')
            for support in supported:
                require(isinstance(support, dict) and isinstance(support.get('target'), str)
                        and isinstance(support.get('features', []), list)
                        and all(isinstance(f, str) for f in support.get('features', [])), 'invalid SDK target catalog')
                if (support['target'] == requirement['target']
                        and set(requirement.get('features', [])) <= set(support.get('features', []))):
                    matched = True
        if not matched:
            return check('fail' if catalog.get('complete') is True else 'unknown',
                         'pipeline_target_incompatible' if catalog.get('complete') is True else 'catalog_coverage_unknown',
                         [catalog['provenance']['reference']])
    return check('pass', 'all_program_targets_compatible',
                 [profile['provenance']['reference'], catalog['provenance']['reference']])


def memory_check(profile, hardware, reserve):
    if not profile:
        return check('unknown', 'reviewed_memory_profile_missing'), None, None
    memory = profile.get('memory')
    if not isinstance(memory, dict) or not integer(memory.get('peak_bytes')) or not integer(memory.get('margin_bytes')):
        return check('unknown', 'reviewed_memory_requirement_missing'), None, None
    require(provenance(memory.get('provenance')), 'memory requirement lacks reviewed provenance')
    require(isinstance(memory.get('components'), dict) and all(integer(memory['components'].get(key))
            for key in ('weights', 'context_state', 'companions', 'workspace_runtime'))
            and all(integer(value) for value in memory['components'].values())
            and sum(memory['components'].values()) <= memory['peak_bytes'],
            'memory profile needs consistent component accounting')
    required = memory['peak_bytes'] + memory['margin_bytes']
    if (hardware.get('memory_qualified') is not True or not integer(hardware.get('usable_total_bytes'))
            ):
        return check('unknown', 'shared_pool_budget_unqualified' if hardware.get('memory_topology') == 'shared'
                     else 'gpu_usable_memory_pool_unqualified'), required, None
    total, free = hardware['usable_total_bytes'], hardware['usable_free_bytes']
    require(free is None or integer(free) and free <= total, 'conflicting usable-memory evidence')
    if required + reserve > total:
        return check('fail', 'insufficient_capacity', [memory['provenance']['reference']]), required, False
    if free is None:
        return check('pass', 'capacity_fit_free_memory_unqualified', [memory['provenance']['reference']]), required, None
    if required + reserve > free:
        return check('pass', 'busy', [memory['provenance']['reference']]), required, False
    return check('pass', 'within_qualified_memory_budget', [memory['provenance']['reference']]), required, True


def evaluate(suite, profiles, hardware, manifests, *, cycles=2, reserve=0,
             storage_free=None, output_headroom=OUTPUT_HEADROOM):
    identity = runtime_identity(suite['runtime'])
    entries = suite.get('entries')
    require(suite.get('schema_version') == 1 and isinstance(entries, list), 'invalid prepared suite')
    ids = [e.get('id') for e in entries if isinstance(e, dict)]
    require(len(ids) == len(entries) and len(set(ids)) == len(ids)
            and all(isinstance(i, str) and i for i in ids), 'invalid/duplicate prepared entries')
    require(profiles.get('schema_version') == 1 and isinstance(profiles.get('profiles'), list), 'invalid profile document')
    require(all(isinstance(p, dict) for p in profiles['profiles']), 'profiles must be objects')
    require(type(cycles) is int and cycles > 0 and integer(reserve) and integer(output_headroom), 'invalid workload/budget')
    report = {'schema_version': 1, 'mode': 'advisory', 'attempt_id': suite.get('attempt_id'),
              'inventory_sha256': suite.get('inventory_sha256'), 'runtime_identity': identity,
              'hardware': hardware, 'cycles': cycles, 'runner_reserve_bytes': reserve,
              'storage_free_bytes': storage_free, 'output_headroom_bytes': output_headroom,
              'inventory_errors': suite.get('inventory_errors', []), 'entries': []}
    for entry in entries:
        profile, gap = select_profile(entry, profiles['profiles'], identity, hardware, cycles)
        manifest = manifests.get(entry['id'])
        require(isinstance(entry.get('preparation'), dict), 'prepared entry lacks preparation evidence')
        closure = (entry['preparation'].get('status') == 'ready' and manifest is not None
                   and isinstance(manifest.get('assets'), list) and bool(manifest['assets']))
        storage = (check('unknown', 'verified_prepared_asset_closure_unavailable') if not closure else
                   check('unknown', 'output_storage_observation_unavailable') if not integer(storage_free) else
                   check('fail', 'insufficient_output_storage') if storage_free < output_headroom else
                   check('pass', 'verified_assets_already_cached; output_headroom_available'))
        memory, required, ready = memory_check(profile, hardware, reserve)
        runtime = check('unknown', 'compatibility_not_qualified')
        if profile:
            runtime_facts = profile.get('runtime_compatibility', {})
            if isinstance(runtime_facts, dict) and runtime_facts.get('status') in ('pass', 'fail') and provenance(runtime_facts.get('provenance')):
                runtime = check(runtime_facts['status'], 'reviewed_exact_runtime_compatibility',
                                [runtime_facts['provenance']['reference']])
        checks = {'pipeline': pipeline_check(manifest, profile, hardware, identity),
                  'memory': memory, 'storage': storage, 'runtime': runtime}
        states = [item['status'] for item in checks.values()]
        recommendation = 'skip' if 'fail' in states else 'needs_information' if 'unknown' in states else 'seed'
        report['entries'].append({'id': entry['id'], 'selection': entry.get('selection'),
            'workload_identity': workload_identity(entry, cycles), 'recommendation': recommendation,
            'runnable_now': ready if recommendation == 'seed' else False if recommendation == 'skip' else None,
            'checks': checks, 'required_bytes': required, 'profile_reference': profile['provenance']['reference'] if profile else None,
            'profile_gap': gap})
    report['counts'] = {kind: sum(entry['recommendation'] == kind for entry in report['entries'])
                        for kind in ('seed', 'skip', 'needs_information')}
    return report


def render_markdown(report):
    def cell(value):
        return re.sub(r'([\\`|\[\]<>*_])', r'\\\1', ' '.join(str(value).split()))
    lines = ['# Hardware advisory', '', '**Advisory only: this report does not select or suppress model tests.**', '',
             f"Runtime identity: `{report['runtime_identity']}`", '',
             '| Model | Recommendation | Ready now | Explanation |', '|---|---|---|---|']
    for entry in report['entries']:
        reasons = '; '.join(f'{name}: {facts["reason"]}' for name, facts in entry['checks'].items())
        lines.append(f"| {cell((entry.get('selection') or {}).get('model_id', entry['id']))} | {entry['recommendation']} | {entry['runnable_now']} | {cell(reasons)} |")
    lines += ['', 'Usable GPU memory stays unknown without qualified pool evidence. Raw VRAM/GTT counters are not added together.', '']
    return '\n'.join(lines)


def generate(prepared, output, *, profiles_path=None, observation_path=None, device_uri='amdgpu://0',
             cycles=2, reserve=0, output_headroom=OUTPUT_HEADROOM):
    prepared, output = Path(prepared).resolve(strict=True), Path(output).absolute()
    require(not output.exists() and not output.is_symlink(), 'output must be a new directory')
    require(not output.resolve().is_relative_to(prepared), 'output must be outside immutable prepared inputs')
    suite = read_json(prepared / 'prepared-suite.json')
    profiles = read_json(profiles_path) if profiles_path else {'schema_version': 1, 'profiles': []}
    identity = runtime_identity(suite['runtime'])
    hardware = qualify_observation(observe_amd(device_uri), read_json(observation_path) if observation_path else None, identity)
    manifests = {}
    for entry in suite.get('entries', []):
        try:
            manifest_path = contained(prepared, entry['inputs_manifest'])
            manifest = read_json(manifest_path)
            require(hashlib.sha256(manifest_path.read_bytes()).hexdigest() == entry['inputs_manifest_sha256'],
                    'prepared manifest digest differs')
            require(manifest.get('selection') == entry.get('selection'), 'prepared manifest selection differs')
            manifests[entry['id']] = manifest
        except (KeyError, OSError, ValueError):
            pass
    parent = output.parent
    while not parent.exists():
        parent = parent.parent
    report = evaluate(suite, profiles, hardware, manifests, cycles=cycles, reserve=reserve,
                      storage_free=shutil.disk_usage(parent).free, output_headroom=output_headroom)
    output.mkdir(parents=True)
    (output / 'hardware-selection-report.json').write_text(json.dumps(report, indent=2) + '\n')
    (output / 'summary.md').write_text(render_markdown(report))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepared', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--profiles', type=Path, help='Reviewed version/workload/hardware-bound profiles; default none')
    parser.add_argument('--observation', type=Path, help='Reviewed saved selected-SDK device/pool/catalog observation')
    parser.add_argument('--device-uri', default='amdgpu://0')
    parser.add_argument('--cycles', type=int, default=2)
    parser.add_argument('--runner-reserve-bytes', type=int, default=0)
    parser.add_argument('--output-headroom-bytes', type=int, default=OUTPUT_HEADROOM)
    args = parser.parse_args()
    try:
        report = generate(args.prepared, args.output, profiles_path=args.profiles, observation_path=args.observation,
                          device_uri=args.device_uri, cycles=args.cycles, reserve=args.runner_reserve_bytes,
                          output_headroom=args.output_headroom_bytes)
        print(f"Hardware advisory: {len(report['entries'])} selections; {report['counts']}; {args.output}")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f'Hardware advisory failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
