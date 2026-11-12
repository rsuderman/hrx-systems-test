#!/usr/bin/env python3
"""Render standalone verification artifacts from saved suite results only."""
import argparse
import copy
import hashlib
import html
import importlib.util
import json
import math
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile
from urllib.parse import quote


STATUSES = {"passed", "failed", "not_run", "incomplete"}
PHASES = ("load", "readiness", "query", "assertion", "unload")
MAX_FILE = 2 * 1024 * 1024
MAX_LOG = 4 * 1024 * 1024
MAX_BUNDLE = 128 * 1024 * 1024
MAX_FILES = 4096
SUMMARY_ROWS = 100
SUMMARY_DIAGNOSTICS = 20
SUMMARY_DIAGNOSTIC_CHARS = 2048
_RUNTIME_SPEC = importlib.util.spec_from_file_location("runtime_summary", Path(__file__).with_name("runtime-summary.py"))
RUNTIME_SUMMARY = importlib.util.module_from_spec(_RUNTIME_SPEC)
_RUNTIME_SPEC.loader.exec_module(RUNTIME_SUMMARY)
LABELS = {"passed": "Passed", "failed": "Failed", "not_run": "Not run", "incomplete": "Incomplete"}


class ReportError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ReportError(message)


def text(value):
    return str(value) if value is not None else "Unavailable"


def escaped(value):
    return html.escape(text(value), quote=True)


def markdown(value):
    value = re.sub(r"([\\`|\[\]()*_!#])", r"\\\1", text(value).replace("\n", " "))
    return html.escape(value, quote=True)


def duration(value):
    if value is None:
        return "Unavailable"
    return f"{value:,.2f}s"


def elapsed(value, label):
    require(value is None or (type(value) in (int, float) and math.isfinite(value) and value >= 0),
            f"{label}: elapsed_seconds must be nonnegative or null")
    return value


def phase(value, label):
    if value is None:
        return {"status": "incomplete", "reason": "No phase evidence recorded", "elapsed_seconds": None}
    require(isinstance(value, dict), f"{label}: phase must be an object")
    result = copy.deepcopy(value)
    status = result.get("status", "incomplete")
    status = {"ready": "passed"}.get(status, status)
    require(status in STATUSES, f"{label}: invalid phase status")
    result["status"] = status
    result["elapsed_seconds"] = elapsed(result.get("elapsed_seconds"), label)
    if status != "passed":
        result.setdefault("reason", "No phase evidence recorded" if status == "incomplete" else LABELS[status])
    return result


def combined(statuses):
    values = list(statuses)
    if "failed" in values:
        return "failed"
    if not values or "incomplete" in values:
        return "incomplete"
    if "not_run" in values:
        return "not_run"
    return "passed"


def read_json(path, limit=MAX_FILE):
    require(not path.is_symlink() and path.is_file() and path.stat().st_size <= limit, f"Missing or oversized JSON: {path.name}")
    try:
        return json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReportError(f"Invalid JSON: {path.name}: {error}") from error


def evidence_path(root, relative):
    require(isinstance(relative, str) and relative and "\\" not in relative,
            "Evidence reference must be a relative POSIX path")
    path = PurePosixPath(relative)
    require(not path.is_absolute() and all(p not in (".", "..") for p in relative.split("/")),
            f"Invalid evidence reference: {relative}")
    require(path.suffix.lower() in {".json", ".log", ".txt", ".md"},
            f"Evidence is not an allowed compact file: {relative}")
    require(path.name not in {"tokenizer.json", "model.json", "index.html", "report.json", "summary.md", "suite-result.json"},
            f"Evidence reference names an excluded payload or reserved artifact: {relative}")
    candidate = root.joinpath(*path.parts)
    for parent in [candidate, *candidate.parents]:
        if parent == root:
            break
        require(not parent.is_symlink(), f"Symlink evidence is not allowed: {relative}")
    require(candidate.resolve().is_relative_to(root), f"Evidence escapes results: {relative}")
    require(candidate.is_file() and candidate.stat().st_size <= (MAX_LOG if path.suffix.lower() == ".log" else MAX_FILE),
            f"Missing or oversized evidence: {relative}")
    return candidate


def normalize(root):
    original = read_json(root / "suite-result.json")
    require(isinstance(original, dict) and original.get("schema_version") == 1, "Unsupported suite-result schema")
    require(isinstance(original.get("attempt_id"), str) and original["attempt_id"], "Missing attempt_id")
    require(original.get("mode") in ("real", "fixture"), "mode must be real or fixture")
    for key in ("source", "runtime", "hardware"):
        require(isinstance(original.get(key), dict), f"Missing {key} identity object")
    expected = original.get("expected_entries")
    require(isinstance(expected, list), "Missing frozen expected_entries list")
    ids = []
    for entry in expected:
        require(isinstance(entry, dict) and isinstance(entry.get("id"), str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", entry["id"]), "Invalid expected entry ID")
        require(isinstance(entry.get("selection"), dict), f"Missing selection identity: {entry['id']}")
        require(entry["id"] not in ids, "Duplicate expected entry ID")
        ids.append(entry["id"])
    raw_models = original.get("models", [])
    require(isinstance(raw_models, list), "models must be a list")
    by_id = {}
    for model in raw_models:
        require(isinstance(model, dict) and model.get("id") in ids, "Unexpected model result")
        require(model["id"] not in by_id, "Duplicate model result")
        by_id[model["id"]] = model
    report = copy.deepcopy(original)
    report["elapsed_seconds"] = elapsed(original.get("elapsed_seconds"), "suite")
    server = original.get("server", {})
    require(isinstance(server, dict), "server must be an object")
    report["server"] = {**server, **{key: phase(server.get(key), f"server.{key}")
                                      for key in ("start", "shutdown", "cleanup")}}
    report["models"] = []
    references = set()

    def add_references(items):
        require(isinstance(items, list), "evidence must be a list of relative paths")
        for item in items:
            evidence_path(root, item)
            references.add(item)

    add_references(original.get("evidence", []))
    add_references(server.get("evidence", []))
    for key in ("start", "shutdown", "cleanup"):
        add_references(report["server"][key].get("evidence", []))
    for entry in expected:
        raw = by_id.get(entry["id"], {})
        if "selection" in raw:
            require(raw["selection"] == entry["selection"], f"Selection identity changed: {entry['id']}")
        model = {**copy.deepcopy(raw), "id": entry["id"], "selection": copy.deepcopy(entry["selection"]),
                 "test_kind": entry.get("test_kind", "text smoke"),
                 "expected_text": entry.get("expected_text"),
                 "preparation": phase(raw.get("preparation"), f"{entry['id']}.preparation"), "cycles": []}
        raw_cycles = raw.get("cycles", [])
        require(isinstance(raw_cycles, list), "cycles must be a list")
        add_references(raw.get("evidence", []))
        add_references(model["preparation"].get("evidence", []))
        for number, raw_cycle in enumerate(raw_cycles):
            require(isinstance(raw_cycle, dict), "cycle must be an object")
            cycle = copy.deepcopy(raw_cycle)
            for key in PHASES:
                cycle[key] = phase(raw_cycle.get(key), f"{entry['id']}.cycle{number}.{key}")
            cycle["status"] = combined(cycle[key]["status"] for key in PHASES)
            add_references(raw_cycle.get("evidence", []))
            for key in PHASES:
                add_references(cycle[key].get("evidence", []))
            for key in ("request_path", "response_path"):
                if cycle.get(key):
                    add_references([cycle[key]])
                elif cycle["query"]["status"] == "passed" or cycle["assertion"]["status"] == "passed":
                    raise ReportError(f"Successful query lacks {key}: {entry['id']}")
            if cycle["status"] == "passed":
                require(cycle.get("model_id") == entry["selection"].get("model_id") and cycle.get("incarnation") is not None,
                        f"Successful cycle lacks expected served identity: {entry['id']}")
            model["cycles"].append(cycle)
        observed = [model["preparation"]["status"], *(c["status"] for c in model["cycles"])]
        raw_status = raw.get("status", "incomplete")
        require(raw_status in STATUSES, "Invalid model result status")
        if not model["cycles"]:
            observed.append("not_run" if raw_status == "not_run" else "incomplete")
        if raw_status != "passed":
            observed.append(raw_status)
        model["status"] = combined(observed)
        failures = [("preparation", model["preparation"])]
        failures.extend((f"cycle {i + 1} / {key}", c[key]) for i, c in enumerate(model["cycles"]) for key in PHASES)
        model["reason"] = raw.get("reason") or next((f"{name}: {value.get('reason', LABELS[value['status']])}"
                          for name, value in failures if value["status"] == "failed"), "")
        if not model["reason"]:
            model["reason"] = next((f"{name}: {value.get('reason', LABELS[value['status']])}"
                                    for name, value in failures if value["status"] != "passed"), "")
        if not model["cycles"] and not model["reason"]:
            model["reason"] = "No model cycle recorded"
        report["models"].append(model)
    require(len(references) <= MAX_FILES, "Too many evidence files")
    require(sum(evidence_path(root, p).stat().st_size for p in references) <= MAX_BUNDLE, "Evidence bundle is oversized")
    statuses = [m["status"] for m in report["models"]]
    if report.get('inventory_errors'):
        statuses.append('failed')
    statuses += [report["server"][key]["status"] for key in ("start", "shutdown", "cleanup")]
    raw_status = original.get("status", "incomplete")
    require(raw_status in STATUSES, "Invalid suite status")
    if raw_status != "passed":
        statuses.append(raw_status)
    report["status"] = combined(statuses) if expected else "failed"
    if report["status"] == "passed":
        require(bool(server.get("identity")), "Successful suite lacks server identity")
    if not expected:
        report["reason"] = "The expected suite is empty"
    elif report["status"] != "passed":
        report["reason"] = original.get("reason") or "Some required model or server phases did not pass"
    report["counts"] = {"expected": len(expected), **{key: sum(m["status"] == key for m in report["models"]) for key in STATUSES},
                        "cycles": sum(len(m["cycles"]) for m in report["models"]),
                        "distinct_models": len({entry["selection"]["model_id"] for entry in expected
                                                if isinstance(entry["selection"].get("model_id"), str) and entry["selection"]["model_id"]})}
    report["evidence_files"] = sorted(references)
    report["renderer"] = {"version": 1, "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    return report


def badge(status, label=None):
    return f'<span class="badge {escaped(status)}">{escaped(label or LABELS[status])}</span>'


def local_link(path, label=None):
    return f'<a href="{quote(path, safe="/")}">{escaped(label or path)}</a>'


def identity(value):
    return json.dumps(value, indent=2, ensure_ascii=False)


def cycle_status(model, key):
    return combined(c[key]["status"] for c in model["cycles"]) if model["cycles"] else "not_run"


def pretty_json(root, path):
    if not path:
        return "Not recorded"
    return json.dumps(read_json(evidence_path(root, path)), indent=2, ensure_ascii=False)


def github_links(report):
    """Derive optional links from structured metadata, never arbitrary URLs."""
    workflow = report.get("workflow")
    if not isinstance(workflow, dict):
        return {}
    repository = workflow.get("repository")
    if not isinstance(repository, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository):
        return {}
    base = f"https://github.com/{repository}"
    links = {}
    run_id = workflow.get("run_id")
    if type(run_id) in (str, int) and re.fullmatch(r"[1-9][0-9]*", str(run_id)):
        run_url = f"{base}/actions/runs/{run_id}"
        accepted = {run_url}
        attempt = workflow.get("run_attempt")
        if type(attempt) in (str, int) and re.fullmatch(r"[1-9][0-9]*", str(attempt)):
            accepted.add(f"{run_url}/attempts/{attempt}")
        supplied = workflow.get("run_url")
        if supplied is None or (isinstance(supplied, str) and supplied in accepted):
            links[f"GitHub run #{run_id}"] = supplied or run_url
    sha = workflow.get("sha")
    if isinstance(sha, str) and re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        links[f"Workflow source {sha[:12].lower()}"] = f"{base}/commit/{sha.lower()}"
    return links


def load_failures(report, root, supplied=None):
    """Read the shared saved failure records; never derive another diagnosis."""
    if supplied is not None:
        manifest = read_json(supplied, MAX_LOG)
    else:
        helper_path = Path(__file__).with_name("failure-records.py")
        require(helper_path.is_file(), "Shared failure-records.py helper is unavailable")
        spec = importlib.util.spec_from_file_location("report_failure_records", helper_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        manifest = module.collect_results(root)
    require(isinstance(manifest, dict) and manifest.get("schema_version") == 1,
            "Unsupported failure-record schema")
    require(manifest.get("attempt_id") == report["attempt_id"], "Failure records belong to another attempt")
    cases = manifest.get("cases")
    require(isinstance(cases, list) and len(cases) <= MAX_FILES, "Invalid failure cases")
    by_id = {model["id"]: model for model in report["models"]}
    counts = manifest.get("counts")
    require(isinstance(counts, dict), "Invalid failure coverage counts")
    for key in ("expected", *STATUSES):
        require(type(counts.get(key)) is int and counts[key] == report["counts"][key],
                f"Failure coverage differs from saved results: {key}")
    ids = set()
    for case in cases:
        require(isinstance(case, dict) and case.get("scope") in ("model", "run"), "Invalid failure case")
        for key in ("case_id", "display_name", "phase", "summary", "diagnostic"):
            require(isinstance(case.get(key), str) and bool(case[key]), f"Invalid failure case {key}")
        require(case["case_id"] not in ids, "Duplicate failure case ID")
        ids.add(case["case_id"])
        if case["scope"] == "model":
            model = by_id.get(case.get("selection_id"))
            require(model is not None and model["status"] != "passed", "Failure case references a passing or unknown selection")
            if case.get("selection") is not None:
                require(case["selection"] == model["selection"], "Failure case selection identity changed")
            cycle = case.get("cycle")
            require(cycle is None or (type(cycle) is int and 1 <= cycle <= len(model["cycles"])),
                    "Invalid failure cycle")
        observations = case.get("observations", [])
        require(isinstance(observations, list) and all(isinstance(item, dict) for item in observations),
                "Invalid failed-phase observations")
    require(isinstance(manifest.get("evidence_errors", []), list)
            and all(isinstance(item, str) for item in manifest.get("evidence_errors", [])),
            "Invalid failure evidence errors")
    covered = {case.get("selection_id") for case in cases if case["scope"] == "model"}
    require(all(model["id"] in covered for model in report["models"] if model["status"] == "failed"),
            "Failure records omit a failed selection")
    return manifest


def display_name(model):
    selection = model["selection"]
    name = text(selection.get("model_id"))
    publisher = selection.get("repository")
    return f"{name} · {publisher}" if publisher else name


def runtime_identity(report, root=None):
    installation = None
    if root is not None:
        receipt = root / 'sdk-runtime/sdk.json'
        if receipt.exists() or receipt.is_symlink():
            installation = read_json(evidence_path(root, 'sdk-runtime/sdk.json'))
    try:
        return RUNTIME_SUMMARY.describe(report['runtime'], server=report.get('server'),
            installation=installation, mode=report.get('mode'))
    except ValueError as error:
        raise ReportError(str(error)) from error


def failure_cases(report):
    return report.get("failures", {}).get("cases", [])


def case_name(case):
    selection = case.get("selection") or {}
    qualifiers = [str(selection[key]) for key in ("preset", "variant") if selection.get(key)]
    revision = selection.get("publication_revision")
    if isinstance(revision, str) and revision:
        qualifiers.append(revision[:12])
    return case["display_name"] + (" · " + " / ".join(qualifiers) if qualifiers else "")


def failure_operation(case):
    cycle = case.get("cycle")
    return f"{case['phase']} · cycle {cycle}" if cycle is not None else case["phase"]


def extra_observations(case):
    return [item for item in case.get("observations", []) if item.get("phase") != case["phase"]]


def failure_detail(case):
    extra = extra_observations(case)
    return case["summary"] + ''.join(
        f'; additional failure: {item.get("phase", "unavailable")}: {str(item.get("reason", "No diagnostic recorded"))[:512]}'
        for item in extra)


def full_diagnostic(case):
    return case["diagnostic"] + ''.join(
        f'\n\nAdditional failed operation: {item.get("phase", "unavailable")}\n{item.get("reason", "No diagnostic recorded")}'
        for item in extra_observations(case))


def failure_html(report):
    cases = failure_cases(report)
    errors = report.get("failures", {}).get("evidence_errors", [])
    inventory = report.get("inventory_errors", [])
    if not cases and not errors and not inventory and report["status"] != "failed":
        return ""
    rows = []
    diagnostics = []
    for case in cases:
        rows.append(f'<tr><td>{escaped(case_name(case))}</td><td>{escaped(failure_operation(case))}</td>'
                    f'<td>{escaped(failure_detail(case))}</td></tr>')
        diagnostics.append(f'<details><summary>{escaped(case_name(case))} · {escaped(failure_operation(case))}</summary>'
                           f'<pre class="diagnostic">{escaped(full_diagnostic(case))}</pre></details>')
    table = ('<div class="table-wrap"><table><thead><tr><th>Model / publication or infrastructure</th>'
             '<th>Failed operation</th><th>Observed detail</th></tr></thead><tbody>'
             + ''.join(rows) + '</tbody></table></div>') if rows else '<p>No detailed failure record is available.</p>'
    inventory_html = ('<h3>Cache inventory errors</h3><ul>' + ''.join(
        f'<li>{escaped(item.get("path"))}: {escaped(item.get("reason"))}</li>' for item in inventory) + '</ul>') if inventory else ''
    evidence = '<ul>' + ''.join(f'<li>{escaped(item)}</li>' for item in errors) + '</ul>' if errors else ''
    return (f'<section id="failures"><div class="section-heading"><h2>Failures</h2>'
            f'<span>{report["counts"]["failed"]} failed selections · {len(cases)} failure cases</span></div>'
            + inventory_html + table + evidence + '<details><summary>Full failure diagnostics</summary>'
            + ''.join(diagnostics) + '</details></section>')


def failure_markdown(report):
    cases = failure_cases(report)
    errors = report.get("failures", {}).get("evidence_errors", [])
    inventory = report.get("inventory_errors", [])
    if not cases and not errors and not inventory and report["status"] != "failed":
        return []
    lines = ['## Failures', '', f'{report["counts"]["failed"]} failed selections · {len(cases)} failure cases', '']
    if inventory:
        lines += ['### Cache inventory errors', '']
        lines += [f'- {markdown(item.get("path"))}: {markdown(item.get("reason"))}' for item in inventory]
        lines.append('')
    if cases:
        lines += ['| Model / publication or infrastructure | Failed operation | Observed detail |', '|---|---|---|']
        for case in cases[:SUMMARY_ROWS]:
            lines.append(f'| {markdown(case_name(case)[:512])} | {markdown(failure_operation(case)[:256])} | {markdown(failure_detail(case)[:1024])} |')
        if len(cases) > SUMMARY_ROWS:
            lines += ['', f'Showing {SUMMARY_ROWS} of {len(cases)} cases; all cases remain in the downloadable failure evidence.']
        lines += ['', '<details><summary>Failure diagnostic excerpts</summary>', '']
        for case in cases[:SUMMARY_DIAGNOSTICS]:
            lines += [f'**{markdown(case_name(case)[:512])} · {markdown(failure_operation(case)[:256])}**', '',
                      '<pre>' + escaped(full_diagnostic(case)[:SUMMARY_DIAGNOSTIC_CHARS]).replace('[', '&#91;').replace(']', '&#93;') + '</pre>', '']
            if len(full_diagnostic(case)) > SUMMARY_DIAGNOSTIC_CHARS:
                lines += ['Diagnostic shortened here; the full retained diagnostic is in the HTML report and failure bundle.', '']
        if len(cases) > SUMMARY_DIAGNOSTICS:
            lines += [f'Excerpts shown for {SUMMARY_DIAGNOSTICS} cases; remaining diagnostics are in the downloadable evidence.', '']
        lines += ['</details>', '']
    else:
        lines += ['Detailed failure records are unavailable; inspect saved stage outcomes and raw results.', '']
    for error in errors:
        lines += [f'- Evidence unavailable: {markdown(error)}']
    return lines + ['']


def render_html(report, root):
    counts, mode, status = report["counts"], report["mode"], report["status"]
    title = "Verified" if status == "passed" and mode == "real" else ("Fixture passed" if status == "passed" else LABELS[status])
    rows = {status: [] for status in STATUSES}
    details, timeline = [], []
    header_links = "".join(f'<a href="{escaped(url)}">{escaped(label)}</a>'
                           for label, url in github_links(report).items())
    for model in report["models"]:
        selection = model["selection"]
        model_name = display_name(model)
        repo, rev = selection.get("repository"), selection.get("publication_revision")
        publication = escaped(text(rev)[:12])
        if repo and rev and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", str(repo)) and re.fullmatch(r"[0-9a-f]{40}", str(rev)):
            publication = f'<a href="https://huggingface.co/{quote(repo, safe="/")}/tree/{rev}">{publication}</a>'
        phases = [model["preparation"]["status"], combined([cycle_status(model, "load"), cycle_status(model, "readiness")]),
                  combined([cycle_status(model, "query"), cycle_status(model, "assertion")]), cycle_status(model, "unload")]
        rows[model["status"]].append(f'<tr><td><a class="model-name" href="#model-{escaped(model["id"])}">{escaped(model_name)}</a>'
                    f'<span class="subtle">{escaped(selection.get("preset"))} · {escaped(selection.get("variant"))} · {escaped(model["test_kind"])}</span></td>'
                    f'<td>{publication}</td>' + ''.join(f'<td>{badge(s)}</td>' for s in phases)
                    + f'<td>{badge(model["status"])}<span class="reason">{escaped(model["reason"])}</span></td></tr>')
        cycles = []
        for index, cycle in enumerate(model["cycles"]):
            cells = ''.join(f'<div><span>{escaped(key.title())}</span>{badge(cycle[key]["status"])}<small>{duration(cycle[key]["elapsed_seconds"])}</small></div>' for key in PHASES)
            links = ''.join(local_link(cycle[key], key.replace("_path", "")) for key in ("request_path", "response_path") if cycle.get(key))
            cycles.append(f'<section class="cycle"><div class="cycle-title"><h3>Cycle {index + 1}</h3><span>Incarnation {escaped(cycle.get("incarnation"))}</span></div>'
                          f'<div class="phases">{cells}</div><div class="code-grid"><div><h4>Actual request</h4><pre>{escaped(pretty_json(root, cycle.get("request_path")))}</pre></div>'
                          f'<div><h4>Actual response</h4><pre>{escaped(pretty_json(root, cycle.get("response_path")))}</pre></div></div><nav class="evidence">{links}</nav></section>')
            timeline.append(f'<li><strong>{escaped(model["id"])} · cycle {index + 1}</strong><span>Register → ready → query → unregister</span>{badge(cycle["status"])}</li>')
        extra = ''.join(local_link(path) for path in model.get("evidence", []))
        expected_text = model.get("expected_text")
        prepared_details = ''.join(f'<details class="nested"><summary>Prepared workload and asset identities</summary><pre>{escaped(pretty_json(root, path))}</pre></details>'
                                   for path in model.get("evidence", []) if PurePosixPath(path).name == "prepared-inputs.json")
        details.append(f'<details id="model-{escaped(model["id"])}"><summary><span>{escaped(model_name)}</span>{badge(model["status"])}</summary>'
                       f'<div class="detail-body"><p class="scope">{escaped(model["test_kind"])} · preset {escaped(selection.get("preset"))} · variant {escaped(selection.get("variant"))}</p>'
                       f'<p>Expected assertion: <code>{escaped(expected_text)}</code></p><p class="reason">{escaped(model["reason"])}</p>'
                       + ''.join(cycles) + prepared_details + f'<details class="nested"><summary>Selection identity</summary><pre>{escaped(identity(selection))}</pre></details>'
                       f'<nav class="evidence">{extra}</nav></div></details>')
    sdk = report["runtime"].get("sdk", {})
    sdk = sdk if isinstance(sdk, dict) else {}
    sdk_identity = report["runtime"].get("sdk_identity", {})
    sdk_identity = sdk_identity if isinstance(sdk_identity, dict) else {}
    runtime_title = sdk.get("release") or sdk_identity.get("release") or report["runtime"].get("release")
    hardware_title = report["hardware"].get("name") or report["hardware"].get("gpu") or report["hardware"].get("device_uri")
    source_title = report["source"].get("commit")
    environment = ''.join(f'<div><h3>{label}</h3><strong class="identity-title">{escaped(value)}</strong><details class="identity-details"><summary>Full identity</summary><pre>{escaped(identity(report[key]))}</pre></details></div>'
                          for key,label,value in (("runtime","HRX release",runtime_title),("hardware","Hardware",hardware_title),("source","Harness source",source_title)))
    contract = report.get("execution_contract", {})
    claims = [("Cache mounted read-only", contract.get("cache_read_only") is True),
              ("Fresh pinned pipeline staging", contract.get("pipeline_fresh") is True),
              ("External execution network disabled", contract.get("external_network") is False)]
    claim_html = ''.join(f'<span class="contract">{escaped(label)} · {"Recorded" if confirmed else "Unconfirmed"}</span>' for label, confirmed in claims)
    server = report["server"]
    lifecycle = ''.join(f'<div><h3>{key.title()}</h3>{badge(server[key]["status"])}<p>{duration(server[key]["elapsed_seconds"])}</p><p class="reason">{escaped(server[key].get("reason", ""))}</p></div>' for key in ("start", "shutdown", "cleanup"))
    totals = [(f'{counts["passed"]} / {counts["expected"]}', 'Test entries passed'), (str(counts['failed']), 'Failed'),
              (str(counts['not_run'] + counts['incomplete']), 'Not run / incomplete'), (str(counts['cycles']), 'Lifecycle cycles')]
    tiles = ''.join(f'<div class="tile"><strong>{value}</strong><span>{label}</span></div>' for value, label in totals)
    files = ''.join(f'<li>{local_link(path)}</li>' for path in report['evidence_files'])
    table_header = '<div class="table-wrap"><table><thead><tr><th>Model / selection</th><th>Pipeline</th><th>Inputs</th><th>Load / ready</th><th>Query / assertion</th><th>Unloaded</th><th>Outcome</th></tr></thead><tbody>'
    passing = ('<section><details id="passed"><summary>Passed (' + str(counts["passed"]) + ')</summary>'
               + table_header + ''.join(rows["passed"]) + '</tbody></table></div></details></section>') if rows["passed"] else ''
    remaining = rows["not_run"] + rows["incomplete"]
    incomplete = ('<section><h2>Not run / incomplete</h2>' + table_header + ''.join(remaining)
                  + '</tbody></table></div></section>') if remaining else ''
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'">
<title>HRX model verification · {escaped(report['attempt_id'])}</title><style>{CSS}</style></head><body><main>
<header><div class="eyebrow">HRX SYSTEMS / MODEL COVERAGE</div><div class="heading"><div><h1>Model verification</h1><p>Execution results and supporting evidence.</p></div>{badge(status,title)}</div>
<div class="run-meta"><span>{escaped(report['attempt_id'])}</span><span>{escaped(report.get('started_at'))}</span><span>{duration(report['elapsed_seconds'])}</span><span class="mode">{escaped(mode.upper())}</span>{header_links}</div></header>
<section class="notice {status}"><strong>{escaped(title)}</strong><span>{escaped(report.get('reason') or ('Every expected selection and server cleanup passed.' if mode=='real' else 'Synthetic fixture results; this is not proof of real model support.'))}</span></section>
<section class="tiles">{tiles}</section><p class="caption">{counts['distinct_models']} distinct model IDs · Rows represent configured test entries; repeated cycles increase neither count. Text smoke results cover only the tested text path.</p>
{RUNTIME_SUMMARY.render_html(report["runtime_summary"])}
{failure_html(report)}
{passing}
{incomplete}
<section><h2>Test environment</h2><div class="environment">{environment}</div><div class="contracts">{claim_html}</div></section>
<section><h2>Model evidence</h2>{''.join(details)}</section><section><div class="section-heading"><h2>One server. Sequential validation.</h2><span>{escaped(identity(server.get('identity')))}</span></div><div class="server-grid">{lifecycle}</div><ol class="timeline">{''.join(timeline)}</ol></section>
<section><details><summary>All bundled evidence</summary><ul class="file-list">{files}</ul></details></section>
<footer><span>Saved execution facts · report renderer v1</span><nav>{local_link('report.json','Final JSON')}{local_link('suite-result.json','Raw results')}{local_link('summary.md','Markdown summary')}</nav></footer>
</main></body></html>'''


CSS = '''
:root{color-scheme:light;--ink:#142f3e;--muted:#587080;--line:#dce5e9;--green:#146648;--red:#a13239;--amber:#8a6417}*{box-sizing:border-box}body{margin:0;background:#f3f6f7;color:var(--ink);font:15px/1.55 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:1420px;margin:auto;padding:48px 40px}header{border-top:5px solid #1a7776;padding-top:24px}.eyebrow{color:#216d70;font-size:11px;font-weight:800;letter-spacing:2px}.heading{display:flex;align-items:center;justify-content:space-between;gap:20px}h1{font-size:42px;letter-spacing:-1.7px;margin:10px 0 0;line-height:1.15}.heading p{color:var(--muted);margin:10px 0 20px}h2{font-size:21px;letter-spacing:-.4px;margin:0 0 18px}h3,h4{margin:0 0 10px;font-size:13px}section{margin:32px 0}.run-meta{display:flex;flex-wrap:wrap;gap:12px 24px;color:var(--muted);font-size:12px}.mode{font-weight:800;letter-spacing:1px}.badge{display:inline-block;white-space:nowrap;border-radius:6px;padding:4px 9px;font-size:11px;font-weight:750;line-height:1.4}.passed{background:#e4f3eb;color:var(--green)}.failed{background:#fbe9e9;color:var(--red)}.not_run{background:#edf0f2;color:#5b6974}.incomplete{background:#fff1d4;color:var(--amber)}.heading>.badge{font-size:15px;padding:10px 18px}.notice{display:flex;gap:20px;border:1px solid #dbe7df;border-radius:9px;padding:15px 18px;font-size:13px}.notice span{color:inherit}.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:10px}.tile{background:white;border:1px solid var(--line);border-radius:10px;padding:23px}.tile strong{display:block;font-size:31px;font-weight:700;letter-spacing:-1px}.tile span,.caption{color:var(--muted);font-size:12px}.caption{margin-bottom:36px}.section-heading{display:flex;justify-content:space-between;align-items:baseline;gap:20px}.section-heading>span{font-size:12px;color:var(--muted);overflow-wrap:anywhere}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:10px;background:#fff}table{border-collapse:collapse;width:100%;text-align:left}th{color:var(--muted);background:#f9fbfb;font-size:10px;letter-spacing:.6px;text-transform:uppercase;padding:15px 16px;border-bottom:1px solid var(--line)}td{padding:19px 16px;border-bottom:1px solid #edf1f3;font-size:12px;vertical-align:top}tr:last-child td{border:0}a{color:#176a73;text-underline-offset:3px}.model-name{font-size:13px;font-weight:700;display:block;text-decoration:none}.subtle{display:block;color:var(--muted);font-size:10px;margin-top:5px}.reason{display:block;max-width:340px;color:var(--muted);font-size:12px;margin-top:7px;overflow-wrap:anywhere}.environment{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.environment>div,.server-grid>div{padding:18px;background:white;border:1px solid var(--line);border-radius:9px;min-width:0}.environment h3{color:var(--muted);text-transform:uppercase;font-size:10px;letter-spacing:1px}pre{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;font:11px/1.7 ui-monospace,SFMono-Regular,Consolas,monospace;margin:0;color:#354f5e}.identity-title{display:block;font-size:16px;overflow-wrap:anywhere}.identity-details{border:0;margin:10px 0 0;background:transparent}.identity-details summary{padding:0;font-size:10px;color:var(--muted);justify-content:flex-start}.identity-details pre{margin-top:10px}.contracts{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}.contract{font-size:10px;color:var(--muted);border:1px solid var(--line);border-radius:5px;padding:5px 8px}details{background:white;border:1px solid var(--line);border-radius:9px;margin-bottom:12px;scroll-margin-top:20px}summary{cursor:pointer;padding:18px;display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:13px;font-weight:700}summary:before{content:'+';color:var(--muted);margin-right:2px}details[open]>summary:before{content:'−'}summary>span:first-child{margin-right:auto;overflow-wrap:anywhere}.detail-body{border-top:1px solid var(--line);padding:20px}.scope{color:var(--muted);font-size:12px;margin-top:0}.cycle{background:#f8fafb;border:1px solid var(--line);border-radius:7px;margin:18px 0;padding:18px}.cycle-title{display:flex;justify-content:space-between}.cycle-title>span{color:var(--muted);font-size:11px}.phases{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:15px 0 20px}.phases>div{display:flex;align-items:flex-start;flex-direction:column;gap:6px}.phases>div>span:first-child{font-size:10px;text-transform:uppercase;color:var(--muted)}small{font-size:10px;color:var(--muted)}.code-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}.diagnostic{padding:18px;max-height:420px;overflow:auto}.code-grid pre{max-height:300px;overflow:auto;background:white;border:1px solid var(--line);border-radius:5px;padding:12px}.evidence{display:flex;flex-wrap:wrap;gap:10px 18px;margin-top:15px;font-size:11px;overflow-wrap:anywhere}.nested{margin-top:15px}.nested>pre{padding:18px}.server-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.server-grid p{font-size:12px;color:var(--muted);margin:7px 0 0}.timeline{list-style:none;margin:20px 0;padding:0}.timeline li{display:flex;align-items:center;gap:20px;padding:12px 5px;border-bottom:1px solid var(--line);font-size:12px}.timeline li>span:not(.badge){color:var(--muted);margin-right:auto}.file-list{list-style:none;padding:0 18px 18px;font-size:12px;overflow-wrap:anywhere}footer{display:flex;justify-content:space-between;gap:20px;border-top:1px solid var(--line);padding-top:18px;color:var(--muted);font-size:11px}footer nav{display:flex;gap:16px}:focus-visible{outline:2px solid #267a87;outline-offset:3px}@media(max-width:760px){main{padding:25px 16px}h1{font-size:31px}.heading{align-items:flex-start}.heading>.badge{font-size:11px;padding:7px 10px;margin-top:12px}.notice{flex-direction:column;gap:5px}.tiles{grid-template-columns:repeat(2,1fr);gap:10px}.tile{padding:16px}.tile strong{font-size:27px}.environment,.server-grid,.code-grid{grid-template-columns:1fr}.section-heading{display:block}.section-heading>span{display:block;margin-bottom:12px}.phases{grid-template-columns:repeat(3,1fr)}.timeline li{flex-wrap:wrap;gap:7px 12px}.timeline li>span:not(.badge){width:100%;order:3}.cycle{padding:12px}.detail-body{padding:12px}footer{flex-direction:column}td,th{padding:13px 10px}table{min-width:850px}}@media(prefers-reduced-motion:no-preference){details[open] .detail-body{animation:show .14s ease-out}@keyframes show{from{opacity:.5}to{opacity:1}}}
'''


def render_markdown(report):
    counts = report["counts"]
    title = "Verified" if report["status"] == "passed" and report["mode"] == "real" else LABELS[report["status"]]
    lines = [f'# Model verification: {title}', '',
             f'**Mode: {report["mode"].upper()}** · Attempt `{markdown(report["attempt_id"])}` · {duration(report["elapsed_seconds"])}', '',
             '## Coverage', '',
             f'**{counts["passed"]}/{counts["expected"]} test entries passed** · {counts["failed"]} failed · {counts["not_run"]} not run · {counts["incomplete"]} incomplete',
             f'{counts["distinct_models"]} distinct model IDs · {counts["cycles"]} lifecycle cycles', '',
             'Rows represent configured test entries; repeated cycles do not increase model coverage. Text smoke results cover only the tested text path.', '']
    if report["mode"] == "fixture":
        lines += ['**Fixture results — not proof of real model support.**', '']
    if report.get("reason"):
        lines += [markdown(str(report["reason"])[:512]) + ("… (full reason retained in results)" if len(str(report["reason"])) > 512 else ""), '']
    if report.get('selection_mode') == 'cached':
        lines += ['Selection: all published runner-cache selections, deduplicated and frozen before preparation.',
                  'Decision smoke checks the single-choice service contract, not learned decision quality.', '']
    lines += RUNTIME_SUMMARY.markdown(report["runtime_summary"])
    lines += failure_markdown(report)
    passed = [model for model in report["models"] if model["status"] == "passed"]
    remaining = [model for model in report["models"] if model["status"] in ("not_run", "incomplete")]
    if passed:
        lines += [f'<details><summary>Passed ({len(passed)})</summary>', '',
                  '| Model / publication | Preset / variant | Outcome |', '|---|---|---|']
        for model in passed[:SUMMARY_ROWS]:
            sel = model["selection"]
            lines.append(f'| {markdown(display_name(model)[:512])} | {markdown(sel.get("preset"))} / {markdown(sel.get("variant"))} | Passed |')
        if len(passed) > SUMMARY_ROWS:
            lines += ['', f'Showing {SUMMARY_ROWS} of {len(passed)} passed selections; full results are in the report.']
        lines += ['', '</details>', '']
    if remaining:
        lines += ['## Not run / incomplete', '', '| Model / publication | Outcome | Reason |', '|---|---|---|']
        for model in remaining[:SUMMARY_ROWS]:
            lines.append(f'| {markdown(display_name(model)[:512])} | {LABELS[model["status"]]} | {markdown(model["reason"][:1024])} |')
        lines.append('')
    lines += ['## Runtime and hardware', '']

    def scalar(obj, key):
        value = obj.get(key) if isinstance(obj, dict) else None
        return value if isinstance(value, (str, int, float)) and not isinstance(value, bool) else None

    runtime, hardware, source = (report[key] for key in ("runtime", "hardware", "source"))
    gpu = scalar(hardware, "name") or scalar(hardware, "gpu") or "Unavailable"
    device = scalar(hardware, "device_uri")
    driver = scalar(hardware, "driver")
    gpu_details = ' · '.join(str(value) for value in (device, driver) if value is not None)
    lines.append(f'- **GPU:** {markdown(gpu)}' + (f' · {markdown(gpu_details)}' if gpu_details else ''))
    lines.append(f'- **Container platform:** {markdown(scalar(runtime, "platform") or "Unavailable")}')
    lines += ['', '## Source and run', '']
    commit = scalar(source, "commit")
    source_label = str(commit)[:12] if commit is not None else "Unavailable"
    if source.get("dirty") is True:
        source_label += ' · local changes'
    elif source.get("dirty") is False:
        source_label += ' · clean checkout'
    lines.append(f'- **Harness source:** {markdown(source_label)}')
    links = github_links(report)
    for label, url in links.items():
        lines.append(f'- [{label}]({url})')
    workflow = report.get("workflow")
    runner = scalar(workflow, "runner")
    if runner is not None:
        lines.append(f'- **Runner:** {markdown(runner)}')
    lines += ['', '## Server lifecycle', '']
    for key, label in (("start", "Server start"), ("shutdown", "Server shutdown"), ("cleanup", "Server cleanup")):
        outcome = report["server"][key]
        observed_time = outcome.get("elapsed_seconds")
        detail = f' · {duration(observed_time)}' if observed_time is not None else ''
        reason = f' · {markdown(outcome["reason"])}' if outcome.get("reason") else ''
        lines.append(f'- **{label}: {LABELS[outcome["status"]]}**{detail}{reason}')
    lines += ['', '## Artifacts', '']
    run_url = next((url for label, url in links.items() if label.startswith('GitHub run #')), None)
    if run_url:
        lines.append(f'[Download the HTML report, JSON results and supporting evidence from this run’s artifacts]({run_url}).')
    elif isinstance(workflow, dict) and any(key in workflow for key in ("run_id", "run_url")):
        lines.append('Download the HTML report, JSON results and supporting evidence from the workflow’s artifact bundle.')
    else:
        lines.append('[HTML report](index.html) · [Final JSON](report.json) · [Raw results](suite-result.json)')
    lines += ['', 'Full runtime, hardware and source identities are retained in the downloadable JSON results.', '']
    return '\n'.join(lines)


def generate(results, output, failures=None):
    root = results.resolve(strict=True)
    require(root.is_dir(), "Results must be a directory")
    output = output.absolute()
    require(not output.exists() and not output.is_symlink(), "Output directory must not already exist")
    require(not output.resolve().is_relative_to(root), "Output must be outside the saved results directory")
    report = normalize(root)
    report["runtime_summary"] = runtime_identity(report, root)
    report["renderer"]["runtime_summary_sha256"] = hashlib.sha256(Path(__file__).with_name("runtime-summary.py").read_bytes()).hexdigest()
    report["failures"] = load_failures(report, root, failures)
    rendered = render_html(report, root)
    summary = render_markdown(report)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".model-report-",dir=output.parent) as name:
        staged = Path(name) / "report"
        staged.mkdir()
        read_json(root / "suite-result.json")  # Recheck the bounded regular-file policy before copying.
        shutil.copyfile(root / "suite-result.json", staged / "suite-result.json")
        for relative in report["evidence_files"]:
            destination = staged / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(evidence_path(root,relative),destination)
        (staged / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False)+'\n')
        (staged / "index.html").write_text(rendered)
        (staged / "summary.md").write_text(summary)
        staged.rename(output)
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--failures', type=Path, help='Saved failures.json from failure collection')
    args=parser.parse_args(argv)
    try:
        report=generate(args.results,args.output,args.failures)
    except (OSError,ReportError,TypeError,KeyError) as error:
        print(f'Report generation failed: {error}',file=sys.stderr)
        return 1
    print(f'Report generated: {args.output / "index.html"} (suite {report["status"]})')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
