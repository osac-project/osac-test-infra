#!/usr/bin/env bash
#
# Tear down CaaS VMs, bridges, and agent resources.
# Idempotent — safe to re-run.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="${SCRIPT_DIR}/.."

LAB_NAME="agentless-net-lab"
VM_DIR="/var/lib/libvirt/images/${LAB_NAME}"
HOST_VMS=("host-1" "host-2")
HOST_BRIDGES=("br-host1" "br-host2")
AGENT_NAMESPACE="hardware-inventory"

info() { echo "==> $*"; }

# ---------- destroy VMs ----------

for vm in "${HOST_VMS[@]}"; do
    if virsh dominfo "$vm" &>/dev/null; then
        virsh destroy "$vm" 2>/dev/null || true
        virsh undefine "$vm" --remove-all-storage 2>/dev/null || true
        info "Removed VM $vm"
    fi
done

# ---------- destroy bridges ----------

for br in "${HOST_BRIDGES[@]}"; do
    if ip link show "$br" &>/dev/null; then
        ip link del "$br" 2>/dev/null || true
        info "Removed bridge $br"
    fi
done

# ---------- clean up VM directory ----------

if [ -d "$VM_DIR" ]; then
    rm -rf "$VM_DIR"
    info "Removed $VM_DIR"
fi

# ---------- clean up agent resources ----------

if [ -f "${INFRA_DIR}/.mgmt-network" ]; then
    # shellcheck source=/dev/null
    source "${INFRA_DIR}/.mgmt-network"
    export KUBECONFIG
    oc delete infraenv --all -n "$AGENT_NAMESPACE" --ignore-not-found 2>/dev/null || true
    oc delete agents --all -n "$AGENT_NAMESPACE" --ignore-not-found 2>/dev/null || true
    info "Cleaned up agent resources"
fi

info "destroy-caas complete."
