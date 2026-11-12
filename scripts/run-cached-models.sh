#!/usr/bin/env bash
# Compose the independently runnable preparation, execution and reporting stages.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
root="${HRX_SYSTEMS_ROOT:-$HOME/.local/share/hrx-systems-test}"
output=""
suite=""
selection=""
runtime_baseline=""
runtime=""
runtime_source_args=()
device_uri="amdgpu://0"
cycles=1
keep_prepared=false
full_integrity=false
usage() {
  cat <<'HELP'
Usage: bash scripts/run-cached-models.sh [--root CACHE_ROOT] [--suite SUITE_JSON | --selection cached]
       [--runtime RUNTIME_CONFIG | --runtime-baseline SDK_CONFIG] [--full-integrity]
       [--runtime-source release|actions-main|actions-replay]
       [--runtime-run-id ID --runtime-run-attempt N --runtime-artifact-id ID --runtime-wheel-sha256 SHA256]
       [--output NEW_ATTEMPT] [--device-uri URI] [--cycles COUNT] [--keep-prepared]
Runs the same Prepare, Run and Report Bash commands used by GitHub Actions.
--output should be runner-owned when the model cache belongs to another account.
HELP
}
while (($#)); do
  case "$1" in
    --root|--suite|--selection|--runtime|--runtime-baseline|--output|--device-uri|--cycles|--runtime-source|--runtime-run-id|--runtime-run-attempt|--runtime-artifact-id|--runtime-wheel-sha256)
      (($# >= 2)) || { usage >&2; exit 2; }
      case "$1" in
        --root) root="$2";; --suite) suite="$2";; --output) output="$2";;
        --selection) selection="$2";; --runtime) runtime="$2";; --runtime-baseline) runtime_baseline="$2";;
        --device-uri) device_uri="$2";; --cycles) cycles="$2";;
        --runtime-source|--runtime-run-id|--runtime-run-attempt|--runtime-artifact-id|--runtime-wheel-sha256) runtime_source_args+=("$1" "$2");;
      esac
      shift 2;;
    --keep-prepared) keep_prepared=true; shift;;
    --full-integrity) full_integrity=true; shift;;
    -h|--help) usage; exit 0;;
    *) usage >&2; exit 2;;
  esac
done
[[ "$root" = /* && "$root" != / ]] || { echo 'Cache root must be an absolute directory other than /' >&2; exit 2; }
[[ "$cycles" =~ ^[1-9][0-9]*$ ]] || { echo '--cycles must be positive' >&2; exit 2; }
[[ -z "$selection" || "$selection" = cached ]] || { echo '--selection must be cached' >&2; exit 2; }
[[ -z "$selection" || -z "$suite" ]] || { echo '--selection and --suite are mutually exclusive' >&2; exit 2; }
[[ -z "$runtime" || -z "$runtime_baseline" ]] || { echo '--runtime and --runtime-baseline are mutually exclusive' >&2; exit 2; }
[[ -z "$runtime_baseline" || "$selection" = cached ]] || { echo '--runtime-baseline requires --selection cached' >&2; exit 2; }
output="${output:-$root/runs/suite-$(date -u +%Y%m%dT%H%M%S)-$$}"
[[ "$output" = /* && "$output" != / ]] || { echo '--output must be an absolute new directory' >&2; exit 2; }
mkdir -p -- "$(dirname -- "$output")"
mkdir -- "$output"
output="$(cd -- "$output" && pwd -P)"
prepare_status=null
run_status=null
cleanup_status=null
report_status=null
package_status=0
child_pid=""
current_stage=""
interrupted=0

write_status() {
  python3 -I - "$output/stages.json" "$prepare_status" "$run_status" "$cleanup_status" "$report_status" "$interrupted" <<'PY'
import json, pathlib, sys
names = ('prepare', 'run', 'cleanup', 'report')
result = {'schema_version': 1, 'interrupted': int(sys.argv[6]), 'stages': {}}
for name, value in zip(names, sys.argv[2:6]):
    code = None if value == 'null' else int(value)
    result['stages'][name] = {'exit_code': code,
        'status': 'not_run' if code is None else ('passed' if code == 0 else 'failed')}
pathlib.Path(sys.argv[1]).write_text(json.dumps(result, indent=2) + '\n')
PY
}
run_stage() {
  local name="$1"
  current_stage="$name"
  shift
  if [[ "$name" != prepare ]]; then
    set -- env -u HRX_ACTIONS_TOKEN "$@"
  fi
  echo "Starting $name; log: $output/$name.log"
  python3 -u -I "$script_dir/ci-progress.py" --label "$name" --log "$output/$name.log" -- "$@" &
  child_pid=$!
  local result=0
  wait "$child_pid" || result=$?
  child_pid=""
  printf -v "${name}_status" '%s' "$result"
  current_stage=""
  write_status
  echo "$name exited $result"
}
package_failures() {
  local result=0
  env -u HRX_ACTIONS_TOKEN bash "$script_dir/package-model-failures.sh" --attempt "$output" --output "$output/failures" --zip "$output/model-failures.zip" >>"$output/package.log" 2>&1 || result=$?
  if ((result != 0)); then
    package_status="$result"
    echo "Failure packaging exited $result; see $output/package.log" >&2
  fi
}
on_signal() {
  local code="$1"
  trap '' INT TERM
  interrupted="$code"
  [[ -z "$current_stage" ]] || printf -v "${current_stage}_status" '%s' "$code"
  if [[ -n "$child_pid" ]]; then
    kill -TERM "$child_pid" 2>/dev/null || true
    for ((i=0; i<20; i++)); do
      kill -0 "$child_pid" 2>/dev/null || break
      sleep 1
    done
    kill -KILL "$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
    child_pid=""
  fi
  # The execution owner validates the derived container identity before removal.
  if [[ -d "$output/results" ]]; then
    run_stage cleanup bash "$script_dir/run-prepared-models.sh" --cleanup --output "$output/results"
  fi
  write_status
  package_failures
  if [[ -f "$output/results/suite-result.json" ]]; then
    run_stage report bash "$script_dir/generate-model-report.sh" --results "$output/results" --output "$output/report" --failures "$output/failures/failures.json"
  fi
  write_status
  package_failures
  echo "Interrupted; retained attempt evidence: $output" >&2
  exit "$code"
}
trap 'on_signal 130' INT
trap 'on_signal 143' TERM
python3 -I - "$output/workflow.json" "$(dirname -- "$script_dir")" <<'PY'
import json, os, pathlib, subprocess, sys
value = {'schema_version': 1}
keys = {'repository': 'GITHUB_REPOSITORY', 'run_id': 'GITHUB_RUN_ID',
        'run_attempt': 'GITHUB_RUN_ATTEMPT', 'ref': 'GITHUB_REF',
        'sha': 'GITHUB_SHA', 'runner': 'RUNNER_NAME'}
value.update({key: os.environ[name] for key, name in keys.items() if os.environ.get(name)})
if 'sha' not in value:
    result = subprocess.run(['git', '-C', sys.argv[2], 'rev-parse', 'HEAD'], capture_output=True, text=True)
    if result.returncode == 0:
        value['sha'] = result.stdout.strip()
if value.get('repository') and value.get('run_id'):
    value['run_url'] = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{value['repository']}/actions/runs/{value['run_id']}"
pathlib.Path(sys.argv[1]).write_text(json.dumps(value, indent=2) + '\n')
PY
write_status
prepare_args=(--root "$root" --output "$output/prepared" "${runtime_source_args[@]}")
[[ -z "$suite" ]] || prepare_args+=(--suite "$suite")
[[ -z "$selection" ]] || prepare_args+=(--selection "$selection")
[[ -z "$runtime" ]] || prepare_args+=(--runtime "$runtime")
[[ -z "$runtime_baseline" ]] || prepare_args+=(--runtime-baseline "$runtime_baseline")
[[ "$full_integrity" = false ]] || prepare_args+=(--full-integrity)
run_stage prepare bash "$script_dir/prepare-cached-models.sh" "${prepare_args[@]}"
if [[ -f "$output/prepared/prepared-suite.json" ]]; then
  run_stage run bash "$script_dir/run-prepared-models.sh" --prepared "$output/prepared" \
    --output "$output/results" --workflow "$output/workflow.json" --device-uri "$device_uri" --cycles "$cycles"
  run_stage cleanup bash "$script_dir/run-prepared-models.sh" --cleanup --output "$output/results"
else
  echo "No prepared suite; execution not run. See $output/prepare.log" >&2
fi
write_status
package_failures
if [[ -f "$output/results/suite-result.json" ]]; then
  run_stage report bash "$script_dir/generate-model-report.sh" --results "$output/results" --output "$output/report" --failures "$output/failures/failures.json"
else
  echo "No raw results; report not generated. Stage diagnostics retained in $output" >&2
fi
write_status
package_failures
if [[ "$keep_prepared" = false && "$cleanup_status" = 0 && "$package_status" = 0 ]]; then
  rm -rf -- "$output/prepared"
fi
write_status
printf 'Attempt: %s\n' "$output"
if [[ -f "$output/report/index.html" ]]; then
  printf 'Report: %s\n' "$output/report/index.html"
elif [[ -f "$output/failures/summary.md" ]]; then
  cat "$output/failures/summary.md"
fi
[[ ! -f "$output/model-failures.zip" ]] || printf 'Failures: %s\n' "$output/model-failures.zip"
for result in "$prepare_status" "$run_status" "$cleanup_status" "$report_status" "$package_status"; do
  [[ "$result" != null ]] || exit 1
  ((result == 0)) || exit "$result"
done
