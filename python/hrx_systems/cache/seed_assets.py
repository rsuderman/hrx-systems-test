#!/usr/bin/env python3
"""Single-model seed orchestration and private native acquisition stages."""
import argparse
from .. import layout
from dataclasses import asdict, replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import uuid


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".seed-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def code_file(file):
    return bool(file.roles & {"program", "auxiliary"})


def native_cache(root, hf_root, cancelled):
    from loom.model.fetch_hf import HuggingFace
    from loom.model.fetch_storage import ContentCache
    return ContentCache(Path(root), huggingface=HuggingFace(
        cache_dir=Path(hf_root), cancelled=cancelled))


def routed_cache(selection, work, cache_root):
    """Use native caches/providers; route by native origin and transform identity."""
    from loom.model.fetch_storage import ContentCache
    from loom.model.lock import file_key
    cancelled = threading.Event()
    assets = native_cache(cache_root / "loom-assets", cache_root / "huggingface", cancelled)
    code = native_cache(work / "pipeline-cache", work / "pipeline-hf", cancelled)
    code_origins = {file_key(f.origin) for f in selection.files.values() if code_file(f)}

    class RoutedCache(ContentCache):
        def __init__(self):
            super().__init__(assets.root, huggingface=assets.huggingface)
            self.acquired = {}
            self.record_lock = threading.Lock()

        def owner(self, source):
            return code if file_key(source.origin) in code_origins else assets

        def read_document(self, location, *, offline):
            return code.read_document(location, offline=offline)

        def prepare_source(self, source, identity, *, offline):
            return self.owner(source).prepare_source(source, identity, offline=offline)

        def acquire(self, source, identity, *, offline, progress=None):
            item = self.owner(source).acquire(source, identity, offline=offline, progress=progress)
            with self.record_lock:
                self.acquired[file_key(source.origin)] = item
            return item

        def acquire_transformed(self, source, transform, identity, **kwargs):
            item = self.owner(source).acquire_transformed(source, transform, identity, **kwargs)
            with self.record_lock:
                self.acquired[file_key(source.origin, transform)] = item
            return item

    return RoutedCache(), code


def selection_for(baseline, text):
    from loom.model import ModelCard
    selection = ModelCard.parse(text).select(
        preset=baseline["preset"], variant=baseline["variant"], programs=baseline["programs"])
    if selection.model_id != baseline["model_id"]:
        raise ValueError("published model does not match configured model_id")
    return selection


def prepare(baseline, work, cache_root, *, location=None):
    from loom.model.fetch import plan_fetch
    from loom.model.fetch_source import Document
    # Use an attempt-only provider even for hosted card/lock documents.
    documents = native_cache(work / "pipeline-cache", work / "pipeline-hf", threading.Event())
    repository, revision = baseline["repository"], baseline["publication_revision"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("invalid publication repository")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("publication_revision must be an immutable commit")
    location = location or f"https://huggingface.co/{repository}/resolve/{revision}/model.json"
    document = documents.read_document(location, offline=False)
    lock = documents.read_document(location + ".lock", offline=False)
    (work / "model.json").write_text(document.text)
    (work / "model.json.lock").write_text(lock.text)
    selection = selection_for(baseline, document.text)
    cache, _ = routed_cache(selection, work, cache_root)
    plan = plan_fetch(Document(location, document.text), selection, cache,
                      lock_location=str(work / "model.json.lock"))
    for name, file in selection.files.items():
        if not code_file(file):
            if name not in plan.identities:
                raise ValueError(f"asset lacks locked identity: {name}")
            continue
        if file.transform is not None:
            raise ValueError("the initial seed baseline requires untransformed programs")
        item = cache.acquire(plan.sources[name], plan.identities.get(name), offline=False)
        plan.sources[name] = replace(plan.sources[name], transport={"kind": "local", "path": str(item.path)})
    write_json(work / "prepared.json", {
        "document": asdict(plan.document), "sources": {k: asdict(v) for k, v in plan.sources.items()},
        "identities": {k: asdict(v) for k, v in plan.identities.items()},
        "origins": plan.origins, "lock_mode": plan.lock_mode, "lock_scope": plan.lock_scope})


def fill(baseline, work, cache_root, output, offline):
    from loom.model.fetch import FetchPlan, install
    from loom.model.fetch_source import Document, Source
    from loom.model.lock import FileIdentity, file_key
    prepared = read_json(work / "prepared.json")
    document = Document(**prepared["document"])
    selection = selection_for(baseline, document.text)
    plan = FetchPlan(document, selection,
        {k: Source(**v) for k, v in prepared["sources"].items()},
        {k: FileIdentity(**v) for k, v in prepared["identities"].items()},
        prepared["origins"], offline, prepared["lock_mode"], prepared["lock_scope"], None)
    cache, _ = routed_cache(selection, work, cache_root)
    install(plan, cache, work / "installed")
    files, programs = [], []
    for name, file in selection.files.items():
        installed = work / "installed" / name
        if code_file(file):
            programs.append({"path": name, "sha256": digest(installed)})
            continue
        item = cache.acquired[file_key(file.origin, file.transform)]
        identity = plan.identities[name]
        identity.verify(item.path)
        resolved = item.path.resolve(strict=True)
        for root_kind in ("huggingface", "loom-assets"):
            root = (cache_root / root_kind).resolve(strict=True)
            if resolved.is_relative_to(root):
                relative = str(resolved.relative_to(root))
                break
        else:
            raise ValueError(f"asset cache path escapes designated roots: {name}")
        files.append({"installation_path": name, "roles": sorted(file.roles),
            "origin": plan.sources[name].origin, "resolved": plan.sources[name].resolved,
            "size": identity.size, "sha256": identity.sha256, "verified": True,
            "cache_root": root_kind, "cache_path": relative})
    sdk = read_json(output / "sdk-seed" / "sdk.json")
    manifest = {"schema_version": 1, "selection": {
        k: baseline[k] for k in ("repository", "publication_revision", "model_id", "preset", "variant", "programs")},
        "card_sha256": digest(work / "model.json"), "lock_sha256": digest(work / "model.json.lock"),
        "origin_roots": plan.origins, "sdk_identity": sdk["identity"], "files": files}
    write_json(output / "seed-manifest.json", manifest)
    write_json(output / "seed-report.json", {"schema_version": 1, "status": "ready",
        "selection": manifest["selection"], "sdk_identity": sdk["identity"],
        "asset_network": "offline" if offline else "online", "programs": programs,
        "card_sha256": manifest["card_sha256"], "lock_sha256": manifest["lock_sha256"],
        "verified_asset_bytes": sum(f["size"] for f in files), "model_executed": False})


def run_host(args):
    from ..layout import REPO_ROOT as repo
    from ..sdk import environment as sdk_helper
    from ..selection import configuration as config
    if args.baseline and getattr(args, 'model', None):
        raise ValueError('--baseline and --model are mutually exclusive')
    runtime_path = getattr(args, 'runtime', None)
    if args.baseline:
        baseline_path = args.baseline.resolve()
        baseline, _, _, _ = sdk_helper.load_baseline(baseline_path)
        lock_path = baseline_path.parent / baseline['sdk']['dependency_lock']
        if runtime_path:
            adapter, snapshot, lock_path = config.runtime(runtime_path)
            if baseline['sdk'] != adapter['sdk'] or baseline['container'] != adapter['container']:
                raise ValueError('legacy baseline and runtime settings conflict')
            baseline['resolved_runtime'] = snapshot
    elif getattr(args, 'model', None):
        suite = read_json(repo / 'config/cached-model-tests.json')
        entries = [entry for entry in suite['entries'] if entry['id'] == args.model]
        if len(entries) != 1: raise ValueError('unknown sample model ID')
        baseline, lock_path = config.compose(entries[0], runtime_path)
    else:
        baseline, lock_path = config.default_baseline(runtime_path)
    root = Path(args.root or os.environ.get("HRX_SYSTEMS_ROOT", Path.home() / ".local/share/hrx-systems-test")).expanduser()
    if root == Path("/") or not root.is_absolute():
        raise ValueError("root must be an absolute directory other than /")
    if not shutil.which("docker"):
        raise ValueError("Docker is required; ask the machine owner to configure Docker access")
    subprocess.run(["docker", "info"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cache_root = root / "cache"
    for name in ("huggingface", "loom-assets"):
        (cache_root / name).mkdir(parents=True, exist_ok=True)
    output = (args.output or root / "runs" / ("seed-" + uuid.uuid4().hex)).absolute()
    output.mkdir(parents=True, exist_ok=False)
    manifest_path = (args.seed_manifest or cache_root / "seed-manifest.json").absolute()
    print(f"Seeding {baseline['model_id']}; cache root: {cache_root}", flush=True)
    container_name = "hrx-seed-" + uuid.uuid4().hex
    stage = "prepare"
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    old_handlers = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        with (cache_root / ".seed.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with tempfile.TemporaryDirectory(prefix=".seed-work-", dir=output) as temporary:
                temporary = Path(temporary)
                work = temporary / "run"
                work.mkdir()
                (work / "config").mkdir()
                write_json(work / "config/baseline.json", baseline)
                baseline_path = work / "config/baseline.json"
                shutil.copyfile(lock_path,
                                work / "config" / baseline["sdk"]["dependency_lock"])
                payloads = temporary / "sdk-payloads"
                subprocess.run([*layout.command('sdk-environment'), "acquire",
                    "--baseline", str(baseline_path), "--payload-root", str(payloads),
                    "--output", str(output / "sdk-download")], check=True)
                common = ["docker", "run", "--rm", "--name", container_name, "--read-only",
                    "--platform", baseline["container"]["platform"],
                    "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,size=128m,mode=1777",
                    "--env", "HOME=/tmp", "--env", "PYTHONNOUSERSITE=1",
                    *layout.mount_arguments(),
                    "--mount", f"type=bind,src={payloads},dst=/sdk-payloads,readonly",
                    "--mount", f"type=bind,src={work},dst=/run",
                    "--mount", f"type=bind,src={output},dst=/results",
                    "--mount", f"type=bind,src={cache_root / 'huggingface'},dst=/model-cache/huggingface",
                    "--mount", f"type=bind,src={cache_root / 'loom-assets'},dst=/model-cache/loom-assets"]
                for operation in ("prepare", "fill"):
                    stage = operation
                    print("Fetching fresh pipeline" if operation == "prepare" else
                          f"Verifying/seeding model assets ({args.asset_network})", flush=True)
                    network = "none" if operation == "fill" and args.asset_network == "offline" else "bridge"
                    command = common + ["--network", network]
                    # Credentials are acquisition-scoped environment only, never mounted/copied auth files.
                    if network != "none" and os.environ.get("HF_TOKEN"):
                        command += ["--env", "HF_TOKEN"]
                    command += [baseline["container"]["image"], *layout.command('seed-models', interpreter="python", container=True),
                                "--stage", operation, "--asset-network", args.asset_network]
                    try:
                        with (output / f"{operation}.log").open("w") as log:
                            subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT)
                    finally:
                        subprocess.run(["docker", "rm", "-f", container_name],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    shutil.rmtree(work / "venv")
            # Staging hardlinks are gone before stat-bound writer receipts are
            # published, so removing staging cannot invalidate their ctime.
            stage = "publish"
            from . import assets as asset_helper
            manifest = read_json(output / 'seed-manifest.json')
            verifier = asset_helper.Verifier(full=True)
            receipts = asset_helper.load_index(cache_root / asset_helper.INDEX_NAME)
            for item in manifest['files']:
                receipt = verifier.verify(asset_helper.asset_path(cache_root, item), item['size'], item['sha256'])
                receipts[asset_helper.key(receipt)] = receipt
            asset_helper.write_index(cache_root / asset_helper.INDEX_NAME, receipts)
            write_json(manifest_path, manifest)
        print(f"Seed ready. Index: {manifest_path}\nReport: {output / 'seed-report.json'}")
        return 0
    except (Exception, KeyboardInterrupt) as error:
        write_json(output / "seed-report.json", {"schema_version": 1, "status": "failed",
            "stage": stage, "error": f"{type(error).__name__}: {error}", "model_executed": False})
        print(f"Seed failed at {stage}; see {output}", file=sys.stderr)
        return 1
    finally:
        subprocess.run(["docker", "rm", "-f", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 3 and argv[0] == '--native':
        baseline = read_json('/run/config/baseline.json')
        if argv[1] == 'prepare':
            prepare(baseline, Path('/run'), Path('/model-cache'))
        else:
            fill(baseline, Path('/run'), Path('/model-cache'), Path('/results'), argv[2] == 'offline')
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, help="legacy composed model baseline")
    parser.add_argument("--runtime", type=Path, help="shared runtime config")
    parser.add_argument("--model", help="sample model ID; default first suite entry")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seed-manifest", type=Path)
    parser.add_argument("--asset-network", choices=("online", "offline"), default="online")
    parser.add_argument("--stage", choices=("prepare", "fill"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.stage:
        output, work = Path("/results"), Path("/run")
        subprocess.run([*layout.command('sdk-environment', interpreter=sys.executable, container=True), "install",
            "--baseline", "/run/config/baseline.json", "--payload-root", "/sdk-payloads",
            "--output", str(output / ("sdk-" + ("prepare" if args.stage == "prepare" else "seed")))], check=True)
        command = [*layout.command('seed-models', interpreter="/run/venv/bin/python", container=True),
                   "--native", args.stage, args.asset_network]
        return subprocess.run(command).returncode
    try:
        return run_host(args)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"Cannot seed: {error}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
