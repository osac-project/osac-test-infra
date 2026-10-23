#!/usr/bin/env bash
# Regression test: verify caas-agent-flow input reaches all required
# workflow steps in e2e-caas-full-install.yml.
#
# This tests the YAML structure (not runtime behaviour) to catch the
# exact gap that caused OSAC-5697: the workflow accepted the input but
# no code path consumed it.
#
# Run: bash .github/scripts/test-caas-workflow-dispatch.sh
set -euo pipefail

PASS=0
FAIL=0

assert_contains() {
    local desc="$1" file="$2" pattern="$3"
    if grep -qE "${pattern}" "${file}"; then
        echo "  PASS: ${desc}"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: ${desc} (pattern '${pattern}' not found in ${file})"
        FAIL=$((FAIL + 1))
    fi
}

assert_not_contains() {
    local desc="$1" file="$2" pattern="$3"
    if ! grep -qE "${pattern}" "${file}"; then
        echo "  PASS: ${desc}"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: ${desc} (pattern '${pattern}' unexpectedly found in ${file})"
        FAIL=$((FAIL + 1))
    fi
}

WF=".github/workflows/e2e-caas-full-install.yml"

echo "── Workflow input ──────────────────────────────────────────────"
assert_contains "caas-agent-flow input exists" "${WF}" "caas-agent-flow:"
assert_contains "cluster-namespace is documented" "${WF}" "cluster-namespace"

echo ""
echo "── Setup CaaS agents step ─────────────────────────────────────"
assert_contains "Setup step references CAAS_AGENT_FLOW" "${WF}" "CAAS_AGENT_FLOW"
assert_contains "Setup step calls setup-caas-agents.sh" "${WF}" "setup-caas-agents\\.sh"

echo ""
echo "── Agent watcher step ─────────────────────────────────────────"
assert_contains "Watcher step exists" "${WF}" "Start agent watcher for ClusterOrder namespace"
assert_contains "Watcher step gated on cluster-namespace" "${WF}" "caas-agent-flow.*cluster-namespace"
assert_contains "Watcher sets CAAS_CLUSTER_ORDER_NAMESPACE" "${WF}" "CAAS_CLUSTER_ORDER_NAMESPACE"
assert_contains "Watcher invokes setup-caas-agents.sh" "${WF}" "bash .github/scripts/setup-caas-agents.sh"

echo ""
echo "── Caller wires through input ─────────────────────────────────"
CALLER=".github/workflows/e2e-caas-full-install-caller.yml"
assert_contains "Caller has caas-agent-flow input" "${CALLER}" "caas-agent-flow:"
assert_contains "Caller passes caas-agent-flow to reusable workflow" "${CALLER}" "caas-agent-flow:.*inputs\\.caas-agent-flow"

echo ""
echo "── Teardown cleans up agent VMs ───────────────────────────────"
TEARDOWN=".github/scripts/teardown.sh"
assert_contains "Teardown cleans caas-agent VMs" "${TEARDOWN}" "caas-agent-\\\$\\{CLONE_NAME\\}"
assert_contains "Teardown removes agent disk dir" "${TEARDOWN}" "caas-agent-disks"
assert_contains "Teardown verifies CaaS agent cleanup" "${TEARDOWN}" "CAAS_LEAKED"

echo ""
echo "── No dead code: legacy no-op removed ─────────────────────────"
assert_not_contains "No reference to removed installer script" "${WF}" "INSTALLER_DIR.*setup-caas-agents"

echo ""
echo "── Summary ────────────────────────────────────────────────────"
echo "  ${PASS} passed, ${FAIL} failed"
if [[ "${FAIL}" -gt 0 ]]; then
    exit 1
fi
