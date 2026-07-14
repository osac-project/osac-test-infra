#!/bin/bash

# verify-and-register.sh -- Verify tooling on a freshly-provisioned ephemeral
# EC2 box, then mint a single-use ("JIT") GitHub Actions runner registration
# for it. If verification fails, this hard-stops BEFORE registration so a
# half-configured box never gets scheduled a real test job.
#
# generate-jitconfig only mints a config token -- it does NOT install or
# start the runner process. See start-runner.sh for that step.
#
# Uses the REPO-level generate-jitconfig endpoint, not the org-level one --
# repo-level narrows the required credential to "Administration: write" on
# this one repo instead of an org-wide admin:org-scoped PAT. Either way, a
# real standing credential is required: GITHUB_TOKEN (the automatic workflow
# token) can never call this endpoint at all, at either level, regardless of
# the `permissions:` block requested -- confirmed directly, and
# `administration` isn't even a valid GITHUB_TOKEN permission scope. GH_TOKEN
# below must be a real PAT (fine-grained, scoped to Administration: write on
# this repo) or GitHub App installation token, fetched from a secret store --
# not github.token.
#
# Required env vars:
#   SSH_KEY_PATH     path to the orchestrator's SSH private key
#   SSH_USER         SSH user on the box (from provision.sh output)
#   PUBLIC_IP        the box's public IP (from provision.sh output)
#   RUN_ID           unique per-run identifier, used to build a unique runner
#                     label so the downstream job can target this exact box
#   GH_TOKEN         a real PAT/GitHub App token with `Administration: write`
#                     on this repo (used by `gh api`) -- see the note above,
#                     this cannot be github.token
#   GITHUB_REPOSITORY  owner/repo to register the runner against (standard
#                     GitHub Actions env var, set automatically in workflows)
#
# Optional env vars:
#   TOOLING_CHECK_CMDS   semicolon-separated shell commands to verify on the
#                        box before registering it as a runner. Defaults to a
#                        placeholder check (podman) -- the real tool list is
#                        still an open question pending sync with the
#                        CaaS/Netris team.
#   RUNNER_GROUP_ID      GitHub Actions runner group id (default: 1, the
#                        default group)
#
# Outputs (written to $GITHUB_OUTPUT if set):
#   runner-label     unique label for this run's ephemeral runner
#   jit-config       base64 JIT config to pass to run.sh --jitconfig (masked)
#   runner-id        GitHub runner id -- used by teardown.sh to defensively
#                    deregister if the runner never picked up (or crashed
#                    during) a job, since ephemeral auto-deregistration only
#                    happens after a job completes

set -euo pipefail

RESET="\e[0m"
BOLD="\e[1m"
GREEN="\e[32m"
RED="\e[31m"

: "${SSH_KEY_PATH:?SSH_KEY_PATH is required}"
: "${SSH_USER:?SSH_USER is required}"
: "${PUBLIC_IP:?PUBLIC_IP is required}"
: "${RUN_ID:?RUN_ID is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

TOOLING_CHECK_CMDS="${TOOLING_CHECK_CMDS:-command -v podman}"
RUNNER_GROUP_ID="${RUNNER_GROUP_ID:-1}"
RUNNER_LABEL="ec2-${RUN_ID}"

ssh_exec() {
    ssh -i "$SSH_KEY_PATH" \
        -o StrictHostKeyChecking=accept-new \
        -o UserKnownHostsFile="${HOME}/.ssh/known_hosts" \
        -o BatchMode=yes \
        -o ConnectTimeout=10 \
        "${SSH_USER}@${PUBLIC_IP}" "$@"
}

echo -e "${BOLD}Verifying tooling on ${PUBLIC_IP}${RESET}"

VERIFY_FAILED=0
IFS=';' read -ra CHECKS <<< "$TOOLING_CHECK_CMDS"
for check in "${CHECKS[@]}"; do
    check_trimmed="$(echo "$check" | sed 's/^ *//;s/ *$//')"
    [ -z "$check_trimmed" ] && continue
    echo -n "  checking: ${check_trimmed} ... "
    if ssh_exec "$check_trimmed" > /dev/null 2>&1; then
        echo -e "${GREEN}ok${RESET}"
    else
        echo -e "${RED}FAILED${RESET}"
        VERIFY_FAILED=1
    fi
done

if [ "$VERIFY_FAILED" -ne 0 ]; then
    echo -e "${RED}${BOLD}ERROR: tooling verification failed -- skipping JIT registration.${RESET}" >&2
    echo "The instance is left running for teardown.sh (if: always()) to terminate." >&2
    exit 1
fi

echo -e "${GREEN}${BOLD}Tooling verification passed. Registering as JIT runner: ${RUNNER_LABEL}${RESET}"

JIT_RESPONSE=$(gh api \
    --method POST \
    -H "Accept: application/vnd.github+json" \
    "/repos/${GITHUB_REPOSITORY}/actions/runners/generate-jitconfig" \
    -f "name=${RUNNER_LABEL}" \
    -F "runner_group_id=${RUNNER_GROUP_ID}" \
    -f "labels[]=self-hosted" \
    -f "labels[]=${RUNNER_LABEL}" \
    -f "work_folder=_work")

JIT_CONFIG=$(echo "$JIT_RESPONSE" | jq -r '.encoded_jit_config')
RUNNER_ID=$(echo "$JIT_RESPONSE" | jq -r '.runner.id')
if [ -z "$JIT_CONFIG" ] || [ "$JIT_CONFIG" = "null" ]; then
    echo -e "${RED}${BOLD}ERROR: JIT registration did not return an encoded_jit_config${RESET}" >&2
    echo "$JIT_RESPONSE" >&2
    exit 1
fi

echo "::add-mask::${JIT_CONFIG}"

if [ -n "${GITHUB_OUTPUT:-}" ]; then
    {
        echo "runner-label=${RUNNER_LABEL}"
        echo "jit-config=${JIT_CONFIG}"
        echo "runner-id=${RUNNER_ID}"
    } >> "$GITHUB_OUTPUT"
fi

echo -e "${GREEN}${BOLD}Runner registered: ${RUNNER_LABEL}${RESET}"
echo "runner-label=${RUNNER_LABEL}"
echo "runner-id=${RUNNER_ID}"
