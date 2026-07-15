#!/bin/bash

# write-caas-netris-config.sh -- Write the secret/config files infra/netris's
# Makefile and ansible roles expect, before running any make target against
# it. Runs directly on the ephemeral EC2 box (as root) in the e2e-ec2-runner
# test job -- no SSH hop needed, unlike the equivalent Prow lab-setup step
# this mirrors.
#
# Must run BEFORE `make setup-infra` (Prow copied these during its own
# lab-setup, i.e. early, not lazily at deploy time -- deploy-ocp-snapshot
# needs the pull secret and licenses already in place).
#
# Required env vars:
#   NETRIS_DIR                 path to the vendored infra/netris checkout
#                               (e.g. infra/netris, relative to repo root)
#   NETRIS_LICENSE              raw Netris license key content
#   ROUTE53_AWS_ACCESS_KEY_ID   AWS access key id for infra/netris's Route 53
#                               DNS management (from fetch-caas-route53-creds
#                               -- deliberately NOT the ec2-runner AWS
#                               credential, which has no Route53 permissions)
#   ROUTE53_AWS_SECRET_ACCESS_KEY  matching secret access key
#   AAP_LICENSE_ZIP_PATH        path to the already-fetched, already
#                               base64-decoded AAP license zip (written by
#                               fetch-and-write-secrets to
#                               $RUNNER_TEMP/aap-license.zip)
#   PULL_SECRET_JSON_PATH       path to the already-fetched pull secret JSON
#                               (written by fetch-and-write-secrets to
#                               $RUNNER_TEMP/pull-secret.json)
#   LAB_NAME                    unique lab name (becomes a subdomain under
#                               the shared hosted zone -- see
#                               infra/netris/README.md)
#
# Optional env vars:
#   AWS_REGION                  region for infra/netris's own Route53 config
#                               (default: us-east-1)

set -euo pipefail

RESET="\e[0m"
BOLD="\e[1m"
GREEN="\e[32m"

: "${NETRIS_DIR:?NETRIS_DIR is required}"
: "${NETRIS_LICENSE:?NETRIS_LICENSE is required}"
: "${ROUTE53_AWS_ACCESS_KEY_ID:?ROUTE53_AWS_ACCESS_KEY_ID is required}"
: "${ROUTE53_AWS_SECRET_ACCESS_KEY:?ROUTE53_AWS_SECRET_ACCESS_KEY is required}"
: "${AAP_LICENSE_ZIP_PATH:?AAP_LICENSE_ZIP_PATH is required}"
: "${PULL_SECRET_JSON_PATH:?PULL_SECRET_JSON_PATH is required}"
: "${LAB_NAME:?LAB_NAME is required}"

AWS_REGION="${AWS_REGION:-us-east-1}"

# AWS_REGION is interpolated directly into the config INI file below.
# Real AWS region names are always lowercase alphanumeric plus hyphens
# (e.g. us-east-1) -- reject anything else (a newline could inject an extra
# key into the [default] section; this is a defense-in-depth check, since
# the caller workflow's aws-region input is free text, not a curated list).
if [[ ! "$AWS_REGION" =~ ^[a-z0-9-]+$ ]]; then
    echo "ERROR: AWS_REGION '${AWS_REGION}' is not a valid-looking region name." >&2
    exit 1
fi

echo -e "${BOLD}Writing CaaS/Netris secret and config files...${RESET}"

printf '%s' "${NETRIS_LICENSE}" > "${NETRIS_DIR}/license.key"
chmod 600 "${NETRIS_DIR}/license.key"
echo -e "${GREEN}Wrote ${NETRIS_DIR}/license.key${RESET}"

cp "${AAP_LICENSE_ZIP_PATH}" "${NETRIS_DIR}/license.zip"
chmod 600 "${NETRIS_DIR}/license.zip"
echo -e "${GREEN}Wrote ${NETRIS_DIR}/license.zip${RESET}"

cp "${PULL_SECRET_JSON_PATH}" /root/pull-secret
chmod 600 /root/pull-secret
echo -e "${GREEN}Wrote /root/pull-secret${RESET}"

umask 077
cat > "${NETRIS_DIR}/config" <<EOF
[default]
lab_name = ${LAB_NAME}
aws_access_key_id = ${ROUTE53_AWS_ACCESS_KEY_ID}
aws_secret_access_key = ${ROUTE53_AWS_SECRET_ACCESS_KEY}
aws_region = ${AWS_REGION}
EOF
echo -e "${GREEN}Wrote ${NETRIS_DIR}/config (lab_name=${LAB_NAME}, aws_region=${AWS_REGION})${RESET}"

echo -e "${GREEN}${BOLD}CaaS/Netris config ready.${RESET}"
