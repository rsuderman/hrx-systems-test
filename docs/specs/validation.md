# hst-utq.5 — Basic end-to-end and cache-reuse proof

Status: specification; proof has not run. Depends on manual seeding (`hst-utq.2`)
and the actual-model workflow (`hst-utq.4`, including `hst-40e`). The baseline is
one pinned pipeline/preset, known released SDK and base image on an
operator-selected compatible host. Automatic hardware qualification is not a gate.

## Inputs and evidence boundary

Use `config/single-model-e2e.json` and the exact authorized source commit. An effective
workload override must be explicit and identical across the comparison attempts.
Use an isolated, operator-owned model-cache namespace for the cold proof; never
clear a shared cache. Prepare the SDK/base before testing model-asset reuse.

Consume the seed receipt and prepared-input manifest from their owners, not a
second model inventory. Each selected asset needs its role, immutable origin and
revision, size/digest, cache-root kind/relative path and verification result.
The seed receipt also identifies the pipeline staging and SDK used. Keep each
container's `sdk.json` under a stage-specific artifact path; compare immutable
release/dependency/image identities, not a shared or relocated venv. The workflow
adds run/attempt, source/ref, runner identity and the assumed-compatible host.
The [parent contract](basic-e2e.md#cross-resolved-interfaces) fixes the filenames; outputs
are `workflow.json`, `plan.json`, `report.json`, `summary.md`, stage
`serve-result.json`, `request.json`/`response.json` and bounded logs. Include the
small `seed-manifest.json`/`seed-report.json`, `sdk.json` and
`prepared-inputs.json` records needed to verify the input chain; these are
receipts/manifests, not payload directories. Artifacts exclude weights, tokenizers and SDK payloads.

## Smallest convincing procedure

1. **Cold manual seed.** Invoke the actual `scripts/seed-models.sh` wrapper
   with the checked-in baseline, owned root and new cold output directory. Fetch
   the selected closure through `loom-fetch` into the empty owned namespace.
   Verify all assets, including tokenizer and declared companions, and retain
   `seed-report.json` and the published `seed-manifest.json`. Controlled native
   role-resolution fixtures establish selected-only acquisition; the real asset
   index must match the pinned selected closure.
2. **Warm manual seed.** Invoke that same wrapper again with a distinct output
   and `--asset-network offline`. It downloads the pinned pipeline online into
   new staging, then executes the **same actual seed asset-resolution and
   materialization path** in a network-disabled container. The option changes
   network policy, not the algorithm. Manual caches may remain writable for model-acquisition
   metadata/receipts. SDK/base inputs are prepared beforehand. Success against
   the same verified asset identities proves no model-asset network transfer;
   a separate CI read-only verification is not a substitute for this seed run.
   Retain pinned-source acquisition provenance for fresh pipeline bytes; a new
   pathname alone is insufficient. File mtimes and an invented `loom-fetch
   --json` counter are not proof. Verified sizes are not measured wire bytes;
   report counters only when backed by actual transfer events, otherwise mark
   them unobserved.
3. **Real GitHub attempt.** Manually dispatch the actual inference path against
   those seeded inputs. Its separate CI preparation phase must verify/assemble
   assets offline from read-only mounts and emit `prepared-inputs.json` before
   serving. Require the expected prepared model in `/v1/models`,
   the recorded bounded request, an actual response satisfying the explicit
   assertion, and owned server/container shutdown. This is not the synthetic
   fixture or an input-only check.
4. **Repeat GitHub attempt.** Use a distinct run/attempt and fresh pipeline
   staging, with unchanged baseline/workload pins and the same model assets.
   Require the same real readiness/assertion/cleanup success. Check the reported
   asset mount contract is read-only and the selected asset identities remain
   unchanged. Do not equate cache verification/hash reads with downloads.

## Required negative checks

Use small controlled fixtures for integrity/partial-file rejection and client
readiness timeout, wrong answer and cleanup failure. Their outcomes must be
failed with a preserved report, not `passed` or inferred model coverage. Exercise
a missing-asset case through prepared-input verification using an owned cache
view: report the exact missing identity for manual reseeding, leave the model
cache untouched and do not launch inference. No destructive mutation of real
shared weights or a runtime-version matrix is required for this epic.

## Closure criteria

A compact validation summary links the cold/warm receipts and both actual GitHub
run URLs/artifacts. It checks source commit, pipeline publication/preset/programs,
asset manifest, SDK payload/dependency identity, base-image digest, effective
workload and actual runner/device identity. Both real attempts must pass
readiness, assertion and cleanup; their distinct run IDs establish separate
attempts. Preserve the actual request/response and failure-stage/log evidence.
Workflow artifact publication must also succeed; an upload failure may leave
valid local stage evidence, but missing published evidence does not close this task. A fixture pass, successful parse/download, assumed host
compatibility or an SDK version string alone is insufficient.

No proof is executed during speccing. The implementation task stays open until
this exact-source real evidence exists. The shared artifact/layout and ownership contracts are cross-resolved with the
workflow, seed, input and serving owners; no separate proof-specific cache or
model-selection schema is introduced.
