# Nightly scheduling and model coverage

Bead: `hst-qcw`. The existing Cached-model verification workflow runs on pushes
to this testing repository's `main`, daily at **4 a.m. America/Los_Angeles**, and
manual dispatch. The schedule uses cron `0 4 * * *` with the named timezone, so
it follows local daylight-saving changes rather than a fixed UTC offset. GitHub
can delay scheduled jobs; no exact-time or missing-run monitor is promised.

## Inputs and ownership

Push and schedule events have no dispatch inputs. Initialization explicitly sets
all published cached selections, `actions-main`, two cycles, `amdgpu://0`, and
normal unchanged-asset receipts. Manual dispatch uses these same defaults and
retains explicit sample-suite, release/replay, device, cycle and full-integrity
options. Invalid manual inputs fail before preparation, including zero cycles;
missing-input defaults do not hide an explicitly invalid value.

The shared `config/runtime.json` retains the local pinned-release route. GitHub
passes an explicit runtime source; it does not rewrite that configuration. One
upstream main SDK is frozen per attempt, with verified artifact/wheel identities,
actual tested source, requested HEAD, build age and a visible behind-HEAD relation.
There is no rebuild, per-model source resolution or silent release fallback.
The prepare step alone receives the upstream Actions-read credential.

## Membership and execution

Runner-local published seed manifests define the expected inventory. Discovery
deduplicates immutable six-field selections, freezes the denominator before SDK
acquisition and retains inventory/preparation errors. Models still downloading
without a published manifest enter a future attempt; a blob or an old progress
file does not count as coverage. No sample-list or hardware-profile whitelist
filters cached membership. Hardware qualification remains advisory.

All events invoke the same independent Bash Prepare, Run and Report commands.
Model assets are read-only and verified through unchanged writer receipts;
pipelines are fetched at their seeded publication revision. One preparation SDK
and one serving server are shared across the batch. Continue after a model failure
only when unload/quiescence is confirmed; unsafe cleanup leaves later entries
explicitly not run. No automatic model download, cache repair or upstream fix is
introduced. Existing `passed`, `failed`, `not_run`, `incomplete` statuses remain.

`workflow.json` records event name, normalized inputs and UTC observation time at
runner initialization. Schedule events also record cron and timezone. This clock
is labelled as observation time, not an invented exact nominal trigger timestamp.
The same metadata travels into saved raw results and report artifacts. Harness
identity remains distinct from upstream SDK source/build identity.

## Concurrency, results and acceptance

Push, schedule and manual jobs share `hrx-single-model-hardware`, with
`cancel-in-progress: false`. The existing 60-minute timeout and seven-day compact
report/failure artifact retention remain. GitHub serialization does not lock out
local seeders, downloads or cache maintenance: operators must keep those idle
before allowing verification. This feature adds no cache lease service.

Failures, incomplete coverage, empty inventory and publication failures cannot
produce all-success. Collection runs before reporting and packaging again after
rendering; both report and nonempty failure downloads are published independently,
including on red model outcomes. Scheduled execution does not fork that path.

Focused fixtures execute the actual workflow initialization block for all three
events, default and explicit options, replay and invalid values. Existing serving,
composition, failure and artifact fixtures remain applicable. Enabling source and
observing a push trigger establish wiring; an actual scheduled firing and its
model/report cleanup remain runtime acceptance to inspect when the time arrives.
No additional local model/SDK/GPU run is required merely to check event defaults.
