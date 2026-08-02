#!/usr/bin/env bash
# vault-add-ec2-runner-network-config.sh -- Store the AMI id, subnet id, and
# security group id the osac-ci-orchestrator uses to launch ephemeral EC2 e2e
# runners.
#
# This repo is public: workflow_dispatch inputs are visible forever in the
# Actions run history/API to anyone, so subnet/security-group ids (which
# reveal real VPC layout) and the AMI id must not be passed as dispatch
# inputs. Vault-backed config, fetched at run time the same way the AWS
# credentials and GitHub PAT already are, keeps them out of any public log.
#
# Written to secret/osac/e2e/ec2-runner-network-config, which already falls
# under the existing osac-e2e AppRole policy's "secret/data/osac/e2e/*"
# wildcard (see vault-setup.sh phase 9) -- no policy or role changes needed.
# Run once on the central Vault, then vault-sync.sh to propagate.
#
# Usage:
#   ./vault-add-ec2-runner-network-config.sh [--dry-run]
#
# Prompts for the AMI id, subnet id, and security group id rather than taking
# them as CLI arguments, so they never land in shell history or `ps` output.
# When stdin isn't a TTY (e.g. piped input), reads three lines instead: AMI
# id, subnet id, then security group id.
set -euo pipefail

VAULT_ADDR="${VAULT_ADDR:-http://127.0.0.1:8200}"
export VAULT_ADDR
SECRET_PATH="${SECRET_PATH:-secret/osac/e2e/ec2-runner-network-config}"

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
# Read config
###############################################################################
if [[ -t 0 ]]; then
    read -r -p "AMI id: " AMI_ID
    read -r -p "Subnet id: " SUBNET_ID
    read -r -p "Security group id: " SECURITY_GROUP_ID
else
    read -r AMI_ID
    read -r SUBNET_ID
    read -r SECURITY_GROUP_ID
fi

if [[ -z "${AMI_ID}" || -z "${SUBNET_ID}" || -z "${SECURITY_GROUP_ID}" ]]; then
    echo "ERROR: AMI id, subnet id, and security group id must not be empty." >&2
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
    echo "[DRY RUN] Would write ami_id + subnet_id + security_group_id to ${SECRET_PATH}."
else
    vault kv put "${SECRET_PATH}" \
        "ami_id=${AMI_ID}" \
        "subnet_id=${SUBNET_ID}" \
        "security_group_id=${SECURITY_GROUP_ID}"
    echo "Done. Run vault-sync.sh to propagate to other machines."
fi
