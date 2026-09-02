#!/bin/bash
#
# Collect OSAC cluster diagnostics, redact sensitive data, and surface failure patterns.
#
# Usage:
#   KUBECONFIG=/path/to/kubeconfig ./scripts/gather-osac-logs.sh [output-dir]
#
# Environment:
#   KUBECONFIG        — path to cluster kubeconfig (required)
#   E2E_NAMESPACE     — OSAC namespace (default: osac-e2e-ci)
#   JUNIT_PATH        — path to JUnit XML to include (optional)
#
set -o nounset
set -o pipefail

ARTIFACT_DIR="${1:-./osac-logs}"
E2E_NAMESPACE="${E2E_NAMESPACE:-osac-e2e-ci}"
JUNIT_PATH="${JUNIT_PATH:-}"

if [[ ! -f "${KUBECONFIG:-}" ]]; then
    echo "ERROR: KUBECONFIG not set or file does not exist" >&2
    exit 1
fi

mkdir -p "${ARTIFACT_DIR}"

# ── Collect ──────────────────────────────────────────────────────────

echo "Gathering OSAC logs from namespace ${E2E_NAMESPACE}..."

collect_namespace_logs() {
    local ns="$1"
    local dir="$2"
    mkdir -p "${dir}"
    oc get pods -n "${ns}" -o wide > "${dir}/pods.txt" 2>&1 || true
    oc get events -n "${ns}" --sort-by=.lastTimestamp > "${dir}/events.txt" 2>&1 || true
    oc describe pods -n "${ns}" > "${dir}/pods-describe.txt" 2>&1 || true
    oc get deployments -n "${ns}" -o wide > "${dir}/deployments.txt" 2>&1 || true
    oc get jobs -n "${ns}" -o wide > "${dir}/jobs.txt" 2>&1 || true
    oc get statefulsets -n "${ns}" -o wide > "${dir}/statefulsets.txt" 2>&1 || true
    for pod in $(oc get pods -n "${ns}" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
        for container in $(oc get pod "${pod}" -n "${ns}" -o jsonpath='{.spec.containers[*].name}' 2>/dev/null); do
            oc logs "${pod}" -n "${ns}" -c "${container}" > "${dir}/pod-${pod}-${container}.log" 2>&1 &
            oc logs "${pod}" -n "${ns}" -c "${container}" --previous > "${dir}/pod-${pod}-${container}-previous.log" 2>/dev/null &
        done
        for container in $(oc get pod "${pod}" -n "${ns}" -o jsonpath='{.spec.initContainers[*].name}' 2>/dev/null); do
            oc logs "${pod}" -n "${ns}" -c "${container}" > "${dir}/pod-${pod}-init-${container}.log" 2>&1 &
        done
    done
    wait
}

collect_namespace_logs "${E2E_NAMESPACE}" "${ARTIFACT_DIR}"

for ns in keycloak ansible-aap; do
    if oc get namespace "${ns}" &>/dev/null; then
        echo "Gathering logs from namespace ${ns}..."
        collect_namespace_logs "${ns}" "${ARTIFACT_DIR}/${ns}"
    fi
done

echo "Collecting CNV diagnostics..."
mkdir -p "${ARTIFACT_DIR}/cnv"
oc get hyperconverged -A -o yaml > "${ARTIFACT_DIR}/cnv/hyperconverged.yaml" 2>&1 || true
oc get vms -n "${E2E_NAMESPACE}" -o wide > "${ARTIFACT_DIR}/cnv/vms.txt" 2>&1 || true
oc get vmis -n "${E2E_NAMESPACE}" -o wide > "${ARTIFACT_DIR}/cnv/vmis.txt" 2>&1 || true
oc get datavolumes -n "${E2E_NAMESPACE}" -o wide > "${ARTIFACT_DIR}/cnv/datavolumes.txt" 2>&1 || true
oc get pvc -n "${E2E_NAMESPACE}" -o wide > "${ARTIFACT_DIR}/cnv/pvcs.txt" 2>&1 || true
oc get events -n openshift-cnv --sort-by=.lastTimestamp > "${ARTIFACT_DIR}/cnv/events-openshift-cnv.txt" 2>&1 || true

VM_NAMESPACES=$(oc get computeinstances -n "${E2E_NAMESPACE}" \
    -o jsonpath='{.items[*].status.virtualMachineReference.namespace}' 2>/dev/null | tr ' ' '\n' | sort -u)
for ns in ${VM_NAMESPACES}; do
    [[ -z "${ns}" || "${ns}" == "${E2E_NAMESPACE}" ]] && continue
    echo "  Gathering VM diagnostics from subnet namespace ${ns}..."
    mkdir -p "${ARTIFACT_DIR}/cnv/${ns}"
    oc get vms -n "${ns}" -o wide > "${ARTIFACT_DIR}/cnv/${ns}/vms.txt" 2>&1 || true
    oc get vms -n "${ns}" -o yaml > "${ARTIFACT_DIR}/cnv/${ns}/vms.yaml" 2>&1 || true
    oc get vmis -n "${ns}" -o wide > "${ARTIFACT_DIR}/cnv/${ns}/vmis.txt" 2>&1 || true
    oc get datavolumes -n "${ns}" -o wide > "${ARTIFACT_DIR}/cnv/${ns}/datavolumes.txt" 2>&1 || true
    oc get datavolumes -n "${ns}" -o yaml > "${ARTIFACT_DIR}/cnv/${ns}/datavolumes.yaml" 2>&1 || true
    oc get pvc -n "${ns}" -o wide > "${ARTIFACT_DIR}/cnv/${ns}/pvcs.txt" 2>&1 || true
    oc get events -n "${ns}" --sort-by=.lastTimestamp > "${ARTIFACT_DIR}/cnv/${ns}/events.txt" 2>&1 || true
    oc get networkpolicies -n "${ns}" -o yaml > "${ARTIFACT_DIR}/cnv/${ns}/networkpolicies.yaml" 2>&1 || true
    oc get pods -n "${ns}" -o wide > "${ARTIFACT_DIR}/cnv/${ns}/pods.txt" 2>&1 || true
    for pod in $(oc get pods -n "${ns}" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
        oc logs "${pod}" -n "${ns}" --all-containers > "${ARTIFACT_DIR}/cnv/${ns}/pod-${pod}.log" 2>&1 || true
    done
done

echo "Collecting compute instance status..."
oc get computeinstances -n "${E2E_NAMESPACE}" -o wide > "${ARTIFACT_DIR}/computeinstances.txt" 2>&1 || true
oc get computeinstances -n "${E2E_NAMESPACE}" -o yaml > "${ARTIFACT_DIR}/computeinstances.yaml" 2>&1 || true

echo "Collecting cluster order status..."
oc get clusterorders -n "${E2E_NAMESPACE}" -o wide > "${ARTIFACT_DIR}/clusterorders.txt" 2>&1 || true
oc get clusterorders -n "${E2E_NAMESPACE}" -o json 2>"${ARTIFACT_DIR}/clusterorders-errors.txt" \
    | jq 'del(.items[]?.spec.templateParameters)' \
    > "${ARTIFACT_DIR}/clusterorders.json" || true

echo "Collecting networking status..."
oc get virtualnetworks -n "${E2E_NAMESPACE}" -o wide > "${ARTIFACT_DIR}/virtualnetworks.txt" 2>&1 || true
oc get virtualnetworks -n "${E2E_NAMESPACE}" -o yaml > "${ARTIFACT_DIR}/virtualnetworks.yaml" 2>&1 || true
oc get subnets -n "${E2E_NAMESPACE}" -o wide > "${ARTIFACT_DIR}/subnets.txt" 2>&1 || true
oc get subnets -n "${E2E_NAMESPACE}" -o yaml > "${ARTIFACT_DIR}/subnets.yaml" 2>&1 || true
oc get securitygroups -n "${E2E_NAMESPACE}" -o wide > "${ARTIFACT_DIR}/securitygroups.txt" 2>&1 || true
oc get clusteruserdefinednetwork -o yaml > "${ARTIFACT_DIR}/clusteruserdefinednetwork.yaml" 2>&1 || true

echo "Collecting cert-manager status..."
oc get certificates -n "${E2E_NAMESPACE}" -o wide > "${ARTIFACT_DIR}/certificates.txt" 2>&1 || true
oc get certificates -n "${E2E_NAMESPACE}" -o yaml > "${ARTIFACT_DIR}/certificates.yaml" 2>&1 || true
oc get routes -n "${E2E_NAMESPACE}" -o wide > "${ARTIFACT_DIR}/routes.txt" 2>&1 || true
oc get routes -n keycloak -o wide > "${ARTIFACT_DIR}/routes-keycloak.txt" 2>&1 || true

for ns in cert-manager openshift-machine-api osac-operators; do
    if oc get namespace "${ns}" &>/dev/null; then
        echo "Gathering logs from namespace ${ns}..."
        collect_namespace_logs "${ns}" "${ARTIFACT_DIR}/${ns}"
    fi
done

echo "Collecting OLM operator status..."
mkdir -p "${ARTIFACT_DIR}/olm"
oc get subscriptions -A -o yaml > "${ARTIFACT_DIR}/olm/subscriptions.yaml" 2>&1 || true
oc get csv -A -o wide > "${ARTIFACT_DIR}/olm/csv.txt" 2>&1 || true
oc get installplan -A -o wide > "${ARTIFACT_DIR}/olm/installplans.txt" 2>&1 || true
oc get catalogsource -n openshift-marketplace -o yaml > "${ARTIFACT_DIR}/olm/catalogsources.yaml" 2>&1 || true
oc get pods -n openshift-marketplace -o wide > "${ARTIFACT_DIR}/olm/marketplace-pods.txt" 2>&1 || true

echo "Collecting node resource usage..."
oc adm top node > "${ARTIFACT_DIR}/node-resources.txt" 2>&1 || true
oc adm top pod -n "${E2E_NAMESPACE}" --sort-by=memory > "${ARTIFACT_DIR}/pod-resources.txt" 2>&1 || true
oc get nodes -o wide > "${ARTIFACT_DIR}/nodes.txt" 2>&1 || true
oc describe node > "${ARTIFACT_DIR}/node-describe.txt" 2>&1 || true

echo "Collecting cluster operator status..."
oc get co > "${ARTIFACT_DIR}/clusteroperators.txt" 2>&1 || true
oc get csv -n openshift-cnv -o wide > "${ARTIFACT_DIR}/cnv/csv.txt" 2>&1 || true

echo "Collecting software BOM..."
# Everything below is already gathered elsewhere in this script in raw form
# (olm/csv.txt, clusteroperators.txt, deployments.txt, ...) -- this distills
# just the "what did this run actually test against" facts (OCP version,
# each Red Hat prerequisite operator's resolved CSV, and every workload
# image actually running in the namespaces that matter) into one small,
# structured file, so nobody has to go spelunking through the full
# diagnostic bundle -- or worse, hand-maintain a spreadsheet -- to answer
# that question for a given run.
collect_bom() {
    local ocp_version ocp_channel
    ocp_version=$(oc get clusterversion version -o jsonpath='{.status.desired.version}' 2>/dev/null || echo "")
    ocp_channel=$(oc get clusterversion version -o jsonpath='{.spec.channel}' 2>/dev/null || echo "")

    # component label -> "subscriptionName:namespace" for every OLM-managed
    # prerequisite OSAC itself installs (osac-installer/charts/osac-deps).
    # Not every flavor enables every one of these (e.g. LVMS/Kafka are
    # disabled by default) -- installedCSV/version come back null rather
    # than erroring when a subscription doesn't exist in this cluster.
    local -A olm_components=(
        ["OpenShift Virtualization (CNV/KubeVirt)"]="kubevirt-hyperconverged:openshift-cnv"
        ["Multicluster Engine (MCE)"]="multicluster-engine:multicluster-engine"
        ["Ansible Automation Platform (AAP)"]="dev-ansible-automation-platform:ansible-aap"
        ["cert-manager Operator"]="openshift-cert-manager-operator:cert-manager-operator"
        ["MetalLB Operator"]="metallb-operator:metallb-system"
        ["LVMS (LVM Storage)"]="lvms-operator:openshift-storage"
        ["Kafka / Strimzi (AMQ Streams)"]="amq-streams:osac-kafka"
    )

    local operator_entries=()
    local label sub_name sub_ns installed_csv configured_channel version entry
    for label in "${!olm_components[@]}"; do
        sub_name="${olm_components[$label]%%:*}"
        sub_ns="${olm_components[$label]##*:}"
        installed_csv=$(oc get subscription "${sub_name}" -n "${sub_ns}" \
            -o jsonpath='{.status.installedCSV}' 2>/dev/null || echo "")
        # Subscription.spec.channel is what we asked OLM to track, not
        # necessarily the channel the resolved installedCSV/version actually
        # shipped on (e.g. after a manual startingCSV pin, or mid channel
        # migration) -- name it accordingly so the two can't be confused.
        configured_channel=$(oc get subscription "${sub_name}" -n "${sub_ns}" \
            -o jsonpath='{.spec.channel}' 2>/dev/null || echo "")
        version=""
        if [[ -n "${installed_csv}" ]]; then
            version=$(oc get csv "${installed_csv}" -n "${sub_ns}" \
                -o jsonpath='{.spec.version}' 2>/dev/null || echo "")
        fi
        entry=$(jq -n \
            --arg component "${label}" --arg namespace "${sub_ns}" --arg subscription "${sub_name}" \
            --arg configuredChannel "${configured_channel}" --arg csv "${installed_csv}" --arg version "${version}" \
            'def blank_to_null: if length > 0 then . else null end;
            {
                component: $component,
                namespace: $namespace,
                subscription: $subscription,
                configuredChannel: ($configuredChannel | blank_to_null),
                installedCSV: ($csv | blank_to_null),
                version: ($version | blank_to_null)
            }')
        operator_entries+=("${entry}")
    done
    local operators_json
    operators_json=$(printf '%s\n' "${operator_entries[@]}" | jq -s '.')

    # Every deployment/statefulset's actual *running* image in the namespaces
    # that matter: OSAC's own components plus bundled Postgres (both live in
    # E2E_NAMESPACE), and standalone Keycloak (its own "keycloak" namespace).
    # Reports whatever is actually running rather than hand-listing every
    # component name, so this doesn't need updating when a new one is added.
    #
    # Reads status.containerStatuses on each matching Pod (not
    # spec.template.spec.containers on the Deployment/StatefulSet itself):
    # the template is only the *desired* image, which can be a floating tag
    # that hasn't finished resolving, or can legitimately differ pod-to-pod
    # mid-rollout -- containerStatuses.image/imageID is what that specific
    # pod actually pulled and is running. Pods are matched to their owning
    # workload via its own spec.selector.matchLabels rather than walking
    # ownerReferences (Deployment -> ReplicaSet -> Pod is a 2-hop chain;
    # StatefulSet -> Pod is 1-hop) -- the label selector is exactly what the
    # workload itself already uses to claim its pods, so it's the simpler,
    # equally-correct way to find them. Results are kept per-pod (not
    # deduped by container name) so a rollout in progress -- some pods on
    # the old image, some on the new -- is visible instead of collapsed
    # away.
    collect_namespace_workload_images() {
        local ns="$1"
        local ns_error
        if ! ns_error=$(oc get namespace "${ns}" 2>&1 >/dev/null); then
            if grep -qi "notfound" <<<"${ns_error}"; then
                echo "[]"
            else
                echo "::warning::collect_namespace_workload_images: could not check namespace '${ns}': ${ns_error//$'\n'/ }" >&2
                jq -cn --arg error "${ns_error}" '{error: $error}'
            fi
            return
        fi

        local workloads_raw
        workloads_raw=$(oc get deployments,statefulsets -n "${ns}" -o json 2>/dev/null) || workloads_raw='{"items":[]}'

        local entries=()
        local workload kind name selector pods_json entry
        while IFS= read -r workload; do
            [[ -z "${workload}" ]] && continue
            kind=$(jq -r '.kind' <<<"${workload}")
            name=$(jq -r '.metadata.name' <<<"${workload}")
            selector=$(jq -r '.spec.selector.matchLabels // {} | to_entries | map("\(.key)=\(.value)") | join(",")' <<<"${workload}")
            pods_json='{"items":[]}'
            if [[ -n "${selector}" ]]; then
                pods_json=$(oc get pods -n "${ns}" -l "${selector}" -o json 2>/dev/null) || pods_json='{"items":[]}'
            fi
            entry=$(jq -n --arg kind "${kind}" --arg name "${name}" --argjson pods "${pods_json}" \
                '{
                    kind: $kind,
                    name: $name,
                    pods: [$pods.items[]? | {
                        pod: .metadata.name,
                        containers: [.status.containerStatuses[]? | {name: .name, image: .image, imageID: .imageID}]
                    }]
                }')
            entries+=("${entry}")
        done < <(jq -c '.items[]?' <<<"${workloads_raw}")

        if [[ ${#entries[@]} -eq 0 ]]; then
            echo "[]"
        else
            printf '%s\n' "${entries[@]}" | jq -s '.'
        fi
    }
    local workloads_json
    workloads_json=$(jq -n \
        --argjson main "$(collect_namespace_workload_images "${E2E_NAMESPACE}")" \
        --argjson keycloak "$(collect_namespace_workload_images keycloak)" \
        --arg main_ns "${E2E_NAMESPACE}" \
        '{($main_ns): $main, "keycloak": $keycloak}')

    jq -n \
        --arg capturedAt "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        --arg ocpVersion "${ocp_version}" --arg ocpChannel "${ocp_channel}" \
        --argjson operators "${operators_json}" \
        --argjson workloadImages "${workloads_json}" \
        'def blank_to_null: if length > 0 then . else null end;
        {
            capturedAt: $capturedAt,
            ocp: {version: ($ocpVersion | blank_to_null), channel: ($ocpChannel | blank_to_null)},
            operators: $operators,
            workloadImages: $workloadImages
        }' > "${ARTIFACT_DIR}/bom.json"
}
collect_bom
echo "Software BOM written to ${ARTIFACT_DIR}/bom.json"

echo "Collecting OLM marketplace diagnostics..."
mkdir -p "${ARTIFACT_DIR}/marketplace"
timeout 30s oc get catalogsource -n openshift-marketplace -o wide > "${ARTIFACT_DIR}/marketplace/catalogsources.txt" 2>&1 || true
timeout 30s oc get catalogsource -n openshift-marketplace -o yaml > "${ARTIFACT_DIR}/marketplace/catalogsources.yaml" 2>&1 || true
timeout 30s oc get pods -n openshift-marketplace -o wide > "${ARTIFACT_DIR}/marketplace/pods.txt" 2>&1 || true
timeout 30s oc describe pods -n openshift-marketplace > "${ARTIFACT_DIR}/marketplace/pods-describe.txt" 2>&1 || true
timeout 30s oc get events -n openshift-marketplace --sort-by=.lastTimestamp > "${ARTIFACT_DIR}/marketplace/events.txt" 2>&1 || true
timeout 30s oc get subscriptions -A -o wide > "${ARTIFACT_DIR}/marketplace/subscriptions.txt" 2>&1 || true
timeout 30s oc get installplans -A -o wide > "${ARTIFACT_DIR}/marketplace/installplans.txt" 2>&1 || true
MARKETPLACE_PODS=$(timeout 30s oc get pods -n openshift-marketplace -o jsonpath='{.items[*].metadata.name}' 2>"${ARTIFACT_DIR}/marketplace/pod-discovery-errors.txt") || true
for pod in ${MARKETPLACE_PODS}; do
    { timeout 60s oc logs "${pod}" -n openshift-marketplace --all-containers --tail=200 > "${ARTIFACT_DIR}/marketplace/pod-${pod}.log" 2>&1 || true; } &
done
wait

echo "Collecting storage diagnostics..."
mkdir -p "${ARTIFACT_DIR}/storage"
oc get pods -n openshift-storage -o wide > "${ARTIFACT_DIR}/storage/pods.txt" 2>&1 || true
oc get events -n openshift-storage --sort-by=.lastTimestamp > "${ARTIFACT_DIR}/storage/events.txt" 2>&1 || true
oc get lvmcluster -n openshift-storage -o yaml > "${ARTIFACT_DIR}/storage/lvmcluster.yaml" 2>&1 || true
oc get lvmvolumegroups -n openshift-storage -o yaml > "${ARTIFACT_DIR}/storage/lvmvolumegroups.yaml" 2>&1 || true
oc get sc -o wide > "${ARTIFACT_DIR}/storage/storageclasses.txt" 2>&1 || true
oc get pv -o wide > "${ARTIFACT_DIR}/storage/pvs.txt" 2>&1 || true
oc get pvc -A -o wide > "${ARTIFACT_DIR}/storage/pvcs-all.txt" 2>&1 || true
oc get volumeattachments -o wide > "${ARTIFACT_DIR}/storage/volumeattachments.txt" 2>&1 || true
for pod in $(oc get pods -n openshift-storage -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
    oc logs "${pod}" -n openshift-storage > "${ARTIFACT_DIR}/storage/pod-${pod}.log" 2>&1 || true
done

echo "Collecting MachineConfig diagnostics..."
mkdir -p "${ARTIFACT_DIR}/mco"
oc get mcp -o wide > "${ARTIFACT_DIR}/mco/mcp.txt" 2>&1 || true
oc get mc --sort-by=.metadata.creationTimestamp > "${ARTIFACT_DIR}/mco/mc.txt" 2>&1 || true
oc get secret pull-secret -n openshift-config -o jsonpath='{.data.\.dockerconfigjson}' \
    | base64 -d | jq -r '.auths | keys[]' > "${ARTIFACT_DIR}/mco/pull-secret-registries.txt" 2>&1 || true

echo "Collecting service account and secret state..."
oc get sa -n "${E2E_NAMESPACE}" -o yaml > "${ARTIFACT_DIR}/serviceaccounts.yaml" 2>&1 || true
oc get secrets -n "${E2E_NAMESPACE}" -o custom-columns='NAME:.metadata.name,TYPE:.type' > "${ARTIFACT_DIR}/secrets-types.txt" 2>&1 || true

echo "Collecting AAP operator status..."
oc get ansibleautomationplatform -n "${E2E_NAMESPACE}" -o yaml > "${ARTIFACT_DIR}/aap-status.yaml" 2>&1 || true
oc get automationcontroller -n "${E2E_NAMESPACE}" -o yaml > "${ARTIFACT_DIR}/automationcontroller-status.yaml" 2>&1 || true

echo "Collecting AAP job stdout..."
mkdir -p "${ARTIFACT_DIR}/aap-jobs"
AAP_ROUTE=$(oc get route osac-aap -n "${E2E_NAMESPACE}" -o jsonpath='{.spec.host}' 2>/dev/null) || true
AAP_ADMIN_PW=$(oc get secret osac-aap-controller-admin-password -n "${E2E_NAMESPACE}" \
    -o jsonpath='{.data.password}' 2>/dev/null | base64 -d) || true
if [[ -n "${AAP_ADMIN_PW}" && -n "${GITHUB_ACTIONS:-}" ]]; then
    echo "::add-mask::${AAP_ADMIN_PW}"
fi
if [[ -n "${AAP_ROUTE}" && -n "${AAP_ADMIN_PW}" ]]; then
    AAP_AUTH=(-sk -u "admin:${AAP_ADMIN_PW}")
    MAX_PAGES=5
    page=1
    while [[ ${page} -le ${MAX_PAGES} ]]; do
        page_file="${ARTIFACT_DIR}/aap-jobs/jobs-page-${page}.json"
        curl "${AAP_AUTH[@]}" \
            "https://${AAP_ROUTE}/api/controller/v2/jobs/?page=${page}&page_size=50&order_by=id" \
            > "${page_file}" 2>&1 || break
        jq -e '.results' "${page_file}" &>/dev/null || break
        for job_id in $(jq -r '.results[]?.id // empty' "${page_file}"); do
            status=$(jq -r ".results[] | select(.id == ${job_id}) | .status // \"unknown\"" "${page_file}")
            name=$(jq -r ".results[] | select(.id == ${job_id}) | .name // \"unknown\"" "${page_file}" \
                | tr -c 'A-Za-z0-9._-' '_' | head -c 100)
            curl "${AAP_AUTH[@]}" \
                "https://${AAP_ROUTE}/api/controller/v2/jobs/${job_id}/stdout/?format=txt" \
                > "${ARTIFACT_DIR}/aap-jobs/job-${job_id}-${status}-${name}.txt" 2>&1 &
        done
        next=$(jq -r '.next // empty' "${page_file}")
        [[ -z "${next}" || "${next}" == "null" ]] && break
        page=$((page + 1))
    done
    wait
    echo "  Captured stdout for $(find "${ARTIFACT_DIR}/aap-jobs" -name "job-*.txt" | wc -l) AAP jobs"
    curl "${AAP_AUTH[@]}" \
        "https://${AAP_ROUTE}/api/controller/v2/project_updates/?page_size=50&order_by=id" \
        > "${ARTIFACT_DIR}/aap-jobs/project-updates.json" 2>&1 || true
    if ! jq -e '.results | type == "array"' "${ARTIFACT_DIR}/aap-jobs/project-updates.json" &>/dev/null; then
        echo "  Skipping AAP project updates: invalid response"
    else
    for pu_id in $(jq -r '.results[]?.id // empty' "${ARTIFACT_DIR}/aap-jobs/project-updates.json"); do
        status=$(jq -r ".results[] | select(.id == ${pu_id}) | .status // \"unknown\"" \
            "${ARTIFACT_DIR}/aap-jobs/project-updates.json")
        curl "${AAP_AUTH[@]}" \
            "https://${AAP_ROUTE}/api/controller/v2/project_updates/${pu_id}/stdout/?format=txt" \
            > "${ARTIFACT_DIR}/aap-jobs/project-update-${pu_id}-${status}.txt" 2>&1 &
    done
    wait
    fi
    echo "  Captured $(find "${ARTIFACT_DIR}/aap-jobs" -name "project-update-*.txt" | wc -l) AAP project updates"
else
    echo "  AAP route or admin password not found, skipping job stdout capture"
fi

if [[ -n "${JUNIT_PATH}" && -f "${JUNIT_PATH}" ]]; then
    cp "${JUNIT_PATH}" "${ARTIFACT_DIR}/junit.xml"
    for extra in "$(dirname "${JUNIT_PATH}")"/junit-*.xml; do
        [[ -f "${extra}" ]] && cp "${extra}" "${ARTIFACT_DIR}/"
    done
fi

if [[ -n "${JUNIT_PATH}" ]]; then
    E2E_LOGS=("$(dirname "${JUNIT_PATH}")"/e2e*.log)
    if [[ -e "${E2E_LOGS[0]}" ]]; then
        sort -m -t' ' -k1,1 "${E2E_LOGS[@]}" > "${ARTIFACT_DIR}/e2e.log"
    fi
fi

# ── Redact ───────────────────────────────────────────────────────────

echo "Redacting sensitive data..."

# AAP RESOURCE_SERVER SECRET_KEY in YAML, logs, and escaped JSON annotations
find "${ARTIFACT_DIR}" -type f \( -name "*.yaml" -o -name "*.log" -o -name "*.txt" -o -name "*.json" \) -print0 \
    | xargs -0 sed -i -E \
        -e 's/(SECRET_KEY[":\\]+\s*["\\]*)[A-Za-z0-9_-]{40,}/\1REDACTED/g' \
        -e 's/(SECRET_KEY[^A-Za-z0-9]*value[^A-Za-z0-9]*)[A-Za-z0-9_-]{40,}/\1REDACTED/g' \
        -e 's/("value":\s*")[A-Za-z0-9_-]{40,}/\1REDACTED/g' \
    || true

# JWT tokens in pod descriptions, logs, and AAP job stdout
find "${ARTIFACT_DIR}" \( -name "pods-describe.txt" -o -name "*.log" -o -name "*.txt" \) -print0 \
    | xargs -0 sed -i -E 's/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/REDACTED_JWT/g' || true

# Base64-encoded database passwords and API tokens (operator logs, AAP job stdout)
# [^"]+ (not [A-Za-z0-9+/=]) so passwords with punctuation (@/%/#) are covered too.
# find -exec succeeds on zero matches (unlike xargs); fail closed on sed errors.
find "${ARTIFACT_DIR}" -type f \( -name "*.log" -o -name "*.txt" -o -name "*.json" \) \
    -exec sed -i -E \
        -e 's/"password":\s*"[^"]+"/"password": "REDACTED"/g' \
        -e 's/"token":\s*"[^"]+"/"token": "REDACTED"/g' \
        {} + || {
    echo "ERROR: password/token redaction failed" >&2
    exit 1
}

# Fulfillment: break-glass credentials in controller and grpc-server logs
# (plaintext JSON form). Broader than pod-fulfillment-* so describe/event
# dumps that copy the same payload are covered too.
find "${ARTIFACT_DIR}" -type f \( -name "*.log" -o -name "*.txt" -o -name "*.json" \) \
    -exec sed -i -E 's/"break_glass_credentials"\s*:\s*\{[^}]+\}/"break_glass_credentials":{"password":"REDACTED","username":"REDACTED"}/g' {} + || {
    echo "ERROR: break-glass plaintext redaction failed" >&2
    exit 1
}

# Fulfillment: break-glass credentials base64-encoded inside SQL DEBUG
# parameters (e.g. tenants.data). The plaintext JSON redaction above never
# sees these; the JWT redaction only matches three-segment eyJ.a.b tokens;
# and the large-blob sweep below only kicks in at 500+ chars.
#
# Decode-based (not a base64-substring of the key name): embedding
# "break_glass_credentials" at an arbitrary byte offset does not guarantee
# a stable base64 substring, so we decode each quoted candidate and look
# for the key in the plaintext. Without this, the failure-summary grep
# (-C3 around "error") reprints those SQL lines into the GitHub Actions
# job log (OSAC-1684).
redact_break_glass_b64() {
    # Paths as args: rewrite files in place. No args: stdin -> stdout.
    python3 /dev/fd/3 "$@" 3<<'PY'
import base64
import re
import sys
from pathlib import Path

B64_RE = re.compile(rb'"([A-Za-z0-9+/_-]{32,}={0,2})"')
NEEDLE = b"break_glass_credentials"
PLACEHOLDER = b'"REDACTED_BREAK_GLASS_B64"'


def try_decode(raw):
    s = raw.rstrip(b"=")
    pad = b"=" * (-len(s) % 4)
    candidate = s + pad
    for dec in (
        lambda v: base64.b64decode(v, validate=True),
        lambda v: base64.b64decode(v, altchars=b"-_", validate=True),
    ):
        try:
            return dec(candidate)
        except Exception:
            continue
    return None


def scrub(data):
    def repl(match):
        decoded = try_decode(match.group(1))
        if decoded is not None and NEEDLE in decoded:
            return PLACEHOLDER
        return match.group(0)

    return B64_RE.sub(repl, data)


paths = sys.argv[1:]
if not paths:
    sys.stdout.buffer.write(scrub(sys.stdin.buffer.read()))
else:
    errors = []
    for path in paths:
        p = Path(path)
        try:
            original = p.read_bytes()
        except OSError as exc:
            print(f"ERROR: cannot read {p}: {exc}", file=sys.stderr)
            errors.append(str(p))
            continue
        updated = scrub(original)
        if updated != original:
            try:
                p.write_bytes(updated)
            except OSError as exc:
                print(f"ERROR: cannot write {p}: {exc}", file=sys.stderr)
                errors.append(str(p))
    if errors:
        sys.exit(1)
PY
}

if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 is required to redact base64-encoded break-glass credentials" >&2
    exit 1
fi
readarray -d '' b64_files < <(find "${ARTIFACT_DIR}" -type f \( -name "*.log" -o -name "*.txt" -o -name "*.json" \) -print0)
if [[ ${#b64_files[@]} -gt 0 ]]; then
    redact_break_glass_b64 "${b64_files[@]}" || exit 1
fi

# Broad sweep: .dockerconfigjson base64 blobs
find "${ARTIFACT_DIR}" -type f \( -name "*.yaml" -o -name "*.json" \) -print0 \
    | xargs -0 sed -i -E 's/(\.dockerconfigjson:\s*)[A-Za-z0-9+/=]{50,}/\1REDACTED/g' || true

# Broad sweep: curl -u "admin:password" patterns
find "${ARTIFACT_DIR}" -type f \( -name "*.log" -o -name "*.txt" \) -print0 \
    | xargs -0 sed -i -E 's/-u "admin:[^"]*"/-u "admin:REDACTED"/g' || true

# Env var passwords in pod descriptions (e.g. KEYCLOAK_ADMIN_PASSWORD: admin)
# Skips K8s-masked values that start with '<set to'
find "${ARTIFACT_DIR}" -name "pods-describe.txt" -print0 \
    | xargs -0 sed -i -E 's/(_PASSWORD[A-Z_]*:\s+)([^< \t]\S*)/\1REDACTED/g' || true

# Large base64 blobs in log files (may contain kubeconfigs, certs, serialized secrets)
find "${ARTIFACT_DIR}" -type f -name "*.log" -print0 \
    | xargs -0 sed -i -E 's/"[A-Za-z0-9+/]{500,}[A-Za-z0-9+/=]*"/"REDACTED_BLOB"/g' || true

# Clean up empty files from failed log captures
find "${ARTIFACT_DIR}" -type f -empty -delete || true

# ── Failure Summary ─────────────────────────────────────────────────
# Built only after redaction above. Written to the artifact for offline
# debugging; a short pointer (not the full -C3 dump) goes to the job log
# so a future redaction gap can't re-introduce secrets into GitHub Actions
# console output the way OSAC-1684's scanner caught.

FAILURE_SUMMARY="${ARTIFACT_DIR}/failure-summary.txt"
{
echo "=== Failure Summary (error/panic/fatal/failed/unreachable) ==="
found=0
total_lines=0
MAX_SUMMARY_LINES=1000
while IFS= read -r -d '' f; do
    remaining=$((MAX_SUMMARY_LINES - total_lines))
    [[ ${remaining} -le 0 ]] && break
    matches=$(grep -inE --color=never -B1 -A3 \
        '\b(errors?|panick?|fatal|fail(ed|ure)?|unreachable)\b' "$f" 2>/dev/null \
        | grep -ivE '\b(fail(ed|ure)?|unreachable)=0\b') || continue
    printf '%s\n' "$matches" | grep -qE '^[0-9]+:' || continue
    echo ""
    echo "--- ${f#"${ARTIFACT_DIR}"/} ---"
    printf '%s\n' "$matches" | head -n "${remaining}"
    match_lines=$(printf '%s\n' "$matches" | wc -l)
    total_lines=$((total_lines + (match_lines < remaining ? match_lines : remaining)))
    found=$((found + 1))
done < <(find "${ARTIFACT_DIR}" -type f \( -name '*.log' -o -name '*.txt' \) \
    ! -name 'failure-summary.txt' -print0 | sort -z)
if [[ ${total_lines} -ge ${MAX_SUMMARY_LINES} ]]; then
    echo ""
    echo "... truncated at ${MAX_SUMMARY_LINES} lines — see artifacts for full logs"
fi
if [[ ${found} -eq 0 ]]; then
    echo "No error/panic/fatal/failed/unreachable patterns found in collected artifacts."
fi
echo "=== End Failure Summary ==="
} > "${FAILURE_SUMMARY}"
# Defense in depth: scrub any base64-encoded break-glass credentials
# BEFORE anything from this file is echoed to the job console.
redact_break_glass_b64 "${FAILURE_SUMMARY}" || exit 1
echo "Failure summary written to ${FAILURE_SUMMARY} (${found} pattern group(s) found)"

FILE_COUNT=$(find "${ARTIFACT_DIR}" -type f | wc -l)
TOTAL_SIZE=$(du -sh "${ARTIFACT_DIR}" | cut -f1)
echo "Done. ${FILE_COUNT} files (${TOTAL_SIZE}) in ${ARTIFACT_DIR}"
