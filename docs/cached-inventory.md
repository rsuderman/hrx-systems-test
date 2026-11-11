# Run every seeded model selection on a runner

After seeding and cache maintenance have finished, run:

```bash
bash scripts/run-cached-models.sh --root "$HRX_SYSTEMS_ROOT" --selection cached
```

The preparation stage discovers this runner's published seed manifests and
freezes the expected selection before downloading pipelines or starting models.
Different runners can have different cached subsets without editing a shared
model list. GitHub's manual **Cached-model verification** workflow has the same
`selection: cached` control. The default `suite` option preserves the proven
eight-model suite; `--suite FILE` remains available locally for explicit suites.

Inventory locations, relative to `$HRX_SYSTEMS_ROOT/cache`, are:

- `seed-manifest.json`
- `models/*/seed-manifest.json`
- `models/catalog/*/seed-manifest.json`

The native seeder publishes a manifest atomically only after its selected weights,
tokenizer and companion files have been verified. In-progress downloads without
a successful manifest are not runnable inventory. Existing standalone GGUF files
need a hosted-card seed manifest: seed that publication with `seed-models.sh` and
an acquisition baseline, using the same cache root. Native acquisition reuses
matching cached assets. A blob index, filename or old download log alone cannot
identify the hosted publication and its complete asset closure.

Selections are deduplicated by hosted repository and revision, model ID, preset,
variant and programs. Repeated manifests remain visible as aliases in the saved
`inventory.json`. Distinct revisions or variants are separate test entries.
Conflicting immutable asset/card identities fail explicitly. Malformed,
unreadable or escaping inventory records are reported as inventory errors and
prevent complete success; they do not silently shrink coverage. Symlinked
inventory directories and manifests are rejected. Immutable asset links inside
the designated native cache roots remain supported.

Published card/lock files are pulled at the seeded revision and checked against
the frozen receipt. The card supplies execution capacities; the repository owns
test behavior. All text services get a bounded numeric smoke request and an exact
final-answer assertion. The existing eight profiles retain their proven prompt,
generation and reasoning overrides. Missing overrides do not exclude a model.
Decision services get a single-choice `/v1/systemone` smoke request; its assertion
checks the returned identity, option, probability, confidence and token usage.
This verifies the decision service contract, not learned decision accuracy.
Other service kinds currently produce an explicit `unsupported_test_kind`
preparation failure. No model is counted as passed merely because it is cached.

All entries share one SDK/image identity. Cached selection currently defaults to
the existing pinned release configuration. `--runtime-baseline FILE` supplies a
different SDK/image baseline with its adjacent dependency lock; this is separate
from seed-time SDK provenance. Actions-artifact acquisition is tracked separately.
No runtime is selected from cache-controlled commands or old seed SDK metadata.

Preparation and execution consume model caches read-only. They never fetch missing
weights, repair a cache or change memory capacities silently. Missing/corrupt
assets, incompatible runtime, unsupported service contracts and failed queries
remain visible outcomes. One owned server loads/unloads models sequentially;
uncertain cleanup stops later entries. Hardware fit still requires appropriate
runner/model qualification; published capacity is not a memory recommendation.

The attempt retains the inventory snapshot/hash, exact source/runtime identity,
prepared workload settings and compact per-model evidence. Reports can be
regenerated from saved results without the cache. Normal confirmed cleanup
removes disposable prepared inputs, leaving the immutable model assets intact.
Do not seed, evict or migrate the cache while tests are running.

## Asset integrity without repeated weight reads

Seed writers verify SHA256 and publish stat-bound receipts in
`cache/asset-verification.json`, under the existing seed lock. Each receipt binds
the expected SHA256 to device, inode, size, nanosecond modification time and
change time. Different physical copies of identical bytes require separate
receipts. Fresh programs, cards and SDK inputs retain their full integrity checks.

For an existing cache, perform one manual adoption pass as its normal writer:

```bash
bash scripts/verify-cached-assets.sh --root "$HRX_SYSTEMS_ROOT"
```

This hashes published seed manifests' complete asset closures once per physical
identity, with checks for mutation/replacement during hashing, then publishes
the index atomically. It downloads nothing, installs no SDK, and does not modify
weights or seed manifests. Failed verification preserves the previous index.
Wait for seeders and model tests to be idle before running it.

Normal preparation snapshots that index into its own output and trusts only
receipts whose complete stat identity still matches. Missing/changed receipts
trigger SHA256 verification once per unchanged physical file in the preparation
batch. Preparation records new receipts only in its disposable prepared inputs;
it never writes the shared index. Host/container execution preflights check those
frozen receipts against current metadata and fail if assets changed after
preparation. They do not hash model weights again. Older prepared bundles without
these receipts must be prepared again.

For a deliberate full read, pass `--full-integrity` to the preparation/combined
command, or select the matching manual GitHub input. The shared preparation
verifier hashes each distinct unchanged physical asset once; subsequent aliases
reuse that attempt's verification. Prepared-suite and raw-result metadata expose
`sha256_files`, `sha256_bytes`, `receipt_hits` and `stat_checks` for preparation and
execution preflights. Metadata receipts assume an immutable local cache; use
full integrity verification to detect changes that filesystem metadata alone
cannot establish, such as silent storage corruption.
