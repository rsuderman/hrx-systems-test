# First spike: one model end to end with reusable assets

Pipeline reuse policy update: ordinary preparation now consumes exact original
inputs from the [explicit host pipeline cache](seed-pipelines.md). Historical
fresh-download experiments below describe the earlier spike; they are not the
current CI acquisition contract. SDK packages remain per-attempt downloads.


Status: **scoped for implementation** under Beads epic `hst-utq`. Deliver one
real model test through GitHub Actions, including manual seeding, pinned pipeline
and HRX release acquisition, model readiness, a bounded inference assertion and cleanup.
The existing synthetic fixture does not satisfy this outcome.

## Outcome

Assume the operator selects a model/preset and host that are compatible, and
that `loom-fetch` supplies the required matching asset closure. Automatic
hardware selection, compatibility-matrix construction and memory qualification
are not prerequisites for this epic. Record the actual host/workload/runtime;
if acquisition or execution fails, report that failure instead of substituting
another model or host. Do not invent evidence of qualification.

A person seeds model assets with a simple script. Repeating it reuses unchanged
weights, tokenizer and companions. A manual GitHub run downloads the pinned
pipeline into fresh staging and installs the pinned Loom/HRX release, consumes cached assets read-only, waits for
the model to be prepared, runs one bounded known-answer query and stops its
owned server/container. Repeat to prove asset reuse between runs.

A download-only check may help debug acquisition, but it cannot complete this
end-to-end epic. Automatic model-cache population by CI remains deferred.

The first candidate is the pinned Qwen3.5 0.8B card/preset described in the
[single-model scope](single-model-e2e.md), subject to the operator's compatible-host
selection. Acquire its full declared closure, including tokenizer and selected
vision assets. Record effective execution settings; any reviewed override must
be explicit, not an invisible change to the published preset.

## Hardware-specific model list and seeding

For later hardware-aware seed planning and automatic selection, generate a model list
using the [four selection checks](platform-restrictions.md): pipeline support,
available memory, storage and runtime compatibility. The first epic assumes
a compatible operator-selected model/host; this planner is not its gate.

The planner reads the assigned GPU and its total/free memory, checks the selected
pipeline/preset exists with a compatible GPU target, and compares model memory
recommendations with dedicated VRAM or the supported shared-memory budget.
It estimates missing download bytes from the card/lock and existing cache.

Target matching uses the selected HRX version's compatibility rules. For example,
`gfx1100` can match a pipeline implementation for `gfx1100` or `gfx11-generic`,
but not an implementation available only for another exact GPU. Keep both the
detected GPU target and the matched pipeline target in the plan.

Each normal entry records the model/pipeline revision and preset, **seed**,
**skip**, or **needs information**, and a short reason. The manually invoked
seeding script consumes eligible entries or an explicit download-only selection
and uses `loom-fetch` through the acquisition adapter to acquire only their
required assets. CI does not invoke this writable seeding path initially.
Both the planner and seeding script remain to be implemented.

Use total capacity to decide whether a model is worth caching and live free
memory to decide whether it can run now. A temporarily busy GPU does not prevent
seeding. Unknown memory requirements need clarification, not a guess from GGUF
size. This machine's shared-memory GPU needs an appropriate recommendation before
we can automatically select models for it.

## Inputs and ownership

| Input | Source of identity | Reuse policy |
|---|---|---|
| Pipeline card, lock, programs and auxiliary program files | Frozen pipeline repository/publication/preset | Fresh download into each run's staging |
| Weights, tokenizer and declared companion assets | Card/lock origins, revisions, paths, sizes and digests | Persistent verified asset cache |
| HRX compiler/runtime/acquisition tools | Known release pinned with artifact digest | Download to per-run staging and install in an isolated environment |
| Python acquisition dependencies | Pinned dependency closure for the selected SDK | Per-run download/install; discard with the attempt |
| Base container | Reviewed OS/Python image digest | Docker-managed layers; the per-run SDK owns all Loom tools |

The publisher's model card owns model acquisition. CI owns HRX selection and the
acquisition environment. Changing an HRX artifact must not invalidate otherwise
unchanged model assets. Changing pipeline code must not force weight downloads
when its locked asset identities remain the same.

## HRX artifact source

Use a published **`hrx-loom` SDK wheel** as the initial candidate. The SDK packages
`loom-fetch`, `loom-serve`, compiler tools and matching native libraries. The HF
provider is an optional dependency; resolve and pin that dependency closure too.
This is a different product from the older `hrx-solutions` serving wheel.

The [published v0.1.0 release](https://github.com/ROCm/hrx-system/releases/tag/v0.1.0)
provides this concrete Linux Release candidate, checked against GitHub metadata
and the release's source entry points on 2026-11-10:

```json
{
  "repository": "ROCm/hrx-system",
  "tag": "v0.1.0",
  "source_commit": "d01d1c18b154189fd073777329052d23f6071f14",
  "asset_id": 687300101,
  "filename": "hrx_loom-0.1.0-py3-none-manylinux_2_39_x86_64.whl",
  "size_bytes": 22272294,
  "sha256": "4d3897b83cc0857723593487a4372a31d203944a1637e5e3143cad1d0583b98b"
}
```

This is a proposed artifact selection, not an installed or qualified runtime.
Verify the downloaded bytes against the pinned digest, select a compatible
Linux/Python container, then validate the installed fetcher's CLI and selected
card/lock compatibility without serving. Missing capabilities are an explicit
artifact-selection failure; do not silently fall back to a local source tree,
globally installed SDK or the hrx-solutions wheel.

Pin the release repository, tag, asset name/ID, payload digest and size in
configuration. Every validation run uses that same known release. Do not follow
latest builds, build HRX from source or fall back to an arbitrary CI artifact.
A release update is an explicit configuration change. Candidate-build testing
can be added later; this epic validates the testing infrastructure itself.

Keep any required GitHub read credential in trusted acquisition only. A workflow
token is not assumed to grant access to another private repository. Do not copy
the owner's GitHub CLI credentials into the runner or model execution container.

## Per-run runtime compatibility

Use one known compatible released SDK and one pinned pipeline publication
as the validation baseline. Download and verify that release and its pinned
dependencies into each attempt, then install them in a fresh venv inside the
pinned base container. Invoke explicit venv tool paths.
Verify the installed artifact identity instead of resolving a newer build each
run. "Fresh" describes run staging and environment isolation, not a new version
and the SDK payloads are downloaded into the attempt rather than cached.

Download the pipeline for that run, then use the installed pinned SDK to check:

- The card/lock, model format and selected preset/program set can be understood.
- Declared pipeline targets/requirements match the assigned device, using that
  SDK's compatibility rules, and the required cached asset closure is accessible.
- The installed tools/libraries provide the required interfaces.

Record failures against the selected runtime/pipeline pair. An acquisition,
installation or compatibility failure is visible CI evidence, not grounds for
silently removing a previously expected model from coverage. If a pipeline now
references an uncached asset, report a cache miss and have the operator rerun
seeding; do not make the model cache writable or download into it from CI.

These preflight checks establish input/interface compatibility only. This
epic also runs actual preparation and inference to test operational compatibility.
The precise parser/capability probes are implementation checks, not a claim that
one existing CLI command proves the whole pipeline can execute.

## Storage and execution boundary

Proposed external runner state:

```text
$HRX_SYSTEMS_ROOT/
  cache/huggingface/        reusable HF assets
  cache/loom-assets/        reusable non-HF assets, when required
  runs/<attempt>/sdk-payloads/ temporary SDK and dependency downloads
  runs/<attempt>/pipeline/  freshly downloaded pipeline
  runs/<attempt>/staging/   incomplete acquisition and transient metadata
  runs/<attempt>/results/   manifest, acquisition events and summary
```

Preserve the existing HF/Loom cache implementations for model payloads. Download the selected HRX release into per-run staging; do not build an SDK
cache or another model store.
Cache keys must identify immutable bytes, not merely a filename, model name,
release tag, container version or the whole nightly job.

The manually invoked seeding script launches an acquisition container with write
access to its designated asset caches and per-run staging. CI instead mounts the
model-asset caches read-only, with separate writable pipeline/runtime staging.
Resolve selected file roles with native card/lock semantics. Fetch pipeline files through a fresh per-run path that
bypasses persistent pipeline payload reuse, while allowing asset transport to
reuse verified cached content. A pipeline-local `tokenizer.json` is still a
reusable asset; an auxiliary `weights.loom` program input is still fresh pipeline
content. Neither repository location nor the word "weights" determines policy.

`HF_HUB_CACHE` and `LOOM_CACHE_HOME` choose cache locations, and
`--materialize=auto` chooses sharing/copying behavior. Those settings alone do
not implement the required fresh-pipeline/asset-reuse split. Establish that
integration using the selected SDK's actual APIs before claiming the policy works.

Publish readiness only after verification completes. Coordinate writes and keep
published inputs immutable while readers hold references. Partial files cannot
be cache hits. Use compatible runner/container ownership and consistent paths
so all shared-file references resolve inside containers. Account for copy
fallbacks when linking is unavailable.

After acquisition, start a separate **read-only, network-disabled verification
container** over the prepared pipeline, asset and HRX inputs. With writable
scratch elsewhere, verify closure accessibility and SDK availability. This is
an offline check of already acquired inputs, not a claim that a new attempt
can acquire its pipeline or SDK without network access. No GPU device is needed.

## Evidence and acceptance

The operator runs seeding twice against one persistent model-asset cache. A
manual GitHub workflow then downloads the pipeline and selected runtime, checks
the prepared inputs and uploads reports. CI has no model-cache write mount.
Weights, SDK payloads and caches are not uploaded as result artifacts.

Each file event identifies its role, source/revision, digest/size, verification
result, cache hit/miss, downloaded payload bytes and local materialization
outcome. Record metadata requests separately from payload transfer. File mtimes
or "download complete" messages alone are insufficient proof of reuse; reading
or hashing a cached GGUF is not a network download.

Acceptance checkpoints:

1. The operator selects a compatible host/model/preset; the run records their
   identities and effective workload without requiring the later suitability planner.
2. Two manual seed invocations show cold acquisition followed by reuse of
   unchanged model payloads, including tokenizer and companions.
3. A GitHub run downloads fresh pipeline/SDK inputs, installs the pinned release SDK
   in the pinned base container and reads required cached assets without writes.
   Missing assets are reported for manual reseeding.
4. The real model becomes prepared within a deadline, a bounded request satisfies
   the known-answer assertion, and owned server/container processes shut down.
5. A repeat attempt preserves model assets and acquires its pipeline/runtime
   from its unchanged pins. Reports include source/runtime/model identities, cache
   evidence, readiness/query results and cleanup, including failure paths.

## Completion boundary

The epic completes only when a real manually dispatched GitHub end-to-end test
passes on the operator-selected compatible host, with reviewable acquisition,
readiness, inference and cleanup evidence. Cache-only verification or synthetic
fixtures do not complete it.

Automatic platform selection/qualification (`hst-otm`), CI-driven model seeding,
nightly scheduling, full model coverage and distributed execution remain later.
