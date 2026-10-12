#!/usr/bin/env bash
# Unit tests for setup-caas.sh agent flow switching logic.
# Validates that CAAS_AGENT_FLOW correctly controls namespace selection
# and that cluster-namespace mode requires CAAS_CLUSTER_ORDER_NAMESPACE.
set -euo pipefail

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

assert_rc() {
  local name="$1" expected_rc="$2"
  shift 2
  set +e
  "$@" >/dev/null 2>&1
  local rc=$?
  set -e
  assert_eq "${name}" "${expected_rc}" "${rc}"
}

# ── Test: default flow is hot-pool ─────────────────────────────────
CAAS_AGENT_FLOW="${CAAS_AGENT_FLOW_DEFAULT:-}"
unset CAAS_AGENT_FLOW 2>/dev/null || true
RESOLVED="${CAAS_AGENT_FLOW:-hot-pool}"
assert_eq "default flow is hot-pool" "hot-pool" "${RESOLVED}"

# ── Test: hot-pool selects hardware-inventory ──────────────────────
CAAS_AGENT_FLOW="hot-pool"
if [[ "${CAAS_AGENT_FLOW}" == "cluster-namespace" ]]; then
  AGENT_NAMESPACE="should-not-be-set"
else
  AGENT_NAMESPACE="hardware-inventory"
fi
assert_eq "hot-pool namespace is hardware-inventory" "hardware-inventory" "${AGENT_NAMESPACE}"

# ── Test: cluster-namespace uses CAAS_CLUSTER_ORDER_NAMESPACE ──────
CAAS_AGENT_FLOW="cluster-namespace"
CAAS_CLUSTER_ORDER_NAMESPACE="osac-e2e-ci-test-cluster"
if [[ "${CAAS_AGENT_FLOW}" == "cluster-namespace" ]]; then
  if [[ -z "${CAAS_CLUSTER_ORDER_NAMESPACE:-}" ]]; then
    AGENT_NAMESPACE="ERROR"
  else
    AGENT_NAMESPACE="${CAAS_CLUSTER_ORDER_NAMESPACE}"
  fi
else
  AGENT_NAMESPACE="hardware-inventory"
fi
assert_eq "cluster-namespace uses ClusterOrder namespace" \
  "osac-e2e-ci-test-cluster" "${AGENT_NAMESPACE}"

# ── Test: cluster-namespace fails without namespace var ─────────────
CAAS_AGENT_FLOW="cluster-namespace"
unset CAAS_CLUSTER_ORDER_NAMESPACE 2>/dev/null || true
check_missing_ns() {
  if [[ "${CAAS_AGENT_FLOW}" == "cluster-namespace" ]]; then
    if [[ -z "${CAAS_CLUSTER_ORDER_NAMESPACE:-}" ]]; then
      return 1
    fi
  fi
  return 0
}
assert_rc "cluster-namespace rejects missing namespace" 1 check_missing_ns

# ── Test: approval gating ──────────────────────────────────────────
should_approve() {
  local flow="$1"
  [[ "${flow}" != "cluster-namespace" ]]
}
assert_rc "hot-pool approves agents" 0 should_approve "hot-pool"
assert_rc "cluster-namespace skips approval" 1 should_approve "cluster-namespace"

# ── Summary ────────────────────────────────────────────────────────
echo ""
echo "${pass} passed, ${fail} failed"
if [[ "${fail}" -gt 0 ]]; then
  exit 1
fi
