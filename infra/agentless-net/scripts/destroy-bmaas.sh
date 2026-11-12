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
    # shellcheck source=/dev/null
    source "${INFRA_DIR}/.mgmt-network"
    export KUBECONFIG
fi

OSAC_NAMESPACE="${OSAC_NAMESPACE:-osac-e2e-ci}"
BMH_NAMESPACE="${BMH_NAMESPACE:-host-inventory}"
CLONE_NAME="${MGMT_CLONE_NAME:-agentless-lab-mgmt}"
INVENTORY_FILE="${HOME}/virtual-bmh-inventory-${CLONE_NAME}.json"

info() { echo "==> $*"; }

if [[ -f "$STATE_FILE" ]]; then
    info "Deleting the proof BMI and its network"
    python3 "${SCRIPT_DIR}/prove_bmi.py" --delete --namespace "$OSAC_NAMESPACE" --state-file "$STATE_FILE" || true
fi

INVENTORY_NAMES=()
if [[ -f "$INVENTORY_FILE" ]]; then
    mapfile -t INVENTORY_NAMES < <(python3 -c 'import json,sys; print("\n".join(h["name"] for h in json.load(open(sys.argv[1])).get("hosts", [])))' "$INVENTORY_FILE")
fi
if [[ ${#INVENTORY_NAMES[@]} -gt 0 ]]; then
    for name in "${INVENTORY_NAMES[@]}"; do
        info "Deleting BareMetalHost ${name}"
        oc -n "$BMH_NAMESPACE" delete baremetalhost "$name" --ignore-not-found --wait=false || true
    done
    info "Deleting the virtual BMH VM recorded for ${CLONE_NAME}"
    python3 "${REPO_ROOT}/.github/scripts/manage-virtual-bmh-vms.py" delete \
        --inventory-file "$INVENTORY_FILE" --names "${INVENTORY_NAMES[@]}" || true
fi
if [[ -n "${BMH_NAME:-}" ]]; then
    info "Deleting BareMetalHost ${BMH_NAME}"
    oc -n "$BMH_NAMESPACE" delete baremetalhost "$BMH_NAME" --ignore-not-found --wait=false || true
fi

info "destroy-bmaas complete"
