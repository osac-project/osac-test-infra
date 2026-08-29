#!/bin/bash

# push-netris-secrets.sh -- Push the base set of secrets (pull secret, AAP
# license, Netris license, and a config with the lab name) from the
# orchestrator to the freshly-provisioned ephemeral EC2 box via SCP.
#
# This is the suite-agnostic base -- every Netris-backend workflow needs
# these. Suite-specific secrets (e.g. Route53 for CaaS DNS) are pushed by
# separate scripts that append to the config file this script creates.
#
# See push-caas-netris-secrets.sh's header for the full design rationale
# (Vault access model, SCP vs SSH argv, staging directory lifecycle).
#
# Required env vars:
#   SSH_KEY_PATH          path to the orchestrator's SSH private key
#   SSH_USER              SSH user on the box (from provision.sh output)
#   PUBLIC_IP             the box's public IP (from provision.sh output)
#   KNOWN_HOSTS_FILE      the same run-specific known_hosts path provision.sh
#                         used to establish trust with this box
#   NETRIS_LICENSE        raw Netris license key content
#   AAP_LICENSE_ZIP_PATH  local path to the already-fetched, already
#                         base64-decoded AAP license zip
#   PULL_SECRET_JSON_PATH local path to the already-fetched pull secret JSON
#   LAB_NAME              unique lab name (becomes a subdomain under the
#                         shared hosted zone)
#
# Optional env vars:
#   REMOTE_STAGING_DIR    fixed path on the box to stage secrets at
#                         (default: /root/caas-netris-secrets) -- must match
#                         stage-caas-netris-secrets.sh's value

set -euo pipefail

RESET="\e[0m"
BOLD="\e[1m"
GREEN="\e[32m"

: "${SSH_KEY_PATH:?SSH_KEY_PATH is required}"
: "${SSH_USER:?SSH_USER is required}"
: "${PUBLIC_IP:?PUBLIC_IP is required}"
: "${KNOWN_HOSTS_FILE:?KNOWN_HOSTS_FILE is required}"
: "${NETRIS_LICENSE:?NETRIS_LICENSE is required}"
: "${AAP_LICENSE_ZIP_PATH:?AAP_LICENSE_ZIP_PATH is required}"
: "${PULL_SECRET_JSON_PATH:?PULL_SECRET_JSON_PATH is required}"
: "${LAB_NAME:?LAB_NAME is required}"

REMOTE_STAGING_DIR="${REMOTE_STAGING_DIR:-/root/caas-netris-secrets}"

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

echo -e "${BOLD}Staging Netris secrets on ${PUBLIC_IP}...${RESET}"

NETRIS_LICENSE_FILE=$(mktemp)
CONFIG_FILE=$(mktemp)
trap 'rm -f "$NETRIS_LICENSE_FILE" "$CONFIG_FILE"' EXIT

printf '%s' "${NETRIS_LICENSE}" > "$NETRIS_LICENSE_FILE"

umask 077
cat > "$CONFIG_FILE" <<EOF
[default]
lab_name = ${LAB_NAME}
EOF

ssh_exec "mkdir -p '${REMOTE_STAGING_DIR}' && chmod 700 '${REMOTE_STAGING_DIR}'"

scp_to_box "$NETRIS_LICENSE_FILE" "${REMOTE_STAGING_DIR}/license.key"
scp_to_box "$AAP_LICENSE_ZIP_PATH" "${REMOTE_STAGING_DIR}/license.zip"
scp_to_box "$PULL_SECRET_JSON_PATH" "${REMOTE_STAGING_DIR}/pull-secret"
scp_to_box "$CONFIG_FILE" "${REMOTE_STAGING_DIR}/config"

ssh_exec "chmod 600 '${REMOTE_STAGING_DIR}'/*"

echo -e "${GREEN}${BOLD}Netris secrets staged at ${REMOTE_STAGING_DIR} on the box.${RESET}"
