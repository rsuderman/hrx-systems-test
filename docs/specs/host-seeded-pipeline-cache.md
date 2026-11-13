# Host-seeded pipeline cache and subsequent script cleanup

## Outcome and authority

Reduce repeated pipeline fetching during preparation while keeping confidence in
the exact inputs executed. The host explicitly seeds pipeline inputs, analogous
to the existing GGUF cache. CI and ordinary preparation only read that cache;
they never populate, refresh or repair it. The user also requested a subsequent
script cleanup to separate CI, host setup, pipeline rewriting and related work.

The reviewed plan is based on local main at `7e782cb` (runtime/cache source
unchanged from deployed `d053f9e`; that local commit only trims summaries). The
user has now authorized managed implementation and explicit host pipeline seeding
on this runner. Work takes place in the isolated `host-pipeline-cache` worktree.
Commit/push/main integration and GitHub dispatch require separate delivery
authorization. Model inference, sweeps, tuning and privileged host changes remain
outside scope. Beads owns current status and dependencies.

## Existing behavior and boundaries

`scripts/prepare-cached-models.py` currently downloads publication card/lock files
and pipeline source into disposable per-attempt `pipeline-cache` and `pipeline-hf`
directories. It checks the publication commit and expected card/lock hashes from
the existing model seed. `scripts/seed-models.py` provides native content-cache
routing. Program/auxiliary roles distinguish code from weight assets.

`scripts/sdk-environment.py`, `actions-runtime.py` and `release-runtime.py`
separately resolve/acquire SDK packages and dependencies. `pipeline-preparation.py`
binds runtime policy, request settings, original publication and derived source.
`paired-ci.py` and the workflows orchestrate the two SDK lanes and reporting.

The observed release preparation spent approximately 135 seconds in its online
phase, versus 2.4 seconds acquiring the SDK. These are phase measurements, not a
proof that all online time is byte transfer or a promised cache speedup.

## Proposed host seeding contract

Add an explicit host command, proposed name `scripts/seed-pipelines.sh`, accepting
the existing suite or cached model-seed inventory and configured root. Reuse the
existing selection vocabulary rather than creating a second model catalog.

- Fetch only original cards, locks, selected program files and auxiliary closure.
- Bind entries to repository, immutable publication commit, exact selection and
  expected card/lock hashes; bind source files to verified identities as described
  below. Asset-only locks need not contain upstream source-file hashes.
- Validate the whole selected closure, not only the root card. A pinned root does
  not justify trusting arbitrary unverified external sources.
- Proposed storage is `<root>/cache/pipelines`, distinct from GGUF storage.
  Reuse native content-cache primitives where their actual formats allow it.
- Publish only complete verified entries using a writer lock and atomic manifest
  replacement. Interrupted writes must preserve prior valid entries.
- Repeated explicit seeding reuses verified entries. A changed pin selects a new
  identity. Repairs require this command; readers never perform them.
- Preserve source publication pins, existing GGUF seed manifests, assets and
  verification receipts. Do not fetch weights or persist credentials.
- Document host ownership and runner read access. No automatic account, service,
  ACL or privileged host modifications are part of the seeder.

### Command and selection

The public command is `bash scripts/seed-pipelines.sh --root ROOT`, defaulting to
the existing authored suite. `--selection cached` selects all deduplicated existing
GGUF seed entries; `--suite FILE` selects an authored suite instead. The two
selection options are mutually exclusive. Reuse existing inventory/selection
validation; this command does not invent publication pins or a second catalog.
The existing configured runtime supplies a disposable seeding SDK environment;
`--runtime FILE` may choose another existing runtime configuration. Source cache
identities do not depend on that SDK's version. No GPU devices are mounted.

For example, the host operator runs:

```sh
bash scripts/seed-pipelines.sh --root /srv/loom-models --selection cached
```

Ordinary `prepare-cached-models.sh` and both CI lanes require seeded inputs by
default. There is no opt-in automatic population or fetch-on-miss mode. Existing
`seed-models.sh` remains the explicit GGUF writer and is not silently changed to
seed pipeline inputs; operators run the new command after existing GGUF seeding.

### Stored identities and completeness

Use an SDK-neutral, versioned manifest and content-addressed original bytes:

```text
cache/pipelines/
  .seed.lock
  entries/<selection-key>.json
  blobs/sha256/<digest>
```

`selection-key` is SHA-256 of canonical JSON containing the six existing selection
fields plus expected card and lock SHA-256 values. An entry records schema version,
those inputs, original/resolved document coordinates, card/lock sizes and digests,
the complete selected program/auxiliary installation paths and file identities,
and the resolved source-origin mapping needed to reconstruct a native fetch plan
offline. Record seeding SDK identity as provenance, not as a cache lookup key.
Blob paths are derived from validated digests, never accepted arbitrary paths.

The published card and lock must match the existing GGUF seed's expected hashes.
For source files covered by the upstream lock, require its size/hash/resolved
identity to match. For programs omitted by an asset-only lock, the host seeder
records observed size/hash with explicit `identity_authority=host_seed`, bound to
the pinned publication and resolved immutable source coordinate. Do not claim
these are upstream lock commitments. Relative publication source is anchored to
the publication commit; external HF sources must have an explicitly pinned
immutable revision or an upstream locked immutable revision. Reject unresolved
moving source aliases; non-HF remote source requires a published content digest.
Inline content is bound to the verified card and recorded digest. Preserve the
existing rejection of transformed pipeline inputs.

Never persist SDK objects or fetch plans as this manifest. Native cache/provider
internals differ by SDK and are not the read-only public storage contract. The
seeder may use their acquisition primitives; the reader supplies exact verified
files/origin resolutions through a small adapter with no network provider fallback.
Check actual selected SDK APIs rather than assuming the local upstream checkout
is the same version.

### Publication, repairs and reader access

Hold the writer lock through an explicit seed batch; acquire/verify each entry
in temporary storage before atomic publication. Publish immutable blobs before
their manifest. Readers open one complete manifest and capture its bytes/digest;
they never take a writable lock. Preserve already-published entries when another
entry fails, report every requested selection, and exit nonzero on any failure.
Unreferenced temporary/blobs after interruption are harmless; automatic garbage
collection and eviction are excluded.

An idempotent seed rechecks original bytes and leaves valid entries intact. If
newly downloaded bytes conflict with a valid entry for the same identity, fail
explicitly rather than silently replace it. Repair corrupted/missing data only
from the explicit host seed command; CI cannot repair it. Do not rewrite pins.

Validate path containment, duplicate installation paths, size/hash consistency,
reserved paths and conflicting source/asset roles. Reject cache symlinks and
arbitrary host paths. Native SDK-created installation symlinks must continue to
resolve only to verified staged code or existing read-only GGUF assets. Newly
created cache directories/files should permit runner reads (0755/0644) without
modifying existing account/group/ACL settings or exposing credentials. Document
unreadable parent directories as an operator action.

## Read-only preparation contract

Public preparation and CI mount the pipeline cache read-only, alongside existing
read-only weight caches. They select the exact requested seeded entry, verify its
manifest and source hashes, then stage independent disposable inputs per lane.

Recompute SDK-dependent selection/fetch/preparation state for the current attempt.
Do not cache native fetch plans with stale absolute paths or reuse a previous
SDK's installed environment. Persistent shared inputs are original source bytes;
SDK-dependent artifacts stay per-attempt unless separately designed.

Verified 0.1.0 SDKs execute original inputs. Verified 0.2.0 SDKs apply the current
versioned upgrader and conditional setup to disposable copies. Cached originals
must remain unchanged; retain original/derived identities and integrity checks.

A missing, mismatched, corrupt or incomplete entry fails the affected selection
with actionable host-seeding guidance. Retain the frozen denominator and failure
evidence. There is no pipeline network fallback and no persistent-cache write.
Record requested/cache identities, hits/misses, bytes fetched and phase timings
in saved evidence without replacing existing provenance or GGUF accounting.

### Execution handoff and network boundary

Acquire/install the SDK through the existing network-enabled host boundary.
Perform model preparation in network-disabled containers, without `HF_TOKEN`.
The current two internal preparation passes may remain initially to keep the
change bounded, but both use `--network none`; progress must describe cached input
validation rather than fresh pipeline downloading. Do not add CI jobs.

Read the original card/lock and origin map from the seed entry, verify selected
closure, and rebuild native SDK fetch state in writable per-attempt staging.
Keep the original remote document location for resolving relative source paths;
moving a card to a local path must not change what its relative origins mean.
The adapter must not instantiate a download-capable fallback on an unknown source.

Copy verified source bytes into per-attempt staging, never writable hardlinks into
the cache. Hash the small pipeline files on consumption; do not extend that work
to GGUF weights or replace the existing asset-verification receipts. Source files
referenced by installed symlinks must be staged, so prepared execution remains
valid even if the host pipeline cache becomes unavailable afterward.

Freeze the entry manifest and its digest into the prepared inputs and copy them
to raw evidence before disposable preparation is removed. Bind the cache proof
to the original source identities used by the existing upgrade seal; host and
container preflight validate the sealed prepared copies, not mutable cache paths.
Reports regenerate from saved raw results without cache access. Record honest
`pipeline_source=host_seeded` and requested publication identity; do not retain
the old human claim of freshly downloaded pipelines. Preserve legacy saved-run
rendering and avoid reintroducing per-entry rewrite/status tables in summaries.

Missing cache root, unreadable entry, seed mismatch, malformed manifest, missing
blob and hash mismatch are distinct bounded preparation reasons. Preserve all
expected entries, mark affected selections failed, and include the exact reseed
command. SDK setup and inventory errors retain their existing failure envelopes.

## Freshness

For pipelines, freshness means matching the configured immutable publication and
expected hashes. Updating a publication pin requires explicit host reseeding;
silently following a newer publication is not part of this work. Cached content
does not expire by age when the requested identity is unchanged.

Latest release and latest eligible CI SDK selection remain online checks each
run, with the existing exact-reference freezing and no stale-alias fallback.
SDK/dependency downloads remain fresh per-attempt. Persistent SDK caching,
GitHub metadata response caching and automatic refresh jobs are excluded.

## Validation and work packages

| Bead | Deliverable | Depends on |
| --- | --- | --- |
| `hst-d8f` | Host-seeded pipeline cache outcome | Children below |
| `hst-d8f.1` | Explicit seeding, verified storage and atomic manifests | None |
| `hst-d8f.2` | Read-only consumption through public preparation and both lanes | `.1` |
| `hst-d8f.3` | Focused integration evidence and operator documentation | `.2` |
| `hst-gjs` | Separate script responsibilities after cache delivery | `hst-d8f` |

Check a representative explicit cold seed and warm preparation, with native
preparation on the applicable SDK paths. Prove zero pipeline network requests
and cache writes on warm reads, original-cache immutability across upgrades,
changed-pin misses, corrupt/missing entry failures, interrupted seeding and read
permissions. Use relevant fixtures and existing integrity/orchestration checks.
Measure timings without setting an unsupported speedup target. No GPU inference,
full-inventory sweep, model-pass quota or universal memory-policy gate is required.

### Implementation order and stopping points

1. `hst-d8f.1`: implement the SDK-neutral manifest reader/writer and host seeder;
   cover atomic publication, identity matching, asset-only lock handling and
   targeted cache failure cases. Stop with a reviewed seeder/contract candidate.
2. `hst-d8f.2`: integrate the reader into public preparation and existing CI
   workers; update network mounts, evidence and freshness wording. Stop with
   reviewed source and relevant preparation/integrity/report/orchestration tests.
3. `hst-d8f.3`: run one representative host seed and warm public preparation on
   the 0.1 and 0.2 paths, reusing the same original seeded source; retain request,
   byte, timing and read-only evidence. Tests may intercept network calls or use
   network-disabled containers; SDK acquisition is accounted separately. Verify
   execution preflight/staged source links and offline reporting without starting
   the server. Finish operator documentation and an implementation review.
4. Deliver the cache change through the separately authorized commit/push route.
   Populate the deployment host cache explicitly before any new preparation
   consumer is activated; do not push a consumer requiring an unseeded host.
   Preserve the existing model denominator and scheduled/manual/public behavior.
5. Only after cache delivery, start `hst-gjs` cleanup. It is not a prerequisite or
   bundled hidden dependency of cache delivery.

The native SDK seam must be checked during `.1`/`.2`; it is a bounded engineering
check, not a reason to invent a new planning task or require model qualification.

## Subsequent script cleanup

After cache delivery, refine the caller map below against that delivered source.
Use thin existing public script entrypoints and focused implementation modules.
Suggested internal package root is `scripts/lib/hrx_systems/`; final file names
may change, but the following responsibility boundaries are the contract:

- Host administration and explicit cache seeding.
- SDK selection, acquisition and installation.
- Pipeline cache reading, preparation and integrity verification.
- Pure versioned pipeline source rewriting.
- Execution and lifecycle management.
- CI orchestration and saved-results reporting/publication.

| Current concentration | Proposed responsibility | Preserved entrypoints |
| --- | --- | --- |
| `bootstrap-runner.sh`, model/pipeline seed orchestration | `host/`: explicit operator setup and seed writers | Existing bootstrap/seed commands |
| SDK/release/Actions helpers | `runtime/`: identity resolution, acquisition and offline installation | Existing SDK CLI and selector flags |
| `prepare-cached-models.py`, cache routing | `pipelines/`: original-input reader, native preparation and immutable staging | `prepare-cached-models.sh` |
| `pipeline-preparation.py`, `runtime-policy.py` | Separate source-integrity/upgrade sealing from request/launch policy | Existing policy and verification behavior |
| `upgrade-pipelines-0.1.0-to-0.2.0.py` | Keep pure stdlib callable rewrite and standalone versioned command | Exact current standalone script/API |
| `run-prepared-models.py` | `execution/`: preflight, serving lifecycle and cleanup | `run-prepared-models.sh` |
| `paired-ci.py`, workflow shell glue | `ci/`: inputs, lane sequencing and outcome/publication orchestration | Existing workflow inputs and two lanes |
| Renderer and failure packaging | `reports/`: saved-results normalization, formatting and packaging | Existing report/package commands |

This is a separation of implementation responsibilities, not a requirement to
split every file listed or create one module per noun. Do the minimum moves and
extractions that make the boundaries real. Retain the versioned upgrader's
self-contained standard-library usage; do not make it depend on CI libraries.

Keep existing public stage commands and flags stable as thin entrypoints. Pure
rewriting must not perform network, Docker, host-service or CI operations. Keep
policy selection distinct from source transformation, while preparation composes
their contracts. Avoid a new generic framework or file moves without a concrete
responsibility improvement.

Preserve two-lane behavior, safe cleanup gating, failure accounting, source and
runtime identities, artifact/report contracts and host-only cache writes. Update
imports, Docker mounts, provenance source-hash capture, tests and documentation
when paths change. CI topology redesign and model/runtime remediation are separate
scope; existing model failures do not block this cleanup.

For cleanup, first extract pure storage/integrity/policy functions, then move
host/native orchestration behind thin CLI entrypoints, and finally reduce CI
shell glue where existing helpers own the same behavior. Keep each checkpoint
reviewable and use existing fixture/stage composition checks. Centralize trusted
module loading needed by `python -I`, account for container import paths and
update source-hash capture for every executed helper; simple file moves must not
silently drop provenance. Do not bundle private-repository migration, Actions
policy changes, service changes or a one-job CI redesign into this refactor.
