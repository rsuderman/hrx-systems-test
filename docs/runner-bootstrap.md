# Bootstrap a standalone GitHub runner

This is the initial runner setup, separate from the model-testing system still
being designed. It checks host prerequisites and container storage isolation.
It does not install Loom, download models, register automatically, or enable a
nightly. The repository remains local until you choose to publish it.

Use a dedicated non-root account on a Linux machine with working Docker and GPU
drivers. The account needs Docker access; treat that account as trusted host
infrastructure. Avoid using an account containing unrelated credentials. Install
OS packages and configure Docker using the host's normal administration process;
these scripts do not run sudo or modify groups/drivers.

## 1. Check and prepare the host

From the repository root, as the intended runner account:

```sh
export HRX_SYSTEMS_ROOT="$HOME/.local/share/hrx-systems-test"
bash scripts/bootstrap-runner.sh check
bash scripts/bootstrap-runner.sh prepare
```

Keep this root outside the checkout. It contains `runner/` (including GitHub
registration credentials), `work/`, persistent `cache/`, per-run `runs/` and
`results/`. Keep ownership with the runner account. GPU device visibility in
`check` is diagnostic; it does not prove GPU access or memory admission works
inside a container.

The runner requires outbound HTTPS to GitHub; acquisition will additionally need
the image registry and Hugging Face. See [GitHub's runner requirements](https://docs.github.com/en/actions/reference/runners/self-hosted-runners).

## 2. Download the runner

The repository pins runner versions and checksums in
`config/bootstrap-versions.env`. Install the pinned Linux x64 or arm64 runner:

```sh
bash scripts/bootstrap-runner.sh install-runner
```

The script downloads from `github.com/actions/runner`, checks the supplied SHA256,
and extracts only into an empty runner directory. Repeated installation refuses
to overwrite existing state. Use GitHub's runner update process for upgrades.
If native dependencies are missing, resolve them using GitHub's instructions
before proceeding.

For a reviewed newer release, update the pins or pass `install-runner VERSION
SHA256` explicitly. Obtain checksums from the official
[runner releases](https://github.com/actions/runner/releases). After publishing
the repository, use **Settings → Actions → Runners → New self-hosted runner**
for its registration token and any host-specific instructions. See
[GitHub's setup instructions](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners).

## 3. Register and start

```sh
bash scripts/bootstrap-runner.sh register https://github.com/OWNER/hrx-systems-test
bash scripts/bootstrap-runner.sh start
```

Registration invokes GitHub's interactive configurator; paste the registration
token at its prompt rather than saving it in the repository or shell history.
The script adds the `hrx-systems-test-bootstrap` label and configures the external
work directory. Set `HRX_RUNNER_NAME` before registration if a custom name is
needed. Keep the foreground agent running while dispatching a check; Ctrl-C stops
it. Existing registrations are preserved, never silently replaced.

For unattended operation later, stop the foreground agent, then use GitHub's
service scripts from the runner directory:

```sh
cd "$HRX_SYSTEMS_ROOT/runner"
sudo ./svc.sh install "$(id -un)"
sudo ./svc.sh start
sudo ./svc.sh status
```

This service setup is a separate host administration action, not performed by
the bootstrap. See [GitHub's service documentation](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/configure-the-application).
Keep `HRX_SYSTEMS_ROOT` configured consistently for interactive and workflow use.

## 4. Verify that GitHub assigns and executes work

Publish `.github/workflows/runner-pickup.yml` on the repository's default branch.
With the registered agent online, choose **Actions → Runner pickup test → Run
workflow**. No repository variables, checkout, Docker image or GPU are required.
The job targets the `hrx-systems-test-bootstrap` label added during registration.

Alternatively, with the GitHub CLI authenticated:

```sh
gh workflow run runner-pickup.yml --repo OWNER/hrx-systems-test
gh run list --repo OWNER/hrx-systems-test --workflow runner-pickup.yml --limit 5
gh run watch RUN_ID --repo OWNER/hrx-systems-test --exit-status
```

Success is a green job with a **Runner pickup succeeded** summary and a
`runner-pickup-RUN_ID-ATTEMPT` artifact containing the runner identity, commit,
run ID and execution timestamp. This proves GitHub assigned the job, the agent
executed shell work, and the artifact upload completed. It does not establish
container/GPU/model readiness.

If it remains **Queued**, check that the agent is online, registered to this
repository, and has all three requested labels (`self-hosted`, `linux`, and
`hrx-systems-test-bootstrap`); also check whether it is busy with another job.
The five-minute execution timeout does not bound the time spent queued. A job
that executes but fails uploading evidence indicates a separate artifact/network
problem; inspect its execution log and summary.

The repository must be published and the runner registered before this can run
on GitHub. Neither operation is performed by adding the workflow file.

## 5. Test the container storage boundary

The repository pins an official Alpine image by digest for the storage check:

```sh
bash scripts/check-container-storage.sh
```

Override `BOOTSTRAP_IMAGE` with another reviewed digest-pinned Linux image
containing `/bin/sh` and `cat` if needed.

The check uses tiny temporary fixtures under the external state root. A non-root
container, with no network or extra capabilities, reads a read-only input mount,
fails to write into it, and writes to its separate output mount. Owned containers
and fixtures are removed afterward; no real cache assets are modified. Docker
may pull the selected image if it is absent. This does not exercise actual
`loom-fetch` staging, GPU drivers, VRAM measurement, inference or cache eviction.

For the GitHub path, set repository Actions variables:

- `HRX_SYSTEMS_ROOT`: the same absolute external directory on the runner.
- `BOOTSTRAP_IMAGE`: optional override of the pinned smoke-test image.

Publish `.github/workflows/runner-bootstrap.yml` on the default branch, then
select **Actions → Runner bootstrap check → Run workflow**. It uses the same
checks as local execution, with a ten-minute limit and serialized dispatches.
It has no push, pull-request or schedule trigger. The host orchestration invokes
Docker; the storage-check workload receives neither host control access nor
the runner's credentials. See [manual workflow dispatch](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow).

## Next design and validation checkpoint

Once the runner smoke check succeeds, decide the pinned Loom image and GPU
device mapping, then validate GPU telemetry and fresh pipeline acquisition with
persistent assets. Start with one small model and verify a second run reuses its
weights/tokenizer while downloading its pipeline again. The cache policy and
model admission design remain in [the working design](design.md).

To stop using this runner, stop its agent/service and remove its registration
through GitHub's documented runner removal process. Do not delete shared cached
assets as part of unregistering an agent.

## Optional dedicated account (administrator commands)

The initial test can use the current account and a temporary foreground agent.
Before moving to a dedicated account, stop that agent so both registrations do
not compete for the same label. The administrator can run the following from
the committed repository checkout on this Linux host:

```sh
sudo useradd --create-home --shell /bin/bash hrx-runner
sudo usermod -aG docker,video,render hrx-runner
sudo install -d -o hrx-runner -g hrx-runner /home/hrx-runner/hrx-systems-test
git archive HEAD | sudo -u hrx-runner tar -x -C /home/hrx-runner/hrx-systems-test
sudo -iu hrx-runner
```

The Docker group grants host-level control; this account is trusted runner
infrastructure, not an isolation boundary. The `video` and `render` groups are
for later GPU validation. Verify group names on a different host before use.
The archive copies committed setup files without copying personal GitHub
credentials or the existing runner registration.

In the new account's shell:

```sh
cd /home/hrx-runner/hrx-systems-test
export HRX_SYSTEMS_ROOT="$HOME/.local/share/hrx-systems-test"
bash scripts/bootstrap-runner.sh check
bash scripts/bootstrap-runner.sh prepare
bash scripts/bootstrap-runner.sh install-runner
bash scripts/bootstrap-runner.sh register https://github.com/rsuderman/hrx-systems-test
bash scripts/bootstrap-runner.sh start
```

Get a new registration token from the repository settings as the owner; do not
copy the personal account's GitHub CLI authentication into the runner account.
Install a persistent service only if desired using the earlier service commands,
run by the administrator for this new account. Initial testing requires no
service and no scheduled workflow.

For retirement, stop the owned agent and remove its repository registration in
**Settings → Actions → Runners**, or use GitHub's removal instructions shown
there. Remove only this runner's external state after confirming it has stopped;
cached assets can be retained or moved separately. Removing the runner does not
require deleting the GitHub repository or its test evidence.
