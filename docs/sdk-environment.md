# Per-run SDK environment

The private `python/hrx_systems/sdk/environment.py` helper implements `hst-utq.1`. Each
harness attempt downloads its selected HRX release and locked dependencies into
temporary run storage. Only model assets use persistent caching.

The baseline pins the Python base image, HRX release and 15 dependency wheels.
URLs, sizes and SHA256 hashes are explicit; hashes are verified before install.
There is no persistent SDK cache, cache-hit logic or latest-build lookup.

`acquire --baseline FILE --payload-root NEW_DIRECTORY --output NEW_RESULTS`
downloads into fresh `hrx/` and `python/` subdirectories. Existing payload roots
are refused rather than reused. The acquisition receipt records downloaded bytes.

`install --baseline FILE --payload-root DIRECTORY --output NEW_RESULTS` runs
inside the pinned container. Mount scripts/config and the downloaded payloads
read-only, with separate writable `/run` and scratch. Installation verifies the
payloads again and creates `/run/venv` offline using the locked wheels. Every
container creates its own environment; never relocate an installed venv.

Optional `--card /pipeline/model.json` parses/selects that card and its adjacent
lock and runs offline listing. Native tools are checked using their explicit
venv paths. No model payload is fetched and no server is started.

Results are `sdk-acquisition.json`, `sdk.json`/`sdk-install.log`, or
`sdk-error.json` on failure. They record the selected release and observed tool,
package and import identities. The container owner verifies the actual base
image digest. Delete SDK payloads and installed environments with run cleanup;
retain the small receipts and logs, not the wheels.

These checks cover SDK installation and card metadata, not pipeline compilation,
GPU loading or inference. Model seeding and serving remain separate tasks.
