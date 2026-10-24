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
echo "── Worker network prerequisites ───────────────────────────────"
SETUP_BMH=".github/scripts/setup-virtual-bmh.sh"
assert_contains "Virtual BMH setup exports its gateway" "${SETUP_BMH}" "VIRTUAL_BMH_GATEWAY=.*\\$\\{GW_IP\\}"
assert_contains "Install uses gateway as worker NTP source" "${WF}" "additionalNTPSources\\[0\\]=.*VIRTUAL_BMH_GATEWAY"
assert_contains "Install selects the Route53 DNS driver" "${WF}" "DNS_CLASS.*dns\\.route53\\.dns"

echo ""
echo "── Agent watcher step ─────────────────────────────────────────"
assert_contains "Watcher step exists" "${WF}" "Start CaaS agent and virtual BMH watcher"
assert_contains "Watcher step gated on cluster-namespace" "${WF}" "caas-agent-flow.*cluster-namespace"
assert_contains "Watcher runs for virtual BMHs" "${WF}" "metal3-enabled.*true"
assert_contains "Watcher can disable agent setup" "${WF}" "CAAS_AGENT_WATCHER_SETUP_ENABLED"
assert_contains "Watcher sets CAAS_CLUSTER_ORDER_NAMESPACE" "${WF}" "CAAS_CLUSTER_ORDER_NAMESPACE"
assert_contains "Watcher invokes setup-caas-agents.sh" "${WF}" "bash .github/scripts/setup-caas-agents.sh"

WATCHER=".github/scripts/watch-caas-agent-namespaces.sh"
assert_contains "Watcher passes the ClusterOrder namespace" "${WATCHER}" "CAAS_CLUSTER_ORDER_NAMESPACE=.*\\$\\{namespace\\}"
assert_contains "Watcher repairs virtual BMH reboots" "${WATCHER}" "repair_virtual_bmh_reboot"
assert_contains "Watcher selects disk for virtual BMH reboot" "${WATCHER}" "BootSourceOverrideTarget.*Hdd"
assert_contains "Watcher has opt-in local DNS repair" "${WATCHER}" "CAAS_AGENT_WATCHER_LOCAL_DNS_ENABLED"
assert_contains "Watcher writes libvirt addn-hosts" "${WATCHER}" "\\.addnhosts"
assert_contains "Watcher cleans local DNS entries" "${WATCHER}" "remove_local_dns"

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
echo "── Completion flag race (OSAC-5697 review fix) ────────────────"
SETUP=".github/scripts/setup-caas-agents.sh"
# SETUP_COMPLETED must be set before the final info message so the
# EXIT trap never races with exit-after-echo.
COMPLETED_LINE=$(grep -n 'SETUP_COMPLETED=1' "${SETUP}" | tail -1 | cut -d: -f1)
INFO_LINE=$(grep -n 'info "setup-caas-agents complete' "${SETUP}" | tail -1 | cut -d: -f1)
if [[ -n "${COMPLETED_LINE}" && -n "${INFO_LINE}" && "${COMPLETED_LINE}" -lt "${INFO_LINE}" ]]; then
    echo "  PASS: SETUP_COMPLETED set before final info message"
    PASS=$((PASS + 1))
else
    echo "  FAIL: SETUP_COMPLETED must be set before the final info message"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "── CAAS_CLUSTER_ORDER_NAME absence warning (OSAC-5697 review fix) ─"
assert_contains "Warns when CAAS_CLUSTER_ORDER_NAME is absent" "${SETUP}" \
    'WARNING.*CAAS_CLUSTER_ORDER_NAME.*not set.*deletion detection'

echo ""
echo "── No dead .items[0] jq paths (OSAC-5697 review fix) ──────────"
assert_not_contains "No .items[0] on single-object HostedCluster get" \
    "${WATCHER}" '\.items\[0\]\.status\.controlPlaneEndpoint'
assert_not_contains "No .items[0] on single-object Service get" \
    "${WATCHER}" '\.items\[0\]\.status\.loadBalancer'

echo ""
echo "── BMC credentials not in process args (OSAC-5697 review fix) ─"
assert_not_contains "No --user with BMC credentials in curl args" \
    "${WATCHER}" 'curl.*--user.*bmc_'
assert_contains "Uses --netrc-file for BMC authentication" \
    "${WATCHER}" 'netrc-file'

echo ""
echo "── Regression watcher failure detection (OSAC-5697 review fix) ─"
REGRESSION_WF=".github/workflows/e2e-full-regression.yml"
assert_contains "Regression exports watcher status file" \
    "${REGRESSION_WF}" 'VIRTUAL_BMH_WATCHER_STATUS'
assert_contains "Regression checks watcher status" \
    "${REGRESSION_WF}" 'WATCHER_RESULT.*complete'

echo ""
echo "── Agent inventory/MAC readiness race (review fix) ───────────"
assert_contains "Wait loop counts agents with inventory annotation" \
    "${SETUP}" 'agent\.agent-install\.openshift\.io/inventory'
assert_contains "Wait loop uses jq to count MACs" \
    "${SETUP}" 'fromjson.*interfaces.*mac_address.*length'
assert_contains "MAC parser uses .get() for safe annotation access" \
    "${SETUP}" "\.get.*annotations.*\.get.*agent\.agent-install"
assert_not_contains "MAC parser does not use direct dict access for inventory" \
    "${SETUP}" "annotations\['agent\.agent-install\.openshift\.io/inventory'\]"

echo ""
echo "── Optional jq iteration for missing inventory (review fix) ──"
assert_contains "Watcher uses optional iteration for items" \
    "${WATCHER}" '\.items\[\]\?\.status\.inventory'
assert_contains "Watcher uses optional iteration for interfaces" \
    "${WATCHER}" '\.interfaces\[\]\?\.ipV4Addresses'

echo ""
echo "── Distinguish transient API errors from NotFound (review fix) "
# --ignore-not-found can be on a continuation line, so check it exists
# in the watcher discovery section (near "Do not start VMs").
assert_contains "Watcher discovery block uses --ignore-not-found" \
    "${WATCHER}" 'ignore-not-found -o json'
assert_contains "Transient API error retries on next poll" \
    "${WATCHER}" 'Transient API error.*retry'
assert_not_contains "No unconditional seen_namespaces on oc failure" \
    "${WATCHER}" 'oc get clusterorder.*-o json 2>/dev/null\) \|\|'

echo ""
echo "── Unused variable fix SC2034 (review fix) ───────────────────"
assert_not_contains "No unused attempt variable in watcher stop loop" \
    "${WF}" 'for attempt in.*seq 1 30'
assert_contains "Uses _ for unused loop variable" \
    "${WF}" 'for _ in.*seq 1 30'

echo ""
echo "── DNS_CLASS indentation under config (review fix) ───────────"
OVERLAY="infra/agentless-net/values-overlay.yaml"
# DNS_CLASS must be at 8-space indentation (inside config), not 4-space
assert_contains "DNS_CLASS is at config level (8-space indent)" \
    "${OVERLAY}" '^        DNS_CLASS:'
assert_not_contains "DNS_CLASS is NOT at instanceGroups level (4-space)" \
    "${OVERLAY}" '^    DNS_CLASS:'

echo ""
echo "── NTP variable guarded (review fix) ─────────────────────────"
DEPLOY_OSAC="infra/agentless-net/scripts/deploy-osac.sh"
assert_not_contains "NTP env var is not set unconditionally" \
    "${DEPLOY_OSAC}" '^KUBECONFIG.*oc set env.*OSAC_BARE_METAL_WORKER_ADDITIONAL_NTP_SOURCES'

echo ""
echo "── Internal hostname redaction in published logs (review fix) ─"
assert_not_contains "Watcher DNS log does not expose API hostname" \
    "${WATCHER}" 'echo.*Added local DNS entry.*api_host'
assert_not_contains "Watcher DNS log does not expose apps hostname" \
    "${WATCHER}" 'echo.*Added local DNS entry.*apps_host'
assert_contains "CaaS workflow redacts hostnames from published watcher logs" \
    "${WF}" 'redact_hostnames'
REGRESSION_WF=".github/workflows/e2e-full-regression.yml"
assert_contains "Regression workflow redacts hostnames from published watcher logs" \
    "${REGRESSION_WF}" 'redact_hostnames'
assert_not_contains "Regression workflow does not cat raw watcher log" \
    "${REGRESSION_WF}" 'cat "\$VIRTUAL_BMH_WATCHER_LOG"'

echo ""
echo "── Redfish curl does not leak internal endpoints (review fix) ─"
# The Redfish PATCH curl is a multi-line command, so extract the block
# and check properties within it.
CURL_BLOCK=$(sed -n '/curl --noproxy/,/; then$/p' "${WATCHER}")
if [[ -z "${CURL_BLOCK}" ]]; then
    echo "  FAIL: Redfish curl block not found"
    FAIL=$((FAIL + 1))
else
    echo "  PASS: Redfish curl block found"
    PASS=$((PASS + 1))
fi
# curl must not use --show-error (prints URL on failure to stderr/log)
if echo "${CURL_BLOCK}" | grep -qF -- '--show-error'; then
    echo "  FAIL: Redfish curl must not use --show-error"
    FAIL=$((FAIL + 1))
else
    echo "  PASS: Redfish curl does not use --show-error"
    PASS=$((PASS + 1))
fi
# curl stderr must be redirected so failure messages cannot expose
# the Redfish URL or BMC endpoint in the published watcher log.
if echo "${CURL_BLOCK}" | grep -qE '2>/dev/null'; then
    echo "  PASS: Redfish curl stderr is suppressed"
    PASS=$((PASS + 1))
else
    echo "  FAIL: Redfish curl stderr must be redirected to /dev/null"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "── finish_watcher does not hide incomplete provisioning (review fix) "
# finish_watcher must NOT mark unfinished setups as cancelled.  Only
# cancel_deleting_setups may set cancelled_setups (when the ClusterOrder
# or namespace is gone/deleting).  If finish_watcher itself sets
# cancelled_setups, an unfinished setup stopped during normal workflow
# shutdown would be silently ignored and the watcher would report success
# even though provisioning was incomplete.
assert_not_contains "finish_watcher does not set cancelled_setups" \
    "${WATCHER}" 'finish_watcher\(\).*cancelled_setups\['
# More specific: check no cancelled_setups assignment inside finish_watcher
# by scanning the function body (from "finish_watcher()" to next function
# or trap).  The pattern checks that cancelled_setups[...]=1 does not
# appear between "finish_watcher()" and "trap finish_watcher".
FINISH_BODY=$(sed -n '/^finish_watcher()/,/^trap finish_watcher/p' "${WATCHER}")
if [[ -z "${FINISH_BODY}" ]]; then
    echo "  FAIL: finish_watcher body is empty (function not found)"
    FAIL=$((FAIL + 1))
else
    echo "  PASS: finish_watcher body is nonempty"
    PASS=$((PASS + 1))
fi
if echo "${FINISH_BODY}" | grep -qE 'cancelled_setups\[.*\]='; then
    echo "  FAIL: finish_watcher body must not assign to cancelled_setups"
    FAIL=$((FAIL + 1))
else
    echo "  PASS: finish_watcher body does not assign to cancelled_setups"
    PASS=$((PASS + 1))
fi
# Verify signal_setup is called WITHIN finish_watcher (not just anywhere
# in the watcher) so removing it from the shutdown path is detected.
if echo "${FINISH_BODY}" | grep -qE 'signal_setup'; then
    echo "  PASS: finish_watcher body calls signal_setup"
    PASS=$((PASS + 1))
else
    echo "  FAIL: finish_watcher body must call signal_setup for unfinished setups"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "── Summary ────────────────────────────────────────────────────"
echo "  ${PASS} passed, ${FAIL} failed"
if [[ "${FAIL}" -gt 0 ]]; then
    exit 1
fi
