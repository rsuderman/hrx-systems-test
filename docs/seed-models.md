# Seed the model cache

From this repository, run:

```sh
bash scripts/cache/seed-models.sh
```

The command selects the first entry in `config/cached-model-tests.json`, currently
Qwen3.5 0.8B, and composes it with `config/runtime.json` and the test profiles. It downloads the selected hosted pipeline, resolves its model card/lock
through the pinned Loom SDK, verifies the required model assets and publishes
`cache/seed-manifest.json`. Docker access and writable local storage are required;
the command does not install host packages or request sudo.

Persistent storage defaults to `$HOME/.local/share/hrx-systems-test`. Set
`HRX_SYSTEMS_ROOT` to select another absolute directory. Configure the GitHub
runner with the same root, especially if it uses a different account. The operator
must be able to write this directory; model-test containers consume it read-only.
The wrapper serializes seed writers. Do not reseed or evict assets during a test.

Only the selected model weights, tokenizer and other asset roles persist in the
native Hugging Face/Loom caches. SDK payloads, installed environments and pipeline
staging are temporary and removed after each attempt. Compact reports, SDK
identities and logs remain under `runs/seed-.../`. No model is executed and no GPU
compatibility or memory-fit claim is made.

Repeat the same command to reuse verified model assets. To prove reuse without
asset network access:

```sh
bash scripts/cache/seed-models.sh --asset-network offline
```

This still fetches the SDK and pipeline online, then runs the same native
asset/materialization phase in a network-disabled container. Missing or invalid
assets fail rather than falling back to downloads. The prior published index
remains intact if a seed fails; inspect the printed attempt's logs/report.

Optional `--baseline`, `--root`, `--output` and `--seed-manifest` override the
defaults. An explicit output directory must not already exist. For a gated model,
provide `HF_TOKEN` in the acquisition environment; auth files are not copied or
mounted into containers. The default public model needs no token.

Use `--model qwen25-05b` to select another sample, and `--runtime FILE` to use an
explicit shared runtime. External legacy composed files remain accepted with
`--baseline FILE`; a conflicting `--runtime` is rejected. `--baseline` and
`--model` are mutually exclusive. New model selections belong in the suite or
manual download catalog; model assets still belong in the external cache.
