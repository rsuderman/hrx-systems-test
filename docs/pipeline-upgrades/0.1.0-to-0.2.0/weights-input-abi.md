# Add explicit weight-key byte ranges

Upstream [04aafdbe, Resolve weight keys from reusable buffer ranges](https://github.com/ROCm/hrx-system/commit/04aafdbe3e266109fef6452a67e74e438dadf2a9)
adds a key-buffer byte range to both weight imports:

```text
input(source, key, data_offset, data_length, alignment)
input(source, key, key_offset, key_length, data_offset, data_length, alignment)
parameter(source, key)
parameter(source, key, key_offset, key_length)
```

Legacy callers supplied the entire key buffer. The upgrader inserts zero offset
and the current `buffer.length` converted to i64 at each direct call, and changes
the matching `import("weights")` declaration. Tensor data ranges, alignment,
returned staging offsets and conversion logic remain intact. New explicit-range
signatures and unrelated local functions stay unchanged.

Unknown signatures, indirect/malformed calls, duplicate imports or generated-name
collisions require manual review. Regression cases distinguish nonzero tensor data
offsets from key offsets, preserve metadata/results and test idempotence.

The local migration cleared Nemotron Bolt, Flint, Muse Glimmer and Ornith import
errors. The first three then passed complete inference cycles; Ornith exposed a
separate memory/backing limit. No checkpoint bytes or quantization are changed.
