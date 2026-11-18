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
    if [[ "${OSAC_KUBECONFIG_FROM_CALLER:-}" == "yes" && -n "${KUBECONFIG:-}" ]]; then
        caller="$KUBECONFIG"
    fi
    if [[ -n "$recorded" && -f "$recorded" ]]; then
        # shellcheck disable=SC1090
        source "$recorded"
    elif [[ -n "$recorded" && "$mode" == "required" ]]; then
        echo "ERROR: ${recorded} does not exist" >&2
        return 1
    fi
    if [[ -n "$caller" ]]; then
        KUBECONFIG="$caller"
    fi
    KUBECONFIG="${KUBECONFIG:-$HOME/.kube/${MGMT_CLONE_NAME:-agentless-lab-mgmt}.kubeconfig}"
    export KUBECONFIG
    if [[ -n "$recorded" && -f "$recorded" ]]; then
        printf '%s\n' "$KUBECONFIG" >"$(dirname "$recorded")/.effective-kubeconfig"
    fi
}
