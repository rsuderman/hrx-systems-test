# Single-model cache seed — hst-utq.2

## Outcome

An operator runs one command from `hrx-systems-test` to cache the configured
model's complete asset set. Repeating it reuses verified model assets. CI reads
that cache without writing it. Seeding does not run a model or select suitable
hardware; this first model/host pair is operator-selected and compatible.

```sh
bash scripts/seed-models.sh
```

The repository owns the configuration, container invocation, SDK setup,
acquisition and index creation. Operators do not edit model cards, enumerate
asset URLs, install the SDK manually or construct Docker commands.

Implementation and operator instructions: [seed the model cache](../seed-models.md).

## Small implementation boundary

Use one shell entry point and one private Python helper using the installed
Loom acquisition APIs. Reuse [the existing SDK helper](../sdk-environment.md).
Do not add a catalog manager, provider plugin framework, scheduler or weight
store. Runtime inputs come from `config/runtime.json`; the first explicit sample
in `config/cached-model-tests.json` supplies the
`loom-fun/unsloth-Qwen3.5-0.8B-loom` publication, `chat` preset,
`gguf` variant and `source` programs. Native card/lock selection determines the
base Q4_K_M GGUF, vision GGUF and tokenizer; do not duplicate that asset list in
repository configuration.

Resolve repository files relative to the script. Default external root is
`HRX_SYSTEMS_ROOT`, falling back to `$HOME/.local/share/hrx-systems-test`; create a
unique `runs/<attempt>/` automatically. `--baseline`, `--root`, `--output` and
`--seed-manifest` are optional overrides for existing contracts and isolated
validation. Print the chosen model/root before acquisition and the manifest and
report locations on completion. Operator and runner must use the same root;
different accounts' HOME defaults do not share a cache automatically.

Require Docker access, writable external storage and any needed source access.
Report missing prerequisites directly; do not perform sudo setup or copy personal
auth configuration into the runner. The wrapper owns and removes its containers
and temporary SDK/venv staging on success, failure or interruption, retaining
compact reports/logs.

## Seed flow

1. **Prepare.** Create the attempt, download/verify the configured SDK and pinned
   dependencies into disposable attempt storage, and install a fresh container
   `/run/venv`. Record that stage's `sdk.json`; no persistent SDK cache.
2. **Resolve fresh pipeline.** Download the hosted card/lock into new attempt
   storage, select through `ModelCard.select` and `plan_fetch`, and download the
   selected `program`/`auxiliary` inputs afresh. During native source resolution,
   preserve the pinned remote publication URL as `Document.location` so relative
   tokenizer/program origins resolve remotely rather than against the staged local
   card path. Retain the downloaded card/lock unchanged and carry the prepared
   sources into the offline asset phase. Use native relocation/materialization
   for the local installation.
3. **Fill model cache.** Acquire and verify remaining selected asset roles using
   native HF/Loom caches, reusing valid bytes. A small role-routing adapter keeps
   code in attempt storage and assets in persistent storage; code wins mixed roles.
   Do not classify by filename: a tokenizer is an asset; auxiliary `weights.loom`
   is code. Retain native source resolution, verification and publication rather
   than writing another downloader. Materialize the complete selected tree.
4. **Publish index.** Only after complete verification, atomically replace
   `$root/cache/seed-manifest.json` (or explicit override). Keep an invocation copy
   and `seed-report.json` in the output directory. A failed seed leaves the prior
   valid index and unrelated cached assets intact. Serialize seed writers for the
   chosen root with one local lock; operators do not reseed/evict during CI runs.

Persistent directories are `$root/cache/huggingface` and `cache/loom-assets`.
Mount these at `/model-cache/huggingface` and `/model-cache/loom-assets` throughout
seed and CI so native cache references remain reachable. Seed mounts are writable;
CI mounts and the published index are read-only. CI always stages its own fresh
pipeline; a seeded pipeline installation is not CI's candidate. No second copy
of the weights is a required durable output.

## Output consumed by CI

Use the existing [shared contract](basic-e2e.md) and [CI input consumer](ci-inputs.md),
not an assumed native `.loom-fetch.json` schema. `seed-manifest.json` records
schema version, publication/preset/program selection, card/lock digests, resolved
origin roots (including aliased repository revision pins), SDK
identity and reusable `files[]`. Each file identifies its installation path,
native roles, immutable origin/revision, expected/verified size and SHA256,
verification result, and `cache_root` plus root-relative `cache_path`.
Resolve and validate cache references inside their designated roots; reject
escaping or dangling paths. CI rechecks identities and bytes and reports missing
or invalid assets for manual reseeding; it never calls the writable seed path.

`seed-report.json` records attempt/selection/SDK identities, fresh pipeline
hashes, asset-phase network policy and success/failure stage. Report transfer
observations only when actually supplied by native progress callbacks; verified
file size is not measured network transfer. These reports contain no weights or
credentials.

## Acceptance and one implementation check

- The no-argument command uses checked-in defaults without hand-authored files;
  root overrides work and its index can be consumed at the same runner root.
- Cold real seeding verifies exactly the selected locked asset closure. Repeat
  the actual command with `--asset-network offline` and a new output directory:
  prepare SDK/image and fresh pipeline online, then execute the **same** asset
  resolution/materialization path in a network-disabled container. Manual cache
  metadata may remain writable. Success proves model-asset reuse; a separate
  read-only verifier or unchanged mtimes does not prove it.
- Small fixtures check the fresh-code/reused-asset split, lock/hash rejection,
  interrupted acquisition and preservation of the prior valid index. Missing
  access, incomplete closure or invalid cache paths fail visibly with a nonzero
  exit. Read-only CI preparation is covered separately by `hst-utq.3`.

The installed pinned release provides `Selection.files.roles`,
`plan_fetch`, `ContentCache.prepare_source/acquire`, configurable HF cache paths
and `install`. `install` accepts one cache and `--materialize=auto` controls
links/copies, not fresh downloads. The private helper subclasses the pinned native
cache at its acquisition methods, routes by native origins/roles, and retains
native resolution, verification and materialization. Installed-wheel fixtures
exercise the fresh-code/reused-asset split and actual cache locations without
monkey-patching internals. Prepared native sources cross the online/offline stage
boundary as explicit JSON; each container creates its own SDK venv.

Dependencies: completed SDK bootstrap `hst-utq.1`; downstream `hst-utq.3` and
`hst-utq.5`. Seeding proves acquisition/cache reuse only; read-only CI preparation,
GPU compatibility and inference remain downstream checks.
