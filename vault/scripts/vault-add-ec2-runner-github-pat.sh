#!/usr/bin/env bash
# vault-add-ec2-runner-github-pat.sh -- Store the fine-grained GitHub PAT the
# osac-ci-orchestrator uses to register/deregister ephemeral EC2 e2e runners.
#
# GITHUB_TOKEN (the automatic workflow token) cannot call the self-hosted
# runner generate-jitconfig/delete endpoints under any `permissions:` grant --
# confirmed during implementation (GitHub Docs: this requires a PAT with repo
# scope, or a fine-grained PAT/GitHub App with "Administration: write" repo
# permission; GITHUB_TOKEN supports neither). A standing credential is
# unavoidable here.
#
# Written to secret/osac/e2e/ec2-runner-github-pat, which already falls under
# the existing osac-e2e AppRole policy's "secret/data/osac/e2e/*" wildcard
# (see vault-setup.sh phase 9) -- no policy or role changes needed. Run once
# on the central Vault, then vault-sync.sh to propagate.
#
# Usage:
#   ./vault-add-ec2-runner-github-pat.sh [--dry-run]
#
# Prompts for the token (input is not echoed) rather than taking it as a CLI
# argument, so it never lands in shell history or `ps` output. When stdin
# isn't a TTY (e.g. piped input), reads one line instead.
#
# The token must be a FINE-GRAINED PAT (github.com -> Settings -> Developer
# settings -> Fine-grained tokens) scoped to ONLY:
#   - Repository access: osac-project/osac-test-infra (this repo only)
#   - Permissions: Administration: Read and write
# No other repository or organization permissions. Set an expiration date and
# track it -- fine-grained PATs are not renewed automatically.
set -euo pipefail

VAULT_ADDR="${VAULT_ADDR:-http://127.0.0.1:8200}"
export VAULT_ADDR
SECRET_PATH="${SECRET_PATH:-secret/osac/e2e/ec2-runner-github-pat}"

###############################################################################
# Parse arguments
###############################################################################
DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
elif [[ $# -gt 0 ]]; then
    echo "Usage: $0 [--dry-run]" >&2
    exit 1
fi

###############################################################################
# Read token
###############################################################################
if [[ -t 0 ]]; then
    read -r -s -p "GitHub PAT: " TOKEN
    echo
else
    read -r TOKEN
fi

if [[ -z "${TOKEN}" ]]; then
    echo "ERROR: token must not be empty." >&2
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
# Write
###############################################################################
if [[ "${DRY_RUN}" == "true" ]]; then
    echo "[DRY RUN] Would write token to ${SECRET_PATH}."
else
    vault kv put "${SECRET_PATH}" "token=${TOKEN}"
    echo "Done. Run vault-sync.sh to propagate to other machines."
fi
