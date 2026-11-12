# Run the cached-model verification stages

The public stage commands work independently, both locally and in GitHub:

```sh
bash scripts/prepare-cached-models.sh --output "$attempt/prepared"
bash scripts/run-prepared-models.sh --prepared "$attempt/prepared" --output "$attempt/results"
bash scripts/generate-model-report.sh --results "$attempt/results" --output "$attempt/report"
```

For convenience, run all three through the same thin Bash composition:

```sh
bash scripts/run-cached-models.sh --root "$HRX_SYSTEMS_ROOT" --output "$attempt"
```

`--root` identifies the seeded cache's parent, not the results directory. It
falls back to `HRX_SYSTEMS_ROOT`, then `$HOME/.local/share/hrx-systems-test`.
`--output` must be an absolute new directory; if omitted, the command creates a
unique attempt under the root's `runs/`. When another account owns the model
cache, provide an output directory owned by the calling runner. Cache read
permission does not imply permission to create run output there.

`--suite FILE` overrides the checked-in suite. `--device-uri URI` passes the
explicit device to execution, and `--cycles N` repeats each test entry's
load/query/unload lifecycle without multiplying model coverage. These options
do not change the pinned runtime or pipeline publication. Preparation still
selects the repository-authored model/request settings.

Local commands and GitHub show concise `[hrx]` progress as work runs: preparation
batch/model counters, SDK setup, shared-server startup, model cycle phases, brief
failures and completion times. Quiet operations print a heartbeat after roughly
30 seconds; native server/compiler output, query answers, polls and dependency
lists stay in diagnostic files. Python producers flush immediately across the
host/container boundaries. Cancellation terminates the owned child process group;
log forwarding preserves the real command's failure status.

Each stage writes its own log at the attempt root. Progress transport keeps at
most 4 MiB per stage/container log, retaining its head and tail when truncated.
Adjacent `*.log.metadata.json` records observed/retained bytes, captured ranges
and whether capture completed; an interrupted capture does not claim a known
original length. Per-model and shared server evidence retain their own policies.
 `stages.json` records stage
exits and `workflow.json` carries only optional source/run/runner identity;
local runs need no GitHub credentials. A failed preparation may still produce a
partial suite: execution tests ready entries while preserving failed entries.
A missing manifest prevents execution. Reports are generated whenever raw
results exist, but successful report generation never masks an earlier failure.
The combined command exits nonzero for failed/not-run stages or uncertain cleanup.

The command delegates cleanup to the execution stage's ownership-checked hook:

```sh
bash scripts/run-prepared-models.sh --cleanup --output "$attempt/results"
```

This checks/removes only the container identity derived for those results. An
interrupted combined command forwards termination to the running stage, requests
owned execution cleanup, and attempts reporting if raw results are available.
It retains interrupted inputs for diagnostics. Normal completion removes only
this attempt's prepared model/SDK staging after confirmed execution-container
cleanup; `--keep-prepared` retains that staging for inspection or independent
execution. Model assets and saved raw results are never removed. Reports can
be regenerated later into another new directory using only `$attempt/results`.

The manual `single-model-e2e.yml` workflow now calls the three stage commands as
separate steps. It uses a runner-owned `RUNNER_TEMP` attempt path, the same
configured cache root and an explicit device. It always attempts cleanup and
available reporting and compact artifact upload. After upload, it publishes the
readable GitHub summary with a direct **Download verification report bundle**
link from the upload action. Unzip the bundle and open `report/index.html` to
browse the report and its local evidence links. GitHub summaries link to the
archive rather than treating files inside it as hosted pages. Upload failure
still leaves a readable summary and stage outcomes, with no unavailable download
link. The publisher rejects a generated summary containing relative or non-HTTPS
Markdown links, emits a diagnostic and available bundle link, and fails summary
publication rather than publishing broken links. Every stage, summary publication and artifact upload must pass. The current 60-minute
outer limit covers the initial one-model/two-cycle suite; review that budget
when the checked-in suite grows. The workflow does not seed or repair assets.

Configure the dedicated runner's `HRX_SYSTEMS_ROOT` repository variable to a
cache it can read, or provide `HRX_SYSTEMS_ROOT` in the runner environment. The
workflow preserves the inherited runner setting when its repository variable is
absent; an explicitly configured repository variable takes precedence. The default
HOME path for another account will not discover a cache under the operator's
HOME. Account ACLs, Docker/GPU group membership and any privileged setup remain
operator responsibilities. No repository variable changes or dispatch occur
when installing these scripts. Real GitHub validation requires publishing the
reviewed source and manually dispatching the workflow separately.

## Download failure evidence

The combined command also writes `failures/failures.json` and, when failures
exist, `model-failures.zip`. Unzip it and start with `README.md`: each failed
model/cycle has a readable case folder with the saved request, response when
reached, lifecycle facts and available pipeline source. Common runtime identity
and server/stage logs appear once under `shared/`. Shared log lines are context
for the run, not proof that a particular model emitted them. Bounded logs retain
head and tail, with explicit capture/truncation metadata.

Collection runs before reporting and again afterward, so preparation, startup,
interruption, cleanup and HTML-rendering failures can still produce a bundle.
Missing evidence is labeled. A clean pass produces no empty ZIP. Neither the
bundle nor the separate full-report artifact includes weights, tokenizers,
installed SDKs or environments.

To regenerate from saved attempt evidence without a cache, GPU or network:

```sh
bash scripts/package-model-failures.sh --attempt "$attempt" \
  --output "$attempt/failures" --zip "$attempt/model-failures.zip"
```

GitHub publishes the failure directory as the separate
`model-failures-RUN-ATTEMPT` artifact, retained for seven days. Downloading that
artifact already yields one ZIP; it contains no nested ZIP. The job summary
uses the actual artifact download URL after upload. Packaging/upload failure is
reported separately and never erases the original model/stage failure.

## Shared runtime and test inputs

`config/runtime.json` owns the pinned container, SDK source and adjacent dependency
lock. Pass `--runtime FILE` to the combined, preparation, single/batch seeding or
bulk download command. The default retains the current release exactly. The old
`--runtime-baseline FILE` cached-only option remains a deprecated adapter for
external composed baselines and is mutually exclusive with `--runtime`.

`config/cached-model-tests.json` owns the explicit eight-sample selections and
historical execution settings. `config/model-test-profiles.json` owns text/decision
default requests and exact-selection overrides; these do not filter cached
membership or override capacities in hosted cards. The 45-publication download
catalog and bootstrap version pins remain separate inputs.

Preparation freezes `resolved-runtime.json` once; prepared/raw results embed the
same snapshot under `runtime.resolved_runtime`. Native adapters derive SDK settings
from it and reject conflicting identity/dependency metadata. Model/profile changes
have their own hashes and do not change `runtime_identity_sha256`. A configured
snapshot is selection evidence, not installation or inference proof.
