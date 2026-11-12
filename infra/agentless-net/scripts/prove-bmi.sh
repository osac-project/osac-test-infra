#!/usr/bin/env bash
#
# Wait until the proof BMI is Ready and has a DHCP address.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=/dev/null
source "${INFRA_DIR}/.mgmt-network"
export KUBECONFIG

OSAC_NAMESPACE="${OSAC_NAMESPACE:-osac-e2e-ci}"
SSH_PUB="${BMI_SSH_PUBLIC_KEY_FILE:-${HOME}/.ssh/id_rsa.pub}"

if ! command -v grpcurl >/dev/null 2>&1; then
    echo "==> Installing grpcurl"
    curl -fsSL -o /tmp/grpcurl.tar.gz \
        https://github.com/fullstorydev/grpcurl/releases/download/v1.9.3/grpcurl_1.9.3_linux_x86_64.tar.gz
    sudo tar -C /usr/local/bin -xzf /tmp/grpcurl.tar.gz grpcurl
fi

exec python3 "${SCRIPT_DIR}/prove_bmi.py" \
    --namespace "$OSAC_NAMESPACE" \
    --tenant "${OSAC_TENANT:-$OSAC_NAMESPACE}" \
    --ssh-public-key-file "$SSH_PUB" \
    --state-file "${INFRA_DIR}/.bmaas-proof.json"
