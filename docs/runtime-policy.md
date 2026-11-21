# Freeze runtime-specific test policy

Preparation uses `python/hrx_systems/preparation/runtime_policy.py` after the selected SDK wheel identity
and metadata have been verified. It returns a policy bound to that exact runtime.
Execution validates the frozen policy and copied seccomp bytes; it does not reread
mutable repository configuration or choose new request/server settings.

The public preparation command owns policy acquisition; this helper performs pure
resolution and validates the frozen policy without fetching new inputs.

| Resolved SDK | Source upgrade | Request/server/container settings |
| --- | --- | --- |
| 0.1.0, release or Actions | Skipped | Existing defaults and requests preserved |
| 0.2.0, 0.2.1 or source-qualified 0.3.0, release or Actions | Versioned migrations selected by frozen publication and runner ownership ABI evidence | Versioned policy below |
| Unsupported family in automatic mode | Explicit preparation failure | No guessed fallback |
| Unsupported family with explicit raw `none` | Skipped diagnostic run | Existing defaults preserved |

For a known 0.2.0 runtime, explicit `pipeline_upgrade=none` bypasses only the
source transformation. It keeps the selected runtime request/server/container
settings, so the override is not an implicit change of other test conditions.
The supported versions include prerelease/development and local-version forms.
SDK 0.2.1 and source-qualified 0.3.0 reuse the 0.2.0 policy family and settings;
the exact SDK version remains recorded separately. Automatic 0.3.0 qualification
requires recognized runner ownership evidence from the tested SDK source commit.
Publication and ownership ABI selection use frozen native source contracts, not
the package version alone. Other patch versions and families are not automatically
enabled. Historical schema-1 and schema-2 policies retain their recorded behavior,
including an unsupported-family diagnostic for an older 0.3.0 raw policy.

## Request profiles

`config/runtime-policies/0.2.0/policy.json` retains one exact six-field selection
identity for the inaccessible legacy OpenBMB MiniCPM V 4.7 publication. Updated
Qwen3 14B and Bartowski MiniCPM V selections carry their existing exact requests
in model test profiles; their superseded runtime rules have been removed.
Matching 0.2.0 selections receive only `reasoning_effort: none`. All other request
fields and the exact final-answer assertion remain intact. A changed publication
revision, model ID, repository, preset, variant or programs selection receives no
new override. SDK 0.1.0 receives no new override.

The result records published and effective request/assertion fingerprints and
which fields changed. The original request object is never mutated. The older
MiniCPM publication needs the source upgrade for this request field to work.

## Shared-server launch policy

The 0.2.0 policy selects **fixed backing by default**. This
avoids deriving logical KV capacity from a universal guessed elastic byte budget,
but it eagerly allocates retained storage and can still fail on device limits.
This is not a proven all-model setting or a transfer of the earlier mixed-policy
59/60 result. Known model/resource failures are reported as CI results. Memory
tuning and finding a universal policy are separate from delivery of the CI lanes
and conditional setup.

Operators can select fixed backing, or elastic backing with an explicit positive
memory allowance. Supplying only a memory allowance selects elastic backing.
Fixed backing and an elastic allowance are mutually exclusive. Elastic backing
without an allowance is rejected; zero/negative/unrepresentable values are also
rejected. Byte strings such as `8gib`, `8192mib` or a plain positive byte count are
normalized into an integer before sealing the policy. One policy applies to the
attempt's one shared server; no per-model retries, grouping or restarts occur.

For 0.1.0, server arguments remain empty so the existing native defaults apply.
New launch overrides are rejected on that compatibility path instead of silently
changing its historical behavior. No portable model-card budget, source bound,
weight or host memory setting is modified by this helper.

## NUMA syscall policy

The 0.2.0 descriptor names a checked-in snapshot derived from the Moby default
seccomp profile. Its only semantic change is an appended unconditional allow
rule for `mbind`. The descriptor pins the base and changed file SHA256 values,
source URL, profile ID and exact change. The original source URL points to Moby's
moving main branch; the checked-in snapshot bytes and digests pin this version,
and no network lookup happens during policy resolution or execution.

Preparation must copy the referenced profile into the prepared bundle and bind
its path/digest to the suite. Host and container verification check the copied
bytes before passing the profile to Docker. The policy does not request extra
capabilities, an unconfined container, root execution or daemon changes. Existing
nonroot, cap-drop, read-only inputs/cache, no-new-privileges and offline controls
remain the responsibility of the execution harness. The 0.1.0 descriptor is null,
preserving Docker's existing container policy.

## Helper contract

- `resolve(snapshot, pipeline_upgrade='auto', pool_backing=None, memory_bytes=None,
  publication_abi=None, runner_abi=None)`
  validates the concrete SDK identity, reads versioned configuration once and
  returns a canonical policy dictionary with `policy_sha256`.
- `validate(policy, snapshot)` checks the policy hash, exact runtime binding,
  upgrade decision and embedded configuration semantics. It returns a verified
  copy and performs no configuration file reads.
- `request_profile(policy, exact_selection, base_request, expected_text)` returns
  the effective request/assertion, profile identity, request fingerprints and
  explicit changed fields. Use an already resolved/validated policy.
- `server_args(policy)` translates the already validated launch block into native
  arguments. Default 0.1.0 produces `[]`; fixed produces
  `['--pool_backing=fixed']`; elastic produces one `--memory_bytes=BYTES` argument.

The current source retains active exact rules rather than revision history.
Validation checks each rule and duplicate identity without requiring a fixed
number of logical families, so historical saved three-rule policies still replay
from their embedded content.

The frozen policy embeds its configuration content and both canonical-content and
source-byte identities. The request-profile and seccomp descriptors are derived
from that embedded content. A changed live config has no effect on an existing
prepared run. The harness must additionally bind the complete policy and copied
files into its trusted prepared-input manifest; a hash is not a signature or a
replacement for the existing source/manifest integrity boundary.

## SDK-specific publication and runner ownership ABI

New automatic 0.2 and source-qualified 0.3 inputs use policy schema 3. After
selecting the exact SDK, preparation freezes publication declaration/caller source
and runner/public-entry source from its tested commit. Publication recognition
supports the reviewed 12-, 13- and 14-argument contracts. The runner receipt checks
native signature/export construction, canonical HAL reference identities, and
public step/epoch parameter types and bindings. It recognizes owning `REF` or
borrowed `PTR` inputs; unknown runner contracts fail preparation explicitly.

The signature-selected compatibility constructor introduced at HRX `beb700e3`
also qualifies for borrowed derivatives. Recognition checks its reviewed native
export block and the control path that chooses the same ownership for native
runner arguments and public device-buffer arguments. Publication recognition
checks the matching signature-selected 13/14-argument binding and invocation.
The selected profile remains the full 14-argument publication with borrowed
inputs, so existing adapters and strict verification still apply. These SDK
compatibility branches do not require another pipeline rewrite.

Each receipt binds its source bytes to the selected runtime identity and wheel
hash through the existing official build provenance, not independent binary
attestation. Evidence lives under `policies/publication-abi/` and
`policies/runner-abi/`. Offline verification rechecks the frozen bytes and
recognized contracts without fetching source. The resolved SDK snapshot schema
and runtime identity calculation do not change.

`resolve(..., publication_abi=descriptor, runner_abi=descriptor)` consumes this
evidence. Existing schema-1 and schema-2 policies keep their historical replay
rules. Valid unavailable publication evidence remains a model-local compatibility
result; corrupt evidence is an integrity failure. Release 0.1.0 and explicit
`none` bypass acquisition.

A borrowed runner profile selects the versioned
`tools/migrations/upgrade-pipelines-runner-owned-to-borrowed.py` adapter after the
legacy and publication migrations. The adapter rewrites only reviewed borrowed
HAL input and forwarding forms, preserving owning host buffers and original
publication inputs. It rejects ownership-producing results, retention operations,
unknown consumers/imports and mixed ownership forms. Native 0.2 publications still
receive required publication and borrowed-input adaptation for the selected SDK;
an owning runner profile retains its existing source behavior.

See [prepared pipeline policy](prepared-pipeline-policy.md) for stage seals and
[epoch publication migration](pipeline-upgrades/0.1.0-to-0.2.0/epoch-publication-abi.md)
for publication closure, standalone CLI and source-association limits.
