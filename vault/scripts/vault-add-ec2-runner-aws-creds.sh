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
#   ./vault-add-ec2-runner-aws-creds.sh <access-key-id> <secret-access-key>
#   ./vault-add-ec2-runner-aws-creds.sh <access-key-id> <secret-access-key> --dry-run
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
if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <access-key-id> <secret-access-key> [--dry-run]" >&2
    exit 1
fi

ACCESS_KEY_ID="$1"
SECRET_ACCESS_KEY="$2"
DRY_RUN=false
if [[ "${3:-}" == "--dry-run" ]]; then
    DRY_RUN=true
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
