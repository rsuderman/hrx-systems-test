# Honor an explicit no-reasoning selection in the older MiniCPM provider

Both MiniCPM V publications can emit `4` and stop while their pre-opened think block
remains open. Default output then contains reasoning `4` and an empty final answer.
The newer Bartowski provider already honors `reasoning_effort: none`; the older
OpenBMB provider ignores it.

The upgrader recognizes the older provider's exact name, `chat_begin`/`chat_end`
bodies and generation prompt. It parses the root request's string
`reasoning_effort`, using existing JSON member/unescape capabilities. Explicit
`none` selects a closed empty think block and initial answer state. Missing/other
selections retain the original default prompt and reasoning state. This bounded
migration does not implement every template-control spelling or alter defaults.

The flag passes through the provider's preserved message state. No generated
response is relabeled and no final-answer assertion is relaxed. Unknown templates
require review. The optional native test proves exact default and disabled prompts
and output states, with a failing legacy case and passing upgraded cases.

The older affected publication is `loom-fun/openbmb-MiniCPM-V-4.7-35B-A3B-loom`
at `d2183a71a44a0defe94f53823c674b7be343843d`. Two full local cycles passed with
an explicit `reasoning_effort: none` request. That request profile is separate
runtime-qualified configuration: this source command does not inject it. Qwen3
14B and the newer MiniCPM publication also need their qualified request profiles
for the recorded numeric smoke test, using existing source support.


## Base profiles for supported release publications

The exact newer publications below already implement root `reasoning_effort:
none` on release v0.1.0. Their numeric smoke request selects that field in
`config/model-test-profiles.json`, so both runtime lanes receive it before any
0.2 runtime-policy override. The prompt, temperature0, max_tokens256 and exact
final-answer assertion4 remain unchanged.

| Publication | Model | Revision |
| --- | --- | --- |
| `loom-fun/ggml-org-Qwen3-14B-loom` | `Qwen/Qwen3-14B` | `17207bf1c898e0996b4bdcdf2a685f95fd42b511` |
| `loom-fun/bartowski-MiniCPM-V-4.7-35B-A3B-loom` | `openbmb/MiniCPM-V-4.7-35B-A3B` | `0b87ba7fb6fb8cebfc378a148696d4f586c1ff12` |

An unchanged release request reproduces two distinct failures: Qwen spends all
256 completion tokens in reasoning; MiniCPM stops after two tokens with reasoning
`4` and empty final content. Selecting none returns final `4` with normal stop and
two completion tokens. This is an explicit smoke-request contract, not a repair of
default thinking or permission to treat reasoning as a final answer.

Qwen's catalog pin advances to the revision above. MiniCPM exceeds the catalog's
20GB limit and stays outside that catalog; its exact base-profile selection and
host-owned seed alias identify the revision. Historical profiles and release
exclusions remain tied to their older exact revisions. This change neither
applies a pipeline rewrite to v0.1.0 nor changes runtime memory/security settings.
