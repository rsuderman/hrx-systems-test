# Later: nightly scheduling and model coverage

Bead: `hst-qcw`. Implement after the basic end-to-end epic `hst-utq` passes.
This is not a prerequisite for that epic. Reuse its validated manual execution
path; do not introduce another worker, downloader or result protocol.

## Inputs and ownership

- A reviewed expected list of model/preset/test selections and assigned hardware.
  Begin with the proven selection; expand explicitly rather than enabling all 60.
- The same pinned base image, known HRX release/dependencies, pipeline revisions,
  read-only model caches and per-attempt state conventions as manual execution.
  A scheduled trigger does not select newer HRX builds or silently update pins.
  Use shared `config/runtime.json` plus the selected suite and read the operator's
  `$root/cache/seed-manifest.json`; do not create another selection or seed index.
- A selected UTC schedule, bounded execution window and retained artifact policy.
  The workflow owns triggering and serialization; the validated manual entry point
  continues to own preparation, readiness, inference, assertions and cleanup.

## Execution and result contract

The proposed workflow adds a scheduled trigger beside manual dispatch and calls
`bash scripts/run-cached-models.sh --runtime config/runtime.json
--selection cached --root "$root" --output "$attempt"`, the same wrapper owned by
[the workflow task](github-workflow.md). It may pass the shared `--seed-manifest`
override for an explicitly isolated input set. Use common concurrency group
`hrx-single-model-hardware` across manual and scheduled runs, independent of
workflow/ref, with cancellation disabled. Do not overlap the assigned GPU.

Snapshot the full expected job set before execution. Each selection must end in
the planned result vocabulary: passed, failed, blocked or not run, with a reason.
Missing seeded assets, incompatible/unknown capacity, busy hardware, timeouts and
inference failures remain in the denominator. Continue independent models after
a model-specific failure when the host is safe to use; otherwise mark remaining
work not run. There is no automatic model-cache repair or write mount.

Consume the parent contract's per-attempt bundle: workflow-owned `plan.json`,
aggregate `report.json`, `summary.md` and `workflow.json`; serving-owned
`serve-result.json`, `request.json`, `response.json` and bounded logs; plus the
small seed/prepared-input and stage-specific SDK receipts needed for provenance.
The nightly layer does not overwrite stage or aggregate reports. Compare pinned
artifact/dependency identities, not physical `/run/venv` instances across containers.
Add a small coverage summary: expected, attempted, passed and incomplete
counts plus per-selection outcomes and artifact references. Record the intended
night and actual start so a delayed run is distinguishable from a missing one.
Preserve exact source, input and runtime identities; retries get distinct attempts.
Upload compact reports/logs on failure too, never cache payloads. Report upload
failure separately from model failure and retain the local evidence for recovery.

Completion is green only when every required expected check passes and its
results are published. Any required blocked/not-run/missing result is partial
coverage and fails the nightly gate by default; explicit catalog exclusions stay
visible. This policy does not silently shrink the catalog to models that ran.

## Acceptance and remaining decisions

Show manual and scheduled triggers invoking the same proven path and pins;
overlap attempts serialize. A fixture matrix containing pass, failure, cache miss
and timeout yields the correct full-denominator summary and non-success gate.
Demonstrate retained failure artifacts and a real scheduled run of the proven
single model before expanding coverage. Choose the clock time, execution window
and log/artifact retention before enabling the schedule; inherit the manual
workflow's retention policy until a reviewed override is selected. Automatic seeding,
latest-build tracking and head-node fanout are separate future work.

Contract source: [shared epic](basic-e2e.md), [manual workflow](github-workflow.md),
[validation](validation.md) and [results/coverage design](../design.md#results-and-coverage).
Manual seeding and its offline warm-reuse proof remain operator work; scheduled
read-only input verification is not a substitute for that seeder proof.
