#!/bin/bash

# teardown.sh -- Terminate an ephemeral EC2 runner and defensively deregister
# it from GitHub if it's still registered.
#
# Called with if: always() in the reusable workflow so it runs regardless of
# whether provisioning, verification, or the test job succeeded. Safe to call
# with an empty INSTANCE_ID (e.g. provisioning failed before an instance was
# created) -- it logs and exits 0 rather than failing an already-failing run.
#
# Ephemeral JIT runners auto-deregister from GitHub after completing one job,
# but if the job never started or crashed before finishing, the registration
# can linger -- this script removes it defensively when a runner-id is known.
#
# Uses the REPO-level runner-delete endpoint, matching verify-and-register.sh's
# use of the repo-level generate-jitconfig endpoint (see that script's header
# for why: org-level runner administration needs admin:org, which a
# workflow's own GITHUB_TOKEN can never obtain).
#
# Optional env vars (all may be empty/unset if the corresponding step never
# ran, e.g. provision.sh or verify-and-register.sh failed before producing
# outputs):
#   INSTANCE_ID        EC2 instance id to terminate
#   RUNNER_ID          GitHub runner id to defensively deregister
#   GITHUB_REPOSITORY  owner/repo the runner was registered against (required
#                      if RUNNER_ID is set)
#   KNOWN_HOSTS_FILE   the run-specific known_hosts file provision.sh created
#                      (see that script's header) -- removed here since it's
#                      scratch state scoped to this one run's now-terminated
#                      instance

set -euo pipefail

RESET="\e[0m"
BOLD="\e[1m"
GREEN="\e[32m"
YELLOW="\e[33m"
RED="\e[31m"

INSTANCE_ID="${INSTANCE_ID:-}"
RUNNER_ID="${RUNNER_ID:-}"
KNOWN_HOSTS_FILE="${KNOWN_HOSTS_FILE:-}"

if [ -n "$RUNNER_ID" ]; then
    : "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required when RUNNER_ID is set}"
    echo -e "${BOLD}Deregistering runner id ${RUNNER_ID}...${RESET}"
    if gh api --method DELETE "/repos/${GITHUB_REPOSITORY}/actions/runners/${RUNNER_ID}" > /dev/null 2>&1; then
        echo -e "${GREEN}Runner deregistered.${RESET}"
    else
        echo -e "${YELLOW}Runner deregistration failed or it was already removed (expected if the job completed normally -- ephemeral runners self-deregister).${RESET}"
    fi
else
    echo -e "${YELLOW}No RUNNER_ID set -- skipping deregistration.${RESET}"
fi

if [ -n "$INSTANCE_ID" ]; then
    echo -e "${BOLD}Terminating instance ${INSTANCE_ID}...${RESET}"
    if aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" > /dev/null; then
        echo -e "${GREEN}${BOLD}Instance ${INSTANCE_ID} termination requested.${RESET}"
    else
        echo -e "${RED}${BOLD}ERROR: failed to terminate ${INSTANCE_ID} -- manual cleanup required.${RESET}" >&2
        exit 1
    fi
else
    echo -e "${YELLOW}No INSTANCE_ID set -- nothing to terminate.${RESET}"
fi

if [ -n "$KNOWN_HOSTS_FILE" ]; then
    rm -f "$KNOWN_HOSTS_FILE"
fi
