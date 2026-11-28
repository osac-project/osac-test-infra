#!/usr/bin/env bash
# Destroy the cluster clone, remove orphaned bridges, clean up temporary
# files, and remove the test container image.
#
# Required env: E2E_IMAGE
set -euo pipefail

: "${E2E_IMAGE:?E2E_IMAGE is required}"

# --- Destroy cluster clone ---
if [[ -n "${CLONE_NAME:-}" ]]; then
  if [[ "${CLUSTER_TOPOLOGY:-sno}" == "mno" ]]; then
    /usr/bin/pkexec --disable-internal-agent /usr/local/sbin/osac-privileged dns remove "${CLONE_NAME}" 2>/dev/null || true
  fi
  echo "Destroying clone '${CLONE_NAME}'..."
  /usr/bin/pkexec --disable-internal-agent /usr/local/sbin/osac-privileged cluster-tool destroy "${CLONE_NAME}" 2>&1 || true

  # Remove orphaned bridges left behind by either SNO or compact teardown.
  SNO_BRIDGE_PREFIX="br-${CLONE_NAME:0:8}"
  COMPACT_BRIDGE_PREFIX="br-$(printf '%s' "${CLONE_NAME}" | sha256sum | cut -c1-8)"
  while IFS= read -r bridge; do
    if [[ "${bridge}" == "${SNO_BRIDGE_PREFIX}"* || "${bridge}" == "${COMPACT_BRIDGE_PREFIX}"* ]]; then
      echo "Removing orphaned bridge ${bridge}..."
      /usr/bin/pkexec --disable-internal-agent /usr/local/sbin/osac-privileged bridge-down "${bridge}" 2>/dev/null || true
      /usr/bin/pkexec --disable-internal-agent /usr/local/sbin/osac-privileged bridge-delete "${bridge}" 2>/dev/null || true
    fi
  done < <(ip -o link show | awk -F': ' '{print $2}' | sed 's/@.*//')
fi

# --- Clean up temporary files ---
rm -f "$RUNNER_TEMP/pull-secret.json" "$RUNNER_TEMP/aap-license.zip" "$RUNNER_TEMP/kubeconfig"
rm -f "${REGISTRY_AUTH_FILE:-}" "$RUNNER_TEMP/auth.json"
rm -f "${HOME}/.config/containers/auth.json"
/usr/bin/pkexec --disable-internal-agent /usr/local/sbin/osac-privileged auth-clean || true
rm -rf "$RUNNER_TEMP/osac-installer" "$RUNNER_TEMP/osac-installer-clone" "$RUNNER_TEMP/osac-test-source"
podman rmi "${E2E_IMAGE}" 2>/dev/null || true

if [[ "${DELETE_LOCAL_FLAVOR:-false}" == "true" && -n "${FLAVOR_NAME:-}" ]]; then
  echo "Removing local flavor '${FLAVOR_NAME}'..."
  /usr/bin/pkexec --disable-internal-agent /usr/local/sbin/osac-privileged cluster-tool delete-flavor "${FLAVOR_NAME}" 2>&1 || true
  rm -f "/tmp/cluster-tool-flavor-${FLAVOR_NAME}.digest"
fi

if [[ "${DELETE_LOCAL_FLAVOR:-false}" == "true" ]]; then
  echo "Removing temporary Assisted flavor build state..."
  /usr/bin/pkexec --disable-internal-agent /usr/local/sbin/osac-privileged flavor cleanup \
    "${GITHUB_RUN_ID}" "${GITHUB_RUN_ATTEMPT}" 2>&1 || true
fi

# --- Clean up component image on runner ---
# Node-side cleanup is unnecessary: the clone is destroyed above.
if [[ -n "${COMPONENT_IMAGE:-}" ]]; then
  podman rmi "${COMPONENT_IMAGE}" 2>/dev/null || true
fi
