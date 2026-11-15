#!/usr/bin/env bash
# softgate-read.sh - read-only dump of what the Netris softgates apply now.
#
#   softgate-read.sh <journal window> <journal lines>
#
# For each running ns-softgate-<n> libvirt domain (management address 10.3.3.<n+1>,
# the lab layout), prints a "=== ns-softgate-<n> (<ip>)" header followed by the
# softgate's clock, NAT rules, HTTPS conntrack entries and the last <journal lines>
# lines of its Netris agent journal since <journal window> (a journalctl --since
# value such as "-15 min"). Softgates are read in parallel over SSH as root with the
# lab host's key, as setup does. An unreachable softgate is reported in its section
# and does not fail the script. fabric-snapshot.sh runs it at each test failure,
# and gather-infra at the end of the job.
set -uo pipefail

since="${1:?journal window, e.g. -15 min}"
lines="${2:?journal line limit}"
[[ "${lines}" =~ ^[0-9]+$ ]] || { echo "softgate-read: journal line limit must be a number" >&2; exit 2; }

command -v virsh >/dev/null 2>&1 || { echo "softgate-read: virsh is not available" >&2; exit 0; }

work="$(mktemp -d)" || exit 1
trap 'rm -rf -- "${work}"' EXIT

# Expanded on the softgate, not here.
remote="date -u +%FT%T.%NZ
echo '== nat rules'; nft list table ip nat 2>/dev/null || iptables-save -t nat 2>/dev/null
echo '== https conntrack'; conntrack -L -p tcp --dport 443 2>/dev/null | head -300
echo '== netris journal'; journalctl -u 'netris*' --since '${since}' -o short-iso-precise --no-pager 2>/dev/null | tail -${lines}"

# The loop reads from a process substitution, not a pipe, so the reads it starts are
# children of this shell and the wait below waits for them.
while read -r name n; do
    ip="10.3.3.$((n + 1))"
    (
        echo "=== ${name} (${ip})"
        ssh -n -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -o LogLevel=ERROR "root@${ip}" "${remote}" 2>&1 || echo "softgate-read: ${name} (${ip}) read failed (exit $?)"
    ) > "${work}/${name}" &
done < <(virsh list --name 2>/dev/null | sed -n 's/^\(ns-softgate-\([0-9]\+\)\)$/\1 \2/p')
wait
cat "${work}"/* 2>/dev/null
exit 0
