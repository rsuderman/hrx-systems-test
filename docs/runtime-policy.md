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
| 0.2.0 family, release or Actions | Versioned 0.1.0-to-0.2.0 migration | Versioned policy below |
| Unsupported family in automatic mode | Explicit preparation failure | No guessed fallback |
| Unsupported family with explicit raw `none` | Skipped diagnostic run | Existing defaults preserved |

For a known 0.2.0 runtime, explicit `pipeline_upgrade=none` bypasses only the
source transformation. It keeps the selected runtime request/server/container
settings, so the override is not an implicit change of other test conditions.
The supported families here are 0.1.0 and 0.2.0, including prerelease/development
and local-version forms; this is not a promise about later SDK versions.

## Request profiles

`config/runtime-policies/0.2.0/policy.json` lists three exact six-field selection
identities: Qwen3 14B and the two MiniCPM V 4.7 publications qualified in the spec.
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

- `resolve(snapshot, pipeline_upgrade='auto', pool_backing=None, memory_bytes=None)`
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

The frozen policy embeds its configuration content and both canonical-content and
source-byte identities. The request-profile and seccomp descriptors are derived
from that embedded content. A changed live config has no effect on an existing
prepared run. The harness must additionally bind the complete policy and copied
files into its trusted prepared-input manifest; a hash is not a signature or a
replacement for the existing source/manifest integrity boundary.

## SDK-specific publication ABI

New automatic 0.2 inputs use policy schema 2. After selecting the exact SDK,
preparation acquires the two fixed native publication source files at its tested
commit and freezes a recognized 12/13-argument contract or an explicit unknown
result. The receipt is bound to the existing runtime identity and wheel hash;
backing, request and seccomp policy stay unchanged. The resolved SDK snapshot
schema and runtime identity calculation do not change.

`resolve(..., publication_abi=descriptor)` requires this evidence for automatic
0.2 upgrading. `validate` preserves already sealed schema-1 policy behavior,
while schema-2 prepared verification requires the receipt/source closure and
an explicit matching target in the derivative report. Valid unavailable or
unsupported evidence is a model-local compatibility result; corrupted evidence
is an integrity failure. Offline preparation, execution and rendering perform
no source lookups. Release 0.1.0 and explicit `none` bypass acquisition.

See [epoch publication migration](pipeline-upgrades/0.1.0-to-0.2.0/epoch-publication-abi.md)
for the exact closure, standalone CLI and source-association limits.
