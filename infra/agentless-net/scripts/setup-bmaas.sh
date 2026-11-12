#!/usr/bin/env bash
#
# Register one virtual BMH and point AgentlessNet at its fabric NIC.
#
# The provisioning NIC stays on the management libvirt network, which is
# what sushy and Ironic use. eth9 is a second virtio NIC on br-host1.
# Its MAC is recorded on the BMH because AgentlessNet learns fabric MACs
# from osac.openshift.io/interface-macs, not from the hardware NIC list.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${INFRA_DIR}/../.." && pwd)"
# shellcheck source=/dev/null
source "${INFRA_DIR}/.mgmt-network"

OSAC_NAMESPACE="${OSAC_NAMESPACE:-osac-e2e-ci}"
BMH_NAMESPACE="${BMH_NAMESPACE:-host-inventory}"
CLONE_NAME="${MGMT_CLONE_NAME:-agentless-lab-mgmt}"
BMH_COUNT=1
FABRIC_MAC="${FABRIC_MAC:-52:54:00:bb:dd:01}"
FABRIC_BRIDGE="${FABRIC_BRIDGE:-br-host1}"
SSH_KEY="${HOME}/.ssh/id_rsa"
SSH_PUB="${SSH_KEY}.pub"
INVENTORY_FILE="${HOME}/virtual-bmh-inventory-${CLONE_NAME}.json"
export KUBECONFIG

info() { echo "==> $*"; }

if [[ ! -f "$SSH_KEY" ]]; then
    info "Generating the fulfillment SSH key"
    ssh-keygen -t ed25519 -N "" -f "$SSH_KEY"
fi

host_count=0
if [[ -f "$INVENTORY_FILE" ]]; then
    host_count="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("hosts", [])))' "$INVENTORY_FILE")"
fi
if [[ "$host_count" -gt 1 ]]; then
    echo "ERROR: ${INVENTORY_FILE} has ${host_count} virtual BMHs; refusing to create another" >&2
    exit 1
fi
if [[ "$host_count" -eq 1 ]]; then
    info "Reusing the virtual BMH already recorded in ${INVENTORY_FILE}"
    # oc apply in reconcile-bmhs.py updates a BareMetalHost that already
    # exists and creates one when registration stopped after the VM.
    python3 "${REPO_ROOT}/.github/scripts/reconcile-bmhs.py" \
        --inventory-file "${INVENTORY_FILE}" \
        --namespace "${BMH_NAMESPACE}"
else
    info "Registering ${BMH_COUNT} virtual BMH through sushy-tools"
    CLONE_NAME="$CLONE_NAME" BMH_COUNT="$BMH_COUNT" BMH_NAMESPACE="$BMH_NAMESPACE" \
        "${REPO_ROOT}/.github/scripts/setup-virtual-bmh.sh"
fi

mapfile -t BMH_NAMES < <(python3 -c '
import json, sys
document = json.load(open(sys.argv[1]))
names = [host["name"] for host in document.get("hosts", [])]
if len(names) != 1:
    raise SystemExit("expected exactly one virtual BMH, found %s" % len(names))
print(names[0])
' "$INVENTORY_FILE")
BMH_NAME="${BMH_NAMES[0]}"
grep -v '^BMH_NAME=' "${INFRA_DIR}/.mgmt-network" >"${INFRA_DIR}/.mgmt-network.tmp"
echo "BMH_NAME=${BMH_NAME}" >>"${INFRA_DIR}/.mgmt-network.tmp"
mv "${INFRA_DIR}/.mgmt-network.tmp" "${INFRA_DIR}/.mgmt-network"
info "Virtual BMH ${BMH_NAME}"

if ! sudo virsh domiflist "$BMH_NAME" | awk 'NR>2 {print $5}' | grep -qx "$FABRIC_MAC"; then
    info "Attaching ${FABRIC_MAC} to ${FABRIC_BRIDGE}"
    sudo virsh attach-interface "$BMH_NAME" --source "$FABRIC_BRIDGE" --type bridge \
        --model virtio --mac "$FABRIC_MAC" --live --config
fi

info "Recording the fabric MAC and host type on ${BMH_NAME}"
MAC_JSON="$(FABRIC_MAC="$FABRIC_MAC" python3 -c 'import json, os; print(json.dumps({"eth9": os.environ["FABRIC_MAC"]}))')"
oc -n "$BMH_NAMESPACE" annotate baremetalhost "$BMH_NAME" \
    "osac.openshift.io/interface-macs=${MAC_JSON}" --overwrite
oc -n "$BMH_NAMESPACE" label baremetalhost "$BMH_NAME" \
    osac.openshift.io/host-type=agentless-ci --overwrite

info "Publishing the AgentlessNet inventory for ${BMH_NAME}"
INVENTORY_JSON="$(python3 "${SCRIPT_DIR}/bmaas_inventory.py" \
    --mgmt-prefix "$MGMT_PREFIX" --host-name "$BMH_NAME")"
INVENTORY_PATCH="$(INV="$INVENTORY_JSON" python3 -c 'import json, os; print(json.dumps({"data": {"AGENTLESS_NET_VN_INVENTORY": os.environ["INV"]}}))')"
oc -n "$OSAC_NAMESPACE" patch configmap network-fulfillment-ig --type merge -p "$INVENTORY_PATCH"

LAB_PREFIX="${LAB_PREFIX:-clab-agentless-net-lab}"
info "Installing the fulfillment public key on the network node and leaves"
PUB_KEY="$(cat "$SSH_PUB")"
install_authorized_key() {
    local container="$1"
    local home="$2"
    sudo docker exec -i "$container" sh -s -- "$home" <<EOF
set -eu
home="\$1"
mkdir -p "\${home}/.ssh"
touch "\${home}/.ssh/authorized_keys"
grep -qxF '${PUB_KEY}' "\${home}/.ssh/authorized_keys" || echo '${PUB_KEY}' >> "\${home}/.ssh/authorized_keys"
chmod 700 "\${home}/.ssh"
chmod 600 "\${home}/.ssh/authorized_keys"
if [ "\${home}" != /root ]; then
    chown -R cumulus:cumulus "\${home}/.ssh"
fi
EOF
}
install_authorized_key "${LAB_PREFIX}-net-node" /root
install_authorized_key "${LAB_PREFIX}-leaf-1" /home/cumulus
install_authorized_key "${LAB_PREFIX}-leaf-2" /home/cumulus

info "Installing dnsmasq and the DHCP supervisor on the network node"
sudo docker exec "${LAB_PREFIX}-net-node" apk add --no-cache dnsmasq supervisor
sudo docker exec "${LAB_PREFIX}-net-node" sh -ec '
mkdir -p /etc/agentless-net/supervisor.d /var/log/agentless-net
printf "%s\n" "; placeholder so the include glob matches" > /etc/agentless-net/supervisor.d/placeholder.ini
cat > /etc/agentless-net/supervisord.conf <<EOF
[unix_http_server]
file=/run/agentless-net-supervisor.sock

[supervisord]
logfile=/var/log/agentless-net/supervisord.log
pidfile=/run/agentless-net-supervisord.pid
childlogdir=/var/log/agentless-net

[rpcinterface:supervisor]
supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface

[supervisorctl]
serverurl=unix:///run/agentless-net-supervisor.sock

[include]
files = /etc/agentless-net/supervisor.d/*.ini
EOF
'
# A retry finds the daemon already running. The pid check is the condition
# preflight_dhcp_supervisor accepts.
sudo docker exec -d "${LAB_PREFIX}-net-node" supervisord -c /etc/agentless-net/supervisord.conf || true
supervisor_ready=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if sudo docker exec "${LAB_PREFIX}-net-node" supervisorctl -c /etc/agentless-net/supervisord.conf pid | grep -Eq '^[0-9]+$'; then
        supervisor_ready=1
        break
    fi
    sleep 1
done
if [[ "$supervisor_ready" -ne 1 ]]; then
    echo "ERROR: AgentlessNet supervisor is not reachable on ${LAB_PREFIX}-net-node" >&2
    exit 1
fi

info "Storing the fulfillment private key"
KEY_B64="$(base64 -w0 "$SSH_KEY")"
oc -n "$OSAC_NAMESPACE" patch secret network-fulfillment-ig --type merge -p \
    "{\"data\":{\"AGENTLESS_NET_SSH_PRIVATE_KEY\":\"${KEY_B64}\"}}"

info "setup-bmaas complete for ${BMH_NAME}"
