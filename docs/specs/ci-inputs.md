# CI inputs — hst-utq.3

Prepare the selected baseline for `hst-40e` without writing model caches. This
implements the input stage of [the basic E2E contract](basic-e2e.md), not serving
or automatic hardware qualification. The known host/model pair is an input.

## Boundary and inputs

Task .4's public `scripts/run-single-model.sh` invokes a private .3 preparation
helper with the single `config/single-model-e2e.json` baseline, seed index, SDK
context and new attempt directory. Helper spelling/flags are stage-owned, not
an additional public command or a new `loom-fetch` flag. Dependencies are
`hst-utq.1` and `hst-utq.2`. The seed index defaults to
`$HRX_SYSTEMS_ROOT/cache/seed-manifest.json`; the public wrapper may accept an
explicit `--seed-manifest` for isolated validation. CI receives that index read-only.

- The SDK producer supplies `sdk.json`: pinned base-image/release/wheel/dependency
  identities and verified explicit `/run/venv` tool paths for the current
  bootstrapped container. Each container recreates its own venv from that attempt's downloaded pinned
  payloads; a venv is not relocated or shared between containers. Preserve each
  container's `sdk.json` under its stage-specific artifact directory and compare
  immutable image/release/dependency identities, not the physical venv instance.
- The manual seed producer supplies `seed-manifest.json`: selection identity and
  reusable `files[]` with `installation_path`, native `roles`, immutable source
  revision/origin, size/SHA256, `cache_root` (`huggingface` or `loom-assets`),
  relative `cache_path` and verification state. This wrapper manifest is not an
  assumed SDK receipt format. Programs/auxiliary files are fresh code inputs;
  code role wins when roles overlap.
- The single baseline config supplies the exact repository/publication/preset/variant/program set,
  model ID, effective execution settings and bounded request/assertion. Workflow
  owns the selected device URI, container lifecycle and mount permissions.

## Preparation

1. In online acquisition staging, parse the pinned card/lock and select its file
   closure using the installed SDK. Download card, lock and selected
   program/auxiliary bytes afresh from pinned sources into this attempt, bypassing
   persistent program payload caches. Record source identities and content hashes.
   Do not download tokenizer/parameters/companions through this fresh-code path.
2. In a separate **network-disabled** container with model caches read-only,
   match every selected asset against the seed manifest and lock, verify its
   actual bytes, and assemble the selected model tree. Resolve references through
   native selection/relocation semantics; use links into consistently mounted
   cache roots rather than copying weights into a new store. Missing assets,
   digest disagreement or escaping/dangling links fail; no repair/download path.
3. Verify that the selected local card and every required file can be consumed
   through the final mount layout. Publish the handoff manifest only after all
   checks succeed. Retain stage failure details for the caller's report.

Use one container-visible layout throughout: cache roots at
`/model-cache/huggingface` and `/model-cache/loom-assets`, selected model tree at
`/inputs/model` and manifest at `/inputs/prepared-inputs.json`. The serving
consumer receives these read-only with separate writable state/results. The
operator does not reseed/evict referenced assets during the baseline run.

## Handoff to serving

Proposed `prepared-inputs.json` contains `schema_version`, `selection`, `model_id`,
`model_card_path` (`/inputs/model/model.json`), card/lock/program hashes, verified
asset identities/paths, immutable image/release/dependency identity from `sdk.json`,
`effective_execution`,
`smoke_request`, `smoke_expected_text`, and `asset_phase_network: "disabled"`.
A successful manifest means inputs are ready, not that inference passed. The
serving container bootstraps the same pinned SDK independently and verifies its
immutable identities match this handoff before loading. Its explicit device URI
remains a workflow input. Filenames/mounts follow the cross-resolved epic contract.
Serving owns `serve-result.json`; this helper does not write the workflow-owned
aggregate `report.json` or represent ready inputs as a completed real test.

## Acceptance and implementation seam

Tiny fixtures demonstrate fresh code acquisition on two attempts; warmed assets
resolve successfully offline; corrupt/missing assets fail with their identity;
cache and input writes fail while scratch writes succeed; and all links resolve
inside the verification/serving mounts. Native selection/schema failures retain
the selected SDK/pipeline pair. Successful offline/read-only preparation proves
CI can consume the seeded inputs without asset transfer or writes. It does not
prove the manual seeder reuses assets: that requires a second actual seeder
invocation exercising its same resolution/materialization path offline, where
manual cache metadata may be writable. CI model mounts remain read-only.
Verified file size is not a claim about measured network bytes.

Source basis: native `ModelCard.select`/`Selection.files.roles`, lock coverage and
`plan_fetch` facts in `loom/py/loom/model/{manifest,fetch,lock}.py`;
`fetch.install()` acquires/publishes and `--materialize=auto` does not force a
fresh download. Pinned-release API availability is owned by `hst-utq.1`. The
role-aware fresh-code/read-only-asset adapter and faithful selected-card
relocation must be proved against that exact release with fixtures; they are
not existing CLI guarantees. Do not call native acquisition with a writable
model cache and present it as read-only verification.
