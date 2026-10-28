#!/usr/bin/env bash
# Unit tests for CaaS agent flow functions (setup-caas-lib.sh).
# Validates the shared functions used by setup-caas.sh rather than
# duplicating the production branching logic inline.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=infra/agentless-net/scripts/setup-caas-lib.sh
source "${SCRIPT_DIR}/setup-caas-lib.sh"

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

# ── validate_agent_flow ───────────────────────────────────────────
CAAS_AGENT_FLOW="hot-pool"
assert_rc "validate_agent_flow accepts hot-pool" 0 validate_agent_flow
CAAS_AGENT_FLOW="cluster-namespace"
assert_rc "validate_agent_flow accepts cluster-namespace" 0 validate_agent_flow
CAAS_AGENT_FLOW="bogus"
assert_rc "validate_agent_flow rejects bogus" 1 validate_agent_flow
unset CAAS_AGENT_FLOW 2>/dev/null || true
assert_rc "validate_agent_flow defaults to hot-pool (valid)" 0 validate_agent_flow

# ── resolve_agent_namespace ───────────────────────────────────────
unset CAAS_AGENT_FLOW 2>/dev/null || true
unset CAAS_CLUSTER_ORDER_NAMESPACE 2>/dev/null || true
assert_eq "default flow resolves to hardware-inventory" \
  "hardware-inventory" "$(resolve_agent_namespace)"

CAAS_AGENT_FLOW="hot-pool"
assert_eq "hot-pool resolves to hardware-inventory" \
  "hardware-inventory" "$(resolve_agent_namespace)"

CAAS_AGENT_FLOW="cluster-namespace"
export CAAS_CLUSTER_ORDER_NAMESPACE="test-cluster-order-namespace"
assert_eq "cluster-namespace uses ClusterOrder namespace" \
  "test-cluster-order-namespace" "$(resolve_agent_namespace)"

CAAS_AGENT_FLOW="cluster-namespace"
unset CAAS_CLUSTER_ORDER_NAMESPACE 2>/dev/null || true
assert_rc "cluster-namespace rejects missing namespace" 1 resolve_agent_namespace

# ── should_approve_agents ─────────────────────────────────────────
CAAS_AGENT_FLOW="hot-pool"
assert_rc "hot-pool approves agents" 0 should_approve_agents
export CAAS_AGENT_FLOW="cluster-namespace"
assert_rc "cluster-namespace skips approval" 1 should_approve_agents
unset CAAS_AGENT_FLOW 2>/dev/null || true
assert_rc "default (hot-pool) approves agents" 0 should_approve_agents

# ── Structural: setup-caas.sh does not log VM names ───────────────
# VM creation/start messages must use index-only labels.
# Detect both $vm and ${vm} — either form leaks the configured hostname.
SETUP_CAAS="${SCRIPT_DIR}/setup-caas.sh"
if grep -qP 'echo.*(\$vm\b|\$\{vm\})' "${SETUP_CAAS}"; then
    echo "FAIL: setup-caas.sh still logs \$vm or \${vm} by name"
    fail=$((fail + 1))
else
    echo "PASS: setup-caas.sh uses index-only VM labels"
    pass=$((pass + 1))
fi

# ── Summary ───────────────────────────────────────────────────────
echo ""
echo "${pass} passed, ${fail} failed"
if [[ "${fail}" -gt 0 ]]; then
  exit 1
fi
