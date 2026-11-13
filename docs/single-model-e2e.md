# One published model, end to end

This document describes the earlier synthetic/prerequisite skeleton. For the
current real model path, use the [independent Bash stages](specs/pipeline-stages.md)
and [local/GitHub command guide](run-cached-models.md). The manual workflow now
invokes real preparation, shared-server execution and report generation.

Status: **stage skeleton**, with a runnable synthetic fixture and manual GitHub
hookup. Real acquisition, GPU admission and serving adapters are not implemented.
This scopes the first model rather than implementing the full nightly catalog.
A passing fixture is never model coverage.

The first implementation milestone is the
[basic end-to-end epic](acquisition-spike.md), Beads `hst-utq`. It assumes an
operator-selected compatible model/host and includes real readiness, one bounded
inference assertion and cleanup after proving cache reuse. Automatic hardware
selection and qualification are later work; a download-only probe is debugging
groundwork, not completion of this epic.

## Selection and workload

Start with `loom-fun/unsloth-Qwen3.5-0.8B-loom`, publication
`f1ba2f23c44400bd0b1f88b406029daf41d7a143`, preset `chat`, variant `gguf`,
program set `source`. The selected model ID is `Qwen/Qwen3.5-0.8B`.
`config/cached-model-tests.json` records this choice; runtime pins live in
`config/runtime.json` and test behavior in `config/model-test-profiles.json`. The reference asset lock selects:

| Asset | Bytes | Reuse policy |
|---|---:|---|
| `Qwen3.5-0.8B-Q4_K_M.gguf` | 532,517,120 | Persistent verified asset |
| `mmproj-F16.gguf` | 204,987,232 | Persistent verified vision companion |
| `tokenizer.json` | 9,832,586 | Persistent verified asset |
| Card, lock, programs and auxiliary program inputs | Determined by selected closure | Independent verified copies of host-seeded pipeline originals |

These values were checked against the inventoried publication's `model.json`
and `model.json.lock`. The asset origin is pinned to
`unsloth/Qwen3.5-0.8B-GGUF@6ab461498e2023f6e3c1baea90a8f0fe38ab64d0`.
The pipeline is the published Q4 selection; it is not the curated Q5 fixture from
hrx-solutions. Resolve the actual selected closure with native card/lock semantics,
including auxiliary `weights.loom`, rather than downloading only the main GGUF.

The published preset specifies **4 rows**, **262,144 context capacity**,
327,680 pool capacity, 1,024 prefill capacity, 4,096 max tokens and draft depth 3.
A request limited to 8 output tokens does not reduce those allocation settings.
Keep them in the run snapshot. Any reduced-context smoke profile must be an explicit operator-reviewed
execution override, recorded separately from the published preset. The first
epic assumes that selected workload fits its host; automatic qualification is later.

## Stage boundaries

| Stage | Real adapter acceptance | Skeleton evidence |
|---|---|---|
| Resolve | Freeze publication/preset, base image, per-run SDK and workload; record the operator-selected compatible host assumption | Selection and CLI references in `plan.json` |
| Acquire | Operator seeds assets and pipeline originals; CI downloads its SDK and verifies read-only cached inputs, reporting cache misses | Fixture stages a new synthetic pipeline and reuses one content-addressed synthetic asset |
| Admit | Bind the operator-selected compatible GPU/workload and record the assumption; automatic suitability qualification is later | Synthetic eligible/unknown/insufficient decisions only |
| Serve | Launch owned container/server; wait for expected model in `/v1/models` | Loopback synthetic HTTP server becomes model-ready after multiple probes |
| Assert | Bounded `/v1/chat/completions` query; known answer `4`; record response | Synthetic answer and deliberately wrong-answer cases |
| Cleanup | Shut down owned server/container on success, failure and timeout; preserve asset cache and reports | Owned fixture server stopped on success, readiness timeout and assertion failure |

The fixture is an innocuous host-side Python contract check: it does not launch
Docker or execute published programs. It neither qualifies containment nor GPU
memory. The existing runner bootstrap separately checks Docker mount isolation.
The real adapters must execute manual asset seeding, CI input verification and
inference in the Docker boundary specified in
[the working design](design.md#container-execution-and-containment). Only manual
seeding may write model caches in v1; CI reports misses for manual reseeding.

## Local skeleton check

Use a task-owned external venv and output/cache root:

```sh
python3 -m venv /external/single-model-venv
/external/single-model-venv/bin/python -m unittest discover -s tests -v
/external/single-model-venv/bin/python scripts/single-model-e2e.py \
  --mode fixture --cache /external/single-model-cache \
  --output /external/single-model-runs/first
/external/single-model-venv/bin/python scripts/single-model-e2e.py \
  --mode fixture --cache /external/single-model-cache \
  --output /external/single-model-runs/second
```

Output directories must be new. The second report should show asset reuse with a
new pipeline staging path. Synthetic bytes use a dedicated
`single-model-skeleton-fixtures` cache namespace and never stand in for real
weights. `--fixture-failure memory|readiness|answer` exercises failure reports.
Every initialized attempt writes `plan.json`, `report.json` and `summary.md`;
a completed query also writes `response.json`. Exit 0 means `fixture_passed`,
2 means blocked, and 1 means failed. Reports always state `model_tested: false`.
No report from this skeleton can be presented as passing real-model coverage.

## Manual GitHub hookup

The new `.github/workflows/single-model-e2e.yml` has only `workflow_dispatch`,
uses `[self-hosted, linux, hrx-systems-test-bootstrap]`, serializes single-model
hardware jobs, and uploads reports even on failure. The default `fixture` input
needs no image, model credentials or downloaded assets. It writes outputs beneath
`HRX_SYSTEMS_ROOT/runs/single-model/RUN_ID-ATTEMPT`, with the existing external
state-root default when that repository variable is absent.

After reviewing and publishing this workflow on the default branch:

```sh
gh workflow run single-model-e2e.yml --repo rsuderman/hrx-systems-test -f mode=fixture
```

Choosing `real` currently produces a **blocked report**, without downloading or
executing anything. `SINGLE_MODEL_IMAGE` reserves the digest-pinned **base**
container image; it is not the source of the Loom SDK. The real adapter will
fetch the separately pinned known SDK release and install it in a fresh per-run
venv, reusing verified release bytes when available. `SINGLE_MODEL_QUALIFICATION` is reserved for later automatic hardware
qualification, not a prerequisite for the basic compatible-host run. These are
future inputs;
setting them does not implement missing integrations or enable real execution.
Only report/plan/summary/response files are uploaded, never pipeline or asset
payload directories. No nightly schedule is introduced.

## Work required for the first real run

1. Pin a compatible OS/Python base image and a known compatible HRX SDK release
   artifact. Every run uses these same pins; no latest-build selection is needed. Install it and its dependency closure in a fresh container venv,
   use explicit tool paths and record their identity. Verify native help and
   card/target support using that pinned release. `plan.json` contains native CLI
   references, not an installation recipe for an older hrx-solutions wheel.
2. Implement the acquisition boundary: fresh card/lock/program downloads per run,
   reusable asset storage, complete closure verification, immutable ready state,
   and container-visible shared-file paths. Native `loom-fetch --materialize=auto`
   copies programs but its HF/shared content caches can still reuse them; an
   ephemeral Loom document cache alone does not prove fresh program downloads.
   Test this split with tiny fixtures, then two manual seed invocations for the
   selected compatible model. CI must report misses without repairing
   the read-only asset cache.
3. Use the operator-selected compatible host/model/workload and record its
   identity and effective settings. Automatic hardware/memory qualification is
   later work; actual load or resource failures still fail this basic test.
4. Bind the operator-selected GPU and launch one owned
   test container with read-only acquired inputs and separate writable state.
   Execute the bounded readiness/query/assertion client inside that boundary,
   without host networking, model-execution credentials or a Docker socket.
5. Run the single published selection manually; inspect readiness, answer,
   observed memory and cleanup evidence. Repeat with a new verified pipeline staging
   and the warm asset cache. Only then expand model/feature coverage or schedules.

The base image, per-run SDK, cache reuse and real serving lifecycle are this
epic's checkpoints. Host/model compatibility is an explicit assumption; automatic
qualification follows later. The synthetic fixture substitutes for none of the
required real-run evidence.
