#!/usr/bin/env bash
# vault-add-registry-auth.sh -- Merge registry credentials into the Vault
# pull-secret (dockerconfigjson).
#
# This reads the existing pull-secret from Vault, merges the new auth entry,
# and writes it back.  Run on one machine, then use vault-sync.sh to
# propagate to the rest.
#
# Usage:
#   ./vault-add-registry-auth.sh <auth-json-file>
#   ./vault-add-registry-auth.sh <auth-json-file> --dry-run
#
# The auth JSON file should contain a standard Docker auth config, e.g.:
#   {
#     "auths": {
#       "quay.io": {
#         "auth": "base64-encoded-creds",
#         "email": ""
#       }
#     }
#   }
#
# The script merges the "auths" entries into the existing pull-secret.
# Existing registries are overwritten; other registries are preserved.
set -euo pipefail

VAULT_ADDR="${VAULT_ADDR:-http://127.0.0.1:8200}"
export VAULT_ADDR
SECRET_PATH="${SECRET_PATH:-secret/osac/e2e/pull-secret}"

###############################################################################
# Parse arguments
###############################################################################
if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <auth-json-file> [--dry-run]" >&2
    exit 1
fi

AUTH_FILE="$1"
DRY_RUN=false
if [[ "${2:-}" == "--dry-run" ]]; then
    DRY_RUN=true
fi

if [[ ! -f "${AUTH_FILE}" ]]; then
    echo "ERROR: File not found: ${AUTH_FILE}" >&2
    exit 1
fi

# Validate JSON
if ! jq -e '.auths' "${AUTH_FILE}" >/dev/null 2>&1; then
    echo "ERROR: ${AUTH_FILE} must contain an \"auths\" key." >&2
    exit 1
fi

###############################################################################
# Authenticate
###############################################################################
INIT_JSON="${HOME}/.vault-server/.vault-init.json"
if [[ -f "${INIT_JSON}" ]]; then
    export VAULT_TOKEN
    VAULT_TOKEN=$(jq -r '.root_token' "${INIT_JSON}")
elif [[ -z "${VAULT_TOKEN:-}" ]]; then
    echo "ERROR: No VAULT_TOKEN and ${INIT_JSON} not found." >&2
    exit 1
fi

###############################################################################
# Read current pull-secret
###############################################################################
echo "Reading current pull-secret from ${SECRET_PATH}..."
CURRENT=$(vault kv get -format=json "${SECRET_PATH}" | jq -r '.data.data.dockerconfigjson')

if [[ -z "${CURRENT}" || "${CURRENT}" == "null" ]]; then
    echo "ERROR: No dockerconfigjson field found at ${SECRET_PATH}." >&2
    exit 1
fi

# Parse current dockerconfigjson
CURRENT_AUTHS=$(echo "${CURRENT}" | jq -c '.')

echo "Current registries:"
echo "${CURRENT_AUTHS}" | jq -r '.auths | keys[]' | sed 's/^/  - /'

###############################################################################
# Merge
###############################################################################
NEW_AUTHS=$(jq -c '.auths' "${AUTH_FILE}")
echo ""
echo "Adding registries:"
echo "${NEW_AUTHS}" | jq -r 'keys[]' | sed 's/^/  - /'

MERGED=$(echo "${CURRENT_AUTHS}" | jq -c --argjson new "${NEW_AUTHS}" '.auths += $new')

echo ""
echo "Merged registries:"
echo "${MERGED}" | jq -r '.auths | keys[]' | sed 's/^/  - /'

###############################################################################
# Write back
###############################################################################
if [[ "${DRY_RUN}" == "true" ]]; then
    echo ""
    echo "[DRY RUN] Would write merged pull-secret to ${SECRET_PATH}."
    echo "[DRY RUN] Merged JSON (auths keys only):"
    echo "${MERGED}" | jq '.auths | keys'
else
    echo ""
    echo "Writing merged pull-secret to ${SECRET_PATH}..."
    vault kv put "${SECRET_PATH}" "dockerconfigjson=${MERGED}"
    echo "Done. Run vault-sync.sh to propagate to other machines."
fi
