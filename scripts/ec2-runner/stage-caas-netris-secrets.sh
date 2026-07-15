#!/bin/bash

# stage-caas-netris-secrets.sh -- Copy the CaaS/Netris secrets
# push-caas-netris-secrets.sh already staged on this box (before the JIT
# runner even started, since no checkout existed yet at that point) into
# infra/netris/ and /root/pull-secret, where the make targets expect them.
# Runs on the ephemeral box itself, as part of the test job, after its own
# checkout.
#
# Required env vars:
#   NETRIS_DIR          path to the vendored infra/netris checkout (e.g.
#                       infra/netris, relative to repo root)
#
# Optional env vars:
#   REMOTE_STAGING_DIR   must match push-caas-netris-secrets.sh's value
#                       (default: /root/caas-netris-secrets)

set -euo pipefail

RESET="\e[0m"
BOLD="\e[1m"
GREEN="\e[32m"

: "${NETRIS_DIR:?NETRIS_DIR is required}"

REMOTE_STAGING_DIR="${REMOTE_STAGING_DIR:-/root/caas-netris-secrets}"

echo -e "${BOLD}Staging CaaS/Netris secrets into place...${RESET}"

cp "${REMOTE_STAGING_DIR}/license.key" "${NETRIS_DIR}/license.key"
cp "${REMOTE_STAGING_DIR}/license.zip" "${NETRIS_DIR}/license.zip"
cp "${REMOTE_STAGING_DIR}/pull-secret" /root/pull-secret
cp "${REMOTE_STAGING_DIR}/config" "${NETRIS_DIR}/config"
chmod 600 "${NETRIS_DIR}/license.key" "${NETRIS_DIR}/license.zip" "${NETRIS_DIR}/config" /root/pull-secret

# Remove the staging copies once they're in their final place -- a second
# copy lying around outside infra/netris/'s own access patterns is
# unnecessary exposure on an otherwise single-tenant box.
rm -rf "${REMOTE_STAGING_DIR}"

echo -e "${GREEN}${BOLD}CaaS/Netris secrets in place.${RESET}"
