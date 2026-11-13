# hst-utq.4 — compose the Bash stages locally and in GitHub

## Outcome and ownership

Provide an optional `scripts/test/run-cached-models.sh` convenience wrapper and a real
manual GitHub workflow that call the same independent Bash stages. The three
stage owners supply preparation (.3), execution (40e) and reports (.6); this task
owns composition, CI metadata, failure propagation and artifact collection only.
It does not reimplement acquisition, server lifecycle or rendering.

## Inputs and flow

Follow [the public commands/handoff schema](pipeline-stages.md). Resolve the
existing external-root convention, create one new attempt directory and invoke:

1. `prepare-cached-models.sh --output "$attempt/prepared"`.
2. `run-prepared-models.sh --prepared "$attempt/prepared" --output "$attempt/results"`.
3. `generate-model-report.sh --results "$attempt/results" --output "$attempt/report"`.

Expose suite/root/output options on the optional composition command and pass
stage inputs explicitly. Preserve all stage exits. Execute a usable partial
prepared suite even if some entries failed preparation; execution records those
failures. If no usable prepared manifest exists, start no server and preserve
preparation diagnostics. Attempt reporting when raw results exist and always
collect available compact report/raw evidence. A successful renderer must never
mask earlier failed preparation/execution. No source/config mutation between
stages changes the frozen attempt; runtime downloads nothing.

## GitHub boundary

- Manual `workflow_dispatch` only; existing dedicated runner labels, SHA-pinned
  Actions, checkout `persist-credentials: false`, and `contents: read` permissions.
- Separate Prepare, Run and Generate report steps call the public Bash commands.
  Use explicit paths/step outcomes so failure does not suppress possible report
  generation/upload. Record run/ref/source/runner in compact `workflow.json` and
  carry that metadata through evidence; local runs need no GitHub identity.
- Shared concurrency group `hrx-single-model-hardware` and no cancel-in-progress.
  Choose a bounded outer deadline covering acquisition, all recorded per-model
  deadlines, owned cleanup and upload; do not assume15 minutes fits any suite size.
- Prove runner-account cache read access and Docker/GPU permissions before real
  dispatch. Report permission failures; no sudo, silent device change or privilege
  escalation. Execution stage owns named-container cleanup; the always-run CI
  cleanup hook delegates to that ownership without killing unrelated processes.
- Save compact stage outcomes before upload. Upload an explicit allowlist
  for the generated report bundle and compact partial raw/diagnostic evidence
  with `always()`, initially retained seven days. Never recursively upload
  prepared input/weights/cache/SDK trees. HTML is downloadable, not assumed to
  render inline in Actions. After upload, append rendered human-readable
  `summary.md` plus the actual `steps.upload.outputs.artifact-url` to the job
  summary. Label download/open `report/index.html`; never link `index.html`,
  `report.json` or `suite-result.json` as relative GitHub URLs. If upload fails,
  retain a readable failure summary without a fictitious download link.
  Upload/report failure cannot turn a run green.

Clean per-attempt prepared SDK/model staging after execution/needed diagnostics;
never remove the seeded model cache or raw results needed for report regeneration.
Execution can also be run independently without this composition command.

## Acceptance and stopping point

Local stage-composition fixtures verify success, preparation failure with partial
inputs, absent manifest, failing execution with successful reporting, rendering
failure and artifact-collection failure. Same public commands/flags are used by
local and GitHub paths. Prove expected metadata/output allowlist and bounded
cancellation cleanup using stage ownership. A real authorized manual dispatch
must run the actual model path and upload usable artifacts; the prior green
synthetic skeleton is not enough. Final repeated-run validation belongs to .5.
Publication/main integration/dispatch need applicable delivery authorization;
this planning task does not perform them.
