"""Read-only physical identity check for explicitly enabled runner labels."""
import argparse
import json
from pathlib import Path
import re

from hrx_systems.host.hardware_advisory import observe_amd


LEGACY_LABEL = 'hrx-systems-test-bootstrap'
TARGETS = {'hrx-gfx1151': 110501}


def check(label, device_uri='amdgpu://0', *, sysfs=Path('/sys/class/drm'),
          devices=Path('/dev/dri'), kfd=Path('/sys/class/kfd/kfd/topology/nodes')):
    """Return bounded identity evidence; no runtime mapping/memory qualification."""
    result = dict(requested_label=label, device_uri=device_uri, render_node=None,
                  gfx_target_version=None, status='failed', reason=None)
    try:
        if not isinstance(label, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', label):
            raise ValueError('expected one lowercase runner label')
        if label == LEGACY_LABEL:
            result.update(status='legacy', reason='physical identity check not enabled for legacy label')
            return result
        if label not in TARGETS:
            raise ValueError('unsupported hardware label')
        if device_uri != 'amdgpu://0':
            raise ValueError('only amdgpu://0 is supported')
        if len(list(devices.glob('renderD*'))) != 1:
            raise ValueError('expected exactly one render device')
        nodes = list(kfd.glob('[0-9]*'))
        if not nodes or len(nodes) > 64:
            raise ValueError('missing or oversized KFD topology')
        gpu_ids = []
        for node in nodes:
            with (node / 'gpu_id').open() as stream:
                gpu_id = stream.read(128).strip()
            if not gpu_id.isdecimal():
                raise ValueError('unreadable KFD GPU identity')
            if int(gpu_id):
                gpu_ids.append(int(gpu_id))
        if len(gpu_ids) != 1:
            raise ValueError('expected exactly one nonzero KFD GPU identity')
        observed = observe_amd(device_uri, sysfs, devices, kfd)
        result['render_node'] = observed['render_node']
        mapping = observed['kernel_device_mapping']
        if observed['driver'] != 'amdgpu' or not mapping:
            raise ValueError('unreadable or ambiguous AMD render/KFD identity')
        result['gfx_target_version'] = mapping['gfx_target_version']
        if mapping['gfx_target_version'] != TARGETS[label]:
            raise ValueError('physical gfx target does not match requested label')
        result.update(status='passed', reason='physical AMD render/KFD identity matches')
    except (OSError, ValueError) as error:
        result['reason'] = str(error)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', required=True)
    parser.add_argument('--device-uri', default='amdgpu://0')
    args = parser.parse_args(argv)
    result = check(args.label, args.device_uri)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] in ('passed', 'legacy') else 1
