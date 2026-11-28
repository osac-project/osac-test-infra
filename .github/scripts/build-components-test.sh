#!/usr/bin/env bash
# Tests for scripts/ec2-runner/build-components.sh with stubbed git, podman, oc and python.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="${SCRIPT_DIR}/../../scripts/ec2-runner/build-components.sh"

WORK="$(mktemp -d)"
trap 'rm -rf -- "${WORK}"' EXIT
mkdir -p "${WORK}/bin"

cat > "${WORK}/bin/git" <<'STUB'
#!/usr/bin/env bash
echo "git $*" >> "${STUB_LOG}"
case "$*" in
  "init "*) mkdir -p "$2" ;;
  *"rev-parse HEAD"*) echo "${STUB_SHA:-abc123}" ;;
esac
STUB
cat > "${WORK}/bin/podman" <<'STUB'
#!/usr/bin/env bash
echo "podman $*" >> "${STUB_LOG}"
case "$1" in
  build)
    sleep "${STUB_BUILD_SLEEP:-0}"
    echo "stub build output for $*"
    exit "${STUB_PODMAN_RC:-0}"
    ;;
  save) echo "image-bytes" ;;
esac
STUB
# Stands in for the system Python: only "-m venv <dir>" does anything.
cat > "${WORK}/bin/fake-python" <<'STUB'
#!/usr/bin/env bash
echo "python3 $*" >> "${STUB_LOG}"
if [[ "${1:-}" == "-m" && "${2:-}" == "venv" ]]; then
  mkdir -p "$3/bin"
  for tool in pip ansible-builder; do
    printf '#!/usr/bin/env bash\necho "venv-%s $*" >> "${STUB_LOG}"\n' "${tool}" > "$3/bin/${tool}"
    chmod +x "$3/bin/${tool}"
  done
fi
STUB
cat > "${WORK}/bin/python3" <<'STUB'
#!/usr/bin/env bash
echo "validator $*" >> "${STUB_LOG}"
exit "${STUB_VALIDATOR_RC:-0}"
STUB
cat > "${WORK}/bin/oc" <<'STUB'
#!/usr/bin/env bash
echo "oc $*" >> "${STUB_LOG}"
case "$*" in
  "get nodes"*) printf 'node-a' ;;
  *) cat > /dev/null ;;
esac
STUB
chmod +x "${WORK}/bin/"*

export PATH="${WORK}/bin:${PATH}"
export STUB_LOG="${WORK}/calls.log"
export COMPONENT_PYTHON="${WORK}/bin/fake-python"
export GITHUB_RUN_ID=777
export OSAC_REPO=https://example.test/osac.git
export OSAC_BRANCH=deadbeef
export COMPONENT_POLL_SECONDS=1

CORE_OVERRIDES='{"fulfillment_service_image":"localhost/component-0:777","osac_operator_image":"localhost/component-1:777","osac_aap_image":"localhost/component-2:777","bmf_operator_image":"localhost/component-3:777"}'

FAILED=0
CURRENT=""
fail() {
  echo "FAIL ${CURRENT}: $*" >&2
  FAILED=1
}
assert_eq() { [[ "$1" == "$2" ]] || fail "$3: expected '$2', got '$1'"; }
assert_contains() { [[ "$1" == *"$2"* ]] || fail "$3: '$2' not found in: $1"; }
count_calls() { grep -c -- "$1" "${STUB_LOG}" || true; }

# Fresh state directory, call log and step-output file for one test.
reset() {
  CURRENT="$1"
  export COMPONENT_STATE_DIR="${WORK}/state-$1"
  export GITHUB_OUTPUT="${WORK}/output-$1"
  rm -rf "${COMPONENT_STATE_DIR}"
  : > "${STUB_LOG}"
  : > "${GITHUB_OUTPUT}"
  unset STUB_PODMAN_RC STUB_BUILD_SLEEP STUB_VALIDATOR_RC
  export COMPONENTS_JSON_INPUT='[]'
}

# Runs the script, capturing combined output in OUT and the exit code in RC.
run() {
  RC=0
  OUT="$(bash "${SCRIPT}" "$@" 2>&1)" || RC=$?
}

test_build_core_only() {
  reset build_core_only
  run build
  assert_eq "${RC}" 0 "exit code (${OUT})"
  assert_eq "$(jq -cS . "${COMPONENT_STATE_DIR}/overrides.json" 2>&1)" "$(jq -cS . <<<"${CORE_OVERRIDES}")" "overrides.json"
  assert_eq "$(tr '\n' ' ' < "${COMPONENT_STATE_DIR}/images.txt" 2>&1)" \
    "localhost/component-0:777 localhost/component-1:777 localhost/component-2:777 localhost/component-3:777 " "images.txt"
  assert_eq "$(count_calls '^podman build')" 4 "podman build count"
  assert_eq "$(count_calls '^podman build --env GODEBUG=http2client=0 --unsetenv GODEBUG')" 3 "Containerfile builds with GODEBUG flags"
  assert_eq "$(count_calls '^git .* fetch ')" 4 "git fetch count"
  # The first clone resolves OSAC_BRANCH; the other three reuse the commit it resolved to.
  assert_eq "$(count_calls '^git .* fetch .* deadbeef$')" 1 "git fetches of OSAC_BRANCH"
  assert_eq "$(count_calls '^git .* fetch .* abc123$')" 3 "git fetches of the commit the first clone resolved"
  assert_eq "$(count_calls '^validator ')" 3 "Containerfile context validations"
  local last_validator first_build
  last_validator=$(grep -n '^validator ' "${STUB_LOG}" | tail -n 1 | cut -d: -f1)
  first_build=$(grep -n '^podman build' "${STUB_LOG}" | head -n 1 | cut -d: -f1)
  [[ "${last_validator}" -lt "${first_build}" ]] || fail "a build started before every context was validated"
}

test_build_ignores_overridden_core_keys() {
  reset build_ignores_overridden_core_keys
  export COMPONENTS_JSON_INPUT='[{"repo":"x/y","ref":"main","imageKey":"service.images.service"},{"repo":"x/y","ref":"main","imageKey":"aap.configAsCode.eeImage"}]'
  run build
  assert_eq "${RC}" 0 "exit code (${OUT})"
  assert_eq "$(count_calls '^podman build')" 4 "podman build count"
  assert_eq "$(jq -cS . "${COMPONENT_STATE_DIR}/overrides.json" 2>&1)" "$(jq -cS . <<<"${CORE_OVERRIDES}")" "overrides.json"
}

test_build_rejects_unknown_image_key() {
  reset build_rejects_unknown_image_key
  export COMPONENTS_JSON_INPUT='[{"repo":"x/y","ref":"main","imageKey":"nope.key"}]'
  run build
  [[ "${RC}" -ne 0 ]] || fail "expected a non-zero exit"
  assert_contains "${OUT}" "nope.key" "error output"
  assert_eq "$(count_calls '^podman build')" 0 "podman build count"
}

test_build_uses_venv_for_ansible_builder() {
  reset build_uses_venv_for_ansible_builder
  run build
  assert_eq "${RC}" 0 "exit code (${OUT})"
  [[ "$(count_calls '^venv-pip install')" -ge 1 ]] || fail "ansible-builder was not installed with the venv pip"
  [[ "$(count_calls '^venv-ansible-builder create')" -ge 1 ]] || fail "ansible-builder was not run from the venv"
  assert_eq "$(count_calls '^python3 -m pip')" 0 "pip installs into the system Python"
}

test_build_fails_when_podman_fails() {
  reset build_fails_when_podman_fails
  export STUB_PODMAN_RC=1
  run build
  [[ "${RC}" -ne 0 ]] || fail "expected a non-zero exit"
  [[ ! -e "${COMPONENT_STATE_DIR}/overrides.json" ]] || fail "overrides.json was written after a failed build"
}

test_build_fails_before_build_when_context_is_invalid() {
  reset build_fails_before_build_when_context_is_invalid
  export STUB_VALIDATOR_RC=1
  run build
  [[ "${RC}" -ne 0 ]] || fail "expected a non-zero exit"
  assert_eq "$(count_calls '^podman build')" 0 "podman build count"
}

test_requires_state_dir() {
  reset requires_state_dir
  unset COMPONENT_STATE_DIR
  run build
  [[ "${RC}" -ne 0 ]] || fail "expected a non-zero exit"
  assert_contains "${OUT}" "COMPONENT_STATE_DIR" "error output"
}

# Waits up to 15 seconds for the detached builder to record its exit code.
wait_for_status() {
  for _ in $(seq 1 150); do
    [[ -f "${COMPONENT_STATE_DIR}/status" ]] && return 0
    sleep 0.1
  done
  fail "the detached build never wrote a status"
}

test_start_returns_before_build_finishes() {
  reset start_returns_before_build_finishes
  export STUB_BUILD_SLEEP=1
  local began=${SECONDS}
  run start
  assert_eq "${RC}" 0 "exit code (${OUT})"
  [[ $((SECONDS - began)) -lt 2 ]] || fail "start took $((SECONDS - began))s, it must not wait for the build"
  [[ -s "${COMPONENT_STATE_DIR}/build.pid" ]] || fail "build.pid is missing"
  [[ ! -e "${COMPONENT_STATE_DIR}/status" ]] || fail "status exists before the build can have finished"
  wait_for_status
  assert_eq "$(cat "${COMPONENT_STATE_DIR}/status" 2>&1)" 0 "status"
}

test_load_success() {
  reset load_success
  run start
  wait_for_status
  run load
  assert_eq "${RC}" 0 "exit code (${OUT})"
  assert_contains "${OUT}" "stub build output for" "build log in the load output"
  assert_eq "$(count_calls '^oc debug node/node-a ')" 4 "oc debug count"
  assert_eq "$(count_calls '^podman save localhost/component-')" 4 "podman save count"
  assert_eq "$(cat "${GITHUB_OUTPUT}")" "overrides=${CORE_OVERRIDES}" "step output"
}

test_load_waits_for_running_build() {
  reset load_waits_for_running_build
  export STUB_BUILD_SLEEP=1
  run start
  run load
  assert_eq "${RC}" 0 "exit code (${OUT})"
  assert_eq "$(count_calls '^oc debug node/node-a ')" 4 "oc debug count"
}

test_load_fails_on_build_failure() {
  reset load_fails_on_build_failure
  export STUB_PODMAN_RC=1
  run start
  wait_for_status
  run load
  [[ "${RC}" -ne 0 ]] || fail "expected a non-zero exit"
  assert_contains "${OUT}" "stub build output for" "build log in the load output"
  assert_eq "$(count_calls '^oc debug')" 0 "oc debug count"
  assert_eq "$(cat "${GITHUB_OUTPUT}")" "" "step output"
}

test_load_fails_when_builder_died() {
  reset load_fails_when_builder_died
  mkdir -p "${COMPONENT_STATE_DIR}"
  bash -c 'exit 0' &
  local dead=$!
  wait "${dead}"
  echo "${dead}" > "${COMPONENT_STATE_DIR}/build.pid"
  local began=${SECONDS}
  COMPONENT_WAIT_SECONDS=60 run load
  [[ "${RC}" -ne 0 ]] || fail "expected a non-zero exit"
  [[ $((SECONDS - began)) -lt 5 ]] || fail "load waited $((SECONDS - began))s for a builder that is gone"
  assert_eq "$(count_calls '^oc debug')" 0 "oc debug count"
}

test_load_fails_when_build_never_started() {
  reset load_fails_when_build_never_started
  COMPONENT_WAIT_SECONDS=60 run load
  [[ "${RC}" -ne 0 ]] || fail "expected a non-zero exit"
  assert_eq "$(count_calls '^oc debug')" 0 "oc debug count"
}

test_load_times_out() {
  reset load_times_out
  mkdir -p "${COMPONENT_STATE_DIR}"
  sleep 30 &
  local alive=$!
  echo "${alive}" > "${COMPONENT_STATE_DIR}/build.pid"
  COMPONENT_WAIT_SECONDS=2 run load
  kill "${alive}" 2> /dev/null || true
  [[ "${RC}" -ne 0 ]] || fail "expected a non-zero exit"
  assert_contains "${OUT}" "did not finish within 2 seconds" "error output"
  assert_eq "$(count_calls '^oc debug')" 0 "oc debug count"
}

test_abort_stops_running_build_and_prints_log() {
  reset abort_stops_running_build_and_prints_log
  export STUB_BUILD_SLEEP=20
  run start
  local pid
  pid=$(cat "${COMPONENT_STATE_DIR}/build.pid")
  for _ in $(seq 1 50); do
    grep -q 'Build component\[0\]' "${COMPONENT_STATE_DIR}/build.log" 2> /dev/null && break
    sleep 0.1
  done
  run abort
  assert_eq "${RC}" 0 "exit code (${OUT})"
  assert_contains "${OUT}" "Build component[0]" "build log in the abort output"
  for _ in $(seq 1 30); do
    kill -0 "${pid}" 2> /dev/null || break
    sleep 0.1
  done
  ! kill -0 "${pid}" 2> /dev/null || fail "the builder is still running after abort"
}

test_abort_does_not_repeat_a_printed_log() {
  reset abort_does_not_repeat_a_printed_log
  run start
  wait_for_status
  run load
  run abort
  assert_eq "${RC}" 0 "exit code (${OUT})"
  [[ "${OUT}" != *"stub build output for"* ]] || fail "abort printed a build log that load already printed"
}

test_abort_without_a_build() {
  reset abort_without_a_build
  run abort
  assert_eq "${RC}" 0 "exit code (${OUT})"
}

test_build_core_only
test_start_returns_before_build_finishes
test_load_success
test_load_waits_for_running_build
test_load_fails_on_build_failure
test_load_fails_when_builder_died
test_load_fails_when_build_never_started
test_load_times_out
test_abort_stops_running_build_and_prints_log
test_abort_does_not_repeat_a_printed_log
test_abort_without_a_build
test_build_ignores_overridden_core_keys
test_build_rejects_unknown_image_key
test_build_uses_venv_for_ansible_builder
test_build_fails_when_podman_fails
test_build_fails_before_build_when_context_is_invalid
test_requires_state_dir

if [[ "${FAILED}" -ne 0 ]]; then
  echo "build-components tests FAILED" >&2
  exit 1
fi
echo "build-components tests passed"
