#!/usr/bin/env bash
set -Eeuo pipefail

: "${CLUSTER_TOPOLOGY:?CLUSTER_TOPOLOGY is required}"
: "${OPENSHIFT_VERSION:?OPENSHIFT_VERSION is required}"
: "${FLAVOR_NAME:?FLAVOR_NAME is required}"
: "${PULL_SECRET_FILE:?PULL_SECRET_FILE is required}"

HELPER=/usr/local/sbin/osac-privileged
if [[ ! -x "${HELPER}" ]]; then
  echo "::error::Runner helper ${HELPER} is missing. Provision this runner with scripts/install-osac-privileged-helper.sh." >&2
  exit 1
fi

/usr/bin/pkexec --disable-internal-agent "${HELPER}" flavor create \
  "${CLUSTER_TOPOLOGY}" \
  "${OPENSHIFT_VERSION}" \
  "${FLAVOR_NAME}" \
  "${GITHUB_RUN_ID}" \
  "${GITHUB_RUN_ATTEMPT}" \
  "${PULL_SECRET_FILE}"

{
  echo "### Captured local cluster flavor"
  echo
  echo "- Topology: ${CLUSTER_TOPOLOGY}"
  echo "- Flavor: ${FLAVOR_NAME}"
  echo "- Source cluster: created by Assisted Test Infra"
  echo "- This workflow keeps the captured flavor local on the runner."
} >> "${GITHUB_STEP_SUMMARY}"
