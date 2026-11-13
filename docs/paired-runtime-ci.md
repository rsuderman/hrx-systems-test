# Paired release and CI verification

The normal main push, nightly schedule and manual default compare the latest stable release with the latest eligible successful main SDK build. The outer workflow owns the hardware concurrency lock for the entire comparison. Its reusable worker has no competing lock.

The planner freezes the ordered model denominator, exact seed bytes and aliases, authored suite entries, repository configuration hashes and both runtime references before either lane starts. Resolution failures become frozen error references: preparation accounts for every selected entry as a runtime setup failure without retrying a moving alias. A missing plan fails the comparison before GPU execution.

Each lane independently stages verified original pipelines from the host-seeded read-only cache, acquires its selected SDK once, starts one shared server, runs all selected entries, confirms teardown and publishes its report and bounded evidence. The release lane precedes the CI lane. Model, assertion, acquisition, rendering and upload failures do not by themselves block the second lane. The second lane requires confirmed cleanup: an unknown teardown, forced server shutdown or forced model removal produces a blocked second result with every entry marked not run. Cancellation prevents new GPU work; existing cleanup and aggregation remain eligible.

The comparison artifact contains `paired-result.json` and `summary.md`; each lane has its own report, failure bundle and `lane-result.json`. Results retain SDK identities, the common plan digest, frozen inputs, runtime policy and any migration reports. An unavailable lane result is a failed comparison, never a successful partial run. Runtime 0.1.0 keeps its published pipelines; the qualified 0.2.0 family uses the versioned upgrader and pinned runtime policy in either lane.

Manual `runtime_source` choices retain single-lane diagnostics: `release-latest`, `actions-main`, configured pinned `release`, and exact `actions-replay` with run, attempt, artifact and wheel digest. Local preparation defaults remain unchanged.

For local metadata-only planning, with `inputs.json` containing `{}`:

```sh
python3 -I scripts/paired-ci.py plan --root "$HRX_SYSTEMS_ROOT" \
  --output /tmp/new-paired-plan --inputs inputs.json --event workflow_dispatch
```

This writes `ci-plan.json`, the full `selection-snapshot.json`, immutable per-lane runtime selections, and per-lane model snapshots. It does not download SDK payloads or execute models. Both lane snapshots derive from the same frozen inventory; do not rerun discovery between them. Use `paired-ci.py prepare-args --plan FILE --lane latest-release` to obtain the appropriate lane snapshot and runtime arguments, separated by NUL bytes for shell arrays. Pinned release diagnostics use the frozen runtime configuration instead.

Temporary exclusions live in [`config/ci-model-exclusions.json`](../config/ci-model-exclusions.json). The nine exact pipeline selections that failed [run 43326545175, attempt 2](https://github.com/rsuderman/hrx-systems-test/actions/runs/43326545175/attempts/2) are excluded only when `latest-release` resolves to `v0.1.0`. With the current 60-entry inventory, release prepares and runs 51 entries; latest-CI still runs all 60. A different publication, preset or variant does not match an exclusion. A new stable release automatically gets the full inventory. Pinned release, replay and direct local preparation do not apply this CI policy.

The planner freezes the exclusion policy and derives each lane's expected entries from it. `lane-selection.json`, workflow metadata, `lane-result.json` and the combined comparison retain excluded selections, reasons and source-run links. The combined GitHub summary shows enabled and excluded counts separately and lists the reasons; exclusions are neither passes nor execution failures. Missing execution, SDK resolution, preparation, cleanup and publication failures still fail normally. Remove an entry from the configuration to re-enable that selection on v0.1.0, or empty `entries` to re-enable all nine. Existing frozen runs retain their original policy.

`paired-ci.py initialize`, `mark-run`, `finish` and `aggregate` implement the worker attribution and comparison protocol. A plan's digests detect accidental mutation; they do not establish authenticity of externally supplied files. Run only trusted plan artifacts. Any planned full cache qualification still has the normal per-lane workflow timeout; this workflow does not promise a single-run 59/60-model budget.
