# Resolve stable and release-candidate HRX SDKs

The `release-latest` source selector means the upstream's latest published stable
release. It differs from `release`, which retains the configured release pin.
The latest selector does not change `config/runtime.json` or the local default.

`release-rc` selects the highest numeric published RC version newer than the
frozen stable release, such as `0.3.0rc10` after `0.3.0rc2`. Only non-draft
GitHub prereleases with `vX.Y.ZrcN` (or `X.Y.ZrcN`) tags qualify; beta, development
and superseded RC versions do not. Discovery reads complete pages of 100 releases,
up to 20 pages; exceeding that bound fails instead of claiming no candidate.
Duplicate normalized RC versions are an error. A selected RC with an invalid or
missing canonical wheel fails without falling back to an older RC.

The RC reference retains its stable eligibility baseline and the exact candidate
tag/commit, asset ID, version, size and digest. Normal `all` CI runs freeze stable
once and reuse it for RC eligibility. No active RC is explicitly skipped there;
an RC-only request fails clearly when none exists. API/authentication/discovery
failures always fail the RC lane. Existing source-qualified ABI checks also apply
to RC SDKs; selecting an RC does not automatically qualify its runtime contract.

The resolver reads `ROCm/hrx-system` release metadata, excludes drafts and
prereleases, and requires exactly one canonical Linux x86_64 Release SDK wheel.
Debug, sanitizer, tracing and other-platform variants are excluded. Missing,
ambiguous or incompatible assets fail that lane; no older release or Actions SDK
is substituted. Asset size and GitHub's SHA256 digest are required.

Release provenance records the release ID, tag, publication time, asset ID and
size/digest, stable SDK version, and tag commit. Both annotated and lightweight
tags resolve to their commit. A release's `target_commitish` can name `main` and
is not accepted as the released source identity.

## Metadata-only frozen selection

```sh
python3 -I python/run.py release-runtime freeze \
  --runtime-source release-latest --output /absolute/new/runtime-selection.json
python3 -I python/run.py release-runtime verify \
  --input /absolute/runtime-selection.json
# RC-only metadata selection:
python3 -I python/run.py release-runtime freeze \
  --runtime-source release-rc --output /absolute/new/rc-selection.json
```

`freeze` creates a new JSON reference without downloading a wheel, installing an
SDK, or reading model assets. It also accepts the existing `actions-main` and
`actions-replay` source controls. A release published later cannot change that
frozen reference. `verify` performs structural validation only; callers must bind
the reference's file hash into their own plan to detect changes to otherwise
valid recorded identities.

Preparation consumes the frozen reference with `--runtime-selection`; the helper
itself starts no model server or CI lane. Both selected channels reuse the same
verified release-asset downloader and per-attempt SDK staging.

## Integration API

Import `hrx_systems.sdk.releases` from the trusted package. Repository commands use `python3 -I python/run.py release-runtime`; the legacy script is a CLI alias:

- `freeze_selection(selector, *, client=None, now=None, observe_request=None, stable_source=None)`
  returns `{schema_version: 1, selector, source}`. It resolves metadata exactly
  once and supports the `release_latest` and `github_actions` selectors. The
  callback can retain bounded initial request provenance on resolution failure.
- `load_selection(path_or_dict)` returns a validated independent copy, with no
  API request or latest-alias resolution. It checks the source-selector pairing,
  release/tag/filename/asset/digest consistency, and recorded Actions identities.
  It validates structure and relationships, not authenticity by itself.
- `acquire_wheel(source, destination, *, dependency_lock, environment,
  client=None, qualifier=None)` accepts an already frozen **release** source and
  downloads only its exact asset ID. Use the existing Actions acquisition helper
  for a loaded Actions source.

The release source has `kind: release`, `selection_mode: latest-stable`,
`repository`, `requested_at`, `release_id`, `release` (tag), `published_at`,
`tested_sha` (commit), `tag_sha`, `asset_id`, `version`, and an `asset` object with
`id`, `name`, `size_bytes`, `upstream_digest` and a canonical public `url`.

Acquisition returns the same `source`, `wheel`, `metadata`,
`compatibility_status`, `staged_path` shape as Actions acquisition. It verifies
size and SHA256 before qualification, then validates wheel name/version/tags and
active requirements against the existing dependency lock. A qualifier callback
can perform those checks in the pinned target container; no host interpreter or
implicit pip dependency solve should substitute for target qualification.
The SDK wrapper remains responsible for acquiring locked dependencies once and
checking their transitive metadata before its ordinary offline installation.

The output directory must be new and have no symlink parents. Failed transfers
or qualification leave no published wheel. API credentials are sent only to
`api.github.com`; storage redirects receive no Authorization header. Saved
sources contain canonical URLs, never signed storage URLs or credentials.

Actual wheel qualification supplies the SDK version before migration/profile
selection. Metadata-only freezing is not an inference or SDK-installation proof.
