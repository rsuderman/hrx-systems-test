# Run a prepared model suite

Use preparation output from this machine, with its existing seeded model cache:

```sh
bash scripts/run-prepared-models.sh \
  --prepared "$attempt/prepared" --output "$attempt/results"
```

This command downloads nothing. It requires the prepared pinned Docker image
already present locally, the staged SDK wheels, readable prepared/cache inputs,
and access to the selected GPU. It installs the SDK offline in a fresh isolated
container venv, starts one empty server and sequentially registers, tests and
unregisters models before final shutdown. Model/input mounts are read-only and
the serving container has external networking disabled. It does not use a host
SDK, normal user daemon or a previously running inference server.

The current backend is AMD: `--device-uri amdgpu://0` is the default. Device access
uses `/dev/kfd`, the selected render node and their numeric groups; missing access
is an actionable failure, not an attempt to change host permissions. The operator
must configure runner-account cache access and seed any missing assets separately.

Every expected entry remains in `suite-result.json`, including preparation
failures. Each ready entry receives one load/query/unload cycle. `--cycles 2`
repeats that same lifecycle in one server for validation; repetitions do not add
distinct model coverage. Assertions use the exact prepared baseline request and
expected text. A failed model may be followed by another only after confirmed
removal, explicit idle-pool reclamation and a healthy server. Uncertain cleanup
stops the suite.

After each confirmed unregistration, the harness requires an empty registration
inventory, discovers the server's published serving-pool keys and requests
`/v1/pools/trim` with `target_bytes: 0` for each. It waits for retained pending/
releasing bytes and quarantined workspace bytes to become quiescent before the
next model case. This isolates model cases using the supported serving-pool
controls while keeping the same server and compiled-code caches. It does not
reset devices or claim to fix an underlying HRX issue. Committed/reserved bytes
and cumulative counters need not reach zero.

Unregistration and reclamation share the existing unload-plus-abort deadline;
there is no additional unbounded wait. Missing/unsupported pool accounting,
trim errors or timeout fail the unload/cleanup phase and prevent the next model
from registering. Bounded before/trim/after accounting is linked from each cycle's
`unload.evidence` as `pool-reclamation.json`, including failed reclamation when
evidence could be captured.

The output directory must be new. It contains raw `suite-result.json`, bounded
shared server/container logs, offline SDK identity and per-model preparation,
request, response and lifecycle evidence. No rendering occurs here. The raw
results remain interpretable after prepared staging is cleaned up; use the
separate `generate-model-report.sh` command to generate all presentation artifacts.
Suite elapsed time covers host preflight, offline SDK setup, model execution and
final container cleanup. `lifecycle_elapsed_seconds` separately records the
server/client lifecycle duration; per-phase timings retain their own scopes.

Initial deadline defaults are startup180s, readiness180s, query60s, removal30s,
abort removal10s and server shutdown10s. Each has a corresponding
`--startup-timeout`, `--readiness-timeout`, `--query-timeout`, `--unload-timeout`,
`--abort-timeout` or `--shutdown-timeout` option. An outer finite container budget
also accounts for SDK setup and all expected cycles. Outcomes and elapsed times
are retained on failure when process control remains available.

An optional `--workflow FILE` accepts bounded GitHub identity metadata only;
local runs require none. Source credentials are never passed to execution.

For cancellation recovery, the execution-owned cleanup hook is:

```sh
bash scripts/run-prepared-models.sh --cleanup --output "$attempt/results"
```

It derives the owned container identity from the absolute output path, checks a
matching Docker ownership label and removes that exact container ID. It never
trusts a runtime-written container name in the raw JSON. Cleanup is bounded and
returns nonzero when the daemon or removal outcome cannot be confirmed. Normal
execution uses the same cleanup implementation before temporary scratch removal.

Exit0 requires a nonempty all-passing suite, confirmed model removal and final
server/container cleanup. Preparation, load, response, timeout or cleanup errors
exit nonzero and preserve evidence; generating a report later does not change
that execution result.
