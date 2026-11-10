# Model selection restrictions

These four checks guide later automatic model selection. The first end-to-end
epic assumes an operator-selected compatible host/model and does not require
this planner or a qualification system.

| Check | Machine information | Model information |
|---|---|---|
| Pipeline support | GPU target, backend and selected HRX runtime | An available pipeline/preset with a compatible target implementation |
| Available memory | Total/free dedicated VRAM, or the supported shared-memory budget | Recommended memory for weights, context, runtime overhead and headroom |
| Storage | Free space in model cache and run staging | Missing model assets plus temporary pipeline/SDK installation space |
| Runtime compatibility | Container platform and HRX build acquired for this CI run | Required model-card, model-format and pipeline support |

Generate a model list with **seed**, **skip**, or **needs information**, and a
short reason for each decision. An operator can separately name a download-only
probe for cache testing; check its access/storage and retain **needs information**
for unknown suitability. It is not an automatically eligible model. Use the
card/lock for selected files and sizes; keep memory recommendations alongside the model selection where the card does
not provide them. File size alone does not establish a memory requirement.

A model that fits the platform can be cached while its GPU is busy. Check live
free memory again before execution. Shared-memory machines need an appropriate
memory budget rather than adding host RAM to VRAM.

GPU target matching must allow compatible generic targets. For a `gfx1100`
device, the basic architecture comparison is:

| Pipeline target | Match |
|---|---|
| `gfx1100` | Exact match |
| `gfx11-generic` | Compatible generic target |
| `gfx1101` only | No match |
| `gfx12-generic` only | No match |

Detect the device's canonical target and use the selected HRX version's target
compatibility rules/catalog, not a string-prefix guess. Check targets and any
required features for the selected program implementation. A generic target's
compatibility does not make an implementation specialized for a different exact
GPU compatible. Record the device target and matched pipeline target in the
model list. Missing target information means **needs information**.

Use declared program/artifact target metadata, or a reviewed CI support entry
where the hosted card does not expose targets. Do not invent a new publisher
card field. HRX's target catalog maps `gfx1100` to `gfx11-generic`; AMD's
[generic processor documentation](https://rocm.docs.amd.com/projects/llvm-project/en/latest/LLVM/llvm/html/AMDGPUUsage.html#processors)
describes the corresponding generic-target compatibility.

Manual seeding does not certify future HRX builds. CI checks compatibility
using the actual SDK acquired for that run and records failures without silently
dropping expected models. Full execution compatibility is tested later.

See [the acquisition spike](acquisition-spike.md#hardware-specific-model-list-and-seeding)
for how the model list feeds seeding. These checks are planned, not implemented.
