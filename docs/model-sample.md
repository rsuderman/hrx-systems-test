# Representative cached-model sample

The default suite in [cached-model-tests.json](../config/cached-model-tests.json)
registers eight distinct model IDs across seven published architecture labels.
It spans small dense models, the existing hybrid Qwen3.5 baseline, a different
LFM family, and a larger mixture-of-experts model. HyperCLOVA X shares the Llama
architecture label but adds a distinct model/tokenizer/chat-policy path.

This is text smoke coverage of the exact listed presets and variants. It does
not establish vision, tools, long-context accuracy, throughput or every
quantization of these model families. Actual outcomes belong to run artifacts;
registration in the suite is not a passing-support claim.

## Selected publications

Sizes below are decimal GB of selected GGUF assets from the pinned model locks.
Every primary language-model GGUF is Q4_K_M; GPT-OSS here is not MXFP4. The
Qwen3.5 row also includes its F16 vision projector because it is part of the
published selected closure, although this smoke test sends no image.

| Entry | Published model | Architecture | GGUF GB | Selection reason |
|---|---|---|---:|---|
| `qwen35-08b` | [Qwen3.5 0.8B](https://huggingface.co/loom-fun/unsloth-Qwen3.5-0.8B-loom/tree/f1ba2f23c44400bd0b1f88b406029daf41d7a143) | `qwen35` | 0.738 | Retains the previously validated hybrid baseline and companion-asset closure. |
| `qwen25-05b` | [Qwen2.5 0.5B Instruct](https://huggingface.co/loom-fun/bartowski-Qwen2.5-0.5B-Instruct-loom/tree/dad263bdf7c45f2e41c05b02df6cb7fae99f20c3) | `qwen2` | 0.398 | Small older-generation dense model and its chat/tokenizer path. |
| `qwen3-06b` | [Qwen3 0.6B](https://huggingface.co/loom-fun/unsloth-Qwen3-0.6B-loom/tree/78c52c1ae45b15805ef12c4b8c297bc7d03c01f2) | `qwen3` | 0.397 | Small newer-generation dense model with explicit thinking control. |
| `gemma3-1b` | [Gemma 3 1B IT](https://huggingface.co/loom-fun/ggml-org-gemma-3-1b-it-loom/tree/abbadf413c9439ac9679380b70ec5bb7dbbac136) | `gemma3` | 0.806 | Independent model family, program and tokenizer policy. |
| `llama32-3b` | [Llama 3.2 3B Instruct](https://huggingface.co/loom-fun/unsloth-Llama-3.2-3B-Instruct-loom/tree/5906a5a0876c68730cdb33544af4bee4766e2d07) | `llama` | 2.019 | Larger dense model and Llama-family serving path. |
| `lfm3-17b` | [LFM3 1.7B](https://huggingface.co/loom-fun/bartowski-LFM3-1.7B-loom/tree/1c1e204f4986c85e6de5708da44c50679d6a7332) | `lfm3` | 1.116 | Non-Qwen/non-Llama program family with explicit thinking control. |
| `hyperclovax-15b` | [HyperCLOVA X SEED Text Instruct 1.5B](https://huggingface.co/loom-fun/rippertnt-HyperCLOVAX-SEED-Text-Instruct-1.5B-loom/tree/c408c5b27093c60eac08762010c669257197a9cb) | `llama` | 1.134 | Distinct model and tokenizer/chat policy within a shared architecture. |
| `gpt-oss-20b` | [GPT-OSS 20B](https://huggingface.co/loom-fun/unsloth-gpt-oss-20b-loom/tree/a43b033fd89810c73daa35636f79dcc1eaf589cc) | `gpt-oss` | 11.625 | Larger MoE pipeline and an always-reasoning policy. |

The complete selected closure is **18,302,580,627 bytes**: 18,231,825,024 GGUF
bytes plus 70,755,603 tokenizer bytes. This is about **18.30 GB / 17.05 GiB** of
asset bytes before native-cache metadata or filesystem overhead. Existing verified
blobs may be reused. No generated serving output or model weight belongs in Git.

## Reproducible test settings

Every profile uses the same pinned `hrx-loom` 0.1.0 wheel, dependency lock and
container image. Published `chat` preset execution settings are copied unchanged
into `published_execution`; no context, pool, rows or speculative-depth reduction
is hidden in the smoke test. All profiles keep memory qualification `unknown`:
publisher memory hints are advisory, and runtime success is the actual evidence.
The current sample is intended for an operator-selected compatible host, including
the shared-memory `gfx1151` runner; its dedicated VRAM aperture alone is not the
whole usable memory budget.

Every request uses temperature zero and nonstreaming output. Most ask “What is
two plus two? Reply with the digit only.” Gemma uses the equivalent arithmetic
prompt “Calculate 2 + 2. Reply with a single numeral.” The stripped final response
must equal `4`; reasoning text and longer final responses do not satisfy that assertion.

- The existing Qwen3.5 profile is unchanged, including its eight-token budget.
- New ordinary text profiles use a 32-token budget.
- Qwen3 and LFM3 explicitly use supported `reasoning_effort: high` and a
  256-token total generation budget. Native probes returned separate reasoning
  and the exact final answer `4` on this request path.

- GPT-OSS is always reasoning. Its request sets supported `reasoning_effort: low`
  and a 256-token total generation budget so a final answer can follow analysis.
  This does not change the published execution capacities.

The reasoning controls were checked against each pinned `program/control.loom`
and its imported model-policy data, then the request choices were validated in
native probes. These settings describe the tested smoke path. Earlier probe
failures and their subsequent request-policy/prompt adjustments remain in the
validation evidence; they are not claims about all request modes or model quality.
The complete final suite must still pass using these exact recorded requests.

## Seed and run

The existing Qwen3.5 selection retains `cache/seed-manifest.json`. Every new entry
has a separate `cache/models/<entry-id>/seed-manifest.json`, while all assets use
the same native HF/Loom caches. A seed for one new model therefore does not
replace the existing model's index. Seed the registered suite with one command:

```sh
cache_root="${HRX_SYSTEMS_ROOT:-$HOME/.local/share/hrx-systems-test}"
bash scripts/cache/seed-cached-models.sh --root "$cache_root"
```

For a runner using a different account, append `--reader-user ACCOUNT` to the
suite-seeding command so the selected reader can consume the published cache
indices. Keep the cache root consistent between seed and CI. Test containers
still consume assets and indices read-only. For a single entry, the existing
`seed-models.sh --baseline config/<entry-id>.json --seed-manifest PATH` command
remains available.

After seeding, the [existing staged commands](specs/pipeline-stages.md) prepare,
execute and report the full default suite. For one combined local run:

```sh
bash scripts/test/run-cached-models.sh --root "$cache_root" --cycles 2
```

One server loads, queries and fully unregisters each model sequentially. Repeated
cycles are reported separately from the eight distinct model IDs. Failures remain
visible for every expected entry; no eligibility filter silently hides a model.
