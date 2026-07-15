#!/bin/bash

# reap-orphans.sh -- Find and terminate ephemeral EC2 instances that are no
# longer legitimately in flight, independent of whether the workflow run
# that created them is still capable of tearing them down itself.
#
# e2e-ec2-runner.yml's own teardown job (if: always()) handles the normal
# case, but that only runs at all if the provision/test jobs reach a
# terminal state that lets `needs: [provision, test]` resolve. A cancelled
# run, an orchestrator crash mid-job, or a GitHub outage can all break that
# chain and strand a real, billed c5n.metal instance with nothing watching
# it. This script is the independent backstop, run on a schedule.
#
# Detection logic -- either check alone is sufficient to terminate:
#   1. Run-completed check (precise, fires fast): every ephemeral instance
#      is tagged osac-run-id=<github.run_id>-<github.run_attempt> (see
#      provision.sh's --tag-specifications). If the corresponding GitHub
#      Actions run has already reached status=completed, teardown.sh should
#      already have run -- an instance still alive at that point is
#      orphaned by definition, regardless of how young it is.
#   2. Max-age check (safety net for anything check 1 misses -- a gh API
#      lookup failure, a malformed/missing tag, or a run somehow still
#      in_progress far past any reasonable bound): terminate anything older
#      than MAX_INSTANCE_AGE_MINUTES regardless of what check 1 found.
#
# Every instance examined is logged either way (terminate + reason, or
# "still within bounds, skipping") -- no silent skips.
#
# Also attempts (best-effort) to deregister any lingering GitHub runner
# whose label matches the terminated instance's run-id tag, mirroring
# teardown.sh's own defensive deregistration.
#
# Required env vars:
#   GITHUB_REPOSITORY  owner/repo to query/deregister runners against
#   GH_TOKEN           real PAT/GitHub App token with Administration: write
#                      on this repo (used by `gh`) -- see verify-and-register.sh
#                      for why this can't be github.token
#
# Optional env vars:
#   MAX_INSTANCE_AGE_MINUTES  safety-net threshold (default 480). The
#                      provision/test/teardown job timeouts sum to 415
#                      minutes (test alone is 360m/6h -- the real CaaS/
#                      Netris flow's own Prow step timeouts summed to
#                      several hours in the worst case), but LaunchTime
#                      (what age is measured from) occurs partway into the
#                      provision job, and GitHub Actions queue time before a
#                      job starts executing doesn't count against its
#                      timeout-minutes budget -- 415 would leave only a few
#                      minutes of real margin once queueing is considered,
#                      especially since this watchdog itself shares the same
#                      singleton osac-ci-orchestrator runner slot with
#                      provision/teardown (though each watchdog run only
#                      occupies it for seconds). 480 leaves ~65m headroom;
#                      tune via this workflow's max-age-minutes dispatch
#                      input once more real run data exists.
#   DRY_RUN            "true" to log what would happen without terminating
#                      instances or deregistering runners (default "false")
#   AWS_REGION         defaults to the AWS CLI's configured region

set -euo pipefail

RESET="\e[0m"
BOLD="\e[1m"
GREEN="\e[32m"
RED="\e[31m"
YELLOW="\e[33m"

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"

MAX_INSTANCE_AGE_MINUTES="${MAX_INSTANCE_AGE_MINUTES:-480}"
DRY_RUN="${DRY_RUN:-false}"

DESCRIBE_OUTPUT=$(mktemp)
trap 'rm -f "$DESCRIBE_OUTPUT"' EXIT

echo -e "${BOLD}Scanning for orphaned osac-ephemeral EC2 instances...${RESET}"

aws ec2 describe-instances \
    --filters "Name=tag:osac-ephemeral,Values=true" "Name=instance-state-name,Values=pending,running" \
    --query 'Reservations[].Instances[].[InstanceId,LaunchTime,Tags]' \
    --output json > "$DESCRIBE_OUTPUT"

CANDIDATE_COUNT=$(jq 'length' "$DESCRIBE_OUTPUT")
echo -e "${GREEN}Found ${CANDIDATE_COUNT} candidate instance(s).${RESET}"

FOUND=0
TERMINATE_FAILED=0
NOW_EPOCH=$(date +%s)

while IFS=$'\t' read -r INSTANCE_ID LAUNCH_TIME RUN_ID_TAG; do
    [ -z "$INSTANCE_ID" ] && continue

    LAUNCH_EPOCH=$(date -d "$LAUNCH_TIME" +%s)
    AGE_MINUTES=$(( (NOW_EPOCH - LAUNCH_EPOCH) / 60 ))

    RUN_STATUS="unknown"
    if [ "$RUN_ID_TAG" != "unknown" ]; then
        PLAIN_RUN_ID="${RUN_ID_TAG%-*}"
        RUN_STATUS=$(gh run view "$PLAIN_RUN_ID" --json status -q '.status' 2>/dev/null || echo "unknown")
    fi

    ORPHAN_REASON=""
    if [ "$RUN_STATUS" = "completed" ]; then
        ORPHAN_REASON="run ${RUN_ID_TAG%-*} (attempt tag ${RUN_ID_TAG}) is completed but instance is still ${LAUNCH_TIME}"
    elif [ "$AGE_MINUTES" -ge "$MAX_INSTANCE_AGE_MINUTES" ]; then
        ORPHAN_REASON="age ${AGE_MINUTES}m exceeds MAX_INSTANCE_AGE_MINUTES=${MAX_INSTANCE_AGE_MINUTES}m (run status: ${RUN_STATUS})"
    fi

    if [ -n "$ORPHAN_REASON" ]; then
        FOUND=$((FOUND + 1))
        echo -e "${YELLOW}${BOLD}ORPHAN: ${INSTANCE_ID}${RESET} -- ${ORPHAN_REASON}"

        if [ "$DRY_RUN" = "true" ]; then
            if [ "$RUN_ID_TAG" != "unknown" ]; then
                echo "[DRY RUN] Would terminate ${INSTANCE_ID} and deregister runner label ec2-${RUN_ID_TAG}"
            else
                echo "[DRY RUN] Would terminate ${INSTANCE_ID} (no run-id tag -- would not attempt deregistration)"
            fi
        else
            if aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" > /dev/null; then
                echo -e "${GREEN}Terminated ${INSTANCE_ID}.${RESET}"
            else
                echo -e "${RED}${BOLD}ERROR: failed to terminate ${INSTANCE_ID} -- manual cleanup required.${RESET}" >&2
                TERMINATE_FAILED=$((TERMINATE_FAILED + 1))
                continue
            fi

            if [ "$RUN_ID_TAG" != "unknown" ]; then
                RUNNER_LABEL="ec2-${RUN_ID_TAG}"
                # `|| true` on the assignment: under set -e -o pipefail, a
                # transient failure of `gh api` here (rate limit, network
                # blip) would otherwise abort the whole script mid-loop,
                # silently skipping every remaining candidate instance this
                # cycle -- exactly the "no silent skips" promise above.
                RUNNER_ID=$(gh api --paginate "repos/${GITHUB_REPOSITORY}/actions/runners" 2>/dev/null \
                    | jq -r --arg label "$RUNNER_LABEL" '.runners[] | select(.labels[].name == $label) | .id' \
                    | head -n1) || true
                if [ -n "$RUNNER_ID" ] && [ "$RUNNER_ID" != "null" ]; then
                    if gh api --method DELETE "repos/${GITHUB_REPOSITORY}/actions/runners/${RUNNER_ID}" > /dev/null 2>&1; then
                        echo -e "${GREEN}Deregistered lingering runner ${RUNNER_LABEL} (id ${RUNNER_ID}).${RESET}"
                    else
                        echo -e "${YELLOW}Runner ${RUNNER_LABEL} deregistration failed or already gone.${RESET}"
                    fi
                else
                    echo -e "${YELLOW}No runner found with label ${RUNNER_LABEL} -- nothing to deregister (or the lookup itself failed).${RESET}"
                fi
            fi
        fi
    else
        echo -e "  ${INSTANCE_ID}: still within bounds, skipping (age ${AGE_MINUTES}m, run status: ${RUN_STATUS})"
    fi
done < <(jq -r '.[] | [.[0], .[1], ((.[2][] | select(.Key=="osac-run-id") | .Value) // "unknown")] | @tsv' "$DESCRIBE_OUTPUT")

echo -e "${BOLD}Done. ${FOUND} orphan(s) found out of ${CANDIDATE_COUNT} candidate(s).${RESET}"

if [ "$TERMINATE_FAILED" -gt 0 ]; then
    echo -e "${RED}${BOLD}ERROR: ${TERMINATE_FAILED} termination(s) failed -- manual cleanup required.${RESET}" >&2
    exit 1
fi
