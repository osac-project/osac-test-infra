#!/usr/bin/env bash
# deploy-full-resilient.sh — Runs the full deploy pipeline with progress tracking.
# If a step fails, fix the issue and re-run this script; completed steps are skipped.
# Delete /root/.deploy-progress to force a fresh start.
set -euo pipefail

PROGRESS_FILE="${DEPLOY_PROGRESS_FILE:-/root/.deploy-progress}"
EXTRA_VARS="${1:-}"

# Pick the right OSAC deploy step based on infra_provider
if [[ "${EXTRA_VARS:-}" == *"infra_provider=onprem"* ]]; then
    OSAC_STEP="deploy-osac-bm"
else
    OSAC_STEP="deploy-osac"
fi

STEPS=(
    setup
    deploy-lab
    deploy-ocp
    "${OSAC_STEP}"
    setup-caas
    deploy-caas
    post-install
)

touch "$PROGRESS_FILE"

for step in "${STEPS[@]}"; do
    if grep -qxF "$step" "$PROGRESS_FILE" 2>/dev/null; then
        echo "=== SKIP $step (already completed) ==="
        continue
    fi

    echo ""
    echo "========================================"
    echo "  Running: make $step"
    echo "========================================"
    echo ""

    if make "$step" ${EXTRA_VARS:+EXTRA_VARS="$EXTRA_VARS"}; then
        echo "$step" >> "$PROGRESS_FILE"
        echo "=== DONE $step ==="
    else
        echo ""
        echo "========================================"
        echo "  FAILED at: $step"
        echo "  Fix the issue and re-run this script."
        echo "  Progress saved to: $PROGRESS_FILE"
        echo "========================================"
        exit 1
    fi
done

echo ""
echo "========================================"
echo "  Full deploy completed successfully!"
echo "========================================"
rm -f "$PROGRESS_FILE"
