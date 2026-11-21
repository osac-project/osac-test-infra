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
# Same VMs and Redfish emulation as setup-virtual-bmh.sh; only the
# inventory differs: NetBox devices instead of BMHs — the
# bare-metal-fulfillment operator creates each BMH on the fly from the
# NetBox device when it claims it.
#
# This script owns the CI specifics only: CLONE_NAME-derived names
# (teardown.sh derives the same) and the clone network gateway discovery.
# The cluster is selected through KUBECONFIG or oc's default kubeconfig.
#
# Must be on PATH before running:
#   osac — logged in; the reconcile creates the BMC Secrets through the
#          OSAC Secret API
#   uv   — the reconcile runs in this repository's uv environment (the
#          workflow provides uv through astral-sh/setup-uv)
#
# Required env:
#   CLONE_NAME   — cluster-tool clone name (finds the libvirt network)
#
# Optional env:
#   BMH_COUNT        — number of virtual hosts to create (default: 2)
#   SUSHY_PORT       — sushy-tools listen port (default: 8000)
#   SUSHY_CACHE_PORT — nginx ISO cache listen port (default: 8080)
#   NETBOX_URL       — NetBox base URL. Default: the https URL of the
#                      `netbox` Route the osac-infra chart creates in
#                      NETBOX_NAMESPACE, verified against the router's CA
#                      chain (openshift-ingress/router-certs-default).
#   NETBOX_TOKEN     — NetBox API token. Default: the api_token key of the
#                      superuser Secret the NetBox chart pre-provisions.
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
NETBOX_SECRET="${NETBOX_SECRET:-osac-infra-netbox-superuser}"

CT_NETWORK="test-infra-net-${CLONE_NAME}"
VM_DISK_DIR="/tmp/virtual-bmh-disks-${CLONE_NAME}"
POOL_NAME="bmh-${CLONE_NAME}"
SUSHY_CONFIG_DIR="${HOME}/sushy-${CLONE_NAME}"
SUSHY_CACHE_NAME="sushy-cache-${CLONE_NAME}"
SUSHY_VMEDIA_TMP="${HOME}/sushy-vmedia-tmp-${CLONE_NAME}"
INVENTORY_FILE="${HOME}/virtual-bmh-inventory-${CLONE_NAME}.json"

# Export the path before any VM exists: when a later step fails, failure
# artifacts (gather-osac-logs.sh) still find the inventory — including a
# partially written one, which the redaction at collection handles.
if [[ -n "${GITHUB_ENV:-}" ]]; then
  echo "INVENTORY_FILE=${INVENTORY_FILE}" >> "${GITHUB_ENV}"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
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
ip = root.find('.//ip')
print(ip.get('address') or '' if ip is not None else '')
")
if [[ -z "${GW_IP}" ]]; then
  echo "ERROR: network ${CT_NETWORK} has no gateway <ip address=...> in its libvirt XML." >&2
  exit 1
fi
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
# Every python script runs through this repository's uv environment
# (see the header): uv must be on PATH, and one sync serves all of them.
command -v uv >/dev/null 2>&1 || {
  echo "ERROR: uv is not on PATH — run astral-sh/setup-uv before this script." >&2
  exit 1
}
uv --directory "${REPO_ROOT}" sync --frozen

VM_ARGS=(
  --inventory-file "${INVENTORY_FILE}"
  --network "${CT_NETWORK}"
  --disk-dir "${VM_DISK_DIR}"
  --redfish-host "${GW_IP}"
  --redfish-port "${SUSHY_PORT}"
  --count "${BMH_COUNT}"
)
uv --directory "${REPO_ROOT}" run --frozen \
  python3 "${SCRIPT_DIR}/manage-virtual-bmh-vms.py" create "${VM_ARGS[@]}"

# --- Fetch the NetBox API token ---
# The chart pre-provisions the superuser's token in the Secret's api_token
# key. Never printed, never passed on a command line.
if [[ -z "${NETBOX_TOKEN:-}" ]]; then
  NETBOX_TOKEN="$(oc get secret "${NETBOX_SECRET}" -n "${NETBOX_NAMESPACE}" \
    -o jsonpath='{.data.api_token}' | base64 -d)" || {
    echo "ERROR: could not read the NetBox superuser Secret ${NETBOX_SECRET} (key api_token) in namespace ${NETBOX_NAMESPACE}." >&2
    exit 1
  }
  if [[ -z "${NETBOX_TOKEN}" ]]; then
    echo "ERROR: Secret ${NETBOX_SECRET} in namespace ${NETBOX_NAMESPACE} has no api_token key." >&2
    exit 1
  fi
fi

# --- Resolve the NetBox URL ---
# NETBOX_URL comes from the caller (a lab NetBox, an exposed route) or
# defaults to the `netbox` Route the osac-infra chart creates in
# NETBOX_NAMESPACE. The chart Route is edge-terminated with the router's
# certificate, so the fallback also exports the router's own CA chain
# (openshift-ingress/router-certs-default: leaf + CA in tls.crt) as
# OSAC_NETBOX_CA_FILE — the reconcile verifies instead of skipping.
# Transport policy for a passed URL is the reconcile's own: it rejects
# plain HTTP without its explicit opt-in.
if [[ -z "${NETBOX_URL:-}" ]]; then
  NETBOX_HOST="$(oc get route netbox -n "${NETBOX_NAMESPACE}" -o jsonpath='{.spec.host}' 2>/dev/null || true)"
  if [[ -z "${NETBOX_HOST}" ]]; then
    echo "ERROR: NETBOX_URL is unset and no 'netbox' Route exists in namespace ${NETBOX_NAMESPACE}." >&2
    echo "       Pass NETBOX_URL, or install the NetBox chart (it creates the Route)." >&2
    exit 1
  fi
  NETBOX_URL="https://${NETBOX_HOST}"
  NETBOX_CA_FILE="$(mktemp)"
  trap 'rm -f "${NETBOX_CA_FILE}"' EXIT
  if ! oc get secret -n openshift-ingress router-certs-default \
    -o jsonpath='{.data.tls\.crt}' | base64 -d >"${NETBOX_CA_FILE}" || [[ ! -s "${NETBOX_CA_FILE}" ]]; then
    echo "ERROR: could not read a non-empty CA chain from openshift-ingress/router-certs-default." >&2
    exit 1
  fi
  export OSAC_NETBOX_CA_FILE="${NETBOX_CA_FILE}"
fi

# --- Reconcile the NetBox devices and BMC Secrets with the raw inventory ---
# The reconciler shares the repository uv environment synced above:
# pynetbox and requests come from pyproject.toml + uv.lock, so nothing
# is installed into the runner's interpreter.
RECONCILE_ARGS=(--inventory-file "${INVENTORY_FILE}")
OSAC_NETBOX_URL="${NETBOX_URL}" \
OSAC_NETBOX_TOKEN="${NETBOX_TOKEN}" \
  uv --directory "${REPO_ROOT}" run --frozen \
  python3 "${SCRIPT_DIR}/reconcile-netbox-devices.py" "${RECONCILE_ARGS[@]}"

echo "==> Virtual NetBox device setup complete. ${BMH_COUNT} devices staged."
echo "Raw inventory: ${INVENTORY_FILE}"
