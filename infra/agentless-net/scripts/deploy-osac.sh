#!/usr/bin/env bash
#
# Refresh OSAC on the snapshot cluster with agentless-net values.
# Clones the osac mono-repo, merges caas-ci base values with the
# agentless-net overlay, and runs refresh-after-snapshot.py.
# Corresponds to setup-lab.sh steps 7-10.
# Idempotent — safe to re-run.
#
set -euo pipefail

info() { echo "==> $*"; }

# A branch flag cannot select a raw commit. Fetch the requested ref and
# detach at that commit so a SHA, branch, and tag all work. Tags are fetched
# only for the requested tag, so a conflicting local tag cannot reject a branch.
checkout_osac_ref() {
    local dir="$1"
    local repo="$2"
    local branch="$3"
    if [[ ! -d "${dir}/.git" ]]; then
        info "Cloning osac mono-repo (${repo})..."
        git clone "$repo" "$dir"
    fi
    info "Checking out osac ${branch}"
    git -C "$dir" remote set-url origin "$repo"
    git -C "$dir" fetch --prune --no-tags origin
    if git -C "$dir" show-ref --verify --quiet "refs/remotes/origin/${branch}"; then
        git -C "$dir" checkout --detach "refs/remotes/origin/${branch}"
    elif git -C "$dir" ls-remote --exit-code --tags origin "refs/tags/${branch}" >/dev/null; then
        git -C "$dir" fetch --force origin "refs/tags/${branch}:refs/tags/${branch}"
        git -C "$dir" checkout --detach "refs/tags/${branch}^{}"
    elif git -C "$dir" fetch origin "$branch"; then
        git -C "$dir" checkout --detach FETCH_HEAD
    else
        echo "ERROR: ${repo} has no branch, tag, or commit ${branch}" >&2
        return 1
    fi
    installed_revision="$(git -C "$dir" rev-parse HEAD)"
}

if [[ "${1:-}" == "--checkout-only" ]]; then
    : "${OSAC_REPO:?}"
    : "${OSAC_BRANCH:?}"
    : "${OSAC_DIR:?}"
    checkout_osac_ref "$OSAC_DIR" "$OSAC_REPO" "$OSAC_BRANCH"
    printf '%s\n' "$installed_revision"
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="${SCRIPT_DIR}/.."

# shellcheck source=use-kubeconfig.sh
source "${SCRIPT_DIR}/use-kubeconfig.sh"
require_cluster_tool_kubeconfig
use_kubeconfig "${INFRA_DIR}/.mgmt-network"
KUBECONFIG="${HOME}/.kube/${MGMT_CLONE_NAME:-agentless-lab-mgmt}.kubeconfig"
export KUBECONFIG

OSAC_NAMESPACE="${OSAC_NAMESPACE:-osac-e2e-ci}"
OSAC_VALUES_FILE="${OSAC_VALUES_FILE:-values/caas-ci/values.yaml}"
OSAC_REPO="${OSAC_REPO:-https://github.com/osac-project/osac.git}"
OSAC_BRANCH="${OSAC_BRANCH:-main}"
# A blank workflow input is exported as an empty string, which must not override the default.
if [[ -z "$OSAC_REPO" ]]; then
    OSAC_REPO="https://github.com/osac-project/osac.git"
fi
if [[ -z "$OSAC_BRANCH" ]]; then
    OSAC_BRANCH="main"
fi
OSAC_DIR="/opt/osac"
INSTALLER_DIR="${OSAC_DIR}/osac-installer"
VALUES_OVERLAY="${INFRA_DIR}/values-overlay.yaml"
PULL_SECRET="${OSAC_PULL_SECRET_PATH:-/root/pull-secret}"
DNS_CREDS="${DNS_CREDENTIALS_PATH:-${INFRA_DIR}/config}"
SSH_PRIVATE_KEY="${SSH_PRIVATE_KEY_PATH:-${HOME}/.ssh/id_rsa}"

export KUBECONFIG

# ---------- clone osac mono-repo ----------
# Stamp this checkout inside checkout_osac_ref. A later rev-parse could observe a different HEAD.
checkout_osac_ref "$OSAC_DIR" "$OSAC_REPO" "$OSAC_BRANCH"

# ---------- merge values overlay ----------
# Profile installs read values/<profile>/instance.yaml. The legacy CaaS file
# is not part of that layout, so the merge runs only for the CaaS path.

MERGED_VALUES_DIR="${INSTALLER_DIR}/values/agentless-net-ci"
MERGED_VALUES="${MERGED_VALUES_DIR}/values.yaml"

if [[ "${DEPLOY_SUITE:-}" != "bmaas" ]]; then
mkdir -p "$MERGED_VALUES_DIR"

info "Merging values overlay with ${OSAC_VALUES_FILE}..."
python3 -c "
import yaml, sys, copy

def deep_merge(base, overlay):
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result

with open(sys.argv[1]) as f:
    base = yaml.safe_load(f)
with open(sys.argv[2]) as f:
    overlay = yaml.safe_load(f)

merged = deep_merge(base, overlay)

with open(sys.argv[3], 'w') as f:
    yaml.dump(merged, f, default_flow_style=False, sort_keys=False)

print(f'  Merged {sys.argv[1]} + {sys.argv[2]} -> {sys.argv[3]}')
" "${INSTALLER_DIR}/${OSAC_VALUES_FILE}" "$VALUES_OVERLAY" "$MERGED_VALUES"

# Copy pull-secret and license into the merged values directory
AAP_LICENSE="${AAP_LICENSE_PATH:-/root/aap-license.zip}"
cp "$PULL_SECRET" "${MERGED_VALUES_DIR}/pull-secret.json"
cp "$AAP_LICENSE" "${MERGED_VALUES_DIR}/license.zip"
fi

# ---------- step 8: install OSAC ----------

# Every install is fresh. A stamp must not skip the charts on a reused lab.
if [[ "${DEPLOY_SUITE:-}" == "bmaas" ]]; then
    overlay_src="${INFRA_DIR}/values-bmaas-overlay.yaml"
    overlay_dst="${INSTALLER_DIR}/values/bmaas-agentless-overlay.yaml"
    overlay_sum="$(sha256sum "$overlay_src" | awk '{print $1}')"
    info "Installing OSAC profile ${OSAC_PROFILE}..."
    cp "$overlay_src" "$overlay_dst"
    installed_overlay="$(sha256sum "$overlay_dst" | awk '{print $1}')"
    if [[ "$installed_overlay" != "$overlay_sum" ]]; then
        echo "ERROR: values overlay changed while it was copied" >&2
        exit 1
    fi
    extra_helm="-f values/bmaas-agentless-overlay.yaml"
    add_split_image() {
        local image="$1" repo_key="$2" tag_key="$3" pull_key="$4"
        local tag repo
        [[ -n "$image" ]] || return 0
        if [[ "$image" == *" "* || "$image" != *:* ]]; then
            echo "ERROR: refusing image override ${image}" >&2
            exit 1
        fi
        tag="${image##*:}"
        repo="${image%:*}"
        extra_helm+=" --set ${repo_key}=${repo} --set ${tag_key}=${tag} --set ${pull_key}=IfNotPresent"
    }
    add_image() {
        local image="$1" key="$2"
        [[ -n "$image" ]] || return 0
        if [[ "$image" == *" "* ]]; then
            echo "ERROR: refusing image override ${image}" >&2
            exit 1
        fi
        extra_helm+=" --set-string ${key}=${image}"
    }
    add_split_image "${FULFILLMENT_SERVICE_IMAGE:-}" service.images.service.repository service.images.service.tag service.images.service.pullPolicy
    add_split_image "${OSAC_OPERATOR_IMAGE:-}" operator.image.repository operator.image.tag operator.image.pullPolicy
    add_split_image "${BMF_OPERATOR_IMAGE:-}" bmf.image.repository bmf.image.tag bmf.image.pullPolicy
    add_image "${OSAC_AAP_IMAGE:-}" aap.bootstrap.image
    add_image "${OSAC_AAP_IMAGE:-}" aap.configAsCode.eeImage
    (cd "$INSTALLER_DIR" && \
        KUBECONFIG="$KUBECONFIG" \
        make install \
            PLATFORM=openshift \
            PROFILE="$OSAC_PROFILE" \
            NS="$OSAC_NAMESPACE" \
            AAP_LICENSE_FILE="${AAP_LICENSE_FILE:-/root/aap-license.zip}" \
            EXTRA_HELM_ARGS="$extra_helm")
else
    info "Installing OSAC via Helm (this may take several minutes)..."
    (cd "$INSTALLER_DIR" && \
        KUBECONFIG="$KUBECONFIG" \
        make install \
            INSTALLER_NAMESPACE="$OSAC_NAMESPACE" \
            VALUES_FILE=values/agentless-net-ci/values.yaml)
fi

# ---------- step 9: patch DNS credentials ----------

if [ -f "$DNS_CREDS" ]; then
    info "Patching cluster-fulfillment-ig with DNS credentials..."
    # shellcheck source=/dev/null
    source "$DNS_CREDS"
    KUBECONFIG="$KUBECONFIG" oc patch secret cluster-fulfillment-ig -n "$OSAC_NAMESPACE" --type merge \
        -p "{\"data\":{\"AWS_ACCESS_KEY_ID\":\"$(echo -n "$AWS_ACCESS_KEY_ID" | base64)\",\"AWS_SECRET_ACCESS_KEY\":\"$(echo -n "$AWS_SECRET_ACCESS_KEY" | base64)\"}}"
else
    echo "  WARN: ${DNS_CREDS} not found — DNS (Route 53) will not work."
fi

# ---------- step 9b: patch SSH key ----------

if [ -f "$SSH_PRIVATE_KEY" ]; then
    info "Patching cluster-fulfillment-ig with SSH key..."
    KUBECONFIG="$KUBECONFIG" oc patch secret cluster-fulfillment-ig -n "$OSAC_NAMESPACE" --type merge \
        -p "{\"data\":{\"SERVER_SSH_KEY\":\"$(base64 -w0 < "$SSH_PRIVATE_KEY")\"}}"
else
    echo "  WARN: ${SSH_PRIVATE_KEY} not found — NMState live apply will not work."
fi

# ---------- step 10: validate OSAC ----------

info "Validating OSAC..."
KUBECONFIG="$KUBECONFIG" oc wait deploy/fulfillment-grpc-server -n "$OSAC_NAMESPACE" \
    --for=condition=Available --timeout=300s
KUBECONFIG="$KUBECONFIG" oc wait deploy/osac-operator -n "$OSAC_NAMESPACE" \
    --for=condition=Available --timeout=300s
if [[ "${DEPLOY_SUITE:-}" == "bmaas" ]]; then
    mapfile -t BMF_DEPLOYS < <(KUBECONFIG="$KUBECONFIG" oc get deploy -n "$OSAC_NAMESPACE" -o name | grep bmf)
    if [[ ${#BMF_DEPLOYS[@]} -eq 0 ]]; then
        echo "ERROR: no bare-metal fulfillment deployment in ${OSAC_NAMESPACE}" >&2
        exit 1
    fi
    KUBECONFIG="$KUBECONFIG" oc wait "${BMF_DEPLOYS[@]}" -n "$OSAC_NAMESPACE" \
        --for=condition=Available --timeout=300s
fi
info "OSAC is running:"
KUBECONFIG="$KUBECONFIG" oc get pods -n "$OSAC_NAMESPACE" --no-headers | \
    awk '{print $3}' | sort | uniq -c | sort -rn

# ---------- step 10b: patch MetalLB to use fabric subnet ----------

if [[ "${DEPLOY_SUITE:-}" != "bmaas" ]]; then
    info "Patching MetalLB to use fabric subnet..."
    KUBECONFIG="$KUBECONFIG" oc patch ipaddresspool caas-address-pool -n metallb-system \
        --type=merge -p '{"spec":{"addresses":["10.0.0.240-10.0.0.250"]}}'
    echo "  Pool: 10.0.0.240-10.0.0.250"
fi

printf '%s\n' "$KUBECONFIG" > "${INFRA_DIR}/.effective-kubeconfig"
info "deploy-osac complete."
