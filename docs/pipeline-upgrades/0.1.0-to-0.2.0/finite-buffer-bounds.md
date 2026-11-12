# Check finite-provider buffer access widths

Once d1's finite ABI binds, the compiler exposes `SUBRANGE/026`: its byte helper's
access may end at `9223372036854775807` although a constant buffer is only 2238 bytes.
The older helper does not express a checked relationship to buffer extent.

Within the exact verified decision provider, seven byte/i32/i64 read and indexed
write helpers obtain a checked offset. For position `P`, width `W` and extent `N`:

```text
require N >= W and P <= N - W
bounded_position = index.assume P [le(P, N - W)]
```

The existing `input.require` path checks both conditions before the assumption.
The subtraction form avoids overflowing `P+W`. Access widths are one, four or
eight bytes. Invalid byte accesses fail; they are not clamped, ignored or read
past the buffer. This is not a general rewrite or a complete malformed-packet
security qualification.

Preparation, tensor math, packet layout and result-plan construction remain intact.
The ABI-only local attempt reproduced the compiler rejection, and the combined
repair passed d1's two-cycle decision smoke contract. Tests also preserve the
original runner import and preparation body and require idempotent output.
