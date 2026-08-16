#!/usr/bin/env bash
#
# Remove the cloned osac mono-repo.
# OSAC itself is destroyed with the OCP cluster (destroy-ocp).
# Idempotent — safe to re-run.
#
set -euo pipefail

OSAC_DIR="/opt/osac"

info() { echo "==> $*"; }

if [ -d "$OSAC_DIR" ]; then
    rm -rf "$OSAC_DIR"
    info "Removed ${OSAC_DIR}"
else
    info "${OSAC_DIR} does not exist — skipping"
fi

info "destroy-osac complete."
