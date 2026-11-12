# Migrate typed vector literal broadcasts

The newer compiler removes `vector.constant`. Upstream
[51f48e8, Split typed vector literal splats](https://github.com/ROCm/hrx-system/commit/51f48e8ded0ad1851eb664f611ad4e4e13711be8)
replaces scalar literal broadcasts with `vector.splati` for integer/index/offset
result elements or `vector.splatf` for supported floating elements.

```text
%mask = vector.constant 252645135 : vector<8xi32>
%mask = vector.splati 252645135 : vector<8xi32>
```

The upgrader preserves literal spelling, result type/shape and surrounding text.
Comments, strings and operand-driven `vector.splat` remain unchanged. Only supported
single-line scalar literals are rewritten; unknown syntax, elements, lists or
runtime operands stop for review.

Qwen2.5's qualified source contained 185 literals: 130 integer and 55 floating
broadcasts. This repair exposed the subsequent command-operation failure rather
than independently proving model readiness. Tests cover typed values, negative
floating zero, comments/strings, unsupported forms and idempotence.
