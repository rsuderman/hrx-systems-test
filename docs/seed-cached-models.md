# Seed the configured suite

From the repository:

```sh
bash scripts/seed-cached-models.sh
```

This reads `config/cached-model-tests.json` and sequentially invokes the existing
[single-model seeder](seed-models.md) for every entry. Each entry's baseline and
cache-relative manifest path come from that same suite file. It adds no downloader,
asset store or model-selection logic.

`HRX_SYSTEMS_ROOT` selects external storage, defaulting to
`$HOME/.local/share/hrx-systems-test`. Optional `--root`, `--suite` and `--output`
override the defaults; an explicit output directory must be new. The suite's
baseline paths are repository-relative and manifest paths are relative to
`$HRX_SYSTEMS_ROOT/cache`. Invalid/escaping paths and duplicate IDs or manifest
destinations are rejected before any model downloads.

Child logs and reports remain under the printed attempt directory, together with
`seed-suite.json`, which records every expected entry and child exit status. A
failed entry does not hide later entries: remaining models are attempted and the
wrapper exits nonzero if any seed fails. Interruption terminates the active
single-model command and retains completed/current/not-run outcomes.

To exercise the same seeding path with model-asset networking disabled:

```sh
bash scripts/seed-cached-models.sh --asset-network offline
```

SDK and pipeline acquisition still occur online. Persistent assets must already
be seeded; missing or corrupt assets fail. CI model-cache mounts remain read-only.

When seeding as a different account from the runner, optionally grant an existing
user read access to each successfully published manifest:

```sh
bash scripts/seed-cached-models.sh --reader-user EXISTING_RUNNER_USER
```

This requires `setfacl` and ownership of the manifest; it uses no sudo and never
changes accounts or recursively changes cache permissions. It grants that user
`r--` on each manifest and extends its ACL mask by read permission only. Existing
directory traversal and asset-read permissions must already be configured by the
machine owner. Without `--reader-user`, the wrapper changes no ACLs. Do not reseed
or evict assets during model tests.
