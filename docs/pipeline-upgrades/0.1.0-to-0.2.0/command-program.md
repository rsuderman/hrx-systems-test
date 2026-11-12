# Rename legacy command operations

Upstream [d835680, Flatten command program operation names](https://github.com/ROCm/hrx-system/commit/d83568010afbbdf526fc152e5bb2a1381785075b)
changes three operation spellings while preserving their callable contracts:

| Old | New |
| --- | --- |
| `command.program.def` | `command.def` |
| `command.program.decl` | `command.decl` |
| `command.program.launch` | `command.launch` |

The upgrader changes operation tokens only. It preserves symbol names, result and
argument types, bodies, comments, strings and `command.return`. An unfamiliar
`command.program.*` operation requires manual review.

Four Qwen2.5 definitions needed this change during local qualification. Its
combined card/vector/command migration then reached the separate source-pool and
container memory-placement requirements. Tests cover definitions, declarations,
launches, symbol/comment exclusions and rejection of unknown family members.
