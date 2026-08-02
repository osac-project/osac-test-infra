#!/usr/bin/env bash
# vault-migrate-secrets.sh -- Copy all KV v2 secrets from one Vault to another.
#
# Runs from a machine that can SSH into both Vault hosts.  The vault
# CLI does NOT need to be installed locally -- all vault commands are
# executed over SSH on the respective remote hosts.
#
# Usage:
#   vault-migrate-secrets.sh <src-ssh> <src-init-json> <dst-ssh> <dst-init-json> [kv-mount]
#
# Example:
#   vault-migrate-secrets.sh \
#       github-runner@osac-ci-1.example.com  ~/vaults/osac-ci-1.vault-init.json \
#       github-runner@osac-10.example.com    ~/vaults/osac-10-vault-init.json
#
# Prerequisites: jq, ssh (vault CLI on remote hosts)
set -euo pipefail

###############################################################################
# Arguments
###############################################################################
if [[ $# -lt 4 ]]; then
    echo "Usage: $0 <src-ssh> <src-init-json> <dst-ssh> <dst-init-json> [kv-mount]" >&2
    echo "" >&2
    echo "  src-ssh         SSH target for source Vault (e.g. user@host)" >&2
    echo "  src-init-json   Local path to source Vault's init JSON" >&2
    echo "  dst-ssh         SSH target for destination Vault (e.g. user@host)" >&2
    echo "  dst-init-json   Local path to destination Vault's init JSON" >&2
    echo "  kv-mount        KV v2 mount path (default: secret)" >&2
    exit 1
fi

SRC_SSH="$1"
SRC_INIT_JSON="$2"
DST_SSH="$3"
DST_INIT_JSON="$4"
KV_MOUNT="${5:-secret}"

###############################################################################
# Helpers
###############################################################################

# Run a vault command on the source host.
src_vault() {
    ssh "$SRC_SSH" "VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN='${SRC_TOKEN}' vault $*"
}

# Run a vault command on the destination host.
dst_vault() {
    ssh "$DST_SSH" "VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN='${DST_TOKEN}' vault $*"
}

# Recursively list all leaf keys under a KV v2 path.
# KV v2 list returns folder names ending in '/' and leaf keys without.
list_recursive() {
    local path="$1"
    local list_output key

    list_output=$(src_vault "kv list -format=json '${KV_MOUNT}/${path}'" 2>/dev/null) || return 0

    while IFS= read -r key; do
        if [[ "$key" == */ ]]; then
            list_recursive "${path}${key}"
        else
            echo "${path}${key}"
        fi
    done < <(echo "$list_output" | jq -r '.[]')
}

###############################################################################
# Main
###############################################################################

# Validate init JSONs exist
for f in "$SRC_INIT_JSON" "$DST_INIT_JSON"; do
    if [[ ! -f "$f" ]]; then
        echo "ERROR: Init JSON not found: $f" >&2
        exit 1
    fi
done

# Extract root tokens
SRC_TOKEN=$(jq -r '.root_token' "$SRC_INIT_JSON")
DST_TOKEN=$(jq -r '.root_token' "$DST_INIT_JSON")

if [[ -z "$SRC_TOKEN" || "$SRC_TOKEN" == "null" ]]; then
    echo "ERROR: Could not extract root_token from $SRC_INIT_JSON" >&2
    exit 1
fi
if [[ -z "$DST_TOKEN" || "$DST_TOKEN" == "null" ]]; then
    echo "ERROR: Could not extract root_token from $DST_INIT_JSON" >&2
    exit 1
fi

# Verify connectivity to both vaults
echo "Verifying source Vault (${SRC_SSH}) ..."
if ! src_vault "status -format=json" >/dev/null 2>&1; then
    echo "ERROR: Cannot reach Vault on ${SRC_SSH}" >&2
    exit 1
fi
echo "  Connected."

echo "Verifying destination Vault (${DST_SSH}) ..."
if ! dst_vault "status -format=json" >/dev/null 2>&1; then
    echo "ERROR: Cannot reach Vault on ${DST_SSH}" >&2
    exit 1
fi
echo "  Connected."

echo ""
echo "Source:      ${SRC_SSH}"
echo "Destination: ${DST_SSH}"
echo "KV mount:    ${KV_MOUNT}/"
echo ""

# Enumerate all keys
echo "Listing secrets from source ..."
keys=$(list_recursive "")

if [[ -z "$keys" ]]; then
    echo "No secrets found under ${KV_MOUNT}/ -- nothing to migrate."
    exit 0
fi

count=$(echo "$keys" | wc -l)
echo "Found ${count} secret(s) to migrate."
echo ""

# Migrate each key
migrated=0
failed=0
while IFS= read -r key <&3; do
    echo -n "  ${KV_MOUNT}/${key} ... "

    # Read secret JSON from source
    secret_json=$(src_vault "kv get -format=json '${KV_MOUNT}/${key}'" 2>/dev/null \
        | jq -c '.data.data') || true

    if [[ -z "$secret_json" || "$secret_json" == "null" ]]; then
        echo "SKIP (empty or unreadable)"
        ((failed++)) || true
        continue
    fi

    # Write secret JSON to destination via stdin
    if echo "$secret_json" | ssh "$DST_SSH" \
        "VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN='${DST_TOKEN}' vault kv put '${KV_MOUNT}/${key}' -" \
        >/dev/null 2>&1; then
        echo "OK"
        ((migrated++)) || true
    else
        echo "FAIL"
        ((failed++)) || true
    fi
done 3<<< "$keys"

echo ""
echo "============================================"
echo "  Migration complete"
echo "  Migrated: ${migrated}  Failed: ${failed}"
echo "============================================"

if (( failed > 0 )); then
    exit 1
fi
