#!/usr/bin/env bash
# Provision virtual NetBox devices for BMaaS E2E tests (netbox inventory
# backend).
#
# Thin orchestrator over four tools (no CLONE_NAME in any of them):
#   1. activate-ironic.sh           — Ironic activation (Provisioning CR)
#   2. setup-sushy-tools.sh         — storage pool, sushy-tools emulator,
#                                     nginx virtual-media cache
#   3. manage-virtual-bmh-vms.py    — libvirt VMs; maintains the raw inventory
#   4. reconcile-netbox-devices.py  — NetBox devices and Secret API BMC
#                                     Secrets reconciled with the raw inventory
#
# The counterpart of setup-virtual-bmh.sh: the VMs and the Redfish
# emulation are identical — only the inventory handover differs, NetBox
# devices instead of BareMetalHosts, because the operator creates each
# BMH itself when it claims a device.
#
# This script owns the CI specifics: CLONE_NAME-derived names (teardown.sh
# derives the same) and the clone network gateway discovery. The cluster
# is selected through KUBECONFIG or oc's default kubeconfig. The osac CLI
# must be on PATH and logged in: the reconcile creates the BMC Secrets
# through the OSAC Secret API.
#
# Required env:
#   CLONE_NAME   — cluster-tool clone name (finds the libvirt network)
#
# Optional env:
#   BMH_COUNT        — number of virtual hosts to create (default: 2)
#   SUSHY_PORT       — sushy-tools listen port (default: 8000)
#   SUSHY_CACHE_PORT — nginx ISO cache listen port (default: 8080)
#   NETBOX_URL       — NetBox base URL. Default: an oc port-forward to the
#                      in-cluster service (plain HTTP on the loopback).
#   NETBOX_TOKEN     — NetBox API token. Default: the api_token key of the
#                      superuser Secret the NetBox chart pre-provisions.
#   NETBOX_NAMESPACE — namespace of the NetBox release (default: osac-infra)
#   NETBOX_SERVICE   — NetBox service name (default: osac-infra-netbox)
#   NETBOX_SECRET    — NetBox superuser Secret name (default:
#                      osac-infra-netbox-superuser)
#
# The raw inventory is written to
#   ${HOME}/virtual-bmh-inventory-${CLONE_NAME}.json
# and exported as INVENTORY_FILE through $GITHUB_ENV for later steps.
set -euo pipefail

: "${CLONE_NAME:?CLONE_NAME is required}"

BMH_COUNT="${BMH_COUNT:-2}"
SUSHY_PORT="${SUSHY_PORT:-8000}"
SUSHY_CACHE_PORT="${SUSHY_CACHE_PORT:-8080}"
NETBOX_NAMESPACE="${NETBOX_NAMESPACE:-osac-infra}"
NETBOX_SERVICE="${NETBOX_SERVICE:-osac-infra-netbox}"
NETBOX_SECRET="${NETBOX_SECRET:-osac-infra-netbox-superuser}"
NETBOX_PORT_FORWARD_PORT="${NETBOX_PORT_FORWARD_PORT:-18080}"

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

# --- Fetch the NetBox API token ---
# The chart pre-provisions the superuser's token in the Secret's api_token
# key. Never printed, never passed on a command line.
if [[ -z "${NETBOX_TOKEN:-}" ]]; then
  NETBOX_TOKEN="$(oc get secret "${NETBOX_SECRET}" -n "${NETBOX_NAMESPACE}" \
    -o jsonpath='{.data.api_token}' | base64 -d)"
fi

# --- Reach the NetBox service ---
# The runner cannot resolve the in-cluster service DNS: forward the
# service port to the loopback unless an URL was given (a route, a lab
# NetBox). The forward dies with this script — the reconcile is the only
# consumer.
RECONCILE_ARGS=(--inventory-file "${INVENTORY_FILE}")
if [[ -n "${NETBOX_URL:-}" ]]; then
  PORT_FORWARD_LOG=""
else
  echo "==> Port-forwarding the NetBox service..."
  PORT_FORWARD_LOG="${HOME}/netbox-port-forward-${CLONE_NAME}.log"
  oc port-forward -n "${NETBOX_NAMESPACE}" "svc/${NETBOX_SERVICE}" \
    "${NETBOX_PORT_FORWARD_PORT}:80" >"${PORT_FORWARD_LOG}" 2>&1 &
  PORT_FORWARD_PID=$!
  trap 'kill "${PORT_FORWARD_PID}" 2>/dev/null || true' EXIT
  NETBOX_URL="http://127.0.0.1:${NETBOX_PORT_FORWARD_PORT}"
  for attempt in $(seq 1 30); do
    if curl -sf "${NETBOX_URL}/api/" >/dev/null 2>&1; then
      break
    fi
    if [[ "${attempt}" -eq 30 ]]; then
      echo "ERROR: NetBox not reachable through the port-forward." >&2
      tail "${PORT_FORWARD_LOG}" >&2 || true
      exit 1
    fi
    sleep 2
  done
fi

# Plain HTTP — the port-forward, or an in-cluster service URL — needs the
# reconcile's explicit opt-in; an https URL keeps TLS verification.
if [[ "${NETBOX_URL}" == http://* ]]; then
  RECONCILE_ARGS+=(--insecure)
fi

# --- Reconcile the NetBox devices and BMC Secrets with the raw inventory ---
# The runner's python3 is not the repository environment: install the
# pynetbox version the repository's uv.lock pins (requests comes along).
pip install --quiet 'pynetbox==7.8.0'

OSAC_NETBOX_URL="${NETBOX_URL}" \
OSAC_NETBOX_TOKEN="${NETBOX_TOKEN}" \
  python3 "${SCRIPT_DIR}/reconcile-netbox-devices.py" "${RECONCILE_ARGS[@]}"

# Make the raw inventory path available to later workflow steps (it is
# also fixed and derivable from CLONE_NAME).
if [[ -n "${GITHUB_ENV:-}" ]]; then
  echo "INVENTORY_FILE=${INVENTORY_FILE}" >> "${GITHUB_ENV}"
fi

echo "==> Virtual NetBox device setup complete. ${BMH_COUNT} devices staged."
echo "Raw inventory: ${INVENTORY_FILE}"
