#!/usr/bin/env bash
# vcluster-run-teardown.sh -- remove one OSAC E2E run's objects from the
# shared outer cluster, the counterpart of scripts/vcluster-run-setup.sh.
#
# The teardown first disables the run's periodic AAP schedules (otherwise a
# dead run keeps launching error jobs in the shared AAP every few minutes),
# then deletes, in order:
#   1. the run's vcluster Helm release (before the namespace, so the
#      chart's cluster-scoped RBAC is removed by helm itself);
#   2. namespace vc-<run>, which held the vcluster's pods and copied
#      objects;
#   3. the orders namespace (osac) on the outer cluster, but only when
#      this run created it: its instance label carries the run's name,
#      and a pre-existing namespace (the shared AAP and CA bundle live
#      in osac on today's outer clusters) is left alone;
#   4. the run's virtual bare-metal hosts (b and c suites): through
#      scripts/vcluster-run-bmhs.sh --delete, the VMs, sushy-tools
#      emulator, storage pool and cache on this machine, then namespace
#      <run>-hosts with its per-run RBAC and BareMetalHosts. Best effort,
#      verified at the end, so one stuck host never leaves the rest of the
#      run behind;
#   5. the run's outer KafkaUsers and their Strimzi secrets;
#   6. the run's Keycloak realm, through the outer Keycloak's admin API;
#   7. the run's output files (.vcluster-runs/<run> by default).
#
# It then verifies that nothing named after the run remains among the kinds
# the run setup and the vcluster create, and fails loudly otherwise.
#
# Known gaps, deliberate:
#   - The run's AAP organization is left in place, and so are its job
#     templates. Only its schedules are disabled (see above), because
#     deleting an organization the shared AAP still references is more
#     destructive than leaving it for the nightly fresh outer cluster.
#   - Objects the TESTS create in the outer cluster through OSAC (subnet
#     namespaces and ClusterUserDefinedNetworks, VirtualMachines,
#     StorageClasses, MetalLB pools) are not touched. They carry the
#     instance label; label-based deletion is the follow-up.
#
# Usage:
#   KUBECONFIG=/path/to/outer-kubeconfig ./scripts/vcluster-run-teardown.sh RUN_NAME [options]
#
# Options:
#   --output-dir PATH   the run's output directory to delete
#                       (default: <repo>/.vcluster-runs/RUN_NAME; must be
#                       the same value the setup was given)
#   --keycloak-url URL  outer Keycloak base URL (default:
#                       https://keycloak.keycloak.svc.cluster.local)
#   --aap-instance NAME        shared AAP instance name (default: osac-aap)
#   --aap-namespace NAMESPACE  namespace of the shared AAP instance
#                              (default: osac)
#   -h, --help          show this help
#
# Environment:
#   KUBECONFIG          kubeconfig of the outer cluster (required)
#   VM_DISK_DIR         parent of the run's VM disks, when the setup was
#                       given it (default: /tmp)
set -euo pipefail

###############################################################################
# Constants
###############################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

KAFKA_NAMESPACE="osac-kafka"
KEYCLOAK_NAMESPACE="keycloak"
KEYCLOAK_ADMIN_CREDENTIALS_SECRET="keycloak-admin-credentials"
KEYCLOAK_URL_DEFAULT="https://keycloak.keycloak.svc.cluster.local"
CERT_MANAGER_NAMESPACE="cert-manager"

# The shared AAP whose schedules the run leaves behind. The defaults mirror
# scripts/vcluster-run-setup.sh (--aap-instance/--aap-namespace): the
# operator names the controller route and the admin password secret after
# the instance, and the run's org, templates and schedules after the run.
AAP_INSTANCE_DEFAULT="osac-aap"
AAP_NAMESPACE_DEFAULT="osac"
AAP_ADMIN_USERNAME="admin"

# The ClusterOrder namespace on the outer cluster, created by the run setup
# when absent (OSAC-8054). Deleted only when this run created it: the
# instance label the setup wrote carries the run's name, and a namespace
# without this run's label is not this run's to delete.
ORDERS_NAMESPACE="osac"
INSTANCE_LABEL_KEY="osac.openshift.io/instance"

# The kinds whose names the run setup and the vcluster chart prefix with
# the run. Synced objects inside vc-<run> keep their inner names, so the
# namespace deletion is what removes them; these kinds are the ones a
# name-anchored scan can speak about.
NAMESPACED_SCAN_KINDS=("kafkauser.kafka.strimzi.io" "secret")
CLUSTER_SCOPED_SCAN_KINDS=("namespace" "clusterrole" "clusterrolebinding")

###############################################################################
# Arguments and globals
###############################################################################

RUN_NAME=""
OUTPUT_DIR=""
KEYCLOAK_URL="${KEYCLOAK_URL_DEFAULT}"
AAP_INSTANCE="${AAP_INSTANCE_DEFAULT}"
AAP_NAMESPACE="${AAP_NAMESPACE_DEFAULT}"
AAP_ADMIN_PASSWORD=""

# Set when scripts/vcluster-run-bmhs.sh --delete reports leftovers: the rest
# of the teardown still runs, and the final verification fails the run.
BMH_CLEANUP_FAILED="false"

HOST_NAMESPACE=""
KEYCLOAK_CA_FILE=""
KEYCLOAK_ADMIN_USERNAME=""
KEYCLOAK_ADMIN_PASSWORD=""

usage() {
    cat <<'EOF'
Usage:
  KUBECONFIG=/path/to/outer-kubeconfig ./scripts/vcluster-run-teardown.sh RUN_NAME [options]

Options:
  --output-dir PATH   the run's output directory to delete
                      (default: <repo>/.vcluster-runs/RUN_NAME; must be
                      the same value the setup was given)
  --keycloak-url URL  outer Keycloak base URL (default:
                      https://keycloak.keycloak.svc.cluster.local)
  --aap-instance NAME       shared AAP instance name (default: osac-aap)
  --aap-namespace NAMESPACE namespace of the shared AAP instance
                            (default: osac)
  -h, --help          show this help

Disables the run's AAP schedules, then deletes the run's vcluster (Helm
release, then namespace vc-<run>), its orders namespace on the outer
cluster when the run created it, its virtual bare-metal hosts (VMs,
sushy-tools emulator, storage pool, cache and namespace <run>-hosts with
its RBAC and BareMetalHosts), its outer KafkaUsers and Strimzi secrets,
its Keycloak realm and its output files, then verifies nothing named after
the run remains among the kinds the run setup and the vcluster create. The
run's AAP organization and job templates are left in place (known gap).
Objects the tests create in the outer cluster are not touched (known gap,
label-based deletion is the follow-up).
EOF
}

die() {
    echo "ERROR: $*" >&2
    exit 1
}

require_command() {
    local command_name="$1"
    command -v "${command_name}" >/dev/null 2>&1 \
        || die "${command_name} is not installed or not in PATH; install it and rerun."
}

###############################################################################
# Helpers
###############################################################################

read_secret_key() {
    local namespace="$1" secret_name="$2" key="$3"
    local jsonpath_key="${key//./\\.}"
    kubectl --namespace "${namespace}" get secret "${secret_name}" \
        -o jsonpath="{.data.${jsonpath_key}}"
}

keycloak_curl() {
    curl --cacert "${KEYCLOAK_CA_FILE}" "$@"
}

# The shared AAP's controller API, reached through its outer route. The
# route's certificate is signed by the outer cluster's ingress CA, which
# this script does not copy (only the CI CA that signs the service URLs
# is copied), so the connection is not verified: the request carries the
# shared AAP's admin password over the cluster's own route.
aap_curl() {
    curl -sS -k --connect-timeout 10 --max-time 60 \
        -u "${AAP_ADMIN_USERNAME}:${AAP_ADMIN_PASSWORD}" "$@"
}

keycloak_admin_token() {
    keycloak_curl -s --fail-with-body -X POST \
        "${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
        -H 'Content-Type: application/x-www-form-urlencoded' \
        -d 'grant_type=password' \
        -d 'client_id=admin-cli' \
        --data-urlencode "username=${KEYCLOAK_ADMIN_USERNAME}" \
        --data-urlencode "password=${KEYCLOAK_ADMIN_PASSWORD}" \
        | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])"
}

read_keycloak_admin_credentials() {
    KEYCLOAK_ADMIN_USERNAME="$(read_secret_key "${KEYCLOAK_NAMESPACE}" \
        "${KEYCLOAK_ADMIN_CREDENTIALS_SECRET}" admin-username | base64 -d)" \
        || die "Cannot read key admin-username of secret ${KEYCLOAK_ADMIN_CREDENTIALS_SECRET} in ${KEYCLOAK_NAMESPACE}."
    KEYCLOAK_ADMIN_PASSWORD="$(read_secret_key "${KEYCLOAK_NAMESPACE}" \
        "${KEYCLOAK_ADMIN_CREDENTIALS_SECRET}" admin-password | base64 -d)" \
        || die "Cannot read key admin-password of secret ${KEYCLOAK_ADMIN_CREDENTIALS_SECRET} in ${KEYCLOAK_NAMESPACE}."
}

fetch_keycloak_ca() {
    KEYCLOAK_CA_FILE="$(mktemp)"
    read_secret_key "${CERT_MANAGER_NAMESPACE}" default-ca ca.crt | base64 -d >"${KEYCLOAK_CA_FILE}"
    [[ -s "${KEYCLOAK_CA_FILE}" ]] \
        || die "Key ca.crt of secret default-ca in ${CERT_MANAGER_NAMESPACE} is empty."
    trap 'rm -f "${KEYCLOAK_CA_FILE}"' EXIT
}

kubectl_get() {
    local resource="$1" name="$2" namespace="$3"
    if [[ -n "${namespace}" ]]; then
        kubectl --namespace "${namespace}" get "${resource}" "${name}" -o name
    else
        kubectl get "${resource}" "${name}" -o name
    fi
}

# Wait until one named object is gone. The probe is an existence check, so
# its error output is captured; a failure other than NotFound is shown and
# fails the teardown instead of passing for successful deletion.
wait_until_absent() {
    local resource="$1" name="$2" namespace="$3" attempts="$4"
    local attempt output
    for attempt in $(seq 1 "${attempts}"); do
        if output="$(kubectl_get "${resource}" "${name}" "${namespace}" 2>&1)"; then
            echo "  Waiting for ${resource}/${name} to disappear (attempt ${attempt}/${attempts})..."
            sleep 5
            continue
        fi
        if [[ "${output}" == *"NotFound"* ]]; then
            echo "${resource}/${name} is gone."
            return 0
        fi
        die "Cannot read ${resource}/${name}: ${output}"
    done
    die "${resource}/${name} did not disappear within $(( attempts * 5 ))s. Check what still holds it (finalizers, operator)."
}

###############################################################################
# Teardown steps
###############################################################################

# Disable the run's periodical AAP schedules through the shared AAP's
# controller API. The run's organization and job templates stay (known
# gap), but the three schedules the config-as-code role creates in it --
# <run>-sync every 10 minutes, <run>-config-as-code hourly,
# <run>-publish-templates every 30 minutes -- keep launching jobs against a
# vcluster that no longer exists, and every launch ends as an error job in
# the shared AAP. PATCH {"enabled": false} stops that without deleting
# anything the shared AAP still references.
#
# Only schedules whose name starts with "<run>-" are touched: the listing
# is a name substring match, so this prefix test is what keeps run vcrun1
# away from run vcrun10's schedules.
#
# Best effort on purpose: an unreachable shared AAP, a missing admin
# password secret, or one schedule that cannot be updated must not fail the
# teardown or stop it from deleting the run's other objects. Each such
# failure is a warning that names what is still armed.
disable_aap_schedules() {
    local route_host password_b64 listing url
    route_host="$(kubectl --namespace "${AAP_NAMESPACE}" get route "${AAP_INSTANCE}" \
        -o jsonpath='{.spec.host}' 2>/dev/null)" || true
    if [[ -z "${route_host}" ]]; then
        echo "WARNING: cannot read route ${AAP_INSTANCE} in namespace ${AAP_NAMESPACE}; the AAP schedules of run ${RUN_NAME} stay enabled."
        return
    fi

    # The AAP operator creates the admin password secret the setup also
    # checks, <instance>-admin-password, with the gateway admin's password
    # under key password.
    password_b64="$(read_secret_key "${AAP_NAMESPACE}" "${AAP_INSTANCE}-admin-password" password 2>/dev/null)" || true
    if [[ -z "${password_b64}" ]]; then
        echo "WARNING: cannot read key password of secret ${AAP_INSTANCE}-admin-password in namespace ${AAP_NAMESPACE}; the AAP schedules of run ${RUN_NAME} stay enabled."
        return
    fi
    AAP_ADMIN_PASSWORD="$(printf '%s' "${password_b64}" | base64 -d)"
    url="https://${route_host}/api/controller/v2/schedules/"

    local listing_http
    if ! listing="$(aap_curl -w '\n%{http_code}' "${url}?name__icontains=${RUN_NAME}&page_size=200")"; then
        echo "WARNING: cannot reach the AAP to list schedules; the AAP schedules of run ${RUN_NAME} stay enabled."
        return
    fi
    listing_http="${listing##*$'\n'}"
    listing="${listing%$'\n'*}"
    if [[ "${listing_http}" != "200" ]]; then
        # Without this check a 401 looks exactly like "no schedules found"
        # (curl -sS without --fail exits 0, the body has no results), and
        # the run's schedules would silently stay armed.
        echo "WARNING: the AAP schedule listing returned HTTP ${listing_http}; the AAP schedules of run ${RUN_NAME} stay enabled."
        return
    fi

    local schedule_lines schedule_id schedule_name status
    local disabled=0 failed=0
    if ! schedule_lines="$(python3 -c '
import json
import sys

prefix = sys.argv[1] + "-"
try:
    results = json.load(sys.stdin).get("results", [])
except ValueError:
    raise SystemExit("schedule listing is not JSON")
for item in results:
    name = item.get("name") or ""
    if name.startswith(prefix):
        print("{}\t{}".format(item.get("id", ""), name))
' "${RUN_NAME}" <<<"${listing}")"; then
        echo "WARNING: cannot parse the AAP schedule listing; the AAP schedules of run ${RUN_NAME} stay enabled."
        return
    fi

    if [[ -z "${schedule_lines}" ]]; then
        echo "No AAP schedules named ${RUN_NAME}-* in the shared AAP; nothing to disable."
        return
    fi

    while IFS=$'\t' read -r schedule_id schedule_name; do
        [[ -n "${schedule_id}" ]] || continue
        status="$(aap_curl -o /dev/null -w '%{http_code}' -X PATCH \
            -H 'Content-Type: application/json' -d '{"enabled": false}' \
            "${url}${schedule_id}/")" || status=""
        case "${status}" in
            200)
                echo "Disabled AAP schedule ${schedule_name}."
                disabled=$((disabled + 1))
                ;;
            *)
                echo "WARNING: disabling AAP schedule ${schedule_name} returned HTTP ${status:-no response}; it stays enabled and keeps launching jobs."
                failed=$((failed + 1))
                ;;
        esac
    done <<<"${schedule_lines}"

    if [[ "${failed}" -gt 0 ]]; then
        echo "Disabled ${disabled} AAP schedule(s) of run ${RUN_NAME}; ${failed} could not be disabled (see the warnings above)."
    else
        echo "Disabled ${disabled} AAP schedule(s) of run ${RUN_NAME}."
    fi
}

uninstall_vcluster_release() {
    local existing
    existing="$(helm list --namespace "${HOST_NAMESPACE}" --filter "^${RUN_NAME}$" --short)"
    if [[ -z "${existing}" ]]; then
        echo "No vcluster Helm release ${RUN_NAME} in namespace ${HOST_NAMESPACE}; nothing to uninstall."
        return
    fi
    # Before the namespace deletion: helm removes the chart's cluster-scoped
    # RBAC (the ClusterRole and ClusterRoleBinding the syncer needs), which
    # a namespace deletion would leave behind.
    helm uninstall "${RUN_NAME}" --namespace "${HOST_NAMESPACE}" --wait --timeout 10m
}

delete_host_namespace() {
    kubectl delete namespace "${HOST_NAMESPACE}" --ignore-not-found
    # Namespace termination is asynchronous and finalizes every object in
    # it, so it can take minutes; the teardown must not report success
    # while the namespace still exists.
    wait_until_absent namespace "${HOST_NAMESPACE}" "" 120
}

# Delete the orders namespace only when this run created it: the instance
# label the setup wrote at creation carries the run's name. An absent
# namespace, or one labelled for another run (or not labelled at all --
# today's outer clusters keep the shared AAP and CA bundle in osac), is
# left in place; the run's ClusterOrders are gone with the vcluster
# anyway.
delete_orders_namespace() {
    local output label
    if ! output="$(kubectl get namespace "${ORDERS_NAMESPACE}" \
        -o jsonpath="{.metadata.labels.${INSTANCE_LABEL_KEY//./\\.}}" 2>&1)"; then
        if [[ "${output}" == *"NotFound"* ]]; then
            echo "Namespace ${ORDERS_NAMESPACE} does not exist on the outer cluster; nothing to delete."
            return
        fi
        die "Cannot read namespace ${ORDERS_NAMESPACE}: ${output}"
    fi
    label="${output}"
    if [[ -z "${label}" ]]; then
        echo "Namespace ${ORDERS_NAMESPACE} carries no ${INSTANCE_LABEL_KEY} label; not this run's to delete."
        return
    fi
    if [[ "${label}" != "${RUN_NAME}" ]]; then
        echo "Namespace ${ORDERS_NAMESPACE} carries ${INSTANCE_LABEL_KEY}=${label}, not ${RUN_NAME}; leaving it in place."
        return
    fi
    kubectl delete namespace "${ORDERS_NAMESPACE}" --ignore-not-found
    wait_until_absent namespace "${ORDERS_NAMESPACE}" "" 120
}

# The run's virtual bare-metal hosts live half on the outer cluster
# (namespace <run>-hosts, its RBAC, the BareMetalHosts) and half on this
# machine (the libvirt VMs, the sushy-tools emulator, the storage pool and
# the virtual-media cache). scripts/vcluster-run-bmhs.sh --delete owns both
# halves and derives the same run-named objects the setup created.
#
# Best effort like the AAP schedules: a failure here must not stop the
# Kafka, realm and file cleanup, but it must fail the teardown at the end
# rather than pass as a clean run with leaked VMs or hosts.
delete_run_bmhs() {
    local status=0
    "${SCRIPT_DIR}/vcluster-run-bmhs.sh" "${RUN_NAME}" --delete \
        --output-dir "${OUTPUT_DIR}" || status=$?
    if [[ "${status}" -ne 0 ]]; then
        BMH_CLEANUP_FAILED="true"
        echo "WARNING: removing the virtual bare-metal hosts of run ${RUN_NAME} failed (exit ${status}); continuing with the rest of the teardown."
    fi
}

delete_kafka_users() {
    local kafka_user
    local kafka_users=("${RUN_NAME}-fulfillment-service" "${RUN_NAME}-osac-metering" "${RUN_NAME}-osac-metering-echo-adapter")
    for kafka_user in "${kafka_users[@]}"; do
        # The KafkaUsers go first: while a KafkaUser exists, the Strimzi
        # user operator recreates its secret, so deleting the secret first
        # would race the operator.
        kubectl --namespace "${KAFKA_NAMESPACE}" delete kafkauser "${kafka_user}" --ignore-not-found
    done
    for kafka_user in "${kafka_users[@]}"; do
        wait_until_absent kafkauser "${kafka_user}" "${KAFKA_NAMESPACE}" 60
    done
    # Strimzi removes each secret with its user; this catches a secret left
    # behind by a broken operator.
    for kafka_user in "${kafka_users[@]}"; do
        kubectl --namespace "${KAFKA_NAMESPACE}" delete secret "${kafka_user}" --ignore-not-found
    done
    for kafka_user in "${kafka_users[@]}"; do
        wait_until_absent secret "${kafka_user}" "${KAFKA_NAMESPACE}" 60
    done
}

delete_keycloak_realm() {
    local token status
    token="$(keycloak_admin_token)"
    # 204: deleted. 404: already gone. Anything else is an error body,
    # surfaced by --fail-with-body.
    status="$(keycloak_curl -s -o /dev/null -w '%{http_code}' -X DELETE \
        "${KEYCLOAK_URL}/admin/realms/${RUN_NAME}" \
        -H "Authorization: Bearer ${token}")"
    case "${status}" in
        204|404) echo "Keycloak realm ${RUN_NAME} is deleted (HTTP ${status})." ;;
        *) die "Deleting Keycloak realm ${RUN_NAME} returned HTTP ${status}." ;;
    esac
}

delete_output_files() {
    [[ -n "${OUTPUT_DIR}" && "${OUTPUT_DIR}" != "/" ]] \
        || die "Refusing to delete output directory '${OUTPUT_DIR}'."
    if [[ -d "${OUTPUT_DIR}" ]]; then
        rm -rf -- "${OUTPUT_DIR}"
        echo "Deleted output directory ${OUTPUT_DIR}."
    else
        echo "Output directory ${OUTPUT_DIR} does not exist; nothing to delete."
    fi
}

###############################################################################
# Verification
###############################################################################

# Print every object of the scanned kinds whose name is anchored to this
# run: the run name followed by end-of-string or a dash. The leading slash
# anchors on the "<kind>/<name>" lines kubectl -o name prints, so run
# vcrun1 matches vc-vcrun1 and vcrun1-anything but not objects of run
# vcrun10, and nothing outside the run's own names.
#
# This function runs inside a command substitution, where bash does not
# apply set -e to the function body, so every listing is checked
# explicitly: a failed listing must fail the teardown, never pass the
# verification as an empty result.
run_name_leftovers() {
    local resource names matches found=""
    for resource in "${NAMESPACED_SCAN_KINDS[@]}"; do
        if ! names="$(kubectl get "${resource}" -A -o name)"; then
            die "Cannot list ${resource} across namespaces; cannot verify run ${RUN_NAME}'s leftovers."
        fi
        matches="$(printf '%s\n' "${names}" | grep -E "/${RUN_NAME}(\$|-)" || true)"
        if [[ -n "${matches}" ]]; then
            found+="${matches}"$'\n'
        fi
    done
    for resource in "${CLUSTER_SCOPED_SCAN_KINDS[@]}"; do
        if ! names="$(kubectl get "${resource}" -o name)"; then
            die "Cannot list ${resource}; cannot verify run ${RUN_NAME}'s leftovers."
        fi
        matches="$(printf '%s\n' "${names}" | grep -E "/${RUN_NAME}(\$|-)" || true)"
        if [[ -n "${matches}" ]]; then
            found+="${matches}"$'\n'
        fi
    done
    if ! names="$(helm list -A --short)"; then
        die "Cannot list Helm releases; cannot verify run ${RUN_NAME}'s leftovers."
    fi
    matches="$(printf '%s\n' "${names}" | grep -E "^${RUN_NAME}\$" || true)"
    if [[ -n "${matches}" ]]; then
        found+="helm release(s): ${matches}"$'\n'
    fi
    printf '%s' "${found}"
}

# The realm check re-uses the admin API: 404 means gone, 200 means the
# realm survived the deletion above, anything else means the check itself
# could not run.
keycloak_realm_leftovers() {
    local token status
    if ! token="$(keycloak_admin_token)"; then
        die "Cannot get an admin token from the outer Keycloak; cannot verify realm ${RUN_NAME} is gone."
    fi
    status="$(keycloak_curl -s -o /dev/null -w '%{http_code}' \
        "${KEYCLOAK_URL}/admin/realms/${RUN_NAME}" \
        -H "Authorization: Bearer ${token}")"
    case "${status}" in
        404) return 0 ;;
        200) echo "Keycloak realm ${RUN_NAME}"; return 0 ;;
        *) die "Checking Keycloak realm ${RUN_NAME} returned HTTP ${status}." ;;
    esac
}

verify_no_run_objects_remain() {
    local leftovers
    leftovers="$(run_name_leftovers)"
    local realm_leftovers
    realm_leftovers="$(keycloak_realm_leftovers)"
    if [[ -n "${realm_leftovers}" ]]; then
        leftovers+="${realm_leftovers}"$'\n'
    fi
    if [[ "${BMH_CLEANUP_FAILED}" == "true" ]]; then
        leftovers+="virtual bare-metal hosts of run ${RUN_NAME} (see the error above)"$'\n'
    fi

    if [[ -n "${leftovers}" ]]; then
        echo "ERROR: objects of run ${RUN_NAME} remain after teardown:" >&2
        printf '%s' "${leftovers}" >&2
        exit 1
    fi

    echo
    echo "Teardown complete: no objects matching ${RUN_NAME}-* remain among"
    echo "the kinds the run setup and the vcluster create (namespaces,"
    echo "KafkaUsers, secrets, cluster roles and bindings, Helm releases,"
    echo "Keycloak realm), the run's virtual bare-metal hosts are gone with"
    echo "their VMs, storage pool and Redfish services, and the run's output"
    echo "files are deleted. The orders namespace was deleted only if this"
    echo "run created it."
    echo "Left in place on purpose: the run's AAP organization and job"
    echo "templates (its schedules are disabled above), and any objects the"
    echo "tests created in the outer cluster (see the known gaps in"
    echo "docs/vcluster-run-setup.md)."
}

###############################################################################
# Main
###############################################################################

main() {
    local positional_args=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --output-dir)
                [[ $# -ge 2 ]] || die "--output-dir needs a value."
                OUTPUT_DIR="$2"
                shift 2
                ;;
            --keycloak-url)
                [[ $# -ge 2 ]] || die "--keycloak-url needs a value."
                KEYCLOAK_URL="$2"
                shift 2
                ;;
            --aap-instance)
                [[ $# -ge 2 ]] || die "--aap-instance needs a value."
                AAP_INSTANCE="$2"
                shift 2
                ;;
            --aap-namespace)
                [[ $# -ge 2 ]] || die "--aap-namespace needs a value."
                AAP_NAMESPACE="$2"
                shift 2
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            -*)
                die "Unknown option: $1"
                ;;
            *)
                positional_args+=("$1")
                shift
                ;;
        esac
    done

    [[ ${#positional_args[@]} -eq 1 ]] \
        || die "Exactly one run name is required. Usage: $0 RUN_NAME [options]."
    RUN_NAME="${positional_args[0]}"
    [[ "${RUN_NAME}" =~ ^[a-z0-9]([-a-z0-9]{0,10}[a-z0-9])?$ ]] \
        || die "Run name '${RUN_NAME}' must be a DNS label of lowercase letters, digits and '-', at most 12 characters."

    HOST_NAMESPACE="vc-${RUN_NAME}"
    OUTPUT_DIR="${OUTPUT_DIR:-${REPO_ROOT}/.vcluster-runs/${RUN_NAME}}"

    require_command kubectl
    require_command helm
    require_command curl
    require_command python3

    [[ -n "${KUBECONFIG:-}" ]] \
        || die "KUBECONFIG is not set. Point it at the outer cluster's kubeconfig."
    [[ -f "${KUBECONFIG}" ]] \
        || die "KUBECONFIG (${KUBECONFIG}) is not a file."

    echo "Checking outer cluster connectivity..."
    kubectl get --raw /readyz \
        || die "Cannot reach the outer cluster with KUBECONFIG=${KUBECONFIG}."

    # Before anything else: a run that is being torn down must stop firing
    # jobs in the shared AAP immediately, even if a later precondition fails
    # (the Keycloak admin secret is gone, say) and the teardown aborts.
    disable_aap_schedules

    read_keycloak_admin_credentials
    fetch_keycloak_ca

    uninstall_vcluster_release
    delete_host_namespace
    delete_orders_namespace
    delete_run_bmhs
    delete_kafka_users
    delete_keycloak_realm
    delete_output_files
    verify_no_run_objects_remain
}

main "$@"
