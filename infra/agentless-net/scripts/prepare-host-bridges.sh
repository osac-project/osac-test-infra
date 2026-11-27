#!/usr/bin/env bash
#
# Attach the containerlab host veths to the bridges the virtual BMH fabric
# NICs use, and keep bridged frames out of the host firewall.
#
# DHCP discovers are IPv4 broadcasts from 0.0.0.0 to 255.255.255.255. With
# net.bridge.bridge-nf-call-iptables=1 those frames hit the host FORWARD
# chain, whose policy drops anything outside 10/8 and 192.168/16. ARP is not
# filtered, so a bridge can look healthy while the guest never gets a lease.
# Disabling the bridge netfilter hook is what lets the discover reach the leaf.
#
# The bridge sysctls are host-wide, not per-bridge. We save the previous values
# once (root-owned, mode 600) and restore them from destroy-fabric, so a shared
# lab goes back to how it was. On a disposable CI box the restore is a no-op.
#
# Idempotent. Called after containerlab has created leaf1-swp2 and leaf2-swp2.
#
set -euo pipefail

BRIDGES=(br-host1 br-host2)
VETHS=(leaf1-swp2 leaf2-swp2)
SYSCTL_FILE=/etc/sysctl.d/99-osac-host-bridges.conf
# Root-owned so /var/tmp planting cannot make us skip the save or restore a
# forged value later.
STATE_DIR=/var/lib/osac-host-bridges
STATE_FILE=${STATE_DIR}/sysctl.prior
SYSCTLS=(net.bridge.bridge-nf-call-iptables net.bridge.bridge-nf-call-ip6tables)

info() { echo "==> $*"; }

# sudo stat: a 700 root-owned directory is invisible to a non-root [[ -e ]].
state_is_trusted() {
    if sudo test -L "$STATE_DIR" || sudo test -L "$STATE_FILE"; then
        echo "ERROR: ${STATE_FILE} is a symlink; refusing to use it." >&2
        return 1
    fi
    if ! sudo test -d "$STATE_DIR" || [ "$(sudo stat -c '%u:%a' "$STATE_DIR")" != "0:700" ]; then
        echo "ERROR: ${STATE_DIR} is not a root-owned 700 directory." >&2
        return 1
    fi
    if sudo test -e "$STATE_FILE" && [ "$(sudo stat -c '%u:%a' "$STATE_FILE")" != "0:600" ]; then
        echo "ERROR: ${STATE_FILE} is not a root-owned 600 file." >&2
        return 1
    fi
    return 0
}

restore_sysctls() {
    if ! sudo test -e "$STATE_FILE" && ! sudo test -L "$STATE_FILE"; then
        return 0
    fi
    state_is_trusted || return 1
    local name value
    for name in "${SYSCTLS[@]}"; do
        value="$(sudo sed -n "s/^${name}=//p" "$STATE_FILE")"
        if [[ ! "$value" =~ ^[01]$ ]]; then
            echo "ERROR: ${STATE_FILE} has no saved ${name}; leaving runtime settings unchanged." >&2
            return 1
        fi
        # Fail before deleting the checkpoint if a restore does not take.
        sudo sysctl -w "${name}=${value}" || return 1
    done
    sudo rm -f "$SYSCTL_FILE" "$STATE_FILE"
    info "Restored bridge netfilter sysctls"
}

if [[ "${1:-}" == "restore" ]]; then
    restore_sysctls
    exit $?
fi

command -v sudo >/dev/null 2>&1 || { echo "ERROR: sudo is required to configure host bridges" >&2; exit 1; }
sudo modprobe br_netfilter

for i in "${!BRIDGES[@]}"; do
    bridge="${BRIDGES[$i]}"
    veth="${VETHS[$i]}"
    ip link show "$veth" >/dev/null 2>&1 || {
        echo "ERROR: ${veth} does not exist. Deploy the containerlab topology first." >&2
        exit 1
    }
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
sudo install -d -m 700 -o root -g root "$STATE_DIR"
state_is_trusted
if ! sudo test -e "$STATE_FILE"; then
    current=()
    for name in "${SYSCTLS[@]}"; do
        value="$(sudo sysctl -n "$name")"
        if [[ ! "$value" =~ ^[01]$ ]]; then
            echo "ERROR: refusing to change ${name}; current value '${value}' is not 0 or 1." >&2
            exit 1
        fi
        current+=("${name}=${value}")
    done
    printf '%s\n' "${current[@]}" | sudo install -m 600 -o root -g root /dev/stdin "$STATE_FILE"
fi
sudo tee "$SYSCTL_FILE" >/dev/null <<'EOF'
# Host bridges br-host1 and br-host2 carry tenant DHCP. Bridged frames
# must not enter iptables; the FORWARD policy drops 0.0.0.0 -> 255.255.255.255.
net.bridge.bridge-nf-call-iptables = 0
net.bridge.bridge-nf-call-ip6tables = 0
EOF
for name in "${SYSCTLS[@]}"; do
    sudo sysctl -w "${name}=0"
done
