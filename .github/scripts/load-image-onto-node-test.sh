#!/usr/bin/env bash
# Unit tests for load-image-onto-node.sh retry logic.
#
# Stubs out 'podman' and 'oc' with controlled exit codes so we can
# verify retry behaviour, back-off, and final exit status without a
# real cluster.  The 'sleep' command is also stubbed to avoid actual
# delays.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="${SCRIPT_DIR}/load-image-onto-node.sh"

pass=0
fail=0

assert_eq() {
  local name="$1" expected="$2" actual="$3"
  if [[ "${expected}" == "${actual}" ]]; then
    echo "PASS: ${name}"
    pass=$((pass + 1))
  else
    echo "FAIL: ${name} (expected=${expected} actual=${actual})"
    fail=$((fail + 1))
  fi
}

assert_contains() {
  local name="$1" needle="$2" haystack="$3"
  if [[ "${haystack}" == *"${needle}"* ]]; then
    echo "PASS: ${name}"
    pass=$((pass + 1))
  else
    echo "FAIL: ${name} (expected output to contain '${needle}')"
    fail=$((fail + 1))
  fi
}

# ── helpers ──────────────────────────────────────────────────────────
# Create a temp dir with stub binaries; prepend it to PATH so the
# script uses our stubs instead of real podman/oc/sleep.
setup_stubs() {
  STUB_DIR="$(mktemp -d)"
  trap 'rm -rf "${STUB_DIR}"' RETURN

  # podman stub -- always succeeds (the save side is not the failure point)
  cat > "${STUB_DIR}/podman" <<'STUB'
#!/usr/bin/env bash
echo "podman-stub: $*"
exit 0
STUB
  chmod +x "${STUB_DIR}/podman"

  # sleep stub -- no-op to keep tests fast
  cat > "${STUB_DIR}/sleep" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
  chmod +x "${STUB_DIR}/sleep"
}

# ── Test 1: succeeds on first attempt ────────────────────────────────
test_success_first_attempt() {
  local stub_dir
  stub_dir="$(mktemp -d)"

  cat > "${stub_dir}/podman" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
  chmod +x "${stub_dir}/podman"

  cat > "${stub_dir}/oc" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
  chmod +x "${stub_dir}/oc"

  cat > "${stub_dir}/sleep" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
  chmod +x "${stub_dir}/sleep"

  local out rc=0
  out=$(PATH="${stub_dir}:${PATH}" bash "${SCRIPT}" "test-image:latest" "test-node" 3 2>&1) || rc=$?
  rm -rf "${stub_dir}"

  assert_eq "first-attempt-exit-code" "0" "${rc}"
  assert_contains "first-attempt-msg" "attempt 1/3" "${out}"
  assert_contains "first-attempt-success" "successfully" "${out}"
}

# ── Test 2: fails then succeeds (retry works) ───────────────────────
test_retry_then_success() {
  local stub_dir
  stub_dir="$(mktemp -d)"

  cat > "${stub_dir}/podman" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
  chmod +x "${stub_dir}/podman"

  # oc fails twice, then succeeds
  cat > "${stub_dir}/oc" <<'STUB'
#!/usr/bin/env bash
COUNTER_FILE="/tmp/load-image-test-counter"
count=0
if [[ -f "${COUNTER_FILE}" ]]; then
  count=$(cat "${COUNTER_FILE}")
fi
count=$((count + 1))
echo "${count}" > "${COUNTER_FILE}"
if [[ "${count}" -le 2 ]]; then
  echo "Unable to connect to the server: EOF" >&2
  exit 1
fi
exit 0
STUB
  chmod +x "${stub_dir}/oc"

  cat > "${stub_dir}/sleep" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
  chmod +x "${stub_dir}/sleep"

  rm -f /tmp/load-image-test-counter
  local out rc=0
  out=$(PATH="${stub_dir}:${PATH}" bash "${SCRIPT}" "test-image:latest" "test-node" 3 2>&1) || rc=$?
  rm -f /tmp/load-image-test-counter
  rm -rf "${stub_dir}"

  assert_eq "retry-exit-code" "0" "${rc}"
  assert_contains "retry-attempt-3" "attempt 3/3" "${out}"
  assert_contains "retry-success" "successfully on attempt 3" "${out}"
}

# ── Test 3: all attempts fail → exit 1 ──────────────────────────────
test_all_attempts_fail() {
  local stub_dir
  stub_dir="$(mktemp -d)"

  cat > "${stub_dir}/podman" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
  chmod +x "${stub_dir}/podman"

  cat > "${stub_dir}/oc" <<'STUB'
#!/usr/bin/env bash
echo "Unable to connect to the server: EOF" >&2
exit 1
STUB
  chmod +x "${stub_dir}/oc"

  cat > "${stub_dir}/sleep" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
  chmod +x "${stub_dir}/sleep"

  local out rc=0
  out=$(PATH="${stub_dir}:${PATH}" bash "${SCRIPT}" "test-image:latest" "test-node" 3 2>&1) || rc=$?
  rm -rf "${stub_dir}"

  assert_eq "exhaust-exit-code" "1" "${rc}"
  assert_contains "exhaust-error-msg" "Failed to load image after 3 attempts" "${out}"
}

# ── Test 4: missing arguments → exit 1 ──────────────────────────────
test_missing_args() {
  local rc=0
  bash "${SCRIPT}" 2>/dev/null || rc=$?
  assert_eq "missing-args-exit-code" "1" "${rc}"
}

# ── Test 5: single attempt mode ─────────────────────────────────────
test_single_attempt_fail() {
  local stub_dir
  stub_dir="$(mktemp -d)"

  cat > "${stub_dir}/podman" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
  chmod +x "${stub_dir}/podman"

  cat > "${stub_dir}/oc" <<'STUB'
#!/usr/bin/env bash
exit 1
STUB
  chmod +x "${stub_dir}/oc"

  cat > "${stub_dir}/sleep" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
  chmod +x "${stub_dir}/sleep"

  local out rc=0
  out=$(PATH="${stub_dir}:${PATH}" bash "${SCRIPT}" "test-image:latest" "test-node" 1 2>&1) || rc=$?
  rm -rf "${stub_dir}"

  assert_eq "single-attempt-exit-code" "1" "${rc}"
  assert_contains "single-attempt-error" "Failed to load image after 1 attempts" "${out}"
}

# ── Test 6: default max-attempts is 3 ───────────────────────────────
test_default_max_attempts() {
  local stub_dir
  stub_dir="$(mktemp -d)"

  cat > "${stub_dir}/podman" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
  chmod +x "${stub_dir}/podman"

  cat > "${stub_dir}/oc" <<'STUB'
#!/usr/bin/env bash
exit 1
STUB
  chmod +x "${stub_dir}/oc"

  cat > "${stub_dir}/sleep" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
  chmod +x "${stub_dir}/sleep"

  local out rc=0
  # Omit the third argument to use the default
  out=$(PATH="${stub_dir}:${PATH}" bash "${SCRIPT}" "test-image:latest" "test-node" 2>&1) || rc=$?
  rm -rf "${stub_dir}"

  assert_eq "default-max-exit-code" "1" "${rc}"
  assert_contains "default-max-attempts" "attempt 3/3" "${out}"
}

# ── Run all tests ────────────────────────────────────────────────────
test_success_first_attempt
test_retry_then_success
test_all_attempts_fail
test_missing_args
test_single_attempt_fail
test_default_max_attempts

echo
echo "${pass} passed, ${fail} failed"
[[ "${fail}" -eq 0 ]]
