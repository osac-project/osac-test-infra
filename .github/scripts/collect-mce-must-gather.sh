#!/usr/bin/env bash
set -euo pipefail
umask 077

: "${RUNNER_TEMP:?RUNNER_TEMP is required}"
artifact_dir="${RUNNER_TEMP}/osac-artifacts"
rm -f "${artifact_dir}/.redaction-complete" "${RUNNER_TEMP}/mce-scan-complete"
output=$(mktemp -d "${RUNNER_TEMP}/artifact-collection.XXXXXX")
status="${output}/status.txt"
log="${output}/collection.log"
printf 'status=started\n' > "${status}"

stage() {
  if python3 -I "${GITHUB_WORKSPACE}/.github/scripts/inspect-artifacts.py" \
    --prepare-only --directory "${output}" > /dev/null 2>&1; then
    mkdir -p "${artifact_dir}/mgmt-cluster"
    rm -rf "${artifact_dir}/mgmt-cluster/mce-must-gather"
    mv "${output}" "${artifact_dir}/mgmt-cluster/mce-must-gather"
  else
    rm -rf "${output}"
    echo "::warning::MCE output withheld: preparation unavailable or failed."
  fi
}
trap stage EXIT

skip() {
  printf 'status=skipped\nreason=%s\n' "$1" > "${status}"
  echo "::notice::MCE diagnostics skipped: $1"
  exit 0
}

[[ -s "${KUBECONFIG:-}" ]] || skip "management kubeconfig unavailable"
for dependency in oc timeout jq python3; do
  command -v "${dependency}" >/dev/null || skip "required collection tool unavailable"
done

csv=""
if ! csv=$(timeout --kill-after=5s 15s oc get subscription multicluster-engine \
  -n multicluster-engine -o jsonpath='{.status.installedCSV}' 2>> "${log}"); then
  skip "MCE subscription unavailable"
fi
[[ -n "${csv}" ]] || skip "MCE installed CSV unavailable"
metadata=""
if ! metadata=$(timeout --kill-after=5s 15s oc get csv "${csv}" \
  -n multicluster-engine -o json 2>> "${log}"); then
  skip "MCE installed CSV unreadable"
fi
image=""
if ! image=$(printf '%s' "${metadata}" | jq -er \
  '.metadata.annotations["operators.openshift.io/must-gather-image"] | select(type == "string" and length > 0)' \
  2>> "${log}"); then
  skip "MCE must-gather image annotation unavailable"
fi
printf 'csv=%s\nimage=%s\n' "${csv}" "${image}" >> "${status}"

gather_rc=0
timeout --kill-after=10s 300s oc adm must-gather \
  --image="${image}" --dest-dir="${output}/management" >> "${log}" 2>&1 || gather_rc=$?
printf 'must_gather_exit_code=%s\n' "${gather_rc}" >> "${status}"

inspect_rc=0
timeout --kill-after=10s 60s oc adm inspect ns/hypershift \
  --dest-dir="${output}/hypershift" >> "${log}" 2>&1 || inspect_rc=$?
printf 'hypershift_inspect_exit_code=%s\n' "${inspect_rc}" >> "${status}"

resources_rc=0
resources=(hostedclusters.hypershift.openshift.io nodepools.hypershift.openshift.io)
# HC/NodePool coverage varies by collector; webhooks only need a failed-gather fallback.
if [[ "${gather_rc}" -ne 0 ]]; then
  resources+=(mutatingwebhookconfigurations.admissionregistration.k8s.io
    validatingwebhookconfigurations.admissionregistration.k8s.io)
fi
timeout --kill-after=10s 30s oc adm inspect "${resources[@]}" \
  --all-namespaces --dest-dir="${output}/hcp-resources" >> "${log}" 2>&1 || resources_rc=$?
printf 'hcp_resources_inspect_exit_code=%s\n' "${resources_rc}" >> "${status}"

if [[ "${gather_rc}" -eq 0 && "${inspect_rc}" -eq 0 && "${resources_rc}" -eq 0 ]]; then
  printf 'status=completed\n' >> "${status}"
  echo "MCE diagnostics collected; sanitization and scanning are still required."
else
  printf 'status=partial\n' >> "${status}"
  echo "::warning::MCE diagnostics incomplete; retaining partial output for sanitization and scanning."
fi
