#!/usr/bin/env bash
# A tiny Docker/mount smoke test. No GPU, runtime or model payloads are needed.
set -euo pipefail
root="${HRX_SYSTEMS_ROOT:-$HOME/.local/share/hrx-systems-test}"
source "$(dirname -- "${BASH_SOURCE[0]}")/../config/bootstrap-versions.env"
image="${BOOTSTRAP_IMAGE:-$STORAGE_CHECK_IMAGE}"
[[ "$root" = /* && "$root" != / ]] || { echo 'Invalid HRX_SYSTEMS_ROOT' >&2; exit 1; }
[[ "$image" =~ ^[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[a-f0-9]{64}$ ]] || {
  echo 'Set BOOTSTRAP_IMAGE to a trusted Linux image pinned by digest, with /bin/sh and cat.' >&2
  exit 1
}
mkdir -p "$root/cache/bootstrap" "$root/results"
assets="$(mktemp -d "$root/cache/bootstrap/check.XXXXXXXX")"
results="$(mktemp -d "$root/results/bootstrap.XXXXXXXX")"
container="hrx-storage-check-$(id -u)-$$"
cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  rm -rf -- "$assets" "$results"
}
trap cleanup EXIT
printf 'cached-asset\n' > "$assets/fixture.txt"
docker run --rm --name "$container" --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" --entrypoint /bin/sh \
  --mount "type=bind,src=$assets,dst=/assets,readonly" \
  --mount "type=bind,src=$results,dst=/results" \
  "$image" -ec '
    test "$(cat /assets/fixture.txt)" = cached-asset
    if (echo forbidden > /assets/unexpected) 2>/dev/null; then
      echo "Cache was writable" >&2; exit 1
    fi
    echo storage-check-passed > /results/status.txt
  '
test "$(cat "$results/status.txt")" = storage-check-passed
echo 'Container read-only input and writable output checks passed.'
