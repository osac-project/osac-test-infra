#!/usr/bin/env bash
#
# Boot CaaS agent VMs on the cluster-tool network and register them
# with the Assisted Service running on the hub cluster.
#
# This is the cluster-tool counterpart of
# infra/agentless-net/scripts/setup-caas.sh — same agent registration
# flow (register host type → create InfraEnv → boot VMs from discovery
# ISO → wait for agents → label/approve), adapted to run on the
# cluster-tool libvirt network instead of containerlab bridges.
#
# The VM creation pattern follows .github/scripts/setup-virtual-bmh.sh,
# which already proves this approach works on cluster-tool networks.
#
# Agent flow modes (CAAS_AGENT_FLOW, default: hot-pool):
#   hot-pool          — legacy: agents in hardware-inventory, this script
#                       labels AND approves them.
#   cluster-namespace — Option C: agents in the ClusterOrder namespace
#                       (CAAS_CLUSTER_ORDER_NAMESPACE). This script labels
#                       but does NOT approve — osac ci_create owns approval.
#
# Required env:
#   CLONE_NAME   — cluster-tool clone name (used to find the libvirt network)
#   KUBECONFIG   — path to the cluster kubeconfig
#
# Optional env:
#   CAAS_AGENT_FLOW              — "hot-pool" (default) or "cluster-namespace"
#   CAAS_CLUSTER_ORDER_NAMESPACE — required when CAAS_AGENT_FLOW=cluster-namespace
#   OSAC_NAMESPACE               — OSAC deployment namespace (default: osac-e2e-ci)
#   OSAC_PULL_SECRET_PATH        — path to pull-secret JSON (default: $RUNNER_TEMP/pull-secret.json)
#   AGENT_HOST_COUNT             — number of agent VMs to create (default: 2)
#   AGENT_RESOURCE_CLASS         — resource class label (default: ci-worker)
#
# Teardown: teardown.sh cleans up VMs by the "caas-agent-${CLONE_NAME}-"
# prefix — see the "Clean up CaaS agent VMs" section there.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=.github/scripts/setup-caas-agent-flow-lib.sh
source "${SCRIPT_DIR}/setup-caas-agent-flow-lib.sh"

: "${CLONE_NAME:?CLONE_NAME is required}"
: "${KUBECONFIG:?KUBECONFIG is required}"

CAAS_AGENT_FLOW="${CAAS_AGENT_FLOW:-hot-pool}"
OSAC_NAMESPACE="${OSAC_NAMESPACE:-osac-e2e-ci}"
PULL_SECRET="${OSAC_PULL_SECRET_PATH:-${RUNNER_TEMP:-/tmp}/pull-secret.json}"
AGENT_HOST_COUNT="${AGENT_HOST_COUNT:-2}"
AGENT_RESOURCE_CLASS="${AGENT_RESOURCE_CLASS:-ci-worker}"
INFRAENV_NAME="infraenv"

VIRSH="virsh -c qemu:///system"
CT_NETWORK="test-infra-net-${CLONE_NAME}"
VM_PREFIX="caas-agent-${CLONE_NAME}"
VM_DISK_DIR="/tmp/caas-agent-disks-${CLONE_NAME}"
VM_VCPUS=4
VM_MEMORY=16384
VM_DISK_SIZE=120  # GB — Assisted needs ≥100 GB

if ! validate_agent_flow; then
    echo "ERROR: invalid CAAS_AGENT_FLOW '${CAAS_AGENT_FLOW}' (expected hot-pool or cluster-namespace)" >&2
    exit 1
fi

SSH_PUB_KEY="$(cat ~/.ssh/id_rsa.pub 2>/dev/null || cat ~/.ssh/id_ed25519.pub 2>/dev/null || true)"

# Resolve target namespace based on agent flow mode.
# In cluster-namespace mode without CAAS_CLUSTER_ORDER_NAMESPACE, only
# register the host type (pre-order phase) — the full agent setup runs
# later after ClusterOrder creation provides the namespace.
if [[ "${CAAS_AGENT_FLOW}" == "cluster-namespace" ]]; then
    info() { echo "==> [cluster-namespace] $*"; }
    if [[ -z "${CAAS_CLUSTER_ORDER_NAMESPACE:-}" ]]; then
        AGENT_NAMESPACE=""
    else
        AGENT_NAMESPACE="${CAAS_CLUSTER_ORDER_NAMESPACE}"
    fi
else
    AGENT_NAMESPACE="hardware-inventory"
    info() { echo "==> $*"; }
fi

# ---------- step 1: register host type ----------

INTERNAL_API="https://$(oc get route fulfillment-internal-api -n "${OSAC_NAMESPACE}" -o jsonpath='{.status.ingress[0].host}')"
TOKEN=$(oc create token -n "${OSAC_NAMESPACE}" admin)

RESPONSE_BODY=$(mktemp)
HTTP_CODE=$(curl -sk -w "%{http_code}" -X POST "${INTERNAL_API}/api/private/v1/host_types" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"id\": \"${AGENT_RESOURCE_CLASS}\", \"title\": \"CI Worker\", \"description\": \"Worker nodes for CI testing\"}" \
    -o "${RESPONSE_BODY}") || true
if [[ "${HTTP_CODE}" == "200" || "${HTTP_CODE}" == "201" ]]; then
    info "Host type '${AGENT_RESOURCE_CLASS}' created"
elif [[ "${HTTP_CODE}" == "409" ]]; then
    info "Host type '${AGENT_RESOURCE_CLASS}' already exists — skipping"
else
    echo "ERROR: Failed to create host type (HTTP ${HTTP_CODE})" >&2
    cat "${RESPONSE_BODY}" >&2
    rm -f "${RESPONSE_BODY}"
    exit 1
fi
rm -f "${RESPONSE_BODY}"

# In cluster-namespace mode without a namespace, host-type registration
# is all we can do before the ClusterOrder exists.  Exit cleanly so the
# workflow can create the order, discover the namespace, then re-run
# this script with CAAS_CLUSTER_ORDER_NAMESPACE set.
if [[ "${CAAS_AGENT_FLOW}" == "cluster-namespace" ]] && [[ -z "${AGENT_NAMESPACE}" ]]; then
    info "Pre-order phase complete. Re-run with CAAS_CLUSTER_ORDER_NAMESPACE after ClusterOrder creation."
    exit 0
fi

# ---------- step 2: create agent namespace and InfraEnv ----------

info "Creating agent namespace '${AGENT_NAMESPACE}'..."
oc create namespace "${AGENT_NAMESPACE}" --dry-run=client -o yaml | oc apply -f -

info "Creating pull-secret in ${AGENT_NAMESPACE}..."
oc create secret generic pull-secret \
    -n "${AGENT_NAMESPACE}" \
    --from-file=.dockerconfigjson="${PULL_SECRET}" \
    --type=kubernetes.io/dockerconfigjson \
    --dry-run=client -o yaml | oc apply -f -

info "Creating CAPI provider role in ${AGENT_NAMESPACE}..."
oc apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: capi-provider-role
  namespace: ${AGENT_NAMESPACE}
rules:
- apiGroups: ["agent-install.openshift.io"]
  resources: ["agents"]
  verbs: ["*"]
EOF

info "Creating InfraEnv '${INFRAENV_NAME}' in ${AGENT_NAMESPACE}..."
oc apply -f - <<EOF
apiVersion: agent-install.openshift.io/v1beta1
kind: InfraEnv
metadata:
  name: ${INFRAENV_NAME}
  namespace: ${AGENT_NAMESPACE}
spec:
  pullSecretRef:
    name: pull-secret
  sshAuthorizedKey: "${SSH_PUB_KEY}"
EOF

info "Waiting for discovery ISO URL..."
elapsed=0
while true; do
    ISO_URL=$(oc get infraenv "${INFRAENV_NAME}" -n "${AGENT_NAMESPACE}" \
        -o jsonpath='{.status.isoDownloadURL}' 2>/dev/null) || true
    [[ -n "${ISO_URL}" ]] && break
    sleep 5; elapsed=$((elapsed + 5))
    if [[ "${elapsed}" -ge 300 ]]; then
        echo "ERROR: Timed out waiting for ISO URL after ${elapsed}s" >&2
        exit 1
    fi
done
# Log readiness without exposing the signed URL.
info "Discovery ISO URL ready"

# ---------- step 3: discover cluster-tool network and boot VMs ----------

info "Discovering cluster-tool network '${CT_NETWORK}'..."
if ! ${VIRSH} net-info "${CT_NETWORK}" &>/dev/null; then
    echo "ERROR: libvirt network '${CT_NETWORK}' not found." >&2
    echo "Available networks:" >&2
    ${VIRSH} net-list --all >&2
    exit 1
fi

mkdir -p "${VM_DISK_DIR}"

ISO_FILE="${VM_DISK_DIR}/discovery.iso"
info "Downloading discovery ISO..."
curl -k -L --fail-with-body -o "${ISO_FILE}" "${ISO_URL}"

OVMF_CODE="/usr/share/OVMF/OVMF_CODE.secboot.fd"
OVMF_VARS="/usr/share/OVMF/OVMF_VARS.fd"

info "Creating ${AGENT_HOST_COUNT} agent VMs on network ${CT_NETWORK}..."
VM_NAMES=()
for i in $(seq 1 "${AGENT_HOST_COUNT}"); do
    VM_NAME="${VM_PREFIX}-${i}"
    MAC="52:54:00:ca:a5:$(printf '%02x' "${i}")"
    DISK_PATH="${VM_DISK_DIR}/${VM_NAME}.qcow2"
    VARS_PATH="${VM_DISK_DIR}/${VM_NAME}-VARS.fd"

    if ${VIRSH} domstate "${VM_NAME}" 2>/dev/null | grep -q "running"; then
        echo "  VM ${VM_NAME} already running — skipping"
        VM_NAMES+=("${VM_NAME}")
        continue
    fi

    echo "  Creating VM: ${VM_NAME} (MAC: ${MAC})..."
    qemu-img create -f qcow2 "${DISK_PATH}" "${VM_DISK_SIZE}G"
    cp "${OVMF_VARS}" "${VARS_PATH}"

    ${VIRSH} define /dev/stdin <<VMXML
<domain type='kvm'>
  <name>${VM_NAME}</name>
  <memory unit='MiB'>${VM_MEMORY}</memory>
  <vcpu>${VM_VCPUS}</vcpu>
  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
    <loader readonly='yes' type='pflash'>${OVMF_CODE}</loader>
    <nvram>${VARS_PATH}</nvram>
    <boot dev='cdrom'/>
    <boot dev='hd'/>
  </os>
  <cpu mode='host-passthrough' check='none' migratable='on'/>
  <features>
    <acpi/>
    <apic/>
  </features>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='${DISK_PATH}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='${ISO_FILE}'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
    </disk>
    <interface type='network'>
      <mac address='${MAC}'/>
      <source network='${CT_NETWORK}'/>
      <model type='virtio'/>
    </interface>
    <console type='pty'/>
  </devices>
  <seclabel type='none'/>
</domain>
VMXML

    ${VIRSH} start "${VM_NAME}"
    VM_NAMES+=("${VM_NAME}")
    echo "  Started VM ${VM_NAME}: ${VM_VCPUS} vCPU, ${VM_MEMORY}MB RAM, ${VM_DISK_SIZE}GB disk"
done

info "VMs created: ${VM_NAMES[*]}"

# ---------- step 4: wait for agents, label, and conditionally approve ----------

EXPECTED_AGENTS=${AGENT_HOST_COUNT}

info "Waiting for ${EXPECTED_AGENTS} agents to register (this may take 5-10 minutes)..."
elapsed=0
while true; do
    count=$(oc get agent -n "${AGENT_NAMESPACE}" --no-headers 2>/dev/null | wc -l)
    [[ "${count}" -ge "${EXPECTED_AGENTS}" ]] && break
    sleep 30; elapsed=$((elapsed + 30))
    echo "  ${elapsed}s elapsed — ${count}/${EXPECTED_AGENTS} agents registered"
    if [[ "${elapsed}" -ge 900 ]]; then
        echo "ERROR: Timed out waiting for agents after ${elapsed}s (${count}/${EXPECTED_AGENTS} registered)" >&2
        exit 1
    fi
done
info "All ${EXPECTED_AGENTS} agents registered"

if should_approve_agents; then
    info "Approving, labeling, and annotating agents..."
else
    info "Labeling and annotating agents (no approval — osac owns approval)..."
fi

AGENT_MAC_MAP=$(oc get agent -n "${AGENT_NAMESPACE}" -o json | python3 -c "
import json, sys
data = json.load(sys.stdin)
for agent in data['items']:
    name = agent['metadata']['name']
    inv = json.loads(agent['metadata']['annotations']['agent.agent-install.openshift.io/inventory'])
    mac = inv['interfaces'][0]['mac_address']
    print(f'{name} {mac}')
")

ANNOTATED_COUNT=0
for agent in $(oc get agent -n "${AGENT_NAMESPACE}" -o jsonpath='{.items[*].metadata.name}'); do
    if should_approve_agents; then
        oc patch agent/"${agent}" -n "${AGENT_NAMESPACE}" --type=merge \
            -p '{"spec":{"approved":true}}'
    fi
    oc label agent/"${agent}" -n "${AGENT_NAMESPACE}" \
        "osac.openshift.io/resource_class=${AGENT_RESOURCE_CLASS}" --overwrite

    AGENT_MAC=$(echo "${AGENT_MAC_MAP}" | awk -v a="${agent}" '$1==a{print $2}')
    for vm_name in "${VM_NAMES[@]}"; do
        VM_MAC=$(${VIRSH} domiflist "${vm_name}" | awk 'NR==3{print $5}')
        if [[ "${AGENT_MAC}" == "${VM_MAC}" ]]; then
            oc annotate agent/"${agent}" -n "${AGENT_NAMESPACE}" \
                "osac.openshift.io/host_uuid=${vm_name}" --overwrite
            if should_approve_agents; then
                echo "  Approved, labeled, and annotated agent $(( ANNOTATED_COUNT + 1 ))"
            else
                echo "  Labeled and annotated agent $(( ANNOTATED_COUNT + 1 )) (no approval)"
            fi
            ANNOTATED_COUNT=$((ANNOTATED_COUNT + 1))
            break
        fi
    done
done

if [[ "${ANNOTATED_COUNT}" -ne "${EXPECTED_AGENTS}" ]]; then
    echo "ERROR: Only ${ANNOTATED_COUNT}/${EXPECTED_AGENTS} agents got host_uuid annotation (MAC matching failed)" >&2
    exit 1
fi

info "setup-caas-agents complete (agent_flow=${CAAS_AGENT_FLOW})."
