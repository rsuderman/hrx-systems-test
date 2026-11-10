# hst-40e: one real serving test

Consume prepared inputs from `hst-utq.3`, run the selected native model, assert
one answer, and stop everything this attempt started. The operator-selected
host/model is assumed compatible; automatic hardware/memory qualification is
not a prerequisite. This is a proposed wrapper contract, not implemented code.

## Inputs and boundary

The producer supplies `/inputs/prepared-inputs.json` and the read-only model
tree at `/inputs/model/model.json`. The manifest carries model ID, pinned
publication/preset/asset identities, effective execution settings and SDK identity.
The selected request/assertion comes from `config/single-model-e2e.json`. The
workflow supplies attempt/source identity, explicit device URI and mounts.
Follow the [cross-resolved epic contract](basic-e2e.md) for shared fields/paths.

Task .4 owns one named non-root test container, assigned GPU device access,
required numeric supplementary groups and cancellation cleanup. This client
does not alter host groups or device permissions. Inside it, the client owns its server subprocess group.
Run the client and server on container loopback with external networking disabled;
no published host port, host network, Docker socket or model-source credentials.
Model/pipeline and cache mounts are read-only. This test container recreates
its own `/run/venv` from pinned read-only SDK/dependency payloads and checks its
`sdk.json` identity matches prepared inputs; no relocated/shared venv. Install
completes before the serving lifecycle; scratch/state/logs/results are separate
writable paths. Preserve this container's `sdk.json` in a stage-specific result
subdirectory; compare immutable SDK/image/dependency identity, not venv instances.

Private in-container helper boundary (owned here; public host wrapper is .4):

```sh
/run/venv/bin/python scripts/serve-model-test.py \
  --inputs /inputs/prepared-inputs.json --output /run/results \
  --device-uri amdgpu://0
```

This wrapper name and its flags are new testing-repository interfaces. It invokes
the pinned native binary, not a host or image-baked tool:

```sh
/run/venv/bin/loom-serve --device=amdgpu://0 \
  --model=/inputs/model/model.json --preset=chat \
  --listen=127.0.0.1:18080
```

The fixed port is private to this one owned container. Refuse an existing
listener/process rather than querying a server left by another attempt. Preserve
recorded workload settings; changes require an explicit operator baseline update.

## Lifecycle and result

1. Record input identities and a started result before launching the subprocess.
   Refuse missing prepared inputs without downloading or modifying model assets.
2. Capture stdout/stderr to bounded owned logs. Fail promptly if the process exits.
   Poll `GET /v1/models` until its `data` contains the expected model `id`;
   an open port, `/healthz` or `/readyz` alone does not establish model readiness.
3. Submit the baseline's nonstreaming `POST /v1/chat/completions` request with the
   exact model ID. Initial request asks two plus two, uses `max_tokens=8` and
   `temperature=0`; stripped `choices[0].message.content` must equal `4`.
   Record the actual request/response. Do not replace a failed model response
   with a fixture or weaken the assertion automatically.
4. Always terminate the owned server group, wait, then kill remaining owned
   descendants if necessary. The outer workflow/container owner handles a client
   crash or cancellation. Record forced termination when needed; cleanup passes
   only after all owned processes are confirmed stopped. Cleanup failure prevents
   a passing result.

Proposed baseline limits are 180 seconds for readiness, 60 seconds for the query
and 10 seconds for graceful shutdown before forced termination. They are wrapper
limits, not invented native SDK flags. Use bounded per-request timeouts and a
monotonic overall deadline. Any operator adjustment is pinned/recorded, not an
unbounded retry. The workflow budget must exceed these limits plus acquisition.

Cross-resolved stage files are `serve-result.json`, `request.json`, `response.json`
and bounded `server.log` under the results mount. Task 4 owns the host wrapper
and aggregate `report.json`; this client never overwrites that aggregate.
The stage report echoes attempt/source and prepared-input identities, states
that execution was real, and records readiness/request/assertion/cleanup status,
actual response evidence, elapsed times, process exit and log references. Distinguish
an attempted real query from a passing assertion. Overall success/exit 0 requires
expected-model readiness, valid response/assertion and confirmed cleanup; all other
outcomes exit nonzero with the failed stage/reason retained. Load/OOM, readiness
or query timeout, malformed response, wrong answer and cleanup failure cannot pass.
Task .4 aggregates these stage files under the agreed small-artifact allowlist;
artifact publication and its failure status remain workflow responsibilities.

## Acceptance and source basis

Small process/HTTP fixtures prove preparation-aware readiness, deadlines, wrong
answer/malformed response rejection and shutdown on every path. They do not close
this task. A real GPU run must then produce prepared-model, actual response and
owned-shutdown evidence with read-only model inputs. Text success is text smoke
coverage only. The later validation task repeats this exact baseline through
GitHub and checks warm asset reuse.

Native flags and HTTP distinctions were checked against HRX release source
`d01d1c18b154189fd073777329052d23f6071f14`,
`loom/docs/src/serving/index.md` and `http-profiles.md` (`--model`, `--preset`,
`--device`, `--listen`, prepared `/v1/models`). The SDK task must still verify the
actual installed released binary and pinned card pair before calling the baseline
known compatible. No model payload was downloaded or executed to write this spec.
