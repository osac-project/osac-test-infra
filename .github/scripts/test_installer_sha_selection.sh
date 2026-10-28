#!/bin/bash
# Tests for the installer clone SHA selection logic used by the four
# E2E full-install / full-regression reusable workflows (OSAC-5356).
#
# Exercises the bmaas/caas/regression jq pattern AND the vmaas jq
# pattern.  Each scenario simulates the env the installer step sees
# and verifies the selected CLONE_REF against the expected value.
#
# Run: bash .github/scripts/test_installer_sha_selection.sh
set -euo pipefail

PASS=0; FAIL=0; TOTAL=0

run() {
  local name="$1" expected="$2" actual="$3"
  TOTAL=$((TOTAL + 1))
  if [[ "$actual" == "$expected" ]]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    echo "FAIL: ${name}"
    echo "  expected: '${expected}'"
    echo "  actual:   '${actual}'"
  fi
}

# ── bmaas/caas/regression pattern (used when no per-repo jq filter exists) ──
# Mirrors the jq + fallback logic from e2e-bmaas-full-install.yml's
# "Install infrastructure operators" step.
select_sha_bmaas() {
  local COMPONENT_BUILD_RESULTS="$1"
  local INSTALLER_REPO="$2" INSTALLER_REF="$3"
  local TEST_REPOSITORY="$4" TEST_REF="$5"
  local PINNED_OSAC_SHA="${6:-}"

  local PINNED_SHA
  if [[ "${INSTALLER_REF}" == "${TEST_REF}" ]]; then
    PINNED_SHA=$(echo "${COMPONENT_BUILD_RESULTS}" | jq -r \
      --arg repo "${INSTALLER_REPO}" \
      '[.[] | select(.repo == $repo) | .sha] | unique | if length == 1 then .[0] else empty end')
  else
    PINNED_SHA=$(echo "${COMPONENT_BUILD_RESULTS}" | jq -r \
      --arg repo "${INSTALLER_REPO}" \
      '[.[] | select(.repo == $repo) | select(.source != "osac-5356") | .sha] | unique | if length == 1 then .[0] else empty end')
  fi
  if [[ -z "${PINNED_SHA}" ]] && \
     [[ "${INSTALLER_REPO}" == "${TEST_REPOSITORY}" ]] && \
     [[ "${INSTALLER_REF}" == "${TEST_REF}" ]]; then
    PINNED_SHA="${PINNED_OSAC_SHA:-}"
  fi
  if [[ -n "${PINNED_SHA}" ]]; then
    echo "${PINNED_SHA}"
  else
    echo "${INSTALLER_REF}"
  fi
}

# ── vmaas pattern (per-repo jq filter with multi-SHA check) ──
select_sha_vmaas() {
  local COMPONENT_BUILD_RESULTS="$1"
  local INSTALLER_REPO="$2" INSTALLER_REF="$3"
  local TEST_REPOSITORY="$4" TEST_REF="$5"
  local PINNED_OSAC_SHA="${6:-}"

  local MATCHING_SHAS
  if [[ "${INSTALLER_REF}" == "${TEST_REF}" ]]; then
    MATCHING_SHAS=$(echo "${COMPONENT_BUILD_RESULTS}" | jq -r \
      --arg repo "${INSTALLER_REPO}" \
      '[.[] | select(.repo == $repo) | .sha] | unique | .[]')
  else
    MATCHING_SHAS=$(echo "${COMPONENT_BUILD_RESULTS}" | jq -r \
      --arg repo "${INSTALLER_REPO}" \
      '[.[] | select(.repo == $repo) | select(.source != "osac-5356") | .sha] | unique | .[]')
  fi
  local MATCH_COUNT
  MATCH_COUNT=$(echo "${MATCHING_SHAS}" | grep -c . || true)
  if [[ "${MATCH_COUNT}" -gt 1 ]]; then
    echo "ERROR:multi-sha"
    return
  fi
  local PINNED_SHA="${MATCHING_SHAS}"
  if [[ -z "${PINNED_SHA}" ]] && \
     [[ "${INSTALLER_REPO}" == "${TEST_REPOSITORY}" ]] && \
     [[ "${INSTALLER_REF}" == "${TEST_REF}" ]]; then
    PINNED_SHA="${PINNED_OSAC_SHA:-}"
  fi
  local CLONE_REF="${PINNED_SHA:-${INSTALLER_REF}}"
  echo "${CLONE_REF}"
}

SHA_MAIN="aaaa1111bbbb2222cccc3333dddd4444eeee5555"
SHA_COMP="bbbb2222cccc3333dddd4444eeee5555ffff6666"
OSAC="osac-project/osac"
OTHER="other/installer"
SYNTHETIC="[{\"image\":\"localhost/fs:1\",\"imageKey\":\"service.images.service.repository\",\"repo\":\"${OSAC}\",\"ref\":\"main\",\"sha\":\"${SHA_MAIN}\",\"containerfile\":\"fs/Containerfile\",\"crdsDir\":\"\",\"source\":\"osac-5356\"}]"
REAL_COMP="[{\"image\":\"localhost/fs:2\",\"imageKey\":\"service.images.service.repository\",\"repo\":\"${OSAC}\",\"ref\":\"feat\",\"sha\":\"${SHA_COMP}\",\"containerfile\":\"fs/Containerfile\",\"crdsDir\":\"\"}]"
EMPTY="[]"

echo "── bmaas/caas/regression pattern ──"

run "S1 default no-build: pinned SHA" "$SHA_MAIN" \
  "$(select_sha_bmaas "$SYNTHETIC" "$OSAC" "main" "$OSAC" "main" "$SHA_MAIN")"

run "S2 explicit installer-ref=release-1.0: ref honored" "release-1.0" \
  "$(select_sha_bmaas "$SYNTHETIC" "$OSAC" "release-1.0" "$OSAC" "main" "$SHA_MAIN")"

run "S3 different installer-repo: installer-ref used" "main" \
  "$(select_sha_bmaas "$SYNTHETIC" "$OTHER" "main" "$OSAC" "main" "$SHA_MAIN")"

run "S4 real component SHA used" "$SHA_COMP" \
  "$(select_sha_bmaas "$REAL_COMP" "$OSAC" "main" "$OSAC" "main" "")"

run "S5 real comp + explicit installer-ref: comp SHA wins" "$SHA_COMP" \
  "$(select_sha_bmaas "$REAL_COMP" "$OSAC" "release-1.0" "$OSAC" "main" "")"

run "S6 empty results, matching refs: PINNED_OSAC_SHA fallback" "$SHA_MAIN" \
  "$(select_sha_bmaas "$EMPTY" "$OSAC" "main" "$OSAC" "main" "$SHA_MAIN")"

run "S7 empty results, differing refs: installer-ref used" "release-1.0" \
  "$(select_sha_bmaas "$EMPTY" "$OSAC" "release-1.0" "$OSAC" "main" "$SHA_MAIN")"

run "S8 empty results, different repo: installer-ref used" "main" \
  "$(select_sha_bmaas "$EMPTY" "$OTHER" "main" "$OSAC" "main" "$SHA_MAIN")"

echo ""
echo "── vmaas pattern ──"

run "V1 default no-build: pinned SHA" "$SHA_MAIN" \
  "$(select_sha_vmaas "$SYNTHETIC" "$OSAC" "main" "$OSAC" "main" "$SHA_MAIN")"

run "V2 explicit installer-ref=release-1.0: ref honored" "release-1.0" \
  "$(select_sha_vmaas "$SYNTHETIC" "$OSAC" "release-1.0" "$OSAC" "main" "$SHA_MAIN")"

run "V3 different installer-repo: installer-ref used" "main" \
  "$(select_sha_vmaas "$SYNTHETIC" "$OTHER" "main" "$OSAC" "main" "$SHA_MAIN")"

run "V4 real component SHA used" "$SHA_COMP" \
  "$(select_sha_vmaas "$REAL_COMP" "$OSAC" "main" "$OSAC" "main" "")"

run "V5 real comp + explicit installer-ref: comp SHA wins" "$SHA_COMP" \
  "$(select_sha_vmaas "$REAL_COMP" "$OSAC" "release-1.0" "$OSAC" "main" "")"

run "V6 empty results, matching refs: PINNED_OSAC_SHA fallback" "$SHA_MAIN" \
  "$(select_sha_vmaas "$EMPTY" "$OSAC" "main" "$OSAC" "main" "$SHA_MAIN")"

run "V7 empty results, differing refs: installer-ref used" "release-1.0" \
  "$(select_sha_vmaas "$EMPTY" "$OSAC" "release-1.0" "$OSAC" "main" "$SHA_MAIN")"

echo ""
echo "── Results: ${PASS}/${TOTAL} passed, ${FAIL} failed ──"
exit "${FAIL}"
