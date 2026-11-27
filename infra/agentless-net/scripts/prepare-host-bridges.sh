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
# /var/tmp is writable by every user, so a planted file could skip the real
# save or be restored later. Keep the prior values in a root-owned directory.
STATE_DIR=/var/lib/osac-host-bridges
STATE_FILE=/var/lib/osac-host-bridges/sysctl.prior

info() { echo "==> $*"; }

ensure_private_state_dir() {
    # sudo test: the caller is not root, and a 700 directory is invisible to [[ -e ]].
    if sudo test -L "$STATE_DIR"; then
        echo "ERROR: ${STATE_DIR} is a symlink; refusing to store rollback state there." >&2
        return 1
    fi
    if sudo test -e "$STATE_DIR"; then
        if ! sudo test -d "$STATE_DIR"; then
            echo "ERROR: ${STATE_DIR} is not a directory." >&2
            return 1
        fi
        local owner mode
        owner="$(sudo stat -c '%u' "$STATE_DIR")"
        mode="$(sudo stat -c '%a' "$STATE_DIR")"
        if [[ "$owner" != "0" || "$mode" != "700" ]]; then
            echo "ERROR: ${STATE_DIR} is not a root-owned private directory." >&2
            return 1
        fi
        return 0
    fi
    sudo install -d -m 700 -o root -g root "$STATE_DIR"
}

state_file_is_trusted() {
    if sudo test -L "$STATE_FILE"; then
        echo "ERROR: ${STATE_FILE} is a symlink; refusing to use it." >&2
        return 1
    fi
    if ! sudo test -f "$STATE_FILE"; then
        echo "ERROR: ${STATE_FILE} is not a regular file; refusing to use it." >&2
        return 1
    fi
    local owner mode
    owner="$(sudo stat -c '%u' "$STATE_FILE")"
    mode="$(sudo stat -c '%a' "$STATE_FILE")"
    if [[ "$owner" != "0" || "$mode" != "600" ]]; then
        echo "ERROR: ${STATE_FILE} is not a root-owned private file; refusing to use it." >&2
        return 1
    fi
    return 0
}

restore_host_bridge_sysctls() {
    trap - ERR
    if ! sudo -n true; then
        echo "ERROR: cannot inspect ${STATE_FILE}; sudo is required." >&2
        return 1
    fi
    if ! sudo test -e "$STATE_FILE" && ! sudo test -L "$STATE_FILE"; then
        return 0
    fi
    if ! state_file_is_trusted; then
        return 1
    fi
    local ipt ip6
    ipt="$(sudo sed -n 's/^net.bridge.bridge-nf-call-iptables=//p' "$STATE_FILE")"
    ip6="$(sudo sed -n 's/^net.bridge.bridge-nf-call-ip6tables=//p' "$STATE_FILE")"
    if [[ ! "$ipt" =~ ^[01]$ || ! "$ip6" =~ ^[01]$ ]]; then
        echo "ERROR: ${STATE_FILE} has no saved bridge sysctl values; leaving the runtime settings unchanged." >&2
        return 1
    fi
    # Check each write so a failure cannot fall through to deleting the saved values.
    if ! sudo sysctl -w "net.bridge.bridge-nf-call-iptables=${ipt}"; then
        echo "ERROR: failed to restore net.bridge.bridge-nf-call-iptables; keeping ${STATE_FILE}." >&2
        return 1
    fi
    if ! sudo sysctl -w "net.bridge.bridge-nf-call-ip6tables=${ip6}"; then
        echo "ERROR: failed to restore net.bridge.bridge-nf-call-ip6tables; keeping ${STATE_FILE}." >&2
        return 1
    fi
    sudo rm -f "$SYSCTL_FILE" "$STATE_FILE"
    info "Restored bridge netfilter sysctls"
}

if [[ "${1:-}" == "restore" ]]; then
    restore_host_bridge_sysctls || exit $?
    exit 0
fi

# Roll back only sysctls this attempt changed. A retry that fails before
# those writes must not restore the original checkpoint over a running lab.
changed_sysctls=0
checkpoint_existed=0
attempt_ipt=0
attempt_ip6=0
sysctl_file_written=0
sysctl_file_existed=0
sysctl_backup=""
prepare_failed() {
    local status=$?
    if [[ "$changed_sysctls" -eq 1 ]]; then
        local ipt_restored=0
        local ip6_restored=0
        if sudo sysctl -w "net.bridge.bridge-nf-call-iptables=${attempt_ipt}"; then
            ipt_restored=1
        fi
        if sudo sysctl -w "net.bridge.bridge-nf-call-ip6tables=${attempt_ip6}"; then
            ip6_restored=1
        fi
        # A failed rollback must keep the new checkpoint so a later restore can retry.
        if [[ "$ipt_restored" -eq 1 && "$ip6_restored" -eq 1 && "$checkpoint_existed" -ne 1 ]]; then
            sudo rm -f "$STATE_FILE" || true
        fi
    fi
    if [[ "$sysctl_file_written" -eq 1 ]]; then
        if [[ "$sysctl_file_existed" -eq 1 && -n "$sysctl_backup" ]]; then
            sudo tee "$SYSCTL_FILE" <"$sysctl_backup" >/dev/null || true
        else
            sudo rm -f "$SYSCTL_FILE" || true
        fi
    fi
    if [[ -n "$sysctl_backup" ]]; then
        rm -f "$sysctl_backup" || true
    fi
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
ensure_private_state_dir
if sudo test -e "$STATE_FILE" || sudo test -L "$STATE_FILE"; then
    checkpoint_existed=1
    state_file_is_trusted
else
    ipt="$(sudo sysctl -n net.bridge.bridge-nf-call-iptables)"
    ip6="$(sudo sysctl -n net.bridge.bridge-nf-call-ip6tables)"
    if [[ ! "$ipt" =~ ^[01]$ || ! "$ip6" =~ ^[01]$ ]]; then
        echo "ERROR: refusing to change bridge sysctls; current values are not 0 or 1." >&2
        exit 1
    fi
    state_tmp="$(mktemp)"
    printf 'net.bridge.bridge-nf-call-iptables=%s\nnet.bridge.bridge-nf-call-ip6tables=%s\n' "$ipt" "$ip6" >"$state_tmp"
    sudo install -m 600 -o root -g root "$state_tmp" "$STATE_FILE"
    rm -f "$state_tmp"
fi
if sudo test -e "$SYSCTL_FILE" || sudo test -L "$SYSCTL_FILE"; then
    sysctl_file_existed=1
    sysctl_backup="$(mktemp)"
    sudo cat "$SYSCTL_FILE" | tee "$sysctl_backup" >/dev/null
fi
sysctl_file_written=1
sudo tee "$SYSCTL_FILE" >/dev/null <<'EOF'
# Host bridges br-host1 and br-host2 carry tenant DHCP. Bridged frames
# must not enter iptables; the FORWARD policy drops 0.0.0.0 -> 255.255.255.255.
net.bridge.bridge-nf-call-iptables = 0
net.bridge.bridge-nf-call-ip6tables = 0
EOF
attempt_ipt="$(sudo sysctl -n net.bridge.bridge-nf-call-iptables)"
attempt_ip6="$(sudo sysctl -n net.bridge.bridge-nf-call-ip6tables)"
if [[ ! "$attempt_ipt" =~ ^[01]$ || ! "$attempt_ip6" =~ ^[01]$ ]]; then
    echo "ERROR: refusing to change bridge sysctls; current values are not 0 or 1." >&2
    exit 1
fi
changed_sysctls=1
sudo sysctl -w net.bridge.bridge-nf-call-iptables=0
sudo sysctl -w net.bridge.bridge-nf-call-ip6tables=0
if [[ -n "$sysctl_backup" ]]; then
    rm -f "$sysctl_backup"
fi
sysctl_file_written=0
