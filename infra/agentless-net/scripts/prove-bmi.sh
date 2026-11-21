#!/usr/bin/env bash
#
# Wait until the proof BMI is Ready and has a DHCP address.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=use-kubeconfig.sh
source "${SCRIPT_DIR}/use-kubeconfig.sh"
use_kubeconfig "${INFRA_DIR}/.mgmt-network"

OSAC_NAMESPACE="${OSAC_NAMESPACE:-osac-e2e-ci}"

exec python3 "${SCRIPT_DIR}/prove_bmi.py" \
    --namespace "$OSAC_NAMESPACE" \
    --tenant "${OSAC_TENANT:-$OSAC_NAMESPACE}" \
    --state-file "${INFRA_DIR}/.bmaas-proof.json"
