# hst-utq.3 — prepare cached-model test inputs

## Outcome and ownership

Provide `scripts/prepare-cached-models.sh` and its Python implementation. Starting
from the repo-authored suite and manually seeded assets, produce an independently
consumable prepared suite. Downloading/verifying inputs happens here; never start
a GPU server or generate presentation reports. Depends on completed `.1/.2`.

```sh
bash scripts/prepare-cached-models.sh --output "$attempt/prepared"
```

## Inputs

Default to `config/cached-model-tests.json` (authored by this task), with one entry
for the existing baseline. Each unique entry supplies `id`, repository-relative
`baseline` and cache-root-relative `seed_manifest`. Baselines retain publication,
preset/variant/programs, SDK/image pins and bounded request/assertion. Additional
seeded selections reference their own configs/indices; do not duplicate asset
lists or scan arbitrary HF blobs. Freeze all expected entries before acquisition.

Use `HRX_SYSTEMS_ROOT` with its existing HOME fallback and explicit `--suite`,
`--root` and `--output` overrides. Seed files and model caches are read-only.
Entries share one selected runtime/image/device contract; report conflicting
runtime pins instead of choosing silently. See [stage contracts](pipeline-stages.md).

## Flow and outputs

1. Validate suite/config paths and pins, snapshot inputs and initialize per-entry
   outcomes. Pull the pinned image and acquire/verify SDK/dependency payloads into
   per-attempt staging using the existing SDK helper. No host SDK or venv reuse.
2. In online acquisition containers, fetch each hosted card/lock and selected
   program/auxiliary inputs from the pinned publication into fresh staging.
   Resolve native relative sources against their remote document location.
   Preserve native card/lock/role/relocation semantics; code wins mixed roles.
3. In a separate network-disabled phase, match every selected asset to the seed
   manifest and lock, verify its bytes and assemble a model tree referencing
   read-only native caches. Reject missing/corrupt/escaping paths; no download or
   writable-cache fallback. Use immutable links/references rather than another
   persistent weight store. Prove all links resolve at final serving mounts.
4. Publish a ready `models/<entry-id>/prepared-inputs.json` only after that model's
   verification succeeds. Publish top-level `prepared-suite.json` containing every
   expected entry with ready/failed preparation status and reason, tree/evidence
   references and runtime/cache contract. Include SDK payloads and config/lock
   snapshots for offline execution. Follow the shared schema in the stage spec.

Keep failures per entry and continue preparing other entries where possible.
Exit nonzero for any preparation failure, while retaining a usable partial suite
manifest. Invalid top-level input may prevent a usable manifest; retain diagnostic
failure evidence. Never mark incomplete inputs ready. Clean owned acquisition
containers/venvs/scratch; retain prepared trees and per-attempt SDK payloads until
execution finishes. Preparation does not mount GPU devices or require GPU access.

## Acceptance and source-backed check

Fixtures prove fresh program acquisition across attempts, exact native selected
asset closure, missing/corrupt/path-escape rejection, partial-suite accounting,
read-only cache enforcement and final-mount link resolution. Verify attempts can
stage the same pipeline while reusing asset bytes. A real prepared Qwen bundle
must be independently consumed by execution without new acquisition. Closing
this task proves prepared inputs, not successful inference.

Native `ModelCard.select`, roles, lock coverage, `plan_fetch` and provider/cache
surfaces were checked on the pinned release. `install` writes acquisition state
and stock cache flags do not guarantee read-only consumption or fresh code.
Prove the small adapter against installed-release fixtures; report a precise
unsupported seam rather than weakening read-only or selection behavior.
