# Script responsibilities and callers

The cleanup starts from accepted cache commit `1344d969`. Public commands, flags,
container paths and saved-result formats stay stable. Existing scripts that
already own one responsibility remain in place; directory moves alone would not
improve their boundaries.

| Responsibility | Current callers | Implementation owner after cleanup |
| --- | --- | --- |
| Host cache publication | `seed-pipelines.py`; compatibility API in `pipeline-cache.py` | `lib/hrx_systems/host/cache_writer.py` |
| Original pipeline schema and read-only storage | preparation host precheck, native adapter, source preflight, host seeder | `lib/hrx_systems/pipelines/cache_reader.py` |
| Native SDK cache protocol and disposable staging | preparation native passes | `lib/hrx_systems/pipelines/native_cache.py` |
| Native selection, indexed assets and installation | `prepare-cached-models.py` online/offline passes | `lib/hrx_systems/pipelines/preparation.py` |
| Preparation orchestration and SDK installation | public prepare/composite commands; both CI workers | `prepare-cached-models.py` composes native preparation, SDK and source seals |
| Runtime selection/acquisition | preparation and paired planner | existing `sdk-environment.py`, `release-runtime.py`, `actions-runtime.py` |
| Pinned native publication ABI evidence | preparation and offline policy verification | `lib/hrx_systems/pipelines/publication_abi.py` |
| Runtime-qualified request/launch policy | preparation and execution preflight | existing `runtime-policy.py` |
| Original/derived source seals | preparation and execution preflight | existing `pipeline-preparation.py` |
| Pure source rewriting | source-seal helper and standalone CLI | `upgrade-pipelines-0.1.0-to-0.2.0.py` |
| Execution lifecycle | public runner and CI worker | existing `run-prepared-models.py` |
| Paired CI sequencing and publication | workflows and `paired-ci.py` | existing CI helpers/workflows |
| Saved-results rendering and bounded evidence | public report/package commands | existing report/evidence helpers |

`pipeline-cache.py` remains a compatibility facade. It exposes the existing reader,
writer and native-adapter API while each implementation has one owner. Read-only
lookup never calls the writer. Native preparation accepts its existing services
explicitly and has no Docker or CLI orchestration. The public preparation helpers
continue to delegate through their original callable signatures.

`_bootstrap.py` loads trusted repository modules under `python -I`, using absolute
paths rooted at the scripts directory. The three affected public helper loaders
share this boundary. Container mounts already include the whole scripts directory,
so internal modules are available at the same relative paths. Preparation and
execution provenance include the bootstrap and internal Python files, preserving
the implementation hash closure after extraction.

The versioned upgrader remains self-contained and standard-library-only. It does
not import the internal package, acquire inputs, invoke Docker or select runtime
policy. Host publication, read-only preparation, runtime policy and pure rewriting
remain distinct operations. This cleanup does not move or reseed the host cache,
change two-lane CI topology, or alter model/runtime behavior.
