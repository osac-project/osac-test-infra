#!/usr/bin/env bash
# vault-health-check.sh -- Health check for the OSAC Vault instance.
#
# Detects whether this machine runs a central (local) vault or an agent
# (SSH tunnel to central vault) and adjusts checks accordingly.
#
# Detection logic:
#   - vault-tunnel.service active  -> agent mode
#   - vault.service active         -> central mode
#   - neither                      -> unknown (checks what it can)
set -euo pipefail

VAULT_ADDR="${VAULT_ADDR:-http://127.0.0.1:8200}"
export VAULT_ADDR

VAULT_HOME="${HOME}/.vault-server"
APPROLE_DIR="${VAULT_HOME}/.approle"

# Authenticated checks need a token.
# Fallback order: init JSON -> env var -> AppRole login.
INIT_JSON="${VAULT_HOME}/.vault-init.json"
if [[ -f "${INIT_JSON}" ]]; then
    VAULT_TOKEN=$(jq -r '.root_token' "${INIT_JSON}")
    export VAULT_TOKEN
elif [[ -n "${VAULT_TOKEN:-}" ]]; then
    export VAULT_TOKEN
elif [[ -f "${APPROLE_DIR}/role-id" && -f "${APPROLE_DIR}/secret-id" ]]; then
    if VAULT_TOKEN=$(vault write -format=json auth/approle/login \
        role_id="$(<"${APPROLE_DIR}/role-id")" \
        secret_id="$(<"${APPROLE_DIR}/secret-id")" \
        | jq -r '.auth.client_token // empty') && [[ -n "${VAULT_TOKEN}" ]]; then
        export VAULT_TOKEN
    else
        unset VAULT_TOKEN
        echo "WARNING: AppRole login failed; authenticated checks will fail."
    fi
else
    echo "WARNING: No VAULT_TOKEN set and ${INIT_JSON} not found."
    echo "         Authenticated checks will fail."
fi

passed=0
failed=0
CHECK_NUM=0

check() {
    local name="$1"
    shift
    CHECK_NUM=$(( CHECK_NUM + 1 ))
    if "$@" >/dev/null 2>&1; then
        echo "  [PASS] ${CHECK_NUM}. ${name}"
        passed=$(( passed + 1 ))
    else
        echo "  [FAIL] ${CHECK_NUM}. ${name}"
        failed=$(( failed + 1 ))
    fi
}

###############################################################################
# Detect mode
###############################################################################
IS_AGENT=false
IS_CENTRAL=false

if systemctl --user is-active vault-tunnel.service &>/dev/null; then
    IS_AGENT=true
elif systemctl --user is-active vault.service &>/dev/null; then
    IS_CENTRAL=true
fi

if [[ "${IS_AGENT}" == "true" ]]; then
    echo "=== Vault Health Check (Agent — tunnel mode) ==="
elif [[ "${IS_CENTRAL}" == "true" ]]; then
    echo "=== Vault Health Check (Central) ==="
else
    echo "=== Vault Health Check (unknown mode) ==="
fi
echo ""

###############################################################################
# Common checks (run on all machines)
###############################################################################

check "Vault is reachable" vault status -format=json

check "Vault is unsealed" \
    bash -c 'test "$(vault status -format=json | jq -r .sealed)" = "false"'

check "Vault is initialized" \
    bash -c 'test "$(vault status -format=json | jq -r .initialized)" = "true"'

check "Vault version reported" \
    bash -c 'vault status -format=json | jq -e .version'

check "JWT auth method enabled" \
    bash -c 'vault auth list -format=json | jq -e ".\"jwt/\""'

check "osac-e2e role exists" \
    vault read auth/jwt/role/osac-e2e

check "osac-e2e policy exists" \
    vault policy read osac-e2e

check "KV v2 secrets engine at secret/" \
    bash -c 'vault secrets list -format=json | jq -e ".\"secret/\""'

check "AppRole auth method enabled" \
    bash -c 'vault auth list -format=json | jq -e ".\"approle/\""'

check "AppRole credentials present" \
    bash -c 'test -f "'"${APPROLE_DIR}/role-id"'" && test -f "'"${APPROLE_DIR}/secret-id"'"'

check "AppRole login succeeds" \
    bash -c 'vault write -format=json auth/approle/login \
        role_id="$(cat "'"${APPROLE_DIR}/role-id"'")" \
        secret_id="$(cat "'"${APPROLE_DIR}/secret-id"'")" \
        | jq -e ".auth.client_token"'

###############################################################################
# Mode-specific checks
###############################################################################
if [[ "${IS_CENTRAL}" == "true" ]]; then
    # Central: local vault.service and backup timer
    # (vault.service is-active was already used to detect IS_CENTRAL, so this
    # always passes here -- kept as an explicit numbered check for the report.)
    check "vault.service is active" \
        systemctl --user is-active vault.service

    check "vault.service is enabled" \
        systemctl --user is-enabled vault.service

    check "vault-backup.timer is active" \
        systemctl --user is-active vault-backup.timer

elif [[ "${IS_AGENT}" == "true" ]]; then
    # Agent: tunnel service
    # (vault-tunnel.service is-active was already used to detect IS_AGENT.)
    check "vault-tunnel.service is active" \
        systemctl --user is-active vault-tunnel.service

    check "vault-tunnel.service is enabled" \
        systemctl --user is-enabled vault-tunnel.service

    # Verify local vault is NOT running (would conflict)
    check "local vault.service is not active (expected)" \
        bash -c '! systemctl --user is-active vault.service'
fi

###############################################################################
# Summary
###############################################################################
echo ""
echo "=== Results: ${passed} passed, ${failed} failed ==="

if (( failed > 0 )); then
    echo ""
    echo "Troubleshooting:"
    if [[ "${IS_AGENT}" == "true" ]]; then
        echo "  systemctl --user status vault-tunnel.service  # Check tunnel"
        echo "  journalctl --user -u vault-tunnel.service     # Tunnel logs"
    else
        echo "  systemctl --user status vault.service         # Check vault"
        echo "  podman logs systemd-vault                     # Container logs"
    fi
    echo "  vault status                                  # Vault status"
    exit 1
fi
