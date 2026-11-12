#!/usr/bin/env bash
#
# Collect the virtual BMH and the proof BMI. Secrets are redacted later.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOG_DIR="${INFRA_DIR}/logs/bmaas"
mkdir -p "$LOG_DIR"

if [[ -f "${INFRA_DIR}/.mgmt-network" ]]; then
    # shellcheck source=/dev/null
    source "${INFRA_DIR}/.mgmt-network"
    export KUBECONFIG
fi

OSAC_NAMESPACE="${OSAC_NAMESPACE:-osac-e2e-ci}"
BMH_NAMESPACE="${BMH_NAMESPACE:-host-inventory}"

oc -n "$BMH_NAMESPACE" get baremetalhosts -o yaml >"${LOG_DIR}/baremetalhosts.yaml" 2>&1 || true
oc -n "$OSAC_NAMESPACE" get baremetalinstances -o yaml >"${LOG_DIR}/baremetalinstances.yaml" 2>&1 || true
oc -n "$OSAC_NAMESPACE" get virtualnetworks,subnets -o yaml >"${LOG_DIR}/networks.yaml" 2>&1 || true
sysctl net.bridge.bridge-nf-call-iptables net.bridge.bridge-nf-call-ip6tables >"${LOG_DIR}/bridge-sysctl.txt" 2>&1 || true
ip -d link show br-host1 >"${LOG_DIR}/br-host1.txt" 2>&1 || true
echo "gather-bmaas complete"
