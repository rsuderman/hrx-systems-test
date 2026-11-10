#!/usr/bin/env python3
"""Private SDK payload acquisition and isolated-container installation helper.

This never fetches model assets or starts a server. Public model/run wrappers
will call these operations once their respective implementation tasks land.
"""
import argparse
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


def load_baseline(path):
    baseline = json.loads(path.read_text())
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


def main(argv=None):
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
