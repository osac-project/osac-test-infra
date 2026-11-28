#!/usr/bin/env bash
# vcluster-run-bmhs.sh -- provision (or remove) one OSAC E2E run's virtual
# bare-metal hosts on the shared outer cluster and this machine.
#
# The b and c suites need BareMetalHosts: the BMaaS tests order
# BareMetalInstances against them, and the CaaS tests get their
# bare-metal workers from them. On a shared outer cluster every run owns
# its own group, and every object is named after the run:
#
#   namespace   <run>-hosts (labelled osac.openshift.io/instance=<run>)
#   BMHs        bmh-<run>-<token>   BMC secrets <bmh name>-bmc-secret
#   libvirt     pool bmh-<run>, domains bmh-<run>-<token>,
#               disks /tmp/virtual-bmh-disks-<run>
#   Redfish     sushy-tools on a run-derived port pair (~/sushy-<run>)
#   inventory   <output dir>/virtual-bmh-inventory.json
#
# The script:
#   1. checks every precondition against the outer cluster and this
#      machine and fails loudly;
#   2. applies namespace <run>-hosts and the namespaced Role/RoleBinding
#      that let the outer osac namespace's osac-remote-access service
#      account manage BareMetalHosts, HardwareData and BMC secrets there:
#      in remote mode the BMF chart renders no such RBAC
#      (bare-metal-fulfillment-operator/charts/operator/templates/
#      bmc-secret-role.yaml);
#   3. starts the run's sushy-tools emulator and nginx virtual-media cache
#      (setup-sushy-tools.sh) on the run's ports and storage pool;
#   4. creates the run's VM group and its raw inventory
#      (manage-virtual-bmh-vms.py);
#   5. reconciles the run's BareMetalHosts with the inventory and waits for
#      them to become available (reconcile-bmhs.py).
#
# activate-ironic.sh is deliberately NOT run here: Ironic activation is a
# one-time outer-cluster ops step (idempotent), shared by every run. The
# script verifies it happened and points at the script otherwise.
#
# --delete reverses steps 2-5 for the run's teardown: the VMs go first,
# then the BMHs (emptied-inventory reconcile, with finalizer stripping),
# then the per-run RBAC and namespace, then the sushy/pool/disk artifacts.
#
# Usage:
#   KUBECONFIG=/path/to/outer-kubeconfig \
#       ./scripts/vcluster-run-bmhs.sh RUN_NAME --network NET [options]
#   KUBECONFIG=/path/to/outer-kubeconfig \
#       ./scripts/vcluster-run-bmhs.sh RUN_NAME --delete [options]
#
# Options:
#   --network NAME      libvirt network the outer SNO node reaches
#                       (required for create, unused on --delete; the CI
#                       scripts derive test-infra-net-<clone>, runs pass
#                       the shared outer/poc network name)
#   --delete            remove the run's hosts and host-side artifacts
#                       instead of creating them
#   --output-dir PATH   where the run's files live
#                       (default: <repo>/.vcluster-runs/RUN_NAME; must be
#                       the same value vcluster-run-setup.sh was given)
#   --port-offset N     offset into the run's port block (default: derived
#                       from the run name's checksum; ports are
#                       8100 + 2*N and 8101 + 2*N for N in 0-63)
#   -h, --help          show this help
#
# Environment:
#   KUBECONFIG          kubeconfig of the outer cluster (required)
#   BMH_COUNT           virtual BMHs per run (default: 2)
#   BMH_CPU             vCPUs per virtual BMH (default: 4)
#   BMH_MEMORY_MIB      memory per virtual BMH in MiB (default: 8192)
#   BMH_DISK_SIZE       qcow2 virtual disk size (default: 150G)
#   VM_DISK_DIR         parent directory for the run's VM disks
#                       (default: /tmp; must be absolute)
set -euo pipefail

###############################################################################
# Constants
###############################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TOOLS_DIR="${REPO_ROOT}/.github/scripts"

# The run-identity label every object a run creates in the outer cluster
# carries (the design's convention, same key as vcluster-run-setup.sh). It
# marks the hosts namespace, so the teardown knows the namespace is this
# run's to delete and a concurrent run's is not.
INSTANCE_LABEL_KEY="osac.openshift.io/instance"

# The outer cluster's shared OSAC namespace: it holds the
# osac-remote-access service account the operator and the BMF operator use
# to reach the outer cluster (the per-run RBAC below is bound to it).
REMOTE_ACCESS_NAMESPACE="osac"
REMOTE_ACCESS_SERVICE_ACCOUNT="osac-remote-access"

# Name of the per-run Role and RoleBinding in <run>-hosts. The BMC secrets
# have dynamic names, so RBAC cannot narrow by name; that is exactly why
# the chart keeps the BMH namespace dedicated (see the header).
HOST_RBAC_NAME="osac-remote-access-bmh"

# Ironic activation is a cluster-wide ops step (activate-ironic.sh), so
# the script only verifies its outcome: the Provisioning CR and the BMH
# admission webhook.
PROVISIONING_CR="provisioning-configuration"
METAL3_NAMESPACE="openshift-machine-api"
METAL3_WEBHOOK_SERVICE="baremetal-operator-webhook-service"

# Defaults from setup-virtual-bmh.sh: 2 hosts x 4 vCPU / 8 GiB / 150G.
BMH_COUNT="${BMH_COUNT:-2}"
BMH_CPU="${BMH_CPU:-4}"
BMH_MEMORY_MIB="${BMH_MEMORY_MIB:-8192}"
BMH_DISK_SIZE="${BMH_DISK_SIZE:-150G}"

# Each run gets two consecutive ports out of 8100-8227: the fixed sushy
# 8000/8080 pair would collide between concurrent runs on one machine.
BMH_SUSHY_PORT_BASE=8100
BMH_PORT_SPAN=64

VIRSH=(virsh -c qemu:///system)

###############################################################################
# Arguments and globals
###############################################################################

RUN_NAME=""
NETWORK=""
OUTPUT_DIR=""
DELETE="false"
PORT_OFFSET=""

# Run-derived names, set in main once RUN_NAME is validated. The teardown
# derives the same values from the same run name, so a rerun of either
# script finds exactly the objects the other created.
BMH_NAMESPACE=""      # <run>-hosts
VM_NAME_PREFIX=""     # bmh-<run>; VM names are <prefix>-<hex token>
POOL_NAME=""          # bmh-<run>
SUSHY_CONFIG_DIR=""   # ~/sushy-<run>
SUSHY_CACHE_NAME=""   # sushy-cache-<run> (container and volume)
SUSHY_VMEDIA_TMP=""   # ~/sushy-vmedia-tmp-<run>
INVENTORY_FILE=""     # <output dir>/virtual-bmh-inventory.json

VM_DISK_ROOT=""
VM_DISK_DIR="${VM_DISK_DIR_OVERRIDE:-}"

NETWORK_GATEWAY=""
SUSHY_PORT=""
SUSHY_CACHE_PORT=""

# Deletions record failures instead of aborting, so one stuck object never
# leaves the rest of a run behind; the final verification fails loudly.
DELETE_FAILURES=()

usage() {
    cat <<'EOF'
Usage:
  KUBECONFIG=/path/to/outer-kubeconfig \
      ./scripts/vcluster-run-bmhs.sh RUN_NAME --network NET [options]
  KUBECONFIG=/path/to/outer-kubeconfig \
      ./scripts/vcluster-run-bmhs.sh RUN_NAME --delete [options]

Options:
  --network NAME      libvirt network the outer SNO node reaches (required
                      for create; the CI scripts derive
                      test-infra-net-<clone>, runs pass the shared
                      outer/poc network name)
  --delete            remove the run's hosts and host-side artifacts
                      instead of creating them
  --output-dir PATH   where the run's files live
                      (default: <repo>/.vcluster-runs/RUN_NAME; must be the
                      same value vcluster-run-setup.sh was given)
  --port-offset N     offset into the run's port block (default: derived
                      from the run name's checksum)
  -h, --help          show this help

Provisions namespace <run>-hosts with its per-run RBAC, the run's
sushy-tools emulator, its libvirt VM group and its BareMetalHosts.
Ironic must already be active on the outer cluster (activate-ironic.sh is
a one-time ops step, not run here). --delete reverses all of it.
EOF
}

die() {
    echo "ERROR: $*" >&2
    exit 1
}

require_command() {
    local command_name="$1"
    command -v "${command_name}" >/dev/null 2>&1 \
        || die "${command_name} is not installed or not in PATH; install it and rerun."
}

record_failure() {
    DELETE_FAILURES+=("$*")
    echo "ERROR: $*" >&2
}

###############################################################################
# Preconditions
###############################################################################

# Read the run's suite letter from its name exactly like
# vcluster-run-setup.sh's suite_from_run_name: the last letter before the
# trailing attempt digits. Only the b and c suites have bare-metal hosts.
check_suite_letter() {
    local base="${RUN_NAME}"
    while [[ "${base}" =~ [0-9-]$ ]]; do
        base="${base%?}"
    done
    case "${base: -1}" in
        b|c) ;;
        *)
            die "Run name '${RUN_NAME}' does not carry the BMaaS (b) or CaaS (c) suite letter; only those suites have virtual bare-metal hosts."
            ;;
    esac
}

check_outer_cluster() {
    [[ -n "${KUBECONFIG:-}" ]] \
        || die "KUBECONFIG is not set. Point it at the outer cluster's kubeconfig."
    [[ -f "${KUBECONFIG}" ]] \
        || die "KUBECONFIG (${KUBECONFIG}) is not a file."
    echo "Checking outer cluster connectivity..."
    kubectl get --raw /readyz >/dev/null \
        || die "Cannot reach the outer cluster with KUBECONFIG=${KUBECONFIG}."
}

# Ironic must be active before any BMH can be created: its webhook validates
# every BareMetalHost. Activation itself is the lead's one-time ops step
# (activate-ironic.sh), so a missing activation is a loud precondition, not
# something this script silently fixes for one run.
check_ironic_active() {
    if ! kubectl get provisionings.metal3.io "${PROVISIONING_CR}" -o name >/dev/null 2>&1; then
        die "Ironic is not active on the outer cluster (no Provisioning/${PROVISIONING_CR}). Run .github/scripts/activate-ironic.sh once on the outer cluster; that is a shared ops step, not per run."
    fi
    local endpoints
    endpoints="$(kubectl --namespace "${METAL3_NAMESPACE}" get endpoints "${METAL3_WEBHOOK_SERVICE}" \
        -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null || true)"
    [[ -n "${endpoints}" ]] \
        || die "Service ${METAL3_WEBHOOK_SERVICE} in ${METAL3_NAMESPACE} has no ready endpoints; the BareMetalHost webhook cannot admit hosts. Run .github/scripts/activate-ironic.sh on the outer cluster."
    echo "Ironic is active; BMH webhook endpoints: ${endpoints}."
}

check_remote_access_service_account() {
    kubectl --namespace "${REMOTE_ACCESS_NAMESPACE}" get serviceaccount \
        "${REMOTE_ACCESS_SERVICE_ACCOUNT}" -o name >/dev/null 2>&1 \
        || die "ServiceAccount ${REMOTE_ACCESS_SERVICE_ACCOUNT} is missing in namespace ${REMOTE_ACCESS_NAMESPACE} of the outer cluster; the per-run BMH RBAC would bind nothing. Was the shared OSAC install run?"
}

# The hosts namespace may already exist from a rerun; it may not belong to
# another run. An existent namespace with this run's label (or none at all,
# which only a hand-created one has) is reused.
check_host_namespace_ownership() {
    local output
    if ! output="$(kubectl get namespace "${BMH_NAMESPACE}" \
        -o jsonpath="{.metadata.labels.${INSTANCE_LABEL_KEY//./\\.}}" 2>&1)"; then
        case "${output}" in
            *NotFound*) return ;;
            *) die "Cannot read namespace ${BMH_NAMESPACE}: ${output}" ;;
        esac
    fi
    if [[ -n "${output}" && "${output}" != "${RUN_NAME}" ]]; then
        die "Namespace ${BMH_NAMESPACE} carries ${INSTANCE_LABEL_KEY}=${output}, not ${RUN_NAME}; refusing to attach this run's hosts to another run's namespace."
    fi
}

# Discover the libvirt network and its gateway IP: the gateway is where the
# VMs live and where sushy-tools must listen for the outer node.
check_run_network() {
    [[ -n "${NETWORK}" ]] \
        || die "--network is required for create: name the libvirt network the outer SNO node reaches (the shared outer/poc network)."
    if ! "${VIRSH[@]}" net-info "${NETWORK}" >/dev/null 2>&1; then
        echo "ERROR: libvirt network '${NETWORK}' not found." >&2
        "${VIRSH[@]}" net-list --all >&2 || true
        exit 1
    fi
    NETWORK_GATEWAY="$("${VIRSH[@]}" net-dumpxml "${NETWORK}" | python3 -c '
import sys
import xml.etree.ElementTree as ET

root = ET.parse(sys.stdin).getroot()
address = root.find(".//ip").get("address")
if not address:
    raise SystemExit("network has no IPv4 address element")
print(address)
')" || die "Cannot read the gateway IP of libvirt network ${NETWORK}."
    echo "libvirt network ${NETWORK} gateway: ${NETWORK_GATEWAY}."
}

port_is_free() {
    python3 - "$1" <<'PY'
import socket
import sys

sock = socket.socket()
try:
    sock.bind(("0.0.0.0", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    sock.close()
PY
}

# A rerun of the same run reuses its own emulator and cache (the setup tool
# replaces them); any other holder is a concurrent run whose hash collided,
# and silently sharing the port would cross-wire two runs' BMCs.
check_ports_available() {
    if [[ -f "${SUSHY_CONFIG_DIR}/sushy.pid" ]] \
        || podman container exists "${SUSHY_CACHE_NAME}" 2>/dev/null; then
        return
    fi
    local port
    for port in "${SUSHY_PORT}" "${SUSHY_CACHE_PORT}"; do
        if ! port_is_free "${port}"; then
            die "Port ${port} is already in use and is not this run's (no ${SUSHY_CONFIG_DIR}/sushy.pid, no ${SUSHY_CACHE_NAME} container). Another run holds it; pass a different --port-offset for run ${RUN_NAME}."
        fi
    done
}

check_preconditions() {
    require_command kubectl
    require_command oc
    require_command virsh
    require_command qemu-img
    require_command python3
    require_command podman
    require_command pip
    require_command curl

    check_outer_cluster
    check_ironic_active
    check_remote_access_service_account
    check_host_namespace_ownership
    check_run_network
    check_ports_available
}

###############################################################################
# Outer cluster: namespace, RBAC, BareMetalHosts
###############################################################################

apply_host_namespace() {
    kubectl apply -f - <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: ${BMH_NAMESPACE}
  labels:
    ${INSTANCE_LABEL_KEY}: ${RUN_NAME}
EOF
    echo "Ensured namespace ${BMH_NAMESPACE} (${INSTANCE_LABEL_KEY}=${RUN_NAME})."
}

# In remote mode the BMF chart renders no BMH RBAC (it only has the BMC
# secret Role/RoleBinding inside the release namespace, for an in-cluster
# BMH namespace). The run's hosts live on the outer cluster, so the outer
# osac-remote-access service account needs the rules here: full BMH and
# HardwareData management (create/update/patch/delete plus the reads the
# reconcilers do) and BMC Secret management, the same verbs the chart's
# own bmc-secret-role.yaml grants.
apply_host_rbac() {
    kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: ${HOST_RBAC_NAME}
  namespace: ${BMH_NAMESPACE}
  labels:
    ${INSTANCE_LABEL_KEY}: ${RUN_NAME}
rules:
  - apiGroups: ["metal3.io"]
    resources: ["baremetalhosts", "hardwaredata"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: [""]
    resources: ["secrets"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: ${HOST_RBAC_NAME}
  namespace: ${BMH_NAMESPACE}
  labels:
    ${INSTANCE_LABEL_KEY}: ${RUN_NAME}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: ${HOST_RBAC_NAME}
subjects:
  - kind: ServiceAccount
    name: ${REMOTE_ACCESS_SERVICE_ACCOUNT}
    namespace: ${REMOTE_ACCESS_NAMESPACE}
EOF
    echo "Granted ${REMOTE_ACCESS_NAMESPACE}:${REMOTE_ACCESS_SERVICE_ACCOUNT} BMH, HardwareData and secret management in ${BMH_NAMESPACE}."
}

# The raw inventory's group prefix is run-derived (not the tool's random
# one), so the teardown, the leftover scan and an operator all find the
# run's VMs by name. Writing the file before any host is added persists the
# prefix exactly like RawInventory.ensure_name_prefix does.
write_empty_inventory() {
    python3 - "${INVENTORY_FILE}" "${VM_NAME_PREFIX}" <<'PY'
import json
import sys

path, name_prefix = sys.argv[1], sys.argv[2]
with open(path, "w") as inventory_file:
    json.dump({"name_prefix": name_prefix, "hosts": []}, inventory_file, indent=2)
    inventory_file.write("\n")
PY
}

ensure_inventory_file() {
    if [[ -f "${INVENTORY_FILE}" ]]; then
        local existing_prefix
        existing_prefix="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("name_prefix", ""))' \
            "${INVENTORY_FILE}")" \
            || die "Cannot read the raw inventory ${INVENTORY_FILE}."
        [[ "${existing_prefix}" == "${VM_NAME_PREFIX}" ]] \
            || die "Raw inventory ${INVENTORY_FILE} holds group '${existing_prefix}', not '${VM_NAME_PREFIX}'; refusing to mix two VM groups."
        return
    fi
    write_empty_inventory
    echo "Wrote raw inventory ${INVENTORY_FILE} for group ${VM_NAME_PREFIX}."
}

inventory_host_count() {
    python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("hosts", [])))' \
        "${INVENTORY_FILE}"
}

start_redfish_services() {
    echo "==> Starting the run's Redfish infrastructure on ${NETWORK_GATEWAY} (sushy :${SUSHY_PORT}, cache :${SUSHY_CACHE_PORT})..."
    SUSHY_LISTEN_IP="${NETWORK_GATEWAY}" \
    STORAGE_POOL_NAME="${POOL_NAME}" \
    STORAGE_DIR="${VM_DISK_DIR}" \
    SUSHY_CONFIG_DIR="${SUSHY_CONFIG_DIR}" \
    SUSHY_CACHE_NAME="${SUSHY_CACHE_NAME}" \
    SUSHY_VMEDIA_TMP="${SUSHY_VMEDIA_TMP}" \
    SUSHY_PORT="${SUSHY_PORT}" \
    SUSHY_CACHE_PORT="${SUSHY_CACHE_PORT}" \
        bash "${TOOLS_DIR}/setup-sushy-tools.sh"
}

create_vm_group() {
    local existing_count needed
    existing_count="$(inventory_host_count)"
    if [[ "${existing_count}" -ge "${BMH_COUNT}" ]]; then
        echo "The run already holds ${existing_count} VM(s); not adding more (raise BMH_COUNT to grow the group)."
        return
    fi
    needed=$(( BMH_COUNT - existing_count ))
    echo "==> Creating ${needed} VM(s) for run ${RUN_NAME} on network ${NETWORK}..."
    python3 "${TOOLS_DIR}/manage-virtual-bmh-vms.py" create \
        --inventory-file "${INVENTORY_FILE}" \
        --network "${NETWORK}" \
        --disk-dir "${VM_DISK_DIR}" \
        --redfish-host "${NETWORK_GATEWAY}" \
        --redfish-port "${SUSHY_PORT}" \
        --count "${needed}" \
        --cpu "${BMH_CPU}" \
        --memory-mib "${BMH_MEMORY_MIB}" \
        --disk-size "${BMH_DISK_SIZE}"
}

reconcile_bmhs() {
    echo "==> Reconciling the run's BareMetalHosts in ${BMH_NAMESPACE} (inspection takes 3-5 minutes)..."
    python3 "${TOOLS_DIR}/reconcile-bmhs.py" \
        --inventory-file "${INVENTORY_FILE}" \
        --namespace "${BMH_NAMESPACE}"
}

print_created_summary() {
    local host_count
    host_count="$(inventory_host_count)"
    echo
    echo "Run ${RUN_NAME} has ${host_count} virtual BareMetalHosts in namespace ${BMH_NAMESPACE}."
    echo "Raw inventory: ${INVENTORY_FILE} (VM group ${VM_NAME_PREFIX}-*)"
    echo "Redfish: http://${NETWORK_GATEWAY}:${SUSHY_PORT}, virtual-media cache :${SUSHY_CACHE_PORT}"
    echo "Remove them again with: $0 ${RUN_NAME} --delete"
    echo
    echo "Run the suite with:"
    echo "  OSAC_BMH_NAMESPACE=${BMH_NAMESPACE}"
}

create_run_bmhs() {
    check_suite_letter
    check_preconditions
    mkdir -p "${OUTPUT_DIR}"
    ensure_inventory_file
    apply_host_namespace
    apply_host_rbac
    start_redfish_services
    create_vm_group
    reconcile_bmhs
    print_created_summary
}

###############################################################################
# Teardown
###############################################################################

# Same identity guard as scripts/vcluster-run-teardown.sh: the PID file's
# process start time proves the PID still belongs to this run's emulator, so
# a reused PID is never killed.
sushy_process_matches() {
    local pid="$1" identity="$2"
    [[ -n "${pid}" && -n "${identity}" \
        && "$(ps -p "${pid}" -o lstart= 2>/dev/null)" == "${identity}" ]]
}

stop_sushy_emulator() {
    local pid_file="${SUSHY_CONFIG_DIR}/sushy.pid"
    local identity_file="${SUSHY_CONFIG_DIR}/sushy.identity"
    local pid="" identity="" attempt
    [[ -f "${pid_file}" ]] || return
    pid="$(cat "${pid_file}")"
    if [[ -f "${identity_file}" ]]; then
        identity="$(cat "${identity_file}")"
    fi
    if [[ -z "${identity}" ]] && [[ -n "$(ps -p "${pid}" -o pid= 2>/dev/null)" ]]; then
        record_failure "sushy-emulator PID ${pid} has no identity record in ${SUSHY_CONFIG_DIR}; leaving it and its config in place."
        return
    fi
    if sushy_process_matches "${pid}" "${identity}"; then
        echo "Stopping sushy-emulator (PID ${pid})..."
        kill "${pid}" 2>/dev/null || true
        for attempt in $(seq 1 10); do
            sushy_process_matches "${pid}" "${identity}" || break
            sleep 1
        done
        if sushy_process_matches "${pid}" "${identity}"; then
            echo "  SIGTERM did not stop sushy-emulator, sending SIGKILL..."
            kill -9 "${pid}" 2>/dev/null || true
        fi
    fi
    rm -f "${pid_file}" "${identity_file}"
}

remove_virtual_media_cache() {
    if ! command -v podman >/dev/null 2>&1; then
        record_failure "podman is not installed; the virtual-media cache container and volume ${SUSHY_CACHE_NAME} may remain."
        return
    fi
    if podman container exists "${SUSHY_CACHE_NAME}" 2>/dev/null; then
        echo "Stopping nginx virtual-media cache ${SUSHY_CACHE_NAME}..."
        podman stop --time 10 "${SUSHY_CACHE_NAME}" >/dev/null 2>&1 || true
        podman rm --force "${SUSHY_CACHE_NAME}" >/dev/null 2>&1 || true
    fi
    if podman volume exists "${SUSHY_CACHE_NAME}" 2>/dev/null; then
        podman volume rm --force "${SUSHY_CACHE_NAME}" >/dev/null 2>&1 || true
    fi
}

# Remove every libvirt domain of the run's group. The names come from
# libvirt itself, not the inventory, so a VM created before the inventory
# was saved is removed too; the inventory is rewritten by the tool as it
# goes. manage-virtual-bmh-vms.py refuses names outside the group prefix.
delete_vm_group() {
    local listing vm_names=()
    if ! listing="$("${VIRSH[@]}" list --all --name 2>/dev/null)"; then
        record_failure "cannot list libvirt domains; the run's VMs could not be removed or verified."
        return
    fi
    mapfile -t vm_names < <(grep -E "^${VM_NAME_PREFIX}-[0-9a-f]{2}$" <<<"${listing}" || true)
    if [[ ${#vm_names[@]} -eq 0 ]]; then
        echo "No libvirt domains of group ${VM_NAME_PREFIX} remain."
        return
    fi
    ensure_inventory_file
    echo "==> Deleting ${#vm_names[@]} VM(s) of group ${VM_NAME_PREFIX}..."
    python3 "${TOOLS_DIR}/manage-virtual-bmh-vms.py" delete \
        --inventory-file "${INVENTORY_FILE}" \
        "${vm_names[@]}"
}

# The BMHs are deleted through reconcile-bmhs.py with an emptied inventory:
# implement exactly the pattern that tool already applies to hosts the
# inventory no longer lists -- delete without waiting, then strip the
# finalizers (the backing VMs are gone, so Ironic never finishes its own
# cleanup and the finalizers would otherwise hold the namespace forever).
delete_bmhs() {
    local output
    if ! output="$(kubectl get namespace "${BMH_NAMESPACE}" -o name 2>&1)"; then
        case "${output}" in
            *NotFound*)
                echo "Namespace ${BMH_NAMESPACE} does not exist; no BareMetalHosts to delete."
                return
                ;;
            *)
                record_failure "cannot read namespace ${BMH_NAMESPACE}: ${output}"
                return
                ;;
        esac
    fi
    mkdir -p "${OUTPUT_DIR}"
    write_empty_inventory
    echo "==> Deleting the run's BareMetalHosts (emptied inventory, finalizers stripped)..."
    python3 "${TOOLS_DIR}/reconcile-bmhs.py" \
        --inventory-file "${INVENTORY_FILE}" \
        --namespace "${BMH_NAMESPACE}"
}

delete_host_rbac() {
    kubectl get namespace "${BMH_NAMESPACE}" -o name >/dev/null 2>&1 || return
    kubectl --namespace "${BMH_NAMESPACE}" delete role "${HOST_RBAC_NAME}" --ignore-not-found
    kubectl --namespace "${BMH_NAMESPACE}" delete rolebinding "${HOST_RBAC_NAME}" --ignore-not-found
    echo "Deleted the per-run BMH RBAC in ${BMH_NAMESPACE}."
}

wait_for_namespace_absent() {
    local attempt output
    for attempt in $(seq 1 120); do
        if ! output="$(kubectl get namespace "${BMH_NAMESPACE}" -o name 2>&1)"; then
            case "${output}" in
                *NotFound*)
                    echo "Namespace ${BMH_NAMESPACE} is gone."
                    return
                    ;;
                *)
                    record_failure "cannot read namespace ${BMH_NAMESPACE}: ${output}"
                    return
                    ;;
            esac
        fi
        echo "  Waiting for namespace ${BMH_NAMESPACE} to disappear (attempt ${attempt}/120)..."
        sleep 5
    done
    record_failure "namespace ${BMH_NAMESPACE} did not disappear within 600s. Check what still holds it (finalizers, operator)."
}

delete_host_namespace() {
    kubectl delete namespace "${BMH_NAMESPACE}" --ignore-not-found
    wait_for_namespace_absent
}

destroy_storage_pool() {
    if ! "${VIRSH[@]}" pool-info "${POOL_NAME}" >/dev/null 2>&1; then
        echo "libvirt storage pool ${POOL_NAME} is already gone."
        return
    fi
    echo "Removing libvirt storage pool ${POOL_NAME}..."
    "${VIRSH[@]}" pool-destroy "${POOL_NAME}" 2>/dev/null || true
    "${VIRSH[@]}" pool-undefine "${POOL_NAME}" 2>/dev/null || true
}

remove_run_artifacts() {
    local path
    for path in "${VM_DISK_DIR}" "${SUSHY_CONFIG_DIR}" "${SUSHY_VMEDIA_TMP}"; do
        # A preserved PID file means stop_sushy_emulator could not prove the
        # process was this run's; the config dir is then the only map to it.
        if [[ "${path}" == "${SUSHY_CONFIG_DIR}" && -f "${SUSHY_CONFIG_DIR}/sushy.pid" ]]; then
            echo "Keeping ${SUSHY_CONFIG_DIR}: its sushy.pid was preserved (see above)."
            continue
        fi
        if [[ -d "${path}" ]]; then
            rm -rf -- "${path}"
            echo "Deleted ${path}."
        fi
    done
    rm -f "${INVENTORY_FILE}"
}

verify_delete() {
    local listing failures=("${DELETE_FAILURES[@]}")

    if listing="$("${VIRSH[@]}" list --all --name 2>/dev/null)"; then
        if grep -qE "^${VM_NAME_PREFIX}-[0-9a-f]{2}$" <<<"${listing}"; then
            failures+=("libvirt domains of group ${VM_NAME_PREFIX}: $(grep -E "^${VM_NAME_PREFIX}-[0-9a-f]{2}$" <<<"${listing}" | tr '\n' ' ')")
        fi
    else
        failures+=("could not list libvirt domains")
    fi
    if "${VIRSH[@]}" pool-info "${POOL_NAME}" >/dev/null 2>&1; then
        failures+=("libvirt storage pool ${POOL_NAME}")
    fi
    if kubectl get namespace "${BMH_NAMESPACE}" -o name >/dev/null 2>&1; then
        failures+=("namespace ${BMH_NAMESPACE}")
    fi
    if [[ -d "${VM_DISK_DIR}" || -d "${SUSHY_CONFIG_DIR}" || -d "${SUSHY_VMEDIA_TMP}" ]]; then
        failures+=("host-side artifact directories under ${VM_DISK_DIR}, ${SUSHY_CONFIG_DIR} or ${SUSHY_VMEDIA_TMP}")
    fi

    if [[ ${#failures[@]} -gt 0 ]]; then
        echo "ERROR: the virtual bare-metal hosts of run ${RUN_NAME} did not clean up completely:" >&2
        printf '  - %s\n' "${failures[@]}" >&2
        exit 1
    fi

    echo
    echo "Virtual BMH teardown complete: no namespace ${BMH_NAMESPACE}, no BMH or" \
        "HardwareData of run ${RUN_NAME}, no VMs, pool, sushy emulator or" \
        "virtual-media cache named after it."
}

# The teardown deletes namespace <run>-hosts whole, so it must never do so
# for a namespace that carries another run's instance label: the name is
# run-derived and the label is the authority on ownership.
check_delete_ownership() {
    local output
    if ! output="$(kubectl get namespace "${BMH_NAMESPACE}" \
        -o jsonpath="{.metadata.labels.${INSTANCE_LABEL_KEY//./\\.}}" 2>&1)"; then
        case "${output}" in
            *NotFound*) return ;;
            *) record_failure "cannot read namespace ${BMH_NAMESPACE}: ${output}" ;;
        esac
        return
    fi
    if [[ -n "${output}" && "${output}" != "${RUN_NAME}" ]]; then
        die "Namespace ${BMH_NAMESPACE} carries ${INSTANCE_LABEL_KEY}=${output}, not ${RUN_NAME}; refusing to delete another run's hosts."
    fi
}

delete_run_bmhs() {
    require_command kubectl
    require_command oc
    require_command virsh
    require_command python3
    check_outer_cluster
    check_delete_ownership
    mkdir -p "${OUTPUT_DIR}"

    stop_sushy_emulator
    remove_virtual_media_cache
    delete_vm_group
    delete_bmhs
    delete_host_rbac
    delete_host_namespace
    destroy_storage_pool
    remove_run_artifacts
    verify_delete
}

###############################################################################
# Main
###############################################################################

main() {
    local positional_args=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --network)
                [[ $# -ge 2 ]] || die "--network needs a value."
                NETWORK="$2"
                shift 2
                ;;
            --delete)
                DELETE="true"
                shift
                ;;
            --output-dir)
                [[ $# -ge 2 ]] || die "--output-dir needs a value."
                OUTPUT_DIR="$2"
                shift 2
                ;;
            --port-offset)
                [[ $# -ge 2 ]] || die "--port-offset needs a value."
                PORT_OFFSET="$2"
                shift 2
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            -*)
                die "Unknown option: $1"
                ;;
            *)
                positional_args+=("$1")
                shift
                ;;
        esac
    done

    [[ ${#positional_args[@]} -eq 1 ]] \
        || die "Exactly one run name is required. Usage: $0 RUN_NAME [--network NET] [--delete] [options]."
    RUN_NAME="${positional_args[0]}"
    [[ "${RUN_NAME}" =~ ^[a-z0-9]([-a-z0-9]{0,10}[a-z0-9])?$ ]] \
        || die "Run name '${RUN_NAME}' must be a DNS label of lowercase letters, digits and '-', at most 12 characters."

    # The same derivation as scripts/vcluster-run-setup.sh and
    # scripts/vcluster-run-teardown.sh; keep the three in step.
    OUTPUT_DIR="${OUTPUT_DIR:-${REPO_ROOT}/.vcluster-runs/${RUN_NAME}}"
    BMH_NAMESPACE="${RUN_NAME}-hosts"
    VM_NAME_PREFIX="bmh-${RUN_NAME}"
    POOL_NAME="bmh-${RUN_NAME}"
    SUSHY_CONFIG_DIR="${HOME}/sushy-${RUN_NAME}"
    SUSHY_CACHE_NAME="sushy-cache-${RUN_NAME}"
    SUSHY_VMEDIA_TMP="${HOME}/sushy-vmedia-tmp-${RUN_NAME}"
    INVENTORY_FILE="${OUTPUT_DIR}/virtual-bmh-inventory.json"

    VM_DISK_ROOT="${VM_DISK_DIR:-/tmp}"
    [[ "${VM_DISK_ROOT}" == /* ]] \
        || die "VM_DISK_DIR must be an absolute path."
    VM_DISK_DIR="${VM_DISK_ROOT%/}/virtual-bmh-disks-${RUN_NAME}"

    if [[ -n "${PORT_OFFSET}" ]]; then
        [[ "${PORT_OFFSET}" =~ ^[0-9]+$ ]] && [[ "${PORT_OFFSET}" -lt "${BMH_PORT_SPAN}" ]] \
            || die "--port-offset must be an integer from 0 to $(( BMH_PORT_SPAN - 1 ))."
    else
        local checksum
        checksum="$(printf '%s' "${RUN_NAME}" | cksum | awk '{print $1}')"
        PORT_OFFSET=$(( checksum % BMH_PORT_SPAN ))
    fi
    SUSHY_PORT=$(( BMH_SUSHY_PORT_BASE + PORT_OFFSET * 2 ))
    SUSHY_CACHE_PORT=$(( SUSHY_PORT + 1 ))

    if [[ "${DELETE}" == "true" ]]; then
        delete_run_bmhs
        return
    fi
    create_run_bmhs
}

main "$@"
