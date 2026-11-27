#!/usr/bin/env bash
#
# Register the virtual BMHs and point AgentlessNet at their fabric NICs.
#
# Each host keeps its provisioning NIC on the cluster-tool libvirt network,
# which is what sushy and Ironic use, and gets a second management NIC on the
# secondary libvirt network. The management NIC is what a host-side test SSHes
# to, the way the Netris lab uses bmaas_ssh_hosts; the fabric NIC (eth9) is
# tenant-only. Its MAC is recorded on the BMH because AgentlessNet learns
# fabric MACs from osac.openshift.io/interface-macs, not from the hardware NIC
# list.
#
# BMH_COUNT hosts are registered, one per fabric access port, so a
# Netris-style suite can place BMIs on several hosts and check same-VN
# connectivity and cross-VN isolation. The number of hosts must not exceed the
# number of access ports the lab exposes.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${INFRA_DIR}/../.." && pwd)"
# shellcheck source=use-kubeconfig.sh
source "${SCRIPT_DIR}/use-kubeconfig.sh"
use_kubeconfig "${INFRA_DIR}/.mgmt-network"

OSAC_NAMESPACE="${OSAC_NAMESPACE:-osac-e2e-ci}"
BMH_NAMESPACE="${BMH_NAMESPACE:-host-inventory}"
CLONE_NAME="${MGMT_CLONE_NAME:-agentless-lab-mgmt}"
BMH_COUNT="${BMH_COUNT:-2}"
# The libvirt network each host's management NIC attaches to. The cluster-tool
# link layer creates `test-infra-secondary-network-<clone>`; a forwarded
# override lets a lab use a different one.
MGMT_LIBVIRT_NETWORK="${MGMT_LIBVIRT_NETWORK:-test-infra-secondary-network-${CLONE_NAME}}"
SSH_KEY="${OSAC_FULFILLMENT_SSH_KEY:-${HOME}/.ssh/osac-bmaas-fulfillment}"
SSH_PUB="${SSH_KEY}.pub"
INVENTORY_FILE="${HOME}/virtual-bmh-inventory-${CLONE_NAME}.json"
info() { echo "==> $*"; }

# One access port per host, in a fixed order: host N uses entry N. Both leaves
# expose swp2, which is the only fabric access port this lab has.
FABRIC_PORTS=("leaf-1:swp2" "leaf-2:swp2")
if [[ "$BMH_COUNT" -lt 1 || "$BMH_COUNT" -gt "${#FABRIC_PORTS[@]}" ]]; then
    echo "ERROR: BMH_COUNT must be between 1 and ${#FABRIC_PORTS[@]} (access ports: ${FABRIC_PORTS[*]})" >&2
    exit 1
fi
if ! sudo virsh net-info "$MGMT_LIBVIRT_NETWORK" &>/dev/null; then
    echo "ERROR: libvirt network ${MGMT_LIBVIRT_NETWORK} not found; the management NIC needs it" >&2
    exit 1
fi

if [[ ! -f "$SSH_KEY" ]]; then
    info "Generating the fulfillment SSH key"
    ssh-keygen -t ed25519 -N "" -f "$SSH_KEY"
fi

host_count=0
if [[ -f "$INVENTORY_FILE" ]]; then
    host_count="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("hosts", [])))' "$INVENTORY_FILE")"
fi
if [[ "$host_count" -gt "$BMH_COUNT" ]]; then
    echo "ERROR: ${INVENTORY_FILE} has ${host_count} virtual BMHs, more than BMH_COUNT=${BMH_COUNT}" >&2
    exit 1
fi
if [[ "$host_count" -gt 0 ]]; then
    info "Reusing ${host_count} virtual BMH(s) already recorded in ${INVENTORY_FILE}"
    # oc apply in reconcile-bmhs.py updates a BareMetalHost that already
    # exists and creates one when registration stopped after the VM.
    python3 "${REPO_ROOT}/.github/scripts/reconcile-bmhs.py" \
        --inventory-file "${INVENTORY_FILE}" \
        --namespace "${BMH_NAMESPACE}"
fi
if [[ "$host_count" -lt "$BMH_COUNT" ]]; then
    missing_count=$((BMH_COUNT - host_count))
    info "Registering ${missing_count} more virtual BMH(s) through sushy-tools"
    CLONE_NAME="$CLONE_NAME" BMH_COUNT="$missing_count" BMH_NAMESPACE="$BMH_NAMESPACE" \
        "${REPO_ROOT}/.github/scripts/setup-virtual-bmh.sh"
fi

mapfile -t BMH_NAMES < <(python3 -c '
import json, sys
document = json.load(open(sys.argv[1]))
names = sorted(host["name"] for host in document.get("hosts", []))
if not names:
    raise SystemExit("the virtual BMH inventory is empty")
print("\n".join(names))
' "$INVENTORY_FILE")
if [[ "${#BMH_NAMES[@]}" -lt "$BMH_COUNT" ]]; then
    echo "ERROR: ${INVENTORY_FILE} lists ${#BMH_NAMES[@]} virtual BMHs, expected ${BMH_COUNT}" >&2
    exit 1
fi

grep -v '^BMH_NAME=' "${INFRA_DIR}/.mgmt-network" >"${INFRA_DIR}/.mgmt-network.tmp"
echo "BMH_NAME=${BMH_NAMES[0]}" >>"${INFRA_DIR}/.mgmt-network.tmp"
mv "${INFRA_DIR}/.mgmt-network.tmp" "${INFRA_DIR}/.mgmt-network"

# Ironic powers the domain off after inspection. --live fails on a shut-off
# domain and would abort before the inventory is published. --config persists
# either way.
domain_state="$(sudo virsh domstate "${BMH_NAMES[0]}")"
live_flags=()
if [[ "$domain_state" == "running" ]]; then
    live_flags=(--live)
fi

# Per-host state used for the fabric annotation, the inventory and the tests.
inventory_hosts=()
bmh_ssh_hosts=""
for index in "${!BMH_NAMES[@]}"; do
    [[ "$index" -lt "$BMH_COUNT" ]] || break
    host="${BMH_NAMES[$index]}"
    iface="${FABRIC_PORTS[$index]}"
    switch="${iface%%:*}"
    port="${iface##*:}"
    AST_HOST_MAC="$(HOST_INDEX="$index" python3 -c '
import os
index = int(os.environ["HOST_INDEX"])
print("52:54:00:bb:dd:%02x" % (index + 1))
')"
    info "Virtual BMH ${host} -> ${switch}/${port}, fabric MAC ${AST_HOST_MAC}"

    # Reuse the fabric NIC only when this MAC is already on the requested
    # bridge. A MAC on another bridge would make the published inventory name
    # the wrong port.
    bridge="br-host$((index + 1))"
    nic_source="$(sudo virsh domiflist "$host" | awk -v mac="$AST_HOST_MAC" 'NR>2 && $5 == mac { print $3; exit }')"
    if [[ -z "$nic_source" ]]; then
        info "Attaching ${AST_HOST_MAC} to ${bridge}"
        sudo virsh attach-interface "$host" --source "$bridge" --type bridge \
            --model virtio --mac "$AST_HOST_MAC" "${live_flags[@]}" --config
    elif [[ "$nic_source" != "$bridge" ]]; then
        info "Moving ${AST_HOST_MAC} from ${nic_source} to ${bridge}"
        sudo virsh detach-interface "$host" bridge --mac "$AST_HOST_MAC" "${live_flags[@]}" --config
        sudo virsh attach-interface "$host" --source "$bridge" --type bridge \
            --model virtio --mac "$AST_HOST_MAC" "${live_flags[@]}" --config
    else
        info "${AST_HOST_MAC} is already attached to ${bridge}"
    fi

    # Management NIC: a second interface on the cluster's secondary libvirt
    # network, which is host-reachable and DHCP-served. Only needed on a hot
    # domain; a cold one gets it on the next start. --config always persists.
    if ! sudo virsh domiflist "$host" | awk -v n="$MGMT_LIBVIRT_NETWORK" 'NR>2 && $3 == n { found=1 } END { exit !found }'; then
        info "Attaching management NIC ${MGMT_LIBVIRT_NETWORK} to ${host}"
        sudo virsh attach-interface "$host" network "$MGMT_LIBVIRT_NETWORK" --model virtio --config
    fi

    MAC_JSON="$(FABRIC_MAC="$AST_HOST_MAC" python3 -c 'import json, os; print(json.dumps({"eth9": os.environ["FABRIC_MAC"]}))')"
    oc -n "$BMH_NAMESPACE" annotate baremetalhost "$host" \
        "osac.openshift.io/interface-macs=${MAC_JSON}" --overwrite
    oc -n "$BMH_NAMESPACE" label baremetalhost "$host" \
        osac.openshift.io/host-type=agentless-ci --overwrite

    # The management NIC's DHCP lease is the host-reachable address a
    # host-side test SSHes to. Resolve it once the guest is running; a host
    # that is powered off keeps an empty value and the test waits on it.
    mgmt_mac="$(sudo virsh domiflist "$host" | awk -v n="$MGMT_LIBVIRT_NETWORK" 'NR>2 && $3 == n { print $5; exit }')"
    mgmt_ip=""
    if [[ -n "$mgmt_mac" ]]; then
        mgmt_ip="$(sudo virsh net-dhcp-leases "$MGMT_LIBVIRT_NETWORK" 2>/dev/null \
            | awk -v mac="$mgmt_mac" '$3 == mac { print $5; exit }' | cut -d/ -f1)"
    fi
    info "Management address for ${host}: ${mgmt_ip:-<no lease yet>}"
    bmh_ssh_hosts="$(HOST="$host" IP="$mgmt_ip" PREV="$bmh_ssh_hosts" python3 -c '
import json, os
data = json.loads(os.environ["PREV"]) if os.environ["PREV"] else {}
data[os.environ["HOST"]] = os.environ["IP"]
print(json.dumps(data))
')"
    inventory_hosts+=(--host "${host}=${switch}:${port}")
done

info "Publishing the AgentlessNet inventory"
INVENTORY_JSON="$(python3 "${SCRIPT_DIR}/bmaas_inventory.py" \
    --mgmt-prefix "$MGMT_PREFIX" "${inventory_hosts[@]}")"
INVENTORY_PATCH="$(INV="$INVENTORY_JSON" python3 -c 'import json, os; print(json.dumps({"data": {"AGENTLESS_NET_VN_INVENTORY": os.environ["INV"]}}))')"
oc -n "$OSAC_NAMESPACE" patch configmap network-fulfillment-ig --type merge -p "$INVENTORY_PATCH"

LAB_PREFIX="${LAB_PREFIX:-clab-agentless-net-lab}"
info "Installing the fulfillment public key on the network node and leaves"
PUB_KEY="$(cat "$SSH_PUB")"
install_authorized_key() {
    local container="$1"
    local home="$2"
    sudo docker exec -i "$container" sh -s -- "$home" "$PUB_KEY" <<'EOF'
set -eu
home="$1"
key="$2"
mkdir -p "${home}/.ssh"
touch "${home}/.ssh/authorized_keys"
grep -qxF "${key}" "${home}/.ssh/authorized_keys" || echo "${key}" >> "${home}/.ssh/authorized_keys"
chmod 700 "${home}/.ssh"
chmod 600 "${home}/.ssh/authorized_keys"
if [ "${home}" != /root ]; then
    chown -R cumulus:cumulus "${home}/.ssh"
fi
EOF
}
install_authorized_key "${LAB_PREFIX}-net-node" /root
install_authorized_key "${LAB_PREFIX}-leaf-1" /home/cumulus
install_authorized_key "${LAB_PREFIX}-leaf-2" /home/cumulus

info "Storing the fulfillment private key"
KEY_B64="$(base64 -w0 "$SSH_KEY")"
# printf is a shell builtin, so the key is not visible in another process's argv.
printf '%s\n' "{\"data\":{\"AGENTLESS_NET_SSH_PRIVATE_KEY\":\"${KEY_B64}\"}}" \
    | oc -n "$OSAC_NAMESPACE" patch secret network-fulfillment-ig --type merge --patch-file=/dev/stdin

# Tenant-only default route for BMI guests.
#
# A BMH VM has three NICs: provisioning (Ironic/sushy), fabric (eth9, the
# tenant network) and management (the host-side SSH address). All run DHCP, so
# the guest would get three default routes and pick the lowest-metric one (the
# provisioning NIC) for egress. Traffic then bypasses the tenant fabric, and
# the Netris-shaped suite's NAT/SNAT and EIP checks look at the provisioning
# network instead of the tenant ExternalIP. The demo this mirrors has guests
# that route only over the tenant NIC.
#
# Publish cloud-init network v2 with dhcp4-overrides.use-routes=false on the
# provisioning and management NICs, selected by MAC (stable per VM), and leave
# the fabric NIC as the only default route. The suite passes this as the BMI
# user data (OSAC_BMI_USER_DATA / bmi_user_data). Agentless-BMaaS only: Netris
# and CaaS build their guests elsewhere and are unaffected.
NETWORK_DATA_FILE="${INFRA_DIR}/bmi-user-data.yaml"
{
    cat <<'EOF'
#cloud-config
network:
  version: 2
  ethernets:
EOF
    for index in "${!BMH_NAMES[@]}"; do
        [[ "$index" -lt "$BMH_COUNT" ]] || break
        host="${BMH_NAMES[$index]}"
        # provisioning NIC and management NIC MACs (fabric NIC keeps the default)
        prov_mac="$(sudo virsh domiflist "$host" | awk -v n="test-infra-net-${CLONE_NAME}" 'NR>2 && $3 == n { print $5; exit }')"
        mgmt_mac="$(sudo virsh domiflist "$host" | awk -v n="$MGMT_LIBVIRT_NETWORK" 'NR>2 && $3 == n { print $5; exit }')"
        for mac in "$prov_mac" "$mgmt_mac"; do
            [[ -n "$mac" ]] || continue
            cat <<EOF
    mac-${mac//:/}:
      match: {macaddress: "${mac}"}
      dhcp4: true
      dhcp4-overrides: {use-routes: false}
EOF
        done
        fabric_mac="$(printf '52:54:00:bb:dd:%02x' $((index + 1)))"
        cat <<EOF
    mac-${fabric_mac//:/}:
      match: {macaddress: "${fabric_mac}"}
      dhcp4: true
EOF
    done
} >"$NETWORK_DATA_FILE"
info "Wrote tenant-only default route to ${NETWORK_DATA_FILE}"

info "Publishing BMaaS test inputs"
env_file="${INFRA_DIR}/.env.infra"
touch "$env_file"
env_tmp="$(mktemp)"
grep_status=0
grep -v -E '^(OSAC_BMI_NETWORK_INTERFACE|OSAC_BMI_INSTANCE_TYPE|OSAC_BMH_SSH_HOSTS|OSAC_BMI_USER_DATA_FILE)=' "$env_file" >"$env_tmp" || grep_status=$?
if [[ "$grep_status" -gt 1 ]]; then
    echo "ERROR: failed to update ${env_file}" >&2
    rm -f "$env_tmp"
    exit 1
fi
{
    echo "OSAC_BMI_NETWORK_INTERFACE=eth9"
    echo "OSAC_BMI_INSTANCE_TYPE=bmaas-agentless-ci"
    printf 'OSAC_BMH_SSH_HOSTS=%q\n' "$bmh_ssh_hosts"
    printf 'OSAC_BMI_USER_DATA_FILE=%q\n' "$NETWORK_DATA_FILE"
} >>"$env_tmp"
mv "$env_tmp" "$env_file"

info "setup-bmaas complete for ${BMH_COUNT} host(s): ${BMH_NAMES[*]}"
