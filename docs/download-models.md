# Download the smaller-model catalog

From the repository, run this as the cache writer, without sudo:

```bash
bash scripts/download-models.sh --root /srv/loom-models
```

This sequentially seeds the 45 pinned publications whose selected GGUF assets
total at most 20 GB per publication, smallest first. The selection is in
`config/download-models-under-20gb.json`. It includes the existing regression
models; their cached assets are reused. Approximately 296 GB of additional GGUF
downloads were needed after the initial ten-model cache, with an estimated
4–6 hours for the complete catalog and approximately two hours for this subset.
Network rates and retries can change those estimates.

Python 3.11 or newer, Docker access, writable cache storage and internet access
are required. No host packages are installed. Run only when regression tests,
other seeders and cache maintenance are idle. The native seeder serializes writes
per model and fails a competing writer rather than running concurrently.

Preview without writing or downloading:

```bash
bash scripts/download-models.sh --root /srv/loom-models --dry-run
```

The terminal prints each model, completion time and log path. The printed run
directory contains `progress.json`, individual logs and verified seed manifests.
Failed models are reported and later entries are still attempted; the command
exits nonzero if any fail. Use `tail -f` on the current model's log for its stage;
the native download can be quiet during a large transfer. Retain the run directory
to inspect failures.

To run independently of a terminal session:

```bash
mkdir -p "$HOME/.local/state/hrx-systems-test"
nohup bash scripts/download-models.sh --root /srv/loom-models \
  > "$HOME/.local/state/hrx-systems-test/download-models.log" 2>&1 < /dev/null &
echo "Download PID: $!"
```

Follow that log with `tail -f`. Interrupt a foreground run with Ctrl-C; send
`kill -TERM PID` to a background run. Rerun the same command to retry: native
acquisition reuses and verifies completed assets and resumes supported partial
downloads. A fresh progress directory is created each time; no completed model
is trusted solely because an old progress entry says it passed.

`HRX_SYSTEMS_ROOT` or `--root` selects the cache, following the existing seeder's
user-independent conventions. Optional `--output` names a fresh report directory.
`--model loom-fun/REPOSITORY` restricts the selection and can be repeated.
`--asset-network offline` checks reuse while SDK/pipeline preparation stays online.

Weights, tokenizers and companion assets persist in the existing native shared
cache. Each model gets an explicit manifest under
`cache/models/catalog/PUBLICATION/seed-manifest.json`; the current regression
suite and its manifests are preserved. SDK installations and pipeline staging
are temporary. This command does not execute models, qualify memory or hardware
compatibility, or enable additional regression tests.
