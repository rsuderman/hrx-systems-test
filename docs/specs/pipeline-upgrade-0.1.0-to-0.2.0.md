# Paired latest-release/latest-CI verification and HRX pipeline upgrades

## Outcome and scope

Add the locally validated pipeline upgrader to hrx-systems-test and automatically
apply it to freshly pulled pipelines when the resolved SDK requires the 0.2.0
source contract. Normal CI must perform two separately reported executions: the
latest official stable release SDK and the latest available successful upstream
main CI SDK. A resolved HRX 0.1.0 release must execute the original pulled pipelines
without these upgrades. Local public commands and GitHub CI must use the same
policy and preserve enough evidence to identify what was executed.

This is the implementation specification for epic `hst-7x3`. The user has
authorized managed implementation in an isolated worktree. Commits, pushes and
actual CI dispatch require separate delivery authorization. The backlog and
acceptance contracts are recorded below; current progress belongs in Beads.

The user clarified the delivery scope on November 12: enable the two CI lanes,
the versioned upgrader and conditional setup. Making models pass, selecting a
universal memory policy, further tuning and repeated inventory sweeps are not
delivery requirements. Preserve collected evidence and track model failures
separately. This clarification supersedes the broader qualification checkpoints
from the earlier planning phase.

In scope:

- A version-named standalone source upgrade command, regression fixtures and
  documentation for every supported repair.
- Two CI lanes with independently frozen latest-release and latest-CI SDK identities.
- Resolved-runtime policy selection, preparation integration, derivative integrity,
  offline execution verification and self-contained CI evidence.
- Explicit runtime-qualified request, shared-server memory/backing and container
  syscall settings, reusing existing follow-up issues rather than duplicating them.
- Wiring checks and honest model/failure reporting, using the native evidence
  already collected without requiring further model qualification.

Outside scope: publishing or editing upstream model generators/publications,
refreshing seed pins, downloading/requantizing weights, changing kernel/GTT/driver
settings, rebooting, repairing HRX itself, or promising that all cached models pass.
The GTT installer is a host administration tool and is not this pipeline upgrader.
Existing publication rollout hst-50t and Ornith/GTT work hst-d67/hst-bzv remain
separate.

## Established baseline

Current production boundaries are `prepare-cached-models.sh`,
`run-prepared-models.sh` and `generate-model-report.sh`, optionally composed by
`run-cached-models.sh`. Preparation downloads and verifies fresh pinned hosted
cards/locks/programs, then installs assets from read-only shared caches. It seals
per-model manifests and a prepared suite. Execution verifies those hashes and
uses offline, read-only inputs with one shared server. Reporting consumes saved
results only.

Runtime selection currently supports `release`, `actions-main` and `actions-replay`.
The existing `release` selector means a configured pin (currently 0.1.0), not
latest-release discovery. The current workflow has one verification job defaulting
to `actions-main`; the two-lane behavior is new work. Resolved Actions provenance
already records the actual commit/artifact/wheel and lag behind requested HEAD.
An upstream metadata check during planning confirmed v0.1.0 as the current stable
release, with a canonical Linux Release wheel and upstream SHA256 asset digest;
implementation must discover releases dynamically rather than hardcoding that fact.

The local source prototype is:
`/home/rsuderman/codex/project-workspaces/hrx-systems-test/tools/model-compatibility-fixup.py`,
SHA256 `ecb0e1d3f7a960d158bf3615dae90017c2e2a51a6e907a04db58ef32a0eb030b`.
Its 24 regression tests and small fixtures live beside it. Per-fix technical
records are in that workspace's `plans/model-compatibility-fixes/`.
The latest local matrix is `gates/remaining-six/report/matrix.json` in that
workspace, against HRX commit `338c5ad9f09d4337185e3488acda0a8087a6a29e`.

That matrix qualifies 59/60 selections across several server policies and three
explicit reasoning profiles. It is not a proof of 59 passes in one CI server.
Ornith remains a reported allocation/backing failure. The SDK was behind upstream
HEAD; package version alone does not establish equivalence to another build.

## Upgrade command and source semantics

Proposed repository command:

```sh
python3 tools/migrations/upgrade-pipelines-0.1.0-to-0.2.0.py SOURCE --check
python3 tools/migrations/upgrade-pipelines-0.1.0-to-0.2.0.py SOURCE --output NEW_DIRECTORY
```

Migration identifier: `hrx-pipelines-0.1.0-to-0.2.0`, with a separately recorded
implementation/report revision and script SHA256. The name describes the source
contract migration; pipelines need not contain an explicit producer-version field.
Already-compatible recognized source must produce an unchanged/no-op result.

Port the validated repairs: public preset pool-capacity removal; typed vector
literal splats; command operation renames; guarded text-service ABI closure;
weight-key byte ranges; verified convolution token guards; finite decision batch
ABI and checked buffer accesses; LFM Audio opening-reasoning detection; and older
MiniCPM explicit reasoning-selection support. Preserve exact-family guards and
failure behavior. Unknown source is a model-local upgrade failure, not permission
to remove bounds, fabricate exports or accept arbitrary output.

The tool uses the standard library, needs no network/GPU/SDK, preserves original
input and asset references, refuses overlapping/existing output and regular weight
payload copying, and checks idempotence. It emits machine-readable file hashes,
operation counts and bounded diffs. Published locks remain original provenance;
they are not evidence that transformed programs match the original publication.
The derivative remains explicitly not publication-ready.

Repository documentation belongs under `docs/pipeline-upgrades/0.1.0-to-0.2.0/`,
with one entry per repair and a catalog. Port small source fixtures and essential
reproduction/validation facts; do not commit cached assets, SDKs or large run logs.

## Two CI lanes and runtime selection

Push, scheduled and default manual CI run both lanes:

| Lane | SDK selection | Source/profile policy |
| --- | --- | --- |
| `latest-release` | Latest official stable upstream release | Based on the resolved SDK version; 0.1.0 stays unchanged |
| `latest-ci` | Latest available successful eligible upstream main SDK | Based on the resolved SDK version; 0.2.0 uses the migration |

Each lane has its own prepared bundle, server lifetime, results, status, timing,
SDK installation, artifact names and attempt/container identifiers. Freeze the
same repository revision, ordered model selections/seed identities and requested
cycles once for both lanes so the comparison is meaningful. A seed inventory change
between workers must not silently change the second lane's selected set. SDK-specific
preparation failures remain entries in that common inventory. Each lane still
pulls/verifies its pipeline inputs freshly; cached model assets remain shared and read-only. Runtime-qualified
requests/settings may intentionally differ and must be visible.

Resolve both runtime selection references once per workflow attempt. A release or
artifact published during the run cannot change a lane's selection halfway through.
Each lane acquires its frozen payload once and completes wheel hash/version
qualification before model execution; metadata selection does not require a
duplicate SDK download. A resolver/acquisition failure is recorded for that lane and must not suppress the
other lane. SDK/dependency integrity and compatibility failures remain failures.

Add `--runtime-source release-latest` for the public local/composite/preparation
commands. Preserve existing pinned `release` and exact `actions-replay` semantics.
Local commands without an explicit selector keep their current pinned default.
At the workflow level, add a `both` selection that expands into the two lanes and
make it the default; retain explicit single-lane/latest, pinned-release and Actions
replay dispatch modes for diagnosis. `both` is orchestration, not a request to
install two runtimes into one prepared suite/server.

### Latest official release resolution

Select the upstream's latest official stable release from trusted GitHub release
metadata, excluding drafts and prereleases. Select exactly one compatible canonical
Linux Release `hrx-loom` SDK wheel, excluding debug/sanitizer/tracing variants.
Freeze release ID, tag, published time, exact asset ID/size/digest, SDK version and
resolved tag commit. Resolve annotated/lightweight tags to their actual commit;
`target_commitish=main` must not be mistaken for the release source commit.

Verify the upstream asset SHA256 and wheel metadata/tags/dependencies in the pinned
container, then install the same staged payload offline. Missing/ambiguous assets,
missing trusted digest, incompatible dependency lock or failed verification must
produce a release-lane failure, not fallback to an older release or to CI. Do not
update the local configured pin or silently loosen dependency locks. Record exact
identities sufficient for replay of the resolved release while its asset exists.

### Latest CI resolution

Reuse the eligible successful main-branch Actions selector: capture requested HEAD,
prefer its valid SDK artifact and otherwise choose the latest available successful
eligible ancestor according to the existing selection contract. Record actual
commit, build age, relation to HEAD, run/attempt/artifact and wheel digest. Clearly
label an older available build as behind HEAD. Failed/in-progress builds are not
usable SDKs; expiration/unavailability and integrity/compatibility failures retain
the existing explicit failure behavior. There is no release fallback.

### Version-conditional upgrading

Expose `--pipeline-upgrade auto|none` on preparation and the composite command;
`auto` is the default in both lanes. No separate workflow upgrade toggle is needed
for normal operation.

| Resolved runtime | `auto` behavior |
| --- | --- |
| SDK 0.1.0, latest/pinned release or Actions | Skip upgrade; record origin and `target-0.1.0` reason |
| SDK 0.2.0 family, stable release or Actions, including development builds | Select the versioned migration and matching settings profile |
| Other runtime family/origin without a defined policy | Explicit unsupported-policy preparation failure |

Use actual verified origin/version and frozen SDK identity, not lane name, event
name, requested HEAD or an unverified workflow input. A future stable 0.2.0 release
therefore receives the appropriate migration/profile; the release lane must not
permanently assume 0.1.0. Later unsupported versions fail visibly until a policy
is deliberately added. The version rule is not an ABI guarantee for every build;
ordinary compilation/inference qualification still applies.

`none` is an explicit raw-pipeline diagnostic mode. It records the bypass and does
not disable SDK/asset/source integrity verification or silently change profiles.
There is no forced-upgrade mode for 0.1.0 in this scope.

## Preparation and integrity contract

Insert upgrading after original acquisition/install verification and before final
executable hashes/readiness are sealed. Do not mutate an already-sealed prepared
bundle or perform migration at execution time.

Retain the verified original source identity and the exact executable derivative
as distinct evidence. Preserve publication revision, original card/lock/program
hashes and unchanged asset receipts. Record final card/program hashes, selected
migration/implementation/hash, target runtime, per-file before/after hashes and
report reference. Do not reinterpret transformed source as a published receipt.
Native asset/source acquisition must finish before modifying executable sources;
there is no fetch of transformed programs against an old publication lock.

The existing manifest distinguishes original card hashes from the installed card,
but its program hashes and `effective_execution` currently describe one tree.
Extend this contract explicitly. Keep original published execution separate from
effective upgraded execution (in particular, the removed `pool_capacity` must not
remain represented as an active setting). Freeze effective request profiles before
baseline/manifest digests are finalized. Update comparisons that currently equate
published and effective settings; do not bypass them or rewrite original evidence.

Use additive versioned records within the existing bundle schema where feasible:

- Suite: requested upgrade mode, resolved policy and runtime-qualified execution
  policy, including configuration identities/hashes.
- Entry: `applied`, `unchanged`, `skipped` or `failed`, reason, migration ID,
  original/derivative identities and compact upgrade-report reference/digest.
- Ready entry: executable hashes match the tree the server will actually mount.

Both host and container preflight validate the derivative and referenced receipts.
Changes to the card, program, migration report, selected policy or frozen effective
request must be detected. Asset receipts retain the existing no-rehash fast path.
Legacy bundles without new metadata keep their existing verification behavior and
are identified as legacy/unrecorded; execution must never infer or apply an upgrade.

A failed upgrade leaves the entry failed, preserves original acquisition evidence,
and does not expose a partial derivative as ready. Other entries may prepare; all
expected selections remain in the result, following existing partial-suite rules.

## Runtime settings and shared-server boundary

These settings are separate from source transformation and must be recorded as
such. Apply new defaults/overrides only to the resolved 0.2.0 runtime policy,
whether a stable release or Actions artifact; resolved 0.1.0 keeps existing
requests and launch/container behavior.

- **Request profiles (hst-wp0):** add exact six-field selection overrides for the
  Qwen3 14B and two MiniCPM publications recorded in the local matrix. Their only
  new request field is `reasoning_effort: none`; retain prompt, token limit,
  temperature, streaming setting and exact final-answer assertion. Do not key
  production profiles by machine-local cached IDs or model name alone. The older
  MiniCPM source upgrade is required for that field to work.
- **NUMA policy (hst-bhi):** pin/review the captured Docker-default-derived profile
  with only the validated `mbind` allowance. Freeze its identity and pass it to
  execution for the applicable runtime. Retain nonroot, cap-drop, read-only mounts,
  no-new-privileges and offline execution; no daemon policy changes or unconfined
  containers.
- **Memory/backing (hst-x99):** provide one explicit, frozen shared-server policy
  per attempt, supporting elastic byte allowance or fixed backing. Validate their
  mutual exclusion and preserve source bounds. Make operator configuration usable
  through public commands/CI without introducing machine budgets into portable
  model cards. Document the fixed-backing default and explicit elastic option;
  neither is a promise that every cached model fits. A universal policy is not
  a delivery checkpoint.

The exact request-profile identities are:

| Repository | Model ID | Publication revision |
| --- | --- | --- |
| `loom-fun/ggml-org-Qwen3-14B-loom` | `Qwen/Qwen3-14B` | `4b7f80710c54cb1b282e0b1c6cdb43b74a00a238` |
| `loom-fun/openbmb-MiniCPM-V-4.7-35B-A3B-loom` | `minicpm-v-4.7-35b-a3b` | `d2183a71a44a0defe94f53823c674b7be343843d` |
| `loom-fun/bartowski-MiniCPM-V-4.7-35B-A3B-loom` | `openbmb/MiniCPM-V-4.7-35B-A3B` | `03376cecc872b32ebe95c90b7044fe362768502a` |

All three additionally require `preset=chat`, `variant=gguf`, `programs=source`.
Extend the existing model-config/profile resolution with an explicit runtime
scope, preserving release-only behavior and freezing the selected effective
configuration. Do not introduce a general plugin/migration framework for this
single versioned upgrade.

The existing lifecycle remains one shared server per attempt. No per-model server
restart, silent policy retry, policy grouping or model exclusion is authorized by
this spec. Further lifecycle changes and memory tuning belong to separate work.
The mixed-policy 59-pass matrix cannot choose a universal budget by itself.
Resource failures are valid CI results
and must remain visible; source upgrading alone does not guarantee a green suite.

## CI and reporting

Use two separately named worker executions with GPU work serialized on the
existing hardware concurrency boundary. Prefer reusable shared worker steps/jobs
so the two lanes do not duplicate the model harness. Each worker has a complete
prepare/run/report/cleanup lifecycle and its own timeout and output namespace.

Ordinary model, upgrade, acquisition or assertion failure in one lane must not
cancel the other. Explicitly disable fail-fast behavior where applicable and
allow the next worker after confirmed safe cleanup, even if the previous lane's
tests failed. If cleanup/server/device safety is unknown, do not launch another
GPU server: report the second lane blocked/not-run with the exact reason and fail
the overall check. Never imply that a missing second result is a pass.

Publish per-lane reports/artifacts and a combined summary identifying both exact
SDKs, selection timestamps, upgrade decisions and passed/failed/not-run counts.
Overall CI succeeds only when both requested lanes and their required cleanup,
reporting and uploads succeed. The aggregation path must run after lane failure,
retain available evidence, and identify resolution failures or blocked execution.

Keep GitHub as composition of the existing public stages. Automatically resolve
and freeze the upgrade during preparation; execution consumes that exact bundle.
Expose migration policy and per-model applied/skipped/failed status alongside
actual SDK origin/commit and qualified request/launch settings.

Copy bounded upgrade reports/provenance into raw results, failure evidence and the
report artifact allowlist before prepared staging is removed. Include script and
configuration identities in source provenance. Reports must regenerate offline
from saved results without the original workspace, prepared tree, cache or server.
Do not upload weights, SDKs, complete source trees or credentials.

Preserve current failure propagation and safe continuation after confirmed model
removal, pool reclamation and server health. Successful report generation cannot
hide a migration, model or cleanup failure. Source/profile upgrades must not turn
Ornith's unqualified capacity case into an implicit pass, exclusion or assertion
relaxation.

## Work packages and dependencies

| Bead | Deliverable | Blocked by |
| --- | --- | --- |
| `hst-7x3.1` | Versioned upgrade command, fixtures and per-fix docs | None |
| `hst-7x3.5` | Latest stable release resolver and frozen paired runtime selection | None |
| `hst-7x3.2` | Runtime policy, preparation integration and derivative integrity | `hst-7x3.1`, `hst-7x3.5` |
| `hst-wp0` | Runtime-scoped exact-selection reasoning profiles | `hst-7x3.2` |
| `hst-bhi` | Frozen, narrowly scoped NUMA container policy | `hst-7x3.2` |
| `hst-x99` | Frozen shared-server memory/backing settings and documented limits | `hst-7x3.2` |
| `hst-7x3.3` | Paired latest-release/latest-CI execution, evidence and reporting | `hst-7x3.2`, `hst-wp0`, `hst-bhi`, `hst-x99` |
| `hst-7x3.4` | Composed wiring validation and delivery evidence | All preceding work |

Unresolved model/resource failures are recorded separately and do not block
acceptance of the lane, upgrade or setup wiring.

Reuse existing runtime issues under the epic; keep upstream publication and host
GTT changes related but outside its acceptance. Beads owns phase, assignment,
review disposition and validation evidence for each work package.

## Acceptance and validation

- Ported regression tests preserve all established repairs, exact-family rejection,
  input/asset immutability and repeat-application behavior. Include the native
  parser/prompt regressions at the correct test seam where the selected SDK is
  available; static source checks alone do not prove their behavior.
- Release-resolution tests cover stable/latest selection, drafts/prereleases,
  canonical versus instrumented wheels, ambiguous/missing assets, tag-commit
  resolution, digest/metadata/dependency failures and once-per-attempt freezing.
- Policy tests cover resolved 0.1.0 bypass, 0.2.0 stable-release and Actions/replay
  application, actual-versus-requested identity, future-version failure,
  already-compatible no-op, explicit raw mode and per-model upgrade failure.
- Paired orchestration tests prove two lanes on normal triggers, preserved single
  diagnostic modes, distinct output/artifact/container identities, serialized GPU
  access, continuation after ordinary first-lane failure, blocked second execution
  after unknown cleanup, independent resolution failures and combined failure status.
- Preparation/execution tests prove original provenance survives and altered
  executable/report/policy/request hashes are rejected. Partial-suite coverage,
  immutable asset references and no extra weight hashing remain intact.
- Resolved 0.1.0 release regression proves original source/card bytes, requests and effective
  server/container settings are unchanged apart from added skip/provenance metadata.
  Existing known model failures are not reclassified to meet acceptance.
- Fresh-build integration proves upgrade and applicable settings occur through
  ordinary public commands, not the external diagnostic runner. Reuse the
  successful small-Qwen native evidence on both paths and the existing full-run
  evidence; no further model sweeps are required.
- Verify paired lane selection, frozen inputs, independent artifacts and combined
  reporting with relevant wiring tests and the existing local paired run. Preserve
  observed failures honestly. An actual GitHub execution remains a delivery step
  after authorization, not a new model-pass qualification gate.
- Document the launch policy and known limitations. Do not make model remediation
  or universal memory-policy selection a dependency of this implementation.

## Delivery and qualification boundary

Managed implementation must produce a reviewed candidate in an isolated worktree
with relevant lane, upgrade and conditional-setup checks. Commit/push and actual
GitHub execution remain explicit delivery steps requiring authorization; report
their status without claiming local checks are GitHub evidence. No further model
qualification, tuning or universal memory-policy decision is required. Known
model/resource failures and host/GTT work remain separate.
