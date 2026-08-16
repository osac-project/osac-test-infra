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
