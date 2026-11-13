#!/usr/bin/env python3
"""Single-selection stage contract. Fixture is runnable; real adapters are pending."""
import argparse
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


STAGES = ("resolve", "acquire", "admit", "serve", "assert", "cleanup")


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def request(url, body=None):
    encoded = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=encoded, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=2) as response:
        return json.load(response)


def wait_ready(url, model_id, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            rows = request(url + "/v1/models").get("data", [])
            if model_id in [row.get("id") for row in rows]:
                return
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(0.02)
    raise TimeoutError("expected prepared model did not appear in /v1/models")


def memory_fit(required, available, reserve):
    if required is None:
        return "unqualified"
    for value in (required, available, reserve):
        if type(value) is not int or value < 0:
            raise ValueError("memory values must be nonnegative integer bytes")
    return "eligible" if required + reserve <= available else "insufficient_memory"


def plan(selection, image):
    locator = "huggingface:{repository}:{preset}@{publication_revision}".format(**selection)
    return {
        "selection": selection,
        "image": image or None,
        "fixture_is_model_coverage": False,
        "stages": {
            "resolve": "freeze publication/preset, runtime image and qualified workload",
            "acquire": "fresh pipeline per run; verify/reuse the selected reusable asset closure",
            "admit": "verify GPU binding/topology, live free memory, runtime/context budget and reserve",
            "serve": "start owned server; wait for the selected prepared model",
            "assert": "POST bounded /v1/chat/completions request and check explicit known answer",
            "cleanup": "stop owned server/container; retain report and shared immutable assets",
        },
        "native_command_reference": {
            "fetch": ["loom-fetch", locator, "--programs=source", "--output=/run/model", "--cache=/run/acquisition",
                      "--materialize=auto", "--no-input", "--json"],
            "serve": ["loom-serve", "--device=REVIEWED_DEVICE_URI", "--model=/run/model/model.json",
                      "--listen=127.0.0.1:18080"],
        },
        "integration_notes": [
            "These are source-verified CLI references, not a working asset-only cache adapter.",
            "Native HF/shared content caches can reuse programs; enforce fresh pipeline downloads separately.",
            "The real adapter must expose selected cached assets and per-run pipeline read-only to testing.",
            "The readiness/query client runs inside the isolated test container; no host-network requirement.",
        ],
    }


def real_blockers(image, qualification):
    blockers = []
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[a-f0-9]{64}", image or ""):
        blockers.append("configure a reviewed digest-pinned native Loom runtime image")
    if qualification is None:
        blockers.append("provide a reviewed model/runtime/workload/hardware memory qualification")
    else:
        # Do not accept an arbitrary total as a qualified serving profile.
        blockers.append("qualification binding and live GPU/topology admission adapter are not implemented")
    blockers.append("fresh-pipeline/reusable-assets acquisition and isolated native serving adapters are not implemented")
    return blockers


def fixture_acquire(output, cache):
    # Distinct namespace: these bytes must never masquerade as model assets.
    payload = b"synthetic tokenizer fixture; not model weights\n"
    digest = hashlib.sha256(payload).hexdigest()
    assets = cache / "single-model-skeleton-fixtures"
    assets.mkdir(parents=True, exist_ok=True)
    asset = assets / digest
    reused = asset.exists()
    if reused and asset.read_bytes() != payload:
        raise ValueError("fixture cache integrity failure")
    if not reused:
        asset.write_bytes(payload)
    pipeline = output / "pipeline"
    pipeline.mkdir()
    # A new pipeline staging area on every invocation, including cache hits.
    write_json(pipeline / "model.json", {"fixture": True, "asset_sha256": digest,
                                       "staged_at": str(output)})
    return {"fixture_asset_reused": reused, "asset_sha256": digest,
            "fresh_pipeline_path": str(pipeline)}


def fixture_run(selection, report, output, cache, failure):
    report["stages"]["acquire"] = {"status": "running"}
    report["stages"]["acquire"] = {"status": "passed", **fixture_acquire(output, cache)}
    # Synthetic counts validate decisions only; never copied into a model profile.
    report["stages"]["admit"] = {"status": "running"}
    decision = memory_fit(None if failure == "memory" else 60, 100, 10)
    report["stages"]["admit"] = {"status": "passed" if decision == "eligible" else "blocked",
                                      "decision": decision, "telemetry": "synthetic"}
    if decision != "eligible":
        report["stages"]["cleanup"] = {"status": "passed", "server_started": False}
        return
    readiness_calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            readiness_calls.append(self.path)
            ready = failure != "readiness" and len(readiness_calls) >= 2
            self.reply({"data": [{"id": selection["model_id"]}] if ready else []})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert body["model"] == selection["model_id"] and body["max_tokens"] == 8
            self.reply({"choices": [{"message": {"content": "5" if failure == "answer" else "4"}}]})

        def reply(self, body):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        report["stages"]["serve"] = {"status": "running"}
        url = "http://127.0.0.1:" + str(server.server_port)
        wait_ready(url, selection["model_id"], 0.25 if failure == "readiness" else 3)
        report["stages"]["serve"] = {"status": "passed", "readiness_checks": len(readiness_calls)}
        report["stages"]["assert"] = {"status": "running"}
        response = request(url + "/v1/chat/completions",
                           dict(selection["smoke_request"], model=selection["model_id"]))
        write_json(output / "response.json", response)
        if response["choices"][0]["message"]["content"].strip() != selection["smoke_expected_text"]:
            raise AssertionError("response did not match the known answer")
        report["stages"]["assert"] = {"status": "passed"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        report["stages"]["cleanup"] = {"status": "passed" if not thread.is_alive() else "failed",
                                          "owned_fixture_server_stopped": not thread.is_alive()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fixture", "real"), default="fixture")
    parser.add_argument("--selection", type=Path, default=None)
    parser.add_argument("--output", type=Path, required=True, help="new per-attempt output directory")
    parser.add_argument("--cache", type=Path, required=True, help="external cache root")
    parser.add_argument("--image", default="")
    parser.add_argument("--qualification", type=Path)
    parser.add_argument("--fixture-failure", choices=("none", "memory", "readiness", "answer"), default="none")
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": 1, "mode": args.mode, "model_tested": False,
              "started_at": datetime.now(timezone.utc).isoformat(),
              "stages": {name: {"status": "not_run"} for name in STAGES}}
    try:
        if args.selection:
            selection = json.loads(args.selection.read_text())
        else:
            import runpy
            runpy.run_path(str(Path(__file__).resolve().parents[2] / 'python/run.py'))['bootstrap']()
            from hrx_systems.selection import configuration as config
            selection, _ = config.default_baseline()
        report["selection"] = selection
        report["stages"]["resolve"] = {"status": "passed"}
        write_json(args.output / "plan.json", plan(selection, args.image))
        if args.mode == "real":
            qualification = json.loads(args.qualification.read_text()) if args.qualification else None
            report["blockers"] = real_blockers(args.image, qualification)
            report["stages"]["acquire"] = {"status": "blocked", "reason": "real-mode integration prerequisites pending"}
        else:
            fixture_run(selection, report, args.output, args.cache, args.fixture_failure)
    except Exception as exc:
        active = next((name for name in STAGES if report["stages"][name]["status"] == "running"), None)
        report["stages"][active or "resolve"] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        passed = all(stage["status"] == "passed" for stage in report["stages"].values())
        report["outcome"] = ("fixture_passed" if passed and args.mode == "fixture" else
                             "blocked" if any(stage["status"] == "blocked" for stage in report["stages"].values()) else "failed")
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(args.output / "report.json", report)
        summary = f"# Single-model skeleton: {report['outcome']}\n\nMode: {args.mode}. Real-model coverage: **not established**.\n\n"
        summary += "\n".join(f"- {name}: {value['status']}" for name, value in report["stages"].items()) + "\n"
        summary += "\n" + "\n".join(f"- Prerequisite: {value}" for value in report.get("blockers", [])) + "\n"
        (args.output / "summary.md").write_text(summary)
        print(summary)
    return 0 if report["outcome"] == "fixture_passed" else 2 if report["outcome"] == "blocked" else 1


if __name__ == "__main__":
    raise SystemExit(main())
