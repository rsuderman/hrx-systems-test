# Refresh model publications

Create a proposed revision/catalog update from hosted model cards:

```sh
bash scripts/cache/refresh-publications.sh plan \
  --author loom-fun --root /srv/loom-models --include-new \
  --output "$attempt/refresh"
```

The output directory must be new. Read `summary.md` and `configuration.diff`.
`plan.json` retains exact commits, card/lock bytes and hashes, SDK inspection
identity, original configuration, proposed file contents and receipt aliases.
`suite.json` references candidate receipts outside the active cached inventory.

Planning reads metadata and uses one disposable SDK environment to validate cards,
selections and locked asset identities. It never downloads GGUFs, seeds the model
cache or starts a model server. SDK payloads are acquired per attempt and removed;
the inspection container has no network, GPU or model-cache mount. Docker and the
pinned base image are required. `--runtime FILE` selects a supported runtime input
for this inspection; the default is the repository's pinned seeding SDK. An
unsupported card is reported rather than interpreted through guessed schema rules.

Existing selections retain model ID, preset and variant. Missing/private repositories
stay in the catalog and are reported unavailable. Exact-revision test/request
overrides are preserved and copied to updated identities without changing their
values. Historical CI exclusions are retained; they are not extended to new revisions.

`--include-new` discovers additional eligible source/text-or-decision selections
within the existing under-20GB GGUF catalog limit. It adds catalog entries, not
entries in the representative default sample suite. A card's unique supported
choice is selected; ambiguous choices are reported as `needs_selection`. Resolve
one explicitly and make a new plan:

```sh
bash scripts/cache/refresh-publications.sh plan --include-new \
  --select 'loom-fun/example-loom=chat,gguf' \
  --root /srv/loom-models --output "$attempt/selected-refresh"
```

Repeat `--model REPOSITORY` to restrict the plan to named existing/discovered
publications. Unknown asset sizes cannot pass the catalog size filter. The plan
reports locked assets missing from known seed receipts; this is an estimate based
on receipts, not a scan or hash of all cached weights.

Apply a saved plan in its originating checkout:

```sh
bash scripts/cache/refresh-publications.sh apply --plan "$attempt/refresh/plan.json"
```

Apply is offline and uses the saved commits. It updates the declared configuration
files only, checks original/planned file hashes, rejects intervening edits and
restores original bytes if publication fails. An already-applied plan is a no-op.
The digest detects inconsistent or accidentally changed plan data; it is not a
cryptographic signature from the model publisher. Use locally generated plans
whose proposed changes you have inspected.

To reconcile a tested candidate with ready/held decisions, `--only FILE` accepts a
JSON array of the plan's ready `selection_id` values. It regenerates the configuration
from the saved originals and selected rows, rather than requiring manual revision
edits. It accepts original or fully proposed configuration as its starting state;
unrelated edits still fail. An empty array restores original selection contents.

Applying does not activate seed receipts. Use the generated suite with the existing
explicit host seed commands. After seeding, freeze those candidate receipts for
normal cached-mode preparation:

```sh
bash scripts/cache/refresh-publications.sh snapshot \
  --plan "$attempt/refresh/plan.json" --root /srv/loom-models \
  --output "$attempt/candidate-selection.json"
bash scripts/test/prepare-cached-models.sh --root /srv/loom-models \
  --selection-snapshot "$attempt/candidate-selection.json" \
  --runtime-selection "$attempt/runtime-selection.json" --output "$attempt/prepared"
```

The full configuration plan must already be applied in this candidate checkout.
The read-only `snapshot` operation verifies each candidate receipt against the
saved card/lock and selection identities, then uses the existing cached snapshot
validator. Preparation consequently derives text/decision service kind, published
execution and strict defaults from the verified card, just as ordinary cached CI
does. This avoids treating arbitrary new models as authored text-only sample
baselines. It does not change live inventory discovery. `snapshot --only FILE`
restricts it to explicitly selected candidate IDs when other seed attempts failed;
keep those failures visible in the separate plan/result matrix.

Then run/report against the prepared inputs.
Those steps own asset acquisition, local compatibility results and the later
activation of all receipt aliases. Existing cache entries stay active until that
activation. The refresh command does not remove models, modify the host, commit,
push or silently bring new weights into CI.
