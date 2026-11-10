# hrx-systems-test

Design workspace for nightly validation of published Hugging Face Loom model
packages, with an initial standalone-runner bootstrap. Model-test implementation
remains outside the current scope.

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

The tests will use hosted models directly, without Loomify generation. Memory
planning must distinguish checkpoint storage from resident weights, KV/recurrent
state, workspace, companion components, runtime overhead and safety margins.
Runner eligibility must account for currently available VRAM as well as physical
capacity, with host RAM and shared-memory topology treated separately.
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
Weights, tokenizers and other reusable assets persist; the selected pipeline is
downloaded afresh into each nightly run and uses those cached assets.

Next design topics include memory estimation and qualification, required test
coverage, runner hardware, and the policy for models that cannot fit. Interfaces,
schemas and tooling remain proposals until the design is ready for implementation.
