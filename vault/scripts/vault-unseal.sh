#!/usr/bin/env bash
# vault-unseal.sh -- Unseal Vault using stored keys.
# Called by ExecStartPost in the Quadlet unit and by vault-unseal.service.
set -euo pipefail

VAULT_ADDR="${VAULT_ADDR:-http://127.0.0.1:8200}"
export VAULT_ADDR

UNSEAL_KEYS_FILE="${HOME}/.vault-server/.unseal-keys"
MAX_WAIT=30   # seconds to wait for Vault to become reachable

# Helper: get a field from vault status JSON.
# vault status exits 0 (unsealed), 1 (error), or 2 (sealed/uninitialized),
# so we must ignore its exit code.
vault_status_field() {
    local field="$1"
    (vault status -format=json 2>/dev/null || true) | jq -r ".${field}" 2>/dev/null
}

###############################################################################
# Wait for Vault to be reachable
###############################################################################
echo "[vault-unseal] Waiting for Vault at ${VAULT_ADDR} ..."
elapsed=0
while true; do
    if [[ "$(vault_status_field 'type')" != "" && "$(vault_status_field 'type')" != "null" ]]; then
        break
    fi
    if (( elapsed >= MAX_WAIT )); then
        echo "[vault-unseal] ERROR: Vault not reachable after ${MAX_WAIT}s" >&2
        exit 1
    fi
    sleep 1
    (( elapsed++ ))
done

###############################################################################
# If not yet initialized, exit gracefully (vault-setup.sh will handle init)
###############################################################################
if [[ "$(vault_status_field 'initialized')" != "true" ]]; then
    echo "[vault-unseal] Vault is not yet initialized -- skipping unseal."
    exit 0
fi

###############################################################################
# Check if already unsealed
###############################################################################
if [[ "$(vault_status_field 'sealed')" == "false" ]]; then
    echo "[vault-unseal] Vault is already unsealed."
    exit 0
fi

###############################################################################
# Read unseal keys and unseal
###############################################################################
if [[ ! -f "${UNSEAL_KEYS_FILE}" ]]; then
    echo "[vault-unseal] Unseal keys file not found: ${UNSEAL_KEYS_FILE}" >&2
    echo "[vault-unseal] Vault remains sealed -- unseal manually or run vault-setup.sh." >&2
    exit 1
fi

echo "[vault-unseal] Unsealing Vault ..."
# threshold must match -key-threshold in vault-setup.sh Phase 4
threshold=2
applied=0
while IFS= read -r key; do
    [[ -z "${key}" ]] && continue
    vault operator unseal "${key}" >/dev/null
    (( ++applied ))
    if (( applied >= threshold )); then
        break
    fi
done < "${UNSEAL_KEYS_FILE}"

###############################################################################
# Verify
###############################################################################
if [[ "$(vault_status_field 'sealed')" == "false" ]]; then
    echo "[vault-unseal] Vault unsealed successfully."
else
    echo "[vault-unseal] ERROR: Vault is still sealed after applying ${applied} keys." >&2
    exit 1
fi
