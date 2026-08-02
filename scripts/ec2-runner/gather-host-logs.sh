#!/bin/bash

# gather-host-logs.sh -- Collect host-level diagnostics from the ephemeral
# EC2 runner it's invoked on, for upload as a workflow artifact.
#
# Deliberately host-level (journalctl/cloud-init/dmesg), not the
# Kubernetes-cluster-shaped .github/actions/gather-artifacts (which requires
# a kubeconfig this bare box doesn't have). Runs directly on the ephemeral
# runner, not via SSH from the orchestrator, since it's invoked as a step
# within the job that already runs on that box.
#
# Usage: gather-host-logs.sh <output-dir>

set -euo pipefail

OUTPUT_DIR="${1:?usage: gather-host-logs.sh <output-dir>}"
mkdir -p "$OUTPUT_DIR"

journalctl --no-pager > "${OUTPUT_DIR}/journalctl.log" 2>&1 || true
journalctl --no-pager -u osac-ephemeral-runner.service > "${OUTPUT_DIR}/osac-ephemeral-runner.log" 2>&1 || true
dmesg > "${OUTPUT_DIR}/dmesg.log" 2>&1 || true

if [ -f /var/log/cloud-init.log ]; then
    cp /var/log/cloud-init.log "${OUTPUT_DIR}/cloud-init.log" 2>/dev/null || true
fi
if [ -f /var/log/cloud-init-output.log ]; then
    cp /var/log/cloud-init-output.log "${OUTPUT_DIR}/cloud-init-output.log" 2>/dev/null || true
fi

echo "Collected host logs to ${OUTPUT_DIR}"
