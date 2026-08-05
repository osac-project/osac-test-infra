#!/usr/bin/env bash
set -euo pipefail

# deploy-jump.sh — Runs from laptop (or jump host) to bootstrap and launch a deploy
# on a remote bare-metal server.
#
# Flow:
#   1. Pre-cache container images locally (skopeo pull)
#   2. Test SSH connectivity to the target server
#   3. Copy secrets (pull-secret, license) to server
#   4. Rsync the full repository to server
#   5. Sync cached images to server
#   6. Bootstrap server packages (dnf, pip, EPEL)
#   7. Run disk-setup on server (partitions, mounts, SELinux)
#   8. Write config file + symlink credentials
#   9. Start `make redeploy-fresh` in a tmux session on the server
#
# Authentication: SSH key auth is preferred (no PASSWORD needed).
# Set PASSWORD only for initial bootstrap when key auth isn't yet configured.
#
# Usage: source scripts/env.sh && make deploy-jump
# Requires: SERVER, LAB_NAME, PULL_SECRET, LICENSE_KEY, LICENSE_ZIP env vars

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"

# --- Validation ---
missing=()
[[ -z "${SERVER:-}" ]]      && missing+=("SERVER")
[[ -z "${LAB_NAME:-}" ]]    && missing+=("LAB_NAME")
[[ -z "${PULL_SECRET:-}" ]] && missing+=("PULL_SECRET")
[[ -z "${LICENSE_KEY:-}" ]] && missing+=("LICENSE_KEY")
[[ -z "${LICENSE_ZIP:-}" ]] && missing+=("LICENSE_ZIP")

if [[ ${#missing[@]} -gt 0 ]]; then
    echo "ERROR: Missing required variables: ${missing[*]}"
    echo ""
    echo "Usage:"
    echo "  make deploy-jump SERVER=<ip> PASSWORD=<pass> LAB_NAME=<name> \\"
    echo "    PULL_SECRET=/path/to/pull-secret \\"
    echo "    LICENSE_KEY=/path/to/license.key \\"
    echo "    LICENSE_ZIP=/path/to/license.zip"
    exit 1
fi

for f in "$PULL_SECRET" "$LICENSE_KEY" "$LICENSE_ZIP"; do
    if [[ ! -f "$f" ]]; then
        echo "ERROR: File not found: $f"
        exit 1
    fi
done

# --- SSH/SCP helpers ---
SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=10)

run_ssh() {
    if [[ -n "${PASSWORD:-}" ]]; then
        sshpass -p "${PASSWORD}" ssh "${SSH_OPTS[@]}" "root@${SERVER}" "$@"
    else
        ssh "${SSH_OPTS[@]}" "root@${SERVER}" "$@"
    fi
}

run_scp() {
    if [[ -n "${PASSWORD:-}" ]]; then
        sshpass -p "${PASSWORD}" scp "${SSH_OPTS[@]}" "$@"
    else
        scp "${SSH_OPTS[@]}" "$@"
    fi
}

run_rsync() {
    if [[ -n "${PASSWORD:-}" ]]; then
        sshpass -p "${PASSWORD}" rsync -az --delete \
            -e "ssh ${SSH_OPTS[*]}" "$@"
    else
        rsync -az --delete \
            -e "ssh ${SSH_OPTS[*]}" "$@"
    fi
}

rsync_no_delete() {
    if [[ -n "${PASSWORD:-}" ]]; then
        sshpass -p "${PASSWORD}" rsync -az \
            -e "ssh ${SSH_OPTS[*]}" "$@"
    else
        rsync -az \
            -e "ssh ${SSH_OPTS[*]}" "$@"
    fi
}

echo "=== [1/9] Pre-caching container images on jump server ==="
"${REPO_ROOT}/infra/netris/scripts/cache-images.sh"

echo ""
echo "=== [2/9] Testing SSH connectivity to ${SERVER} ==="
run_ssh "hostname && echo OK" || { echo "ERROR: Cannot SSH to ${SERVER}"; exit 1; }

echo ""
echo "=== [3/9] Copying secrets to server ==="
run_scp "${PULL_SECRET}" "root@${SERVER}:/root/pull-secret"
run_scp "${LICENSE_KEY}" "root@${SERVER}:/root/license.key"
run_scp "${LICENSE_ZIP}" "root@${SERVER}:/root/license.zip"
echo "Secrets copied."

echo ""
echo "=== [4/9] Syncing repository to server ==="
run_rsync \
    --exclude='.git' \
    --exclude='config' \
    --exclude='license.key' \
    --exclude='license.zip' \
    "${REPO_ROOT}/" "root@${SERVER}:/root/osac-test-infra/"
echo "Repository synced."

echo ""
echo "=== [5/9] Syncing cached images to server ==="
CACHE_DIR="${CACHE_DIR:-${HOME}/.cache/netris-lab/k3s-images}"
if [[ -d "$CACHE_DIR" ]] && [[ "$(ls -A "$CACHE_DIR" 2>/dev/null)" ]]; then
    run_ssh "mkdir -p /var/cache/netris-lab/k3s-images"
    rsync_no_delete "${CACHE_DIR}/" "root@${SERVER}:/var/cache/netris-lab/k3s-images/"
    echo "Image cache synced ($(du -sh "$CACHE_DIR" | cut -f1))."
else
    echo "No local image cache at ${CACHE_DIR}, server will pull from registries."
fi

echo ""
echo "=== [6/9] Running bootstrap on server ==="
run_ssh "dnf install -y git make ansible-core python3-pip sshpass tmux policycoreutils-python-utils && pip3 install ansible bcrypt netaddr kubernetes"
run_ssh "rpm -q epel-release >/dev/null 2>&1 || dnf install -y https://dl.fedoraproject.org/pub/epel/epel-release-latest-\$(rpm -E %rhel).noarch.rpm || true"

echo ""
echo "=== [7/9] Running disk setup on server ==="
run_ssh "cd /root/osac-test-infra/infra/netris && make disk-setup"

echo ""
echo "=== [8/9] Writing config file ==="
if [[ -n "${AWS_ACCESS_KEY_ID:-}" && -n "${AWS_SECRET_ACCESS_KEY:-}" ]]; then
    AWS_CONFIG="[default]
lab_name = ${LAB_NAME}
dns_mode = route53
aws_access_key_id = ${AWS_ACCESS_KEY_ID}
aws_secret_access_key = ${AWS_SECRET_ACCESS_KEY}"
else
    AWS_CONFIG="[default]
lab_name = ${LAB_NAME}
dns_mode = local"
fi
run_ssh "cat > /root/osac-test-infra/infra/netris/config << 'CONFIGEOF'
${AWS_CONFIG}
CONFIGEOF"
run_ssh "cp /root/osac-test-infra/infra/netris/config /root/.netris-config && chmod 0600 /root/.netris-config"
# Symlink license files into repo
run_ssh "ln -sf /root/license.key /root/osac-test-infra/infra/netris/license.key"
run_ssh "ln -sf /root/license.zip /root/osac-test-infra/infra/netris/license.zip"
echo "Config written."

echo ""
echo "=== [9/9] Starting deploy in tmux session ==="
DEPLOY_TARGET="${DEPLOY_TARGET:-redeploy-fresh}"
run_ssh "tmux kill-session -t deploy 2>/dev/null || true"
run_ssh "tmux new-session -d -s deploy -x 200 -y 50 'cd /root/osac-test-infra/infra/netris && make ${DEPLOY_TARGET} 2>&1 | tee -a /root/deploy.log; exec bash'"
echo ""
echo "============================================"
echo "  Deploy started on ${SERVER} in tmux"
echo "============================================"
echo ""
echo "  Attach:  ssh root@${SERVER} -t tmux attach -t deploy"
echo "  Log:     ssh root@${SERVER} tail -f /root/deploy.log"
echo ""
