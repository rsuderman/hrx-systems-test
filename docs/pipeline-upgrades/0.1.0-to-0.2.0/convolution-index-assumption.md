# Make convolution token bounds explicit

MiniCPM OCR and the Unsloth Qwen3.8 27B source fail one-token specialization:
`index.assume` requires `[0,8191]` for `token-back`, but facts prove `[-3,-1]`.
The old branch tests only unsigned `rel >= back`.

Valid live span metadata gives `rel = token-input` and `0 <= rel <= token`.
Therefore a projection read guarded by `rel >= back` also requires `token >= back`.
The upgrader makes that second bound explicit and combines it with the original
guard. This lets specialization discard the impossible first-token projection
read before checking its assumption.

Only two exact helper bodies are recognized, with 6144 or 10240 channel geometry.
All source index assertions, tap order, activation and fresh/saved-history behavior
remain unchanged. Unknown matching helpers require review; linked source namespaces
are refused rather than silently omitted from inspection.

Regression fixtures preserve the history branch and bounds and reject altered
helpers. Historical local qualification included a minimized compiler reproducer,
288 GPU boundary cases and two complete model cycles for each affected publication
on HRX `338c5ad`; this source guard does not claim a compiler-wide repair.
