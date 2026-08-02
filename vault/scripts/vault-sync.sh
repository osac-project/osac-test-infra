#!/usr/bin/env bash
# vault-sync.sh -- Sync all secrets from one OSAC CI Vault to others via SSH.
#
# Each CI machine runs a local Vault on 127.0.0.1:8200.  This script reads
# every secret under secret/osac/ from the source machine and writes them
# to each target machine through SSH tunnels.
#
# Usage:
#   ./vault-sync.sh <source-host> <target-host> [target-host ...]
#
# Environment:
#   SSH_USER          SSH user for remote connections (default: current user)
#   SSH_KEY           Path to SSH private key (optional, uses ssh-agent if unset)
#   VAULT_TOKEN_FILE  Path to .vault-init.json on remote hosts
#                     (default: ~/.vault-server/.vault-init.json)
#   DRY_RUN           Set to "true" to show what would be synced without writing
#
# Examples:
#   ./vault-sync.sh osac-ci-1 osac-8 osac-9 osac-10
#   SSH_USER=runner ./vault-sync.sh osac-ci-1 osac-8
#   DRY_RUN=true ./vault-sync.sh osac-ci-1 osac-8 osac-9 osac-10
set -euo pipefail

SSH_USER="${SSH_USER:-$(whoami)}"
SSH_KEY="${SSH_KEY:-}"
VAULT_TOKEN_FILE="${VAULT_TOKEN_FILE:-\$HOME/.vault-server/.vault-init.json}"
DRY_RUN="${DRY_RUN:-false}"
SECRET_PREFIX="${SECRET_PREFIX:-secret/osac/e2e}"

###############################################################################
# Parse arguments
###############################################################################
if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <source-host> <target-host> [target-host ...]" >&2
    echo "" >&2
    echo "Syncs all Vault secrets under ${SECRET_PREFIX}/ from source to targets." >&2
    exit 1
fi

SOURCE_HOST="$1"
shift
TARGET_HOSTS=("$@")

###############################################################################
# Helpers
###############################################################################
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)
if [[ -n "${SSH_KEY}" ]]; then
    SSH_OPTS+=(-i "${SSH_KEY}")
fi

ssh_cmd() {
    local host="$1"
    shift
    ssh "${SSH_OPTS[@]}" "${SSH_USER}@${host}" "$@"
}

# get_root_token <host> -- extract root token from the remote .vault-init.json
get_root_token() {
    local host="$1"
    ssh_cmd "${host}" "jq -r '.root_token' ${VAULT_TOKEN_FILE}"
}

# vault_via_ssh <host> <token> <vault-args...> -- run vault CLI on remote host
vault_via_ssh() {
    local host="$1"
    local token="$2"
    shift 2
    ssh_cmd "${host}" "VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN=${token} vault $*"
}

# list_secrets <host> <token> <path> -- recursively list all secret paths
list_secrets() {
    local host="$1"
    local token="$2"
    local path="$3"
    local keys

    # vault kv list returns keys; directories end with /
    keys=$(vault_via_ssh "${host}" "${token}" "kv list -format=json ${path}" 2>/dev/null) || return 0

    echo "${keys}" | jq -r '.[]' | while IFS= read -r key; do
        if [[ "${key}" == */ ]]; then
            # Recurse into subdirectory
            list_secrets "${host}" "${token}" "${path}/${key%/}"
        else
            echo "${path}/${key}"
        fi
    done
}

###############################################################################
# Main
###############################################################################
echo "=== Vault Sync ==="
echo "Source:  ${SOURCE_HOST}"
echo "Targets: ${TARGET_HOSTS[*]}"
echo "Prefix:  ${SECRET_PREFIX}"
if [[ "${DRY_RUN}" == "true" ]]; then
    echo "Mode:    DRY RUN (no writes)"
fi
echo ""

# 1. Get source root token and verify connectivity
echo "Connecting to source (${SOURCE_HOST})..."
SOURCE_TOKEN=$(get_root_token "${SOURCE_HOST}")
vault_via_ssh "${SOURCE_HOST}" "${SOURCE_TOKEN}" "status -format=json" | jq -r '"  Vault: \(.version), sealed: \(.sealed)"'

# 2. List all secrets under the prefix
echo ""
echo "Listing secrets under ${SECRET_PREFIX}/..."
SECRET_PATHS=()
while IFS= read -r path; do
    [[ -z "${path}" ]] && continue
    SECRET_PATHS+=("${path}")
done < <(list_secrets "${SOURCE_HOST}" "${SOURCE_TOKEN}" "${SECRET_PREFIX}")

if [[ ${#SECRET_PATHS[@]} -eq 0 ]]; then
    echo "  No secrets found under ${SECRET_PREFIX}/."
    exit 0
fi

echo "  Found ${#SECRET_PATHS[@]} secret(s):"
for p in "${SECRET_PATHS[@]}"; do
    echo "    - ${p}"
done

# 3. Read all secrets from source
echo ""
echo "Reading secrets from source..."
declare -A SECRET_DATA
for path in "${SECRET_PATHS[@]}"; do
    data=$(vault_via_ssh "${SOURCE_HOST}" "${SOURCE_TOKEN}" "kv get -format=json ${path}" | jq -c '.data.data')
    SECRET_DATA["${path}"]="${data}"
    # Show keys (not values) for verification
    keys=$(echo "${data}" | jq -r 'keys | join(", ")')
    echo "  ${path}: [${keys}]"
done

# 4. Connect to each target and write secrets
for target in "${TARGET_HOSTS[@]}"; do
    echo ""
    echo "--- Syncing to ${target} ---"

    TARGET_TOKEN=$(get_root_token "${target}")
    vault_via_ssh "${target}" "${TARGET_TOKEN}" "status -format=json" | jq -r '"  Vault: \(.version), sealed: \(.sealed)"'

    for path in "${SECRET_PATHS[@]}"; do
        data="${SECRET_DATA[${path}]}"
        if [[ "${DRY_RUN}" == "true" ]]; then
            echo "  [DRY RUN] Would write ${path}"
        else
            # vault kv put expects key=value pairs; use @- for JSON input
            echo "${data}" | vault_via_ssh "${target}" "${TARGET_TOKEN}" \
                "kv put ${path} -" 2>/dev/null \
            || {
                # Fallback: construct key=value args from JSON
                kv_args=""
                while IFS= read -r key; do
                    value=$(echo "${data}" | jq -r --arg k "${key}" '.[$k]')
                    kv_args+="${key}=${value} "
                done < <(echo "${data}" | jq -r 'keys[]')
                vault_via_ssh "${target}" "${TARGET_TOKEN}" "kv put ${path} ${kv_args}"
            }
            echo "  Wrote ${path}"
        fi
    done

    echo "  Done."
done

echo ""
echo "=== Sync complete ==="
