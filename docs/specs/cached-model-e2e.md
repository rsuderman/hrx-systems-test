# Direct path to cached-model end-to-end validation

## Outcome

One local command or manual GitHub dispatch tests every registered cached model
sequentially: verified host-seeded pipeline copies, read-only assets, real readiness,
one bounded query/assertion and confirmed model removal. Start one empty server
for the suite, hot-load/unload models sequentially, then stop that server once. The currently indexed cache holds
one Qwen3.5 0.8B selection. Prove that path first, then exercise the same loop for
additional seeded selections. No scheduler, automatic GPU qualifier or nightly
service is required for this checkpoint.

## Independently runnable Bash stages

Use [the three-stage contract](pipeline-stages.md): `prepare-cached-models.sh`
fetches/verifies and produces prepared inputs; `run-prepared-models.sh` starts
one server and saves raw execution results without downloads;
`generate-model-report.sh` generates all remaining artifacts from saved results.
Python stays behind these Bash entry points. The optional combined command and
GitHub invoke these exact stages, not separate implementations.

## Small suite input

The testing repository authors `config/cached-model-tests.json`, an explicit sample
suite with unique IDs, immutable selections, historical execution settings and
cache-relative seed manifest paths. Shared runtime pins live in `config/runtime.json`;
requests/assertions live in `config/model-test-profiles.json`. Cached discovery uses
published runner seed manifests as membership and hosted model cards for capacities;
test overrides never whitelist it. Seed manifests locate verified assets. Do not
infer model membership from HF blob filenames.

Additional models use their own checked-in baseline config and a distinct seed
manifest, such as `models/<id>/seed-manifest.json`, created with the existing
seeder's `--baseline` and `--seed-manifest` overrides. Assets still share the
existing native caches. Register every intended seeded selection in this list;
there is no implicit discovery of unregistered downloads. Freeze the list at run
start and include every entry in the report. A missing or mismatched manifest is
a failed cache preflight, not a silently skipped model. This avoids changing the
implemented single-model seed format or introducing a second model inventory.

## Implementation checkpoints

1. **Prepare read-only inputs.** `hst-utq.3` obtains each hosted card/lock/program
   selection at its configured Hugging Face repository and publication revision,
   verifies its model assets offline with the cache mounted read-only, and stages
   an isolated input directory. Record preparation failures per entry and continue
   preparing the others. Never repair/reseed assets or silently change a pin.
2. **One server, sequential model lifecycle.** `hst-40e` starts the pinned released
   `loom-serve` empty once, in one non-root network-disabled GPU container. Wait
   for server readiness, then for each prepared entry: register its local card via
   `POST /v1/models/register`, wait for that registration to be ready with open
   admission, perform the bounded baseline query/assertion, unregister it and wait
   for its disappearance from `/v1/status`. Only then advance. No server restart
   between models. Stop the server/container once after the loop, including failure
   paths. A one-entry suite proves the real local path first.
3. **One command and GitHub hookup.** `hst-utq.4` supplies
   optional `scripts/test/run-cached-models.sh`, which composes the prepare, execution and report commands; execution calls
   the lifecycle client once; it does not loop over subprocesses that each start a server.
   An optional `run-single-model.sh` convenience entry uses the same one-entry
   suite path. One manual workflow job invokes the same three Bash stages and uploads
   compact results/logs even on failure. `hst-utq.5` proves repeat runs with fresh
   pipeline/SDK staging and unchanged read-only assets. Scheduling and automatic
   hardware-driven selection remain later work.

All prepared trees are mounted together read-only in the serving container as
`/inputs/<entry-id>/model`; manifests live at
`/inputs/<entry-id>/prepared-inputs.json`. Preparation accepts this path prefix
rather than hard-coding the old single-model `/inputs/model` example. The same
HF/Loom cache roots remain read-only at `/model-cache/...`. Per-model deployment
references use paths visible to that server, never acquisition-container paths.
Use one pinned SDK/image for the suite; reject conflicting per-entry runtime
selections rather than silently swapping SDKs inside a live server.

Before running under the dedicated account, prove that account can read the
configured `HRX_SYSTEMS_ROOT` and manifest/asset files and can access Docker/GPU.
The existing validation cache is on this machine; its availability to that
account has not yet been established. Configure a shared root/read permissions
explicitly; privileged permission changes remain operator-owned.

## Presentation artifacts

Deliver the [verification report](results-report.md) with the initial E2E path:
a self-contained polished `index.html`, concise GitHub `summary.md` and underlying
`report.json`, with per-model evidence links. Highlight verified support by exact
selection/hardware/runtime, show all expected models including failures/not-run,
and keep repeated lifecycle cycles separate from unique-model coverage.

## Results and acceptance

Preserve each model's existing prepared-input, request/response, serving and
cleanup receipts. One suite summary lists every expected model and its passed,
failed or not-run result with stage/reason, including registration/readiness,
request/assertion and removal status. Record server identity once for the suite
and each model incarnation/served name. Keep per-model request/response/lifecycle
files plus bounded shared server logs and final shutdown evidence. Exit zero only if the suite is
nonempty and every expected model passes readiness, assertion and cleanup.
Missing assets, unavailable pipeline, runtime/load/OOM, timeout, bad response or
failed cleanup remain failures; no silent eligibility filter or substitute model.

Existing cache-seeding proof is complete and need not be repeated to author this
path. For this checkpoint, prove one real local model run first, then the full
registered suite through GitHub, including a failure case that preserves results.
A model-local failure permits continuation only after confirmed unregistration
and a healthy server. If model removal or server health cannot be established,
fail the suite, mark the remaining entries not run and stop the owned server;
no automatic restart masks the failure.

Model inputs/cache remain read-only during validation; pipeline/SDK acquisition
is online in temporary staging, and execution has no external networking or
source credentials. Record exact source, suite/model selections, release,
pipeline revision, asset identities, device and effective workload. Preserve
published settings unless a reviewed explicit baseline override is recorded.
