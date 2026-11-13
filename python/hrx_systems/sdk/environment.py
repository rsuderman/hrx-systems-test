#!/usr/bin/env python3
"""Private SDK payload acquisition and isolated-container installation helper.

This never fetches model assets or starts a server. Public model/run wrappers
will call these operations once their respective implementation tasks land.
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request


class SdkError(Exception):
    def __init__(self, message, kind="configuration"):
        super().__init__(message)
        self.kind = kind


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def validate_payload(payload):
    name = payload["filename"]
    if not name.endswith(".whl") or Path(name).name != name or "\\" in name:
        raise SdkError("payload must name a single wheel file")
    if not re.fullmatch(r"[0-9a-f]{64}", payload["sha256"]):
        raise SdkError("payload requires a lowercase SHA256 digest")
    if type(payload["size_bytes"]) is not int or payload["size_bytes"] <= 0:
        raise SdkError("payload requires a positive integer size")
    url = urllib.parse.urlsplit(payload["url"])
    # Loopback HTTP exists solely for tiny local fixture transports.
    if url.scheme != "https" and not (
            url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1")):
        raise SdkError("payload URL must use HTTPS")
    if url.username or url.password or url.fragment:
        raise SdkError("payload URLs must not contain credentials or fragments")


def verify(path, payload):
    if not path.is_file() or path.is_symlink():
        raise SdkError(f"missing/nonregular payload: {payload['filename']}", "integrity")
    if path.stat().st_size != payload["size_bytes"] or sha256(path) != payload["sha256"]:
        raise SdkError(f"payload integrity mismatch: {payload['filename']}", "integrity")


def payload_path(root, kind, payload):
    validate_payload(payload)
    if kind not in ("hrx", "python"):
        raise SdkError("invalid SDK payload kind")
    return root / kind / payload["filename"]


def acquire_payload(root, kind, payload):
    target = payload_path(root, kind, payload)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise SdkError("SDK acquisition requires fresh per-attempt payload storage")
    descriptor, name = tempfile.mkstemp(prefix=".partial-", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            req = urllib.request.Request(payload["url"], headers={"User-Agent": "hrx-systems-test-sdk/1"})
            with urllib.request.urlopen(req, timeout=60) as response:
                count = 0
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > payload["size_bytes"]:
                        raise SdkError(f"payload exceeds pinned size: {payload['filename']}", "integrity")
                    output.write(chunk)
        verify(temporary, payload)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"kind": kind, "filename": payload["filename"],
            "sha256": payload["sha256"], "verified_bytes": payload["size_bytes"],
            "downloaded_bytes": count}


def runtime_baseline(settings, snapshot=None):
    """Compose the native adapter only after a concrete wheel has been selected."""
    source = settings['sdk'].get('source')
    if source is None:
        return copy.deepcopy(settings)
    if snapshot is not None:
        wheel = snapshot['wheel']
        resolved = snapshot['source']
        descriptor = {k: wheel[k] for k in ('filename', 'sha256', 'size_bytes')}
        descriptor['url'] = wheel['source_url']
        sdk = {'name': wheel['name'], 'version': wheel['version'],
               'repository': resolved['repository'], 'release': resolved.get('release'),
               'source_commit': resolved['tested_sha'], 'asset_id': resolved.get('asset_id'),
               'wheel': descriptor}
    elif source['kind'] == 'release':
        sdk = {k: copy.deepcopy(v) for k, v in source.items() if k != 'kind'}
        sdk['name'] = settings['sdk']['name']
    else:
        raise SdkError('Actions runtime has not been resolved to a verified wheel')
    sdk['dependency_lock'] = settings['sdk']['dependency_lock']
    return {'schema_version': 1, 'container': copy.deepcopy(settings['container']), 'sdk': sdk}


def load_runtime(path):
    """Validate source-neutral authored inputs; no downloads or source resolution."""
    path = Path(path).resolve(strict=True)
    settings = json.loads(path.read_text())
    if settings.get('schema_version') != 1:
        raise SdkError('unsupported runtime schema')
    image = settings['container']['image']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[0-9a-f]{64}', image):
        raise SdkError('base container must be pinned by digest')
    sdk = settings['sdk']
    lock_name = sdk['dependency_lock']
    if Path(lock_name).name != lock_name or '\\' in lock_name:
        raise SdkError('dependency lock must be adjacent to runtime config')
    lock_path = path.parent / lock_name
    lock = json.loads(lock_path.read_text())
    if lock.get('schema_version') != 1 or not isinstance(lock.get('wheels'), list):
        raise SdkError('unsupported runtime dependency lock')
    names = set()
    for wheel in lock['wheels']:
        validate_payload(wheel)
        if wheel['filename'] in names: raise SdkError('duplicate locked wheel filename')
        names.add(wheel['filename'])
    source = sdk.get('source')
    if source is None:
        # Explicit compatibility input: a legacy full baseline supplies runtime only.
        source = {'kind': 'release', **{k: v for k, v in sdk.items() if k not in ('name', 'dependency_lock')}}
        settings = {'schema_version': 1, 'container': settings['container'],
                    'sdk': {'name': sdk['name'], 'dependency_lock': lock_name, 'source': source}}
    if source.get('kind') == 'release':
        validate_payload(source['wheel'])
        for key in ('repository', 'release', 'source_commit', 'version', 'asset_id'):
            if key not in source: raise SdkError('release runtime lacks ' + key)
        if source['wheel']['filename'] in names: raise SdkError('duplicate locked wheel filename')
    elif source.get('kind') == 'github_actions':
        if (source.get('repository') != 'ROCm/hrx-system' or source.get('branch') != 'main'
                or source.get('mode') not in ('latest-successful', 'replay')
                or source.get('artifact_name') != 'loom-sdk-wheel-linux-Release'):
            raise SdkError('unsupported Actions source selector')
    else:
        raise SdkError('unknown runtime source kind')
    inputs = {'runtime_config_sha256': sha256(path), 'dependency_lock_sha256': sha256(lock_path)}
    settings['_dependency_lock'], settings['_input_identity'] = copy.deepcopy(lock), inputs
    return settings, lock, [('python', p) for p in lock['wheels']], inputs


def runtime_identity_sha256(snapshot):
    """Hash concrete runtime identity, excluding observation times and model data."""
    semantic = {'container': snapshot['container'],
        'source': {key: snapshot['source'].get(key) for key in
                   ('kind', 'repository', 'tested_sha', 'release', 'asset_id', 'workflow_path', 'run_id', 'run_attempt')},
        'artifact_id': (snapshot['source'].get('artifact') or {}).get('id'),
        'wheel': {key: snapshot['wheel'][key] for key in ('filename', 'name', 'version', 'size_bytes', 'sha256')},
        'dependency_lock_sha256': snapshot['dependencies']['lock_sha256']}
    return hashlib.sha256(json.dumps(semantic, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def resolve_runtime(settings, selector=None, destination=None):
    """Freeze configured release identity; transfer/install evidence is separate."""
    inputs = {}
    if isinstance(settings, (str, Path)):
        settings, lock, _, inputs = load_runtime(Path(settings))
    else:
        settings = copy.deepcopy(settings)
        lock = settings.pop('_dependency_lock', None)
        inputs = settings.pop('_input_identity', {})
    if selector is not None:
        settings['sdk']['source'] = copy.deepcopy(selector)
    source = settings['sdk']['source']
    if source['kind'] != 'release':
        raise SdkError('Actions source requires the Actions resolver extension')
    stamp = datetime.now(timezone.utc).isoformat()
    snapshot = {'schema_version': 1, 'container': copy.deepcopy(settings['container']),
        'source': {'kind': 'release', 'repository': source['repository'],
            'requested_head_sha': None, 'tested_sha': source['source_commit'],
            'relation_to_head': 'not_applicable', 'requested_at': stamp,
            'build_created_at': None, 'build_completed_at': None, 'build_age_seconds': None,
            'release': source['release'], 'asset_id': source['asset_id'], 'workflow_path': None,
            'run_id': None, 'run_attempt': None, 'run_url': None, 'artifact': None},
        'wheel': {'filename': source['wheel']['filename'], 'name': settings['sdk']['name'],
            'version': source['version'], 'size_bytes': source['wheel']['size_bytes'],
            'sha256': source['wheel']['sha256'], 'source_url': source['wheel']['url'],
            'source_commit_evidence': 'configured_release_pin'},
        'dependencies': {'lock_sha256': inputs.get('dependency_lock_sha256'),
            'wheels': copy.deepcopy(lock['wheels']) if lock else [], 'compatibility_status': 'not_checked'},
        'inputs': inputs}
    snapshot['runtime_identity_sha256'] = runtime_identity_sha256(snapshot)
    if destination is not None:
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        write_json(Path(destination), snapshot)
    return snapshot


def acquire_actions_runtime(settings, payload_root, *, selector, environment,
                            qualifier=None, client=None, observe_source=None, selection=None):
    """Stage one selected wheel and locked dependencies; never redownload the wheel."""
    from . import actions
    payload_root = Path(payload_root)
    if payload_root.exists() or payload_root.is_symlink():
        raise SdkError('Actions payload root must be new per attempt')
    payload_root.mkdir(parents=True)
    releases = None
    if selection is not None or selector.get('kind') == 'release_latest':
        from . import releases
        frozen = (releases.load_selection(selection) if selection is not None else
                  releases.freeze_selection(selector, client=client, observe_request=observe_source))
        if frozen['selector'] != selector:
            raise SdkError('frozen runtime selector disagrees with requested selector')
        source = frozen['source']
    else:
        source = actions.resolve_source(selector, client=client, observe_request=observe_source)
    if observe_source is not None:
        observe_source(source)
    lock = settings['_dependency_lock']
    acquire = releases.acquire_wheel if source['kind'] == 'release' else actions.acquire_wheel
    acquired = acquire(source, payload_root / 'hrx', dependency_lock=lock,
                                     environment=environment, client=client, qualifier=qualifier)
    events = [{'kind': 'hrx', 'filename': acquired['wheel']['filename'],
               'sha256': acquired['wheel']['sha256'], 'verified_bytes': acquired['wheel']['size_bytes'],
               'downloaded_bytes': (acquired['source']['artifact']['size_bytes'] if acquired['source']['kind'] == 'github_actions'
                                    else acquired['wheel']['size_bytes'])}]
    events.extend(acquire_payload(payload_root, 'python', item) for item in lock['wheels'])
    if qualifier is None:
        actions.validate_dependencies(acquired['metadata'], lock, environment=environment,
                                      dependency_directory=payload_root / 'python')
    else:
        qualifier(Path(acquired['staged_path']), lock, environment,
                  dependency_directory=payload_root / 'python')
    snapshot = {'schema_version': 1, 'container': copy.deepcopy(settings['container']),
                'source': acquired['source'], 'wheel': acquired['wheel'],
                'dependencies': {'lock_sha256': settings['_input_identity']['dependency_lock_sha256'],
                                 'wheels': copy.deepcopy(lock['wheels']),
                                 'compatibility_status': 'locked_metadata_compatible_pending_offline_install'},
                'inputs': copy.deepcopy(settings['_input_identity'])}
    snapshot['runtime_identity_sha256'] = runtime_identity_sha256(snapshot)
    return snapshot, events


def load_baseline(path):
    baseline = json.loads(path.read_text())
    if "source" in baseline.get("sdk", {}):
        baseline = runtime_baseline(baseline)
    sdk = baseline["sdk"]
    image = baseline["container"]["image"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[0-9a-f]{64}", image):
        raise SdkError("base container must be pinned by digest")
    lock_name = sdk["dependency_lock"]
    if Path(lock_name).name != lock_name:
        raise SdkError("dependency lock must be adjacent to the baseline")
    lock_path = path.parent / lock_name
    lock = json.loads(lock_path.read_text())
    if lock["schema_version"] != 1 or baseline["schema_version"] != 1:
        raise SdkError("unsupported baseline/dependency lock version")
    if 'resolved_runtime' in baseline:
        snapshot = baseline['resolved_runtime']
        if not isinstance(snapshot, dict):
            raise SdkError('frozen runtime snapshot must be an object')
        if any(key not in snapshot for key in ('container', 'source', 'wheel', 'dependencies', 'runtime_identity_sha256')):
            raise SdkError('frozen runtime snapshot is incomplete')
        if snapshot.get('runtime_identity_sha256') != runtime_identity_sha256(snapshot):
            raise SdkError('frozen runtime identity digest changed')
        expected = runtime_baseline({'schema_version': 1, 'container': baseline['container'],
            'sdk': {'name': sdk['name'], 'dependency_lock': sdk['dependency_lock'],
                    'source': {'kind': 'release'}}}, snapshot)
        if expected['sdk'] != sdk or snapshot['container'] != baseline['container']:
            raise SdkError('frozen runtime snapshot disagrees with native adapter')
        if snapshot['dependencies']['lock_sha256'] != sha256(lock_path):
            raise SdkError('dependency lock changed after runtime resolution')
    payloads = [("hrx", sdk["wheel"]), *(("python", p) for p in lock["wheels"])]
    names = set()
    for _, payload in payloads:
        validate_payload(payload)
        if payload["filename"] in names:
            raise SdkError("duplicate locked wheel filename")
        names.add(payload["filename"])
    return baseline, lock, payloads, {
        "baseline_sha256": sha256(path),
        "dependency_lock_sha256": sha256(lock_path),
        "base_image": image,
        "sdk": {key: sdk[key] for key in
                ("name", "version", "repository", "release", "source_commit", "asset_id")},
        "wheel_sha256": sdk["wheel"]["sha256"],
    }


def python_environment():
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "LD_LIBRARY_PATH", "LD_PRELOAD"):
        env.pop(key, None)
    env.update(PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
               PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_CACHE_DIR="1")
    return env


def run(command, log, *, env, timeout=120, failure_kind="install"):
    try:
        result = subprocess.run([str(part) for part in command], env=env,
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise SdkError(f"command exceeded {timeout}s: {Path(str(command[0])).name}",
                       failure_kind) from error
    log.write("$ " + " ".join(str(part) for part in command) + "\n")
    log.write(result.stdout + result.stderr)
    log.flush()
    if result.returncode:
        raise SdkError(f"command failed ({result.returncode}): {Path(str(command[0])).name}; see sdk-install.log",
                       failure_kind)
    return result.stdout


PROBE = r"""
import importlib.metadata, json, pathlib, platform, sys
import loom
import huggingface_hub
baseline = json.loads(pathlib.Path(sys.argv[1]).read_text())
venv = pathlib.Path(sys.argv[2])
assert sys.prefix == str(venv) and sys.prefix != sys.base_prefix
assert not __import__('site').ENABLE_USER_SITE
for module in (loom, huggingface_hub):
    assert pathlib.Path(module.__file__).is_relative_to(venv), module.__file__
assert importlib.metadata.version(baseline['sdk']['name']) == baseline['sdk']['version']
result = {'python_executable': sys.executable, 'prefix': sys.prefix,
          'python_version': sys.version.split()[0], 'loom_module': loom.__file__,
          'hf_module': huggingface_hub.__file__,
          'platform': platform.platform(), 'libc': platform.libc_ver(),
          'sdk_version': importlib.metadata.version(baseline['sdk']['name'])}
if len(sys.argv) > 3:
    from loom.model import ModelCard
    from loom.model.lock import ModelLock
    card_path = pathlib.Path(sys.argv[3])
    card = ModelCard.parse(card_path.read_text())
    selection = card.select(preset=baseline['preset'], programs=baseline['programs'],
                            variant=baseline['variant'])
    assert selection.model_id == baseline['model_id']
    lock_path = pathlib.Path(str(card_path) + '.lock')
    identities = ModelLock.parse(lock_path.read_text()).cover(selection)
    result['card_check'] = {'model_id': selection.model_id,
                            'preset': baseline['preset'],
                            'programs': selection.programs,
                            'locked_asset_count': len(identities),
                            'card_sha256': __import__('hashlib').sha256(card_path.read_bytes()).hexdigest(),
                            'lock_sha256': __import__('hashlib').sha256(lock_path.read_bytes()).hexdigest()}
print(json.dumps(result))
"""


def install(args, baseline, lock, payloads, identity):
    if Path(sys.prefix).resolve() == args.venv.resolve():
        raise SdkError("bootstrap must run outside the venv it creates")
    if args.venv.exists():
        raise SdkError("SDK venv already exists; use a fresh container/run")
    verified = {}
    for kind, payload in payloads:
        path = payload_path(args.payload_root, kind, payload)
        verify(path, payload)
        verified[payload["filename"]] = path
    env = python_environment()
    with (args.output / "sdk-install.log").open("w") as log:
        run([sys.executable, "-I", "-m", "venv", args.venv], log, env=env)
        python = args.venv / "bin/python"
        for name, wheels in (("dependencies", lock["wheels"]), ("sdk", [baseline["sdk"]["wheel"]])):
            requirements = args.output / f"{name}.requirements.txt"
            requirements.write_text("".join(
                verified[p["filename"]].as_uri() + " --hash=sha256:" + p["sha256"] + "\n"
                for p in wheels))
            run([python, "-I", "-m", "pip", "--isolated", "install", "--no-index",
                 "--no-deps", "--require-hashes", "-r", requirements], log, env=env)
        run([python, "-I", "-m", "pip", "--isolated", "check"], log, env=env)
        probe = [python, "-I", "-c", PROBE, args.baseline, args.venv]
        if args.card:
            probe.append(args.card)
        installation = json.loads(run(probe, log, env=env, failure_kind="capability"))
        tools = {name: args.venv / "bin" / name for name in ("loom-fetch", "loom-serve")}
        for name, command in tools.items():
            if not command.is_file() or not command.resolve().is_relative_to(args.venv.resolve()):
                raise SdkError(f"tool is outside SDK venv: {name}", "capability")
            run([command, "--help"], log, env=env, failure_kind="capability")
        if args.card:
            run([tools["loom-fetch"], args.card, "--list", "--json", "--no-input", "--offline",
                 "--cache", args.output / "acquisition"], log, env=env, failure_kind="capability")
        versions = json.loads(run([python, "-I", "-c",
            "import importlib.metadata,json; print(json.dumps({d.metadata['Name']:d.version "
            "for d in importlib.metadata.distributions()}))"], log, env=env))
        for package in lock["wheels"]:
            normalized = {re.sub(r"[-_.]+", "-", name).lower(): version for name, version in versions.items()}
            if normalized.get(re.sub(r"[-_.]+", "-", package["name"]).lower()) != package["version"]:
                raise SdkError(f"installed dependency version mismatch: {package['name']}")
        write_json(args.output / "sdk.json", {
            "schema_version": 1, "status": "ready", "identity": identity,
            "base_image_identity_source": "configured pin; container owner verifies actual image",
            "installation": installation, "tools": {k: str(v) for k, v in tools.items()},
            "packages": versions, "model_executed": False,
        })


def _inspection(argv):
    """Fixed data-only SDK target checks, before any SDK installation."""
    parser = argparse.ArgumentParser(description='Inspect target environment or a staged wheel')
    parser.add_argument('operation', choices=('target-environment', 'qualify-wheel'))
    parser.add_argument('arguments', nargs='*')
    args = parser.parse_args(argv)
    if args.operation == 'target-environment':
        if args.arguments:
            parser.error('target-environment takes no arguments')
        import platform
        v = platform.python_version()
        value = {'python_version': '.'.join(v.split('.')[:2]), 'python_full_version': v,
            'sys_platform': sys.platform, 'platform_machine': platform.machine(),
            'glibc_version': platform.libc_ver()[1], 'os_name': 'posix',
            'platform_system': platform.system(), 'platform_python_implementation': platform.python_implementation(),
            'implementation_name': sys.implementation.name, 'implementation_version': v,
            'platform_release': platform.release(), 'platform_version': platform.version()}
    else:
        if len(args.arguments) != 2:
            parser.error('qualify-wheel requires a staged wheel and dependency directory')
        from . import actions
        data = json.loads(Path('/prepared/actions-qualification-input.json').read_text())
        metadata = actions.wheel_metadata(Path(args.arguments[0]), environment=data['environment'])
        status = actions.validate_dependencies(metadata, data['lock'], environment=data['environment'],
            dependency_directory=Path(args.arguments[1]) if args.arguments[1] else None)
        value = {'metadata': metadata, 'compatibility_status': status}
    print(json.dumps(value))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ('target-environment', 'qualify-wheel'):
        return _inspection(argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("acquire", "install"))
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--payload-root", type=Path, required=True,
                        help="per-attempt SDK/dependency payload directory; new for acquire")
    parser.add_argument("--output", type=Path, required=True, help="empty stage result directory")
    parser.add_argument("--venv", type=Path, default=Path("/run/venv"))
    parser.add_argument("--card", type=Path, help="optional local card with adjacent .lock; parse/list only")
    args = parser.parse_args(argv)
    args.baseline = args.baseline.resolve()
    args.payload_root = args.payload_root.resolve()
    args.output = args.output.resolve()
    args.venv = args.venv.absolute()
    args.card = args.card.resolve() if args.card else None
    args.output.mkdir(parents=True, exist_ok=True)
    if any(args.output.iterdir()):
        parser.error("--output must be empty; use a new attempt directory")
    try:
        baseline, lock, payloads, identity = load_baseline(args.baseline)
        if args.operation == "acquire":
            if args.payload_root.exists() or args.payload_root.is_symlink():
                raise SdkError("--payload-root must be new; SDK payloads are not cached between attempts")
            args.payload_root.mkdir(parents=True, exist_ok=False)
            events = [acquire_payload(args.payload_root, kind, p) for kind, p in payloads]
            write_json(args.output / "sdk-acquisition.json", {
                "schema_version": 1, "status": "ready", "identity": identity,
                "payload_policy": "per_attempt", "payloads": events,
                "downloaded_bytes": sum(e["downloaded_bytes"] for e in events)})
        else:
            install(args, baseline, lock, payloads, identity)
        print(f"SDK {args.operation}: ready; evidence in {args.output}")
        return 0
    except (SdkError, KeyError, ValueError, OSError, subprocess.SubprocessError) as error:
        write_json(args.output / "sdk-error.json", {
            "schema_version": 1, "status": "failed", "stage": args.operation,
            "error_kind": getattr(error, "kind", "download" if args.operation == "acquire"
                                  and isinstance(error, OSError) else "configuration"),
            "error": f"{type(error).__name__}: {error}", "model_executed": False})
        print(f"SDK {args.operation} failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
