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

if [[ "${rc}" -eq 0 ]]; then
  touch "${ARTIFACT_DIR}/.redaction-complete"
elif [[ "${rc}" -eq 124 ]]; then
  echo "::warning::Log collection stopped after ${COLLECT_TIMEOUT_SECONDS}s" \
    "- the artifacts are incomplete and will not be uploaded"
fi
