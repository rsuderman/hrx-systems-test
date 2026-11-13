# Upgrade pipeline source contracts from HRX 0.1.0 to 0.2.0

The [versioned upgrader](../../../tools/migrations/upgrade-pipelines-0.1.0-to-0.2.0.py)
creates a local executable derivative of supported older hosted pipelines. It
preserves the original tree, publication locks and asset references. It does not
publish a new model, select an SDK, configure GPU memory or change smoke requests.
The caller selects this migration only for a compatible target runtime; the
pinned 0.1.0 execution path must use the original source.

```sh
python3 tools/migrations/upgrade-pipelines-0.1.0-to-0.2.0.py SOURCE --check
python3 tools/migrations/upgrade-pipelines-0.1.0-to-0.2.0.py SOURCE --output NEW_DIRECTORY
```

`--check` prints a JSON plan without writing. `--output` requires a new, separate
directory and writes an adjacent `NEW_DIRECTORY.upgrade.json` receipt. Neither
the output nor an existing receipt is overwritten. Linked code, linked directories
and unsupported legacy provider bodies require manual review. Individual asset
file symlinks, including container-relative links unresolved on the host, are
preserved. Regular GGUF, safetensors and binary payloads cannot be copied.

| Repair | Supported change |
| --- | --- |
| [Public pool capacity](pool-capacity.md) | Remove the obsolete preset execution field |
| [Vector constants](vector-constant.md) | Select integer or floating literal splat operations |
| [Command operations](command-program.md) | Rename the three legacy command operations |
| [Text-service ABI](text-service-abi.md) | Migrate verified provider/export closures together |
| [Epoch publication ABI](epoch-publication-abi.md) | Forward retained control state for a verified 13-argument SDK contract |
| [Weight-key ranges](weights-input-abi.md) | Add explicit full-buffer key ranges |
| [Convolution indexing](convolution-index-assumption.md) | Make the token-bound guard explicit |
| [Finite decision batches](decision-batch-export.md) | Adapt the verified one-shot provider to retained batches |
| [Finite buffer bounds](finite-buffer-bounds.md) | Check access widths against buffer extent |
| [Audio reasoning output](reasoning-markup.md) | Recognize generated opening reasoning markers |
| [MiniCPM reasoning selection](reasoning-selection.md) | Honor an explicit root `reasoning_effort: none` request |

## Plan API and evidence

`plan(source, *, epoch_publication_abi='legacy-12')` returns `(changes, report)`. `changes` maps source-relative filenames
to complete replacement bytes and performs no writes. `write_derivative(source,
output, *, epoch_publication_abi='legacy-12')` writes one separate audited copy and returns `(report, report_path)`.
Preparation can use the plan API after verifying original acquisition and before
sealing executable hashes. Execution verifies the resulting frozen bundle; it
must not perform migration or reinterpret the derivative as original publication.

Automatic preparation passes the target verified from the selected SDK explicitly.
Standalone callers can use `--epoch-publication-abi control-state-13`; the default
preserves the historical 12-argument publication contract.

The report includes:

- Migration ID `hrx-pipelines-0.1.0-to-0.2.0`, implementation revision and the exact
  upgrader file SHA256; source and target contract labels.
- `applied` or `unchanged` status, complete operation counters and changed-file
  before/after SHA256 identities.
- `source_files` and `output_files` identities for the card, Loom programs and
  publication locks, with relative paths and byte sizes. Asset payloads are not
  read or hashed. Informational source/output paths are not verification authority.
- Diffs capped at 64 KiB per changed file and edit details capped at 1024 entries
  per file. Explicit truncation metadata and full diff/edit-list hashes remain;
  full operation counts and source/output hashes never depend on truncated text.
- Preserved source `llm.pool` range constraints, `locks_modified: false` and
  `publication_ready: false`.

An unsupported repair fails before publishing output. Publication failures remove
the newly created receipt and empty output reservation. A copied source must
match its planned identities, and the result must be idempotent before publication.
Original locks prove acquisition provenance only; publishing a refreshed model
still requires authoritative generation and lock regeneration.

## Validation and limits

Run the standard-library tests from the repository:

```sh
python3 -m unittest discover -s tests -p test_upgrade_pipelines.py -v
```

The suite retains the 24 prototype regression cases and tests receipt integrity,
bounded evidence, refusal/cleanup behavior and directory-link namespace safety.
Two additional tests run 21 parser and two prompt cases through a selected SDK:

```sh
LOOM_TEST=/path/to/selected-sdk/bin/loom-test \
  python3 -m unittest discover -s tests -p test_upgrade_pipelines.py -v
```

Those native tests assert that the legacy fixtures fail and the generated source
passes. They skip explicitly when `LOOM_TEST` is unset; no SDK is installed or
fetched implicitly. Native qualification uses the selected SDK, not a mock parser.

The source prototype was qualified against HRX commit
`338c5ad9f09d4337185e3488acda0a8087a6a29e`. The local 59/60 model result additionally
used several memory policies, a NUMA syscall allowance and explicit reasoning
profiles for three selections. That is historical mixed-policy evidence, not a
59-pass guarantee from this command or a single CI server. Ornith 122B remains
resource-blocked. See the [integration specification](../../specs/pipeline-upgrade-0.1.0-to-0.2.0.md)
for runtime selection, integrity, paired CI and settings ownership.
