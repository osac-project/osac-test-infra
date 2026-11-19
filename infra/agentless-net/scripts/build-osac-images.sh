#!/usr/bin/env bash
# Build the OSAC components for this commit and load them onto the cluster node.
set -euo pipefail

: "${GITHUB_OUTPUT:?}"
: "${GITHUB_RUN_ID:?}"
KUBECONFIG="${KUBECONFIG:-${HOME}/.kube/agentless-lab-mgmt.kubeconfig}"

OSAC_REPO="${OSAC_REPO:-https://github.com/osac-project/osac.git}"
OSAC_BRANCH="${OSAC_BRANCH:-main}"
if [[ -z "$OSAC_REPO" ]]; then
    OSAC_REPO="https://github.com/osac-project/osac.git"
fi
if [[ -z "$OSAC_BRANCH" ]]; then
    OSAC_BRANCH="main"
fi

# Resolve the ref once. Later component fetches and the chart install use this
# commit, so a branch moving mid-build cannot mix images and charts.
pin="$(mktemp -d)"
git init "$pin"
git -C "$pin" fetch --depth 1 "$OSAC_REPO" "$OSAC_BRANCH"
git -C "$pin" checkout FETCH_HEAD
osac_sha="$(git -C "$pin" rev-parse HEAD)"
rm -rf "$pin"
printf 'osac_sha=%s\n' "$osac_sha" >>"$GITHUB_OUTPUT"

mapfile -t COMPONENTS <<'EOF'
fulfillment-service/Containerfile|.|fulfillment_service_image|containerfile
osac-operator/Containerfile|.|osac_operator_image|containerfile
osac-aap/execution-environment/execution-environment.yaml|.|osac_aap_image|ansible-builder
bare-metal-fulfillment-operator/Containerfile|.|bmf_operator_image|containerfile
EOF

NODE_NAME="$(oc get nodes -o jsonpath='{.items[0].metadata.name}')"
index=0
for spec in "${COMPONENTS[@]}"; do
    IFS='|' read -r file context output_name build_type <<<"$spec"
    tag="localhost/component-${index}:${GITHUB_RUN_ID}"
    src="${RUNNER_TEMP:-/tmp}/component-${index}-src"
    echo "::group::Build ${output_name} from ${OSAC_REPO}@${osac_sha}"
    rm -rf "$src"
    git init "$src"
    git -C "$src" fetch --depth 1 "$OSAC_REPO" "$osac_sha"
    git -C "$src" checkout FETCH_HEAD
    component_dir="$src"
    containerfile_name="$file"
    if [[ "$file" == */* ]]; then
        component_dir="${src}/$(dirname "$file")"
        containerfile_name="$(basename "$file")"
    fi
    if [[ "$build_type" == "ansible-builder" ]]; then
        /usr/bin/python3 -m pip install --ignore-installed --quiet 'ansible-builder~=3.0'
        ansible-builder create --file "${component_dir}/${containerfile_name}" --context "${component_dir}/context"
        podman build -t "$tag" -f "${component_dir}/context/Containerfile" "${component_dir}/context"
    else
        build_context="${src}/${context}"
        podman build --env GODEBUG=http2client=0 --unsetenv GODEBUG -t "$tag" -f "${component_dir}/${containerfile_name}" "$build_context"
    fi
    echo "Loading ${tag} onto node ${NODE_NAME}"
    podman save "$tag" | oc debug "node/${NODE_NAME}" -t=false -- chroot /host podman load
    printf '%s=%s\n' "$output_name" "$tag" >>"$GITHUB_OUTPUT"
    echo "::endgroup::"
    index=$((index + 1))
done
