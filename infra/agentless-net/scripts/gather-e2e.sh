#!/usr/bin/env bash
#
# Gather E2E test diagnostics from hosted clusters.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="${SCRIPT_DIR}/.."
LOG_DIR="${INFRA_DIR}/logs"

mkdir -p "$LOG_DIR"

info() { echo "==> $*"; }

if [ ! -f "${INFRA_DIR}/.mgmt-network" ]; then
    info "No .mgmt-network — skipping"
    exit 0
fi

# shellcheck source=/dev/null
source "${INFRA_DIR}/.mgmt-network"
export KUBECONFIG

OSAC_NAMESPACE="${OSAC_NAMESPACE:-osac-e2e-ci}"

# ---------- cluster state ----------

info "Gathering hosted cluster diagnostics..."

# Mgmt-side hosted-cluster CRs whose .status.conditions carry the provisioning
# failure reason. They reference the pull secret by NAME (not exposed here);
# strip AgentClusterInstall's signed debugInfo URLs (eventsURL/logsURL tokens)
# that would otherwise land in this raw-uploaded dir. (Hardware-inventory CRs
# -- agents, infraenvs -- live in gather-infra.sh.)
oc get hostedclusters -A > "$LOG_DIR/hostedclusters.txt" 2>&1 || true
oc get hostedclusters -A -o yaml > "$LOG_DIR/hostedclusters.yaml" 2>&1 || true
oc get nodepools -A -o yaml > "$LOG_DIR/nodepools.yaml" 2>&1 || true
oc get agentclusterinstalls -A -o yaml 2>/dev/null \
    | yq 'del(.items[].status.debugInfo)' > "$LOG_DIR/agentclusterinstalls.yaml" 2>/dev/null || true

for order in $(oc get clusterorders -n "$OSAC_NAMESPACE" --no-headers -o custom-columns='NAME:.metadata.name' 2>/dev/null); do
    hc_ns="${OSAC_NAMESPACE}-${order}"
    kc_secret="${order}-admin-kubeconfig"
    kc_file=$(mktemp)
    oc get secret "$kc_secret" -n "$hc_ns" -o jsonpath='{.data.kubeconfig}' 2>/dev/null | base64 -d > "$kc_file"
    if [ -s "$kc_file" ]; then
        KUBECONFIG="$kc_file" oc get nodes -o wide > "$LOG_DIR/${order}-nodes.txt" 2>&1 || true
        KUBECONFIG="$kc_file" oc get co > "$LOG_DIR/${order}-clusteroperators.txt" 2>&1 || true
        KUBECONFIG="$kc_file" oc get pods -A --no-headers | grep -v Running | grep -v Completed > "$LOG_DIR/${order}-unhealthy-pods.txt" 2>&1 || true
    fi
    rm -f "$kc_file"
done

info "Diagnostics saved to $LOG_DIR"
