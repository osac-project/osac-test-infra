#!/bin/bash

# run-bmaas-agentless.sh -- Copy the revision under test onto the ephemeral
# host, run the BMaaS Agentless install over SSH, and copy diagnostics back.
#
# `copy-logs` copies diagnostics only. The workflow runs that after the
# install so a step timeout still collects logs before the instance is
# terminated.
#
# The host is not registered as a GitHub Actions runner. The workflow job that
# calls this script also provisions the instance and tears it down, so a
# failed-job rerun starts a new instance instead of targeting one that has
# already been terminated.
#
# Required environment:
#   SSH_KEY_PATH          orchestrator private key
#   SSH_USER              SSH user on the host (from provision.sh)
#   PUBLIC_IP             the host's public IP (from provision.sh)
#   KNOWN_HOSTS_FILE      the run-specific known_hosts file provision.sh used
#   UNDER_TEST_DIR        local checkout of the commit under test
#   LOG_DIR               local directory that receives the remote logs
#   TOOLING_CHECK_CMDS    semicolon-separated commands to run on the host
#
# Optional environment:
#   OSAC_REPO             osac git URI; empty selects the installer's default
#   OSAC_BRANCH           osac revision; empty selects the installer's default

set -euo pipefail

: "${SSH_KEY_PATH:?SSH_KEY_PATH is required}"
: "${SSH_USER:?SSH_USER is required}"
: "${PUBLIC_IP:?PUBLIC_IP is required}"
: "${KNOWN_HOSTS_FILE:?KNOWN_HOSTS_FILE is required}"

OSAC_REPO="${OSAC_REPO:-}"
OSAC_BRANCH="${OSAC_BRANCH:-}"
REMOTE_DIR=/root/osac-test-infra
REMOTE_LOG_DIR="${REMOTE_DIR}/infra/agentless-net/logs"
REMOTE_INFRA="${REMOTE_DIR}/infra/agentless-net"

# timeout(1) executes a program. It cannot call this function, so the limit
# wraps the ssh binary itself. Callers pass --timeout MINUTES; other calls
# have no limit.
ssh_exec() {
    local minutes=""
    if [ "${1:-}" = "--timeout" ]; then
        minutes="$2"
        shift 2
    fi
    local -a ssh_cmd
    if [ -n "$minutes" ]; then
        ssh_cmd=(timeout "${minutes}m" ssh)
    else
        ssh_cmd=(ssh)
    fi
    "${ssh_cmd[@]}" -i "$SSH_KEY_PATH" \
        -o StrictHostKeyChecking=accept-new \
        -o UserKnownHostsFile="${KNOWN_HOSTS_FILE}" \
        -o BatchMode=yes \
        -o ConnectTimeout=10 \
        -o ServerAliveInterval=30 \
        -o ServerAliveCountMax=240 \
        "${SSH_USER}@${PUBLIC_IP}" "$@"
}

run_remote() {
    local minutes="$1"
    shift
    ssh_exec --timeout "$minutes" "$@"
}

copy_diagnostics() {
    local minutes="$1"
    local copy_status=0
    local copy_cmd
    : "${LOG_DIR:?LOG_DIR is required}"
    echo "Copying diagnostics back to ${LOG_DIR}"
    rm -rf "$LOG_DIR"
    mkdir -p "$LOG_DIR"
    copy_cmd=$(printf 'mkdir -p %q && tar -C %q -czf - .' "$REMOTE_LOG_DIR" "$REMOTE_LOG_DIR")
    ssh_exec --timeout "$minutes" "$copy_cmd" | tar -xzf - -C "$LOG_DIR" || copy_status=$?
    if [ "$copy_status" -ne 0 ]; then
        echo "ERROR: failed to copy diagnostics (status ${copy_status})" >&2
        return "$copy_status"
    fi
}

if [ "${1:-}" = "copy-logs" ]; then
    copy_diagnostics 3
    exit
fi

: "${UNDER_TEST_DIR:?UNDER_TEST_DIR is required}"
: "${LOG_DIR:?LOG_DIR is required}"
: "${TOOLING_CHECK_CMDS:?TOOLING_CHECK_CMDS is required}"
if [ ! -d "$UNDER_TEST_DIR" ]; then
    echo "ERROR: UNDER_TEST_DIR is not a directory: ${UNDER_TEST_DIR}" >&2
    exit 1
fi

echo "Copying the revision under test to ${PUBLIC_IP}:${REMOTE_DIR}"
ssh_exec "$(printf 'mkdir -p %q' "$REMOTE_DIR")"
tar -C "$UNDER_TEST_DIR" -czf - . | ssh_exec "$(printf 'tar -C %q -xzf -' "$REMOTE_DIR")"

echo "Checking tooling on ${PUBLIC_IP}"
tooling_failed=0
IFS=';' read -ra CHECKS <<< "$TOOLING_CHECK_CMDS"
for check in "${CHECKS[@]}"; do
    check_trimmed="$(echo "$check" | sed 's/^ *//;s/ *$//')"
    [ -z "$check_trimmed" ] && continue
    echo "  checking: ${check_trimmed}"
    if ! output=$(ssh_exec --timeout 15 "$check_trimmed" 2>&1); then
        echo "  FAILED: ${check_trimmed}" >&2
        printf '%s\n' "$output" >&2
        tooling_failed=1
    fi
done
if [ "$tooling_failed" -ne 0 ]; then
    echo "ERROR: tooling verification failed. The instance is left for teardown." >&2
    exit 1
fi

echo "Staging secrets into place"
stage_cmd=$(printf 'set -euo pipefail; staging=%q; dest=%q; install -d -m 700 /root/.config/containers; install -m 600 -- "${staging}/pull-secret" /root/pull-secret; install -m 600 -- "${staging}/pull-secret" /root/.config/containers/auth.json; install -m 600 -- "${staging}/license.zip" /root/aap-license.zip; install -m 600 -- "${staging}/config" "${dest}/config"' \
    /root/agentless-net-secrets \
    "$REMOTE_INFRA")
ssh_exec "$stage_cmd"

install_status=0
run_required() {
    local minutes="$1"
    local target="$2"
    local remote_cmd
    if [ "$install_status" -ne 0 ]; then
        return 0
    fi
    echo "Running make ${target}"
    remote_cmd=$(printf 'cd %q && make %q' "$REMOTE_INFRA" "$target")
    status=0
    run_remote "$minutes" "$remote_cmd" || status=$?
    if [ "$status" -ne 0 ]; then
        install_status=$status
        echo "ERROR: make ${target} failed with status ${install_status}" >&2
    fi
}

run_optional() {
    local minutes="$1"
    local target="$2"
    local remote_cmd
    echo "Running make ${target}"
    remote_cmd=$(printf 'cd %q && make %q' "$REMOTE_INFRA" "$target")
    status=0
    run_remote "$minutes" "$remote_cmd" || status=$?
    if [ "$status" -ne 0 ]; then
        echo "WARNING: make ${target} failed with status ${status}" >&2
    fi
}

run_required 30 setup-infra
run_required 60 deploy-ocp
if [ "$install_status" -eq 0 ]; then
    echo "Running make deploy-osac"
    deploy_cmd=$(printf 'cd %q && OSAC_PROFILE=bmaas-ci OSAC_REPO=%q OSAC_BRANCH=%q make deploy-osac' \
        "$REMOTE_INFRA" "$OSAC_REPO" "$OSAC_BRANCH")
    status=0
    run_remote 90 "$deploy_cmd" || status=$?
    if [ "$status" -ne 0 ]; then
        install_status=$status
        echo "ERROR: make deploy-osac failed with status ${install_status}" >&2
    fi
fi
run_required 30 deploy-fabric
run_required 45 setup-bmaas
run_required 60 prove-bmi
run_optional 10 gather-infra
run_optional 10 gather-bmaas
run_optional 15 destroy-bmaas

copy_status=0
copy_diagnostics 10 || copy_status=$?
if [ "$install_status" -ne 0 ]; then
    exit "$install_status"
fi
exit "$copy_status"
