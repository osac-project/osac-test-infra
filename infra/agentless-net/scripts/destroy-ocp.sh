#!/usr/bin/env bash
#
# Tear down the management cluster (SNO) and clean up DNS processes.
# Idempotent — safe to re-run.
#
set -euo pipefail

MGMT_CLONE_NAME="${MGMT_CLONE_NAME:-agentless-lab-mgmt}"
MGMT_LIBVIRT_NET="test-infra-net-${MGMT_CLONE_NAME}"

info() { echo "==> $*"; }

# ---------- kill orphaned dnsmasq ----------
#
# deploy-ocp.sh's layer 1 DNS fix manually restarts the libvirt dnsmasq
# process. cluster-tool destroy doesn't know about this process, so it
# survives and holds port 53 on the management IP — blocking the next
# cluster-tool boot.

pkill -f "dnsmasq.*${MGMT_LIBVIRT_NET}" 2>/dev/null || true

# ---------- destroy cluster-tool clone ----------

if cluster-tool list 2>/dev/null | grep -q "$MGMT_CLONE_NAME"; then
    info "Destroying cluster-tool clone ${MGMT_CLONE_NAME}..."
    cluster-tool destroy "$MGMT_CLONE_NAME"
else
    info "Clone ${MGMT_CLONE_NAME} does not exist — skipping"
fi

info "destroy-ocp complete."
