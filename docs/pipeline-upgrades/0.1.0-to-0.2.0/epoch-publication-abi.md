# Match epoch publication to the selected SDK

[Upstream PR #3125](https://github.com/ROCm/hrx-system/pull/3125) adds a leading
borrowed `control_state: buffer` to epoch publication. The newer native host
expects 13 arguments from `text_publish_epoch_v1`; older builds expect 12. Both
contracts appeared under SDK version `0.2.0.dev0`, so the version label cannot
select the rewrite.

Preparation reads two bounded native source files at the selected SDK's exact
`tested_sha`: text `control.c` (types/binding) and `model.c` (caller). The narrow
recognizer accepts only the reviewed declaration and matching borrowed-state
caller shapes. It records `legacy-12`, `control-state-13`, or a precise
unavailable/unsupported result. The existing official artifact-to-source
association is retained; this does not independently attest binary contents.

Automatic 0.2 preparation freezes a schema-2 **runtime policy**, receipt and exact
source bytes under `policies/publication-abi/`. Diagnostic names `control.c.txt`
and `model.c.txt` preserve source bytes while fitting existing report/capture
allowlists. Their paths, sizes, hashes, SDK source, wheel hash and runtime identity
are checked offline. This policy schema is unrelated to model-card schema 2.

For `control-state-13`, the pure upgrader adds and forwards a fresh leading buffer
through the complete eight-site provider/declaration/text-and-sequence-wrapper
closure. Existing provider body, model math, results and locks remain unchanged.
The provider may leave the new state unused; no timing or history policy is added.
Partial or unfamiliar closures and incompatible reverse targets require manual
review. A second application is idempotent with the same explicit target.

Standalone commands preserve the historical `legacy-12` default. Select the new
contract explicitly when using a compatible newer SDK:

```sh
python3 scripts/upgrade-pipelines-0.1.0-to-0.2.0.py SOURCE --check \
  --epoch-publication-abi control-state-13
```

CI passes its frozen target explicitly; it does not guess from pipeline source.
The report records `epoch_publication_abi`, edit counts and implementation identity.
A valid unknown/unavailable SDK contract fails preparation only for models needing
this closure. Decision models without the closure can continue. Modified or
missing frozen evidence is an integrity error, not an unknown-contract fallback.

Release 0.1.0 and explicit raw `none` retain their previous behavior and do not
acquire this evidence. Already sealed schema-1 policies retain their historical
validation/execution behavior without downloads or rewriting; an incompatible old
bundle is not silently repaired. The new migration clears the known arity blocker;
it is not a claim that all affected models execute successfully.
