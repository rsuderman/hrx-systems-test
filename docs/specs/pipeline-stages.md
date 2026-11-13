# Local and CI stages: prepare, run, report

## Public commands

Each stage has a Bash entry point and Python implementation. The same commands
run locally and in separate GitHub steps. No stage requires GitHub metadata. Execution accepts optional `--workflow FILE`
for bounded run metadata emitted by composition; it copies that record into raw
results/evidence rather than requiring post-run mutation.
The names/flags below are proposed testing-repository scripts, not SDK commands.

```sh
bash scripts/test/prepare-cached-models.sh --output "$attempt/prepared"
bash scripts/test/run-prepared-models.sh --prepared "$attempt/prepared" --output "$attempt/results"
bash scripts/report/generate-model-report.sh --results "$attempt/results" --output "$attempt/report"
```

Preparation defaults to the checked-in suite and existing `HRX_SYSTEMS_ROOT`
convention; explicit suite/root/output options support local isolation. Commands
print their output locations. Use fresh explicit destinations, retaining the
prepared directory until execution completes and the raw results for re-rendering.
The optional `run-cached-models.sh` convenience command merely composes these
three public stages, preserves their exit statuses and prints final paths.

## 1. Prepare: acquire and verify inputs

Owner: `hst-utq.3`, reusing completed SDK bootstrap and seeding helpers.

- Read/freeze the suite, baseline configs and seed manifests. Pull the pinned
  container image and download/verify the selected SDK and dependencies into this
  attempt. Model assets and original pipeline inputs are
  explicitly host-seeded beforehand. Verify and stage exact pipeline cache copies;
  misses fail with a reseed command and no download fallback.
- Verify/assemble model trees with asset-cache mounts read-only in a
  network-disabled phase. Preserve selected preset/workload and source identities.
  No GPU/server/model registration is required for preparation.
- Write `prepared-suite.json`: version, frozen expected entries, each entry's
  ready/failed preparation status and reason, relative tree/manifest paths,
  immutable SDK/image/source identities and the required cache mount contract.
  Per-entry prepared manifests retain server-visible paths under
  `/inputs/<entry-id>/model`. Include baseline/dependency-lock snapshots and
  verified SDK payloads needed for offline execution. Do not require later
  stages to reread mutable source configs or acquisition-container venv paths.
- Remove acquisition containers/venvs and transient download scratch. Retain the
  prepared output and per-attempt SDK payloads for the next stage, not as a
  persistent cross-run SDK cache. Preparation failures retain compact evidence
  and a manifest when possible; exit nonzero if any expected entry failed.

## 2. Run: execute prepared inputs and save raw evidence

Owner: `hst-40e`, including public Bash command, Python host/container
orchestration, GPU access, lifecycle and raw evidence.

- Consume `prepared-suite.json`, confirm its identities and required files/cache
  references, and perform runner GPU/device/access checks. No pipeline/model/SDK
  download, source resolution or seeding. Require the prepared image locally
  (`docker run --pull=never`) and verify/install staged SDK wheels offline into
  the runtime container's fresh venv.
- Mount prepared trees/SDK payloads and model caches read-only, use writable
  runtime scratch/results, and run with external networking disabled. Start one
  empty server, register/query/unregister ready entries sequentially and stop
  it once. Preserve a result for every frozen expected entry, including failures
  from preparation. Continue only after confirmed removal and healthy server.
- Write versioned `suite-result.json` plus per-model raw lifecycle/request/response
  and bounded shared logs. Include expected selections, preparation results,
  source/runtime/hardware identities, phase outcomes/times and final shutdown.
  Copy the compact preparation/SDK evidence needed to interpret these results;
  artifact references are relative to the results directory. Raw results remain
  usable without the prepared directory, cache, source checkout or live server.
- Exit nonzero if any expected entry fails/is not run or final cleanup fails.
  Save partial evidence when the process retains control. Rendering/report
  presentation is not part of execution and cannot alter observed outcomes.

## 3. Report: turn saved results into all presentation artifacts

Owner: `hst-utq.6`; verification: `hst-utq.5`.

- Read only the saved results/evidence directory. No Docker, GPU, SDK install,
  model cache, credentials or network access is required. This stage is independently
  runnable after execution and repeatable for the same results.
- Normalize/validate raw results, then generate final `report.json`, standalone
  `index.html`, GitHub `summary.md` and the referenced compact evidence tree in
  a separate report directory. All views share the same facts/status/counts.
  See [the presentation contract](results-report.md).
- Copy only referenced bounded evidence, never model/pipeline/SDK payloads.
  Do not modify raw results or infer success from missing evidence. Keep renderer
  version/source identity separate from the original execution source identity.
- A valid report of failed tests is still successful report generation (exit0).
  Invalid input, missing required evidence or rendering failures exit nonzero.
  Test failure remains in `suite-result.json`/`report.json` and the run-stage exit;
  successful rendering must not make a failing workflow green.

## Version1 handoff fields

Use UTF-8 JSON, `schema_version: 1`, an `attempt_id`, a frozen ordered expected
entry list and explicit source/runtime identity. IDs are unique path-safe names;
validate all relative references remain inside their designated bundle/cache root.
Reject unsupported versions or duplicate entries. Never load arbitrary result
paths outside the compact evidence tree.

| File | Required content |
|---|---|
| Repo suite config | `schema_version`, `entries[]` with unique `id`, repo-relative `baseline`, cache-relative `seed_manifest` |
| `prepared-suite.json` | `attempt_id`, harness `source`, frozen `entries[]`, common `runtime` image/platform/SDK identity and staged SDK/config paths, required host-cache/container mount mapping |
| Prepared entry | `id`, copied selection/request/workload identity, `preparation` status (`ready` or `failed`) and reason, relative ready `inputs_manifest` or null, compact evidence references |
| Per-entry `prepared-inputs.json` | selection/model ID, server-visible `model_card_path`, card/lock/program hashes, verified asset root-relative paths/sizes/digests, SDK identity, effective workload and request/assertion |
| Raw `suite-result.json` | `attempt_id`, `mode` (`real` or `fixture`), `source`/`runtime`/`hardware` identity objects, frozen `expected_entries[]`, overall `status`/`reason`, `started_at`/`ended_at`/`elapsed_seconds`, `server` identity/start/shutdown/cleanup evidence and `models[]` |
| Raw model result | expected entry `id` and selection identity, preparation result, overall status/reason, `cycles[]`, compact relative evidence references |
| Raw cycle | `model_id`, `incarnation`, `load`/`readiness`/`query`/`assertion`/`unload` phase outcomes, `request_path`, `response_path` relative evidence references |
| Phase outcome | `status` (`passed`, `failed`, `not_run` or `incomplete`), reason when not passed; optional bounded observations/evidence paths |

Optional top-level raw `workflow` contains repository/run_id/run_attempt/ref/sha/
runner/run_url metadata, with a bundled `workflow.json` evidence copy. It cannot
override the recorded execution source/runtime/observations. Use validated
structured repository/run ID/commit fields for report run/source links; local
runs without these fields render plain identities.

A concrete fixture is maintained at
[tests/fixtures/model-report/passing/suite-result.json](../../tests/fixtures/model-report/passing/suite-result.json).
It is explicitly fixture evidence, not real support. Expected entries carry
`id`, `selection`, `test_kind` and baseline `expected_text`; phase timing is
nullable `elapsed_seconds`. Optional `execution_contract` records observed
`cache_read_only`, `pipeline_source=host_seeded` and `external_network`; legacy
`pipeline_fresh` remains readable for saved older runs. Omitted or unknown
values must display unconfirmed rather than claim successful isolation.

Every expected entry has exactly one model result, with repeated attempts stored
in its cycles. Entry coverage counts and deduplicated known model-ID counts are
separate; presentation must not label the number of test entries as unique models. Distinct configurations may share a model ID but retain different
entry IDs. Missing/unattempted phases are never successful. A phase that failed
cannot be erased by a later retry; record the additional cycle and keep the
attempt failed unless an explicit future retry policy is approved. `mode: fixture`
results cannot support real coverage claims.

Prepared bundle layout: `prepared-suite.json`, staged `sdk-payloads/`, copied
`config/` and `models/<entry-id>/{model/,prepared-inputs.json,...}`. Mount the
prepared `models/` tree at `/inputs`, retaining asset roots at `/model-cache/...`.
Raw result layout: `suite-result.json`, `models/<entry-id>/` evidence, bounded
shared `server.log` and available `workflow.json`/stage identity receipts. Raw
results copy their required compact preparation evidence; they reference no
required artifacts under the temporary prepared tree or model cache. The report
stage copies allowed evidence at the same relative paths and adds final
`report.json`, `index.html` and `summary.md`, with renderer source/version recorded
separately from the execution identity. Stable recorded times remain unchanged
when regenerating; do not replace them with report-generation time.

## GitHub composition and acceptance

Use separate Prepare, Run and Generate report steps with explicit output paths.
Run may execute ready entries from a partial prepared manifest, but must preserve
preparation failures and exit nonzero for that suite. If no usable preparation
manifest exists, do not start a server; retain preparation diagnostics. Always
attempt reporting when raw evidence exists and upload available compact report/raw
evidence even if an earlier stage failed. Preserve aggregate failure rather than
masking it with a later successful stage. Handle cancellation with owned cleanup
before final evidence collection when control remains available.

Prove execution succeeds with acquisition disabled and a prepared input set,
then regenerate the HTML/Markdown/JSON twice from saved results without Docker,
GPU or cache access. Also prove a failed run renders accurately without changing
its failed outcome. Prepared inputs are per-attempt staging and may be cleaned
explicitly after execution; raw results/report are retained according to policy.
