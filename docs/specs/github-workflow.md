# Manual real-model GitHub hookup — hst-utq.4

Implement the GitHub entry point for [the basic epic](basic-e2e.md). Existing
`single-model-e2e.yml` is a fixture/prerequisite skeleton; its green fixture does
not satisfy this task. Keep bootstrap/pickup checks intact. No workflow code is
changed by this specification.

## Inputs and command boundary

The checked-out `config/single-model-e2e.json` supplies the pinned model/preset,
known HRX release and dependency digests, base-image digest and bounded
request/assertion, as defined by [the shared contract](basic-e2e.md). Use the
same pins on repeated runs; no runtime-version matrix or latest-build lookup.
`HRX_SYSTEMS_ROOT` is an optional host-specific Actions variable, defaulting to
`$HOME/.local/share/hrx-systems-test`. GPU selection is the explicit
operator-selected compatible host/device configuration; no qualification file
or automatic hardware planner gates this path.

The public host wrapper to implement calls private helpers from .1/.3/40e:

```sh
bash scripts/run-single-model.sh --baseline config/single-model-e2e.json \
  --root "$root" --output "$root/runs/<run-id>-<attempt>"
```

Task .4 owns this host wrapper and its aggregate result. This is a new wrapper
contract, not an existing SDK command. It reads the default verified seed index
`$root/cache/seed-manifest.json`; `--seed-manifest FILE` selects an isolated index
for validation. Child helper spellings stay stage-private. The wrapper runs
.1's SDK environment, .3's fresh pipeline/read-only input preparation, then
40e's actual readiness/query/assertion/cleanup client, propagating any failure.
It never invokes manual seeding. A missing asset reports a cache miss for the
operator; neither a fixture nor offline input verification may replace inference.

## GitHub and container ownership

- Manual `workflow_dispatch` only; checked-in pins need no free-form dispatch
  override. Run on `[self-hosted, linux, hrx-systems-test-bootstrap]`, record
  actual runner and checkout commit. Use SHA-pinned Actions, checkout with
  `persist-credentials: false`, and `contents: read` permissions.
- One shared hardware concurrency group, `hrx-single-model-hardware`, independent
  of ref or workflow name; `cancel-in-progress: false`. Start with the existing
  15-minute job deadline; stage budgets leave bounded cleanup/upload time.
  This serializes matching repository jobs, not unrelated host activity.
- Host steps are trusted orchestration. They launch the pinned base container
  and exact release environment; actual pipeline/runtime work stays inside the
  workload boundary. No sudo, privileged container or Docker socket in workloads.
- Model asset caches and staged pipeline are read-only to execution; state,
  temporary compilation output and results are separate writable mounts. SDK
  payload acquisition writes only per-attempt staging, never the model cache. Each container installs its own SDK from those verified payloads into
  writable `/run/venv`; never mount or relocate another container's installed venv.
  Retain distinct per-stage `sdk.json` receipts and compare immutable identities.
  Acquisition-only credentials/network access do not reach inference.
- Task .4 supplies only the operator-selected backend's required device nodes
  and explicit device URI to the named non-root serving container. Set its UID/GID
  and the host devices' required numeric supplementary groups (for example render/
  video on AMD); do not assume image group names map to host GIDs. Check access
  without granting privileged mode or silently selecting another device.
- On failure/cancellation, invoke the wrapper's bounded owned-container cleanup
  before collection (`always()`); the outer runner does not kill other services.
  Wrapper signal handling is required too; a forcibly lost host cannot guarantee
  cleanup or uploads, so retain/report incomplete attempts rather than pass them.

## Results and acceptance

Initialize per-attempt output before checkout/model work and write a small
`workflow.json` identifying run ID/attempt/ref and runner. The wrapper owns the
real `report.json`/`plan.json`/`summary.md`; the serving client supplies
`serve-result.json`, `request.json`, `response.json` and bounded `server.log`.
The explicit artifact allowlist also includes `seed-manifest.json`, available
`seed-report.json` evidence, `prepared-inputs.json`, and each stage's `sdk.json`
receipt under fixed stage-specific result subdirectories. Copy/link only these
small receipts into attempt results; never export their referenced payload trees.
Serving receives read-only `/model-cache/huggingface`, `/model-cache/loom-assets`,
`/inputs/model/model.json` and `/inputs/prepared-inputs.json`; it writes stage
evidence to `/run/results`. Input preparation may assemble its new model tree
in run storage while model caches remain read-only. Append the summary to
`GITHUB_STEP_SUMMARY`, then upload only an explicit small-file allowlist
with `always()`. Never recursively upload model/install/cache payloads. Retain
artifacts seven days initially. Missing output and upload failure cannot turn an
unsuccessful test green; preserve local evidence when publication fails.

After authorized publication on the default branch, a real manual dispatch must
show the expected prepared model, a passing bounded response assertion, successful
owned cleanup and artifact upload on the dedicated-account runner. Exercise
cache-miss and assertion-failure cases: reports survive and jobs fail. A second
real attempt reuses seeded assets while staging the same pinned pipeline/release
in a new run directory. hst-utq.5 validates the actual GitHub evidence; publishing
and dispatch require applicable integration authority, not spec completion.

References: existing [workflow skeleton](../../.github/workflows/single-model-e2e.yml),
[runner bootstrap](../../.github/workflows/runner-bootstrap.yml), GitHub
[manual dispatch](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow),
[runner labels](https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/choose-the-runner-for-a-job)
and [concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).
