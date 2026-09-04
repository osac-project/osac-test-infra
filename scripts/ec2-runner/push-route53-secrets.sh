#!/bin/bash

# push-route53-secrets.sh -- Append Route53 AWS credentials to the config
# file that push-netris-secrets.sh already staged on the ephemeral box.
#
# Needed by any suite that runs `make deploy-ocp` (playbooks/deploy-ocp.yml
# always includes the configure-dns role) -- that's every Netris-backend
# suite, including BMaaS, which still deploys an OCP SNO cluster.
#
# Must run AFTER push-netris-secrets.sh (the config file and staging
# directory must already exist on the box).
#
# Required env vars:
#   SSH_KEY_PATH                    path to the orchestrator's SSH private key
#   SSH_USER                        SSH user on the box
#   PUBLIC_IP                       the box's public IP
#   KNOWN_HOSTS_FILE                run-specific known_hosts path
#   ROUTE53_AWS_ACCESS_KEY_ID       AWS access key id for Route 53
#   ROUTE53_AWS_SECRET_ACCESS_KEY   matching secret access key
#
# Optional env vars:
#   AWS_REGION              region for Route53 config (default: us-east-1)
#   REMOTE_STAGING_DIR      must match push-netris-secrets.sh's value
#                           (default: /root/caas-netris-secrets)

set -euo pipefail

RESET="\e[0m"
BOLD="\e[1m"
GREEN="\e[32m"

: "${SSH_KEY_PATH:?SSH_KEY_PATH is required}"
: "${SSH_USER:?SSH_USER is required}"
: "${PUBLIC_IP:?PUBLIC_IP is required}"
: "${KNOWN_HOSTS_FILE:?KNOWN_HOSTS_FILE is required}"
: "${ROUTE53_AWS_ACCESS_KEY_ID:?ROUTE53_AWS_ACCESS_KEY_ID is required}"
: "${ROUTE53_AWS_SECRET_ACCESS_KEY:?ROUTE53_AWS_SECRET_ACCESS_KEY is required}"

AWS_REGION="${AWS_REGION:-us-east-1}"
REMOTE_STAGING_DIR="${REMOTE_STAGING_DIR:-/root/caas-netris-secrets}"

# AWS_REGION is interpolated directly into the config INI file.
# Real AWS region names are always lowercase alphanumeric plus hyphens
# (e.g. us-east-1) -- reject anything else.
if [[ ! "$AWS_REGION" =~ ^[a-z0-9-]+$ ]]; then
    echo "ERROR: AWS_REGION '${AWS_REGION}' is not a valid-looking region name." >&2
    exit 1
fi

ssh_exec() {
    ssh -i "$SSH_KEY_PATH" \
        -o StrictHostKeyChecking=accept-new \
        -o UserKnownHostsFile="${KNOWN_HOSTS_FILE}" \
        -o BatchMode=yes \
        -o ConnectTimeout=10 \
        "${SSH_USER}@${PUBLIC_IP}" "$@"
}

scp_to_box() {
    scp -p -i "$SSH_KEY_PATH" \
        -o StrictHostKeyChecking=accept-new \
        -o UserKnownHostsFile="${KNOWN_HOSTS_FILE}" \
        -o BatchMode=yes \
        -o ConnectTimeout=10 \
        "$1" "${SSH_USER}@${PUBLIC_IP}:$2"
}

echo -e "${BOLD}Appending Route53 credentials to config on ${PUBLIC_IP}...${RESET}"

# Write the AWS credentials to a local temp file, SCP it to the box, then
# remotely append it to the config and remove the temp. Same SCP-based
# approach as push-netris-secrets.sh -- avoids embedding secret values in
# SSH command arguments where special characters could cause problems.
ROUTE53_FRAGMENT=$(mktemp)
trap 'rm -f "$ROUTE53_FRAGMENT"' EXIT

umask 077
cat > "$ROUTE53_FRAGMENT" <<EOF
aws_access_key_id = ${ROUTE53_AWS_ACCESS_KEY_ID}
aws_secret_access_key = ${ROUTE53_AWS_SECRET_ACCESS_KEY}
aws_region = ${AWS_REGION}
EOF

scp_to_box "$ROUTE53_FRAGMENT" "${REMOTE_STAGING_DIR}/route53-fragment"
ssh_exec "cat '${REMOTE_STAGING_DIR}/route53-fragment' >> '${REMOTE_STAGING_DIR}/config' && rm -f '${REMOTE_STAGING_DIR}/route53-fragment' && chmod 600 '${REMOTE_STAGING_DIR}/config'"

echo -e "${GREEN}${BOLD}Route53 credentials appended.${RESET}"
