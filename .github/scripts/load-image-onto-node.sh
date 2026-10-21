#!/usr/bin/env bash
# Load a locally-built container image onto a cluster node via oc debug.
#
# On RHCOS, root podman and CRI-O share /var/lib/containers/storage, so
# 'podman load' via oc debug makes the image available to the kubelet.
# The API server connection can drop with transient errors (EOF, TLS
# timeout, connection refused) especially right after cluster boot; this
# script retries with exponential back-off so a single blip does not
# fail the entire E2E run.
#
# Usage:
#   load-image-onto-node.sh <image-tag> <node-name> [max-attempts]
#
# Environment:
#   KUBECONFIG  — path to kubeconfig (required by oc)
#
# Exit codes:
#   0  — image loaded successfully
#   1  — usage error or all attempts exhausted

set -euo pipefail

readonly IMAGE_TAG="${1:?Usage: load-image-onto-node.sh <image-tag> <node-name> [max-attempts]}"
readonly NODE_NAME="${2:?Usage: load-image-onto-node.sh <image-tag> <node-name> [max-attempts]}"
readonly MAX_ATTEMPTS="${3:-3}"

# Accept only canonical decimal integers 1–10 (no leading zeros, no
# octal ambiguity).  The regex rejects 00, 08, 011, etc. before any
# arithmetic context can misinterpret them.
if ! [[ "${MAX_ATTEMPTS}" =~ ^(10|[1-9])$ ]]; then
  echo "ERROR: max-attempts must be an integer between 1 and 10 (got '${MAX_ATTEMPTS}')." >&2
  exit 1
fi

# Temporary file for oc debug output; cleaned up on exit regardless of
# success or failure.  Diagnostics stay local — nothing from oc debug
# reaches workflow logs.
_OC_LOG="$(mktemp)"
trap 'rm -f "${_OC_LOG}"' EXIT

_load_image() {
  podman save "${IMAGE_TAG}" | \
    oc debug "node/${NODE_NAME}" -t=false -- chroot /host podman load \
    >"${_OC_LOG}" 2>&1
}

# Classify the captured oc debug output into a fixed, non-sensitive
# category.  Only allowlisted literal strings are emitted — no raw
# log content, node names, endpoints, or image tags.
_classify_failure() {
  local log
  log=$(cat "${_OC_LOG}" 2>/dev/null) || log=""
  case "${log}" in
    *"EOF"*)                          echo "api-connection-eof" ;;
    *"TLS handshake timeout"*)        echo "tls-timeout" ;;
    *"connection refused"*)           echo "connection-refused" ;;
    *"timeout"*|*"deadline"*)         echo "timeout" ;;
    *"Unauthorized"*|*"Forbidden"*)   echo "auth-error" ;;
    *)                                echo "unknown" ;;
  esac
}

attempt=0
delay=10
while true; do
  attempt=$((attempt + 1))
  echo "Loading image onto cluster node (attempt ${attempt}/${MAX_ATTEMPTS})..."

  if _load_image; then
    echo "Image loaded successfully on attempt ${attempt}."
    exit 0
  fi

  if [[ "${attempt}" -ge "${MAX_ATTEMPTS}" ]]; then
    echo "ERROR: Failed to load image after ${MAX_ATTEMPTS} attempts (reason: $(_classify_failure))." >&2
    exit 1
  fi

  echo "Attempt ${attempt} failed; retrying in ${delay}s..."
  sleep "${delay}"
  delay=$((delay * 2))
done
