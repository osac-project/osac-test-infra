#!/usr/bin/env bash
# trust-relay-certs.sh -- Fetch and trust the self-signed TLS certs served
# by a relay's tunneled services (e.g. Grafana, Vault -- see
# monitoring/vpn-relay-access.md), so you don't see "untrusted certificate"
# warnings in the browser.
#
# Run this on YOUR OWN machine (not on the relay or central host), once
# per relay you access -- no SSH access to any server needed, no git clone
# needed. Share the one-liner below with teammates instead of manually
# copying .crt files around:
#
#   curl -sL https://raw.githubusercontent.com/osac-project/osac-test-infra/main/monitoring/scripts/trust-relay-certs.sh \
#     | bash -s -- <relay-host> [port ...]
#
# (raw.githubusercontent.com serves this over a normal, publicly-trusted
# cert -- no bootstrapping-trust problem piping it into bash. This script
# itself only ever fetches from <relay-host>, nothing else remote.)
#
# Or, if you already have this repo cloned:
#   ./trust-relay-certs.sh <relay-host> [port ...]
#
# Defaults to ports 3000 (Grafana) and 8210 (Vault) if none are given.
#
# What this does, per port:
#   1. Connects to <relay-host>:<port> and pulls the certificate it's
#      currently serving (no SSH/file access to any server needed --
#      this is the same cert your browser would see).
#   2. Prints its subject and SHA-256 fingerprint so you can sanity-check
#      it out-of-band (e.g. against a fingerprint posted in Slack) before
#      trusting it -- this script has no way to verify on its own that
#      you're actually talking to the real relay and not something else
#      on the network calling itself by the same name.
#   3. Installs it as a trusted CA in your OS's system trust store.
#
# Firefox keeps its own certificate store, separate from the system one --
# this script does NOT cover it. Import manually: about:preferences#privacy
# -> View Certificates -> Authorities -> Import.
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <relay-host> [port ...]" >&2
    exit 1
fi

RELAY_HOST="$1"
shift
PORTS=("$@")
if [[ ${#PORTS[@]} -eq 0 ]]; then
    PORTS=(3000 8210)
fi

if ! command -v openssl &>/dev/null; then
    echo "ERROR: openssl is required but not found." >&2
    exit 1
fi

OS="$(uname)"
TRUST_DIR=""
UPDATE_CMD=""
if [[ "${OS}" == "Darwin" ]]; then
    : # handled per-cert below via 'security'
elif [[ -d /etc/pki/ca-trust/source/anchors ]]; then
    TRUST_DIR="/etc/pki/ca-trust/source/anchors"
    UPDATE_CMD="update-ca-trust"
elif [[ -d /usr/local/share/ca-certificates ]]; then
    TRUST_DIR="/usr/local/share/ca-certificates"
    UPDATE_CMD="update-ca-certificates"
else
    echo "ERROR: don't know how to install trust anchors on this OS (${OS})." >&2
    echo "Fetched certs will still be saved locally so you can add them manually." >&2
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

INSTALLED=0
for port in "${PORTS[@]}"; do
    echo "=== ${RELAY_HOST}:${port} ==="
    CERT_FILE="${TMP_DIR}/relay-${port}.crt"

    if ! echo | openssl s_client -connect "${RELAY_HOST}:${port}" -servername "${RELAY_HOST}" \
            2>/dev/null | openssl x509 -outform PEM > "${CERT_FILE}" 2>/dev/null \
       || [[ ! -s "${CERT_FILE}" ]]; then
        echo "  Could not fetch a certificate here -- skipping (not every port serves TLS)." >&2
        continue
    fi

    echo "  Subject:    $(openssl x509 -in "${CERT_FILE}" -noout -subject | sed 's/^subject=//')"
    echo "  SHA-256 fingerprint: $(openssl x509 -in "${CERT_FILE}" -noout -fingerprint -sha256 | sed 's/^.*=//')"
    echo "  ^ Confirm this matches what was shared with you out-of-band before trusting it."
    echo

    if [[ "${OS}" == "Darwin" ]]; then
        sudo security add-trusted-cert -d -r trustAsRoot -k /Library/Keychains/System.keychain "${CERT_FILE}"
        echo "  Installed into the macOS System keychain."
    elif [[ -n "${TRUST_DIR}" ]]; then
        DEST="${TRUST_DIR}/relay-${RELAY_HOST//[^a-zA-Z0-9_.-]/_}-${port}.crt"
        sudo cp "${CERT_FILE}" "${DEST}"
        echo "  Installed to ${DEST}"
    else
        SAVE_PATH="${PWD}/relay-${RELAY_HOST}-${port}.crt"
        cp "${CERT_FILE}" "${SAVE_PATH}"
        echo "  Saved to ${SAVE_PATH} -- add it to your trust store manually."
        continue
    fi
    INSTALLED=$((INSTALLED + 1))
done

if [[ -n "${UPDATE_CMD}" ]] && [[ "${INSTALLED}" -gt 0 ]]; then
    echo "=== Updating system trust store ==="
    sudo "${UPDATE_CMD}"
fi

echo ""
echo "Done. Restart your browser for the change to take effect."
echo "Remember: Firefox needs each cert imported separately (see the top of"
echo "this script), since it doesn't use the system trust store."
