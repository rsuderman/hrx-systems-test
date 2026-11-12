# Remove the public preset pool-capacity field

HRX 0.2.0 rejects the older `presets.<name>.execution.pool_capacity` setting. The
upgrader removes that field from every named preset and preserves all other card
settings and model identity. Card formatting may change when the field is removed;
its new bytes receive a separate executable identity.

This is a public-card contract change. It does not rewrite a program's
`config.decl @llm.pool` bound, convert token capacity into a byte allowance, or
choose a host memory budget. Recognized source ranges are reported unchanged.

The original Qwen2.5 0.5B case at publication revision
`dad263bdf7c45f2e41c05b02df6cb7fae99f20c3` advanced beyond card validation with
this repair, then exposed the [vector constant](vector-constant.md) compiler issue.
Only combined source and explicit deployment settings established inference.
Regression coverage preserves unrelated capacities and reports the removed value.

The 0.1.0 release path must bypass the upgrade. No shared cache, seed receipt,
published lock or hosted card is edited by the command.
