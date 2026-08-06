#!/usr/bin/env bash
# audit-relay-host.sh -- Survey a candidate relay machine before adopting
# it (or before decommissioning one being replaced) for the tunnel-relay
# pattern documented in monitoring/vpn-relay-access.md.
#
# Run on the RELAY machine itself (as root or with sudo), same as
# setup-tunnel-relay.sh.
#
# Usage:
#   audit-relay-host.sh
#
# Why this exists: relay machines in practice have turned out to be
# shared lab boxes, not dedicated hosts -- when one was checked before a
# planned replacement, it turned out to also have two unrelated personal
# accounts and an nginx reverse proxy (predating the tunnel setup, not
# mentioned anywhere in vpn-relay-access.md) still actively forwarding a
# port to Prometheus. None of that is a problem by itself, but finding it
# ad hoc, by hand, right before decommissioning a host is exactly the
# situation this script exists to avoid -- run it BEFORE adopting a new
# relay (so you know what else is already on it) and BEFORE decommissioning
# an old one (so nothing undocumented gets silently lost).
#
# Read-only: never modifies anything, changes no config, stops no service.
set -euo pipefail

echo "=== Host ==="
hostname -f
uptime
echo

echo "=== Disk usage ==="
df -h / 2>/dev/null
echo

echo "=== Accounts on this host ==="
echo "Accounts matching *-tunnel are the documented pattern from"
echo "vpn-relay-access.md -- setup-tunnel-relay.sh creates them with"
echo "useradd -r, so they land in the SYSTEM uid range, not 1000+; they're"
echo "matched by name here, not uid range, for that reason. Anything else"
echo "in the 1000-65533 (regular user) range belongs to someone/something"
echo "unrelated to this relay setup -- confirm with its owner before this"
echo "host is decommissioned, since nothing here backs that data up."
echo
TUNNEL_USERS=()
OTHER_USERS=()
while IFS=: read -r name _ uid _; do
    if [[ "${name}" == *-tunnel ]]; then
        TUNNEL_USERS+=("${name}")
    elif (( uid >= 1000 && uid < 65534 )); then
        OTHER_USERS+=("${name}")
    fi
done < /etc/passwd

if [[ ${#TUNNEL_USERS[@]} -gt 0 ]]; then
    echo "Tunnel identities found:"
    for u in "${TUNNEL_USERS[@]}"; do
        echo "  - ${u}"
    done
else
    echo "No *-tunnel accounts found -- this host has no tunnel-relay identity set up yet."
fi
echo
if [[ ${#OTHER_USERS[@]} -gt 0 ]]; then
    echo "OTHER accounts found (not ours -- flag before replacing this host):"
    for u in "${OTHER_USERS[@]}"; do
        size="$(du -sh "/home/${u}" 2>/dev/null | cut -f1)"
        echo "  - ${u} (home dir: ${size:-unknown size})"
    done
else
    echo "No other non-system accounts found."
fi
echo

echo "=== Tunnel systemd services ==="
FOUND_SERVICE=0
for u in "${TUNNEL_USERS[@]}"; do
    svc="${u}.service"
    if systemctl list-unit-files "${svc}" --no-legend 2>/dev/null | grep -q .; then
        FOUND_SERVICE=1
        state="$(systemctl is-active "${svc}" 2>/dev/null || true)"
        target="$(systemctl show -p ExecStart "${svc}" 2>/dev/null | grep -oP '(?<=argv\[\]=)[^;]*' | head -1)"
        echo "  ${svc}: ${state}"
        [[ -n "${target}" ]] && echo "    ${target}" | head -c 400 && echo
    fi
done
if [[ "${FOUND_SERVICE}" -eq 0 && ${#TUNNEL_USERS[@]} -gt 0 ]]; then
    echo "  WARNING: tunnel user(s) exist but no matching *.service unit found."
fi
echo

echo "=== Anything else listening/proxying on the well-known tunnel ports ==="
echo "(3000 Grafana, 9091 Prometheus, 9093 Alertmanager, 8210 Vault)"
echo "If this shows a process that ISN'T an ssh tunnel owned by a *-tunnel"
echo "user above, something undocumented is also using these ports --"
echo "confirm what it is and whether it's still needed before migrating."
echo
if command -v ss >/dev/null 2>&1; then
    ss -tlnp 2>/dev/null | awk 'NR==1 || /:(3000|9091|9093|8210)[[:space:]]/' | cut -c1-160
else
    echo "  (ss not available, skipping)"
fi
echo

echo "=== nginx/httpd configs mentioning these ports ==="
echo "Checked because exactly this was found once: an nginx config from"
echo "well before any tunnel setup, still active, silently proxying one of"
echo "these ports. Not necessarily a problem, but not something"
echo "vpn-relay-access.md knows about either -- so it won't get carried"
echo "over automatically if this host is replaced."
echo
MATCHES="$(grep -rl -E '3000|9091|9093|8210' /etc/nginx/ /etc/httpd/ 2>/dev/null || true)"
if [[ -n "${MATCHES}" ]]; then
    echo "${MATCHES}"
    echo
    echo "${MATCHES}" | while IFS= read -r f; do
        echo "--- ${f} ---"
        cat "${f}"
        echo
    done
else
    echo "  None found."
fi
echo

echo "=== Done ==="
echo "This is a read-only report. Nothing on this host was changed."
