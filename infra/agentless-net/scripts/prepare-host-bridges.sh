#!/usr/bin/env bash
#
# Attach the containerlab host veths to the bridges the virtual BMH fabric
# NICs use, and keep bridged frames out of the host firewall.
#
# DHCP discovers are IPv4 broadcasts from 0.0.0.0 to 255.255.255.255. With
# net.bridge.bridge-nf-call-iptables=1 those frames hit the host FORWARD
# chain, whose policy drops anything outside 10/8 and 192.168/16. ARP is
# not filtered, so a bridge can look healthy while the guest never gets
# a lease. Disabling the bridge netfilter hook is what lets the discover
# reach the leaf.
#
# Idempotent. Called after containerlab has created leaf1-swp2 and leaf2-swp2.
#
set -euo pipefail

BRIDGES=(br-host1 br-host2)
VETHS=(leaf1-swp2 leaf2-swp2)
SYSCTL_FILE=/etc/sysctl.d/99-osac-host-bridges.conf
# Previous runtime values. A second prepare keeps this file so teardown
# restores the values from before the first change, not the zeros we set.
STATE_FILE=/var/tmp/osac-host-bridge-sysctl.prior

info() { echo "==> $*"; }

restore_host_bridge_sysctls() {
    trap - ERR
    if [[ ! -f "$STATE_FILE" ]]; then
        return 0
    fi
    local ipt ip6
    ipt="$(sudo sed -n 's/^net.bridge.bridge-nf-call-iptables=//p' "$STATE_FILE")"
    ip6="$(sudo sed -n 's/^net.bridge.bridge-nf-call-ip6tables=//p' "$STATE_FILE")"
    if [[ ! "$ipt" =~ ^[01]$ || ! "$ip6" =~ ^[01]$ ]]; then
        echo "ERROR: ${STATE_FILE} has no saved bridge sysctl values; leaving the runtime settings unchanged." >&2
        return 0
    fi
    sudo sysctl -w "net.bridge.bridge-nf-call-iptables=${ipt}"
    sudo sysctl -w "net.bridge.bridge-nf-call-ip6tables=${ip6}"
    sudo rm -f "$SYSCTL_FILE" "$STATE_FILE"
    info "Restored bridge netfilter sysctls"
}

if [[ "${1:-}" == "restore" ]]; then
    restore_host_bridge_sysctls
    exit 0
fi

# A failed setup puts the runtime values and the persistent file back.
# The saved values stay in place until that restore runs.
prepare_failed() {
    local status=$?
    restore_host_bridge_sysctls || true
    exit "$status"
}
trap prepare_failed ERR

if ! command -v sudo >/dev/null 2>&1; then
    echo "ERROR: sudo is required to configure host bridges" >&2
    exit 1
fi

sudo modprobe br_netfilter

for i in "${!BRIDGES[@]}"; do
    bridge="${BRIDGES[$i]}"
    veth="${VETHS[$i]}"
    if ! ip link show "$veth" >/dev/null 2>&1; then
        echo "ERROR: ${veth} does not exist. Deploy the containerlab topology first." >&2
        exit 1
    fi
    if ! ip link show "$bridge" >/dev/null 2>&1; then
        info "Creating ${bridge}"
        sudo ip link add "$bridge" type bridge
    fi
    sudo ip link set "$veth" master "$bridge"
    sudo ip link set "$veth" up
    sudo ip link set "$bridge" up
    info "${veth} is a member of ${bridge}"
done

info "Disabling bridge netfilter so DHCP discovers are forwarded"
if [[ ! -f "$STATE_FILE" ]]; then
    ipt="$(sudo sysctl -n net.bridge.bridge-nf-call-iptables)"
    ip6="$(sudo sysctl -n net.bridge.bridge-nf-call-ip6tables)"
    if [[ ! "$ipt" =~ ^[01]$ || ! "$ip6" =~ ^[01]$ ]]; then
        echo "ERROR: refusing to change bridge sysctls; current values are not 0 or 1." >&2
        exit 1
    fi
    state_tmp="$(mktemp)"
    printf 'net.bridge.bridge-nf-call-iptables=%s\nnet.bridge.bridge-nf-call-ip6tables=%s\n' "$ipt" "$ip6" >"$state_tmp"
    sudo install -m 600 "$state_tmp" "$STATE_FILE"
    rm -f "$state_tmp"
fi
sudo tee "$SYSCTL_FILE" >/dev/null <<'EOF'
# Host bridges br-host1 and br-host2 carry tenant DHCP. Bridged frames
# must not enter iptables; the FORWARD policy drops 0.0.0.0 -> 255.255.255.255.
net.bridge.bridge-nf-call-iptables = 0
net.bridge.bridge-nf-call-ip6tables = 0
EOF
sudo sysctl -w net.bridge.bridge-nf-call-iptables=0
sudo sysctl -w net.bridge.bridge-nf-call-ip6tables=0
