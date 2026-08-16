#!/usr/bin/env bash
#
# Tear down containerlab topology, br-mgmt bridge, and clean up
# inventory ConfigMap.
# Idempotent — safe to re-run.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="${SCRIPT_DIR}/.."

MGMT_CLONE_NAME="${MGMT_CLONE_NAME:-agentless-lab-mgmt}"
OSAC_NAMESPACE="${OSAC_NAMESPACE:-osac-e2e-ci}"
LAB_NAME="agentless-net-lab"
TOPO_FILE="${INFRA_DIR}/agentless-net-lab.clab.yml"
CONTAINERLAB="${CONTAINERLAB:-containerlab}"

info() { echo "==> $*"; }

# ---------- destroy containerlab ----------

if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "clab-${LAB_NAME}"; then
    info "Destroying containerlab topology..."
    sudo ${CONTAINERLAB} destroy -t "$TOPO_FILE" 2>/dev/null || true
else
    info "Containerlab not running — skipping"
fi

# ---------- destroy br-mgmt ----------

if ip link show br-mgmt &>/dev/null; then
    ip link del br-mgmt 2>/dev/null || true
    info "Removed bridge br-mgmt"
fi

# ---------- remove host route ----------

ip route del 192.168.100.0/24 2>/dev/null || true

# ---------- clean up inventory ConfigMap ----------

if [ -f "${INFRA_DIR}/.mgmt-network" ]; then
    # shellcheck source=/dev/null
    source "${INFRA_DIR}/.mgmt-network"
    export KUBECONFIG
    oc delete configmap agentless-net-inventory -n "$OSAC_NAMESPACE" --ignore-not-found 2>/dev/null || true
    info "Cleaned up inventory ConfigMap"
fi

info "destroy-fabric complete."
