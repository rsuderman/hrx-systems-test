# Adapt the verified decision provider to retained finite batches

The older d1 provider has no `decision_collect_batch_v1` and uses packed buffers
rather than the newer typed finite-batch handle. Its existing preparation body
already builds the upload and complete result plan.

The upgrader recognizes exact control/service file hashes and updates both:

```text
prepare_batch(state, tags, ranges, packed) -> (stage, upload, plan)
prepare_batch(state, tags, batch) -> (stage, upload, plan)
run(stage, input, output)
run(state, stage, input, output, batch)
collect_batch(state, batch)
```

It imports the `finite.batch` type, obtains old packed inputs via `batch_pack`,
preserves preparation and model math, and finishes the one-shot batch through
`batch_finish` after all rows are published. The runner's `execute_2` import keeps
its original three arguments. Provider declarations and public exports agree.
Unknown or incomplete file pairs stop; no stub probability/result is generated.

This repair exposes a separate [buffer bounds](finite-buffer-bounds.md) requirement,
handled within the same exact provider family. Local d1 qualification at publication
`262d75c8a68d065e046780d285b029cb11075fde` passed two full decision smoke cycles,
including identity, probability, confidence and usage assertions. That is the
recorded single-choice smoke contract, not comprehensive batching/model quality.
