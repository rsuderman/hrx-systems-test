# Command and implementation responsibilities

[The command directory](../scripts/README.md) lists canonical Bash paths and supported legacy aliases. Production Python lives under `python/hrx_systems`; package markers are lazy and do not initialize the SDK or probe hardware.

| Package | Responsibility and principal modules |
| --- | --- |
| `host` | Optional hardware advisory (`hardware_advisory`); bootstrap and storage checks remain Bash. |
| `cache` | Explicit asset/pipeline seeding and publication (`seed_assets`, `seed_suite`, `seed_pipelines`, `download_catalog`, `pipeline_writer`); asset receipts (`assets`); read-only original pipeline validation (`pipeline_reader`) and native staged-copy protocol (`native_adapter`). Readers do not import writers. |
| `selection` | Metadata-only inventory, configuration and frozen selection snapshots (`inventory`, `configuration`, `snapshot`). |
| `sdk` | Release/Actions selection, per-attempt payload acquisition/install, pinned publication ABI evidence (`releases`, `actions`, `environment`, `publication_abi`). Native imports remain lazy. |
| `preparation` | Frozen runtime policies and host/native preparation (`runtime_policy`, `command`, `native`). Reads shared caches without publishing to them. |
| `pipelines` | Saved source seals and offline handoff validation (`seals`); fixed adapter to the standalone migration (`upgrader`). |
| `execution` | Shared-server load/query/unload cycles, owned-container cleanup and saved raw results (`command`). |
| `ci` | Frozen paired planning, lane attribution, exclusions, sequencing and aggregation (`paired`). |
| `reporting` | Saved-only rendering, runtime identity presentation, failure records and bundles (`render`, `runtime_identity`, `failures`, `package_failures`). No SDK/network/cache requirement. |
| `diagnostics` | Bounded progress transport and evidence capture (`progress`, `capture`). |

`python/run.py` establishes its own trusted package root under `python -I` and rejects preloaded foreign package modules. The fixed registry in `cli.py` routes public roles to `main(argv=None)`. Internal code uses ordinary imports. `layout.py` is the single authority for code roots, command vectors, narrow read-only source mounts and source-file enumeration.

Native stages use the same launcher at `/source/python/run.py`; fixed private SDK operations inspect the target environment or a staged wheel. No project filename loader or whole-checkout container mount is needed. The sole production filename-import exception is `pipelines/upgrader.py`, whose fixed path loads the byte-identical portable tool under `tools/migrations/`.

Prepared source metadata now records `source.layout_version: 2`, the canonical `migration_implementation_path` and hashes of actual implementation files. Offline verification accepts historical inputs with an absent layout marker using their saved legacy migration hash key. Explicit unknown, null or wrong-typed markers fail; layout2 requires the canonical path and saved hash. Source layout does not select runtime policy schema or publication ABI. Saved inputs are never rewritten to adapt to the new checkout.

The old private bootstrap/library/facades have been retired. The 13 former Bash paths, paired/release CLI shims, portable-tool symlink and test-fixture CLI shim remain deliberate compatibility aliases. The fixture implementation is under `tests/support/` and is never imported by production.
