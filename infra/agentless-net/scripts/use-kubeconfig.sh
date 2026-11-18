#!/usr/bin/env bash
# Resolve KUBECONFIG without letting Make's default hide a recorded path.
#
# Order: an explicit command-line or environment value, then .mgmt-network,
# then the management-clone default. Make exports its ?= default even when
# the caller did not choose one, so that default is not an override.

use_kubeconfig() {
    local recorded="${1:-}"
    local mode="${2:-required}"
    local caller=""
    local recorded_value=""
    if [[ "${OSAC_KUBECONFIG_FROM_CALLER:-}" == "yes" && -n "${KUBECONFIG:-}" ]]; then
        caller="$KUBECONFIG"
    fi
    if [[ -n "$recorded" && -f "$recorded" ]]; then
        # shellcheck disable=SC1090
        source "$recorded"
        recorded_value="${KUBECONFIG:-}"
    elif [[ -n "$recorded" && "$mode" == "required" ]]; then
        echo "ERROR: ${recorded} does not exist" >&2
        return 1
    fi
    OSAC_RECORDED_KUBECONFIG="$recorded_value"
    export OSAC_RECORDED_KUBECONFIG
    if [[ -n "$caller" ]]; then
        KUBECONFIG="$caller"
    fi
    KUBECONFIG="${KUBECONFIG:-$HOME/.kube/${MGMT_CLONE_NAME:-agentless-lab-mgmt}.kubeconfig}"
    export KUBECONFIG
}

require_recorded_cluster() {
    local recorded="${OSAC_RECORDED_KUBECONFIG:-}"
    local recorded_server=""
    local current_server=""
    if [[ -z "$recorded" || ! -f "$recorded" ]]; then
        echo "ERROR: refusing to change the cluster; the lab kubeconfig was not recorded" >&2
        return 1
    fi
    recorded_server="$(KUBECONFIG="$recorded" oc config view --minify --raw -o jsonpath='{.clusters[0].cluster.server}')"
    current_server="$(oc config view --minify --raw -o jsonpath='{.clusters[0].cluster.server}')"
    if [[ -z "$recorded_server" || "$recorded_server" != "$current_server" ]]; then
        echo "ERROR: refusing to change the cluster; kubeconfig does not match the recorded lab" >&2
        return 1
    fi
}
