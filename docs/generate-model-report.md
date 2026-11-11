# Generate the verification report

After execution, generate all final artifacts from its saved results:

```sh
bash scripts/generate-model-report.sh \
  --results "$attempt/results" --output "$attempt/report"
```

The output directory must be new and outside the saved results. Open `index.html`
in a browser, append `summary.md` to the GitHub job summary, and retain `report.json`
and the bundled evidence for inspection. No Docker, GPU, model cache, SDK, network
or GitHub environment is needed. Regenerate into another output directory without
running models again; saved execution files are never changed.

The version1 input is `suite-result.json` with frozen `expected_entries`, model
selection identities, per-model preparation/cycles/phases, source/runtime/hardware
objects and shared server start/shutdown/cleanup outcomes. Cycles reference compact
request/response JSON paths; model and suite `evidence` lists reference other
bounded JSON/log/text/Markdown files. All references are relative to the results
root, cannot be symlinks, and must name existing allowed files. See the
[stage contract](specs/pipeline-stages.md) and checked-in
[fixture](../tests/fixtures/model-report/passing/suite-result.json) for the exact
shape. Unknown identities/measurements remain unavailable; no successful phase is
inferred from absent data.

A valid report of a failing run exits0 for report generation and retains the
failed suite outcome. Invalid/missing required input/evidence or rendering failure
exits1. The workflow must preserve preparation/execution failures independently
of successful report generation.

Optional saved `workflow` metadata adds GitHub run and workflow-source links in
the HTML header and Markdown summary. Links are built from a validated repository,
positive run identity and full commit SHA; supplied run URLs must match that
GitHub repository/run (and optional attempt). GitHub summaries use that validated
run link for artifact access; local summaries retain relative links to bundled
files. The summary shows concise coverage, model results, runtime/GPU, source/run
and lifecycle sections. Full nested identities stay in downloadable JSON rather
than being flattened into the job summary. Local results work without metadata or with local SHA-only metadata.

The report displays expected test-entry counts, a separate distinct model-ID
count and repeated lifecycle cycles and marks fixture evidence clearly. Text smoke success does not establish
vision or untested preset support. Only a nonempty real suite with successful
assertions, model removal and final server cleanup receives the Verified banner.

Compact files are bounded to2MiB each and128MiB total, at most4096 referenced files.
Weights, tokenizer/model cards and SDK/code payloads are excluded. The renderer
copies only referenced evidence plus raw results; it never scans or recursively
exports a model/cache directory. HTML uses inline CSS/system fonts and no scripts,
remote assets or frontend build tools. GitHub uploads the report as a downloadable
bundle; Actions does not automatically display its HTML inline.
