#!/usr/bin/env bash
# Unit tests for CaaS agent flow functions (setup-caas-agent-flow-lib.sh).
# Run: bash .github/scripts/setup-caas-agent-flow-lib-test.sh
#
# Mirrors infra/agentless-net/scripts/setup-caas-test.sh for the
# cluster-tool counterpart library.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=.github/scripts/setup-caas-agent-flow-lib.sh
source "${SCRIPT_DIR}/setup-caas-agent-flow-lib.sh"

PASS=0
FAIL=0

assert_rc() {
    local desc="$1" expected="$2"
    shift 2
    local rc=0
    "$@" >/dev/null 2>&1 || rc=$?
    if [[ "${rc}" -eq "${expected}" ]]; then
        echo "  PASS: ${desc}"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: ${desc} (expected rc=${expected}, got rc=${rc})"
        FAIL=$((FAIL + 1))
    fi
}

assert_output() {
    local desc="$1" expected="$2"
    shift 2
    local actual
    actual=$("$@" 2>/dev/null) || true
    if [[ "${actual}" == "${expected}" ]]; then
        echo "  PASS: ${desc}"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: ${desc} (expected '${expected}', got '${actual}')"
        FAIL=$((FAIL + 1))
    fi
}

echo "── validate_agent_flow ─────────────────────────────────────────"
CAAS_AGENT_FLOW="hot-pool"
assert_rc "validate_agent_flow accepts hot-pool" 0 validate_agent_flow
CAAS_AGENT_FLOW="cluster-namespace"
assert_rc "validate_agent_flow accepts cluster-namespace" 0 validate_agent_flow
CAAS_AGENT_FLOW="bogus"
assert_rc "validate_agent_flow rejects bogus" 1 validate_agent_flow
unset CAAS_AGENT_FLOW 2>/dev/null || true
assert_rc "validate_agent_flow defaults to hot-pool (valid)" 0 validate_agent_flow

echo ""
echo "── resolve_agent_namespace ────────────────────────────────────"
unset CAAS_AGENT_FLOW 2>/dev/null || true
unset CAAS_CLUSTER_ORDER_NAMESPACE 2>/dev/null || true
assert_output "default resolves to hardware-inventory" "hardware-inventory" resolve_agent_namespace

CAAS_AGENT_FLOW="hot-pool"
assert_output "hot-pool resolves to hardware-inventory" "hardware-inventory" resolve_agent_namespace

CAAS_AGENT_FLOW="cluster-namespace"
assert_rc "cluster-namespace without NS returns 1" 1 resolve_agent_namespace

CAAS_AGENT_FLOW="cluster-namespace"
export CAAS_CLUSTER_ORDER_NAMESPACE="my-cluster-ns"
assert_output "cluster-namespace with NS resolves correctly" "my-cluster-ns" resolve_agent_namespace
unset CAAS_CLUSTER_ORDER_NAMESPACE 2>/dev/null || true

echo ""
echo "── should_approve_agents ──────────────────────────────────────"
CAAS_AGENT_FLOW="hot-pool"
assert_rc "hot-pool approves agents" 0 should_approve_agents

export CAAS_AGENT_FLOW="cluster-namespace"
assert_rc "cluster-namespace does not approve agents" 1 should_approve_agents
unset CAAS_AGENT_FLOW 2>/dev/null || true

echo ""
echo "── Summary ────────────────────────────────────────────────────"
echo "  ${PASS} passed, ${FAIL} failed"
if [[ "${FAIL}" -gt 0 ]]; then
    exit 1
fi
