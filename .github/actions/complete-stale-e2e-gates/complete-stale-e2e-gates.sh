#!/usr/bin/env bash
# Complete orphaned in_progress e2e-*-gate checks on HEAD_SHA.

set -euo pipefail

lib_dir="$(dirname "${BASH_SOURCE[0]}")/../invalidate-e2e-gates"
# shellcheck source=../invalidate-e2e-gates/e2e-gates-lib.sh
source "${lib_dir}/e2e-gates-lib.sh"

if [[ -z "${HEAD_SHA:-}" || -z "${REPO:-}" ]]; then
  echo "HEAD_SHA and REPO are required" >&2
  exit 1
fi

if [[ "${AFTER_NATIVE_SUCCESS:-true}" == "true" ]]; then
  complete_stale_in_progress_merge_gates
else
  dismiss_unlock_orphan_gate_checks
fi
