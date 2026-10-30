#!/usr/bin/env bash
# Collect cluster diagnostics into ${RUNNER_TEMP}/osac-artifacts and mark them
# redacted. This is the only part of the artifact handling that needs the live
# cluster; the scan and upload that follow do not (see the upload-artifacts
# action), so a workflow can destroy the cluster in between.
#
# The collection is all-or-nothing: if gather-osac-logs.sh fails or is stopped
# by the timeout, .redaction-complete is not written and the upload action
# refuses to upload the unredacted files.
#
# Required env: KUBECONFIG, RUNNER_TEMP, GITHUB_WORKSPACE
# Optional env: E2E_NAMESPACE, JUNIT_PATH (read by gather-osac-logs.sh),
#               COLLECT_TIMEOUT_SECONDS (default 900)
set -eo pipefail

ARTIFACT_DIR="${RUNNER_TEMP}/osac-artifacts"
COLLECT_TIMEOUT_SECONDS="${COLLECT_TIMEOUT_SECONDS:-900}"

# The upload action keeps the marker, so one left by an earlier collection of
# this job (a Netris job collects twice, a cancelled run collects again) must
# not vouch for this one.
rm -f "${ARTIFACT_DIR}/.redaction-complete"

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
  "${GITHUB_WORKSPACE}/scripts/gather-osac-logs.sh" "${ARTIFACT_DIR}" || rc=$?

# The release gate censors the values of the secrets the job holds, wherever they
# show up. Teardown deletes the files that hold them before the upload runs, so
# keep a copy now. The snapshots live next to the artifact directory, never inside
# it, readable by this user only, and the upload action deletes them after use.
INVENTORY_DIR="${RUNNER_TEMP}/osac-secret-inventory"
mkdir -p "${INVENTORY_DIR}/secret-files"
chmod 700 "${INVENTORY_DIR}" "${INVENTORY_DIR}/secret-files"
snapshot_ok=1
for secret_file in "${RUNNER_TEMP}/pull-secret.json" "${RUNNER_TEMP}/auth.json" \
  "${RUNNER_TEMP}/containers-auth-base.json" "${RUNNER_TEMP}/chart-registry-auth.json"; do
  if [[ -f "${secret_file}" ]]; then
    # Teardown deletes the original, so a copy that failed means the gate would not know
    # this secret. Do not mark the artifacts complete; the upload action then refuses them.
    (umask 077 && cp -- "${secret_file}" "${INVENTORY_DIR}/secret-files/$(basename "${secret_file}")") \
      || { snapshot_ok=0; echo "::error::Could not save a copy of $(basename "${secret_file}") for the release gate"; }
  fi
done
(umask 077 && timeout 60 oc get secrets -n "${E2E_NAMESPACE:-osac-e2e-ci}" -o json \
  > "${INVENTORY_DIR}/k8s-secrets.json" 2>/dev/null) \
  || { rm -f "${INVENTORY_DIR}/k8s-secrets.json"; echo "::notice::Secret inventory not collected"; }

if [[ "${rc}" -eq 0 && "${snapshot_ok}" -eq 1 ]]; then
  touch "${ARTIFACT_DIR}/.redaction-complete"
elif [[ "${rc}" -eq 124 ]]; then
  echo "::warning::Log collection stopped after ${COLLECT_TIMEOUT_SECONDS}s" \
    "- the artifacts are incomplete and will not be uploaded"
elif [[ "${snapshot_ok}" -eq 0 ]]; then
  echo "::error::A secret file was not saved for the release gate" \
    "- the artifacts will not be uploaded"
fi
