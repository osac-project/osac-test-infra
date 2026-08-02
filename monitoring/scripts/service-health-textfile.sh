#!/usr/bin/env bash
# service-health-textfile.sh -- Writes systemd unit active/inactive state as
# Prometheus textfile-collector metrics.
#
# Tracks machine-level services that node_exporter's built-in collectors
# don't cover: the GitHub Actions runner agent(s), libvirtd, haproxy, and
# podman.socket -- all installed by scripts/machine-init.sh and
# scripts/runners/action-runners-setup.sh, on every runner machine (central
# and agents alike), none of which are otherwise monitored.
#
# These are system-level units; this monitoring stack runs rootless under
# `systemctl --user`. node_exporter's built-in --collector.systemd needs
# D-Bus access to the bus that owns the unit, and mixing system-bus and
# user-bus units in one collector invocation adds real complexity for a
# single boolean signal -- so this uses the plain, standard Prometheus
# textfile-collector pattern instead: a script + timer writes a .prom file,
# node_exporter reads it like any other collector source.
#
# Installed by monitoring-setup.sh as a systemd --user timer
# (service-health-textfile.timer, every 30s) on every machine.
set -euo pipefail

TEXTFILE_DIR="${TEXTFILE_DIR:-${HOME}/.monitoring-server/data/textfile-collector}"
OUTPUT_FILE="${TEXTFILE_DIR}/osac_service_health.prom"

mkdir -p "${TEXTFILE_DIR}"

# Fixed units present on every runner machine. libvirtd is checked
# separately below: CentOS Stream 10 hosts split between the legacy
# monolithic libvirtd.service and the modular virtqemud.service depending
# on how/when they were provisioned, and both are equally valid running
# states (confirmed live: osac-9 runs VMs under virtqemud with
# libvirtd.service never having started since boot). Alerting on either
# unit by name alone false-pages on whichever mode a given host doesn't use.
UNITS=(haproxy.service podman.socket)

# Whether libvirt is available, either daemon mode. Checked via the SOCKET
# units, not the .service units: both daemons are socket-activated and their
# .service idles back to inactive after a timeout with no active connection
# (confirmed live: virtqemud.service reads inactive on hosts with no VM
# currently running, even though libvirt is fully available on demand). The
# socket units stay persistently active regardless of idle state, so they're
# the reliable "is libvirt available" signal.
LIBVIRT_SOCKETS=(libvirtd.socket virtqemud.socket)

# GitHub Actions runner agent(s) -- a machine can have one or several
# runner-NN instances (see scripts/runners/action-runners-setup.sh), so
# discover them by glob rather than hardcoding a count.
while IFS= read -r unit; do
    [[ -n "${unit}" ]] && UNITS+=("${unit}")
done < <(systemctl list-units --all --plain --no-legend 'actions.runner.*' 2>/dev/null | awk '{print $1}')

tmp="$(mktemp "${TEXTFILE_DIR}/.osac_service_health.XXXXXX")"
{
    echo "# HELP osac_service_active Whether a monitored systemd unit is active (1) or not (0)."
    echo "# TYPE osac_service_active gauge"
    for unit in "${UNITS[@]}"; do
        if systemctl is-active --quiet "${unit}" 2>/dev/null; then
            state=1
        else
            state=0
        fi
        echo "osac_service_active{unit=\"${unit}\"} ${state}"
    done

    # Synthetic aggregate: healthy if EITHER libvirt daemon's socket is
    # active (see LIBVIRT_SOCKETS comment above). "libvirt" is not a real
    # systemd unit name.
    libvirt_state=0
    for sock in "${LIBVIRT_SOCKETS[@]}"; do
        if systemctl is-active --quiet "${sock}" 2>/dev/null; then
            libvirt_state=1
            break
        fi
    done
    echo "osac_service_active{unit=\"libvirt\"} ${libvirt_state}"
} > "${tmp}"

# node_exporter's textfile collector watches the DIRECTORY, not a specific
# file's inode (unlike prometheus.yml's single-file bind mount -- see
# OSAC-2202), so a plain atomic `mv` into place is the standard, safe
# pattern here: readers never see a partially-written file.
chmod 644 "${tmp}"
mv "${tmp}" "${OUTPUT_FILE}"
