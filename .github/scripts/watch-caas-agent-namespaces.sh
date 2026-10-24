#!/usr/bin/env bash
# Watch for active ClusterOrders and provision agents in each order namespace.
# Set CAAS_AGENT_WATCHER_SETUP_ENABLED=false to use the watcher only for
# virtual-BMH boot-order repair (for example, hot-pool BMI flows).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OSAC_NAMESPACE="${OSAC_NAMESPACE:-osac-e2e-ci}"
RUNNER_TEMP="${RUNNER_TEMP:-/tmp}"
LOG_DIR="${CAAS_AGENT_WATCHER_ORDER_LOG_DIR:-${RUNNER_TEMP}/caas-agent-order-logs}"
STATUS_FILE="${CAAS_AGENT_WATCHER_STATUS_FILE:-${RUNNER_TEMP}/caas-agent-watcher.status}"
WATCH_TIMEOUT_SECONDS="${CAAS_AGENT_WATCHER_TIMEOUT_SECONDS:-0}"
POLL_SECONDS="${CAAS_AGENT_WATCHER_POLL_SECONDS:-5}"
SETUP_ENABLED="${CAAS_AGENT_WATCHER_SETUP_ENABLED:-true}"
LOCAL_DNS_ENABLED="${CAAS_AGENT_WATCHER_LOCAL_DNS_ENABLED:-false}"
LIBVIRT_NETWORK="${CAAS_AGENT_WATCHER_LIBVIRT_NETWORK:-test-infra-net-${CLONE_NAME:-sno}}"
LOCAL_DNS_HOSTS_FILE="${CAAS_AGENT_WATCHER_LOCAL_DNS_HOSTS_FILE:-/var/lib/libvirt/dnsmasq/${LIBVIRT_NETWORK}.addnhosts}"
LOCAL_DNS_PID_FILE="/var/run/libvirt/network/${LIBVIRT_NETWORK}.pid"

mkdir -p "${LOG_DIR}"
rm -f "${STATUS_FILE}"
printf 'running\n' > "${STATUS_FILE}"

declare -A seen_namespaces=()
declare -A setup_pids=()
declare -A setup_child_pid_files=()
declare -A setup_order_names=()
declare -A setup_status_files=()
declare -A setup_log_files=()
declare -A cancelled_setups=()
declare -A boot_order_patched=()
declare -A local_dns_lines=()
setup_failed=0
start_time=$(date +%s)

namespace_id() {
    printf '%s' "$1" | sha256sum | cut -c1-12
}

signal_setup() {
    local namespace="$1" child_pid_file child_pid wrapper_pid
    child_pid_file="${setup_child_pid_files[${namespace}]}"
    wrapper_pid="${setup_pids[${namespace}]}"

    if [[ -f "${child_pid_file}" ]]; then
        child_pid=$(cat "${child_pid_file}")
        if [[ "${child_pid}" =~ ^[0-9]+$ ]]; then
            # The setup process runs in its own session when setsid is
            # available, so this also stops a foreground oc/curl/sleep.
            kill -TERM -- "-${child_pid}" 2>/dev/null || kill -TERM "${child_pid}" 2>/dev/null || true
            return
        fi
    fi
    kill -TERM "${wrapper_pid}" 2>/dev/null || true
}

start_setup() {
    local namespace="$1" order_name="$2" id status_file log_file child_pid_file
    id=$(namespace_id "${namespace}")
    status_file="${LOG_DIR}/${id}.status"
    log_file="${LOG_DIR}/${id}.log"
    child_pid_file="${status_file}.pid"
    rm -f "${status_file}" "${status_file}.tmp"
    rm -f "${child_pid_file}" "${child_pid_file}.tmp"

    echo "==> Starting agent setup for ClusterOrder ${order_name} in namespace ${namespace}"
    (
        set +e
        if command -v setsid >/dev/null 2>&1; then
            setsid env \
                CAAS_AGENT_FLOW=cluster-namespace \
                CAAS_CLUSTER_ORDER_NAMESPACE="${namespace}" \
                CAAS_CLUSTER_ORDER_NAME="${order_name}" \
                bash "${SCRIPT_DIR}/setup-caas-agents.sh" &
        else
            env \
                CAAS_AGENT_FLOW=cluster-namespace \
                CAAS_CLUSTER_ORDER_NAMESPACE="${namespace}" \
                CAAS_CLUSTER_ORDER_NAME="${order_name}" \
                bash "${SCRIPT_DIR}/setup-caas-agents.sh" &
        fi
        child_pid=$!
        printf '%s\n' "${child_pid}" > "${child_pid_file}.tmp"
        mv "${child_pid_file}.tmp" "${child_pid_file}"
        wait "${child_pid}"
        rc=$?
        printf '%s\n' "${rc}" > "${status_file}.tmp"
        mv "${status_file}.tmp" "${status_file}"
        exit "${rc}"
    ) > "${log_file}" 2>&1 &

    setup_pids["${namespace}"]=$!
    setup_child_pid_files["${namespace}"]="${child_pid_file}"
    setup_order_names["${namespace}"]="${order_name}"
    setup_status_files["${namespace}"]="${status_file}"
    setup_log_files["${namespace}"]="${log_file}"
}

reap_finished_setups() {
    local namespace pid status_file rc
    for namespace in "${!setup_pids[@]}"; do
        pid="${setup_pids[${namespace}]}"
        status_file="${setup_status_files[${namespace}]}"
        [[ -f "${status_file}" ]] || continue

        rc=0
        wait "${pid}" || rc=$?
        if [[ "${rc}" -ne 0 ]]; then
            if [[ -n "${cancelled_setups[${namespace}]:-}" ]]; then
                echo "==> Agent setup stopped for deleting ClusterOrder namespace ${namespace}"
            else
                echo "::error::Agent setup failed for namespace ${namespace} (exit ${rc}); log: ${setup_log_files[${namespace}]}"
                setup_failed=1
            fi
        elif [[ -n "${cancelled_setups[${namespace}]:-}" ]]; then
            echo "==> Agent setup stopped for deleting ClusterOrder namespace ${namespace}"
        else
            echo "==> Agent setup completed for namespace ${namespace}"
        fi
        unset "setup_pids[${namespace}]" "setup_child_pid_files[${namespace}]" \
            "setup_order_names[${namespace}]" "setup_status_files[${namespace}]" \
            "setup_log_files[${namespace}]" "cancelled_setups[${namespace}]"
    done
}

cancel_deleting_setups() {
    local namespace order_name order_json deletion_timestamp state namespace_json namespace_deletion
    for namespace in "${!setup_pids[@]}"; do
        [[ -f "${setup_status_files[${namespace}]}" ]] && continue
        order_name="${setup_order_names[${namespace}]}"

        if ! order_json=$(oc get clusterorder "${order_name}" -n "${OSAC_NAMESPACE}" \
            --ignore-not-found -o json 2>/dev/null); then
            continue
        fi
        if [[ -z "${order_json}" ]]; then
            echo "==> ClusterOrder ${order_name} is gone; stopping agent setup in ${namespace}"
            cancelled_setups["${namespace}"]=1
            signal_setup "${namespace}"
            continue
        fi
        deletion_timestamp=$(jq -r '.metadata.deletionTimestamp // ""' <<< "${order_json}")
        state=$(jq -r '.status.state // .status.phase // ""' <<< "${order_json}")
        if [[ -n "${deletion_timestamp}" || "${state}" =~ [Dd][Ee][Ll][Ee][Tt] ]]; then
            echo "==> ClusterOrder ${order_name} is deleting; stopping agent setup in ${namespace}"
            cancelled_setups["${namespace}"]=1
            signal_setup "${namespace}"
            continue
        fi

        if ! namespace_json=$(oc get namespace "${namespace}" --ignore-not-found -o json 2>/dev/null); then
            continue
        fi
        if [[ -z "${namespace_json}" ]]; then
            echo "==> ClusterOrder namespace ${namespace} is gone; stopping agent setup"
            cancelled_setups["${namespace}"]=1
            signal_setup "${namespace}"
            continue
        fi
        namespace_deletion=$(jq -r '.metadata.deletionTimestamp // ""' <<< "${namespace_json}")
        if [[ -n "${namespace_deletion}" ]]; then
            echo "==> ClusterOrder namespace ${namespace} is terminating; stopping agent setup"
            cancelled_setups["${namespace}"]=1
            signal_setup "${namespace}"
        fi
    done
}

repair_virtual_bmh_reboot() {
    local namespace="$1"
    local agents_json agent_rows bmh_json secret_json
    local agent_name mac order_name repair_key bmh_row
    local bmh_name bmh_namespace bmc_address secret_name redfish_url
    local bmc_username bmc_password

    if ! agents_json=$(oc get agent -n "${namespace}" -o json 2>/dev/null); then
        return 0
    fi
    [[ -n "${agents_json}" ]] || return 0

    agent_rows=$(jq -r '
        .items[]
        | select(
            .status.debugInfo.state == "installing-in-progress"
            and ((.status.debugInfo.stateInfo // "") | startswith("Rebooting"))
          )
        | .metadata.name as $name
        | (.metadata.labels["osac.openshift.io/clusterorder"] // "") as $order
        | (.metadata.annotations["agent.agent-install.openshift.io/inventory"] // ""
            | fromjson? // empty
            | .interfaces[0].mac_address // empty) as $mac
        | [$name, $mac, $order]
        | @tsv
    ' <<< "${agents_json}")

    while IFS=$'\t' read -r agent_name mac order_name; do
        [[ -n "${agent_name}" && -n "${mac}" ]] || continue
        repair_key="${namespace}/${order_name:-none}/${agent_name}"

        if ! bmh_json=$(oc get bmh -A -o json 2>/dev/null); then
            continue
        fi
        [[ -n "${bmh_json}" ]] || continue

        bmh_row=$(jq -r --arg mac "${mac}" '
            .items[]
            | select(.spec.bootMACAddress == $mac)
            | [.metadata.name, .metadata.namespace, .spec.bmc.address, .spec.bmc.credentialsName]
            | @tsv
        ' <<< "${bmh_json}")
        [[ -n "${bmh_row}" ]] || continue
        IFS=$'\t' read -r bmh_name bmh_namespace bmc_address secret_name <<< "${bmh_row}"

        # The cluster-tool sushy emulator applies Redfish boot overrides as
        # persistent libvirt boot order. Patch it before the Agent reboots;
        # forcing an extra reset can race with the Agent's own reboot.
        redfish_url="${bmc_address#redfish-virtualmedia+}"
        [[ "${redfish_url}" == http://* ]] || continue

        if ! secret_json=$(oc get secret "${secret_name}" -n "${bmh_namespace}" -o json 2>/dev/null); then
            continue
        fi
        bmc_username=$(jq -r '.data.username // ""' <<< "${secret_json}" | base64 -d)
        bmc_password=$(jq -r '.data.password // ""' <<< "${secret_json}" | base64 -d)
        [[ -n "${bmc_username}" && -n "${bmc_password}" ]] || continue

        if [[ -z "${boot_order_patched[${repair_key}]:-}" ]]; then
            # Write credentials to a temporary netrc file so they do not
            # appear in the process argument list (visible in /proc and ps).
            local netrc_file
            netrc_file=$(mktemp "${RUNNER_TEMP}/bmc-netrc.XXXXXX")
            chmod 600 "${netrc_file}"
            local bmc_host
            bmc_host=$(printf '%s\n' "${redfish_url}" | sed -E 's|^https?://||; s|[:/].*||')
            printf 'machine %s login %s password %s\n' \
                "${bmc_host}" "${bmc_username}" "${bmc_password}" > "${netrc_file}"
            # Suppress curl diagnostics so a failure cannot print the
            # raw redfish_url or internal BMC endpoint to the watcher
            # log, which is published in public CI output.  Use --silent
            # without verbose error flags and redirect stderr to /dev/null.
            if curl --noproxy '*' --silent --fail-with-body \
                --connect-timeout 5 --max-time 15 \
                --netrc-file "${netrc_file}" \
                -H 'Content-Type: application/json' \
                -X PATCH "${redfish_url}" \
                --data '{"Boot":{"BootSourceOverrideTarget":"Hdd"}}' >/dev/null 2>/dev/null; then
                boot_order_patched["${repair_key}"]=1
                echo "==> Set virtual BMH ${bmh_name} next boot to disk for Agent ${agent_name} reboot"
            else
                echo "WARNING: Failed to set virtual BMH ${bmh_name} next boot to disk (Redfish PATCH failed)"
                rm -f "${netrc_file}"
                continue
            fi
            rm -f "${netrc_file}"
        fi
    done <<< "${agent_rows}"
}

reload_local_dns() {
    local pid
    if [[ -f "${LOCAL_DNS_PID_FILE}" ]]; then
        pid=$(head -n 1 "${LOCAL_DNS_PID_FILE}")
        if [[ "${pid}" =~ ^[0-9]+$ ]] && kill -0 "${pid}" 2>/dev/null; then
            kill -HUP "${pid}" 2>/dev/null || true
        fi
    fi
}

ensure_local_dns() {
    local namespace="$1"
    local order_name="$2"
    local hosted_json service_json agents_json
    local api_host api_int_host vip api_line
    local base_domain agent_namespace worker_ip apps_host canary_host apps_line
    local dns_changed=false

    if ! hosted_json=$(oc get hostedcluster "${order_name}" -n "${namespace}" \
        --ignore-not-found -o json 2>/dev/null); then
        return 0
    fi
    api_host=$(jq -r '.status.controlPlaneEndpoint.host // ""' <<< "${hosted_json}")
    [[ -n "${api_host}" ]] || return 0
    api_int_host="${api_host/#api./api-int.}"
    base_domain=$(jq -r '.spec.dns.baseDomain // ""' <<< "${hosted_json}")
    agent_namespace=$(jq -r '.spec.platform.agent.agentNamespace // ""' <<< "${hosted_json}")
    agent_namespace="${agent_namespace:-${OSAC_NAMESPACE}}"
    [[ -n "${base_domain}" ]] || return 0

    if ! service_json=$(oc get service kube-apiserver -n "${namespace}-${order_name}" \
        --ignore-not-found -o json 2>/dev/null); then
        return 0
    fi
    vip=$(jq -r '.status.loadBalancer.ingress[0].ip // ""' <<< "${service_json}")
    [[ -n "${vip}" ]] || return 0

    api_line="${vip}	${api_host}	${api_int_host}"
    local_dns_lines["${namespace}:api"]="${api_line}"
    if ! grep -qxF "${api_line}" "${LOCAL_DNS_HOSTS_FILE}" 2>/dev/null; then
        touch "${LOCAL_DNS_HOSTS_FILE}"
        printf '%s\n' "${api_line}" >> "${LOCAL_DNS_HOSTS_FILE}"
        dns_changed=true
        echo "==> Added local DNS entry for API endpoint (namespace ${namespace})"
    fi

    if ! agents_json=$(oc get agent -n "${agent_namespace}" \
        -l "osac.openshift.io/clusterorder=${order_name}" -o json 2>/dev/null); then
        agents_json=""
    fi
    worker_ip=$(jq -r '
        [.items[]?.status.inventory.interfaces[]?.ipV4Addresses[]? |
            split("/")[0] |
            select(. != "")][0] // ""
    ' <<< "${agents_json}" 2>/dev/null || true)
    if [[ -n "${worker_ip}" ]]; then
        apps_host="console-openshift-console.apps.${order_name}.${base_domain}"
        canary_host="canary-openshift-ingress-canary.apps.${order_name}.${base_domain}"
        apps_line="${worker_ip}	${apps_host}	${canary_host}"
        local_dns_lines["${namespace}:apps"]="${apps_line}"
        if ! grep -qxF "${apps_line}" "${LOCAL_DNS_HOSTS_FILE}" 2>/dev/null; then
            printf '%s\n' "${apps_line}" >> "${LOCAL_DNS_HOSTS_FILE}"
            dns_changed=true
            echo "==> Added local DNS entry for apps endpoint (namespace ${namespace})"
        fi
    fi

    if [[ "${dns_changed}" == "true" ]]; then
        reload_local_dns
    fi
}

remove_local_dns() {
    local key namespace line temporary_file
    for key in "${!local_dns_lines[@]}"; do
        line="${local_dns_lines[${key}]}"
        [[ -n "${line}" ]] || continue
        if [[ -f "${LOCAL_DNS_HOSTS_FILE}" ]] && grep -qxF "${line}" "${LOCAL_DNS_HOSTS_FILE}"; then
            temporary_file="${LOCAL_DNS_HOSTS_FILE}.tmp"
            grep -vxF "${line}" "${LOCAL_DNS_HOSTS_FILE}" > "${temporary_file}"
            mv "${temporary_file}" "${LOCAL_DNS_HOSTS_FILE}"
            reload_local_dns
            namespace="${key%:*}"
            echo "==> Removed local DNS entry for namespace ${namespace}"
        fi
        unset "local_dns_lines[${key}]"
    done
}

finish_watcher() {
    local namespace pid status_file rc
    trap - TERM INT
    echo "==> Stopping CaaS agent namespace watcher"

    for namespace in "${!setup_pids[@]}"; do
        pid="${setup_pids[${namespace}]}"
        status_file="${setup_status_files[${namespace}]}"
        if [[ ! -f "${status_file}" ]]; then
            echo "==> Stopping unfinished agent setup for namespace ${namespace}"
            # Signal the setup to stop, but do NOT mark it as cancelled.
            # Only cancel_deleting_setups marks cancelled_setups (when the
            # ClusterOrder or namespace is gone/deleting).  An unfinished
            # setup stopped by normal workflow shutdown should propagate
            # failure so incomplete provisioning is not hidden.
            signal_setup "${namespace}"
        fi
    done

    for namespace in "${!setup_pids[@]}"; do
        pid="${setup_pids[${namespace}]}"
        status_file="${setup_status_files[${namespace}]}"
        rc=0
        wait "${pid}" || rc=$?
        if [[ -f "${status_file}" ]]; then
            rc=$(cat "${status_file}")
        fi
        if [[ "${rc}" -ne 0 && -z "${cancelled_setups[${namespace}]:-}" ]]; then
            echo "::error::Agent setup did not complete successfully for namespace ${namespace} (exit ${rc})"
            setup_failed=1
        fi
    done

    if [[ "${LOCAL_DNS_ENABLED}" == "true" ]]; then
        remove_local_dns
    fi

    if [[ "${setup_failed}" -eq 0 ]]; then
        printf 'complete\n' > "${STATUS_FILE}"
        echo "==> Agent setup results: complete"
        exit 0
    fi
    printf 'failed\n' > "${STATUS_FILE}"
    echo "==> Agent setup results: failed"
    exit 1
}

trap finish_watcher TERM INT

echo "==> Agent watcher started, polling for active ClusterOrder namespaces..."
while true; do
    reap_finished_setups
    cancel_deleting_setups

    namespace_json=$(oc get namespaces -l 'osac.openshift.io/clusterorder' -o json 2>/dev/null) || {
        sleep "${POLL_SECONDS}"
        continue
    }

    namespace_rows=$(jq -r '.items[] | [.metadata.name, .metadata.labels["osac.openshift.io/clusterorder"] // ""] | @tsv' <<< "${namespace_json}")
    while IFS=$'\t' read -r namespace order_name; do
        [[ -n "${namespace}" ]] || continue
        [[ -n "${seen_namespaces[${namespace}]:-}" ]] && continue
        [[ -n "${order_name}" ]] || continue

        # Do not start VMs for a ClusterOrder that was deleted before the
        # watcher got to it (for example, the delete-before-provision test).
        # Use --ignore-not-found so a transient API error (503, timeout,
        # token hiccup) retries on the next poll instead of permanently
        # marking the namespace as seen.
        if ! order_json=$(oc get clusterorder "${order_name}" -n "${OSAC_NAMESPACE}" \
            --ignore-not-found -o json 2>/dev/null); then
            # Transient API error — retry on the next poll.
            continue
        fi
        if [[ -z "${order_json}" ]]; then
            echo "==> ClusterOrder ${order_name} is gone; skipping namespace ${namespace}"
            seen_namespaces["${namespace}"]=1
            continue
        fi
        deleting=$(jq -r '.metadata.deletionTimestamp // ""' <<< "${order_json}")
        state=$(jq -r '.status.state // .status.phase // ""' <<< "${order_json}")
        if [[ -n "${deleting}" || "${state}" =~ [Dd][Ee][Ll][Ee][Tt] ]]; then
            echo "==> ClusterOrder ${order_name} is deleting; skipping namespace ${namespace}"
            seen_namespaces["${namespace}"]=1
            continue
        fi

        seen_namespaces["${namespace}"]=1
        if [[ "${SETUP_ENABLED}" == "true" ]]; then
            start_setup "${namespace}" "${order_name}"
        else
            echo "==> Agent setup disabled; watching virtual BMH reboots in namespace ${namespace}"
        fi
    done <<< "${namespace_rows}"

    if [[ "${LOCAL_DNS_ENABLED}" == "true" ]]; then
        while IFS=$'\t' read -r namespace order_name; do
            [[ -n "${namespace}" && -n "${order_name}" ]] || continue
            ensure_local_dns "${namespace}" "${order_name}"
        done <<< "${namespace_rows}"
    fi

    repair_virtual_bmh_reboot "${OSAC_NAMESPACE}"
    for namespace in "${!seen_namespaces[@]}"; do
        [[ "${namespace}" == "${OSAC_NAMESPACE}" ]] || repair_virtual_bmh_reboot "${namespace}"
    done

    now=$(date +%s)
    elapsed=$((now - start_time))
    if [[ "${WATCH_TIMEOUT_SECONDS}" -gt 0 && "${elapsed}" -ge "${WATCH_TIMEOUT_SECONDS}" ]]; then
        echo "WARNING: Agent watcher timed out after ${elapsed}s"
        finish_watcher
    fi
    sleep "${POLL_SECONDS}"
done
