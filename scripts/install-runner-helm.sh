#!/usr/bin/env bash
set -euo pipefail

: "${RUNNER_TEMP:?RUNNER_TEMP is required}"
[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || {
  echo "Only Linux amd64 is supported by the pinned Helm bootstrap." >&2
  exit 1
}

helm_version=v3.22.1
helm_sha256=896cf63684a42a723d61c980369d6e588330927deb13676ca0d85006dd6da0ea
archive="${RUNNER_TEMP}/helm-${helm_version}-linux-amd64.tar.gz"
tools_bin="${RUNNER_TEMP}/bin"
mkdir -p "${tools_bin}"
curl -fsSL -o "${archive}" \
  "https://get.helm.sh/helm-${helm_version}-linux-amd64.tar.gz"
echo "${helm_sha256}  ${archive}" | sha256sum -c -
tar -xzf "${archive}" -C "${RUNNER_TEMP}" linux-amd64/helm
install -m 0755 "${RUNNER_TEMP}/linux-amd64/helm" "${tools_bin}/helm"
rm -rf "${archive}" "${RUNNER_TEMP}/linux-amd64"
"${tools_bin}/helm" version --short
