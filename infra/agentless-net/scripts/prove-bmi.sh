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
    version=1.9.1
    work="$(mktemp -d)"
    trap 'rm -rf "$work"' EXIT
    tarball="grpcurl_${version}_linux_x86_64.tar.gz"
    curl --retry 5 --retry-delay 2 -fsSL -o "${work}/${tarball}" \
        "https://github.com/fullstorydev/grpcurl/releases/download/v${version}/${tarball}"
    curl --retry 5 --retry-delay 2 -fsSL \
        "https://github.com/fullstorydev/grpcurl/releases/download/v${version}/grpcurl_${version}_checksums.txt" \
        | grep " ${tarball}\$" \
        | sed "s|${tarball}|${work}/${tarball}|" \
        | sha256sum -c -
    tar -C "$work" -xzf "${work}/${tarball}" grpcurl
    sudo install -m 0755 "${work}/grpcurl" /usr/local/bin/grpcurl
    rm -rf "$work"
    trap - EXIT
fi

exec python3 "${SCRIPT_DIR}/prove_bmi.py" \
    --namespace "$OSAC_NAMESPACE" \
    --tenant "${OSAC_TENANT:-$OSAC_NAMESPACE}" \
    --ssh-public-key-file "$SSH_PUB" \
    --state-file "${INFRA_DIR}/.bmaas-proof.json"
