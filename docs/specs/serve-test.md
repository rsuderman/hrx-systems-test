# hst-40e — one server, sequential model validation

## Outcome and ownership

Start one empty pinned-release `loom-serve`, hot-load/test/unload each prepared
model sequentially, and stop the server once at suite completion. A one-entry
suite is the first real validation checkpoint. This supersedes the earlier
per-model server process design. See [suite contract](cached-model-e2e.md).

This task owns the execution Bash wrapper and Python host/container orchestration,
one non-root GPU container, device/group access, cancellation cleanup and raw
execution results. Task .4 composes this command without duplicating its logic. One private in-container
client owns the native server process group and the sequential lifecycle. Model
inputs and caches are read-only; SDK/compilation/state/results are writable
per-attempt storage. Execution has no external networking, credentials, Docker
socket or published host port. Install the selected pinned SDK once in this
container's fresh `/run/venv`; compare its identity with prepared inputs.

## Public execution stage

`bash scripts/test/run-prepared-models.sh --prepared "$attempt/prepared"
--output "$attempt/results"` is the public Bash entry point, backed by Python.
Consume `prepared-suite.json` and staged payloads only; pull no image/pipeline/SDK
or assets. Require the prepared image locally and install SDK wheels offline.
Save self-contained raw `suite-result.json`, model evidence and logs after cleanup.
The separate report stage consumes those files; this stage does not render HTML,
Markdown or the final `report.json`. See [stage contract](pipeline-stages.md).

## Inputs and server start

Use the frozen suite list and successfully prepared entries from `hst-utq.3`.
Each `/inputs/<entry-id>/prepared-inputs.json` points to the local selected card
under `/inputs/<entry-id>/model` and carries source/asset/SDK identities, effective
workload, request and assertion. All entries share the suite SDK/image and device.
Record preparation failures rather than omitting them from suite coverage.

Start with no `--model` argument:

```sh
/run/venv/bin/loom-serve --device=amdgpu://0 --listen=127.0.0.1:18080
```

Wait for server `/readyz`, check empty `/v1/status` registrations and record the
owned server identity. Server readiness is not model readiness. Refuse an existing
listener and never attach to an unrelated server.

## Per-model loop

1. Register with `POST /v1/models/register` using a deployment document that
   references the local selected card. It has `name` equal to the expected served
   model ID, an absolute server-visible `base_directory` of
   `/inputs/<entry-id>/model`, and `model: {path: "model.json", preset: ...,
   programs: ...}` from the baseline, plus required `definition: {kind: "text"}`
   for the initial text service. The installed HTTP parser requires kind even for
   a card-reference deployment. Preserve published execution settings and
   record any explicit baseline overrides. HTTP 202 accepts intent only.
2. Poll `/v1/status` for that registration/incarnation: require registration
   `ready`, open admission and no model error, plus the expected prepared entry
   in `/v1/models`. Fail promptly on terminal preparation/server errors. Once
   identified, track the incarnation so an unrelated registration cannot satisfy
   readiness or removal. A single shared server exposes no external admin access.
3. Send the baseline's nonstreaming `/v1/chat/completions` request with that model
   ID. Capture the actual request/response and check the configured assertion.
   The first baseline asks two plus two with max_tokens8/temperature0 and expects
   stripped content `4`. No automatic assertion weakening or fixture substitution.
4. In a per-model finally path, `POST /v1/models/unregister` with
   `{model: <served-name>, mode: "drain"}` whenever registration may have been
   accepted, including failed/uncertain registration requests. Poll `/v1/status`
   until that owned registration is absent. Disappearance from `/v1/models` or
   HTTP 202 alone does not prove removal. On timeout, allow one bounded abort
   removal attempt and verify absence. Record forced removal. Drain or deactivate
   alone is insufficient: both can retain the registration/model resources.

Continue after a model-local failure only when removal is confirmed and the
server remains healthy. Unknown removal, `release_failed`, server failure or a
changed incarnation is a suite failure: stop the loop, mark later entries not
run and shut down the owned server/container. Do not restart between entries or
continue accumulating model registrations.

Use bounded HTTP calls and monotonic deadlines: initially180s server startup,
180s model readiness,60s query,30s graceful removal and10s abort removal, then10s
server shutdown before owned-process kill. These are wrapper limits. Account
for all expected entries and acquisition in the outer job timeout; the old fixed
15-minute budget is sufficient only when it exceeds the recorded suite budget.

## Results, shutdown and acceptance

Keep per-model `serve-result.json`, request/response and lifecycle observations
under that entry's results directory; retain one bounded shared `server.log`
and suite server-start/shutdown evidence. Record incarnation, failure stage,
readiness, assertion and confirmed removal. The execution wrapper owns raw aggregate
`suite-result.json`. Finish cleanup before finalizing its outcome. The separate
report stage generates final `report.json` and all presentation artifacts.
Always stop and join the owned server process group after the loop or failure;
the execution wrapper owns outer container cleanup for client crash/cancellation.

Pass requires a nonempty suite where every expected entry passes input
preparation, readiness, assertion and model removal, and final server cleanup
succeeds. Fixture coverage must include asynchronous registration/removal,
model-local failure followed by a successful next model, uncertain-registration
cleanup and removal/server failure stopping the loop. Actual proof must show two
load/query/unload cycles in the same server process (the one available model may
be registered again after confirmed removal), plus final process/container exit.
This validates lifecycle reuse before a second distinct model is cached.

## Verified source basis

Checked local HRX sources at release commit
`d01d1c18b154189fd073777329052d23f6071f14`:
`loom/docs/src/serving/workflows/serve-models.md` (empty startup, registration and
server-visible base_directory), `operate-model-server.md` (asynchronous removal,
incarnations and release failures), `model-documents.md` (card-reference deployment)
and `http-profiles.md` (routes). Installed-release HTTP registration of the
prepared published card remains an explicit implementation validation; SDK
help/card parsing does not prove serving. No new native load/unload API is needed.
