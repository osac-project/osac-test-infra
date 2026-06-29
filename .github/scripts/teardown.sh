#!/usr/bin/env bash
# Destroy the cluster clone, remove orphaned bridges, clean up temporary
# files, and remove the test container image.
#
# Required env: CLONE_NAME, E2E_IMAGE
set -euo pipefail

: "${CLONE_NAME:?CLONE_NAME is required}"
: "${E2E_IMAGE:?E2E_IMAGE is required}"

# --- Destroy cluster clone ---
echo "Destroying clone '${CLONE_NAME}'..."
sudo python3 /usr/local/bin/cluster-tool destroy "${CLONE_NAME}" 2>&1 || true

# Remove orphaned bridges that survive virsh net-destroy
BRIDGE_PREFIX="br-${CLONE_NAME:0:8}"
for br in $(ip -o link show | grep -oP "${BRIDGE_PREFIX}[^ @]*"); do
  echo "Removing orphaned bridge ${br}..."
  sudo ip link set "${br}" down 2>/dev/null || true
  sudo ip link delete "${br}" 2>/dev/null || true
done

# --- Clean up temporary files ---
rm -f "$RUNNER_TEMP/pull-secret.json" "$RUNNER_TEMP/aap-license.zip" "$RUNNER_TEMP/kubeconfig"
rm -f "${REGISTRY_AUTH_FILE:-}" "$RUNNER_TEMP/auth.json"
rm -f "${HOME}/.config/containers/auth.json"
sudo rm -f /root/.config/containers/auth.json
rm -rf "$RUNNER_TEMP/osac-installer"
podman rmi "${E2E_IMAGE}" 2>/dev/null || true

# --- Clean up component image (runner + cluster node) ---
if [[ -n "${COMPONENT_IMAGE:-}" ]]; then
  podman rmi "${COMPONENT_IMAGE}" 2>/dev/null || true
  if [[ -n "${COMPONENT_NODE_IP:-}" ]]; then
    CT_KEY="${HOME}/.config/cluster-tool/cluster-tool.key"
    ssh -i "${CT_KEY}" -o StrictHostKeyChecking=no "core@${COMPONENT_NODE_IP}" \
      "sudo podman rmi '${COMPONENT_IMAGE}'" 2>/dev/null || true
  fi
fi
