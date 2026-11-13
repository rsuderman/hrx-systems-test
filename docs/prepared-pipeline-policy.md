# Frozen pipeline policy during preparation

The public preparation and composite commands accept `--pipeline-upgrade auto|none`,
`--pool-backing fixed|elastic`, and `--memory-bytes VALUE`. Defaults come from the
verified SDK version: HRX 0.1.0 retains the original pipelines and runtime behavior;
HRX 0.2.0 receives the versioned migration and its frozen runtime policy. Unsupported
versions fail automatic selection. `none` records a diagnostic source bypass.
Runtime settings remain version qualified; see [runtime policy](runtime-policy.md).

For example, use the ordinary three stages with a dynamically selected release:

```bash
scripts/test/prepare-cached-models.sh --selection cached --runtime-source release-latest \
  --root /srv/loom-models --output /tmp/prepared-release
scripts/test/run-prepared-models.sh --prepared /tmp/prepared-release \
  --output /tmp/results-release --device-uri amdgpu://0 --cycles 2
scripts/report/generate-model-report.sh --results /tmp/results-release \
  --output /tmp/report-release
```

`--selection-snapshot FILE` consumes the paired planner's frozen ordered inventory
and exact seed bytes instead of rescanning receipts. `--runtime-selection FILE`
consumes a resolver-frozen reference instead of resolving an alias again. It is
mutually exclusive with `--runtime-source`. A tagged resolution failure produces
an ordinary runtime-setup failure for every expected entry, with no acquisition
or serving attempt. The composite command forwards the same preparation flags.

Before preparation, the operator seeds exact original pipeline inputs using
[seed-pipelines.sh](seed-pipelines.md). Preparation reads this cache without
network fallback, stages independent copies, and installs the verified originals
before applying the migration. The executable tree stays at `models/ID/model`; an upgraded entry keeps
its installed original at `models/ID/model-original`. Asset links remain shared
and asset integrity checks retain their existing receipt-based behavior. Raw
published card and lock hashes are separate from hashes of the relocated installed
original and executable derivative. Original locks describe publication provenance;
they are not regenerated executable locks.

The prepared suite records `runtime_policy`, `policy_files`, and `frozen_inputs`.
The runtime policy embeds its configuration and request rules. Policy evidence
lives at `policies/runtime-policy.json` and, when selected, `policies/seccomp.json`.
Frozen input evidence lives at `selection-snapshot.json` and
`runtime-selection.json`. Selection snapshots are bounded to 32 MiB; other copied
JSON evidence is bounded to 4 MiB.

Each entry and input manifest share `pipeline_upgrade`, containing the policy and
migration identities, original tree path, original publication hashes, and the
path and hash of `models/ID/pipeline-upgrade.json`. That report identifies the
migration implementation, all original and output source hashes, operations and
bounded diffs. Published card and lock bytes are retained as
`publication-card.json` and `publication-lock.json`. Failed offline preparation
retains a failed or not-reached migration record and available publication evidence.

Per-model baseline JSON preserves `published_execution` and
`published_test_profile` alongside `effective_execution` and
`effective_test_profile`. Preparation selects request overrides before sealing the
baseline digest and derives effective execution settings from the final card.
Execution checks original and executable hashes, baseline and policy identities,
report identity, effective requests and the copied seccomp profile on both host
and container. Execution does not apply migrations or reread live policy config.
Legacy prepared bundles without this policy retain their existing verification.

Each ready entry also records `pipeline_cache`, binding captured original cache
manifest bytes and a read receipt to the source identities used by the upgrade
seal. `pipeline-cache-entry.json` and `pipeline-cache-read.json` remain in raw
evidence; execution preflight needs only these staged copies, not the host pipeline
cache. Missing or corrupt entries fail with the host reseed command.

Raw results retain policy files, frozen references, baseline and dependency-lock
JSON, publication evidence and migration reports through their evidence lists.
Per-model raw metadata also carries the migration decision and published/effective
execution and request profile. Reports can be produced after prepared staging is
removed. Model checkpoints and installed source trees are not copied into results.
