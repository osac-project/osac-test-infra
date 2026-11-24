#!/usr/bin/env bash
# Collect cluster diagnostics into ${RUNNER_TEMP}/osac-artifacts and mark them
# redacted. This is the only part of the artifact handling that needs the live
# cluster; the scan and upload that follow do not (see the upload-artifacts
# action), so a workflow can destroy the cluster in between.
#
# Cluster collection is all-or-nothing: incomplete output is never published.
# Completed compact host diagnostics remain available independently, including
# when cluster collection is interrupted or times out.
#
# Required env: KUBECONFIG, RUNNER_TEMP, GITHUB_WORKSPACE
# Optional env: E2E_NAMESPACE, JUNIT_PATH, INVENTORY_FILE (read by
#               gather-osac-logs.sh), COLLECT_TIMEOUT_SECONDS (default 900)
set -eo pipefail

ARTIFACT_DIR="${RUNNER_TEMP}/osac-artifacts"
COLLECTION_DIR="${ARTIFACT_DIR}"
COLLECT_TIMEOUT_SECONDS="${COLLECT_TIMEOUT_SECONDS:-900}"

# The upload action keeps the marker, so one left by an earlier collection of
# this job (a Netris job collects twice, a cancelled run collects again) must
# not vouch for this one.
rm -f "${ARTIFACT_DIR}/.redaction-complete"

# Keep validated host files in the upload directory and collect cluster data
# separately. A timeout or cancellation must not mix unredacted partial output
# into the host bundle or cause the upload gate to discard that bundle.
if [[ -f "${ARTIFACT_DIR}/compact-boot/.collection-complete" ]]; then
  COLLECTION_DIR=$(mktemp -d "${RUNNER_TEMP}/osac-cluster-artifacts.XXXXXX")
  trap 'rm -rf -- "${COLLECTION_DIR}"' EXIT
  cp -a "${ARTIFACT_DIR}/." "${COLLECTION_DIR}/"
  find "${ARTIFACT_DIR}" -mindepth 1 -maxdepth 1 ! -name compact-boot -exec rm -rf -- {} +
  # Only helper-validated host files are left here. They still require the
  # existing gitleaks scan and release gate before upload.
  touch "${ARTIFACT_DIR}/.redaction-complete"
  if [[ ! -f "${KUBECONFIG:-}" ]]; then
    echo "No kubeconfig is available; retaining selected compact-boot host diagnostics only."
    exit 0
  fi
fi

NETRIS_ARTIFACT_LAYOUT=false
if [[ -d "${ARTIFACT_DIR}/netris-logs" \
  || -d "${GITHUB_WORKSPACE}/infra/netris/logs" \
  || -f "${ARTIFACT_DIR}/cluster-collection-status.txt" ]]; then
  NETRIS_ARTIFACT_LAYOUT=true
  "${GITHUB_WORKSPACE}/.github/scripts/stage-netris-artifacts.sh"
fi
export NETRIS_ARTIFACT_LAYOUT

rc=0
timeout "${COLLECT_TIMEOUT_SECONDS}" \
  "${GITHUB_WORKSPACE}/scripts/gather-osac-logs.sh" "${COLLECTION_DIR}" || rc=$?

# The release gate censors the values of the cluster's own Secrets wherever they
# show up. The snapshot lives next to the artifact directory, never inside it,
# and is readable by this user only.
INVENTORY_DIR="${RUNNER_TEMP}/osac-secret-inventory"
mkdir -p "${INVENTORY_DIR}"
(umask 077 && timeout 60 oc get secrets -n "${E2E_NAMESPACE:-osac-e2e-ci}" -o json \
  > "${INVENTORY_DIR}/k8s-secrets.json" 2>/dev/null) \
  || { rm -f "${INVENTORY_DIR}/k8s-secrets.json"; echo "::notice::Secret inventory not collected"; }

if [[ "${rc}" -eq 0 ]]; then
  if [[ "${COLLECTION_DIR}" != "${ARTIFACT_DIR}" ]]; then
    # The gather completed redaction; only now may its files reach upload.
    cp -a "${COLLECTION_DIR}/." "${ARTIFACT_DIR}/"
  fi
  touch "${ARTIFACT_DIR}/.redaction-complete"
elif [[ "${rc}" -eq 124 ]]; then
  echo "::warning::Log collection stopped after ${COLLECT_TIMEOUT_SECONDS}s" \
    "- the artifacts are incomplete and will not be uploaded"
fi
