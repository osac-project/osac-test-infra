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

    # Podman container/image metrics (OSAC-2207): running container count and
    # total image storage bytes, so a runaway container count or unbounded
    # image growth shows up before it trickles down into a generic
    # DiskAlmostFull alert with no earlier warning. `podman system df --format
    # json`'s Images entry already reports RawSize as the total on-disk bytes
    # across all images, not just the active/reclaimable split -- exactly
    # "image storage bytes", no extra summing needed.
    # `if var=$(cmd | cmd2); then` -- not `var=$(cmd | cmd2 2>/dev/null || echo 0)`:
    # under `set -o pipefail`, a failing left-hand command in a piped command
    # substitution assignment aborts the whole script (confirmed live), and
    # a naive `|| echo 0` fallback risks double-output if the right-hand
    # command in the pipe already printed something before the pipeline as a
    # whole reported failure. Commands used as an `if` condition are exempt
    # from `set -e`/`pipefail`-triggered abort, so this pattern is safe
    # either way, and each branch controls its own single line of output.
    echo "# HELP osac_podman_running_containers Number of currently-running podman containers."
    echo "# TYPE osac_podman_running_containers gauge"
    if podman_ps_output="$(podman ps -q 2>/dev/null)"; then
        running_containers="$(printf '%s' "${podman_ps_output}" | grep -c . || true)"
    else
        running_containers=0
    fi
    echo "osac_podman_running_containers ${running_containers:-0}"

    echo "# HELP osac_podman_image_storage_bytes Total on-disk size of all podman images, in bytes."
    echo "# TYPE osac_podman_image_storage_bytes gauge"
    # podman system df's Images entry already reports RawSize as the total
    # on-disk bytes across all images, not just the active/reclaimable split
    # -- exactly "image storage bytes", no extra summing needed.
    if podman_df_json="$(podman system df --format json 2>/dev/null)"; then
        image_bytes="$(printf '%s' "${podman_df_json}" | python3 -c \
            'import json,sys; data=json.load(sys.stdin); print(next((e["RawSize"] for e in data if e.get("Type")=="Images"), 0))' \
            2>/dev/null || true)"
    else
        image_bytes=0
    fi
    echo "osac_podman_image_storage_bytes ${image_bytes:-0}"

    # cluster-tool VM overlay/flavor/container disk usage (OSAC-2208): broken
    # out by subdirectory so a runaway overlay is identifiable by *what* grew,
    # not just that the filesystem did -- generic root-filesystem metrics
    # give no such breakdown, and on hosts where this data path is a
    # separate mount (confirmed live: osac-ci-1's /disk1), a runaway overlay
    # there wouldn't even show up in root-filesystem usage at all.
    #
    # scripts/machine-init.sh writes the real, authoritative path to
    # ~/.config/cluster-tool/config as CLUSTER_TOOL_DATA=<path> (auto-detected
    # from the largest partition at provisioning time, so it varies by host
    # for reasons this script has no other way to know) -- read that first.
    # /disk1/cluster-tool and /cluster-tool (confirmed live on osac-ci-1 and
    # ordinary runner hosts respectively) are kept only as a fallback for a
    # host where that config is missing or stale, not the primary source.
    CLUSTER_TOOL_DIR=""
    CT_CONFIG="${HOME}/.config/cluster-tool/config"
    if [[ -f "${CT_CONFIG}" ]]; then
        configured_data="$(grep '^CLUSTER_TOOL_DATA=' "${CT_CONFIG}" 2>/dev/null | cut -d= -f2- || true)"
        if [[ -n "${configured_data}" ]] && [[ -d "${configured_data}" ]]; then
            CLUSTER_TOOL_DIR="${configured_data}"
        fi
    fi
    if [[ -z "${CLUSTER_TOOL_DIR}" ]]; then
        for candidate in /disk1/cluster-tool /cluster-tool; do
            if [[ -d "${candidate}" ]]; then
                CLUSTER_TOOL_DIR="${candidate}"
                break
            fi
        done
    fi
    if [[ -n "${CLUSTER_TOOL_DIR}" ]]; then
        echo "# HELP osac_cluster_tool_disk_bytes Disk usage of cluster-tool's data subdirectories, in bytes."
        echo "# TYPE osac_cluster_tool_disk_bytes gauge"
        for subdir in flavors overlays containers; do
            path="${CLUSTER_TOOL_DIR}/${subdir}"
            if [[ -d "${path}" ]]; then
                # --block-size=1, not -b/--bytes: -b implies --apparent-size,
                # which reports sparse VM overlay files' logical length
                # rather than actual disk consumption -- exactly the wrong
                # number for a metric whose entire purpose is "is disk about
                # to fill up". Plain `du` (no --apparent-size) already
                # reports real block usage; --block-size=1 only changes the
                # unit to bytes, cheap on this timer's 30s cadence same as
                # before (confirmed live: a few hundred GB of VM images
                # summed in single-digit milliseconds either way).
                bytes="$(du -s --block-size=1 "${path}" 2>/dev/null | cut -f1 || true)"
                echo "osac_cluster_tool_disk_bytes{path=\"${subdir}\"} ${bytes:-0}"
            fi
        done
    fi
} > "${tmp}"

# node_exporter's textfile collector watches the DIRECTORY, not a specific
# file's inode (unlike prometheus.yml's single-file bind mount -- see
# OSAC-2202), so a plain atomic `mv` into place is the standard, safe
# pattern here: readers never see a partially-written file.
chmod 644 "${tmp}"
mv "${tmp}" "${OUTPUT_FILE}"
