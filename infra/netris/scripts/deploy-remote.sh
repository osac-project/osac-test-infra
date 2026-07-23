#!/usr/bin/env bash
set -euo pipefail

# deploy-remote.sh — Orchestrates a full deploy on a remote bare-metal server.
# Invoked by `make deploy-bg` from the laptop. Expects env vars exported by the Makefile.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

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
    echo "  make deploy-bg SERVER=<ip> PASSWORD=<pass> LAB_NAME=<name> \\"
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
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10"
if [[ -n "${PASSWORD:-}" ]]; then
    SSH="sshpass -p '${PASSWORD}' ssh ${SSH_OPTS} root@${SERVER}"
    SCP="sshpass -p '${PASSWORD}' scp ${SSH_OPTS}"
else
    SSH="ssh ${SSH_OPTS} root@${SERVER}"
    SCP="scp ${SSH_OPTS}"
fi

run_ssh() { eval "${SSH} \"$*\""; }
run_scp() { eval "${SCP} $*"; }

echo "=== [1/9] Pre-caching container images on jump server ==="
"${REPO_ROOT}/scripts/cache-images.sh"

echo ""
echo "=== [2/9] Testing SSH connectivity to ${SERVER} ==="
run_ssh "hostname && echo OK" || { echo "ERROR: Cannot SSH to ${SERVER}"; exit 1; }

echo ""
echo "=== [3/9] Copying secrets to server ==="
run_scp "${PULL_SECRET} root@${SERVER}:/root/pull-secret"
run_scp "${LICENSE_KEY} root@${SERVER}:/root/license.key"
run_scp "${LICENSE_ZIP} root@${SERVER}:/root/license.zip"
echo "Secrets copied."

echo ""
echo "=== [4/9] Syncing repository to server ==="
if [[ -n "${PASSWORD:-}" ]]; then
    sshpass -p "${PASSWORD}" rsync -az --delete \
        -e "ssh ${SSH_OPTS}" \
        --exclude='.git' \
        --exclude='config' \
        --exclude='license.key' \
        --exclude='license.zip' \
        "${REPO_ROOT}/" "root@${SERVER}:/root/netris-test-infra/"
else
    rsync -az --delete \
        -e "ssh ${SSH_OPTS}" \
        --exclude='.git' \
        --exclude='config' \
        --exclude='license.key' \
        --exclude='license.zip' \
        "${REPO_ROOT}/" "root@${SERVER}:/root/netris-test-infra/"
fi
# Sync .git separately (needed for submodule operations on the server).
# In a monorepo layout where this code lives under a subdirectory,
# .git may not exist at REPO_ROOT — skip if absent.
if [[ -d "${REPO_ROOT}/.git" ]]; then
    if [[ -n "${PASSWORD:-}" ]]; then
        sshpass -p "${PASSWORD}" rsync -az \
            -e "ssh ${SSH_OPTS}" \
            "${REPO_ROOT}/.git" "root@${SERVER}:/root/netris-test-infra/"
    else
        rsync -az \
            -e "ssh ${SSH_OPTS}" \
            "${REPO_ROOT}/.git" "root@${SERVER}:/root/netris-test-infra/"
    fi
else
    echo "No .git at REPO_ROOT (monorepo layout), skipping .git sync."
    # Ensure netris-lab submodule content is present via the main rsync
    run_ssh "cd /root/netris-test-infra && git init 2>/dev/null || true"
fi
echo "Repository synced."

echo ""
echo "=== [5/9] Syncing cached images to server ==="
CACHE_DIR="${CACHE_DIR:-${HOME}/.cache/netris-lab/k3s-images}"
if [[ -d "$CACHE_DIR" ]] && [[ "$(ls -A "$CACHE_DIR" 2>/dev/null)" ]]; then
    run_ssh "mkdir -p /var/cache/netris-lab/k3s-images"
    if [[ -n "${PASSWORD:-}" ]]; then
        sshpass -p "${PASSWORD}" rsync -az \
            -e "ssh ${SSH_OPTS}" \
            "${CACHE_DIR}/" "root@${SERVER}:/var/cache/netris-lab/k3s-images/"
    else
        rsync -az \
            -e "ssh ${SSH_OPTS}" \
            "${CACHE_DIR}/" "root@${SERVER}:/var/cache/netris-lab/k3s-images/"
    fi
    echo "Image cache synced ($(du -sh "$CACHE_DIR" | cut -f1))."
else
    echo "No local image cache at ${CACHE_DIR}, server will pull from registries."
fi

echo ""
echo "=== [6/9] Running bootstrap on server ==="
run_ssh "dnf install -y git make ansible-core python3-pip sshpass tmux && pip3 install ansible bcrypt netaddr kubernetes"
run_ssh "rpm -q epel-release >/dev/null 2>&1 || dnf install -y https://dl.fedoraproject.org/pub/epel/epel-release-latest-\$(rpm -E %rhel).noarch.rpm || true"

echo ""
echo "=== [7/9] Running disk setup on server ==="
run_ssh "cd /root/netris-test-infra && make disk-setup"

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
run_ssh "cat > /root/netris-test-infra/config << 'CONFIGEOF'
${AWS_CONFIG}
CONFIGEOF"
# Symlink license files into repo root
run_ssh "ln -sf /root/license.key /root/netris-test-infra/license.key"
run_ssh "ln -sf /root/license.zip /root/netris-test-infra/license.zip"
echo "Config written."

echo ""
echo "=== [9/9] Starting deploy in tmux session ==="
DEPLOY_TARGET="${DEPLOY_TARGET:-deploy}"
MAKE_CMD="make ${DEPLOY_TARGET}"
[[ -n "${EXTRA_VARS:-}" ]] && MAKE_CMD="${MAKE_CMD} EXTRA_VARS=\"${EXTRA_VARS}\""
run_ssh "tmux kill-session -t deploy 2>/dev/null || true"
run_ssh "tmux new-session -d -s deploy -x 200 -y 50 'cd /root/netris-test-infra && ${MAKE_CMD} 2>&1 | tee /root/deploy.log; exec bash'"
echo ""
echo "============================================"
echo "  Deploy started on ${SERVER} in tmux"
echo "============================================"
echo ""
echo "  Attach:  ssh root@${SERVER} -t tmux attach -t deploy"
echo "  Log:     ssh root@${SERVER} tail -f /root/deploy.log"
echo ""
