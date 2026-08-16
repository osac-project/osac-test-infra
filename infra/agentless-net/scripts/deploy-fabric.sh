#!/usr/bin/env bash
#
# Deploy containerlab topology, configure switches, attach mgmt VM
# to the fabric, configure BGP peering.
# Corresponds to setup-lab.sh steps 11-18.
# Idempotent — safe to re-run.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="${SCRIPT_DIR}/.."

# shellcheck source=/dev/null
source "${INFRA_DIR}/.mgmt-network"

export KUBECONFIG

OSAC_NAMESPACE="${OSAC_NAMESPACE:-osac-e2e-ci}"

LAB_NAME="agentless-net-lab"
PREFIX="clab-${LAB_NAME}"
CONTAINERLAB="${CONTAINERLAB:-containerlab}"
SWITCHES=("${PREFIX}-leaf-1" "${PREFIX}-leaf-2")
NET_NODE="${PREFIX}-net-node"
UPSTREAM_ROUTER="${PREFIX}-upstream-router"

# BGP peering link (net-node:eth2 <-> upstream-router:eth1)
BGP_NET_NODE_IP="10.253.0.1/30"
BGP_UPSTREAM_IP="10.253.0.2/30"
BGP_NET_NODE_AS=65001
BGP_UPSTREAM_AS=65000

info() { echo "==> $*"; }

# ---------- deploy containerlab ----------

TOPO_FILE="${INFRA_DIR}/agentless-net-lab.clab.yml"

if docker ps --format '{{.Names}}' | grep -q "^${PREFIX}-leaf-1$"; then
    info "Containerlab already running — skipping deploy"
else
    info "Deploying containerlab topology..."
    sudo MGMT_BRIDGE="$MGMT_BRIDGE" MGMT_CIDR="$MGMT_CIDR" MGMT_GW="$MGMT_GW" MGMT_PREFIX="$MGMT_PREFIX" \
        ${CONTAINERLAB} deploy -t "$TOPO_FILE"
fi

# ---------- wait for switches ----------

wait_for_switch() {
    local sw="$1"
    local elapsed=0
    while ! docker exec "$sw" nv show system 2>/dev/null | grep -q "hostname"; do
        sleep 3; elapsed=$((elapsed + 3))
        [ "$elapsed" -ge 90 ] && break
    done
    local mgmt_ip
    mgmt_ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$sw")
    while ! sshpass -p cumulus ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=2 cumulus@"$mgmt_ip" true &>/dev/null; do
        sleep 3; elapsed=$((elapsed + 3))
        [ "$elapsed" -ge 120 ] && break
    done
}

info "Waiting for Cumulus switches to be ready..."
for sw in "${SWITCHES[@]}"; do
    wait_for_switch "$sw" &
done
wait
info "All switches ready."

# ---------- fix sudo on switches ----------

info "Fixing sudo permissions on switches..."
for sw in "${SWITCHES[@]}"; do
    docker exec "$sw" bash -c \
        "echo 'cumulus ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/cumulus && chmod 440 /etc/sudoers.d/cumulus"
done

# ---------- attach mgmt VM to switch fabric ----------

if ip link show br-mgmt &>/dev/null; then
    info "br-mgmt already exists — skipping"
else
    info "Creating br-mgmt and attaching mgmt VM to switch fabric..."
    sudo ip link add br-mgmt type bridge
    sudo ip link set leaf1-swp4 master br-mgmt
    sudo ip link set br-mgmt up
    virsh attach-interface "$MGMT_VM_NAME" bridge br-mgmt --model virtio --live --persistent
fi

FABRIC_MAC=$(virsh domiflist "$MGMT_VM_NAME" | grep br-mgmt | awk '{print $5}')
MGMT_NODE=$(KUBECONFIG="$KUBECONFIG" oc get nodes -o name | head -1 | cut -d/ -f2)
FABRIC_NIC=$(KUBECONFIG="$KUBECONFIG" oc debug "node/$MGMT_NODE" -- \
    nsenter -t 1 -n ip -o link show 2>&1 | grep "$FABRIC_MAC" | awk -F'[ :]+' '{print $2}')

info "Configuring mgmt VM fabric NIC ($FABRIC_NIC) with net-node as gateway..."
KUBECONFIG="$KUBECONFIG" oc debug "node/$MGMT_NODE" -- \
    nsenter -a -t 1 -- bash -c "
        if nmcli connection show fabric-native &>/dev/null; then
            nmcli connection modify fabric-native \
                ipv4.gateway 10.0.0.30 ipv4.route-metric 10
            nmcli connection up fabric-native 2>/dev/null
        else
            nmcli connection add type ethernet ifname $FABRIC_NIC con-name fabric-native \
                ipv4.method manual ipv4.addresses 10.0.0.10/24 \
                ipv4.gateway 10.0.0.30 ipv4.route-metric 10 \
                ipv6.method disabled
            nmcli connection up fabric-native
        fi
    "
echo "  mgmt VM $FABRIC_NIC = 10.0.0.10/24, gateway 10.0.0.30 (net-node)"

KUBECONFIG="$KUBECONFIG" oc patch l2advertisement caas-l2-advertisement -n metallb-system \
    --type=merge -p "{\"spec\":{\"interfaces\":[\"$FABRIC_NIC\"]}}"
echo "  L2Advertisement: announcing on $FABRIC_NIC (fabric)"

# ---------- configure trunk ports ----------

INVENTORY="${INFRA_DIR}/inventory/inventory.yml"
RESOLVED_INVENTORY=$(mktemp --suffix=.yml)
MGMT_PREFIX="$MGMT_PREFIX" envsubst < "$INVENTORY" > "$RESOLVED_INVENTORY"

info "Configuring trunk ports on switches..."
ansible-playbook \
    -i "$RESOLVED_INVENTORY" \
    "${INFRA_DIR}/playbooks/configure_network.yml"
rm -f "$RESOLVED_INVENTORY"

# ---------- install packages on network node ----------

info "Preparing network node..."
docker exec "$NET_NODE" apk add --no-cache iptables iproute2 python3 openssh frr >/dev/null 2>&1
docker exec "$NET_NODE" ssh-keygen -A >/dev/null 2>&1
docker exec "$NET_NODE" sh -c "echo 'root:root' | chpasswd"
docker exec "$NET_NODE" sh -c "echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config"
docker exec "$NET_NODE" /usr/sbin/sshd
echo "  Installed iptables, iproute2, python3, openssh, frr on net-node"

docker exec "$NET_NODE" ip addr replace 10.0.0.30/24 dev eth1
echo "  net-node:eth1 = 10.0.0.30/24 (native VLAN)"

docker exec "$NET_NODE" iptables -t nat -C POSTROUTING -s 10.0.0.0/24 -o eth2 -j MASQUERADE 2>/dev/null || \
    docker exec "$NET_NODE" iptables -t nat -A POSTROUTING -s 10.0.0.0/24 -o eth2 -j MASQUERADE
echo "  net-node: MASQUERADE 10.0.0.0/24 → eth2"

# ---------- configure BGP peering ----------

info "Configuring BGP peering link..."
docker exec "$NET_NODE" ip addr replace "$BGP_NET_NODE_IP" dev eth2
docker exec "$NET_NODE" ip link set eth2 up
docker exec "$UPSTREAM_ROUTER" ip addr replace "$BGP_UPSTREAM_IP" dev eth1
docker exec "$UPSTREAM_ROUTER" ip link set eth1 up
echo "  net-node:eth2 = ${BGP_NET_NODE_IP}, upstream-router:eth1 = ${BGP_UPSTREAM_IP}"

info "Configuring FRR on net-node (AS ${BGP_NET_NODE_AS})..."
docker exec "$NET_NODE" sh -c "cat > /etc/frr/frr.conf <<EOF
frr defaults traditional
hostname net-node
log syslog informational

router bgp ${BGP_NET_NODE_AS}
 bgp router-id ${BGP_NET_NODE_IP%/*}
 no bgp ebgp-requires-policy
 neighbor ${BGP_UPSTREAM_IP%/*} remote-as ${BGP_UPSTREAM_AS}
 address-family ipv4 unicast
  redistribute static
 exit-address-family
EOF"
docker exec "$NET_NODE" sh -c "sed -i 's/bgpd=no/bgpd=yes/' /etc/frr/daemons"
docker exec "$NET_NODE" sh -c "/usr/lib/frr/frrinit.sh start" 2>/dev/null || true

info "Preparing upstream router..."
docker exec "$UPSTREAM_ROUTER" apk add --no-cache frr iptables >/dev/null 2>&1

info "Configuring FRR on upstream-router (AS ${BGP_UPSTREAM_AS})..."
docker exec "$UPSTREAM_ROUTER" sh -c "cat > /etc/frr/frr.conf <<EOF
frr defaults traditional
hostname upstream-router
log syslog informational

router bgp ${BGP_UPSTREAM_AS}
 bgp router-id ${BGP_UPSTREAM_IP%/*}
 no bgp ebgp-requires-policy
 neighbor ${BGP_NET_NODE_IP%/*} remote-as ${BGP_NET_NODE_AS}
 address-family ipv4 unicast
 exit-address-family
EOF"
docker exec "$UPSTREAM_ROUTER" sh -c "sed -i 's/bgpd=no/bgpd=yes/' /etc/frr/daemons"
docker exec "$UPSTREAM_ROUTER" sh -c "/usr/lib/frr/frrinit.sh start" 2>/dev/null || true

info "Waiting for BGP session to establish (10s)..."
sleep 10

docker exec "$UPSTREAM_ROUTER" sysctl -w net.ipv4.ip_forward=1 >/dev/null
docker exec "$UPSTREAM_ROUTER" iptables -t nat -C POSTROUTING -o eth0 -j MASQUERADE 2>/dev/null || \
    docker exec "$UPSTREAM_ROUTER" iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
docker exec "$NET_NODE" ip route replace default via ${BGP_UPSTREAM_IP%/*} dev eth2
echo "  Lab hack: net-node default route via upstream-router, u/s MASQUERADE → eth0"

ip route replace 192.168.100.0/24 via "${MGMT_PREFIX}.40"
echo "  Added host route 192.168.100.0/24 via ${MGMT_PREFIX}.40 (upstream-router)"

# ---------- create inventory ConfigMap ----------

info "Creating agentless-net inventory ConfigMap..."
RESOLVED_INVENTORY=$(mktemp --suffix=.yml)
MGMT_PREFIX="$MGMT_PREFIX" envsubst < "$INVENTORY" > "$RESOLVED_INVENTORY"
KUBECONFIG="$KUBECONFIG" oc create configmap agentless-net-inventory \
    --from-file=inventory.yml="$RESOLVED_INVENTORY" \
    -n "$OSAC_NAMESPACE" \
    --dry-run=client -o yaml | \
    KUBECONFIG="$KUBECONFIG" oc apply -f -
rm -f "$RESOLVED_INVENTORY"

info "deploy-fabric complete."
