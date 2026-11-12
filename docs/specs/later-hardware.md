# Later: hardware-aware model selection (`hst-otm`)

This follows the basic end-to-end epic. That epic assumes an operator-selected
compatible model/host; this selector and memory qualification are **not gates**
for it. See [the shared contracts](basic-e2e.md).

## Boundary and inputs

Build one small later planner over existing baseline records and reviewed
profiles. It observes the assigned GPU/storage and small pipeline metadata, then
writes a planning report. It never downloads weights, launches inference, writes
model caches or publishes the seeder's ready index. No additional public command
or configuration format is required by the current epic.

Inputs use the composed prepared selection/workload and shared
`config/runtime.json` SDK/image/dependency identity; test requests are independent
profiles, not runtime settings.
The planner also reads pipeline target requirements, reviewed workload-memory
recommendations and, when present, `cache/seed-manifest.json` for missing-byte
estimates. It cannot rewrite the baseline or seed index to make a model fit.
Record GPU identity/canonical target, memory topology, usable total/free bytes,
storage availability and snapshot time. Unknown facts remain unknown.

## Four checks

| Check | Required comparison |
|---|---|
| Pipeline support | Selected pipeline/preset exists and its actual program target requirements match the assigned GPU/backend |
| Available memory | One topology-aware workload recommendation plus runner reserve fits usable capacity; current free bytes determine readiness now |
| Storage | Missing selected asset bytes after cache reuse/deduplication plus staging/headroom fit the cache budget |
| Runtime compatibility | Pinned SDK/container supports the selected card/model format and pipeline interfaces |

For `gfx1100`, accept exact `gfx1100` or compatible `gfx11-generic`; reject
`gfx1101`-only or `gfx12-generic`-only implementations. Use the selected HRX
version's structured compatibility rules, including applicable target features,
not name prefixes or a union of unrelated pipeline variants. Record the actual
device and matched program target. Missing declared targets may use a reviewed
CI support entry bound to the same publication/runtime; do not invent publisher
card fields or assume a missing declaration means universal support.

The memory recommendation covers peak weights, context/recurrent state,
companions, workspace/runtime overhead and margin for the effective workload.
Apply runner reserve separately without double counting. Dedicated VRAM and
shared-memory platforms each need a supported usable-pool budget; do not add
GTT, host RAM and reported VRAM. No separate host-RAM eligibility category is
introduced. File size and publisher hints are not qualified memory budgets.

## Output and ownership

Propose `hardware-selection-report.json`, not another baseline/configuration.
Each entry references its unchanged baseline path/digest and pinned selection,
SDK identity, `seed`, `skip` or `needs_information`, short reason, matched target,
required/total/free bytes, profile reference and `runnable_now`. Record the
hardware snapshot and check time once. Missing or invalid cache entries affect
the storage estimate, not qualification evidence or the seeder's verification.

- Known incompatibility, insufficient capacity or storage: `skip` with reason.
- Missing target/budget/topology/support evidence: `needs_information`.
- Suitable profile but GPU temporarily busy: `seed`, `runnable_now: false`.
- All four checks pass and memory is available: `seed`, `runnable_now: true`.

For an eligible entry, the operator invokes the existing
`bash scripts/seed-models.sh --baseline <referenced-baseline> --root ... --output ...`
contract. Do not pass this report as a baseline or add a second selection API.
The seeder still owns native resolution, integrity and ready-index publication;
CI preparation still owns read-only closure verification. No CI cache writes or
memory reservation follow from selection. Refresh free memory before loading.
Acquisition-only success never promotes a profile to qualified. Current E2E may
use its operator-selected baseline without this report or qualification profile.

A qualification records publication/preset/programs, effective context/rows and
components, SDK/image identity, hardware/topology, estimate or measured bound,
method/evidence/date and reviewer. Runtime or workload changes need re-evaluation
rather than inheriting a different configuration's certification.

## Acceptance and implementation facts

Fixtures cover exact/generic matches, mismatch, missing pipeline, known fit,
insufficient memory/storage, busy GPU and missing/shared-memory qualification.
Every input remains in the output with a reason; unknown is never auto-eligible.
An eligible report entry references the same baseline accepted by the existing
manual-seed interface; the baseline, seed index and model cache remain unchanged.
Use a reviewed measured or conservative estimated profile to demonstrate the
real selector later. No real-model execution is claimed by selection alone.

Local HRX `d8cead9838c5af0c2c7c0d92fdb3287f1b24aaba` supplies the mapping in
`runtime/src/iree/hal/drivers/amd/gpu/target/identity_catalog.inl` and structured
compatibility in `target/identity.h`; Loom's `target_identity.h` defines source
requirement satisfaction. Whether the **pinned release** exposes a convenient
query/parser is an implementation check: use its supported interface or a
reviewed versioned support entry, never claim that the current C API is an
already available Python CLI. Shared-memory usable-capacity measurement and
pipeline target metadata extraction also require explicit implementation proof.

## Standalone advisory implementation

The bounded implementation is documented in [Hardware advisory](../hardware-advisory.md).
`qualify-cached-models.sh` reads a frozen prepared suite and optional reviewed
runtime/workload/hardware evidence, writes JSON/Markdown, and never filters or
changes model membership. The Linux DRM/KFD observer exposes raw device facts;
SDK normalization, target catalog and qualified shared-memory pool remain explicit
unknowns until supported by exact-version evidence. Fixture coverage does not
certify dogwood or any model's real peak. One coordinated measured profile remains
a separate validation checkpoint, with precise needs-information permitted when
interfaces are unavailable. Initial nightly behavior remains independent.
