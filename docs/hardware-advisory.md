# Hardware advisory

`qualify-cached-models.sh` explains qualification evidence for each frozen prepared
selection. It does not select models, seed assets, start inference, change the
cache, or alter test outcomes. Continue running the normal suite when advisory
metadata is unknown.

```sh
bash scripts/qualify-cached-models.sh \
  --prepared "$attempt/prepared" \
  --output "$attempt/hardware-advisory" \
  --device-uri amdgpu://0
```

The new output directory contains `hardware-selection-report.json` and a concise
`summary.md`. Inputs are read-only. This command uses the Python standard library;
no installed SDK, network, container or GPU benchmark is needed.

Every prepared selection remains in the report, including failed preparation and
missing profiles. `seed` means the four suitability checks pass, not an instruction
to download again. `skip` means a demonstrated incompatibility or capacity/storage
failure. `needs_information` means one or more checks are unknown. `runnable_now`
is independent: a qualified total-capacity fit can be temporarily busy (`false`),
ready (`true`), or have no qualified current free-memory observation (`null`).
Malformed inputs fail the command; model suitability does not change its exit code.

## What the default observer establishes

The AMD Linux observer reads the selected sorted DRM render node's `uevent`, four
bounded VRAM/GTT counters, and up to 64 KFD topology node property files. It records
PCI/driver/kernel identity and joins a nonzero KFD GPU to the exact render minor,
retaining `gfx_target_version` as a raw kernel number. Linux's
[KFD topology implementation](https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/amd/amdkfd/kfd_topology.c)
exports these fields. It does not establish that this node ordering matches the
selected SDK's `amdgpu://INDEX` enumeration. The command does not convert the raw
version to an HRX target name or infer exact/generic support from its prefix.

Without a selected-SDK mapping/catalog adapter and a qualified usable-memory pool,
the default report explicitly says those facts are missing. Neither GGUF bytes,
publisher hints nor raw VRAM/GTT/host-memory sums become memory requirements.
There is no separate host-RAM eligibility test.

## Reviewed evidence contracts

No production hardware/model certification ships with this command. The minimal
profile document is valid and intentionally leaves requirements unknown:

```json
{"schema_version": 1, "profiles": []}
```

Pass `--profiles FILE` for reviewed records. Each profile is an object with these
fields; the exact publication identity and workload must match:

| Field | Required meaning |
|---|---|
| `selection` | Exact six-field repository/publication_revision/model_id/preset/variant/programs object from the prepared entry |
| `runtime_identity` | Exact SHA256 from the advisory report; uses `runtime.resolved_runtime.runtime_identity_sha256` when present, otherwise the semantic legacy SDK/image/dependency/wheel identity |
| `workload_identity` | Exact advisory SHA256 over effective execution, smoke request, test kind and `--cycles` (default 2) |
| `hardware` | Exact canonical_target, driver, driver_version and memory_topology object from qualified observation |
| `provenance` | Nonempty method, reference, reviewer and ISO observation/date strings |
| `programs` | One record per manifest program path: either `target_independent:true` with reviewed rationale or `targets:[{"target":"...","features":[]}]` alternatives |
| `memory` | Reviewed peak_bytes, margin_bytes, component accounting and provenance |
| `runtime_compatibility` | Reviewed `status:pass` or `fail`, with provenance tied to the exact SDK/publication/workload |

Memory accounting names `weights`, `context_state`, `companions` and
`workspace_runtime` in bytes, including explicit zeros. Component sum cannot
exceed the reviewed peak. `peak_bytes + margin_bytes` is compared with usable
capacity. `--runner-reserve-bytes` (default 0; operator must qualify a suitable
reserve) is separate and applied once. Profile provenance should explain how peak,
components and margin were measured or conservatively bounded. Changing runtime,
workload or hardware invalidates the match rather than reusing stale certification.

Pass `--observation FILE` only for a reviewed saved observation from the exact
selected SDK. Its schema version is1. Required fields are runtime_identity,
device_uri, pci_slot, driver, driver_version, mapping_qualified:true,
canonical_target, memory_topology (`dedicated`, `shared` or `unknown`) and provenance.
The device/PCI/driver/kernel binding must agree with the current Linux observation.
This is an evidence import, not a claim that a native SDK query CLI already exists.

A `target_catalog` object names that runtime_identity and canonical_target,
provenance, `compatible_targets:[{"target":"...","features":[]}]`, and whether
its coverage is `complete:true`. The program passes when one of its declared
alternatives is explicitly supported, with all required features. Lack of a match
proves incompatibility only for a complete catalog; partial catalog coverage stays
unknown. Every manifest program needs reviewed target coverage. No other
variant/program can supply missing support for this immutable selection.

A qualified memory observation additionally supplies memory_qualified:true,
usable_total_bytes, usable_free_bytes and memory_provenance with a GPU-usable pool
method/reference. Free bytes must be within total. Free-memory evidence older than
five minutes or future-dated is discarded for readiness; known total capacity can
still establish fit. Shared-memory accounting must identify the supported pool and
allocation limit, rather than add counters. Unknown pools remain unknown.

Cached storage closure uses the digest-checked prepared-input manifest and ready
preparation snapshot; it does not rescan or hash weights. This is snapshot evidence,
not a second live cache verifier. Output filesystem free bytes must cover
`--output-headroom-bytes` (default 8 MiB for the small report). No pipeline/weight
staging is performed by this command. Planning unseeded models is a later extension.

## Completing real qualification

The initial concrete gaps are the selected SDK's device enumeration/canonical
identity/catalog query and a supported shared-pool/peak measurement on dogwood.
The command reports these gaps instead of invoking a different installed SDK.
A coordinated proof may reuse one already-prepared small workload and its existing
SDK/server to review one profile. Successful allocation alone is not measured fit;
retain `needs_information` until pool, peak and margin evidence are established.
No nightly or model-inventory change depends on that qualification.

A bounded ZIP-source inspection of the selected `hrx-loom 0.2.0.dev0` wheel from
upstream commit `619e759f2c3f412c6489a52339f3ca016d87b934` found compiler-side
`loom/target/arch/amdgpu/target_catalog.py` and `target_identity.py` with structured
exact/generic/physical target data and parsing. Their module contract explicitly
keeps compiler and runtime-driver catalogs separate. These compiler APIs must not
be relabelled runtime support. A bounded Python-source scan found no runtime
GPU enumeration/usable-pool API; the wheel's native `loom-serve` executable was
not executed/disassembled during this author checkpoint. A native selected-SDK
query may close that concrete interface gap during coordinated validation.
