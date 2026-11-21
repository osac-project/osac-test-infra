#!/usr/bin/env bash
# Collect cluster diagnostics into ${RUNNER_TEMP}/osac-artifacts and mark them
# redacted. This is the only part of the artifact handling that needs the live
# cluster; the scan and upload that follow do not (see the upload-artifacts
# action), so a workflow can destroy the cluster in between.
#
# Only successful collection and shared redaction authorize .redaction-complete.
#
# Required env: KUBECONFIG, RUNNER_TEMP, GITHUB_WORKSPACE
# Optional env: E2E_NAMESPACE, JUNIT_PATH, INVENTORY_FILE (read by
#               gather-osac-logs.sh), COLLECT_TIMEOUT_SECONDS (default 900)
set -eo pipefail

ARTIFACT_DIR="${RUNNER_TEMP}/osac-artifacts"
COLLECT_TIMEOUT_SECONDS="${COLLECT_TIMEOUT_SECONDS:-900}"
if [[ -L "${ARTIFACT_DIR}" || -L "${ARTIFACT_DIR}/mgmt-cluster" ]]; then
  echo "::error::Artifact directory links are not permitted"
  exit 1
fi

# The upload action keeps the marker, so one left by an earlier collection of
# this job (a Netris job collects twice, a cancelled run collects again) must
# not vouch for this one.
rm -f "${ARTIFACT_DIR}/.redaction-complete"

# Compact boot diagnostics are deliberately collected without kubeconfig
# access. Their helper omits raw cluster-tool logs; the upload action must scan
# and redact this path successfully before it can be uploaded.
if [[ ! -f "${KUBECONFIG:-}" && -f "${ARTIFACT_DIR}/compact-boot/.collection-complete" ]]; then
  echo "No kubeconfig is available; retaining selected compact-boot host diagnostics only."
  rm -rf -- "${ARTIFACT_DIR}/mgmt-cluster/must-gather"
  touch "${ARTIFACT_DIR}/.redaction-complete"
  exit 0
fi

NETRIS_ARTIFACT_LAYOUT=false
if [[ -d "${ARTIFACT_DIR}/netris-logs" \
  || -d "${GITHUB_WORKSPACE}/infra/netris/logs" \
  || -f "${ARTIFACT_DIR}/cluster-collection-status.txt" ]]; then
  NETRIS_ARTIFACT_LAYOUT=true
  "${GITHUB_WORKSPACE}/.github/scripts/stage-netris-artifacts.sh"
fi
export NETRIS_ARTIFACT_LAYOUT

export OSAC_COLLECTION_DEADLINE=$(( $(date +%s) + COLLECT_TIMEOUT_SECONDS ))
INVENTORY_DIR="${RUNNER_TEMP}/osac-secret-inventory"
mkdir -p "${INVENTORY_DIR}"
chmod 700 "${INVENTORY_DIR}"
rm -f "${INVENTORY_DIR}/k8s-secrets.json"
inventory_seconds=$(( COLLECT_TIMEOUT_SECONDS < 15 ? COLLECT_TIMEOUT_SECONDS : 15 ))
(umask 077 && timeout -k 2 "${inventory_seconds}" oc get secrets \
  -n "${E2E_NAMESPACE:-osac-e2e-ci}" -o json \
  > "${INVENTORY_DIR}/k8s-secrets.json" 2>/dev/null) \
  || { rm -f "${INVENTORY_DIR}/k8s-secrets.json"; echo "::notice::Secret inventory not collected"; }

remaining=$(( OSAC_COLLECTION_DEADLINE - $(date +%s) ))
if (( remaining > 0 )) && timeout -k 5 "${remaining}" \
  "${GITHUB_WORKSPACE}/scripts/gather-osac-logs.sh" "${ARTIFACT_DIR}"; then
  touch "${ARTIFACT_DIR}/.redaction-complete"
else
  echo "::warning::Collection or redaction incomplete; artifacts will not be uploaded"
fi
