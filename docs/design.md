# Hosted model nightly CI design

Status: **Draft for discussion**. This repository is a workspace for developing
the model-test design. The separate [runner bootstrap](runner-bootstrap.md)
provides host setup and a manual storage smoke check; it does not implement the
nightly model tests. Update this document as requirements and decisions resolve.

The system acquires a known set of published Hugging Face model packages, retains
GGUFs and other assets locally between runs, executes nightly serving checks, and
uploads results that show model and feature coverage. A scheduled GitHub Actions
workflow triggers the nightly, which executes on GitHub Actions runners. The
initial proposal uses one dedicated hardware runner; later versions add a head
node coordinating workers on different hardware.

## Confirmed direction

- Test the published Hugging Face model packages directly. No Loomify generation
  or model-conversion pipeline is part of this system.
- Exercise all models in the agreed catalog and make missing coverage visible.
- Use model cards to identify the selected programs, checkpoint assets, tokenizer
  and service/preset configuration.
- Cache large assets locally across runs; acquisition should support a prefetch
  phase instead of redownloading the corpus each night.
- Run nightly validation and upload results for coverage tracking.
- Use GitHub Actions for the nightly trigger and runner execution.
- Run acquisition and model-test workloads in Docker containers, with explicit
  GPU access, storage mounts and a defined containment boundary.
- Use a separate caching/prefetch run with writable persistent storage, followed
  by test containers with read-only access to verified cached inputs. Limit
  acquisition to the assets required by the planned model/preset selections.
- Persist reusable assets such as weights and tokenizers across runs. Download
  the selected pipeline afresh for each nightly into that run's staging area;
  do not reuse a previous run's installed pipeline as the candidate under test.
- Maintain a model-size index and account for runners that cannot fit every model
  in device memory.
- Identify the GPU assigned to the worker and compare its live available VRAM
  against Loom Serve memory recommendations for the selected model and workload,
  including runtime overhead and context allocations.
- Preserve a path to a head node that fans work out to hardware workers running
  the service.

The design lives in the local `hrx-systems-test` repository. GitHub Actions is
settled; a dedicated self-hosted hardware runner is proposed for v1. Runner
hardware, nightly time, upload destination and coverage policy remain open. A
persistent runner would allow local checkpoint reuse; an ephemeral runner would
need separately provisioned persistent storage.

## Intended outcome

Every nightly run produces an identifiable result for every expected model/test
job, tied to the exact publication, runtime, test profile and hardware used.
Unchanged checkpoint files are reused from persistent storage. One failing model
can be diagnosed without suppressing results from the rest of the catalog.

A result upload is not itself proof of model correctness. The report distinguishes
expected, acquired, attempted and passing coverage, and identifies missing or
incomplete work.

## Initial system shape

```mermaid
flowchart LR
    GitHub[GitHub Actions nightly trigger] --> Plan
    Catalog[Known model catalog] --> Plan[Nightly run snapshot]
    Sizes[Model size and memory index] --> Plan
    Capacity[Runner capabilities] --> Plan
    Plan --> Prefetch[Prefetch]
    Prefetch --> Cache[(Persistent local model cache)]
    Plan --> Worker[Local model test worker]
    Cache --> Worker
    Worker --> Results[Local result bundle]
    Results --> Publish[Upload and coverage report]
```

GitHub Actions triggers one nightly coordinator invocation. The coordinator
resolves the run, checks runner eligibility, acquires inputs, calls the local
worker and publishes results. A continuously running head-node service is not
required for v1.

## GitHub Actions execution

The CI repository owns a scheduled workflow using the GitHub Actions `schedule`
event. An additional manual `workflow_dispatch` trigger is proposed for reruns,
prefetching and investigating failures. Exact clock time/timezone remain open;
the schedule should be explicit and recorded with each run.

Scheduled workflows run from the default branch and may be delayed or dropped
under load. Reports should retain both the intended nightly date and actual
execution time so a missing or delayed nightly is distinguishable from a model
failure. See [GitHub schedule behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

Runner labels/groups provide coarse placement by OS, accelerator family and
memory class. They are routing hints, not live VRAM measurements: the assigned
runner must confirm actual device capacity/availability before testing. GitHub
documents [label and group routing](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/use-in-a-workflow).

The proposed hardware job serializes accelerator use. Overlapping scheduled or
manual runs must not both assume exclusive use of the same device. Cleanup and
result collection should still run after test failures. Exact job layout and
concurrency policy will be chosen before implementation; no workflow YAML is
created by this design.

| Responsibility | Owned behavior |
|---|---|
| Model catalog | The repositories/presets and checks we promise to exercise, with hardware requirements and explicit exclusions |
| Size and memory index | Track acquisition sizes separately from workload-specific memory estimates and observations |
| Run planning | Freeze publication/runtime/test identities, evaluate runner eligibility and record assigned/excluded jobs |
| Acquisition | Fetch and verify the selected model-card closure through `loom-fetch`; retain persistent caches |
| Worker | Manage the service lifecycle, run checks and return a result bundle |
| Results publication | Upload evidence, aggregate coverage and preserve history |

These are logical responsibilities, not a requirement to introduce separate
services, packages or frameworks immediately.

## Container execution and containment

The nightly workload will run in Docker on a Linux GPU runner. GitHub Actions
supports [job containers](https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/run-jobs-in-a-container);
[self-hosted container execution](https://docs.github.com/en/actions/reference/runners/self-hosted-runners)
requires Linux and Docker. The GitHub runner and Docker control plane remain
trusted host infrastructure. A containerized workload is distinct from running
the runner agent itself inside Docker.

Docker provides a useful execution boundary, but does not guarantee that code
cannot escape to the host: workloads share the host kernel, and GPU access also
exposes host driver interfaces. The security goal is to constrain model/runtime
execution to assigned devices, mounted inputs and run output, without granting
host administration. See [Docker's security model](https://docs.docker.com/engine/security/).
Use a dedicated runner for trusted nightly inputs. If future requirements include
hostile executable inputs, evaluate a stronger VM or disposable-host boundary.

The proposed execution contract is:

- Pin the runtime/test image by digest; record that digest alongside the host
  driver and runtime identities. Host GPU drivers remain a provisioned dependency.
- Expose the assigned GPU and required driver interfaces only. Verify physical
  device identity inside the container; an environment visibility setting alone
  must not be treated as enforced device isolation. Backend-specific device
  permissions and telemetry access still need validation.
- Use a non-root workload user where supported, minimal capabilities and normal
  isolation profiles. Avoid privileged mode, host PID/network namespaces, broad
  host mounts and access to the Docker socket from model execution.
- Keep checkpoint caches on persistent host storage. Prefetch needs write access
  to its designated cache; execution should receive immutable acquired inputs
  read-only, with separate writable scratch/state and results directories. Mount
  the selected asset closure so all shared-file references resolve inside the
  container. Shared caches remain an integrity boundary, not disposable scratch.
- Limit CPU, host RAM, processes and scratch usage explicitly. Docker's host-RAM
  limit does not establish a GPU VRAM budget; retain the separate live-VRAM
  admission check. Host-memory admission must also respect container limits.
- Give prefetch only the credentials/network access needed to acquire assets.
  Keep GitHub publication credentials outside model execution. Prefer network
  isolation during inference, with readiness/query traffic inside the test
  boundary; trusted orchestration uploads the resulting artifacts afterward.
- Collect partial results and remove owned containers after completion or failure,
  while retaining persistent cache assets. Per-model containers are a proposed
  way to isolate failures; container granularity remains open.

Choose between native GitHub job containers and a trusted orchestration step
launching dedicated workload containers after reviewing their actual mounts,
credentials and GPU support. The selected approach must enforce the boundary
above, including keeping Docker control access out of model execution; default
job-container settings are not assumed sufficient. Image construction, exact
container options and backend-specific exceptions remain design work. No
Dockerfile or workflow is introduced by this decision.

## Known model catalog

Start from the existing inventory of 60 loom-fun pipeline repositories. The exact
initial membership should be confirmed; the inventory is a seed rather than an
automatic promise to test every future repository published by the organization.

Each catalog entry identifies the published repository, selected preset, test
profile, intended hardware coverage and any required service/component behavior.
Adding or removing a model changes the versioned catalog explicitly.

The published card owns file acquisition and compatible program/service/variant
selection. The CI project owns the assertions and coverage expectations. Expected
answers must not be inferred from the candidate model's own output.

One selected quantization/preset per model is a reasonable initial matrix. Testing
all published quantizations, presets or hardware combinations would be a separate
expansion of that matrix.

## Model size and runtime memory index

Maintain an index that supports both disk planning and runner selection. A
checkpoint's download size is not its VRAM requirement. Prepared weights, KV or
recurrent state, scratch/workspace, companion components and runtime overhead all
contribute to memory usage; quantized loading or preparation can change the
relationship between file bytes and resident bytes.

The [draft size index](../catalog/model-size-index.csv) seeds all 60 current models with
reported GGUF file sizes and pinned publication/preset identities. It is a
design/reference artifact, not an implemented scheduler configuration. All runtime
memory fields are explicitly unknown. Fifty-two entries retain a publisher
`info.json` memory hint with provenance and an unspecified memory domain; these
are not measured VRAM requirements or evidence that a model fits.

| Indexed information | Purpose |
|---|---|
| Model publication revision, variant, service and preset | Identify the exact selected package |
| Base, vision, draft and total GGUF bytes | Budget acquisition/storage using actual file sizes |
| Publisher memory hint and source | Preserve advisory information without promoting it to a measured requirement |
| Workload profile | Associate memory with context, prefill/batch, resident rows, enabled components and draft settings |
| Estimated device and host memory, with method/confidence | Support provisional fit decisions while identifying assumptions |
| Measured peak device and host memory, with measurement scope | Record observed readiness/inference usage for an identified workload |
| Runtime identity, accelerator/backend and memory topology | Prevent one runtime or device's result from qualifying a different configuration |
| Qualification state | Distinguish unknown, estimated and measured/qualified profiles |

The logical memory entry is keyed by model selection, workload profile, runtime
and hardware target. One model can need multiple entries. The initial CSV is a
per-selection seed with no qualified hardware/runtime rows; the design does not
yet choose a final machine-readable schema.

Refresh acquisition sizes when the resolved publication/asset lock changes and
freeze the chosen index revision into the run. Upload the index with the coverage
results so exclusions can be explained later. Per-model totals can include files
shared with other models; aggregate physical-cache estimates must deduplicate
file identities rather than simply sum every row.

For a discrete accelerator, the proposed estimate includes resident parameters,
mutable state, scratch/workspace, runtime allocations and an explicit margin.
Host RAM and free disk are checked separately. Unified/shared memory needs a
topology-aware budget: do not add reported GPU and host capacity when they refer
to the same physical pool. Multiple devices do not automatically form one usable
memory pool. Offload or multi-device execution must be a distinct supported
profile rather than an implicit fallback that changes the test.

Measurements apply only to the workload actually exercised. Updating the card,
runtime, target, memory layout or execution settings requires re-evaluating the
entry. Discovery/qualification runs will fill unknown entries; the policy for
trying unqualified estimates is still open.

### GPU identity and recommended serving memory

Before admitting a test, identify the physical GPU assigned to the worker, its
backend/architecture, total usable VRAM and currently free VRAM. Record a stable
device identifier and verify that Loom Serve will use that same device. A runner
label or a GPU's advertised memory capacity is insufficient. If multiple devices
are visible, select the assigned device explicitly rather than pooling their
memory. Device ownership and serialization must prevent other test workers from
concurrently spending the same available memory budget.

The recommendation belongs to a model/runtime/workload/hardware profile, not
just a model name. The profile should include context capacity, concurrent
requests or resident rows, prefill/batch limits, cache precision, enabled
companion components and any supported offload settings. Budget the configured
allocation limits even if the smoke-test prompt is short. GPU/backend support
is a separate compatibility check: enough VRAM alone does not establish support.

For a worker starting a fresh Loom Serve process, the proposed budget is:

```text
recommended serving VRAM = peak additional device memory across preparation
                          and the configured inference workload
                          + model/workload safety margin

admissible when: recommended serving VRAM + runner reserve <= live free VRAM
```

Explain that peak using resident weights, context/KV or recurrent state,
execution workspace, enabled companions and Loom Serve runtime allocations.
Account for preparation-time temporary allocations as well as inference, with
each allocation counted once. Where only component estimates exist, use a
conservative combined estimate and record its uncertainty. A fixed percentage
of GGUF file size is not an established recommendation. The runner reserve is
separate host/device headroom and must not duplicate the profile's safety margin.

Recommendations should record whether they are estimated or measured, their
source/method, runtime and driver identities, workload limits, hardware target
and observation date. Publisher guidance with an unspecified memory domain
remains advisory until it can be mapped to this profile. The current inventory
has no qualified Loom Serve VRAM recommendations; deriving or measuring them is
still design/qualification work.

Use a documented memory baseline. For a fresh process, sample free VRAM before
starting Loom Serve and budget its complete additional peak footprint. If a
future worker reuses an already-running server, sample the matching baseline
and budget incremental allocations, avoiding charging existing allocations
twice. The baseline and comparison must be recorded with the admission result.

Check capacity during planning and refresh availability immediately before
loading. Report the GPU identity, required bytes, available bytes, headroom,
profile provenance and decision. Unavailable telemetry or an unknown memory
profile must produce an explicit unresolved eligibility result. A fit decision
is not a memory reservation; retain observed peaks and any OOM as evidence for
refining the recommendation. Exact telemetry tools and recommendation storage
remain implementation choices to settle later.

## Runner eligibility and partial coverage

Each runner reports accelerator/backend identity, device count, memory topology,
total device memory, host RAM and local cache/disk capacity. Its configured test
budget reserves explicit headroom. Free memory is checked immediately before
execution as well as during planning; a temporarily busy device is different
from a permanently undersized device.

| Eligibility | Behavior |
|---|---|
| Qualified fit | Schedule the matching workload on the runner, subject to current availability |
| Estimated fit | Identify the unqualified estimate and apply the chosen discovery/admission policy |
| Known not to fit | Do not launch on this runner; record required/available memory and a capacity exclusion |
| Fits total capacity but not current free VRAM | Defer or report temporary unavailability under an explicit wait/retry policy |
| Memory requirement unknown | Route to an explicit qualification attempt or report unknown capacity; do not claim fit from GGUF size |
| Backend or topology incompatible | Record a hardware compatibility exclusion independently of memory |

Not every runner is expected to execute every catalog model. A small runner may
succeed for its assigned eligible subset while total catalog coverage remains
partial. A capacity exclusion is not a model correctness failure or a passing
model result. Aggregation distinguishes models covered on another eligible
runner from models with no suitable runner anywhere.

An admitted attempt that runs out of memory is a recorded resource failure.
Keep the original attempt and fit assumptions; do not silently relabel it as a
planned exclusion after the fact. The observation can refine the memory index.
Whether a catalog-wide capacity gap fails the overall GitHub workflow or produces
a partial-coverage warning remains an explicit policy decision.

## Nightly run phases

| Phase | Behavior | Recorded outcome |
|---|---|---|
| Resolve | Resolve immutable publications/runtime/test profiles, consult the size index and confirm runner capacity | Frozen inputs, expected jobs, assignments and explicit eligibility exclusions |
| Prefetch | Acquire missing files and verify the complete selected closure, using persistent caches | Available inputs or a model-specific acquisition failure |
| Execute | Start the service, wait for actual model readiness, run bounded checks and shut down owned processes | Test outcomes, diagnostics and timings |
| Collect | Assemble model, runtime, hardware and test provenance with responses/logs | A durable local result bundle |
| Publish | Upload results and produce an aggregate coverage summary | Published evidence or a retained pending upload |

Resolve inputs once for the run. Publication changes during acquisition or testing
must not mix revisions within that run. Whether resolution follows the latest
published revision nightly or uses a reviewed pinned set remains a policy decision.

Prefetch is a separate caching run that can warm a host before the nightly.
The nightly completes or verifies that acquisition phase before execution so
large transfers and hashing do not unnecessarily interfere with test timing.
A failed acquisition produces a visible outcome for that model; remaining models
with complete inputs can still run.

Prefetch coverage and execution eligibility are separate. A storage host can
cache the whole catalog even when its accelerator cannot execute every model.
By default, each hardware runner's caching run acquires the planned selections
assigned to that worker. A whole-catalog warmup is a distinct explicit scope;
it does not require every cached model to fit that worker's GPU. Temporary GPU
occupancy need not prevent caching an assigned model. The acquisition report
must identify the requested scope and which selections are complete.

## Persistent cache and storage

Use `loom-fetch` and the HF SDK's existing cache ownership instead of implementing
another GGUF downloader or weight store.

| Storage | Lifetime | Contents |
|---|---|---|
| HF Hub cache | Persistent across runs | HF payload blobs, repository snapshots and SDK-managed partial transfers |
| Loom acquisition state | Asset cache persistent; pipeline acquisition state per run | Reusable non-HF assets and asset receipts; fresh pipeline documents/programs staged separately |
| Selected pipeline installation | Per nightly run, immutable during testing | Freshly downloaded card/lock, programs and references to reusable cached assets |
| Run directories | Per nightly run/attempt | Isolated runtime state, responses, diagnostics and result bundles |

`HF_HUB_CACHE` controls the HF cache. `--cache` or `LOOM_CACHE_HOME` controls Loom
acquisition state. Persistent payload storage must remain outside disposable run
directories. The acquisition configuration must keep fresh pipeline downloads
separate from reusable asset storage; the exact SDK/cache arrangement is an
integration check, not a settled command-line recipe.

The persistent-cache policy applies to assets: model weights, tokenizers and
other declared reusable model inputs. The pipeline candidate consists of its
model card/lock, control and other `.loom` files, executable kernels/programs and
pipeline configuration. Download that candidate for each nightly from its
resolved publication revision into a new run directory, even when its assets
are already cached. Freeze its revision before resolving required assets. This
does not decide whether the nightly follows the latest publication or a reviewed
pin; it ensures the selected pipeline is acquired anew in either case.

Default `--materialize=auto` keeps programs independently editable while sharing
weights and tokenizers where linking is supported. Copy fallback must be accounted
for in disk budgeting. Shared asset files are immutable inputs.

The complete selected closure includes the card/lock, programs, tokenizer, all
checkpoint shards and declared companion assets. Having the main GGUF alone does
not establish that an offline installation is complete.

The measured current corpus is approximately **734 GB** for the selected base,
vision and draft GGUFs. Approximately **1 TB free for the cache** is a starting
budget for one selected corpus, before retained additional asset revisions,
installation copies, build/runtime files or result retention. Disk capacity does
not establish that the host can load every model into memory.

Cache retention should preserve inputs referenced by active runs and retained
baselines. The retention period, storage quota and eviction policy remain open.
Deleting one run's generated state should not delete shared cached checkpoints.
Checkpoint payloads remain in persistent local storage; result uploads contain
their identities/index entries and test evidence, not copies of the weights.

### Persistent cache across container runs

The chosen storage boundary is a persistent model store on the GPU host,
mounted into disposable containers. Model weights are not baked into the image
or retained only in a container's writable layer. Use a dedicated host directory
or managed persistent volume; choose the exact mechanism during infrastructure
design. Docker supports [read-only bind mounts](https://docs.docker.com/engine/storage/bind-mounts/#use-a-read-only-bind-mount).

| Phase | Persistent model-store access | Other writable storage |
|---|---|---|
| Acquisition/prefetch | Read/write to designated reusable-asset caches | Fresh pipeline staging, acquisition scratch and logs |
| Test | Read-only access to referenced cached assets | Per-attempt runtime state, scratch and results; staged pipeline mounted read-only |
| Maintenance | Controlled access for quota management and eviction | Maintenance records |

Acquisition downloads the selected pipeline into fresh run staging. Prefetch
uses that pipeline's frozen card/lock selection, acquires missing reusable assets
and verifies the complete closure before marking the run's installation ready.
Reuse verified assets across nights and container-image updates, while refreshing
the pipeline candidate for each nightly. Interrupted
downloads or staging directories are not runnable installations. Define a
publication/locking protocol around staging and readiness, while retaining the
SDK's own download/cache coordination rather than inventing a new blob store.

The acquisition unit is the complete asset set selected by a pinned model card
and preset, not a clone of every file in its repositories. Include the selected
programs into per-run staging, and tokenizer/model assets, all required GGUF
shards, and vision, draft or other companion assets into reusable storage when
that selection requires them. Do not acquire unused
quantizations, alternate presets, unrelated repository files or historical
revisions merely because they are published alongside the selected assets.
Retained assets from previous runs may remain under the retention policy.

Resolve the selection using `loom-fetch` and its card/lock semantics; do not
approximate it with a filename glob for GGUF files. Download missing required
assets and reuse already verified cached assets, including shared companions.
Do not let an existing pipeline installation or SDK document/program cache
silently substitute for the fresh pipeline download required for this run.
The caching result identifies the pinned selection, required assets and whether
the complete set is ready. Correct selection filtering for the chosen
`loom-fetch` version remains an integration acceptance check.

Use a consistent container-visible model-store layout in both phases. Materialized
cards, HF snapshot symlinks and shared-file references must resolve inside test
containers; mounting only a directory containing a GGUF symlink can leave its
target inaccessible. Avoid embedding host-specific paths. Validate the mounted
closure without network access before inference. Missing files should produce
an acquisition failure rather than a hidden download from the test container.
Whether Loom's installed-card layout can be used unchanged with read-only inputs
is an explicit integration check; any required writes belong in per-run state.

Read-only mounts prevent writes through the test container, but do not freeze
files that another host process can change. Keep published installations and
their referenced blobs immutable while in use, record active references, and
prevent concurrent prefetch or eviction from replacing/removing them. New
nightly runs get distinct pipeline installations; cleanup of one run must not
remove shared assets. Configure compatible UID/GID ownership so non-root
prefetch and test users can share inputs without broad host permissions.

This favors persistent storage on a dedicated self-hosted worker for v1. An
ephemeral worker would need a separately attached persistent store or explicit
rehydration; the design does not assume a GitHub cache upload/restore step for
the roughly 734 GB corpus. Weights are excluded from result artifacts. Track
physical disk usage and deduplicate shared assets, reserve space for updates and
partial downloads, and evict only unreferenced revisions under an explicit quota.
Cache loss affects acquisition time, not the frozen identity of the intended test.

## Worker and test execution

For v1, execute one model at a time on the accelerator. Prefetch may have bounded
parallelism. Model attempts have isolated state and owned service processes;
cleanup must not stop unrelated services.

The worker receives a frozen job description and produces a result bundle. It
owns preparation, model readiness, test requests, deadlines, diagnostics and
shutdown. This same responsibility will remain on hardware workers later.

The proposed initial pass criterion is:

1. The selected model package can be loaded by the selected runtime.
2. The model becomes ready within its deadline, beyond merely opening an HTTP port.
3. A bounded service-appropriate inference check satisfies explicit assertions.
4. Owned service processes shut down, with cleanup failures reported.

This criterion is a proposal, not an agreed test specification. Numerical-reference
comparisons and performance thresholds remain open. Initial performance data can
be informational rather than gating.

Test profiles should state inputs, expected assertions, required services or
components, execution settings and time/token bounds. Deterministic settings are
preferred where supported, but assertions must account for the actual service and
model behavior.

A successful text check establishes text coverage. Image, tool, reasoning,
decision, draft and other capabilities require their own checks when included in
the coverage contract. Capability names must come from the selected card and
verified test profile rather than being guessed from the repository's model name.

## Results and coverage

A nightly job is identified by its catalog model/preset, publication snapshot,
service/test profile, runtime artifact, hardware target and attempt. The run also
records the catalog/test revision and the complete expected job set.

A result bundle should contain:

- Outcome and failure stage/reason.
- Publication/card/lock identities and selected asset provenance.
- Runtime artifact/version and test-profile identity.
- Host/hardware, relevant driver/runtime configuration and workload settings.
- Size-index evidence, eligibility decision, device budget, available memory
  and observed peak memory where measurable.
- Readiness/inference timings, assertions, bounded responses and service logs.
- Cleanup outcome and whether the attempt completed.

The initial outcome vocabulary can distinguish passed, failed, blocked and not
run. Acquisition failure, insufficient hardware, timeout and service failure must
remain explicit rather than silently disappearing from the denominator. Whether
a case is required or intentionally excluded is declared in the catalog/profile.

Report catalog, per-runner and feature coverage separately. Capacity exclusions
must retain the model/workload and reason, rather than reducing the catalog's
denominator invisibly. Also distinguish a fully
published run containing test failures from a successful validation run. No required
missing model/check should produce a green all-model result.

Compare results only within meaningful hardware/runtime/workload identities.
A changed node or workload must not silently inherit an unrelated performance
baseline. Exact regression and baseline-update policies remain open.

## Upload and retention

Publish machine-readable results, a readable coverage summary and diagnostic
artifacts. Storage may be the CI provider's artifact system or a separate artifact
store; the destination and authentication mechanism are not selected yet.

Use immutable run identities so uploaded evidence can be referenced historically.
Results from failed or partially executed runs are useful and should still be
uploaded with an explicit completeness status.

If publication fails, preserve the local bundle and record a pending upload for
retry. Model-test success and upload success are separate outcomes. Retention
policies can differ for summaries, full logs and large diagnostic artifacts.

## Failure and restart behavior

One model failure should not terminate the remainder of the catalog unnecessarily.
Each job has a bounded deadline; cleanup results remain attached to the attempt.
An accelerator or host that cannot safely continue should leave remaining jobs
explicitly unexecuted rather than producing misleading results.

A restarted nightly should retain the original frozen run inputs and previous
attempt evidence, or start a distinct identified run. Acquisition can reuse
completed cache entries. Retry policy should distinguish transient infrastructure
failures from correctness failures, with all attempts visible.

Exact restart/resume semantics and retry limits remain design questions; no queue
or distributed recovery implementation is selected by this draft.

## Future head node and hardware workers

The head node will own planning, scheduling, aggregation and publication. Hardware
workers will own their local caches, acquisition, service execution, tests and
result bundles.

The same frozen job description and result bundle used locally are the proposed
boundary for remote execution. Scheduling transport can change without moving
model-specific test logic into the head node.

Workers advertise hardware/runtime capabilities and available capacity. The head
node assigns compatible jobs and reports required jobs that have no eligible
worker, using the same size/memory index and eligibility rules established for v1.
GitHub Actions can remain the nightly trigger while this coordinator is introduced.
The desired model-by-hardware matrix is explicit; adding a worker does
not automatically require every model to run on every device.

Workers prefetch their assigned closures locally. The head node does not need to
stream hundreds of gigabytes of checkpoint data for each job. A shared storage
layer could be evaluated later if deployment topology makes it useful.

Worker health, assignment ownership, timeouts and repeated-attempt handling will
need refinement for distribution. They do not require deploying a head node in v1.

## Open design decisions

| Concern | Current proposal or unresolved choice |
|---|---|
| Catalog scope | Confirm the initial 60 entries and selected presets/quantizations |
| Publication updates | Nightly latest-published resolution versus reviewed pinned revisions |
| Runtime under test | Select a release, candidate artifact or another explicit runtime policy |
| Pass criteria | Proposed load/readiness/bounded inference/shutdown; determine stronger correctness requirements |
| Feature coverage | Decide which components and behaviors are required initially |
| GitHub workflow | Local repository is `hrx-systems-test`; choose nightly time/timezone, manual inputs and runner labels/groups |
| Initial runners | Choose hosting/hardware and reserved device/host-memory budgets |
| Memory qualification | Establish measurement scope, estimation margin and admission of unknown/estimated profiles |
| GPU admission | Choose telemetry and device-binding verification, establish recommended Loom Serve VRAM profiles, and define the wait policy for temporarily busy devices |
| Container boundary | Docker is required; choose job versus workload containers, per-run/per-model granularity, image provisioning, mounts, GPU permissions and network policy |
| Capacity gaps | Decide whether no eligible runner for a catalog model fails the workflow or reports partial coverage |
| Nightly budget | Establish the execution window, per-model limits and acceptable incomplete-run behavior |
| Upload destination | CI artifacts versus a separate store; choose access and result presentation |
| Retention | Cache versions, baselines, logs, diagnostics and disk quota |
| Retries and restart | Define attempt limits and run-resume behavior |
| Distributed scheduling | Refine worker assignment and health semantics when additional hardware is introduced |

## Prior evidence and references

The initial catalog and design were transferred from the hrx-solutions external
workspace investigations on 2026-11-10. The catalog records each selected
publication commit and preset, with byte counts for its declared GGUF assets.
Publisher hints came from `info.json#/card/memory_bytes`; their memory domain is
unspecified. Neither the hints nor GGUF byte counts qualify execution memory.
The source investigations remain outside this repository; their reports are not
runtime dependencies. The transferred [model-size index](../catalog/model-size-index.csv)
is the local planning input. Refreshing publications or qualifying new workloads
requires reviewing both catalog identity and memory/test profiles.
