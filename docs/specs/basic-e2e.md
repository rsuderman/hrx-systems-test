# Basic end-to-end specification contracts

Planning pass for epic `hst-utq`. Beads owns scope, dependencies and status;
per-task specifications live alongside this document. Implementation is separate.

## Agreed outcome

One operator-selected compatible host/model runs a real GitHub test: manual
asset seed, pinned pipeline and known HRX release, read-only model inputs,
prepared-model readiness, bounded query/assertion, owned shutdown and artifacts.
Synthetic or input-only checks do not complete the epic. Automatic hardware
selection/qualification (`hst-otm`) and nightlies (`hst-qcw`) are later work.

## Shared constraints for task authors

- Use the pinned Qwen3.5 0.8B publication/preset in the existing selection config
  as the initial baseline; keep the published workload unless an explicit
  operator-selected override is recorded. Assume a compatible host/model. Do not
  gate this epic on a future memory qualifier or automatic platform selector.
- Use one known compatible released `hrx-loom` SDK with verified digest and one
  digest-pinned OS/Python base. Verify the proposed release/card pair before
  calling it known compatible. No latest-build tracking, implicit artifact/source
  fallback, host SDK or image-baked SDK. Install the pinned SDK/dependencies in a
  fresh venv from per-attempt downloads; no persistent SDK payload cache.
- Operator-run seeding uses `loom-fetch`/native card-lock semantics and existing
  HF/Loom asset caches. CI mounts model asset storage read-only, reports misses
  for manual reseeding and never repairs it. Pipeline card/lock/programs are
  downloaded into fresh per-attempt staging from their unchanged publication
  pin. The pipeline/asset split must use real supported interfaces; do not assume
  `--materialize=auto` or an ephemeral document cache forces program redownloads.
- Retain existing external-root conventions: `$HRX_SYSTEMS_ROOT/cache/huggingface`,
  `cache/loom-assets`, and `runs/<attempt>/...`. SDK/dependency payloads live
  under the attempt, not the persistent model cache.
  Preserve consistent container-visible shared-file paths and separate writable
  runtime state/results from immutable inputs. Do not create a new weight store.
- The parent/root plan owns shared filenames/manifest decisions and dependency
  graph changes after cross-review. Initially propose the smallest contracts in
  each task spec; resolve inconsistent assumptions with adjacent owners.
- Workflow uses the existing dedicated self-hosted runner labels and is manual
  only. Artifacts contain compact identities/results/logs, never weights/cache
  payloads. Token/SDK/model-source access is acquisition-scoped. No sudo by agents.
- Source CLI/data/API claims need local HRX/card evidence or primary references.
  Clearly mark a proposed wrapper versus an existing SDK API. Passing parsing,
  a helper script or fixture is not real inference proof.

## Authoring and cross-resolution

Each non-epic Bead has one specification owner and one uniquely named spec file.
Keep current-epic specs about one page each and later-work specs shorter. Include
inputs/outputs, ownership, proposed command boundary, dependency contracts,
meaningful acceptance checks and unresolved implementation facts. Avoid creating
implementation, workflow code or a general scheduler/framework.

Authors attach their spec to their own Bead and report ready for cross-review.
Then adjacent authors check producer/consumer contracts and correct their own
files. The coordinator resolves shared decisions, checks the whole path still
reaches actual inference and records consensus. Specs do not close the underlying
implementation issues. No commits, publication, model downloads or runner changes
are part of this planning pass.

## Cross-resolved interfaces

These are proposed testing-repository interfaces to implement, not SDK commands.
The existing `config/single-model-e2e.json` is the single baseline input: retain
its selected model/request and extend it with known SDK release, dependency-lock
and base-image pins. Do not create a competing model-selection config. Device
URI is an explicit operator/host input, not automatic hardware qualification.

Two public host wrappers are sufficient:

```sh
bash scripts/seed-models.sh --baseline config/single-model-e2e.json \
  --root "$root" --output "$seed_attempt"
bash scripts/run-single-model.sh --baseline config/single-model-e2e.json \
  --root "$root" --output "$attempt"
```

The test repository owns all seed configuration and tooling. The operator can
run `bash scripts/seed-models.sh` with no arguments: use the checked-in baseline,
`HRX_SYSTEMS_ROOT` (default `$HOME/.local/share/hrx-systems-test`) and an automatically
created attempt output directory. The explicit options above are overrides.
Configure the runner to use that same cache root, especially across accounts.
Operators do not author model cards, asset lists or container invocations.

Task .2 owns the seed wrapper. Task .4 owns the run wrapper, named outer
containers, cancellation cleanup and aggregate result; it invokes .1/.3/40e
helpers rather than duplicating their logic. Default seed index for this single
baseline is `$root/cache/seed-manifest.json`, atomically published by .2 after
successful verification. Both wrappers may accept an explicit `--seed-manifest`
path for isolated validation. CI reads the index and model caches only. Private
helper spellings are owned by their stage, not additional public APIs.

| Producer | Output consumed by the next stage |
|---|---|
| SDK bootstrap (.1) | `sdk.json`: pinned image/release/dependency identity, observed installation/tool paths |
| Manual seed (.2) | `seed-manifest.json`: selection identity plus reusable `files[]` role/origin/revision/size/digest and designated root-relative cache location; `seed-report.json` records this invocation |
| CI preparation (.3) | `prepared-inputs.json`: selection/model/card/asset/program identities, effective workload/request/assertion and SDK identity |
| Serving (40e) | `serve-result.json`, `request.json`, `response.json`, bounded `server.log`; never overwrites aggregate report |
| Run/workflow (.4) | `plan.json`, aggregate `report.json`, `summary.md`, and `workflow.json` with GitHub/source/runner identity |
| Validation (.5) | Small closure summary linking seed receipts and two real GitHub runs/artifacts |

Preserve per-container SDK receipts under stage-specific artifact subdirectories;
compare immutable release/dependency/image identities, not identical physical
venv instances. Each container installs its own SDK at `/run/venv`; never share
or relocate an installed venv between containers. SDK payloads are temporary per-attempt downloads,
separate from persistent read-only model caches.

The common container input layout is `/model-cache/huggingface`,
`/model-cache/loom-assets`, `/inputs/model/model.json`, and
`/inputs/prepared-inputs.json`. Serving mounts these read-only, creates its own
writable `/run/venv` and scratch, and emits stage outputs to `/run/results`.
The workflow owns device-node access and required numeric supplementary groups
for the assigned non-root GPU container. The client owns only its native server
process group. Execution uses container loopback with external network disabled.
Proposed 180s readiness, 60s query and 10s graceful shutdown budgets fit beneath
an explicit outer job budget (initially 15 minutes), which must leave cleanup and
artifact time. Forced stop of owned processes is allowed; confirmed cleanup is
required for pass. Use shared `hrx-single-model-hardware` workflow concurrency.

### Warm seed proof versus read-only CI verification

A second invocation of the **actual manual seeder** must exercise its real asset
resolution/materialization path. Stage the pinned pipeline online first, then
run the warm asset phase in a network-disabled container. A proposed wrapper
option `--asset-network offline` selects that network policy only; it must not
substitute a different verification-only algorithm for the seed path. Manual
seed caches may remain writable for model-acquisition metadata/receipts; CI model caches are
always read-only. The separate .3 offline/read-only check proves CI consumption,
not by itself that the seeder avoids repeat downloads.

Do not require nonexistent `loom-fetch --json` byte counters. Report measured
transfer events only if supported; verified asset sizes are not wire bytes.
Successful warm seeding with asset-phase networking disabled demonstrates zero
asset payload transfer. Fresh pipeline acquisition remains online and is recorded
separately. If this phased offline seed path cannot be implemented with the
pinned SDK, report the exact seam; do not replace its proof with file mtimes or
a synthetic fixture.

## Specification index

| Bead | Specification |
|---|---|
| hst-utq | This shared contract and [epic scope](../acquisition-spike.md) |
| hst-utq.1 | [SDK release](sdk-release.md) |
| hst-utq.2 | [Manual seed](manual-seed.md) |
| hst-utq.3 | [CI inputs](ci-inputs.md) |
| hst-40e | [Serving test](serve-test.md) |
| hst-utq.4 | [GitHub hookup](github-workflow.md) |
| hst-utq.5 | [Validation](validation.md) |
| hst-otm | [Later hardware selection](later-hardware.md) |
| hst-qcw | [Later nightly coverage](later-nightly.md) |
