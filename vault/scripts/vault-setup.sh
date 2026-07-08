#!/usr/bin/env bash
# vault-setup.sh -- Vault deployment on OSAC CI runners.
#
# Modes:
#   --central   Full local Vault setup (all phases).  Run on the designated
#               central Vault machine only.  This is the default if no mode
#               is specified (backwards-compatible).
#
#   --agent <central-host>
#               Tunnel-only setup for agent runners.  Connects to the central
#               Vault via an SSH tunnel and fetches AppRole credentials.
#               Requires VAULT_TOKEN env var (root token of central vault).
#
# Central phases:
#   1.  Create directory layout under ~/.vault-server/
#   2.  Copy config files from this repo
#   3.  Install Quadlet unit + systemd services, start Vault
#   4.  Initialize Vault (3 key shares, threshold 2)
#   5.  Extract unseal keys
#   6.  Unseal Vault
#   7.  Enable KV v2 secrets engine at secret/
#   8.  Enable and configure JWT auth (GitHub OIDC)
#   9.  Create osac-e2e policy and role
#  10.  Enable AppRole auth, write role-id/secret-id to ~/.vault-server/.approle/
#  11.  Enable loginctl linger, enable vault.service + backup timer
#
# Agent phases:
#   1.  Create directory layout (~/.vault-server/, .ssh/, .approle/)
#   2.  Generate SSH key for tunnel
#   3.  Install vault-tunnel.service, stop local vault
#   4.  Start tunnel, wait for Vault to be reachable
#   5.  Fetch AppRole credentials through tunnel
#   6.  Enable loginctl linger
#
# Prerequisites:
#   - Central: podman, jq, vault CLI installed
#   - Agent:   ssh, jq, vault CLI installed; VAULT_TOKEN env var set
#   - This script is run from the repo root (or VAULT_REPO_DIR is set)
set -euo pipefail

VAULT_ADDR="${VAULT_ADDR:-http://127.0.0.1:8200}"
export VAULT_ADDR

VAULT_HOME="${HOME}/.vault-server"
# Resolve to the vault/ directory: scripts live at vault/scripts/
VAULT_REPO_DIR="${VAULT_REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"

QUADLET_DIR="${HOME}/.config/containers/systemd"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"

###############################################################################
phase() { echo -e "\n==> Phase $1: $2"; }
info()  { echo "    $1"; }

# Helper: get a field from vault status JSON.
# vault status exits 0 (unsealed), 1 (error), or 2 (sealed/uninitialized),
# so we must suppress its exit code to avoid triggering pipefail.
vault_status_field() {
    (vault status -format=json 2>/dev/null || true) | jq -r ".$1" 2>/dev/null
}
###############################################################################

###############################################################################
# Parse arguments
###############################################################################
usage() {
    echo "Usage: $(basename "$0") [--central | --agent <central-host>]"
    echo ""
    echo "Modes:"
    echo "  --central              Full local Vault setup (default)"
    echo "  --agent <central-host> Tunnel-only agent setup"
    exit 1
}

MODE=""
CENTRAL_HOST=""

case "${1:-}" in
    --central)  MODE="central" ;;
    --agent)
        MODE="agent"
        CENTRAL_HOST="${2:-}"
        if [[ -z "${CENTRAL_HOST}" ]]; then
            echo "ERROR: --agent requires <central-host>" >&2
            usage
        fi
        # Validate host: IP address or hostname (alphanumeric, dots, hyphens)
        if ! [[ "${CENTRAL_HOST}" =~ ^[a-zA-Z0-9._-]+$ ]]; then
            echo "ERROR: Invalid hostname: ${CENTRAL_HOST}" >&2
            exit 1
        fi
        ;;
    "")         MODE="central" ;;  # Default: backwards-compatible
    *)          usage ;;
esac

###############################################################################
###############################################################################
#                           AGENT MODE
###############################################################################
###############################################################################
if [[ "${MODE}" == "agent" ]]; then

    echo "============================================"
    echo "  Vault Agent Setup (tunnel to ${CENTRAL_HOST})"
    echo "============================================"

    # Agent mode requires VAULT_TOKEN to fetch AppRole credentials
    if [[ -z "${VAULT_TOKEN:-}" ]]; then
        echo "ERROR: VAULT_TOKEN must be set to the root token of the central vault." >&2
        echo "  export VAULT_TOKEN='hvs.xxxxx'" >&2
        exit 1
    fi
    export VAULT_TOKEN

    ###########################################################################
    # Agent Phase 1: Create directory layout
    ###########################################################################
    phase 1 "Creating directory layout under ${VAULT_HOME}"
    mkdir -p "${VAULT_HOME}"/{.ssh,.approle}
    chmod 700 "${VAULT_HOME}" "${VAULT_HOME}/.ssh" "${VAULT_HOME}/.approle"

    ###########################################################################
    # Agent Phase 2: Generate SSH key for tunnel
    ###########################################################################
    phase 2 "Generating SSH key for vault tunnel"

    KEY_FILE="${VAULT_HOME}/.ssh/vault_tunnel_ed25519"
    if [[ -f "${KEY_FILE}" ]]; then
        echo "SSH key already exists at ${KEY_FILE} -- skipping."
    else
        ssh-keygen -t ed25519 -N "" -f "${KEY_FILE}" -C "vault-tunnel@$(hostname)"
        info "SSH key generated: ${KEY_FILE}"
    fi

    echo ""
    echo "  Add this public key to ${CENTRAL_HOST}'s ~/.ssh/authorized_keys:"
    echo ""
    cat "${KEY_FILE}.pub"
    echo ""
    echo "  Press Enter once the key has been added (or Ctrl+C to abort)..."
    read -r

    ###########################################################################
    # Agent Phase 3: Install vault-tunnel.service, stop local vault
    ###########################################################################
    phase 3 "Installing vault-tunnel.service"
    mkdir -p "${SYSTEMD_USER_DIR}"

    # Write the EnvironmentFile consumed by vault-tunnel.service
    echo "VAULT_CENTRAL_HOST=${CENTRAL_HOST}" > "${VAULT_HOME}/.tunnel-env"
    chmod 600 "${VAULT_HOME}/.tunnel-env"
    info "Wrote ${VAULT_HOME}/.tunnel-env (VAULT_CENTRAL_HOST=${CENTRAL_HOST})"

    # Seed known_hosts so the tunnel uses StrictHostKeyChecking=yes
    KNOWN_HOSTS="${VAULT_HOME}/.ssh/known_hosts"
    echo "  Fetching SSH host key for ${CENTRAL_HOST} ..."
    ssh-keyscan -H "${CENTRAL_HOST}" > "${KNOWN_HOSTS}" 2>/dev/null
    chmod 600 "${KNOWN_HOSTS}"
    info "Host key seeded in ${KNOWN_HOSTS}"

    # Install the service unit (no sed needed -- host comes from EnvironmentFile)
    cp "${VAULT_REPO_DIR}/vault-tunnel.service" \
        "${SYSTEMD_USER_DIR}/vault-tunnel.service"
    info "Installed vault-tunnel.service (target: ${CENTRAL_HOST})"

    systemctl --user daemon-reload

    # Stop local vault if running
    if systemctl --user is-active vault.service &>/dev/null; then
        echo "  Stopping local vault.service ..."
        systemctl --user stop vault.service || true
        info "Local vault.service stopped."
    else
        info "No local vault.service running."
    fi

    # Disable local vault independently (may be enabled but inactive)
    if systemctl --user is-enabled vault.service &>/dev/null; then
        systemctl --user disable vault.service 2>/dev/null || true
        info "Local vault.service disabled."
    fi

    # Stop and disable backup timer (not needed in agent mode)
    if systemctl --user is-active vault-backup.timer &>/dev/null; then
        systemctl --user stop vault-backup.timer || true
        systemctl --user disable vault-backup.timer 2>/dev/null || true
        info "vault-backup.timer stopped and disabled."
    fi

    ###########################################################################
    # Agent Phase 4: Start tunnel, wait for Vault
    ###########################################################################
    phase 4 "Starting vault tunnel and waiting for Vault"

    systemctl --user enable --now vault-tunnel.service
    info "vault-tunnel.service started."

    echo "  Waiting for Vault to be reachable through tunnel ..."
    vault_reachable=false
    for _ in $(seq 1 30); do
        if vault status -format=json &>/dev/null; then
            vault_reachable=true
            break
        fi
        sleep 1
    done

    if [[ "${vault_reachable}" != "true" ]]; then
        echo "ERROR: Vault not reachable through tunnel after 30 seconds." >&2
        echo "  Check: systemctl --user status vault-tunnel.service" >&2
        echo "  Ensure the SSH key is authorized on ${CENTRAL_HOST}." >&2
        exit 1
    fi

    info "Vault is reachable through tunnel."
    vault status -format=json | jq -r '"  Version: \(.version), Sealed: \(.sealed)"' 2>/dev/null || true

    ###########################################################################
    # Agent Phase 5: Fetch AppRole credentials through tunnel
    ###########################################################################
    phase 5 "Fetching AppRole credentials from central Vault"

    APPROLE_DIR="${VAULT_HOME}/.approle"
    mkdir -p "${APPROLE_DIR}"

    ROLE_ID=$(vault read -field=role_id auth/approle/role/osac-e2e/role-id)
    SECRET_ID=$(vault write -field=secret_id -f auth/approle/role/osac-e2e/secret-id)

    echo "${ROLE_ID}"  > "${APPROLE_DIR}/role-id"
    echo "${SECRET_ID}" > "${APPROLE_DIR}/secret-id"
    chmod 700 "${APPROLE_DIR}"
    chmod 600 "${APPROLE_DIR}/role-id" "${APPROLE_DIR}/secret-id"

    info "role-id and secret-id written to ${APPROLE_DIR}/"

    # Verify AppRole login works
    echo "  Verifying AppRole login ..."
    if vault write -format=json auth/approle/login \
        role_id="${ROLE_ID}" secret_id="${SECRET_ID}" \
        | jq -e '.auth.client_token' >/dev/null 2>&1; then
        info "AppRole login successful."
    else
        echo "ERROR: AppRole login verification failed." >&2
        echo "  Refusing to complete agent setup with unusable credentials." >&2
        exit 1
    fi

    ###########################################################################
    # Agent Phase 6: Enable linger
    ###########################################################################
    phase 6 "Enabling loginctl linger"
    loginctl enable-linger "$(whoami)" 2>/dev/null || true

    echo ""
    echo "============================================"
    echo "  Vault agent setup complete!"
    echo "============================================"
    echo ""
    echo "Vault is accessible at ${VAULT_ADDR} (via SSH tunnel to ${CENTRAL_HOST})"
    echo ""
    echo "Services:"
    echo "  vault-tunnel.service  $(systemctl --user is-active vault-tunnel.service 2>/dev/null || echo 'unknown')"
    echo ""
    echo "AppRole credentials:"
    echo "  ${APPROLE_DIR}/role-id"
    echo "  ${APPROLE_DIR}/secret-id"
    echo ""
    echo "Next steps:"
    echo "  1. Run vault-health-check.sh to verify"
    echo "  2. Test: vault kv get secret/osac/e2e/pull-secret"
    echo ""

    exit 0
fi

###############################################################################
###############################################################################
#                          CENTRAL MODE
###############################################################################
###############################################################################

echo "============================================"
echo "  Vault Central Setup"
echo "============================================"

###############################################################################
# Phase 1: Create directory layout
###############################################################################
phase 1 "Creating directory layout under ${VAULT_HOME}"
mkdir -p "${VAULT_HOME}"/{config,data,logs,scripts,backups}
chmod 700 "${VAULT_HOME}"

# The Vault container runs as uid=100(vault) gid=1000(vault).
# The data and logs dirs must be owned by that uid/gid within
# the container's user namespace.
VAULT_UID=100
VAULT_GID=1000
if [[ "${EUID}" -eq 0 ]]; then
    chown "${VAULT_UID}:${VAULT_GID}" "${VAULT_HOME}/data" "${VAULT_HOME}/logs"
else
    # Rootless Podman: chown inside the user namespace so that uid 100
    # on the host side maps to the correct subordinate UID.
    podman unshare chown "${VAULT_UID}:${VAULT_GID}" "${VAULT_HOME}/data" "${VAULT_HOME}/logs"
fi

###############################################################################
# Phase 2: Copy config files from repo
###############################################################################
phase 2 "Copying config files from repo"
cp "${VAULT_REPO_DIR}/config/vault.hcl" "${VAULT_HOME}/config/vault.hcl"
cp "${VAULT_REPO_DIR}/scripts/vault-unseal.sh" "${VAULT_HOME}/scripts/vault-unseal.sh"
cp "${VAULT_REPO_DIR}/scripts/vault-backup.sh" "${VAULT_HOME}/scripts/vault-backup.sh"
chmod +x "${VAULT_HOME}/scripts/"*.sh

###############################################################################
# Phase 3: Install Quadlet unit + systemd services, start Vault
###############################################################################
phase 3 "Installing Quadlet and systemd units"
mkdir -p "${QUADLET_DIR}" "${SYSTEMD_USER_DIR}"

cp "${VAULT_REPO_DIR}/quadlet/vault.container" "${QUADLET_DIR}/vault.container"
cp "${VAULT_REPO_DIR}/vault-unseal.service"    "${SYSTEMD_USER_DIR}/vault-unseal.service"
cp "${VAULT_REPO_DIR}/vault-backup.service"    "${SYSTEMD_USER_DIR}/vault-backup.service"
cp "${VAULT_REPO_DIR}/vault-backup.timer"      "${SYSTEMD_USER_DIR}/vault-backup.timer"

systemctl --user daemon-reload

echo "Starting vault.service ..."
# On first run, ExecStartPost (unseal) will exit 0 gracefully because Vault
# is not yet initialized.  If it fails for another reason, continue anyway --
# the setup script handles init + unseal in later phases.
systemctl --user start vault.service || true

# Wait for the Vault API to become reachable.
# vault status exits 0 (unsealed), 1 (error), or 2 (sealed/uninitialized).
# Any JSON response means the server is up.
echo "Waiting for Vault to be reachable ..."
vault_reachable=false
for _ in $(seq 1 30); do
    if [[ "$(vault_status_field 'type')" != "" && "$(vault_status_field 'type')" != "null" ]]; then
        vault_reachable=true
        break
    fi
    sleep 1
done

if [[ "${vault_reachable}" != "true" ]]; then
    echo "ERROR: Vault not reachable after 30 seconds." >&2
    echo "Check 'systemctl --user status vault.service' and 'podman logs systemd-vault'." >&2
    exit 1
fi

###############################################################################
# Phase 4: Initialize Vault
###############################################################################
phase 4 "Initializing Vault (3 key shares, threshold 2)"

init_status=$(vault_status_field 'initialized')
if [[ "${init_status}" == "true" ]]; then
    echo "Vault is already initialized -- skipping."
else
    vault operator init \
        -key-shares=3 \
        -key-threshold=2 \
        -format=json > "${VAULT_HOME}/.vault-init.json"
    chmod 0600 "${VAULT_HOME}/.vault-init.json"
    echo "Init output saved to ${VAULT_HOME}/.vault-init.json (mode 0600)"
    echo "IMPORTANT: Back up this file securely, then delete it from the runner."
fi

###############################################################################
# Phase 5: Extract unseal keys
###############################################################################
phase 5 "Extracting unseal keys"

if [[ -f "${VAULT_HOME}/.vault-init.json" ]]; then
    jq -r '.unseal_keys_b64[]' "${VAULT_HOME}/.vault-init.json" \
        > "${VAULT_HOME}/.unseal-keys"
    chmod 0600 "${VAULT_HOME}/.unseal-keys"
    echo "Unseal keys written to ${VAULT_HOME}/.unseal-keys (mode 0600)"
else
    echo "No init JSON found -- assuming unseal keys already exist."
fi

###############################################################################
# Phase 6: Unseal Vault
###############################################################################
phase 6 "Unsealing Vault"
"${VAULT_HOME}/scripts/vault-unseal.sh"

###############################################################################
# Phase 7: Enable KV v2 secrets engine
###############################################################################
phase 7 "Enabling KV v2 secrets engine at secret/"

# Authenticate with the root token from init
if [[ -f "${VAULT_HOME}/.vault-init.json" ]]; then
    export VAULT_TOKEN
    VAULT_TOKEN=$(jq -r '.root_token' "${VAULT_HOME}/.vault-init.json")
elif [[ -z "${VAULT_TOKEN:-}" ]]; then
    echo "ERROR: No VAULT_TOKEN set and ${VAULT_HOME}/.vault-init.json not found." >&2
    echo "Export VAULT_TOKEN before re-running, or provide the init JSON." >&2
    exit 1
fi

# Check if already enabled
if vault secrets list -format=json 2>/dev/null | jq -e '."secret/"' >/dev/null 2>&1; then
    echo "KV v2 at secret/ already enabled -- skipping."
else
    vault secrets enable -path=secret kv-v2
    echo "KV v2 enabled at secret/"
fi

###############################################################################
# Phase 8: Enable and configure JWT auth (GitHub OIDC)
###############################################################################
phase 8 "Configuring JWT auth for GitHub OIDC"

if vault auth list -format=json 2>/dev/null | jq -e '."jwt/"' >/dev/null 2>&1; then
    echo "JWT auth already enabled -- reconfiguring."
else
    vault auth enable jwt
    echo "JWT auth enabled."
fi

vault write auth/jwt/config \
    oidc_discovery_url="https://token.actions.githubusercontent.com" \
    bound_issuer="https://token.actions.githubusercontent.com"

echo "JWT auth configured with GitHub Actions OIDC provider."

###############################################################################
# Phase 9: Create osac-e2e policy and role
###############################################################################
phase 9 "Creating osac-e2e policy and role"

vault policy write osac-e2e - <<'POLICY'
# Allow reading e2e test secrets
path "secret/data/osac/e2e/*" {
  capabilities = ["read"]
}
# Allow reading the monitoring stack's GitHub token (OSAC-2204
# deploy-monitoring.yml reuses this same AppRole/policy -- the real trust
# boundary here is "who can run workflows as github-runner on osac-ci-1",
# which a second AppRole on the same box wouldn't actually narrow, just
# add rotation overhead for). Scoped to this one secret, not a
# secret/data/osac/monitoring/* wildcard, so adding other monitoring
# secrets later doesn't implicitly grant this AppRole read access to them.
path "secret/data/osac/monitoring/github-token" {
  capabilities = ["read"]
}
# Alertmanager's Slack webhook -- was only ever manually configured in the
# deployed alertmanager.yml on osac-ci-1, never committed to git (it's a
# credential). --update-central needs it to render the config without
# clobbering the deployed file with the repo template's placeholder.
path "secret/data/osac/monitoring/slack-webhook-url" {
  capabilities = ["read"]
}
# Grafana's GitHub OAuth app credentials + root URL -- previously only in
# the manually-configured .env.grafana / hardcoded in grafana.container
# on osac-ci-1, never in git.
path "secret/data/osac/monitoring/grafana-oauth" {
  capabilities = ["read"]
}
POLICY
echo "Policy 'osac-e2e' created."

vault write auth/jwt/role/osac-e2e - <<'ROLE'
{
  "role_type": "jwt",
  "bound_claims": {
    "repository_owner": "osac-project",
    "environment": "e2e-test"
  },
  "bound_audiences": ["https://github.com/osac-project"],
  "user_claim": "repository",
  "token_policies": ["osac-e2e"],
  "token_ttl": "60m",
  "token_max_ttl": "120m"
}
ROLE

echo "Role 'osac-e2e' created (bound to osac-project org + e2e-test environment, 60m TTL)."

###############################################################################
# Phase 10: Enable AppRole auth and write credentials
###############################################################################
phase 10 "Configuring AppRole auth for GitHub Actions runners"

if vault auth list -format=json 2>/dev/null | jq -e '."approle/"' >/dev/null 2>&1; then
    echo "AppRole auth already enabled -- skipping."
else
    vault auth enable approle
    echo "AppRole auth enabled."
fi

# Create (or update) the osac-e2e AppRole role.
# secret_id_ttl=90d: agents must rotate credentials within 90 days.
# secret_id_num_uses=0: unlimited uses within the TTL window (agents
# authenticate on every workflow run).
vault write auth/approle/role/osac-e2e \
    token_policies="osac-e2e" \
    token_ttl=10m \
    token_max_ttl=30m \
    secret_id_num_uses=0 \
    secret_id_ttl=90d

# Fetch role-id and generate a secret-id
APPROLE_DIR="${VAULT_HOME}/.approle"
mkdir -p "${APPROLE_DIR}"

ROLE_ID=$(vault read -field=role_id auth/approle/role/osac-e2e/role-id)
SECRET_ID=$(vault write -field=secret_id -f auth/approle/role/osac-e2e/secret-id)

echo "${ROLE_ID}"  > "${APPROLE_DIR}/role-id"
echo "${SECRET_ID}" > "${APPROLE_DIR}/secret-id"
chmod 700 "${APPROLE_DIR}"
chmod 600 "${APPROLE_DIR}/role-id" "${APPROLE_DIR}/secret-id"

echo "AppRole credentials written to ${APPROLE_DIR}/"

###############################################################################
# Phase 11: Enable linger, enable services
###############################################################################
phase 11 "Enabling loginctl linger and systemd services"

loginctl enable-linger "$(whoami)" 2>/dev/null || true

# vault.service is a Quadlet-generated unit; it is enabled automatically
# via its [Install] WantedBy in the .container file and cannot be
# enabled with systemctl.  We only need to enable the backup timer.
systemctl --user enable vault-backup.timer
systemctl --user start vault-backup.timer

echo ""
echo "============================================"
echo "  Vault central setup complete!"
echo "============================================"
echo ""
echo "Next steps:"
echo "  1. Back up ${VAULT_HOME}/.vault-init.json securely"
echo "  2. Run vault-health-check.sh to verify"
echo "  3. Delete the init JSON from this machine, or export VAULT_TOKEN for future authenticated checks"
echo "  4. Populate e2e secrets at secret/osac/e2e/"
echo "  5. On agent runners, run: vault-setup.sh --agent osac-ci-1.redhat.com"
echo ""
