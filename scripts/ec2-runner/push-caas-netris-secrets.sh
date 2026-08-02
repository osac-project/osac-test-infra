#!/bin/bash

# push-caas-netris-secrets.sh -- Fetch secrets on the orchestrator (which has
# working Vault access) and push them to the freshly-provisioned ephemeral
# EC2 box via SCP, before the JIT runner even starts.
#
# The ephemeral box itself never talks to Vault and never receives any
# Vault credential of its own -- confirmed the hard way: the test job
# originally tried to fetch these secrets directly on the box, and failed
# immediately ("AppRole credentials missing or empty in
# ~/.vault-server/.approle") because that machine was never bootstrapped
# with the Vault SSH-tunnel/AppRole setup persistent machines like the
# orchestrator have. This matches the design's own "nothing to revoke is
# safer than something to remember to revoke" principle for disposable
# machines -- the fix is not to give the box its own Vault trust, it's to
# have the orchestrator (which already has trust) push what the box needs.
#
# Uses real scp file transfer, not an SSH command with the secret value
# embedded in argv (the way start-runner.sh's JIT_CONFIG push works, with
# its documented ps/proc exposure caveat) -- scp streams file bytes over
# SSH's data channel, so secret content never appears as a process argument
# on either machine.
#
# Secrets land in a fixed staging directory rather than directly under
# infra/netris/: at this point in the flow the JIT runner hasn't started
# yet, so there's no git checkout on the box for infra/netris/ to exist
# under. scripts/ec2-runner/stage-caas-netris-secrets.sh (run by the test
# job, after its own checkout) copies them into their final place.
#
# Required env vars:
#   SSH_KEY_PATH        path to the orchestrator's SSH private key
#   SSH_USER            SSH user on the box (from provision.sh output)
#   PUBLIC_IP           the box's public IP (from provision.sh output)
#   KNOWN_HOSTS_FILE     the same run-specific known_hosts path provision.sh
#                        used to establish trust with this box
#   NETRIS_LICENSE       raw Netris license key content
#   ROUTE53_AWS_ACCESS_KEY_ID       AWS access key id for Route 53 DNS
#                        management (deliberately not the ec2-runner AWS
#                        credential, which has no Route53 permissions)
#   ROUTE53_AWS_SECRET_ACCESS_KEY   matching secret access key
#   AAP_LICENSE_ZIP_PATH  local path to the already-fetched, already
#                        base64-decoded AAP license zip (written by
#                        fetch-and-write-secrets to
#                        $RUNNER_TEMP/aap-license.zip)
#   PULL_SECRET_JSON_PATH  local path to the already-fetched pull secret
#                        JSON (written by fetch-and-write-secrets to
#                        $RUNNER_TEMP/pull-secret.json)
#   LAB_NAME             unique lab name (becomes a subdomain under the
#                        shared hosted zone -- see infra/netris/README.md)
#
# Optional env vars:
#   AWS_REGION            region for infra/netris's own Route53 config
#                        (default: us-east-1)
#   REMOTE_STAGING_DIR    fixed path on the box to stage secrets at
#                        (default: /root/caas-netris-secrets) -- must match
#                        stage-caas-netris-secrets.sh's value

set -euo pipefail

RESET="\e[0m"
BOLD="\e[1m"
GREEN="\e[32m"

: "${SSH_KEY_PATH:?SSH_KEY_PATH is required}"
: "${SSH_USER:?SSH_USER is required}"
: "${PUBLIC_IP:?PUBLIC_IP is required}"
: "${KNOWN_HOSTS_FILE:?KNOWN_HOSTS_FILE is required}"
: "${NETRIS_LICENSE:?NETRIS_LICENSE is required}"
: "${ROUTE53_AWS_ACCESS_KEY_ID:?ROUTE53_AWS_ACCESS_KEY_ID is required}"
: "${ROUTE53_AWS_SECRET_ACCESS_KEY:?ROUTE53_AWS_SECRET_ACCESS_KEY is required}"
: "${AAP_LICENSE_ZIP_PATH:?AAP_LICENSE_ZIP_PATH is required}"
: "${PULL_SECRET_JSON_PATH:?PULL_SECRET_JSON_PATH is required}"
: "${LAB_NAME:?LAB_NAME is required}"

AWS_REGION="${AWS_REGION:-us-east-1}"
REMOTE_STAGING_DIR="${REMOTE_STAGING_DIR:-/root/caas-netris-secrets}"

# AWS_REGION is interpolated directly into the config INI file below.
# Real AWS region names are always lowercase alphanumeric plus hyphens
# (e.g. us-east-1) -- reject anything else (a newline could inject an extra
# key into the [default] section; this is a defense-in-depth check, since
# the caller workflow's aws-region input is free text, not a curated list).
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
    # -p preserves the source file's mode (all four sources below are 600,
    # written by mktemp or an explicit chmod) instead of falling back to
    # sftp's default create mode -- without it, each file would briefly sit
    # on the box at whatever the remote sshd's default permissions are until
    # the batch chmod below runs afterwards in a separate SSH round-trip.
    scp -p -i "$SSH_KEY_PATH" \
        -o StrictHostKeyChecking=accept-new \
        -o UserKnownHostsFile="${KNOWN_HOSTS_FILE}" \
        -o BatchMode=yes \
        -o ConnectTimeout=10 \
        "$1" "${SSH_USER}@${PUBLIC_IP}:$2"
}

echo -e "${BOLD}Staging CaaS/Netris secrets on ${PUBLIC_IP}...${RESET}"

NETRIS_LICENSE_FILE=$(mktemp)
CONFIG_FILE=$(mktemp)
trap 'rm -f "$NETRIS_LICENSE_FILE" "$CONFIG_FILE"' EXIT

printf '%s' "${NETRIS_LICENSE}" > "$NETRIS_LICENSE_FILE"

umask 077
cat > "$CONFIG_FILE" <<EOF
[default]
lab_name = ${LAB_NAME}
aws_access_key_id = ${ROUTE53_AWS_ACCESS_KEY_ID}
aws_secret_access_key = ${ROUTE53_AWS_SECRET_ACCESS_KEY}
aws_region = ${AWS_REGION}
EOF

ssh_exec "mkdir -p '${REMOTE_STAGING_DIR}' && chmod 700 '${REMOTE_STAGING_DIR}'"

scp_to_box "$NETRIS_LICENSE_FILE" "${REMOTE_STAGING_DIR}/license.key"
scp_to_box "$AAP_LICENSE_ZIP_PATH" "${REMOTE_STAGING_DIR}/license.zip"
scp_to_box "$PULL_SECRET_JSON_PATH" "${REMOTE_STAGING_DIR}/pull-secret"
scp_to_box "$CONFIG_FILE" "${REMOTE_STAGING_DIR}/config"

ssh_exec "chmod 600 '${REMOTE_STAGING_DIR}'/*"

echo -e "${GREEN}${BOLD}CaaS/Netris secrets staged at ${REMOTE_STAGING_DIR} on the box.${RESET}"
