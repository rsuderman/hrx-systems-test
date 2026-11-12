# Test an upstream HRX Actions SDK

The default runtime remains the pinned release. To exercise an available upstream
main build without rebuilding HRX, select Actions mode during preparation:

```sh
bash scripts/run-cached-models.sh --selection cached \
  --runtime-source actions-main --output /absolute/new/attempt
```

Use the same source options with `prepare-cached-models.sh` for independent stages.
The prepared run consumes staged, verified wheels offline; report generation reads
saved results only. This does not seed models, refresh their pinned publications,
create a persistent SDK cache or enable nightly scheduling.

The selector captures requested upstream main HEAD once, prefers a successful
Linux Release SDK artifact at that commit, and otherwise permits the newest
available successful ancestor build. Summary provenance identifies both SHAs and
build age. Artifacts must belong to the canonical main branch, an allowlisted
SDK-producing workflow and the recorded attempt. Missing/expired artifacts can
lead latest selection to an older candidate; authentication/API, checksum or
compatibility failures are reported, with no silent release fallback.

For exact replay, copy all identifiers and the wheel checksum from the prior
`resolved-runtime.json`:

```sh
bash scripts/run-cached-models.sh --selection cached \
  --runtime-source actions-replay \
  --runtime-run-id RUN_ID --runtime-run-attempt ATTEMPT \
  --runtime-artifact-id ARTIFACT_ID --runtime-wheel-sha256 WHEEL_SHA256 \
  --output /absolute/new/replay
```

Replay fails if its artifact expired or the run attempt cannot be established;
it never substitutes another build. The archive digest verifies the GitHub ZIP;
the wheel SHA256 identifies the exact SDK package inside. Package version alone
is insufficient to identify an Actions build.

## Authentication

Provide `HRX_ACTIONS_TOKEN` through your local launch environment, or a repository
secret of that name for the manual GitHub workflow. It needs Actions read access
to `ROCm/hrx-system`; a narrowly scoped fine-grained PAT or GitHub App credential
can supply it. Do not place token values in config files, command arguments or
logs. The workflow exposes the secret only to preparation. The caller repository's
ordinary `GITHUB_TOKEN` is not assumed to grant upstream artifact-download access.

The downloader authenticates only to `api.github.com`; the signed storage redirect
receives no API Authorization header. Signed URLs and token values are not saved
in runtime snapshots, results or published artifacts.

## Compatibility and failure evidence

Acquisition verifies bounded ZIP members, the upstream archive digest, exactly
one `hrx-loom` Linux wheel, wheel name/version/tags and the selected container's
Python/glibc. Active requirements, including the harness's Hugging Face capability,
are checked against the exact dependency lock. Unpinned requirements are never
resolved. The existing offline installation, `pip check` and native capability
probes remain the final dependency/runtime qualification; metadata-only success
is explicitly provisional.

Preparation runs wheel/tag/requirement qualification in the selected pinned image,
using its bootstrap pip packaging support. The host needs Python's standard library
and Docker; it does not implicitly install host packages. These short offline
qualification commands neither install SDKs nor receive the Actions credential.
The artifact wheel is transferred once, then the same payload is used with the
locked dependencies for ordinary offline SDK installation.

If requirements differ, retain the exact incompatibility and review the dependency
lock. Do not relax hashes or switch to an older SDK automatically. Model failures
are still valid regression outcomes, and a new SDK is not assumed to repair an
older pipeline. Raw source/run/artifact/wheel identities and acquisition failures
remain available in the ordinary report/failure-evidence path.
