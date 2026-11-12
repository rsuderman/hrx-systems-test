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
