#!/usr/bin/env bash
# Bootstrap the host runner only. No model downloads or serving installation.
set -euo pipefail

root="${HRX_SYSTEMS_ROOT:-$HOME/.local/share/hrx-systems-test}"
runner="$root/runner"

die() { echo "Error: $*" >&2; exit 1; }
usage() {
  cat <<'EOF'
Usage: scripts/bootstrap-runner.sh COMMAND [ARGS]
  check                       Check Linux, tools, Docker access and visible GPUs
  prepare                     Create external runner/cache/work/results directories
  install-runner [VERSION SHA] Download and verify the pinned official Linux runner
  register REPOSITORY_URL     Register interactively (GitHub asks for the token)
  start                       Run the registered agent in the foreground

HRX_SYSTEMS_ROOT selects an absolute external state directory.
Default: $HOME/.local/share/hrx-systems-test
Run as the intended non-root runner account. No sudo or package installs are run.
EOF
}

[[ "$root" = /* && "$root" != / ]] || die 'HRX_SYSTEMS_ROOT must be an absolute directory other than /'
case "${1:-help}" in
  help|--help|-h) usage ;;
  check)
    [[ "$(uname -s)" = Linux ]] || die 'Linux is required'
    [[ "$(id -u)" != 0 ]] || die 'use the intended non-root runner account'
    for tool in curl tar sha256sum docker timeout; do
      command -v "$tool" >/dev/null || die "missing prerequisite: $tool"
    done
    case "$(uname -m)" in x86_64|aarch64) ;; *) die 'supported architectures: x86_64, aarch64' ;; esac
    timeout 20 docker info --format 'Docker server: {{.ServerVersion}}'
    printf 'State root: %s\n' "$root"
    printf 'GPU device nodes (visibility only, not qualification):\n'
    for device in /dev/kfd /dev/dri/renderD* /dev/nvidia[0-9]*; do
      [[ ! -e "$device" ]] || ls -l "$device"
    done
    echo 'Host prerequisites passed. GPU access, VRAM and Loom Serve still need qualification.'
    ;;
  prepare)
    mkdir -p "$runner" "$root/cache/huggingface" "$root/cache/loom-assets" \
      "$root/work" "$root/runs" "$root/results"
    echo "Prepared external state at $root"
    ;;
  install-runner)
    [[ $# = 1 || $# = 3 ]] || die 'install-runner accepts no arguments or VERSION SHA256'
    if [[ $# = 1 ]]; then
      source "$(dirname -- "${BASH_SOURCE[0]}")/../config/bootstrap-versions.env"
      version="$RUNNER_VERSION"
      case "$(uname -m)" in
        x86_64) expected="$RUNNER_SHA256_X64" ;;
        aarch64) expected="$RUNNER_SHA256_ARM64" ;;
        *) die 'unsupported architecture' ;;
      esac
    else
      version="$2"; expected="$3"
    fi
    [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 'invalid runner version'
    [[ "$expected" =~ ^[a-fA-F0-9]{64}$ ]] || die 'invalid SHA256'
    case "$(uname -s):$(uname -m)" in
      Linux:x86_64) arch=x64 ;; Linux:aarch64) arch=arm64 ;;
      *) die 'unsupported platform' ;;
    esac
    mkdir -p "$runner"
    [[ -z "$(ls -A "$runner")" ]] || die 'runner directory is not empty; reuse it or select a new state root'
    archive="$(mktemp)"
    trap 'rm -f -- "$archive"' EXIT
    curl --fail --location --proto '=https' --tlsv1.2 --retry 3 \
      "https://github.com/actions/runner/releases/download/v$version/actions-runner-linux-$arch-$version.tar.gz" \
      --output "$archive"
    printf '%s  %s\n' "$expected" "$archive" | sha256sum --check --status || die 'runner checksum mismatch'
    tar -xzf "$archive" -C "$runner"
    echo 'Runner extracted. Next: register with your repository URL.'
    ;;
  register)
    [[ $# = 2 ]] || die 'register requires https://github.com/OWNER/REPOSITORY'
    [[ "$2" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?$ ]] || die 'expected a GitHub.com repository URL'
    [[ -x "$runner/config.sh" ]] || die 'install-runner must be completed first'
    [[ ! -f "$runner/.runner" ]] || die 'runner is already registered; do not overwrite it'
    [[ "$(id -u)" != 0 ]] || die 'register as the intended non-root runner account'
    cd "$runner"
    ./config.sh --url "$2" --name "${HRX_RUNNER_NAME:-$(hostname)-hrx-bootstrap}" \
      --labels hrx-systems-test-bootstrap --work "$root/work"
    ;;
  start)
    [[ -f "$runner/.runner" ]] || die 'register must be completed first'
    cd "$runner"
    exec ./run.sh
    ;;
  *) usage >&2; exit 1 ;;
esac
