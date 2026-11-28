#!/usr/bin/env bash
# Functional test for scripts/vcluster-run-setup.sh and
# scripts/vcluster-run-teardown.sh.
#
# Runs both REAL scripts end to end against stubbed kubectl, helm, vcluster
# and curl binaries backed by a directory of marker files (the "cluster
# state"), with the REAL realm.json from the osac monorepo and REAL
# python3, openssl, sed and base64. A VMaaS and a BMaaS run are set up side
# by side to prove the per-run isolation; the first is torn down to prove
# the name-anchored verification leaves the second (and a run whose name is
# a prefix of another) untouched, and that a pre-existing orders namespace
# survives; a CaaS run on an outer cluster without an orders namespace
# proves the run creates it (labelled) and its teardown deletes it; a
# planted leftover must fail the teardown loudly. A run name without a
# suite letter must fail the setup loudly.
#
# The sed-hostile Keycloak admin password (p#ss&\word$x) and the
# JSON-hostile fixture password (p@ss"w0rd\2) exercise the escape handling
# copied from osac-infra's resolve-realm-secrets.sh and the reset-password
# payload encoding.
#
# Usage:
#   ./tests/vcluster-run-setup-test.sh [/path/to/osac/realm.json]
#
# The realm.json path may also come from $OSAC_REALM_JSON; it defaults to
# an osac checkout next to this repository.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SETUP_SCRIPT="${REPO_ROOT}/scripts/vcluster-run-setup.sh"
TEARDOWN_SCRIPT="${REPO_ROOT}/scripts/vcluster-run-teardown.sh"

REALM_JSON="${1:-${OSAC_REALM_JSON:-${REPO_ROOT}/../osac/osac-installer/charts/osac-infra/files/realm.json}}"
[[ -f "${REALM_JSON}" ]] || { echo "usage: $0 /path/to/realm.json (or set OSAC_REALM_JSON)" >&2; exit 1; }

# Fixture values the outer "cluster" holds. The Keycloak admin password is
# sed-hostile on purpose (#, &, backslash, dollar); the default user
# password is JSON-hostile on purpose (quote, backslash).
OUTER_DOMAIN="apps.ci.example.com"
KEYCLOAK_ADMIN_USERNAME="admin"
# shellcheck disable=SC2016  # the $ and every other character must stay literal: this is the sed-hostile fixture
KEYCLOAK_ADMIN_PASSWORD='p#ss&\word$x'
KEYCLOAK_DEFAULT_USER_PASSWORD='p@ss"w0rd\2'

WORKSPACE="$(mktemp -d)"
KEEP_WORKSPACE="false"
# Keep the workspace when a check fails, so the stub state and recorded
# calls can be inspected.
trap 'if [[ "${KEEP_WORKSPACE}" == "true" ]]; then
        echo "Workspace kept for debugging: ${WORKSPACE}"
    else
        rm -rf "${WORKSPACE}"
    fi' EXIT
export WORKSPACE

echo "Workspace: ${WORKSPACE}"

###############################################################################
# Stubs
###############################################################################

mkdir -p "${WORKSPACE}/bin" "${WORKSPACE}/calls"

# Stub kubectl. Backs two kinds of "cluster": the outer one (no
# --kubeconfig) and one vcluster per --kubeconfig path, so objects created
# inside a vcluster are invisible to the outer-cluster scans the teardown
# runs, exactly as on a real outer cluster.
cat >"${WORKSPACE}/bin/kubectl" <<'EOF'
#!/usr/bin/env bash
# Stub kubectl: implements exactly the calls the run setup and teardown
# scripts make, against marker files under ${WORKSPACE}/state. Any other
# call fails loudly so an unexpected invocation surfaces in the test.
set -euo pipefail

# One summary line per invocation, shared with the other stubs, so the test
# can assert call order across binaries.
log_call() {
    printf 'kubectl %s\n' "$*" >>"${WORKSPACE}/calls/all-calls.txt"
}

# A --kubeconfig flag selects the vcluster state named after the
# kubeconfig's directory (the run's output directory); without one, the
# outer cluster's state is used.
state_root() {
    local args=("$@") i
    for ((i = 0; i < ${#args[@]}; i++)); do
        if [[ "${args[i]}" == "--kubeconfig" ]]; then
            local run
            run="$(basename "$(dirname "${args[i + 1]}")")"
            printf '%s/state/vcluster-%s' "${WORKSPACE}" "${run}"
            return
        fi
    done
    printf '%s/state/outer' "${WORKSPACE}"
}

NAMESPACE=""
KUBECONFIG_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --kubeconfig)
            KUBECONFIG_ARGS+=(--kubeconfig "$2")
            shift 2
            ;;
        --namespace|-n)
            NAMESPACE="$2"
            shift 2
            ;;
        *)
            break
            ;;
    esac
done

STATE="$(state_root "${KUBECONFIG_ARGS[@]}")"
mkdir -p "${STATE}"
log_call "$*"

case "$1" in
    get)
        shift
        if [[ "$1" == "--raw" ]]; then
            exit 0
        fi
        resource="$1"
        name=""
        output=""
        shift
        while [[ $# -gt 0 ]]; do
            case "$1" in
                -A|--all-namespaces) shift ;;
                -o) output="$2"; shift 2 ;;
                *) name="$1"; shift ;;
            esac
        done
        # kubectl receives the output format as "jsonpath={...}"; strip the
        # format name so only the query remains.
        output="${output#jsonpath=}"
        case "${resource}" in
            ingresses.config/cluster)
                [[ "${output}" == *spec.domain* ]] || { echo "unexpected jsonpath ${output}" >&2; exit 1; }
                echo "apps.ci.example.com"
                ;;
            namespace)
                if [[ -z "${name}" ]]; then
                    find "${STATE}/namespace" -mindepth 1 -maxdepth 1 2>/dev/null \
                        | sed 's|.*/|namespace/|' || true
                    echo "namespace/default"
                elif [[ "${output}" == *uid-range* ]]; then
                    [[ -d "${STATE}/namespace/${name}" ]] || { echo "Error from server (NotFound): namespaces \"${name}\" not found" >&2; exit 1; }
                    echo "1000880000/10000"
                elif [[ "${output}" == *".labels."* ]]; then
                    # A label query: the instance label the setup records
                    # when it creates the namespace, empty for a
                    # pre-existing one.
                    [[ -d "${STATE}/namespace/${name}" ]] || { echo "Error from server (NotFound): namespaces \"${name}\" not found" >&2; exit 1; }
                    if [[ -f "${STATE}/namespace/${name}/instance-label" ]]; then
                        cat "${STATE}/namespace/${name}/instance-label"
                    fi
                elif [[ -d "${STATE}/namespace/${name}" ]]; then
                    echo "namespace/${name}"
                else
                    echo "Error from server (NotFound): namespaces \"${name}\" not found" >&2
                    exit 1
                fi
                ;;
            service)
                # The only service read is the outer cluster's DNS service.
                echo "172.30.0.10"
                ;;
            route)
                # The only route read is the shared AAP instance's.
                echo "osac-aap.apps.ci.example.com"
                ;;
            kafka)
                echo "kafka.kafka.strimzi.io/osac-kafka"
                ;;
            secret)
                if [[ -n "${name}" ]]; then
                    [[ -d "${STATE}/secret/${NAMESPACE}/${name}" ]] \
                        || { echo "Error from server (NotFound): secrets \"${name}\" not found" >&2; exit 1; }
                    if [[ "${output}" == go-template* ]]; then
                        find "${STATE}/secret/${NAMESPACE}/${name}" -type f | sed 's|.*/||' | tr '\n' ' '
                        echo
                    else
                        key="${output#\{.data.}"
                        key="${key%\}}"
                        key="${key//\\./.}"
                        [[ -f "${STATE}/secret/${NAMESPACE}/${name}/${key}" ]] \
                            || { echo "Error from server (NotFound): key ${key} of secret \"${name}\" not found" >&2; exit 1; }
                        base64 -w0 "${STATE}/secret/${NAMESPACE}/${name}/${key}"
                        echo
                    fi
                else
                    find "${STATE}/secret" -mindepth 2 -maxdepth 2 -type d 2>/dev/null \
                        | sed "s|.*secret/[^/]*/|secret/|" || true
                fi
                ;;
            configmap)
                key="${output#\{.data.}"
                key="${key%\}}"
                key="${key//\\./.}"
                [[ -f "${STATE}/configmap/${NAMESPACE}/${name}/${key}" ]] \
                    || { echo "Error from server (NotFound): configmaps \"${name}\" not found" >&2; exit 1; }
                cat "${STATE}/configmap/${NAMESPACE}/${name}/${key}"
                ;;
            kafkauser.kafka.strimzi.io|kafkauser)
                if [[ -n "${name}" ]]; then
                    [[ -d "${STATE}/kafkauser/${name}" ]] \
                        || { echo "Error from server (NotFound): kafkausers \"${name}\" not found" >&2; exit 1; }
                    echo "kafkauser.kafka.strimzi.io/${name}"
                else
                    find "${STATE}/kafkauser" -mindepth 1 -maxdepth 1 2>/dev/null \
                        | sed 's|.*/|kafkauser.kafka.strimzi.io/|' || true
                fi
                ;;
            clusterrole|clusterrolebinding)
                if [[ -n "${name}" ]]; then
                    [[ -d "${STATE}/${resource}/${name}" ]] \
                        || { echo "Error from server (NotFound): ${resource} \"${name}\" not found" >&2; exit 1; }
                    echo "${resource}/${name}"
                else
                    find "${STATE}/${resource}" -mindepth 1 -maxdepth 1 2>/dev/null \
                        | sed "s|.*/|${resource}/|" || true
                fi
                ;;
            *)
                echo "stub kubectl: unhandled get: ${resource} ${name}" >&2
                exit 1
                ;;
        esac
        ;;
    create)
        subcommand="$2"
        # `create secret generic NAME` carries the type word "generic";
        # every other kind the scripts use takes the name directly.
        if [[ "${subcommand}" == "secret" ]]; then
            name="$4"; shift 4
        else
            name="$3"; shift 3
        fi
        dry_run="false"
        args=()
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --dry-run=client) dry_run="true"; shift ;;
                -o) shift 2 ;;
                *) args+=("$1"); shift ;;
            esac
        done
        case "${subcommand}" in
            namespace)
                if [[ "${dry_run}" == "true" ]]; then
                    printf 'apiVersion: v1\nkind: Namespace\nmetadata:\n  name: %s\n' "${name}"
                else
                    mkdir -p "${STATE}/namespace/${name}"
                    echo "namespace/${name} created"
                fi
                ;;
            secret|configmap)
                # Emit the object as YAML with the literal or file-sourced
                # values in stringData, including the namespace like
                # kubectl's own --dry-run=client -o yaml output does; the
                # piped `apply -f -` records it and writes it into the
                # state.
                kind="$(printf '%s' "${subcommand}" | sed 's/^./\U&/')"
                {
                    echo "apiVersion: v1"
                    echo "kind: ${kind}"
                    echo "metadata:"
                    echo "  name: ${name}"
                    if [[ -n "${NAMESPACE}" ]]; then
                        echo "  namespace: ${NAMESPACE}"
                    fi
                    echo "stringData:"
                    for arg in "${args[@]}"; do
                        case "${arg}" in
                            --from-literal=*)
                                pair="${arg#--from-literal=}"
                                echo "  ${pair%%=*}: ${pair#*=}"
                                ;;
                            --from-file=*)
                                pair="${arg#--from-file=}"
                                echo "  ${pair%%=*}:"
                                sed 's/^/    /' "${pair#*=}"
                                ;;
                        esac
                    done
                }
                ;;
            serviceaccount|clusterrolebinding)
                kind="$(printf '%s' "${subcommand}" | sed 's/^./\U&/')"
                printf 'apiVersion: v1\nkind: %s\nmetadata:\n  name: %s\n' "${kind}" "${name}"
                ;;
            token)
                # kubectl create token SA --duration 8760h
                echo "stub-token-${name}"
                ;;
            *)
                echo "stub kubectl: unhandled create: ${subcommand}" >&2
                exit 1
                ;;
        esac
        ;;
    apply)
        shift
        [[ "$1" == "-f" && "$2" == "-" ]] || { echo "stub kubectl: unhandled apply: $*" >&2; exit 1; }
        shift 2
        input="$(cat)"
        {
            echo "# kubectl apply -n ${NAMESPACE}"
            printf '%s\n' "${input}"
        } >>"${WORKSPACE}/calls/applies.txt"
        # Write the applied objects into the state: secrets and configmaps
        # key by key, other kinds by name. The namespace comes from the -n
        # flag or from the object's own metadata.
        kind="$(printf '%s\n' "${input}" | sed -n 's/^kind: //p' | head -1)"
        object_name="$(printf '%s\n' "${input}" | sed -n 's/^  name: //p' | head -1)"
        object_namespace="$(printf '%s\n' "${input}" | sed -n 's/^  namespace: //p' | head -1)"
        if [[ -n "${object_namespace}" ]]; then
            NAMESPACE="${object_namespace}"
        fi
        case "${kind}" in
            Secret|ConfigMap)
                mkdir -p "${STATE}/${kind,,}/${NAMESPACE}/${object_name}"
                # Multi-line (file-sourced) values do not match the
                # single-line pattern; only single-line keys reach the
                # state, which nothing in the test reads back.
                string_lines="$(printf '%s\n' "${input}" \
                    | sed -n '/^stringData:/,$p' | grep '^  [a-zA-Z0-9._-]*: ' || true)"
                while IFS= read -r line; do
                    [[ -n "${line}" ]] || continue
                    key="${line%%:*}"
                    key="${key#  }"
                    value="${line#*: }"
                    printf '%s\n' "${value}" >"${STATE}/${kind,,}/${NAMESPACE}/${object_name}/${key}"
                done <<<"${string_lines}"
                ;;
            KafkaUser)
                # Strimzi generates a secret named after each user; the
                # stub does it synchronously on apply.
                for user in $(printf '%s\n' "${input}" | sed -n 's/^  name: //p'); do
                    mkdir -p "${STATE}/kafkauser/${user}"
                    mkdir -p "${STATE}/secret/osac-kafka/${user}"
                    echo "stub-password-${user}" >"${STATE}/secret/osac-kafka/${user}/password"
                done
                ;;
            Namespace)
                # Record the instance label the run setup writes when it
                # creates the orders namespace, so the teardown's
                # label-based check can read it back.
                mkdir -p "${STATE}/namespace/${object_name}"
                label="$(printf '%s\n' "${input}" | sed -n 's/^    osac\.openshift\.io\/instance: //p')"
                if [[ -n "${label}" ]]; then
                    printf '%s\n' "${label}" >"${STATE}/namespace/${object_name}/instance-label"
                fi
                ;;
            *)
                mkdir -p "${STATE}/${kind,,}/${object_name}"
                ;;
        esac
        echo "${kind,,}/${object_name} created"
        ;;
    delete)
        resource="$2"
        name="$3"
        shift 3
        # --ignore-not-found and --wait are accepted and ignored.
        case "${resource}" in
            namespace) rm -rf "${STATE}/namespace/${name}" ;;
            kafkauser) rm -rf "${STATE}/kafkauser/${name}" ;;
            secret) rm -rf "${STATE}/secret/${NAMESPACE}/${name}" ;;
            *) echo "stub kubectl: unhandled delete: ${resource}" >&2; exit 1 ;;
        esac
        echo "${resource}/${name} deleted"
        ;;
    wait)
        exit 0
        ;;
    *)
        echo "stub kubectl: unhandled command: $*" >&2
        exit 1
        ;;
esac
EOF

# Stub helm: releases are markers under the state, keyed by --kubeconfig
# the same way the kubectl stub keys its state.
cat >"${WORKSPACE}/bin/helm" <<'EOF'
#!/usr/bin/env bash
# Stub helm: list, upgrade --install and uninstall over marker files.
set -euo pipefail

log_call() {
    printf 'helm %s\n' "$*" >>"${WORKSPACE}/calls/all-calls.txt"
}

NAMESPACE="default"
KUBECONFIG_ARG=""
FILTER=""
ALL_NAMESPACES="false"
subcommand=""
positional=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --kubeconfig) KUBECONFIG_ARG="$2"; shift 2 ;;
        --namespace|-n) NAMESPACE="$2"; shift 2 ;;
        --filter) FILTER="$2"; shift 2 ;;
        -A|--all-namespaces) ALL_NAMESPACES="true"; shift ;;
        --short|--install|--wait|--create-namespace) shift ;;
        --repo|--version|--values|--timeout|--set|--repository-config) shift 2 ;;
        --repository-config=*) shift ;;
        list|upgrade|uninstall)
            if [[ -z "${subcommand}" ]]; then
                subcommand="$1"
            else
                positional+=("$1")
            fi
            shift
            ;;
        *) positional+=("$1"); shift ;;
    esac
done

log_call "${subcommand} ${positional[*]} (namespace ${NAMESPACE})"

STATE="${WORKSPACE}/state/outer"
if [[ -n "${KUBECONFIG_ARG}" ]]; then
    run="$(basename "$(dirname "${KUBECONFIG_ARG}")")"
    STATE="${WORKSPACE}/state/vcluster-${run}"
fi
mkdir -p "${STATE}/helmrelease"

case "${subcommand}" in
    list)
        if [[ "${ALL_NAMESPACES}" == "true" ]]; then
            find "${STATE}/helmrelease" -type f 2>/dev/null | sed 's|.*/||' | while IFS= read -r marker; do
                echo "${marker#*__}"
            done
        else
            # The filter arrives anchored (^run$); strip the anchors to get
            # the release name the markers are named after.
            release="${FILTER#^}"
            release="${release%\$}"
            if [[ -f "${STATE}/helmrelease/${NAMESPACE}__${release}" ]]; then
                echo "${release}"
            fi
        fi
        ;;
    upgrade)
        name="${positional[0]}"
        echo "helm upgrade --install ${name} (namespace ${NAMESPACE})" >>"${WORKSPACE}/calls/helm.txt"
        touch "${STATE}/helmrelease/${NAMESPACE}__${name}"
        ;;
    uninstall)
        name="${positional[0]}"
        echo "helm uninstall ${name} (namespace ${NAMESPACE})" >>"${WORKSPACE}/calls/helm.txt"
        rm -f "${STATE}/helmrelease/${NAMESPACE}__${name}"
        ;;
    *)
        echo "stub helm: unhandled command: ${subcommand}" >&2
        exit 1
        ;;
esac
EOF

# Stub vcluster: connect prints a kubeconfig.
cat >"${WORKSPACE}/bin/vcluster" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "$1" in
    connect)
        cat <<'KUBECONFIG'
apiVersion: v1
kind: Config
clusters:
- cluster:
    server: https://stub-vcluster.example.com
  name: stub
contexts:
- context:
    cluster: stub
    user: stub
  name: stub
current-context: stub
users:
- name: stub
  user:
    token: stub-vcluster-token
KUBECONFIG
        ;;
    *)
        echo "stub vcluster: unhandled command: $*" >&2
        exit 1
        ;;
esac
EOF

# Stub curl: a small stateful Keycloak. Realm imports create a marker the
# readiness probe and the teardown's realm checks read; every request is
# recorded under ${WORKSPACE}/calls.
cat >"${WORKSPACE}/bin/curl" <<'EOF'
#!/usr/bin/env bash
# Stub curl for the outer Keycloak's admin API. Handles the exact request
# shapes the setup and teardown scripts issue.
set -euo pipefail

METHOD="GET"
URL=""
DATA=""
DATA_BINARY_FILE=""
WRITE_OUT=""
ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -X) METHOD="$2"; shift 2 ;;
        -H) shift 2 ;;
        -d|--data) DATA="$2"; shift 2 ;;
        --data-binary) DATA_BINARY_FILE="${2#@}"; shift 2 ;;
        --data-urlencode) ARGS+=("$2"); shift 2 ;;
        -G) shift ;;
        --cacert|-o) shift 2 ;;
        -s|-f|--fail|--fail-with-body) shift ;;
        -w) WRITE_OUT="$2"; shift 2 ;;
        http://*|https://*) URL="$1"; shift ;;
        *) echo "stub curl: unhandled argument: $1" >&2; exit 1 ;;
    esac
done

REALMS_DIR="${WORKSPACE}/state/outer/realm"
mkdir -p "${REALMS_DIR}" "${WORKSPACE}/calls"

if [[ "${URL}" == */realms/master/protocol/openid-connect/token ]]; then
    echo "{\"access_token\":\"stub-admin-token\"}"
    exit 0
fi

# The master realm always exists; the setup's reachability probe uses it.
if [[ "${URL}" == */realms/master ]]; then
    echo '{}'
    exit 0
fi

if [[ "${URL}" == */admin/realms && "${METHOD}" == "POST" ]]; then
    run="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['realm'])" "${DATA_BINARY_FILE}")"
    if [[ -e "${REALMS_DIR}/${run}" ]]; then
        echo "stub curl: realm ${run} already exists (HTTP 409)" >&2
        exit 22
    fi
    cp "${DATA_BINARY_FILE}" "${WORKSPACE}/calls/imported-realm-${run}.json"
    touch "${REALMS_DIR}/${run}"
    if [[ -n "${WRITE_OUT}" ]]; then
        printf '%s' "201"
    fi
    exit 0
fi

# The admin API branches come before the public-realm branch: an URL like
# /admin/realms/<run> also ends in /realms/<run> and must not answer the
# public probe instead.
if [[ "${URL}" =~ /admin/realms/([^/]+)/clients$ ]]; then
    client_id=""
    for arg in "${ARGS[@]:-}"; do
        case "${arg}" in
            clientId=*) client_id="${arg#clientId=}" ;;
        esac
    done
    echo "client-query ${client_id}" >>"${WORKSPACE}/calls/client-queries.txt"
    echo "[{\"id\":\"client-id-${client_id}\",\"clientId\":\"${client_id}\"}]"
    exit 0
fi

if [[ "${URL}" =~ /admin/realms/([^/]+)/clients/([^/]+)/service-account-user$ ]]; then
    echo "{\"id\":\"sa-user-${BASH_REMATCH[2]}\"}"
    exit 0
fi

if [[ "${URL}" =~ /admin/realms/([^/]+)/clients/([^/]+)/roles/([^/]+)$ ]]; then
    role="${BASH_REMATCH[3]}"
    echo "{\"id\":\"role-id-${role}\",\"name\":\"${role}\",\"description\":\"stub role\",\"scope\":\"client\"}"
    exit 0
fi

if [[ "${URL}" =~ /admin/realms/([^/]+)/users/([^/]+)/role-mappings/clients/([^/]+)$ ]]; then
    echo "${METHOD} ${URL} ${DATA}" >>"${WORKSPACE}/calls/role-mappings.txt"
    exit 0
fi

if [[ "${URL}" =~ /admin/realms/([^/]+)/components$ ]]; then
    if [[ "${METHOD}" == "PUT" ]]; then
        echo "stub curl: unhandled PUT ${URL}" >&2
        exit 1
    fi
    echo '[{"id":"component-allowed-protocol-mappers","name":"Allowed Protocol Mappers","providerId":"allowed-protocol-mappers","subType":"authenticated","config":{"allowed-protocol-mapper-types":["oidc-user-property-mapper"]}},{"id":"component-allowed-client-templates","name":"Allowed Client Templates","providerId":"allowed-client-templates","subType":"authenticated","config":{"allowed-client-scopes":["basic"]}}]'
    exit 0
fi

if [[ "${URL}" =~ /admin/realms/([^/]+)/components/([^/]+)$ ]]; then
    echo "${DATA}" >>"${WORKSPACE}/calls/component-puts.txt"
    exit 0
fi

if [[ "${URL}" =~ /admin/realms/([^/]+)/client-policies/(profiles|policies)$ ]]; then
    policies_kind="${BASH_REMATCH[2]}"
    if [[ "${METHOD}" == "PUT" ]]; then
        echo "${DATA}" >>"${WORKSPACE}/calls/client-policies-${policies_kind}.txt"
        exit 0
    fi
    echo "{\"${policies_kind}\":[]}"
    exit 0
fi

if [[ "${URL}" =~ /admin/realms/([^/]+)/users$ ]]; then
    username=""
    for arg in "${ARGS[@]:-}"; do
        case "${arg}" in
            username=*) username="${arg#username=}" ;;
        esac
    done
    echo "${username}" >>"${WORKSPACE}/calls/user-queries.txt"
    echo "[{\"id\":\"user-id-${username}\"}]"
    exit 0
fi

if [[ "${URL}" =~ /admin/realms/([^/]+)/users/([^/]+)/reset-password$ ]]; then
    echo "${DATA}" >>"${WORKSPACE}/calls/reset-passwords.txt"
    exit 0
fi

if [[ "${URL}" =~ /admin/realms/([^/]+)$ ]]; then
    run="${BASH_REMATCH[1]}"
    if [[ "${METHOD}" == "DELETE" ]]; then
        rm -f "${REALMS_DIR}/${run}"
        printf '%s' "204"
        exit 0
    fi
    if [[ -e "${REALMS_DIR}/${run}" ]]; then
        printf '%s' "200"
    else
        printf '%s' "404"
    fi
    exit 0
fi

if [[ "${URL}" =~ /realms/([^/]+)$ ]]; then
    run="${BASH_REMATCH[1]}"
    if [[ -e "${REALMS_DIR}/${run}" ]]; then
        echo '{"issuer":"stub"}'
        exit 0
    fi
    # --fail turns a 404 into a non-zero exit, which the realm wait treats
    # as not ready.
    exit 22
fi

echo "stub curl: unhandled URL: ${METHOD} ${URL}" >&2
exit 1
EOF

chmod +x "${WORKSPACE}/bin/"*

###############################################################################
# Fixtures: the outer cluster's pre-existing objects
###############################################################################

seed_outer_cluster() {
    local outer="${WORKSPACE}/state/outer"
    mkdir -p "${outer}/secret/cert-manager/default-ca" \
        "${outer}/secret/keycloak/keycloak-admin-credentials" \
        "${outer}/secret/osac/osac-aap-admin-password" \
        "${outer}/configmap/osac/ca-bundle" \
        "${outer}/namespace/osac"
    # The outer cluster's own osac namespace, as a real outer cluster has
    # it (the shared AAP and the CA bundle live there): the run setup must
    # reuse it for the run's ClusterOrders and never label or delete it.
    printf -- '-----BEGIN CERTIFICATE-----\nstub-ca\n-----END CERTIFICATE-----\n' \
        >"${outer}/secret/cert-manager/default-ca/ca.crt"
    printf -- '-----BEGIN CERTIFICATE-----\nstub-leaf\n-----END CERTIFICATE-----\n' \
        >"${outer}/secret/cert-manager/default-ca/tls.crt"
    printf -- '-----BEGIN PRIVATE KEY-----\nstub-key\n-----END PRIVATE KEY-----\n' \
        >"${outer}/secret/cert-manager/default-ca/tls.key"
    printf '%s' "${KEYCLOAK_ADMIN_USERNAME}" >"${outer}/secret/keycloak/keycloak-admin-credentials/admin-username"
    printf '%s' "${KEYCLOAK_ADMIN_PASSWORD}" >"${outer}/secret/keycloak/keycloak-admin-credentials/admin-password"
    printf '%s' "${KEYCLOAK_DEFAULT_USER_PASSWORD}" >"${outer}/secret/keycloak/keycloak-admin-credentials/default-user-password"
    printf '%s' "stub-aap-admin-password" >"${outer}/secret/osac/osac-aap-admin-password/password"
    printf -- '-----BEGIN CERTIFICATE-----\nstub-bundle\n-----END CERTIFICATE-----\n' \
        >"${outer}/configmap/osac/ca-bundle/bundle.pem"
}

seed_outer_cluster

export PATH="${WORKSPACE}/bin:${PATH}"
export KUBECONFIG="${WORKSPACE}/outer-kubeconfig"
printf 'stub outer kubeconfig\n' >"${KUBECONFIG}"

# Reruns start clean.
rm -rf "${REPO_ROOT}/.vcluster-runs/vcrunv1" "${REPO_ROOT}/.vcluster-runs/vcrunb1" \
    "${REPO_ROOT}/.vcluster-runs/vcrunc1"

###############################################################################
# Assertions
###############################################################################

FAILURES=0

fail() {
    echo "FAIL: $*" >&2
    FAILURES=$((FAILURES + 1))
    KEEP_WORKSPACE="true"
}

assert_contains() {
    local file="$1" pattern="$2" description="$3"
    if grep -q -- "${pattern}" "${file}"; then
        echo "ok: ${description}"
    else
        fail "${description}: '${pattern}' not found in ${file}"
    fi
}

assert_not_contains() {
    local file="$1" pattern="$2" description="$3"
    if grep -q -- "${pattern}" "${file}"; then
        fail "${description}: '${pattern}' unexpectedly found in ${file}"
    else
        echo "ok: ${description}"
    fi
}

assert_count() {
    local file="$1" pattern="$2" expected="$3" description="$4"
    local count
    count="$(grep -c -- "${pattern}" "${file}" || true)"
    if [[ "${count}" -eq "${expected}" ]]; then
        echo "ok: ${description}"
    else
        fail "${description}: expected ${expected} matches of '${pattern}' in ${file}, found ${count}"
    fi
}

assert_file_exists() {
    if [[ -e "$1" ]]; then
        echo "ok: $2"
    else
        fail "$2: $1 does not exist"
    fi
}

assert_file_absent() {
    if [[ -e "$1" ]]; then
        fail "$2: $1 still exists"
    else
        echo "ok: $2"
    fi
}

###############################################################################
# Run the setup for the runs
###############################################################################

run_setup() {
    local run="$1"
    "${SETUP_SCRIPT}" "${run}" --realm-json "${REALM_JSON}" \
        --output-dir "${REPO_ROOT}/.vcluster-runs/${run}"
}

echo
echo "== A run name without a suite letter fails loudly =="
if "${SETUP_SCRIPT}" vcrun1 --realm-json "${REALM_JSON}" \
    --output-dir "${REPO_ROOT}/.vcluster-runs/vcrun1" \
    >"${WORKSPACE}/calls/setup-badname.log" 2>&1; then
    fail "setup accepted a run name without a suite letter"
else
    echo "ok: setup rejected a run name without a suite letter"
fi
assert_contains "${WORKSPACE}/calls/setup-badname.log" "suite letter" \
    "the failure explains the suite-letter convention"

echo
echo "== Setting up run vcrunv1 (VMaaS) =="
run_setup vcrunv1 >"${WORKSPACE}/calls/setup-vcrunv1.log" 2>&1 \
    || { cat "${WORKSPACE}/calls/setup-vcrunv1.log"; fail "setup for vcrunv1 failed"; }

echo
echo "== Setting up run vcrunb1 (BMaaS) =="
run_setup vcrunb1 >"${WORKSPACE}/calls/setup-vcrunb1.log" 2>&1 \
    || { cat "${WORKSPACE}/calls/setup-vcrunb1.log"; fail "setup for vcrunb1 failed"; }

APPLIES="${WORKSPACE}/calls/applies.txt"

echo
echo "== KafkaUsers are run-prefixed =="
assert_contains "${APPLIES}" "name: vcrunv1-fulfillment-service$" "fulfillment KafkaUser named after run vcrunv1"
assert_contains "${APPLIES}" "name: vcrunv1-osac-metering$" "metering KafkaUser named after run vcrunv1"
assert_contains "${APPLIES}" "name: vcrunv1-osac-metering-echo-adapter$" "echo adapter KafkaUser named after run vcrunv1"
assert_contains "${APPLIES}" "name: vcrunb1-fulfillment-service$" "fulfillment KafkaUser named after run vcrunb1"
assert_not_contains "${APPLIES}" "^  name: fulfillment-service$" "no unprefixed fulfillment KafkaUser"
assert_not_contains "${APPLIES}" "^  name: osac-metering$" "no unprefixed metering KafkaUser"

echo
echo "== Kafka ACLs carry the run prefixes =="
for prefix in "vcrunv1.events." "vcrunv1.system." "vcrunv1.metering.heartbeat." "vcrunv1.metering.dlq."; do
    assert_contains "${APPLIES}" "name: ${prefix}" "ACL prefix ${prefix}"
done
assert_contains "${APPLIES}" "name: vcrunv1.metering.inference$" "inference topic is run-prefixed"
assert_contains "${APPLIES}" "name: vcrunv1.metering.inference.dlq$" "inference dead-letter topic is run-prefixed"
assert_not_contains "${APPLIES}" "name: osac.events." "no shared osac.events. ACL"
assert_not_contains "${APPLIES}" "name: osac.system." "no shared osac.system. ACL"

echo
echo "== The vcluster secrets are run-prefixed =="
assert_contains "${APPLIES}" "name: vcrunv1-fulfillment-service-kafka" "fulfillment Kafka connection secret"
assert_contains "${APPLIES}" "user: vcrunv1-fulfillment-service" "connection secret carries the run-prefixed user"
assert_contains "${APPLIES}" "password: stub-password-vcrunv1-fulfillment-service" "connection secret carries this run's Strimzi password"
assert_contains "${APPLIES}" "name: vcrunv1-osac-metering$" "metering Strimzi secret mirrored under its run-prefixed name"
assert_contains "${APPLIES}" "name: vcrunv1-aap-vcluster-token" "AAP token secret"
assert_contains "${APPLIES}" "token: stub-token-vcrunv1-aap" "AAP token secret holds the minted token"
assert_contains "${APPLIES}" "name: vcrunv1-fulfillment-controller-credentials" "controller credentials secret"
assert_contains "${APPLIES}" "client-id: osac-controller" "controller credentials carry the realm client id"
assert_contains "${APPLIES}" "name: vcrunv1-osac-ui-backend-credentials" "UI backend credentials secret"
assert_contains "${APPLIES}" "client-id: osac-ui-backend" "UI backend credentials carry the realm client id"
assert_contains "${APPLIES}" "name: osac-aap-admin-password" "AAP admin password copied under the chart's name"

echo
echo "== Each run's Strimzi password is used exactly once, by that run =="
assert_count "${APPLIES}" "stub-password-vcrunv1-fulfillment-service" 1 "vcrunv1's fulfillment password appears once"
assert_count "${APPLIES}" "stub-password-vcrunb1-fulfillment-service" 1 "vcrunb1's fulfillment password appears once"

echo
echo "== The realm JSON: rename, id drop, placeholder resolution =="
python3 - "${WORKSPACE}/calls/imported-realm-vcrunv1.json" \
    "${KEYCLOAK_ADMIN_USERNAME}" "${KEYCLOAK_ADMIN_PASSWORD}" <<'PYEOF' || fail "realm JSON assertions failed"
import json
import sys

realm_path, admin_username, admin_password = sys.argv[1], sys.argv[2], sys.argv[3]
with open(realm_path) as source:
    realm = json.load(source)

checks = []
checks.append(("realm renamed to the run", realm.get("realm") == "vcrunv1"))
checks.append(("top-level id dropped", "id" not in realm))

admin_user = next(u for u in realm["users"] if u["username"] == admin_username)
checks.append((
    "realm admin password resolved with sed-hostile characters intact",
    admin_user["credentials"][0]["value"] == admin_password,
))

controller = next(c for c in realm["clients"] if c["clientId"] == "osac-controller")
ui_backend = next(c for c in realm["clients"] if c["clientId"] == "osac-ui-backend")
checks.append(("controller client secret resolved", bool(controller["secret"])))
checks.append(("UI backend client secret resolved", bool(ui_backend["secret"])))

ui_client = next(c for c in realm["clients"] if c["clientId"] == "osac-ui")
checks.append((
    "UI URL resolved to the run's route",
    ui_client["rootUrl"] == "https://osac-ui-vcrunv1.apps.ci.example.com",
))

failed = [description for description, ok in checks if not ok]
for description, ok in checks:
    print(("ok: " if ok else "FAIL: ") + description)
if failed:
    sys.exit(1)
PYEOF

assert_not_contains "${WORKSPACE}/calls/imported-realm-vcrunv1.json" "__OSAC_" "no unresolved placeholders in the realm"

echo
echo "== The realm's controller secret matches the vcluster credentials =="
controller_secret="$(python3 -c "
import json, sys
with open(sys.argv[1]) as source:
    realm = json.load(source)
print(next(c for c in realm['clients'] if c['clientId'] == 'osac-controller')['secret'])
" "${WORKSPACE}/calls/imported-realm-vcrunv1.json")"
assert_contains "${APPLIES}" "client-secret: ${controller_secret}" "vcluster controller credentials hold the realm's secret"

echo
echo "== Fixture user passwords are set with JSON-safe encoding =="
# Both setups reset the same 8 fixture users of realm.json, so 16 payloads.
python3 - "${WORKSPACE}/calls/reset-passwords.txt" "${KEYCLOAK_DEFAULT_USER_PASSWORD}" <<'PYEOF' || fail "reset-password assertions failed"
import json
import sys

password_path, expected_password = sys.argv[1], sys.argv[2]
with open(password_path) as source:
    payloads = [json.loads(line) for line in source if line.strip()]

checks = []
checks.append(("one reset per fixture user per run (8 users x 2 runs)", len(payloads) == 16))
for payload in payloads:
    checks.append((
        "password payload for a fixture user is correctly encoded",
        payload == {"type": "password", "value": expected_password, "temporary": False},
    ))

failed = [description for description, ok in checks if not ok]
for description, ok in checks:
    print(("ok: " if ok else "FAIL: ") + description)
if failed:
    sys.exit(1)
PYEOF
assert_contains "${WORKSPACE}/calls/user-queries.txt" "tenant1_admin" "fixture user tenant1_admin looked up"
assert_not_contains "${WORKSPACE}/calls/user-queries.txt" "service-account-" "service-account users are skipped"

echo
echo "== The controller client's service account gets its realm-management roles =="
assert_contains "${WORKSPACE}/calls/client-queries.txt" "osac-controller" "controller client looked up"
assert_contains "${WORKSPACE}/calls/client-queries.txt" "realm-management" "realm-management client looked up"
python3 - "${WORKSPACE}/calls/role-mappings.txt" <<'PYEOF' || fail "role-grant assertions failed"
import json
import sys

with open(sys.argv[1]) as source:
    calls = [line.split(" ", 2) for line in source if line.strip()]

checks = []
grants = [json.loads(data) for method, url, data in calls if method == "POST"]
revocations = [json.loads(data) for method, url, data in calls if method == "DELETE"]
checks.append(("one role grant and one revocation per run (2 runs)", len(grants) == 2 and len(revocations) == 2))
for grant in grants:
    role_names = sorted(role["name"] for role in grant)
    checks.append((
        "the grant carries create-client and view-clients",
        role_names == ["create-client", "view-clients"],
    ))
for revocation in revocations:
    checks.append((
        "the revocation removes manage-clients",
        [role["name"] for role in revocation] == ["manage-clients"],
    ))
for method, url, data in calls:
    checks.append((
        "the mapping targets the controller's service-account user in realm-management",
        "/users/sa-user-client-id-osac-controller/role-mappings/clients/client-id-realm-management" in url,
    ))

failed = [description for description, ok in checks if not ok]
for description, ok in checks:
    print(("ok: " if ok else "FAIL: ") + description)
if failed:
    sys.exit(1)
PYEOF

echo
echo "== The registration policies allow the service-account client shape =="
python3 - "${WORKSPACE}/calls/component-puts.txt" <<'PYEOF' || fail "registration-policy assertions failed"
import json
import sys

with open(sys.argv[1]) as source:
    components = [json.loads(line) for line in source if line.strip()]

checks = []
by_provider = {component["providerId"]: component for component in components}
checks.append(("both policy components updated (per run: 2 runs x 2)", len(components) == 4))
mappers = by_provider.get("allowed-protocol-mappers", {}).get("config", {}).get("allowed-protocol-mapper-types", [])
checks.append((
    "the protocol-mapper allowlist carries the tenant-client mappers",
    all(mapper in mappers for mapper in ("oidc-hardcoded-claim-mapper", "oidc-audience-mapper", "oidc-usersessionmodel-note-mapper")),
))
scopes = by_provider.get("allowed-client-templates", {}).get("config", {}).get("allowed-client-scopes", [])
checks.append(("the client-scope allowlist carries service_account", "service_account" in scopes))

failed = [description for description, ok in checks if not ok]
for description, ok in checks:
    print(("ok: " if ok else "FAIL: ") + description)
if failed:
    sys.exit(1)
PYEOF
python3 - "${WORKSPACE}/calls/client-policies-profiles.txt" "${WORKSPACE}/calls/client-policies-policies.txt" <<'PYEOF' || fail "registration-token-policy assertions failed"
import json
import sys

with open(sys.argv[1]) as source:
    profile_payloads = [json.loads(line) for line in source if line.strip()]
with open(sys.argv[2]) as source:
    policy_payloads = [json.loads(line) for line in source if line.strip()]

checks = []
checks.append(("one profile and one policy PUT per run (2 runs)", len(profile_payloads) == 2 and len(policy_payloads) == 2))
for payload in profile_payloads:
    profile = next(p for p in payload["profiles"] if p["name"] == "osac-service-account-registration-token-reuse")
    executors = [executor["executor"] for executor in profile["executors"]]
    checks.append((
        "the profile disables registration-token rotation",
        "registration-access-token-rotation-disabled" in executors,
    ))
for payload in policy_payloads:
    policy = next(p for p in payload["policies"] if p["name"] == "osac-service-account-registration-token-reuse")
    checks.append((
        "the policy binds the profile to registration-token updates",
        policy["conditions"][0]["configuration"]["update-client-source"] == ["ByRegistrationAccessToken"]
        and policy["profiles"] == ["osac-service-account-registration-token-reuse"],
    ))

failed = [description for description, ok in checks if not ok]
for description, ok in checks:
    print(("ok: " if ok else "FAIL: ") + description)
if failed:
    sys.exit(1)
PYEOF

echo
echo "== The per-run values overlay =="
OVERLAY="${REPO_ROOT}/.vcluster-runs/vcrunv1/instance-values.yaml"
assert_file_exists "${OVERLAY}" "overlay written"
python3 - "${OVERLAY}" <<'PYEOF' || fail "overlay assertions failed"
import sys

import yaml

with open(sys.argv[1]) as source:
    values = yaml.safe_load(source)

checks = [
    ("instanceId", values["global"]["instanceId"] == "vcrunv1"),
    ("eventTopicPrefix", values["global"]["eventTopicPrefix"] == "vcrunv1.events."),
    ("issuer realm", values["service"]["auth"]["issuerUrl"].endswith("/realms/vcrunv1")),
    ("vault issuer realm", values["service"]["vault"]["keycloakIssuerUrl"].endswith("/realms/vcrunv1")),
    ("ui issuer realm", values["ui"]["auth"]["oidcIssuerURL"].endswith("/realms/vcrunv1")),
    ("vault namespace", values["service"]["vault"]["namespace"] == "vcrunv1"),
    ("controller credentials", values["service"]["auth"]["controllerCredentials"][0]["secret"]["name"] == "vcrunv1-fulfillment-controller-credentials"),
    ("idp credentials", values["service"]["idp"]["credentials"][0]["secret"]["name"] == "vcrunv1-fulfillment-controller-credentials"),
    ("ui backend credentials", values["ui"]["auth"]["serviceAccount"]["credentialsSecret"] == "vcrunv1-osac-ui-backend-credentials"),
    ("service db config", values["service"]["database"]["connection"][0]["secret"]["name"] == "vcrunv1-db-config"),
    ("service db cert", values["service"]["database"]["connection"][1]["secret"]["name"] == "vcrunv1-db-client-cert"),
    ("metering db config", values["metering"]["database"]["connection"][0]["secret"]["name"] == "vcrunv1-db-metering-config"),
    ("metering db cert", values["metering"]["database"]["connection"][1]["secret"]["name"] == "vcrunv1-db-metering-client-cert"),
    ("kafka connection", values["service"]["kafka"]["connection"][0]["secret"]["name"] == "vcrunv1-fulfillment-service-kafka"),
    ("operator template prefix", values["operator"]["aap"]["templatePrefix"] == "vcrunv1"),
    ("operator aap url", values["operator"]["aap"]["url"] == "https://osac-aap.apps.ci.example.com/api/controller"),
    ("operator fulfillment credentials", values["operator"]["fulfillment"]["credentials"]["secretName"] == "vcrunv1-fulfillment-controller-credentials"),
    ("aap api server url", values["aap"]["vcluster"]["apiServerUrl"] == "https://vcrunv1.vc-vcrunv1.svc.cluster.local"),
    ("aap token secret", values["aap"]["vcluster"]["tokenSecret"] == {"name": "vcrunv1-aap-vcluster-token", "key": "token"}),
    ("no aap.organization", "organization" not in values.get("aap", {})),
    ("no aap.templatePrefix", "templatePrefix" not in values.get("aap", {})),
    # VMaaS suite: the vmaas service key restates the profile default and
    # nothing else is toggled.
    ("vmaas service enabled", values["global"]["services"] == {"vmaas": {"enabled": True}}),
    ("no networking override", "networking" not in values["global"]),
    ("no bmf block", "bmf" not in values),
    ("no clusterVersions block", "clusterVersions" not in values),
    ("no controller overrides", "controllers" not in values["operator"]),
    ("no ignition override", "ignition" not in values["operator"]),
    ("no instance group overrides", "instanceGroups" not in values.get("aap", {})),
]

failed = [description for description, ok in checks if not ok]
for description, ok in checks:
    print(("ok: " if ok else "FAIL: ") + description)
if failed:
    sys.exit(1)
PYEOF

echo
echo "== The BMaaS overlay selects the suite =="
BMAAS_OVERLAY="${REPO_ROOT}/.vcluster-runs/vcrunb1/instance-values.yaml"
assert_file_exists "${BMAAS_OVERLAY}" "BMaaS overlay written"
python3 - "${BMAAS_OVERLAY}" <<'PYEOF' || fail "BMaaS overlay assertions failed"
import sys

import yaml

with open(sys.argv[1]) as source:
    values = yaml.safe_load(source)

checks = [
    ("instanceId", values["global"]["instanceId"] == "vcrunb1"),
    ("bmaas service enabled", values["global"]["services"]["bmaas"]["enabled"] is True),
    ("vmaas service disabled", values["global"]["services"]["vmaas"]["enabled"] is False),
    ("caas absent", "caas" not in values["global"]["services"]),
    ("k8s manager cleared", values["global"]["networking"]["k8sManager"] == ""),
    ("cluster fulfillment instance group", values["aap"]["instanceGroups"]["clusterFulfillment"]["enabled"] is True),
    ("bmf metal3 namespace", values["bmf"]["metal3"] == {"enabled": True, "namespace": "vcrunb1-hosts"}),
    ("bmf aap url", values["bmf"]["env"]["aapUrl"] == "https://osac-aap.apps.ci.example.com/api/controller"),
    ("bmf aap insecure", values["bmf"]["env"]["aapInsecureSkipVerify"] == "true"),
    ("bmf aap template prefix", values["bmf"]["env"]["aapTemplatePrefix"] == "vcrunb1"),
    ("bmf issuer realm", values["bmf"]["secretAPI"]["issuerUrl"].endswith("/realms/vcrunb1")),
    ("bmf credentials secret", values["bmf"]["secretAPI"]["credentialsSecret"] == "vcrunb1-fulfillment-controller-credentials"),
    ("operator aap url", values["operator"]["aap"]["url"] == "https://osac-aap.apps.ci.example.com/api/controller"),
    ("no clusterVersions block", "clusterVersions" not in values),
]

failed = [description for description, ok in checks if not ok]
for description, ok in checks:
    print(("ok: " if ok else "FAIL: ") + description)
if failed:
    sys.exit(1)
PYEOF

echo
echo "== The printed make command =="
assert_contains "${WORKSPACE}/calls/setup-vcrunv1.log" "--values ${OVERLAY}" "make command passes the overlay"
assert_contains "${WORKSPACE}/calls/setup-vcrunv1.log" "fulfillment-api-vcrunv1.${OUTER_DOMAIN}" "make command passes the run-prefixed API hostname"
assert_contains "${WORKSPACE}/calls/setup-vcrunv1.log" "osac-ui-vcrunv1.${OUTER_DOMAIN}" "make command passes the run-prefixed UI hostname"
assert_contains "${WORKSPACE}/calls/setup-vcrunv1.log" "OSAC_KEYCLOAK_REALM=vcrunv1" "printed hint names the run's realm"
assert_contains "${WORKSPACE}/calls/setup-vcrunv1.log" "aap.controllerUrl=https://osac-aap.${OUTER_DOMAIN}" "make command passes the shared AAP controller URL (resolved from the route)"
assert_contains "${WORKSPACE}/calls/setup-vcrunv1.log" "aap.bootstrap.adminPasswordSecret.name=osac-aap-admin-password" "make command passes the AAP admin password secret name"

echo
echo "== Isolation between the two runs =="
assert_file_exists "${WORKSPACE}/state/outer/kafkauser/vcrunv1-fulfillment-service" "vcrunv1 KafkaUser exists in the outer cluster"
assert_file_exists "${WORKSPACE}/state/outer/kafkauser/vcrunb1-fulfillment-service" "vcrunb1 KafkaUser exists in the outer cluster"
assert_file_exists "${WORKSPACE}/state/outer/realm/vcrunv1" "realm vcrunv1 exists"
assert_file_exists "${WORKSPACE}/state/outer/realm/vcrunb1" "realm vcrunb1 exists"
assert_file_exists "${WORKSPACE}/state/vcluster-vcrunv1/secret/osac/vcrunv1-aap-vcluster-token" "vcrunv1's AAP token secret is inside its vcluster"
assert_file_exists "${WORKSPACE}/state/vcluster-vcrunb1/secret/osac/vcrunb1-aap-vcluster-token" "vcrunb1's AAP token secret is inside its vcluster"
assert_file_absent "${WORKSPACE}/state/vcluster-vcrunv1/secret/osac/vcrunb1-aap-vcluster-token" "vcrunb1's AAP token secret is not in vcrunv1"

echo
echo "== The orders namespace is reused, not created or labelled =="
# The outer cluster came with its own osac namespace (seeded above), so the
# setup must reuse it: no labelled Namespace apply, no label written. (The
# plain "kind: Namespace" applies in the log are the vcluster's own osac
# and osac-kafka namespaces, created inside the vcluster.)
assert_not_contains "${APPLIES}" "osac.openshift.io/instance:" "no labelled Namespace is applied when osac exists"
assert_file_absent "${WORKSPACE}/state/outer/namespace/osac/instance-label" "the pre-existing osac namespace carries no instance label"
assert_contains "${WORKSPACE}/calls/setup-vcrunv1.log" "already exists on the outer cluster; the run reuses it" "vcrunv1 setup reports the reuse"
assert_contains "${WORKSPACE}/calls/setup-vcrunb1.log" "already exists on the outer cluster; the run reuses it" "vcrunb1 setup reports the reuse"

###############################################################################
# Teardown
###############################################################################

# A decoy belonging to a run whose name starts with vcrunv1: the teardown of
# vcrunv1 must not match or delete it (name anchoring).
mkdir -p "${WORKSPACE}/state/outer/secret/osac-kafka/vcrunv10-osac-metering"
echo "stub-password-vcrunv10-osac-metering" \
    >"${WORKSPACE}/state/outer/secret/osac-kafka/vcrunv10-osac-metering/password"

echo
echo "== Tearing down run vcrunv1 (vcrunb1 and vcrunv10 stay) =="
"${TEARDOWN_SCRIPT}" vcrunv1 >"${WORKSPACE}/calls/teardown-vcrunv1.log" 2>&1 \
    || { cat "${WORKSPACE}/calls/teardown-vcrunv1.log"; fail "teardown of vcrunv1 failed"; }
assert_contains "${WORKSPACE}/calls/teardown-vcrunv1.log" "Teardown complete" "teardown reports success"

echo
echo "== vcrunv1's objects are gone, vcrunb1's and vcrunv10's remain =="
assert_file_absent "${WORKSPACE}/state/outer/namespace/vc-vcrunv1" "namespace vc-vcrunv1 deleted"
assert_file_absent "${WORKSPACE}/state/outer/kafkauser/vcrunv1-fulfillment-service" "vcrunv1 fulfillment KafkaUser deleted"
assert_file_absent "${WORKSPACE}/state/outer/kafkauser/vcrunv1-osac-metering" "vcrunv1 metering KafkaUser deleted"
assert_file_absent "${WORKSPACE}/state/outer/secret/osac-kafka/vcrunv1-fulfillment-service" "vcrunv1 Strimzi secret deleted"
assert_file_absent "${WORKSPACE}/state/outer/realm/vcrunv1" "realm vcrunv1 deleted"
assert_file_absent "${REPO_ROOT}/.vcluster-runs/vcrunv1" "output files deleted"
assert_file_exists "${WORKSPACE}/state/outer/kafkauser/vcrunb1-fulfillment-service" "vcrunb1 KafkaUser untouched"
assert_file_exists "${WORKSPACE}/state/outer/secret/osac-kafka/vcrunb1-osac-metering" "vcrunb1 Strimzi secret untouched"
assert_file_exists "${WORKSPACE}/state/outer/realm/vcrunb1" "realm vcrunb1 untouched"
assert_file_exists "${WORKSPACE}/state/outer/secret/osac-kafka/vcrunv10-osac-metering" "vcrunv10 decoy untouched (name anchoring)"
assert_file_exists "${REPO_ROOT}/.vcluster-runs/vcrunb1" "vcrunb1 output files untouched"
# The orders namespace pre-existed the runs, so the teardown of vcrunv1
# must leave it exactly as it was.
assert_file_exists "${WORKSPACE}/state/outer/namespace/osac" "the pre-existing orders namespace survives the teardown"
assert_contains "${WORKSPACE}/calls/teardown-vcrunv1.log" "carries no osac.openshift.io/instance label" "teardown explains why the orders namespace stays"
assert_not_contains "${WORKSPACE}/calls/teardown-vcrunv1.log" "kubectl delete namespace osac" "the pre-existing orders namespace is not deleted"

echo
echo "== The vcluster release was uninstalled before the namespace deletion =="
# grep exits non-zero on no match; under pipefail that would kill the test
# silently, so the match count is checked explicitly instead.
uninstall_line="$(grep -n "helm uninstall vcrunv1" "${WORKSPACE}/calls/all-calls.txt" | cut -d: -f1 || true)"
delete_namespace_line="$(grep -n "kubectl delete namespace vc-vcrunv1" "${WORKSPACE}/calls/all-calls.txt" | cut -d: -f1 || true)"
if [[ -n "${uninstall_line}" && -n "${delete_namespace_line}" \
    && "${uninstall_line}" -lt "${delete_namespace_line}" ]]; then
    echo "ok: helm uninstall of the vcluster precedes the namespace deletion"
else
    fail "helm uninstall of the vcluster does not precede the namespace deletion (uninstall line '${uninstall_line}', namespace deletion line '${delete_namespace_line}')"
    echo "--- all-calls.txt (teardown of vcrunv1 onwards) ---" >&2
    grep -n "vcrunv1" "${WORKSPACE}/calls/all-calls.txt" | tail -20 >&2
fi

###############################################################################
# A CaaS run on an outer cluster without an osac namespace
###############################################################################

# Remove the pre-seeded orders namespace: an outer cluster whose osac
# namespace does not exist (the AAP placed elsewhere, say). The next run
# must create it, labelled, and its teardown must delete it again.
rm -rf "${WORKSPACE}/state/outer/namespace/osac"

echo
echo "== Setting up run vcrunc1 (CaaS, orders namespace absent) =="
run_setup vcrunc1 >"${WORKSPACE}/calls/setup-vcrunc1.log" 2>&1 \
    || { cat "${WORKSPACE}/calls/setup-vcrunc1.log"; fail "setup for vcrunc1 failed"; }

echo
echo "== The run created the orders namespace, labelled with its name =="
assert_contains "${APPLIES}" "^kind: Namespace$" "a Namespace object is applied when osac is absent"
assert_contains "${APPLIES}" "osac.openshift.io/instance: vcrunc1" "the orders namespace carries the run's instance label"
assert_file_exists "${WORKSPACE}/state/outer/namespace/osac/instance-label" "the label is recorded on the namespace"
assert_contains "${WORKSPACE}/calls/setup-vcrunc1.log" "Created namespace osac on the outer cluster" "setup reports the creation"

echo
echo "== The CaaS overlay selects the suite =="
CAAS_OVERLAY="${REPO_ROOT}/.vcluster-runs/vcrunc1/instance-values.yaml"
assert_file_exists "${CAAS_OVERLAY}" "CaaS overlay written"
python3 - "${CAAS_OVERLAY}" <<'PYEOF' || fail "CaaS overlay assertions failed"
import sys

import yaml

with open(sys.argv[1]) as source:
    values = yaml.safe_load(source)

cluster_fulfillment = values["aap"]["instanceGroups"]["clusterFulfillment"]
checks = [
    ("instanceId", values["global"]["instanceId"] == "vcrunc1"),
    ("caas service enabled", values["global"]["services"]["caas"]["enabled"] is True),
    ("bmaas service enabled", values["global"]["services"]["bmaas"]["enabled"] is True),
    ("vmaas service stays on", values["global"]["services"]["vmaas"]["enabled"] is True),
    ("agentless_net fabric manager", values["global"]["networking"]["fabricManager"] == "agentless_net"),
    ("compute instance controller off", values["operator"]["controllers"]["computeInstance"] is False),
    ("ingress CA trusted", values["operator"]["ignition"]["trustIngressCA"] is True),
    (
        "cluster fulfillment config",
        cluster_fulfillment["config"] == {
            "HOSTED_CLUSTER_CONTROLLER_AVAILABILITY_POLICY": "SingleReplica",
            "HOSTED_CLUSTER_INFRASTRUCTURE_AVAILABILITY_POLICY": "SingleReplica",
            "HOSTED_CLUSTER_BASE_DOMAIN": "hosted.ci.example.com",
        },
    ),
    ("cluster fulfillment enabled", cluster_fulfillment["enabled"] is True),
    (
        "cluster versions",
        values["clusterVersions"] == {
            "enabled": True,
            "versions": [
                {"version": "4.20.0", "image": "quay.io/openshift-release-dev/ocp-release:4.20.0-multi"},
                {
                    "version": "4.22.0",
                    "image": "quay.io/openshift-release-dev/ocp-release:4.22.0-multi",
                    "default": True,
                    "diskImage": {"name": "rhcos-4-22", "sourceRef": "oci://quay.io/osac-project/rhcos:4.22.0"},
                },
            ],
        },
    ),
    ("bmf metal3 namespace", values["bmf"]["metal3"] == {"enabled": True, "namespace": "vcrunc1-hosts"}),
    ("bmf aap template prefix", values["bmf"]["env"]["aapTemplatePrefix"] == "vcrunc1"),
    ("bmf credentials secret", values["bmf"]["secretAPI"]["credentialsSecret"] == "vcrunc1-fulfillment-controller-credentials"),
    ("k8s manager not overridden", "k8sManager" not in values["global"]["networking"]),
]

failed = [description for description, ok in checks if not ok]
for description, ok in checks:
    print(("ok: " if ok else "FAIL: ") + description)
if failed:
    sys.exit(1)
PYEOF

echo
echo "== Tearing down run vcrunc1 deletes the orders namespace it created =="
"${TEARDOWN_SCRIPT}" vcrunc1 >"${WORKSPACE}/calls/teardown-vcrunc1.log" 2>&1 \
    || { cat "${WORKSPACE}/calls/teardown-vcrunc1.log"; fail "teardown of vcrunc1 failed"; }
assert_contains "${WORKSPACE}/calls/teardown-vcrunc1.log" "Teardown complete" "vcrunc1 teardown reports success"
assert_file_absent "${WORKSPACE}/state/outer/namespace/osac" "the orders namespace the run created is deleted"

echo
echo "== A leftover fails the teardown loudly =="
mkdir -p "${WORKSPACE}/state/outer/secret/osac-kafka/vcrunb1-stray-secret"
echo "leftover" >"${WORKSPACE}/state/outer/secret/osac-kafka/vcrunb1-stray-secret/password"
if "${TEARDOWN_SCRIPT}" vcrunb1 >"${WORKSPACE}/calls/teardown-vcrunb1.log" 2>&1; then
    fail "teardown of vcrunb1 succeeded with a leftover present"
else
    echo "ok: teardown of vcrunb1 failed with a leftover present"
fi
assert_contains "${WORKSPACE}/calls/teardown-vcrunb1.log" "secret/vcrunb1-stray-secret" "failure names the leftover object"

echo
if [[ "${FAILURES}" -eq 0 ]]; then
    echo "ALL CHECKS PASSED"
else
    echo "${FAILURES} CHECK(S) FAILED" >&2
    exit 1
fi
