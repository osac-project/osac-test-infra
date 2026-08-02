#!/usr/bin/env bash
# authorize-tunnel-relay.sh -- Authorize a relay machine's SSH key to
# forward specific local ports FROM this (central) host, without
# granting it a shell, command-execution, or any-port-forwarding
# capability.
#
# Run on the CENTRAL host (e.g. the monitoring-central machine), as
# root -- NOT on the relay machine. Pairs with setup-tunnel-relay.sh,
# which is run on the relay.
#
# Usage:
#   authorize-tunnel-relay.sh <user> <public-key> <port> [<port> ...]
#
# Example:
#   ./authorize-tunnel-relay.sh grafana-tunnel "ssh-ed25519 AAAA... osac-tunnel@relay-hostname" 3000 9091 9093
#
# What this does:
#   1. Creates a dedicated, unprivileged, shell-less system user named
#      <user> if it doesn't already exist -- isolated from every other
#      account on this host, in particular from github-runner (which
#      has broad sudo for CI/e2e purposes).
#   2. Installs the given public key into that user's authorized_keys
#      with "restrict,port-forwarding,permitopen=..." -- an SSH-protocol
#      -level restriction, enforced before any shell or sudo is even
#      reachable, that permits ONLY forwarding a connection to
#      127.0.0.1:<port> for each <port> given here. It cannot open a
#      shell, run a command, or forward to any other host/port,
#      regardless of what OS-level permissions the account might
#      otherwise have.
#
# Idempotent: re-running with the same user replaces that user's
# authorized_keys with the given key and port list (single relay key
# per user -- for a second relay, use a different <user>).
set -euo pipefail

if [[ $# -lt 3 ]]; then
    echo "Usage: $0 <user> <public-key> <port> [<port> ...]" >&2
    exit 1
fi

TUNNEL_USER="$1"
PUBKEY="$2"
shift 2
PORTS=("$@")

if [[ ! "${TUNNEL_USER}" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    echo "ERROR: user must be alphanumeric (plus - or _): ${TUNNEL_USER}" >&2
    exit 1
fi
# Reject embedded newlines before the format check below -- a pubkey
# argument containing a newline followed by a second, unrestricted key
# would otherwise get written verbatim into authorized_keys, smuggling
# in an unrestricted second entry alongside the intended restricted one.
if [[ "${PUBKEY}" == *$'\n'* ]]; then
    echo "ERROR: public key must not contain embedded newlines" >&2
    exit 1
fi
if [[ ! "${PUBKEY}" =~ ^ssh-(ed25519|rsa|ecdsa) ]]; then
    echo "ERROR: doesn't look like a public key (expected 'ssh-ed25519 ...' etc.): ${PUBKEY}" >&2
    exit 1
fi
for port in "${PORTS[@]}"; do
    if ! [[ "${port}" =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then
        echo "ERROR: invalid port: ${port}" >&2
        exit 1
    fi
done

TUNNEL_HOME="/home/${TUNNEL_USER}"

echo "=== Creating tunnel-only system user: ${TUNNEL_USER} ==="
if ! id "${TUNNEL_USER}" &>/dev/null; then
    useradd -r -m -d "${TUNNEL_HOME}" -s /usr/sbin/nologin "${TUNNEL_USER}"
else
    echo "  Already exists, skipping."
fi

PERMITOPEN=""
for port in "${PORTS[@]}"; do
    PERMITOPEN="${PERMITOPEN}permitopen=\"127.0.0.1:${port}\","
done

mkdir -p "${TUNNEL_HOME}/.ssh"
chmod 700 "${TUNNEL_HOME}/.ssh"
echo "${PERMITOPEN}restrict,port-forwarding ${PUBKEY}" > "${TUNNEL_HOME}/.ssh/authorized_keys"
chmod 600 "${TUNNEL_HOME}/.ssh/authorized_keys"
chown -R "${TUNNEL_USER}:${TUNNEL_USER}" "${TUNNEL_HOME}/.ssh"

CENTRAL_FQDN="$(hostname -f)"

echo ""
echo "=== Done ==="
echo "Authorized for: ${PORTS[*]} (127.0.0.1 only)"
echo ""
echo "Verify the restriction actually holds before trusting it:"
echo ""
echo "  # From the relay machine, as its tunnel user -- should be REFUSED:"
echo "  ssh -i <relay-keyfile> ${TUNNEL_USER}@${CENTRAL_FQDN} whoami"
echo ""
echo "  # Port forwarding to an authorized port -- should WORK (assuming"
echo "  # something is listening on 127.0.0.1:<port> here):"
echo "  ssh -N -i <relay-keyfile> -L <local-port>:127.0.0.1:<port> ${TUNNEL_USER}@${CENTRAL_FQDN} &"
echo "  curl http://127.0.0.1:<local-port>/..."
echo ""
echo "  # Port forwarding to an UNAUTHORIZED port -- should be REFUSED:"
echo "  ssh -N -i <relay-keyfile> -L <local-port>:127.0.0.1:<some-other-port> ${TUNNEL_USER}@${CENTRAL_FQDN} &"
