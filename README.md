# hrx-systems-test

Tools and design for validating published Hugging Face Loom model packages.
Model assets are manually seeded and reused read-only; hosted pipelines and the
pinned HRX inputs are prepared per attempt. Local commands and GitHub use the same
stages, with one server hot-loading/unloading models. GitHub tests the latest
available successful upstream main SDK on pushes to this repository's `main` and
daily at 4 a.m. America/Los_Angeles. Manual controls retain release/sample replay;
see [workflow scheduling](docs/specs/later-nightly.md).

## Run locally

To download the 45 publications with selected GGUF assets under 20 GB each,
see [bulk cache downloads](docs/download-models.md). This uses the existing
native seeder and does not expand the regression suite.
For a shared cache, pass `--reader-user` with the dedicated runner account so
new and reseeded inventory manifests remain readable by CI.

To test every hosted selection seeded on this runner, use
`bash scripts/run-cached-models.sh --selection cached` with the configured cache
root. See [runner-cache inventory selection](docs/cached-inventory.md) for
discovery, deduplication, test profiles and incomplete-coverage handling.

The default sample contains eight model IDs across seven architecture labels,
from Qwen2.5 0.5B through GPT-OSS 20B. Its pinned selected assets total about
18.30 GB; see [the model sample and rationale](docs/model-sample.md). The
[suite seeder](docs/seed-cached-models.md) caches all registered entries through
the existing native acquisition system; `seed-models.sh` remains available for
one model. Use `--reader-user ACCOUNT` when published seed indices need read-only
access from a separate runner account.

Docker access, the selected GPU and a seeded compatible model are required.
`HRX_SYSTEMS_ROOT` selects the external model-cache root (default
`$HOME/.local/share/hrx-systems-test`). Keep generated output in a writable location,
which may differ from a cache owned by another account.

```sh
bash scripts/seed-cached-models.sh

attempt="$HOME/.local/share/hrx-systems-test/runs/manual-$(date -u +%Y%m%dT%H%M%S)-$$"
bash scripts/prepare-cached-models.sh --output "$attempt/prepared"
bash scripts/run-prepared-models.sh --prepared "$attempt/prepared" --output "$attempt/results"
bash scripts/generate-model-report.sh --results "$attempt/results" --output "$attempt/report"
```

Open `$attempt/report/index.html`. Report generation can run again from saved
results without Docker, GPU or cache access. For the same stages in one command,
use `bash scripts/run-cached-models.sh --output "$attempt"`; choose a fresh attempt
directory rather than reusing one from the separate-stage example.

- [Prepare inputs](docs/prepare-cached-models.md)
- [Run the prepared suite](docs/run-prepared-models.md)
- [Generate verification artifacts](docs/generate-model-report.md)
- [Local/GitHub composition](docs/run-cached-models.md)

## Design and supporting tools

- [Working design](docs/design.md): nightly GitHub Actions execution, model-card
  prefetching and persistent caches, runner eligibility, test lifecycle, result
  publication, and future coordination across hardware workers.
- [Model-size inventory](catalog/model-size-index.csv): 60 pinned publications,
  selected presets and GGUF sizes. Runtime-memory requirements remain unknown;
  publisher hints are advisory and do not establish VRAM requirements.
- [Runner bootstrap](docs/runner-bootstrap.md): prepare a local Linux machine,
  register its GitHub agent, and check read-only container input mounts locally
  or through a manually triggered workflow.
- [Runner pickup test](.github/workflows/runner-pickup.yml): manually dispatch a
  small job to the registered runner and receive execution evidence, before
  testing Docker or GPUs.

- [Legacy synthetic stage skeleton](docs/single-model-e2e.md): fixture-only
  orchestration checks; the staged commands above perform real execution.
- [Per-Bead specifications](docs/specs/basic-e2e.md#responsibilities):
  current task boundaries, data contracts and acceptance checks.
- [First end-to-end epic](docs/acquisition-spike.md): manual seeding and cache
  reuse, host-seeded pipelines and fresh HRX inputs, and a real GitHub inference
  test on an operator-selected compatible host.
- [Seed one model](docs/seed-models.md): populate the model cache with one command,
  using the checked-in model selection and release pins.
- [Hardware-specific seed list](docs/acquisition-spike.md#hardware-specific-model-list-and-seeding):
  inspect GPU memory and pipeline availability before selecting model assets to cache.
- [Platform/model restrictions](docs/platform-restrictions.md): four checks for choosing which
  models to cache on a machine.

The tests will use hosted models directly, without Loomify generation. Memory
planning must distinguish checkpoint storage from resident weights, KV/recurrent
state, workspace, companion components, runtime overhead and safety margins.
Runner eligibility must account for available memory and total capacity using
the GPU's dedicated or shared-memory topology, plus compatible pipeline targets.
The [GPU admission design](docs/design.md#gpu-identity-and-recommended-serving-memory)
describes identifying the assigned device and comparing live free VRAM with
workload-specific Loom Serve recommendations, including runtime and context
allocations.

Workloads will run in Docker on a Linux GPU runner. The
[container design](docs/design.md#container-execution-and-containment) outlines
GPU access, persistent-cache mounts, resource limits and the boundary between
trusted orchestration and model execution.
The [persistent-cache design](docs/design.md#persistent-cache-across-container-runs)
separates writable acquisition from read-only test inputs so containers can be
discarded while GGUF files remain available between nights.
An operator manually seeds weights, tokenizers and companion assets, then runs
[`seed-pipelines.sh`](docs/seed-pipelines.md) for the exact pinned pipeline inputs.
CI consumes both caches read-only and reports misses for manual reseeding. Each
run stages independent verified pipeline copies and installs the exact selected
HRX SDK in a new venv inside a pinned base container. The first acquisition spike
permits an explicit download-only probe without claiming GPU suitability or model execution.

Automatic memory qualification and distributed hardware execution remain later
work. The current suite assumes
operator-selected compatible models/device and reports actual failures explicitly.
