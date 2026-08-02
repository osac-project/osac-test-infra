#!/usr/bin/env bash
# vault-add-ec2-runner-aws-creds.sh -- Store the AWS credentials the
# osac-ci-orchestrator uses to launch/terminate ephemeral EC2 e2e runners.
#
# Written to secret/osac/e2e/ec2-runner-aws-credentials, which already falls
# under the existing osac-e2e AppRole policy's "secret/data/osac/e2e/*"
# wildcard (see vault-setup.sh phase 9) -- no policy or role changes needed.
# Run once on the central Vault, then vault-sync.sh to propagate.
#
# Usage:
#   ./vault-add-ec2-runner-aws-creds.sh [--dry-run]
#
# Prompts for the access key id and secret access key (secret input is not
# echoed) rather than taking them as CLI arguments, so they never land in
# shell history or `ps` output. When stdin isn't a TTY (e.g. piped input),
# reads two lines instead: access key id, then secret access key.
#
# These credentials should be scoped to exactly the EC2 lifecycle actions the
# orchestrator scripts need (RunInstances/TerminateInstances/DescribeInstances/
# DescribeInstanceStatus/CreateTags) -- create a dedicated IAM user for this,
# not a broad admin credential.
set -euo pipefail

VAULT_ADDR="${VAULT_ADDR:-http://127.0.0.1:8200}"
export VAULT_ADDR
SECRET_PATH="${SECRET_PATH:-secret/osac/e2e/ec2-runner-aws-credentials}"

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
# Read credentials
###############################################################################
if [[ -t 0 ]]; then
    read -r -p "AWS Access Key ID: " ACCESS_KEY_ID
    read -r -s -p "AWS Secret Access Key: " SECRET_ACCESS_KEY
    echo
else
    read -r ACCESS_KEY_ID
    read -r SECRET_ACCESS_KEY
fi

if [[ -z "${ACCESS_KEY_ID}" || -z "${SECRET_ACCESS_KEY}" ]]; then
    echo "ERROR: access key id and secret access key must not be empty." >&2
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
    echo "[DRY RUN] Would write access_key_id + secret_access_key to ${SECRET_PATH}."
else
    vault kv put "${SECRET_PATH}" \
        "access_key_id=${ACCESS_KEY_ID}" \
        "secret_access_key=${SECRET_ACCESS_KEY}"
    echo "Done. Run vault-sync.sh to propagate to other machines."
fi
