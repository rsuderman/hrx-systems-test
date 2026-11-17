# Enroll and operate a hardware runner

Dogwood is the only enabled host, selected by the single custom label
`hrx-gfx1151`. Labeling a runner does not enroll another hardware target into
scheduled or push runs. The model workflow explicitly enables its hardware in
source and consumes the host's published model and pipeline caches read-only.

## Prepare the account and keep state separate

Use a dedicated non-root Linux x64 account with functioning AMD drivers and
Docker access. The account is trusted host infrastructure; Docker access grants
host-level control. Host administrators install prerequisites and configure
accounts, groups and device access. These repository scripts do not use sudo,
change drivers, install packages or configure credentials.

Run bootstrap commands from the repository as the intended runner account:

```sh
export HRX_SYSTEMS_ROOT="$HOME/.local/share/hrx-systems-test"
export HRX_RUNNER_LABEL=hrx-gfx1151
bash scripts/host/bootstrap-runner.sh check
bash scripts/host/bootstrap-runner.sh prepare
bash scripts/host/bootstrap-runner.sh install-runner
```

This root is private runner state. Its `runner/` contains registration
credentials; `work/`, `runs/` and `results/` contain execution state. Keep it owned
by the runner account and outside the checkout. `install-runner` verifies the
pinned official archive in `config/bootstrap-versions.env` and refuses a nonempty
runner directory. Do not reinstall an existing registration.

The shared model root is a separate location, currently `/srv/loom-models` on
Dogwood. Do not set the bootstrap state root to this cache. Model commands accept
an explicit `--root` before considering `HRX_SYSTEMS_ROOT`, then the per-user
default. GitHub model jobs use the repository Actions variable
`HRX_SYSTEMS_ROOT` first, the runner environment second, then the per-user default.
For Dogwood the repository variable selects `/srv/loom-models`; it does not move
the private installed runner. Bootstrap's storage smoke step uses disposable
`RUNNER_TEMP` fixtures, so it needs no write access to the shared cache.

`check` requires Linux, a non-root account, curl, tar, sha256sum, Docker and
`timeout`. Hardware labels also require host Python 3, without Loom or any SDK.
Run only the read-only physical check independently:

```sh
bash scripts/host/check-runner-hardware.sh --label hrx-gfx1151
```

The supported mapping is raw KFD target 110501 for `hrx-gfx1151`, exactly one
render device driven by amdgpu, one nonzero KFD GPU identity, and matching render
minor. Only `amdgpu://0` is supported. Missing, ambiguous, unknown or mismatched
identity fails. The result does not certify pipeline features or usable memory.

## Register a new runner or update an existing one

Keep the explicit `HRX_RUNNER_LABEL` in the intended account's shell, then run:

```sh
bash scripts/host/bootstrap-runner.sh register https://github.com/OWNER/hrx-systems-test
bash scripts/host/bootstrap-runner.sh start
```

Obtain the short-lived registration token through **Settings → Actions → Runners
→ New self-hosted runner** as the repository owner. Paste it into GitHub's
interactive configurator; never save it in a command, source file or runner
account's unrelated GitHub CLI credentials. Set `HRX_RUNNER_NAME` before
registration if needed. The script forwards the selected single custom label,
keeps GitHub's automatic labels, uses the private external work directory and
refuses to overwrite an existing `.runner`. Keep `start` running in the
foreground during initial checks; Ctrl-C stops it.

New registrations and local `check` commands require an explicit
`HRX_RUNNER_LABEL`; there is no legacy registration default. Both smoke workflows
default to the enabled hardware label.

For an existing runner, the repository owner adds `hrx-gfx1151` in **Settings →
Actions → Runners → the existing runner → Labels**. Preserve automatic labels and
retain any older selector until its source migration and queued jobs are
accepted. Check that the original
registration is online and has the requested label; do not re-register, copy
credentials or restart its service for a label update. Dogwood must remain the
sole runner eligible for this hardware label.

For unattended service operation, the administrator stops any foreground runner
and runs the official service commands separately from ordinary bootstrap:

```sh
cd "$HRX_SYSTEMS_ROOT/runner"
sudo ./svc.sh install "$(id -un)"
sudo ./svc.sh start
sudo ./svc.sh status
```

These are administrator commands, not commands performed by an agent or the
bootstrap. Preserve the existing dedicated-account service during this rollout.

## Seed the cache and verify runner reads

The cache owner seeds selected models with [seed-models.sh](seed-models.md) or
the [suite seeder](../scripts/cache/seed-cached-models.sh),
then seeds [original pipelines](seed-pipelines.md):

```sh
bash scripts/cache/seed-cached-models.sh --root /srv/loom-models
bash scripts/cache/seed-pipelines.sh --root /srv/loom-models --selection cached
```

These are explicit operator acquisitions, separate from registration and smoke
checks. Review the selected suite and both seed reports; do not reseed or evict
assets during a model run. Do not download models merely to prove a label.
The runner needs read access to manifests, pipeline entries/blobs and model
assets, plus traversal of every parent directory. As the runner account inspect
these paths before testing:

```sh
ls -ld /srv /srv/loom-models /srv/loom-models/cache
ls -ld /srv/loom-models/cache/pipelines /srv/loom-models/cache/pipelines/entries
```

Follow the seed guide's read-access procedure for the actual published files.
Have the owner resolve an unreadable path; do not grant the runner cache-write
access to make a check pass. Registration does not seed anything. Model tests
attempt every locally published cached selection under the existing runtime
exclusion rules; the hardware label adds no model eligibility or memory policy.

## Prove assignment, then request model verification

With the runner online, choose **Actions → Runner pickup test → Run workflow**,
then **Runner bootstrap check → Run workflow**, selecting
`hardware=hrx-gfx1151`. Equivalent commands are:

```sh
gh workflow run runner-pickup.yml --ref main -f hardware=hrx-gfx1151
gh workflow run runner-bootstrap.yml --ref main -f hardware=hrx-gfx1151
gh run list --limit 10
```

Pickup executes shell work and uploads requested-label/runner evidence.
Bootstrap additionally checks physical identity, host prerequisites and
read-only container input/writable disposable output. These checks do not run a
model. During an idle enrollment checkpoint, bound queue observation separately
from the five-/ten-minute job execution timeouts: if no runner picks up a probe
within 120 seconds, inspect online/busy/label state and cancel the unmatched run:

```sh
gh run cancel RUN_ID
gh run view RUN_ID --json status,conclusion,jobs
gh run download RUN_ID --dir "$HOME/runner-proof-RUN_ID"
```

Retry after resolving assignment. Do not change production routing to rescue an
unmatched smoke check. Confirm the original Dogwood runner name and requested
label in the artifact, and renderD128/raw 110501 in bootstrap's physical record.

Dogwood's additive enrollment passed [pickup run 43985434183](https://github.com/rsuderman/hrx-systems-test/actions/runs/43985434183)
and [bootstrap run 43985762611](https://github.com/rsuderman/hrx-systems-test/actions/runs/43985762611).
Bootstrap keeps its fixture writes in runner scratch space; its physical and
host diagnostics still identify the configured shared cache root.

After both checks pass and cache reads are ready, choose **Cached-model
verification → Run workflow** with `hardware=hrx-gfx1151`, `selection=cached` and
`runtime_source=both`, or run:

```sh
gh workflow run single-model-e2e.yml --ref main \
  -f hardware=hrx-gfx1151 -f selection=cached -f runtime_source=both
```

This performs real preparation and inference, with separate release and CI
lanes. Inspect the combined summary and lane artifacts for requested hardware,
actual runner, observed device, frozen inventory and confirmed cleanup. Model
failures retain their real status; pickup success is not model success. See
[paired runtime verification](paired-runtime-ci.md) for diagnostic single-lane
and exact replay options.

## Enable another hardware target in source

The current explicit enrollment points are `TARGETS` in
`python/hrx_systems/host/runner_hardware.py`, hardware validation/defaults in
`python/hrx_systems/ci/paired.py`, manual choices in `runner-pickup.yml`,
`runner-bootstrap.yml` and `single-model-e2e.yml`, and the default selectors in
`single-model-e2e.yml`. The reusable `_model-verification-worker.yml` receives the
frozen hardware choice and uses it directly. All workflow paths are under
`.github/workflows/`. Keep choices, physical identity mappings, defaults and
fixtures consistent. No discovery API, administrative discovery secret, runner
group or generated enrollment file is involved.

Before enabling another matching runner, replace the present separate-job
layout with one assigned-runner job owning the inventory and both sequential
lanes, or agree another explicit affinity design. A label selects a pool;
post-assignment runner-name checks detect mistakes but cannot create affinity.
Then introduce a hardware matrix/entry, scope concurrency to the physical device
and include hardware in artifact/aggregate keys. Enroll the host, seed its cache
and pass both smoke checks before enabling its source entry. An absent runner
is unavailable coverage. Multi-GPU mapping and ARM64 SDK/container support remain
separate work; keep Dogwood as the sole enabled target in this delivery.

## Recovery, rollback and retirement

An offline or busy runner, missing label or lack of source enrollment explains an
unmatched queue. A physical mismatch fails before acquisition; correct the
explicit label or host assignment rather than weakening the check. Missing or
unreadable cache data requires owner reseeding/read-access repair, not automatic
download fallback or changed model expectations.

During a staged migration, retain the old label until both smoke checks, the
ordinary cutover run and the operator guide are accepted. Before retiring it,
reverting the routing change restores the old selector. After retirement, the
owner must first restore `hrx-systems-test-bootstrap`, then revert routing.
Audit active and queued older workflows before removing only that custom label;
preserve automatic labels, the private registration/service and shared cache.
Remove a target from scheduled/push source routing before retiring its runner.
Stopping/removing the actual service or registration is separate administrator
work and never requires deleting shared model assets.
