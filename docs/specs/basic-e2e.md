# Cached-model verification — shared specification

## Agreed outcome

Validate every registered cached model using three locally runnable Bash stages:
prepare inputs, execute real model tests through one hot-loading server, then
generate polished verification artifacts from saved results. GitHub calls the
same stages. Every expected entry is reported, including failures/not-run.

The current cache has one Qwen3.5 0.8B selection. SDK bootstrap and real cold/warm
asset seeding are complete (`hst-utq.1/.2`). Current implementation gates now
prove local read-only preparation, real shared-server inference and independent
report generation; dedicated-account/GitHub delivery evidence is tracked separately. Source groundwork is committed locally as
48bf73b on `single-model-e2e`, not integrated or pushed. Beads is the source of
truth for current task status, ownership and blockers.

## Responsibilities

| Bead | Deliverable | Specification |
|---|---|---|
| hst-utq.1 | Pinned release acquisition/offline SDK install (complete) | [SDK](sdk-release.md) |
| hst-utq.2 | Manual model-asset seed/cache reuse (complete) | [Seed](manual-seed.md) |
| hst-utq.3 | Prepare Bash stage, suite/config references and prepared bundle | [Preparation](ci-inputs.md) |
| hst-40e | Execution Bash stage, GPU container and shared-server lifecycle | [Execution](serve-test.md) |
| hst-utq.6 | Independent report Bash stage and artifact rendering | [Report](results-report.md) |
| hst-utq.4 | Optional combined wrapper and GitHub stage composition/upload | [Integration](github-workflow.md) |
| hst-utq.5 | Real local/GitHub validation and artifact proof | [Validation](validation.md) |

[Stage commands and data contracts](pipeline-stages.md) define the shared
interfaces; [the direct route](cached-model-e2e.md) explains the implementation
checkpoints. Each implementation task owns its public Bash wrapper and private
Python implementation. No separate implementation for CI, frontend build system
or catalog/service framework.

## Common constraints

- Use known released HRX SDK/image pins and each baseline's fixed pipeline
  publication. No latest-build fallback, source build or implicit pin changes.
  Download SDK/dependencies per preparation attempt; no persistent SDK cache.
  Containers install fresh isolated venvs; do not share or relocate venvs.
- Pipeline card/lock/programs are fetched fresh during preparation. Model weights,
  tokenizer and declared companion assets are reused from native HF/Loom caches.
  Only manual seeding writes those caches. Preparation and execution mount model
  caches read-only, report misses and never repair or silently reseed.
- Assume operator-selected compatible models/device for this checkpoint. Record
  actual hardware and workload; runtime compatibility/load/OOM failures remain
  failures. Hardware qualification (`hst-otm`) and nightlies (`hst-qcw`) are later.
- Preparation needs no GPU. Execution downloads nothing, starts one empty server,
  register/tests/unregisters each ready entry sequentially, then stops once.
  Confirm asynchronous removal through `/v1/status`; drain/deactivate or HTTP202
  alone does not prove unload. Continue only after confirmed cleanup/healthy server.
- Execution owns one named non-root GPU container, numeric device group access,
  isolated runtime scratch and bounded cleanup. No network/source credentials,
  published host port or Docker socket inside execution. Agents perform no sudo;
  operator-owned account/permission changes remain explicit prerequisites.
- `HRX_SYSTEMS_ROOT` falls back to `$HOME/.local/share/hrx-systems-test`; configure
  the same root for the dedicated runner and prove read access. Preserve native
  `cache/huggingface` and `cache/loom-assets`, mounted at `/model-cache/...`.
  All prepared model trees are mounted together read-only at
  `/inputs/<entry-id>/model`, with adjacent `prepared-inputs.json`.
- A passing nonempty suite needs every expected selection's real assertion and
  completed model removal plus final server/container shutdown. Missing evidence
  cannot become success. Repeat cycles do not inflate distinct model coverage.
  Text smoke success establishes only that tested path/preset/variant.
- Report generation reads only saved raw results, producing HTML/Markdown/JSON
  and compact linked evidence. It needs no model cache, GPU, Docker or network.
  Artifacts contain no model/pipeline/SDK payload trees. Preserve failure evidence
  and stage exits even if later rendering succeeds.

## Validation and stopping boundary

Prove a real local prepared-model run first, including two load/query/unload cycles
in the same server process (repeat the sole cached model if necessary). Then
validate all registered entries through manual GitHub runs, repeat with fresh
pipeline/SDK staging and unchanged cached assets, and inspect the rendered report.
Synthetic fixtures, successful acquisition or input verification do not complete
the epic. Implementation tasks stay open until their own evidence exists.
This planning update authorizes no implementation, commits, pushes or publication.
