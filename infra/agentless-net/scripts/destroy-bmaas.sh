#!/usr/bin/env bash
#
# Remove the BMI and the virtual BMH created by this run.
# Host bridges and the containerlab topology belong to deploy-fabric.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${INFRA_DIR}/../.." && pwd)"
STATE_FILE="${INFRA_DIR}/.bmaas-proof.json"

if [[ -f "${INFRA_DIR}/.mgmt-network" ]]; then
    # shellcheck source=use-kubeconfig.sh
    source "${SCRIPT_DIR}/use-kubeconfig.sh"
    use_kubeconfig "${INFRA_DIR}/.mgmt-network"
fi

OSAC_NAMESPACE="${OSAC_NAMESPACE:-osac-e2e-ci}"
BMH_NAMESPACE="${BMH_NAMESPACE:-host-inventory}"
CLONE_NAME="${MGMT_CLONE_NAME:-agentless-lab-mgmt}"
INVENTORY_FILE="${HOME}/virtual-bmh-inventory-${CLONE_NAME}.json"

info() { echo "==> $*"; }

# A delete with --wait=false leaves the object Terminating. Ironic cannot
# drop its finalizer once the backing VM is gone, so clear it explicitly.
# NotFound means the host is already gone.
strip_bmh_finalizers() {
    local name="$1"
    local status=0
    local output
    output="$(oc -n "$BMH_NAMESPACE" patch baremetalhost "$name" --type merge -p '{"metadata":{"finalizers":null}}' 2>&1)" || status=$?
    if [[ "$status" -eq 0 ]] || grep -q 'NotFound' <<<"$output"; then
        return 0
    fi
    echo "ERROR: failed to strip finalizers from BareMetalHost ${name}" >&2
    printf '%s\n' "$output" >&2
    cleanup_failed=1
}

# Give the baremetal-operator time to deprovision. Strip finalizers only
# when the host is still present after that wait.
delete_bmh() {
    local name="$1"
    if ! oc -n "$BMH_NAMESPACE" delete baremetalhost "$name" --ignore-not-found --wait=false; then
        echo "ERROR: failed to delete BareMetalHost ${name}; keeping its finalizers" >&2
        cleanup_failed=1
        return
    fi
    if oc -n "$BMH_NAMESPACE" wait --for=delete "baremetalhost/${name}" --timeout=120s >/dev/null 2>&1; then
        return
    fi
    echo "BareMetalHost ${name} was still present after 120s; stripping finalizers" >&2
    strip_bmh_finalizers "$name"
}

cleanup_failed=0
if [[ -f "$STATE_FILE" ]]; then
    info "Deleting the proof BMI and its network"
    if ! python3 "${SCRIPT_DIR}/prove_bmi.py" --delete --namespace "$OSAC_NAMESPACE" --state-file "$STATE_FILE"; then
        echo "ERROR: proof resource deletion failed; ${STATE_FILE} was kept for a retry" >&2
        cleanup_failed=1
    fi
fi

INVENTORY_NAMES=()
inventory_unreadable=0
if [[ -f "$INVENTORY_FILE" ]]; then
    if ! inventory_text=$(python3 -c 'import json,sys; print("\n".join(h["name"] for h in json.load(open(sys.argv[1])).get("hosts", [])))' "$INVENTORY_FILE"); then
        echo "ERROR: failed to read virtual BMH inventory ${INVENTORY_FILE}" >&2
        cleanup_failed=1
        inventory_unreadable=1
    elif [[ -n "$inventory_text" ]]; then
        mapfile -t INVENTORY_NAMES <<< "$inventory_text"
    fi
fi
if [[ "$inventory_unreadable" -eq 0 && ${#INVENTORY_NAMES[@]} -gt 0 ]]; then
    for name in "${INVENTORY_NAMES[@]}"; do
        info "Deleting BareMetalHost ${name}"
        delete_bmh "$name"
    done
    info "Deleting the virtual BMH VM recorded for ${CLONE_NAME}"
    if ! python3 "${REPO_ROOT}/.github/scripts/manage-virtual-bmh-vms.py" delete \
        --inventory-file "$INVENTORY_FILE" "${INVENTORY_NAMES[@]}"; then
        echo "ERROR: virtual BMH VM deletion failed" >&2
        cleanup_failed=1
    fi
fi
if [[ -n "${BMH_NAME:-}" ]]; then
    info "Deleting BareMetalHost ${BMH_NAME}"
    delete_bmh "$BMH_NAME"
fi

env_file="${INFRA_DIR}/.env.infra"
if [[ "$cleanup_failed" -eq 0 && -f "$env_file" ]]; then
    env_tmp="$(mktemp)"
    grep_status=0
    grep -v -E '^(OSAC_BMI_NETWORK_INTERFACE|OSAC_BMI_INSTANCE_TYPE|OSAC_BMH_SSH_HOSTS|OSAC_BMI_USER_DATA_FILE)=' "$env_file" >"$env_tmp" || grep_status=$?
    if [[ "$grep_status" -gt 1 ]]; then
        echo "ERROR: failed to update ${env_file}" >&2
        rm -f "$env_tmp"
        cleanup_failed=1
    else
        mv "$env_tmp" "$env_file"
    fi
fi
if [[ "$cleanup_failed" -ne 0 ]]; then
    exit 1
fi
info "destroy-bmaas complete"
