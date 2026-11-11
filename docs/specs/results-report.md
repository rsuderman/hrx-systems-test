# Model-support verification report

## Independent report stage

Run `bash scripts/generate-model-report.sh --results "$attempt/results"
--output "$attempt/report"` after execution. It consumes saved `suite-result.json`
and referenced raw evidence, then creates every final artifact below. No rerun,
Docker/GPU/cache access or acquisition is needed. The results directory is
self-contained; regenerate reports into a fresh destination without modifying it.
See [stage ownership and commands](pipeline-stages.md).

## Outcome and deliverables

Make successful model support easy to inspect and share for the exact tested
HRX release, pipeline publication, hardware and workload. `hst-utq.6` owns a small
report renderer using the suite's saved raw results; `hst-utq.5` verifies the
rendered evidence agrees with real execution. This is part of the current E2E
checkpoint, not a separate dashboard service.

Each local run and GitHub artifact bundle contains:

- `index.html`: polished, self-contained report with inline CSS and no external
  fonts/scripts/CDNs or build toolchain. Open the downloaded artifact locally.
- `summary.md`: compact GitHub job summary using the same normalized results.
- `report.json`: versioned machine-readable suite result and evidence references.
- `models/<entry-id>/`: bounded request/response and preparation/lifecycle results
  referenced by relative links, plus shared server/SDK/workflow evidence.

GitHub receives the Markdown summary and a downloadable report bundle. Do not
assume Actions renders uploaded HTML inline. A hosted report site or historical
coverage dashboard is later work; no publication step is needed for this version.

## Visual layout

Use a clean responsive layout with readable system typography, generous spacing,
subtle borders and clearly labelled status chips. Success uses green, failures
red and not-run/incomplete neutral/amber; each includes text/icon labels so color
is not the sole signal. Tables remain readable on narrow screens.

1. **Run header.** A clear overall Verified / Failed / Incomplete banner, timestamp,
   duration and run/source links. An incomplete cleanup, startup or report failure
   must remain visible even if earlier model queries passed.
2. **Coverage tiles.** Expected test entries, fully passed entries, failed entries
   and not-run entries. Show the denominator and a separate distinct model-ID
   count, deduplicated by known nonempty `selection.model_id`. Different presets
   or workload entries for one model remain separate rows, not extra model IDs. List repeated
   load/test/unload cycles separately; repeating one model twice is one model of
   coverage, not two. Coverage is against the frozen suite, not all HF models or
   every quantization/modality of an architecture.
3. **Test environment strip.** HRX release, harness source, GPU/device identity,
   suite identity and shared-server lifecycle. Show a concise statement that
   execution used read-only cached assets and freshly staged pinned pipelines,
   derived from execution evidence rather than hard-coded success text.
4. **Model support matrix.** One row per expected suite entry with model/preset,
   pipeline publication, input verification, load/readiness, query/assertion,
   unload and final outcome. Each phase is Passed / Failed / Not run / Incomplete;
   never leave an unattempted phase visually green. Surface the failed stage and
   short reason immediately. Link model/release/publication identities to their
   exact source where available.
5. **Expandable model details.** Actual prompt, expected assertion, returned text,
   phase durations, lifecycle cycles/incarnation, effective workload and selected
   asset identities. Include links to local JSON evidence and bounded logs. Show
   observed timings as timings, not benchmark claims. Missing measurements display
   unavailable rather than zero. Collapse longer evidence with native details
   elements so successful rows stay easy to scan.
6. **Shared-server footer.** Server startup, sequential register/test/unregister
   timeline and final shutdown. Show removal completion for every attempted model
   and that repeated cycles used the same server identity. Put any cleanup failure
   prominently in the header as well as the detailed lifecycle section.

## Honest success semantics

A verified model selection requires its exact prepared inputs, expected-model
readiness, actual bounded response assertion and confirmed model removal. Overall
Verified additionally requires a nonempty all-passing expected suite and final
server/container cleanup. A successful query alone is not Verified. If final
cleanup fails, retain earlier phase evidence but mark the run Failed and avoid an
unqualified support claim. Observed lifecycle failure/incomplete evidence cannot
be omitted to improve the coverage count.

A text smoke test establishes that tested text path only: a cached projector or
successful text answer does not establish vision support. Include the test kind
and preset/variant in both summary and details. Fixture results are visibly marked
Fixture and kept distinct from real model-support results.

## Data and generation boundary

Read saved versioned `suite-result.json` and its compact evidence after model
removal and final shutdown results are known. Generate final `report.json`, HTML,
Markdown and the bundled evidence tree from that same normalized input. The
runner saves raw results; this independent stage owns all presentation outputs. Normalize every expected suite entry
into a result even when preparation/server startup fails; absent phase evidence
is incomplete/not run, not success. Preserve per-cycle results, count entries
separately from distinct known model IDs, and never infer model identity from
an opaque entry ID. Use wall-clock start/end and monotonic elapsed times.
Keep source/runtime/pipeline/asset identities with their phase evidence.

After owned cleanup, always invoke the separate report command for
failure/cancellation when the wrapper or GitHub step retains control. Preserve raw JSON/logs if rendering
fails, mark report generation failed and fail the job. Rendering a valid failed
run exits0 for report generation; the earlier run failure still fails the job. A lost host may leave only
incomplete evidence; never manufacture a successful final report.

Render publisher/model names and returned text as escaped text. Links to bundled
files are relative and restricted to the compact evidence tree. Do not bundle
weights/tokenizers/SDK payloads, credentials or the staged model trees. Match the
explicit artifact allowlist to this report layout; keep logs/text bounded.

## Acceptance

Small rendering fixtures cover an all-passing real-shaped run, mixed failure,
not-run entries, repeated cycles of one model and failed final cleanup. Check
HTML/Markdown/JSON entry/model-ID counts and outcomes agree, all evidence links resolve, and
model/response text cannot inject markup. Visually inspect the generated HTML at
wide and narrow widths and confirm it works without network access. Actual
GitHub proof must upload a usable bundle and show an accurate job summary even
when a controlled model failure occurs. Rendering fixtures alone do not establish
real model support.
