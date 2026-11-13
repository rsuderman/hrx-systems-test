# hst-utq.5 — validate the staged real-model suite and artifacts

## Outcome and scope

Establish real support evidence for every registered cached selection through
local preparation/execution/report commands and actual manual GitHub runs.
Depends on completed seeding (.2), stage implementations (.3/40e/.6) and GitHub
composition (.4). Fixtures do not replace real model readiness/query/unload proof.
Follow [shared contract](basic-e2e.md) and [stage schema](pipeline-stages.md).

## Procedure

1. Reuse the completed cold/warm seeding receipts rather than downloading the
   model again. Verify the dedicated runner can read the configured manifest,
   native cache files and pipeline inputs and access the selected GPU/Docker.
   Operator owns any privileged permission repair; report the exact blocker.
2. Prepare independent host-seeded pinned pipeline copies and fresh SDK inputs and run a real local one-entry suite
   offline with cache/input mounts read-only. Require real registration/readiness,
   bounded recorded request/assertion, confirmed unregistration and final shutdown.
   Demonstrate two load/query/unload cycles in the same server process; repeat
   the existing model for that proof if necessary without claiming two models.
3. Generate all final artifacts from those saved raw results with no Docker,
   GPU, model cache or network access. Re-render into a fresh directory; model
   execution does not recur and recorded execution facts/counts remain unchanged.
   Inspect wide/narrow HTML, relative evidence links and readable GitHub Markdown.
4. After separately authorized publication, dispatch the real suite on GitHub.
   Record exact source/runtime/pipeline/asset/hardware/workload and every expected
   entry. Verify input/model-cache mounts are read-only and serving has no external
   network/source credentials. Require actual response assertion, removal and
   final cleanup for every model plus successful compact artifact upload.
5. Repeat GitHub dispatch with distinct attempt and independent pipeline copies and fresh SDK staging
   but the same cached assets and pinned selections. Require equivalent real
   coverage and cleanup. New paths alone are not proof of fresh program acquisition;
   preserve acquisition provenance. Hash reads are not network transfer claims.

## Failure and presentation checks

Use controlled fixtures for missing/corrupt assets, asynchronous registration,
wrong/malformed responses, timeouts, model-local failure followed by successful
next entry after cleanup, and removal/server failure stopping the loop. Retain
failed/not-run/incomplete entries and stage reasons; do not mutate shared weights.
Exercise a representative failed composed run with an accurate generated report
and failing overall status. A valid rendering of failure may exit0 for reporting
without changing the original failure. Rendering/upload failures remain visible.

Check HTML/Markdown/JSON agree on denominator, unique model selections, repeated
cycles and every phase/result. A verified selection needs real assertion and
completed removal; overall success also needs final server/container shutdown.
Text smoke results do not imply vision coverage. Fixture runs must be clearly
labelled. Confirm the downloaded HTML bundle works offline and includes all
referenced bounded evidence without model/pipeline/SDK payloads or credentials.

## Closure evidence

Retain a compact summary referencing completed seeding receipts, exact source
identity, local stage commands/exits, real response/lifecycle evidence, offline
report regeneration checks, controlled failure results and both GitHub run URLs
and artifacts. Confirm owned processes/containers have exited and retain raw
results for future re-rendering. State any missing real evidence explicitly;
assumed compatibility, parsing/help checks or a fixture pass cannot close this task.
No runtime-version matrix, automatic qualification or nightly schedule is needed.
