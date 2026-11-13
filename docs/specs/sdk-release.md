# hst-utq.1 — pinned SDK environment

Provide the known released Loom tools for manual seed, CI preparation and serving.
This validates the harness against fixed inputs; it does not follow HRX development
builds or prove model execution. See [shared contracts](basic-e2e.md).

Implementation: [private SDK helper and usage](../sdk-environment.md). The baseline
pins the validated base image and dependency lock; metadata acceptance does not
imply successful GPU execution.

## Inputs and proposed boundary

Extend the single `config/runtime.json` baseline with base-image digest,
Python executable, SDK
release repository/tag/asset ID/name/size/SHA256, and a hash-locked acquisition
dependency list. Initial SDK candidate is `ROCm/hrx-system@v0.1.0`, source
`d01d1c18b154189fd073777329052d23f6071f14`, asset `687300101`:
`hrx_loom-0.1.0-py3-none-manylinux_2_39_x86_64.whl`, 22,272,294 bytes,
SHA256 `4d3897b83cc0857723593487a4372a31d203944a1637e5e3143cad1d0583b98b`.
The installed release passes native tool, model-card and lock checks. Model serving
remains a separate validation stage.

A private SDK helper has two operations: trusted `acquire`
downloads and verifies release/dependency payloads in the current attempt's
`sdk-payloads/` directory; in-container `install` creates `/run/venv` from those
read-only payload mounts and emits `sdk.json` in that container's stage-specific results
directory. The public host entry points remain `scripts/cache/seed-models.sh` and
`scripts/run-single-model.sh`; they invoke this helper. These are contracts to
implement, not existing SDK commands. No model cache is touched.

The selected base must provide Python >=3.12, Linux x86-64 and a compatible
manylinux_2_39 userspace/native dependency closure. A bookworm/glibc-2.36 base is
not sufficient for this wheel. Pin an actual tested image digest during
implementation; a compatible host OS does not substitute for the container ABI.
The release declares optional `hf = huggingface_hub>=2.2,<3`; resolve that extra
and all transitive wheels to exact versions/hashes before offline installation.

## Installation and handoff

- Download the selected release/dependency payloads into new per-attempt storage.
  Verify configured size/digest; do not maintain a persistent SDK cache.
- Create a fresh venv with system/user site packages and inherited Python path
  overrides disabled. Install the hash-locked dependencies offline, then the
  verified SDK wheel without implicit dependency resolution or source builds.
- Recreate this environment inside **each** seed/preparation/serving container
  from that attempt's verified payloads. Do not relocate/share a venv between containers
  or rely on a host/image-baked SDK. Invoke `/run/venv/bin/python`,
  `/run/venv/bin/loom-fetch` and `/run/venv/bin/loom-serve` explicitly.
- Proposed `sdk.json` records schema version, baseline ID, base-image digest,
  release/source/asset identity, observed wheel digest, dependency-lock digest,
  Python/package versions, imported package location and explicit tool paths.
  Paths are valid in that bootstrapped container, not host-executable handoffs.
  Keep each stage's receipt separately; compare immutable image/release/dependency
  identity across containers, not installation instances. Preparation embeds or
  references that identity in `prepared-inputs.json`.
- Fail with a stage-specific configuration/download/integrity/install/capability
  error. Preserve diagnostics, never credentials. No latest/CI/source fallback.

## Verified native surfaces and acceptance

Tagged v0.1.0 sources expose `ModelCard.parse/select`, role-bearing
`Selection.files`, `plan_fetch`/`FetchPlan.sources`/`identities`, and
`ContentCache(root, huggingface=..., progress=...)`. Its `HuggingFace` client
accepts `cache_dir`; lock parsing supports v1/v2 and scope `assets`/`all`.
These Python seams are valid only against this pin until integration-tested.
They are not a built-in role router or read-only installed-card verifier:
`install()` still acquires/publishes files. Seed/preparation own those adapters.

Acceptance: separate attempts each download and verify their SDK payloads;
altered bytes fail before install; two fresh containers report the same artifact/dependency
identity while importing solely from their own venv. Run native help and
`loom-fetch /run/pipeline/model.json --list --json --no-input --offline
--cache=/run/acquisition` with a writable transient document-cache path; use the installed Python APIs to parse
the actual pinned card, select `chat`/`source`, and parse its lock. `--list` does
not check the lock or execute the model. No model payloads/GPU access are needed for
these checks. Missing native dependencies or baseline incompatibility is a
reported baseline-selection gap requiring an explicit pin update, not fallback.

Sources: [release and checksums](https://github.com/ROCm/hrx-system/releases/tag/v0.1.0),
[tagged packaging](https://github.com/ROCm/hrx-system/blob/v0.1.0/loom/packaging/python/defs.bzl),
[tagged fetch/storage](https://github.com/ROCm/hrx-system/tree/v0.1.0/loom/py/loom/model).

SDK payloads and venvs are disposable per-run inputs. Cleanup removes them after
the run; only compact identity/error receipts and logs are retained.
