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
    # shellcheck source=use-kubeconfig.sh
    source "${SCRIPT_DIR}/use-kubeconfig.sh"
    use_kubeconfig "${INFRA_DIR}/.mgmt-network"
fi

OSAC_NAMESPACE="${OSAC_NAMESPACE:-osac-e2e-ci}"
BMH_NAMESPACE="${BMH_NAMESPACE:-host-inventory}"

capture_failed=0
oc -n "$BMH_NAMESPACE" get baremetalhosts -o yaml >"${LOG_DIR}/baremetalhosts.yaml" 2>&1 || capture_failed=1
oc -n "$OSAC_NAMESPACE" get baremetalinstances -o yaml >"${LOG_DIR}/baremetalinstances.yaml" 2>&1 || capture_failed=1
oc -n "$OSAC_NAMESPACE" get virtualnetworks,subnets -o yaml >"${LOG_DIR}/networks.yaml" 2>&1 || capture_failed=1
sysctl net.bridge.bridge-nf-call-iptables net.bridge.bridge-nf-call-ip6tables >"${LOG_DIR}/bridge-sysctl.txt" 2>&1 || true
for bridge in br-host1 br-host2; do
    ip -d link show "$bridge" >"${LOG_DIR}/${bridge}.txt" 2>&1 || true
done
LAB_PREFIX="${LAB_PREFIX:-clab-agentless-net-lab}"
supervisor_status="$(sudo docker exec "${LAB_PREFIX}-net-node" supervisorctl -c /etc/agentless-net/supervisord.conf status 2>&1 || true)"
printf '%s\n' "$supervisor_status" >"${LOG_DIR}/supervisor-status.txt"
if [[ "$capture_failed" -ne 0 ]]; then
    echo "ERROR: BMaaS object capture failed; not marking the snapshot complete" >&2
    exit 1
fi
touch "${LOG_DIR}/.gather-bmaas-complete"
echo "gather-bmaas complete"
