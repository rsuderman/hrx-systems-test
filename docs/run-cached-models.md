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

Each stage writes its own log at the attempt root. `stages.json` records stage
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
available reporting/compact artifact upload, appends the Markdown summary, and
requires every stage plus artifact publication to pass. The current 60-minute
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
