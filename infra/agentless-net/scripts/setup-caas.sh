#!/usr/bin/env bash
#
# Register host type, create InfraEnv, boot host VMs, wait for agents
# to register, approve and label them.
# Corresponds to setup-lab.sh steps 19-22.
# Idempotent — safe to re-run.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="${SCRIPT_DIR}/.."

# shellcheck source=/dev/null
source "${INFRA_DIR}/.mgmt-network"

export KUBECONFIG

OSAC_NAMESPACE="${OSAC_NAMESPACE:-osac-e2e-ci}"
PULL_SECRET="${OSAC_PULL_SECRET_PATH:-/root/pull-secret}"

LAB_NAME="agentless-net-lab"
AGENT_RESOURCE_CLASS="ci-worker"
AGENT_NAMESPACE="hardware-inventory"
INFRAENV_NAME="infraenv"
SSH_PUB_KEY="$(cat ~/.ssh/id_rsa.pub 2>/dev/null || cat ~/.ssh/id_ed25519.pub 2>/dev/null || true)"

# Host VMs — 2 workers
HOST_VMS=("host-1" "host-2")
HOST_BRIDGES=("br-host1" "br-host2")
HOST_VETHS=("leaf1-swp2" "leaf2-swp2")

VM_DIR="/var/lib/libvirt/images/${LAB_NAME}"
VM_VCPUS=4
VM_MEMORY=16384
VM_DISK_SIZE=100

info() { echo "==> $*"; }

# ---------- step 19: register host type ----------

INTERNAL_API="https://$(KUBECONFIG="$KUBECONFIG" oc get route fulfillment-internal-api -n "$OSAC_NAMESPACE" -o jsonpath='{.status.ingress[0].host}')"
TOKEN=$(KUBECONFIG="$KUBECONFIG" oc create token -n "$OSAC_NAMESPACE" admin)

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
    echo "ERROR: Failed to create host type (HTTP ${HTTP_CODE})"
    cat "${RESPONSE_BODY}"
    rm -f "${RESPONSE_BODY}"
    exit 1
fi
rm -f "${RESPONSE_BODY}"

# ---------- step 20: create agent namespace and InfraEnv ----------

info "Creating agent namespace '${AGENT_NAMESPACE}'..."
KUBECONFIG="$KUBECONFIG" oc create namespace "$AGENT_NAMESPACE" --dry-run=client -o yaml | \
    KUBECONFIG="$KUBECONFIG" oc apply -f -

info "Creating pull-secret in ${AGENT_NAMESPACE}..."
KUBECONFIG="$KUBECONFIG" oc create secret generic pull-secret \
    -n "$AGENT_NAMESPACE" \
    --from-file=.dockerconfigjson="$PULL_SECRET" \
    --type=kubernetes.io/dockerconfigjson \
    --dry-run=client -o yaml | \
    KUBECONFIG="$KUBECONFIG" oc apply -f -

info "Creating CAPI provider role in ${AGENT_NAMESPACE}..."
KUBECONFIG="$KUBECONFIG" oc apply -f - <<EOF
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
KUBECONFIG="$KUBECONFIG" oc apply -f - <<EOF
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
    ISO_URL=$(KUBECONFIG="$KUBECONFIG" oc get infraenv "$INFRAENV_NAME" -n "$AGENT_NAMESPACE" \
        -o jsonpath='{.status.isoDownloadURL}' 2>/dev/null) || true
    [ -n "$ISO_URL" ] && break
    sleep 5; elapsed=$((elapsed + 5))
    if [ "$elapsed" -ge 300 ]; then
        echo "ERROR: Timed out waiting for ISO URL after ${elapsed}s"
        exit 1
    fi
done
info "Discovery ISO URL ready"

# ---------- step 21: create bridges and boot host VMs ----------

mkdir -p "$VM_DIR"

ISO_FILE="${VM_DIR}/discovery.iso"
info "Downloading discovery ISO..."
curl -k -L --fail-with-body -o "$ISO_FILE" "$ISO_URL"

info "Creating bridges and booting host VMs..."
for i in "${!HOST_VMS[@]}"; do
    vm="${HOST_VMS[$i]}"
    br="${HOST_BRIDGES[$i]}"
    veth="${HOST_VETHS[$i]}"

    if virsh domstate "$vm" 2>/dev/null | grep -q "running"; then
        echo "  $vm already running — skipping"
        continue
    fi

    if ! ip link show "$br" &>/dev/null; then
        sudo ip link add "$br" type bridge
        sudo ip link set "$veth" master "$br"
        sudo ip link set "$br" up
    fi

    if ! virsh dominfo "$vm" &>/dev/null; then
        disk="$VM_DIR/${vm}.qcow2"
        qemu-img create -f qcow2 "$disk" "${VM_DISK_SIZE}G" >/dev/null 2>&1

        virt-install \
            --name "$vm" \
            --vcpus "$VM_VCPUS" \
            --memory "$VM_MEMORY" \
            --disk "$disk,format=qcow2" \
            --network "bridge=$br" \
            --network "network=$MGMT_LIBVIRT_NET,model=e1000" \
            --cdrom "$ISO_FILE" \
            --osinfo detect=on,name=generic \
            --boot hd,cdrom \
            --noautoconsole >/dev/null 2>&1

        echo "  Created and started $vm: ${VM_VCPUS} vCPU, ${VM_MEMORY}MB RAM, ${VM_DISK_SIZE}GB disk"
    else
        virsh start "$vm" 2>/dev/null || true
        echo "  Started $vm"
    fi
done

# ---------- step 22: wait for agents, approve, and label ----------

EXPECTED_AGENTS=${#HOST_VMS[@]}

info "Waiting for ${EXPECTED_AGENTS} agents to register (this may take 5-10 minutes)..."
elapsed=0
while true; do
    count=$(KUBECONFIG="$KUBECONFIG" oc get agent -n "$AGENT_NAMESPACE" --no-headers 2>/dev/null | wc -l)
    [ "$count" -ge "$EXPECTED_AGENTS" ] && break
    sleep 30; elapsed=$((elapsed + 30))
    echo "  ${elapsed}s elapsed — ${count}/${EXPECTED_AGENTS} agents registered"
    if [ "$elapsed" -ge 900 ]; then
        echo "ERROR: Timed out waiting for agents after ${elapsed}s (${count}/${EXPECTED_AGENTS} registered)"
        exit 1
    fi
done
info "All ${EXPECTED_AGENTS} agents registered"

info "Approving, labeling, and annotating agents..."
AGENT_MAC_MAP=$(KUBECONFIG="$KUBECONFIG" oc get agent -n "$AGENT_NAMESPACE" -o json | python3 -c "
import json, sys
data = json.load(sys.stdin)
for agent in data['items']:
    name = agent['metadata']['name']
    inv = json.loads(agent['metadata']['annotations']['agent.agent-install.openshift.io/inventory'])
    mac = inv['interfaces'][0]['mac_address']
    print(f'{name} {mac}')
")

ANNOTATED_COUNT=0
for agent in $(KUBECONFIG="$KUBECONFIG" oc get agent -n "$AGENT_NAMESPACE" -o jsonpath='{.items[*].metadata.name}'); do
    KUBECONFIG="$KUBECONFIG" oc patch agent/"$agent" -n "$AGENT_NAMESPACE" --type=merge \
        -p '{"spec":{"approved":true}}'
    KUBECONFIG="$KUBECONFIG" oc label agent/"$agent" -n "$AGENT_NAMESPACE" \
        "osac.openshift.io/resource_class=${AGENT_RESOURCE_CLASS}" --overwrite

    AGENT_MAC=$(echo "$AGENT_MAC_MAP" | awk -v a="$agent" '$1==a{print $2}')
    for vm in "${HOST_VMS[@]}"; do
        VM_MAC=$(virsh domiflist "$vm" | awk 'NR==3{print $5}')
        if [ "$AGENT_MAC" = "$VM_MAC" ]; then
            KUBECONFIG="$KUBECONFIG" oc annotate agent/"$agent" -n "$AGENT_NAMESPACE" \
                "osac.openshift.io/host_uuid=$vm" --overwrite
            echo "  Approved, labeled, and annotated $agent → $vm"
            ANNOTATED_COUNT=$((ANNOTATED_COUNT + 1))
            break
        fi
    done
done

if [ "$ANNOTATED_COUNT" -ne "$EXPECTED_AGENTS" ]; then
    echo "ERROR: Only ${ANNOTATED_COUNT}/${EXPECTED_AGENTS} agents got host_uuid annotation (MAC matching failed)"
    exit 1
fi

info "setup-caas complete."
