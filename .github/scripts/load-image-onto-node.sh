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

_load_image() {
  podman save "${IMAGE_TAG}" | oc debug "node/${NODE_NAME}" -t=false -- chroot /host podman load
}

attempt=0
delay=10
while true; do
  attempt=$((attempt + 1))
  echo "Loading image onto node ${NODE_NAME} (attempt ${attempt}/${MAX_ATTEMPTS})..."

  if _load_image; then
    echo "Image loaded successfully on attempt ${attempt}."
    exit 0
  fi

  if [[ "${attempt}" -ge "${MAX_ATTEMPTS}" ]]; then
    echo "ERROR: Failed to load image after ${MAX_ATTEMPTS} attempts." >&2
    exit 1
  fi

  echo "Attempt ${attempt} failed; retrying in ${delay}s..."
  sleep "${delay}"
  delay=$((delay * 2))
done
