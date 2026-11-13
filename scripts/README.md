# Repository commands

Public Bash commands derive the checkout from their own path and dispatch through `python3 -I python/run.py ROLE`. They work from another working directory without installing the harness or setting `PYTHONPATH`.

| Responsibility | Canonical commands |
| --- | --- |
| Host runner and storage | `host/bootstrap-runner.sh`, `host/check-container-storage.sh`, `host/qualify-cached-models.sh` |
| Explicit host cache writers | `cache/seed-models.sh`, `cache/seed-cached-models.sh`, `cache/seed-pipelines.sh`, `cache/download-models.sh` |
| Explicit asset receipt verification | `cache/verify-cached-assets.sh` |
| Publication discovery and revision plans | `cache/refresh-publications.sh` |
| Prepared model lifecycle | `test/prepare-cached-models.sh`, `test/run-prepared-models.sh`, `test/run-cached-models.sh` |
| Saved-results reporting | `report/generate-model-report.sh`, `report/package-model-failures.sh` |

Paths in this table are relative to `scripts/`. Options and defaults remain the same. Preparation reads host-seeded assets and original pipelines; execution runs offline with read-only inputs; reporting uses saved files only. The suite wrapper composes those same commands.

```sh
bash scripts/cache/seed-pipelines.sh --selection cached
bash scripts/test/prepare-cached-models.sh --output "$attempt/prepared"
bash scripts/test/run-prepared-models.sh --prepared "$attempt/prepared" --output "$attempt/results"
bash scripts/report/generate-model-report.sh --results "$attempt/results" --output "$attempt/report"
python3 -I python/run.py paired-ci --help
python3 -I python/run.py release-runtime --help
```

The importable implementation is [python/hrx_systems](../python/hrx_systems). Its launcher has a fixed role registry; arbitrary module names or filenames are not accepted. `layout.py` owns code roots, argument vectors, source mounts and implementation provenance. Native stages mount only `python/` and `tools/migrations/` at their corresponding `/source/` paths, alongside the existing staged inputs and attempt data. Loom imports stay inside native SDK operations.

Use [publication refresh](../docs/refresh-publications.md) to generate revision and
catalog updates from hosted cards before explicit cache seeding and local testing.

All 13 former top-level Bash paths remain thin `exec` aliases to the grouped commands. The documented `paired-ci.py` and `release-runtime.py` paths remain CLI-only aliases. They are not import APIs; Python callers import the corresponding package modules.

The portable [0.1.0-to-0.2.0 upgrader](../tools/migrations/upgrade-pipelines-0.1.0-to-0.2.0.py) runs when copied alone. Its former top-level Python path is a repository-relative symlink, preserving copy-by-value use. New downloads should use the canonical `tools/migrations/` URL.

The historical single-model fixture lives in [tests/support/single_model_e2e.py](../tests/support/single_model_e2e.py). `scripts/single-model-e2e.py` remains its CLI alias. It is test support and has no production dispatcher role.
