# Prepare cached models for an offline test

From the repository, run:

```sh
bash scripts/prepare-cached-models.sh --output "$attempt/prepared"
```

Preparation defaults to the checked-in `config/cached-model-tests.json` suite and
`HRX_SYSTEMS_ROOT` (otherwise `$HOME/.local/share/hrx-systems-test`). `--suite` and
`--root` are optional overrides. Without `--output`, a unique preparation attempt
is created under the external root's `runs/`. An explicit output must be new.
Docker access, source access and a previously seeded model cache are required;
preparation neither seeds assets nor starts a GPU server. No sudo is performed.
For gated sources, acquisition accepts `HF_TOKEN` without copying auth files.

The suite contains unique path-safe entry IDs (`runtime` is reserved), repository-relative baseline config paths
and cache-relative seed-manifest paths. Start with the existing Qwen3.5 0.8B
entry; add other operator-selected compatible models using their baseline and
seed manifest. The checked-in suite does not duplicate asset URLs or digests.

The command pulls the pinned base image, downloads and verifies the selected
release SDK/dependency wheels, and stages each hosted card/lock/program selection
fresh. A separate network-disabled container verifies and materializes model
assets from cache mounts marked read-only. It does not repair cache misses or
write provider metadata into the asset cache. Code and metadata caches are
per-attempt scratch, removed with the acquisition containers and venvs.

The prepared bundle contains:

- `prepared-suite.json`: frozen expected entries, source hashes and common runtime
  and host/container cache-root contract. Entries retain selection, test kind,
  request/assertion, workload, preparation result/reason and evidence references.
  All expected IDs are recorded before acquisition, including interrupted/unreached entries.
- `config/<id>/baseline.json` and dependency lock snapshots, plus the common
  `config/runtime/baseline.json` used for offline SDK installation.
- `sdk-payloads/`: verified wheels retained only for this attempt's execution.
- `models/<id>/model/` and adjacent `prepared-inputs.json`: a native relocated
  selected card/tree and verified file identities. Serving mounts the entire
  `models/` directory at `/inputs` and the cache roots at `/model-cache/...`.
- Compact seed copies, acquisition logs and stage SDK identity receipts.

`runtime` fixes `image`, `platform`, `sdk`, `dependency_lock_sha256`, `sdk_identity`,
`sdk_payloads` and `sdk_baseline`. `cache_roots` maps `huggingface` and `loom-assets`
to `host_path` and `container_path`. Each entry's ready `inputs_manifest` is a
bundle-relative path with `inputs_manifest_sha256`; its `model_card_path` is the
absolute server-visible `/inputs/<id>/model/model.json`. Do not reinterpret those
container paths as host paths. Different accounts must read the same configured
cache roots; preparation cannot grant account access.

A missing/corrupt asset or other entry failure is recorded and remaining entries
are still prepared. The command exits nonzero if any expected entry failed,
while retaining a partial suite with every expected entry. Only a verified entry
gets a ready input manifest. Invalid top-level input fails before acquisition.
Execution can consume ready entries from a partial suite but must preserve the
failures in its overall result.

Keep the prepared bundle until execution finishes; it is not a cross-run SDK or
pipeline cache. Run execution with the local prepared image, offline staged SDK
payloads and read-only inputs/cache. Remove the prepared staging only after it is
no longer needed; retain compact raw results separately for report regeneration.
