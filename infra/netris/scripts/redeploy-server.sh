#!/usr/bin/env bash
# redeploy-server.sh — Runs ON the bare-metal server. Executes the full deploy
# pipeline step-by-step with progress tracking, per-step retries, and JSON status.
#
# If a step fails, it retries once after cleanup. If the retry also fails,
# the script exits with progress saved. Re-run to continue from the failed step.
#
# Status is tracked in /root/.deploy-status.json (one entry per run, keyed by
# unique run ID). This is purely observational — any status-tracking failure
# is silently ignored to avoid breaking the deploy flow.
#
# Steps:
#   setup-infra → deploy-infra → deploy-ocp → deploy-osac
#     → setup-caas|setup-maas → deploy-caas|deploy-maas → post-install
#
# OSAC_DEPLOY_MODE (fresh|snapshot) is passed through to make for deploy-ocp /
# deploy-osac (same meaning as make deploy vs make deploy-fast):
#   fresh    — default; deploy-osac does Helm install
#   snapshot — deploy-ocp uses snapshot flavor; deploy-osac runs refresh
#
# Usage:
#   make redeploy-fresh                         → --fresh, SUITE=caas, fresh OSAC
#   make redeploy-fresh SUITE=maas              → --fresh, MaaS flow
#   make redeploy-fresh OSAC_DEPLOY_MODE=snapshot
#                                               → --fresh --snapshot
#   make redeploy-continue                      → resumes (pass same SUITE / mode)
#   make deploy-jump                            → from laptop, triggers --fresh via tmux
#
# Direct:
#   scripts/redeploy-server.sh --fresh
#   scripts/redeploy-server.sh --fresh --snapshot
#   SUITE=maas scripts/redeploy-server.sh --fresh --snapshot
set -euo pipefail

# --- Configuration ---
PROGRESS_FILE="${DEPLOY_PROGRESS_FILE:-/root/.deploy-progress}"
STATUS_FILE="/root/.deploy-status.json"
STEP_LOG="/tmp/.deploy-step-output"
MAX_RETRIES=2
RETRY_DELAY=120
FRESH=false
SNAPSHOT=false
SUITE="${SUITE:-caas}"
OSAC_DEPLOY_MODE="${OSAC_DEPLOY_MODE:-fresh}"

case "$SUITE" in
    caas)
        SETUP_FLOW=setup-caas
        DEPLOY_FLOW=deploy-caas
        ;;
    maas)
        SETUP_FLOW=setup-maas
        DEPLOY_FLOW=deploy-maas
        ;;
    *)
        echo "ERROR: SUITE must be 'caas' or 'maas' (got: $SUITE)" >&2
        exit 1
        ;;
esac

# Parse flags
while [[ $# -gt 0 ]]; do
    case "$1" in
        --fresh) FRESH=true; shift ;;
        --snapshot) SNAPSHOT=true; shift ;;
        *) break ;;
    esac
done
EXTRA_VARS="${1:-}"

if [[ "$SNAPSHOT" == "true" ]]; then
    OSAC_DEPLOY_MODE=snapshot
fi

case "$OSAC_DEPLOY_MODE" in
    fresh|snapshot) ;;
    *)
        echo "ERROR: OSAC_DEPLOY_MODE must be 'fresh' or 'snapshot' (got: $OSAC_DEPLOY_MODE)" >&2
        exit 1
        ;;
esac

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_ID="$(date +%Y%m%d-%H%M%S)"

# --- Status tracking helpers (all non-fatal) ---

status_init() {
    {
        if [[ ! -f "$STATUS_FILE" ]]; then
            echo '{"runs":{},"current_run":null}' > "$STATUS_FILE"
        fi
        local steps_json
        steps_json=$(jq -nc \
            --arg setup_flow "$SETUP_FLOW" \
            --arg deploy_flow "$DEPLOY_FLOW" \
            '{
              "setup-infra":{"status":"pending"},
              "deploy-infra":{"status":"pending"},
              "deploy-ocp":{"status":"pending"},
              "deploy-osac":{"status":"pending"},
              ($setup_flow):{"status":"pending"},
              ($deploy_flow):{"status":"pending"},
              "post-install":{"status":"pending"}
            }')
        jq --arg id "$RUN_ID" \
            --arg suite "$SUITE" \
            --arg mode "$OSAC_DEPLOY_MODE" \
            --argjson steps "$steps_json" \
            '.runs[$id] = {"started_at": (now | todate), "suite": $suite, "osac_deploy_mode": $mode, "overall_status": "running", "steps": $steps} | .current_run = $id' \
            "$STATUS_FILE" > "${STATUS_FILE}.tmp" && mv "${STATUS_FILE}.tmp" "$STATUS_FILE"
    } 2>/dev/null || true
}

status_resume() {
    {
        if [[ -f "$STATUS_FILE" ]]; then
            RUN_ID=$(jq -r '.current_run // empty' "$STATUS_FILE" 2>/dev/null) || true
            # Prefer saved mode/suite from the in-progress run when continuing
            local saved_mode saved_suite
            saved_mode=$(jq -r --arg run "$RUN_ID" '.runs[$run].osac_deploy_mode // empty' "$STATUS_FILE" 2>/dev/null) || true
            saved_suite=$(jq -r --arg run "$RUN_ID" '.runs[$run].suite // empty' "$STATUS_FILE" 2>/dev/null) || true
            if [[ -n "$saved_mode" ]]; then
                OSAC_DEPLOY_MODE="$saved_mode"
            fi
            if [[ -n "$saved_suite" ]]; then
                SUITE="$saved_suite"
                case "$SUITE" in
                    caas) SETUP_FLOW=setup-caas; DEPLOY_FLOW=deploy-caas ;;
                    maas) SETUP_FLOW=setup-maas; DEPLOY_FLOW=deploy-maas ;;
                esac
            fi
        fi
        if [[ -z "${RUN_ID:-}" ]]; then
            RUN_ID="$(date +%Y%m%d-%H%M%S)"
            status_init
        fi
    } 2>/dev/null || true
}

status_step() {
    local step="$1" status="$2" error="${3:-}"
    {
        jq --arg run "$RUN_ID" --arg step "$step" --arg status "$status" \
            --arg ts "$(date -Iseconds)" --arg err "$error" \
            '(.runs[$run].steps[$step].status = $status) |
             (if $status == "running" then .runs[$run].steps[$step].started_at = $ts
              elif $status == "completed" then .runs[$run].steps[$step].completed_at = $ts
              elif $status == "failed" then .runs[$run].steps[$step].failed_at = $ts
              else . end) |
             (if ($err | length) > 0 then .runs[$run].steps[$step].error = $err else . end)' \
            "$STATUS_FILE" > "${STATUS_FILE}.tmp" && mv "${STATUS_FILE}.tmp" "$STATUS_FILE"
    } 2>/dev/null || true
}

status_step_attempt() {
    local step="$1" attempt="$2"
    {
        jq --arg run "$RUN_ID" --arg step "$step" --argjson attempt "$attempt" \
            '.runs[$run].steps[$step].attempt = $attempt' \
            "$STATUS_FILE" > "${STATUS_FILE}.tmp" && mv "${STATUS_FILE}.tmp" "$STATUS_FILE"
    } 2>/dev/null || true
}

status_complete() {
    local overall="$1"
    {
        jq --arg run "$RUN_ID" --arg status "$overall" --arg ts "$(date -Iseconds)" \
            '.runs[$run].overall_status = $status | .runs[$run].completed_at = $ts' \
            "$STATUS_FILE" > "${STATUS_FILE}.tmp" && mv "${STATUS_FILE}.tmp" "$STATUS_FILE"
    } 2>/dev/null || true
}

is_step_completed() {
    local step="$1"
    jq -e --arg run "$RUN_ID" --arg step "$step" \
        '.runs[$run].steps[$step].status == "completed"' "$STATUS_FILE" >/dev/null 2>&1
}

extract_ansible_error() {
    local task msg stderr
    task=$(grep -B1 'fatal:' "$STEP_LOG" 2>/dev/null | grep '^TASK' | tail -1 | sed 's/TASK \[\(.*\)\].*/\1/' || true)
    msg=$(grep -A2 '^MSG:' "$STEP_LOG" 2>/dev/null | tail -1 | sed 's/^[[:space:]]*//' || true)
    stderr=$(sed -n '/^STDERR:/,/^MSG:/p' "$STEP_LOG" 2>/dev/null | grep -v '^STDERR:\|^MSG:' | head -2 | tr '\n' ' ' || true)
    local result="${task:+$task: }${msg:-$stderr}"
    echo "${result:-(no details captured)}"
}

# --- Restore config/credentials ---

if [[ ! -f "${REPO_DIR}/config" ]] && [[ -f /root/.netris-config ]]; then
    cp /root/.netris-config "${REPO_DIR}/config"
    chmod 0600 "${REPO_DIR}/config"
fi
[[ -f /root/license.key ]] && ln -sf /root/license.key "${REPO_DIR}/license.key"
[[ -f /root/license.zip ]] && ln -sf /root/license.zip "${REPO_DIR}/license.zip"

# --- Pre-retry cleanup ---

pre_retry_cleanup() {
    local step="$1"
    echo "=== Running pre-retry cleanup for $step ==="
    sync
    # 192.168.122.15 = isp-server (FRR/BGP router on virbr0, Ubuntu VM).
    # Clears apt locks that can get stuck if lab setup is interrupted.
    # This is a topology constant — same IP hardcoded in netris-lab/ submodule.
    ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 root@192.168.122.15 \
        "pkill -9 apt; rm -f /var/lib/apt/lists/lock /var/lib/dpkg/lock* /var/cache/apt/archives/lock; sleep 2" 2>/dev/null || true
}

# --- Initialize status ---

if [[ "$FRESH" == "true" ]]; then
    rm -f "$PROGRESS_FILE"
    status_init
else
    status_resume
fi

# --- Run separator in log ---
echo ""
echo "################################################################"
echo "# Deploy run: $RUN_ID ($(date))"
echo "# Mode: $(if [[ "$FRESH" == "true" ]]; then echo "fresh"; else echo "continue"; fi)"
echo "# Suite: $SUITE"
echo "# OSAC_DEPLOY_MODE: $OSAC_DEPLOY_MODE"
echo "################################################################"
echo ""

# --- Main pipeline ---
# Same step names as make deploy / deploy-fast; mode selects fresh vs snapshot
# behavior inside deploy-ocp + deploy-osac (see Makefile OSAC_DEPLOY_MODE).

STEPS=(
    setup-infra
    deploy-infra
    deploy-ocp
    deploy-osac
    "$SETUP_FLOW"
    "$DEPLOY_FLOW"
    post-install
)

touch "$PROGRESS_FILE"

for step in "${STEPS[@]}"; do
    # Skip check: use BOTH json status AND text file for safety
    if is_step_completed "$step" || grep -qxF "$step" "$PROGRESS_FILE" 2>/dev/null; then
        echo "=== SKIP $step (already completed) ==="
        continue
    fi

    status_step "$step" "running"
    attempt=1
    while [[ $attempt -le $MAX_RETRIES ]]; do
        status_step_attempt "$step" "$attempt"

        echo ""
        echo "========================================"
        echo "  Running: make $step (attempt $attempt/$MAX_RETRIES)"
        echo "  SUITE=$SUITE OSAC_DEPLOY_MODE=$OSAC_DEPLOY_MODE"
        echo "========================================"
        echo ""

        if make "$step" \
            SUITE="${SUITE}" \
            OSAC_DEPLOY_MODE="${OSAC_DEPLOY_MODE}" \
            ${EXTRA_VARS:+EXTRA_VARS="${EXTRA_VARS}"} \
            2>&1 | tee "$STEP_LOG"; then
            echo "$step" >> "$PROGRESS_FILE"
            status_step "$step" "completed"
            echo "=== DONE $step ==="
            break
        fi

        local_error=$(extract_ansible_error)

        if [[ $attempt -lt $MAX_RETRIES ]]; then
            status_step "$step" "retrying" "$local_error"
            pre_retry_cleanup "$step"
            echo "=== RETRY $step in ${RETRY_DELAY}s (attempt $attempt failed: $local_error) ==="
            sleep "$RETRY_DELAY"
        else
            status_step "$step" "failed" "$local_error"
            status_complete "failed"
            echo ""
            echo "========================================"
            echo "  FAILED at: $step (all $MAX_RETRIES attempts exhausted)"
            echo "  Error: $local_error"
            echo "  Progress saved to: $PROGRESS_FILE"
            echo "  Status: $STATUS_FILE (run: $RUN_ID)"
            echo "========================================"
            exit 1
        fi
        ((attempt++))
    done
done

status_complete "completed"
echo ""
echo "========================================"
echo "  Full deploy completed successfully!"
echo "  Run ID: $RUN_ID"
echo "  Suite: $SUITE"
echo "  OSAC_DEPLOY_MODE: $OSAC_DEPLOY_MODE"
echo "  Status: cat $STATUS_FILE | jq '.runs[\"$RUN_ID\"]'"
echo "========================================"
rm -f "$PROGRESS_FILE"
