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
# Write common stubs (podman, sleep) into the given directory.
_write_common_stubs() {
  local dir="$1"

  cat > "${dir}/podman" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
  chmod +x "${dir}/podman"

  cat > "${dir}/sleep" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
  chmod +x "${dir}/sleep"
}

# ── Test 1: succeeds on first attempt ────────────────────────────────
test_success_first_attempt() {
  local stub_dir
  stub_dir="$(mktemp -d)"
  _write_common_stubs "${stub_dir}"

  cat > "${stub_dir}/oc" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
  chmod +x "${stub_dir}/oc"

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
  _write_common_stubs "${stub_dir}"

  # Counter file lives inside stub_dir — no global /tmp path.
  local counter_file="${stub_dir}/oc-call-counter"

  # oc fails twice, then succeeds; counter is inside stub_dir.
  cat > "${stub_dir}/oc" <<STUB
#!/usr/bin/env bash
COUNTER_FILE="${counter_file}"
count=0
if [[ -f "\${COUNTER_FILE}" ]]; then
  count=\$(cat "\${COUNTER_FILE}")
fi
count=\$((count + 1))
echo "\${count}" > "\${COUNTER_FILE}"
if [[ "\${count}" -le 2 ]]; then
  echo "Unable to connect to the server: EOF" >&2
  exit 1
fi
exit 0
STUB
  chmod +x "${stub_dir}/oc"

  local out rc=0
  out=$(PATH="${stub_dir}:${PATH}" bash "${SCRIPT}" "test-image:latest" "test-node" 3 2>&1) || rc=$?
  rm -rf "${stub_dir}"

  assert_eq "retry-exit-code" "0" "${rc}"
  assert_contains "retry-attempt-3" "attempt 3/3" "${out}"
  assert_contains "retry-success" "successfully on attempt 3" "${out}"
}

# ── Test 3: all attempts fail → exit 1 ──────────────────────────────
test_all_attempts_fail() {
  local stub_dir
  stub_dir="$(mktemp -d)"
  _write_common_stubs "${stub_dir}"

  cat > "${stub_dir}/oc" <<'STUB'
#!/usr/bin/env bash
echo "Unable to connect to the server: EOF" >&2
exit 1
STUB
  chmod +x "${stub_dir}/oc"

  local out rc=0
  out=$(PATH="${stub_dir}:${PATH}" bash "${SCRIPT}" "test-image:latest" "test-node" 3 2>&1) || rc=$?
  rm -rf "${stub_dir}"

  assert_eq "exhaust-exit-code" "1" "${rc}"
  assert_contains "exhaust-error-msg" "Failed to load image after 3 attempts" "${out}"
  assert_contains "exhaust-eof-category" "reason: api-connection-eof" "${out}"
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
  _write_common_stubs "${stub_dir}"

  cat > "${stub_dir}/oc" <<'STUB'
#!/usr/bin/env bash
exit 1
STUB
  chmod +x "${stub_dir}/oc"

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
  _write_common_stubs "${stub_dir}"

  cat > "${stub_dir}/oc" <<'STUB'
#!/usr/bin/env bash
exit 1
STUB
  chmod +x "${stub_dir}/oc"

  local out rc=0
  # Omit the third argument to use the default
  out=$(PATH="${stub_dir}:${PATH}" bash "${SCRIPT}" "test-image:latest" "test-node" 2>&1) || rc=$?
  rm -rf "${stub_dir}"

  assert_eq "default-max-exit-code" "1" "${rc}"
  assert_contains "default-max-attempts" "attempt 3/3" "${out}"
}

# ── Test 7: zero max-attempts → validation error ────────────────────
test_zero_max_attempts() {
  local stub_dir
  stub_dir="$(mktemp -d)"
  _write_common_stubs "${stub_dir}"

  local out rc=0
  out=$(PATH="${stub_dir}:${PATH}" bash "${SCRIPT}" "test-image:latest" "test-node" 0 2>&1) || rc=$?
  rm -rf "${stub_dir}"

  assert_eq "zero-max-exit-code" "1" "${rc}"
  assert_contains "zero-max-error" "must be an integer between 1 and 10" "${out}"
}

# ── Test 8: non-integer max-attempts → validation error ─────────────
test_non_integer_max_attempts() {
  local stub_dir
  stub_dir="$(mktemp -d)"
  _write_common_stubs "${stub_dir}"

  local out rc=0
  out=$(PATH="${stub_dir}:${PATH}" bash "${SCRIPT}" "test-image:latest" "test-node" "abc" 2>&1) || rc=$?
  rm -rf "${stub_dir}"

  assert_eq "non-integer-exit-code" "1" "${rc}"
  assert_contains "non-integer-error" "must be an integer between 1 and 10" "${out}"
}

# ── Test 9: negative max-attempts → validation error ────────────────
test_negative_max_attempts() {
  local stub_dir
  stub_dir="$(mktemp -d)"
  _write_common_stubs "${stub_dir}"

  local out rc=0
  out=$(PATH="${stub_dir}:${PATH}" bash "${SCRIPT}" "test-image:latest" "test-node" "-1" 2>&1) || rc=$?
  rm -rf "${stub_dir}"

  assert_eq "negative-max-exit-code" "1" "${rc}"
  assert_contains "negative-max-error" "must be an integer between 1 and 10" "${out}"
}

# ── Test 10: out-of-range max-attempts → validation error ────────────
test_out_of_range_max_attempts() {
  local stub_dir
  stub_dir="$(mktemp -d)"
  _write_common_stubs "${stub_dir}"

  local out rc=0
  out=$(PATH="${stub_dir}:${PATH}" bash "${SCRIPT}" "test-image:latest" "test-node" 11 2>&1) || rc=$?
  rm -rf "${stub_dir}"

  assert_eq "out-of-range-exit-code" "1" "${rc}"
  assert_contains "out-of-range-error" "must be an integer between 1 and 10" "${out}"
}

# ── Test 11: status logs do not contain raw node name ────────────────
test_no_node_name_in_status_logs() {
  local stub_dir
  stub_dir="$(mktemp -d)"
  _write_common_stubs "${stub_dir}"

  cat > "${stub_dir}/oc" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
  chmod +x "${stub_dir}/oc"

  local out rc=0
  out=$(PATH="${stub_dir}:${PATH}" bash "${SCRIPT}" "test-image:latest" "secret-node-42" 3 2>&1) || rc=$?
  rm -rf "${stub_dir}"

  assert_eq "no-node-name-exit-code" "0" "${rc}"
  # The status/progress lines must not leak the raw node name.
  if [[ "${out}" == *"secret-node-42"* ]]; then
    echo "FAIL: no-node-name-in-logs (output contains raw node name)"
    fail=$((fail + 1))
  else
    echo "PASS: no-node-name-in-logs"
    pass=$((pass + 1))
  fi
}

# ── Test 12: leading-zero max-attempts → validation error ────────────
test_leading_zero_max_attempts() {
  local stub_dir
  stub_dir="$(mktemp -d)"
  _write_common_stubs "${stub_dir}"

  local out rc=0
  out=$(PATH="${stub_dir}:${PATH}" bash "${SCRIPT}" "test-image:latest" "test-node" "08" 2>&1) || rc=$?
  rm -rf "${stub_dir}"

  assert_eq "leading-zero-exit-code" "1" "${rc}"
  assert_contains "leading-zero-error" "must be an integer between 1 and 10" "${out}"
}

# ── Test 13: oc debug output suppressed from helper stdout/stderr ────
test_oc_output_suppressed() {
  local stub_dir
  stub_dir="$(mktemp -d)"
  _write_common_stubs "${stub_dir}"

  # oc stub that emits a node name, an API endpoint, and other
  # infrastructure details on stdout and stderr — exactly the kind of
  # output the helper must suppress from workflow logs.
  cat > "${stub_dir}/oc" <<'STUB'
#!/usr/bin/env bash
echo "Starting pod/ip-10-0-141-72-debug ..."
echo "Loaded image: localhost/component-0-pr:12345 on node ip-10-0-141-72.ec2.internal"
echo "https://api.osac-ci-42.devcluster.openshift.com:6443/debug/pod" >&2
exit 0
STUB
  chmod +x "${stub_dir}/oc"

  local out rc=0
  out=$(PATH="${stub_dir}:${PATH}" bash "${SCRIPT}" "test-image:latest" "ip-10-0-141-72.ec2.internal" 3 2>&1) || rc=$?
  rm -rf "${stub_dir}"

  assert_eq "oc-suppressed-exit-code" "0" "${rc}"
  # None of the oc debug stdout/stderr content may appear in the
  # helper's own output.
  local leaked=0
  if [[ "${out}" == *"ip-10-0-141-72"* ]]; then leaked=1; fi
  if [[ "${out}" == *"devcluster.openshift.com"* ]]; then leaked=1; fi
  if [[ "${out}" == *"Starting pod"* ]]; then leaked=1; fi

  if [[ "${leaked}" -eq 1 ]]; then
    echo "FAIL: oc-output-suppressed (oc debug output leaked into helper output)"
    fail=$((fail + 1))
  else
    echo "PASS: oc-output-suppressed"
    pass=$((pass + 1))
  fi
}

# ── Test 14: EOF failure is classified as api-connection-eof ──────────
test_classify_eof() {
  local stub_dir
  stub_dir="$(mktemp -d)"
  _write_common_stubs "${stub_dir}"

  cat > "${stub_dir}/oc" <<'STUB'
#!/usr/bin/env bash
echo "Unable to connect to the server: EOF" >&2
exit 1
STUB
  chmod +x "${stub_dir}/oc"

  local out rc=0
  out=$(PATH="${stub_dir}:${PATH}" bash "${SCRIPT}" "test-image:latest" "test-node" 1 2>&1) || rc=$?
  rm -rf "${stub_dir}"

  assert_eq "classify-eof-exit-code" "1" "${rc}"
  assert_contains "classify-eof-category" "reason: api-connection-eof" "${out}"
}

# ── Test 15: unknown failure with sensitive details does not leak ─────
test_classify_unknown_no_leak() {
  local stub_dir
  stub_dir="$(mktemp -d)"
  _write_common_stubs "${stub_dir}"

  # oc stub emits sensitive-looking output that does not match any
  # known category — the classifier must report "unknown" and the
  # raw content must not appear in helper output.
  cat > "${stub_dir}/oc" <<'STUB'
#!/usr/bin/env bash
echo "error: node ip-10-0-141-72.ec2.internal failed at https://api.osac-ci-42.devcluster.openshift.com:6443" >&2
echo "pod/ip-10-0-141-72-debug exited with code 137" >&2
exit 1
STUB
  chmod +x "${stub_dir}/oc"

  local out rc=0
  out=$(PATH="${stub_dir}:${PATH}" bash "${SCRIPT}" "test-image:latest" "test-node" 1 2>&1) || rc=$?
  rm -rf "${stub_dir}"

  assert_eq "classify-unknown-exit-code" "1" "${rc}"
  assert_contains "classify-unknown-category" "reason: unknown" "${out}"
  # Verify no raw sensitive details leaked.
  local leaked=0
  if [[ "${out}" == *"ip-10-0-141-72"* ]]; then leaked=1; fi
  if [[ "${out}" == *"devcluster.openshift.com"* ]]; then leaked=1; fi
  if [[ "${out}" == *"code 137"* ]]; then leaked=1; fi
  if [[ "${leaked}" -eq 1 ]]; then
    echo "FAIL: classify-unknown-no-leak (raw oc output leaked into helper output)"
    fail=$((fail + 1))
  else
    echo "PASS: classify-unknown-no-leak"
    pass=$((pass + 1))
  fi
}

# ── Test 16: every component-building workflow uses the helper ────────
test_all_workflows_use_helper() {
  local repo_root
  repo_root="$(cd "${SCRIPT_DIR}/../.." && pwd)"
  local wf_dir="${repo_root}/.github/workflows"

  # Workflows that build component images contain the "Build and load"
  # step.  Every one of them must call the helper script instead of the
  # raw podman-save pipeline.
  local bare_count=0 helper_count=0
  for f in "${wf_dir}"/e2e-*-full-install.yml "${wf_dir}"/e2e-*-full-install-*.yml "${wf_dir}"/e2e-full-regression.yml; do
    [ -f "$f" ] || continue
    # Only inspect workflows that have the "Build and load component
    # images" step — others (callers, label-cleanup) are irrelevant.
    grep -q 'Build and load component images' "$f" || continue

    if grep -q 'podman save.*oc debug.*podman load' "$f"; then
      bare_count=$((bare_count + 1))
    fi
    if grep -q 'load-image-onto-node\.sh' "$f"; then
      helper_count=$((helper_count + 1))
    fi
  done

  assert_eq "no-bare-podman-save-pipelines" "0" "${bare_count}"
  # At least the 7 originally migrated workflows (5 original + 2 netris)
  if [[ "${helper_count}" -ge 7 ]]; then
    echo "PASS: all-workflows-use-helper (${helper_count} workflows)"
    pass=$((pass + 1))
  else
    echo "FAIL: all-workflows-use-helper (only ${helper_count} workflows, expected >=7)"
    fail=$((fail + 1))
  fi
}

# ── Run all tests ────────────────────────────────────────────────────
test_success_first_attempt
test_retry_then_success
test_all_attempts_fail
test_missing_args
test_single_attempt_fail
test_default_max_attempts
test_zero_max_attempts
test_non_integer_max_attempts
test_negative_max_attempts
test_out_of_range_max_attempts
test_no_node_name_in_status_logs
test_leading_zero_max_attempts
test_oc_output_suppressed
test_classify_eof
test_classify_unknown_no_leak
test_all_workflows_use_helper

echo
echo "${pass} passed, ${fail} failed"
[[ "${fail}" -eq 0 ]]
