# hrx-systems-test

Design workspace for nightly validation of published Hugging Face Loom model
packages, with an initial standalone-runner bootstrap. Model-test implementation
remains in design; a single-model stage skeleton now supports fixture validation.

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

- [Single-model E2E skeleton](docs/single-model-e2e.md): one pinned published
  selection, runnable fixture stage checks, manual GitHub hookup and explicit
  prerequisites for real execution.
- [Per-Bead specifications](docs/specs/basic-e2e.md#specification-index):
  cross-resolved task boundaries, interfaces and acceptance checks.
- [First end-to-end epic](docs/acquisition-spike.md): manual seeding and cache
  reuse, fresh pipeline/HRX inputs, and one real GitHub inference test on an
  operator-selected compatible host.
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
An operator manually seeds weights, tokenizers and companion assets; CI consumes
that cache read-only and reports misses for manual reseeding. Each run downloads
a fresh pipeline and installs the exact selected HRX SDK in a new venv inside a
pinned base container. The first acquisition spike permits an explicit
download-only probe without claiming GPU suitability or model execution.

Next design topics include memory estimation and qualification, required test
coverage, runner hardware, and the policy for models that cannot fit. Interfaces,
schemas and tooling remain proposals until the design is ready for implementation.
