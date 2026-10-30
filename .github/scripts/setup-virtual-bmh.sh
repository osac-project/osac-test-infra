#!/usr/bin/env bash
# Provision virtual BareMetalHosts for BMaaS E2E tests.
#
# Thin orchestrator over four tools (no CLONE_NAME in any of them):
#   1. activate-ironic.sh        — Ironic activation (Provisioning CR)
#   2. setup-sushy-tools.sh      — storage pool, sushy-tools emulator,
#                                  nginx virtual-media cache
#   3. manage-virtual-bmh-vms.py — libvirt VMs; maintains the raw inventory
#   4. reconcile-bmhs.py         — BareMetalHosts reconciled with the raw
#                                  inventory
#
# This script owns the CI specifics: CLONE_NAME-derived names (teardown.sh
# derives the same) and the clone network gateway discovery. The cluster
# is selected through KUBECONFIG or oc's default kubeconfig.
#
# Required env:
#   CLONE_NAME   — cluster-tool clone name (finds the libvirt network)
#
# Optional env:
#   BMH_NAMESPACE    — namespace for BMH resources (default: host-inventory)
#   BMH_COUNT        — number of virtual BMHs to create (default: 2)
#   SUSHY_PORT       — sushy-tools listen port (default: 8000)
#   SUSHY_CACHE_PORT — nginx ISO cache listen port (default: 8080)
#
# The raw inventory is written to
#   ${HOME}/virtual-bmh-inventory-${CLONE_NAME}.json
# and exported as INVENTORY_FILE through $GITHUB_ENV for later steps.
set -euo pipefail

: "${CLONE_NAME:?CLONE_NAME is required}"

BMH_NAMESPACE="${BMH_NAMESPACE:-host-inventory}"
BMH_COUNT="${BMH_COUNT:-2}"
SUSHY_PORT="${SUSHY_PORT:-8000}"
SUSHY_CACHE_PORT="${SUSHY_CACHE_PORT:-8080}"

CT_NETWORK="test-infra-net-${CLONE_NAME}"
VM_DISK_DIR="/tmp/virtual-bmh-disks-${CLONE_NAME}"
POOL_NAME="bmh-${CLONE_NAME}"
SUSHY_CONFIG_DIR="${HOME}/sushy-${CLONE_NAME}"
SUSHY_CACHE_NAME="sushy-cache-${CLONE_NAME}"
SUSHY_VMEDIA_TMP="${HOME}/sushy-vmedia-tmp-${CLONE_NAME}"
INVENTORY_FILE="${HOME}/virtual-bmh-inventory-${CLONE_NAME}.json"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VIRSH="virsh -c qemu:///system"

# --- Discover the clone's libvirt network and its gateway IP ---
echo "==> Discovering cluster-tool network..."
if ! ${VIRSH} net-info "${CT_NETWORK}" &>/dev/null; then
  echo "ERROR: libvirt network '${CT_NETWORK}' not found." >&2
  echo "Available networks:" >&2
  ${VIRSH} net-list --all >&2
  exit 1
fi

GW_IP=$(${VIRSH} net-dumpxml "${CT_NETWORK}" | python3 -c "
import sys, xml.etree.ElementTree as ET
root = ET.parse(sys.stdin).getroot()
print(root.find('.//ip').get('address'))
")
echo "Gateway IP (host): ${GW_IP}"

# --- Activate Ironic (Provisioning CR) ---
bash "${SCRIPT_DIR}/activate-ironic.sh"

# --- Start the Redfish emulator and virtual-media cache ---
SUSHY_LISTEN_IP="${GW_IP}" \
STORAGE_POOL_NAME="${POOL_NAME}" \
STORAGE_DIR="${VM_DISK_DIR}" \
SUSHY_CONFIG_DIR="${SUSHY_CONFIG_DIR}" \
SUSHY_CACHE_NAME="${SUSHY_CACHE_NAME}" \
SUSHY_VMEDIA_TMP="${SUSHY_VMEDIA_TMP}" \
SUSHY_PORT="${SUSHY_PORT}" \
SUSHY_CACHE_PORT="${SUSHY_CACHE_PORT}" \
  bash "${SCRIPT_DIR}/setup-sushy-tools.sh"

# --- Create the VMs and the raw inventory ---
VM_ARGS=(
  --inventory-file "${INVENTORY_FILE}"
  --network "${CT_NETWORK}"
  --disk-dir "${VM_DISK_DIR}"
  --redfish-host "${GW_IP}"
  --redfish-port "${SUSHY_PORT}"
  --count "${BMH_COUNT}"
)
python3 "${SCRIPT_DIR}/manage-virtual-bmh-vms.py" create "${VM_ARGS[@]}"

# --- Reconcile the BareMetalHosts with the raw inventory ---
python3 "${SCRIPT_DIR}/reconcile-bmhs.py" \
  --inventory-file "${INVENTORY_FILE}" \
  --namespace "${BMH_NAMESPACE}"

# Make the raw inventory path available to later workflow steps (it is
# also fixed and derivable from CLONE_NAME).
if [[ -n "${GITHUB_ENV:-}" ]]; then
  echo "INVENTORY_FILE=${INVENTORY_FILE}" >> "${GITHUB_ENV}"
fi

echo "==> Virtual BMH setup complete. ${BMH_COUNT} hosts available."
echo "Raw inventory: ${INVENTORY_FILE}"
