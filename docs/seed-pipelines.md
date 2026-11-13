# Seed original pipeline inputs on the host

Seed pipeline inputs explicitly before deploying a preparation consumer that
requires this cache. Ordinary preparation and both CI lanes read it; they never
download missing pipeline inputs or repair stored entries.

From the repository, seed the existing cached model inventory:

```sh
bash scripts/seed-pipelines.sh --root /srv/loom-models --selection cached
```

This selects the existing model seed manifests, without creating a second model
catalog. The default selects `config/cached-model-tests.json`; use `--suite FILE`
for a smaller authored suite. `--suite` and `--selection cached` are mutually
exclusive. Existing model assets must already be seeded. This command stores
original cards, locks and selected program/auxiliary files, and does not fetch
GGUF weights or execute models. Docker access and writable host storage are
required; no host packages, services or GPU settings are changed.

Use `--output NEW_DIRECTORY` to keep a seed attempt's report and logs at an
explicit location. The directory must not already exist. `--runtime FILE`
selects another existing runtime configuration for the disposable seeding SDK.
The batch report is `NEW_DIRECTORY/pipeline-seed-report.json`; each selected
entry has its own `pipeline-seed.json`, `seed.log` and SDK acquisition evidence.
The current seeder acquires and installs a disposable SDK for each entry, so a
large inventory also repeats those SDK downloads. None persist as an SDK cache.

Use the same root for the operator and runner. Pipeline storage is separate from
the model assets:

```text
/srv/loom-models/cache/pipelines/
  .seed.lock
  .publish.lock
  entries/<selection-key>.json
  blobs/sha256/<digest>
```

An entry binds the immutable publication and exact selection to the card/lock
hashes from its model seed. It records the selected source closure, original
document coordinates and per-file identities. When an asset-only upstream lock
omits source hashes, source identities are explicitly recorded as observed by
the host seeder, rather than claimed as upstream lock commitments. The SDK used
to seed is provenance; it does not become a cache lookup key.

## Read access and rollout

The operator must have write access to the cache root, and the runner must be
able to traverse its parent directories and read the entries and blobs. New
cache directories/files permit runner reads with modes `0755`/`0644`. The seeder
does not change accounts, groups or existing ACLs. An unreadable parent directory
requires an operator to arrange access before running CI.

Deploy in this order:

1. Seed model assets with the existing model-seeding commands if needed.
2. Run `seed-pipelines.sh` for the selections the runner will use.
3. Check the seed report and resolve any failed entries.
4. Activate the preparation consumer and CI workflow changes.

Do not run writers or evict cache files during preparation. The seeder serializes
writers and publishes complete entries atomically. A failed entry does not
invalidate previously published entries; a batch with failures exits nonzero.

## Reseed and repair

Repeat the same explicit seed command to verify and reuse valid entries. After
changing a pipeline publication pin or updating a model seed, seed that new
selection before using it. Age alone does not make immutable cached inputs stale.
New pins cannot silently select an older entry.

If preparation reports a missing, unreadable, mismatched, incomplete or corrupt
entry, inspect the diagnostic and run its host-reseed command. For the full
cached inventory that command is:

```sh
bash scripts/seed-pipelines.sh --root /srv/loom-models --selection cached
```

Only the explicit seeder may repair missing or corrupt data. Do not edit entry
manifests, rewrite pins to match old entries, or copy upgraded sources into this
cache. Conflicting newly fetched bytes for an already valid identity are an
error. Interrupted seed attempts preserve published entries; automatic eviction
and garbage collection are outside this workflow.

## Preparation and SDK selection

Run the existing public preparation stage after seeding:

```sh
bash scripts/prepare-cached-models.sh --root /srv/loom-models \
  --selection cached --runtime-source release-latest --output /tmp/prepared-release
```

Choose a fresh output directory. `--runtime-source actions-main` selects the
latest eligible CI SDK instead; the paired workflow freezes both SDK references
and one common model inventory. Latest SDK resolution remains online, and SDK
packages and dependencies are acquired per attempt. Seeding pipelines does not
cache SDK wheels, installed environments or native fetch plans.

Pipeline preparation uses the exact seeded originals in network-disabled
containers, without a Hugging Face token. A warm hit makes no pipeline download
requests and writes nothing to persistent pipeline storage. Small source files
are verified and copied into disposable staging; existing model-asset receipt
verification remains unchanged. Missing pipeline entries fail the affected
selection with reseeding guidance and retain the requested denominator.

Actual SDK version controls conditional setup. A `0.1.0` SDK uses original
inputs; a supported `0.2.0` SDK applies the versioned upgrade to its disposable
copy. Both lanes share original cached bytes and have independent prepared
copies. Prepared source links remain within staging or the existing read-only
model-asset cache, so execution does not depend on the pipeline cache remaining
available afterward.

Saved preparation and raw evidence identify the requested publication, seeded
entry, source hashes and cache outcome. Reports describe pipeline inputs as
host seeded and regenerate from saved evidence without reading the cache.
Warm-cache receipts record zero pipeline requests, downloaded bytes and cache
writes. The seed manifest records verified content sizes, which are not exact
network transfer or request measurements; redirects, metadata and source reuse
can affect wire traffic. SDK download receipts account for SDK bytes separately.
Timings do not imply a guaranteed speedup or model compatibility.
