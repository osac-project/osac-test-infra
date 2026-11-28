#!/usr/bin/env bash
# vcluster-run-setup.sh -- set up one OSAC E2E run inside a vcluster on a
# shared outer OpenShift cluster.
#
# Every shared-service object the run touches is named after the run: the
# KafkaUsers, their Strimzi secrets, the Keycloak realm and the AAP service
# account all start with the run name, so two runs on one outer cluster do
# not collide. scripts/vcluster-run-teardown.sh removes exactly these
# objects again.
#
# The script:
#   1. checks every precondition against the outer cluster and fails loudly;
#   2. reads the run's suite from the run name's suite letter (v, b or c);
#   3. renders scripts/vcluster-values.yaml.template with the discovered
#      parameters and installs the vcluster Helm release (version pinned);
#   4. mints a long-lived admin kubeconfig for the vcluster;
#   5. installs cert-manager inside the vcluster and copies the shared CI
#      certificate authority into it, so the osac chart can issue the
#      database client certificates itself;
#   6. creates the run's KafkaUsers in the outer cluster's Kafka with
#      run-prefixed names and ACL prefixes, waits for Strimzi to generate
#      their secrets, and copies them into the vcluster (the fulfillment
#      connection secret into osac, the metering secrets into an osac-kafka
#      namespace the pods' initContainers read by local API);
#   7. imports the run's Keycloak realm from the monorepo's realm.json
#      through the outer Keycloak's admin API (realm renamed to the run,
#      placeholders resolved, top-level id dropped), sets the fixture users'
#      passwords, grants the controller client's service account the
#      realm-management roles and registration policies the fulfillment
#      service needs, and writes the controller and UI client credentials
#      into the vcluster;
#   8. creates an AAP service account with cluster-admin in the vcluster
#      and stores its one-year token as a secret in the vcluster, so the
#      shared AAP can launch the run's job pods inside the vcluster;
#   9. copies the AAP admin password and the shared CA bundle into the
#      vcluster;
#  10. creates the orders namespace on the outer cluster when absent
#      (OSAC-8054: remote-mode ClusterOrders need their namespace there);
#  11. for the b and c suites, provisions the run's virtual bare-metal
#      hosts through scripts/vcluster-run-bmhs.sh -- the outer cluster's
#      <run>-hosts namespace and its per-run RBAC, the run's sushy-tools
#      emulator and VM group on this machine, and the run's BareMetalHosts
#      (the suites order BareMetalInstances and hosted-cluster workers
#      against them);
#  12. writes the per-run values overlay for `make install-osac` -- the
#      run's ID plus the suite's service toggles from the run name's suite
#      letter -- and prints the command, plus the path of the vcluster
#      kubeconfig.
#
# Usage:
#   KUBECONFIG=/path/to/outer-kubeconfig \
#       ./scripts/vcluster-run-setup.sh RUN_NAME --realm-json PATH [options]
#
# Options:
#   --realm-json PATH           realm.json from the PR's osac monorepo
#                               checkout (required): the run imports the
#                               PR's realm, so a PR that changes realm.json
#                               is tested like any other change
#   --recreate                  replace an existing vcluster release
#   --output-dir PATH           where to write the run's files
#                               (default: <repo>/.vcluster-runs/RUN_NAME)
#   --bmh-network NAME          libvirt network the outer SNO node reaches
#   --skip-bmhs                 defer BMH provisioning to the libvirt host
#                               (run vcluster-run-bmhs.sh there yourself)
#                               (required for b and c runs: their virtual
#                               bare-metal hosts attach to it)
#   --keycloak-url URL          outer Keycloak base URL (default:
#                               https://keycloak.keycloak.svc.cluster.local,
#                               verified with the CI certificate authority)
#   --aap-instance NAME         shared AAP instance name (default: osac-aap)
#   --aap-namespace NAMESPACE   namespace of the shared AAP instance
#                               (default: osac)
#   --aap-controller-url URL    external AAP controller URL; by default the
#                               script reads the AAP route named
#                               <aap-instance> in <aap-namespace>
#   -h, --help                  show this help
#
# Environment:
#   KUBECONFIG                  kubeconfig of the outer cluster (required)
#
# The run name becomes the vcluster name, its namespace vc-<run name> in the
# outer cluster, and the prefix of every shared-service object the run
# touches. Keep it at most 12 characters, lowercase letters and digits:
# generated names add prefixes to it and must stay within the 63-character
# DNS label limit. The last letter before the trailing attempt digits is
# the suite letter: v for VMaaS, b for BMaaS, c for CaaS (pr2100v1,
# pr2100b2, nightlyc1); it selects the suite's service toggles in the
# per-run values overlay.
#
# See docs/vcluster-run-setup.md for the end-to-end flow, the run-name
# table, and the POC-derived troubleshooting notes.
set -euo pipefail

###############################################################################
# Constants
###############################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VCLUSTER_VALUES_TEMPLATE="${SCRIPT_DIR}/vcluster-values.yaml.template"

# POC-verified versions (2026-11-01): vcluster CLI 0.36.1 with the matching
# chart. Bump both together; the CLI talks to the chart's syncer.
VCLUSTER_CHART_VERSION="0.36.1"
VCLUSTER_CHART_REPO="https://charts.loft.sh"
VCLUSTER_CHART_NAME="vcluster"

# One year in seconds: the 8760h token duration the POC validated. The same
# duration is used for the AAP service account token, expressed in Go
# duration syntax for `kubectl create token`.
VCLUSTER_TOKEN_EXPIRATION_SECONDS=31536000
AAP_TOKEN_DURATION=8760h

# Outer-cluster namespaces, fixed by `make install-infra PLATFORM=openshift
# PROFILE=vmaas-ci NS=osac` in the osac monorepo:
CERT_MANAGER_NAMESPACE="cert-manager" # shared CI certificate authority
OSAC_SHARED_NAMESPACE="osac"          # the CA bundle
KAFKA_NAMESPACE="osac-kafka"          # Strimzi Kafka cluster osac-kafka
KEYCLOAK_NAMESPACE="keycloak"         # the shared Keycloak and its admin
                                      # credentials secret
OUTER_DNS_NAMESPACE="openshift-dns"
OUTER_DNS_SERVICE="dns-default"

# The Keycloak admin credentials the realm import uses, created by the
# osac-infra chart (charts/osac-infra/templates/keycloak/resources.yaml).
KEYCLOAK_ADMIN_CREDENTIALS_SECRET="keycloak-admin-credentials"

# Keycloak's in-cluster service URL. The service certificate's dnsNames
# include this name and it is signed by the CI certificate authority, so
# curl verifies it with the copied default-ca ca.crt. The keycloak zone is
# NOT forwarded into the vcluster, so OSAC itself must keep using the route
# URL (https://keycloak-keycloak.<domain>); only this script, which runs on
# the outer cluster's machine, uses the service URL.
KEYCLOAK_URL_DEFAULT="https://keycloak.keycloak.svc.cluster.local"

# Destination namespace inside the vcluster. The printed make command uses
# NS=osac, so this is not configurable.
VCLUSTER_OSAC_NAMESPACE="osac"

# The ClusterOrder namespace on the OUTER cluster (OSAC-8054): in remote
# mode the operator's baremetalworker reconciler creates the InfraEnv and
# its pull secret in the ClusterOrder's namespace through the outer
# kubeconfig and lists Agents there. ClusterOrders live in the release
# namespace, so the outer cluster needs a namespace of the same name.
ORDERS_NAMESPACE="osac"

# The run-identity label every object a run creates in the outer cluster
# carries (the design's convention); it marks the orders namespace the run
# created, so the teardown deletes exactly that one.
INSTANCE_LABEL_KEY="osac.openshift.io/instance"

# The metering chart's Deployments fetch their Kafka SASL credentials
# through the LOCAL API of whatever cluster OSAC runs in: their
# initContainers read Secrets from metering.kafka.clusterNamespace, which
# defaults to osac-kafka (osac-metering/charts/osac-metering/templates/
# _helpers.tpl, osac-metering.kafkaSecretsInitContainer). The run setup
# creates the users in the outer cluster and mirrors their secrets into an
# osac-kafka namespace inside the vcluster, under their run-prefixed names.
# On current osac main the chart still looks the secrets up under their
# fixed names (osac-metering, osac-metering-echo-adapter); deriving them
# from global.instanceId lands with the parallel chart work (see the
# overlay below).
VCLUSTER_KAFKA_NAMESPACE="osac-kafka"

# Kafka brokers, unchanged by the run prefixing: the shared Strimzi cluster
# itself is not per-run (copied verbatim from the osac chart contract,
# osac-installer/charts/osac/templates/_helpers.tpl:
# osac.fulfillmentKafkaBrokers).
KAFKA_BROKERS="osac-kafka-kafka-bootstrap.osac-kafka.svc:9093"

# Default shared AAP instance, matching aap.aap.instance.name in
# osac-installer values (vmaas-ci/instance.yaml). The osac-aap chart names
# the admin-password secret <instance name>-admin-password even with
# aap.aap.instance.enabled=false, so the run copies the outer cluster's
# secret under that name. Overridable because the outer cluster's AAP
# placement is not pinned by any chart yet.
AAP_INSTANCE_DEFAULT="osac-aap"
AAP_NAMESPACE_DEFAULT="osac"

# Ingress domain of the outer cluster, read from ingresses.config/cluster.
# The vcluster has no OpenShift ingress config, so the printed make command
# carries the domain and the route hostnames explicitly.
OUTER_DOMAIN=""

###############################################################################
# Arguments and globals
###############################################################################

RUN_NAME=""
REALM_JSON_SOURCE=""
RECREATE="false"
OUTPUT_DIR=""
BMH_NETWORK=""
SKIP_BMHS="false"
KEYCLOAK_URL="${KEYCLOAK_URL_DEFAULT}"
AAP_INSTANCE="${AAP_INSTANCE_DEFAULT}"
AAP_NAMESPACE="${AAP_NAMESPACE_DEFAULT}"
AAP_CONTROLLER_URL=""

# The run's suite, read from the run name's suite letter: vmaas, bmaas or
# caas. Selects the suite's service toggles in the per-run overlay.
SUITE=""

# Run-derived names, set in main once RUN_NAME is validated. v0 used fixed
# chart names; v1 prefixes everything with the run so concurrent runs do
# not collide.
KAFKA_USER_NAME=""            # <run>-fulfillment-service
KAFKA_CONNECTION_SECRET=""    # <run>-fulfillment-service-kafka
METERING_KAFKA_USERS=()
EVENT_TOPIC_PREFIX=""         # <run>.events.
SYSTEM_GROUP_PREFIX=""        # <run>.system.
HEARTBEAT_TOPIC_PREFIX=""     # <run>.metering.heartbeat.
DLQ_TOPIC_PREFIX=""           # <run>.metering.dlq.
INFERENCE_TOPIC=""            # <run>.metering.inference
INFERENCE_DLQ_TOPIC=""        # <run>.metering.inference.dlq

HOST_NAMESPACE=""
# The outer-cluster namespace holding the b/c suites' virtual bare-metal
# hosts, labelled osac.openshift.io/instance=<run>; scripts/vcluster-run-bmhs.sh
# creates and removes it, and the overlay passes it as bmf.metal3.namespace.
BMH_NAMESPACE=""
VCLUSTER_KUBECONFIG=""
VALUES_FILE=""
INSTANCE_VALUES_FILE=""
KEYCLOAK_CA_FILE=""

# Keycloak admin credentials, read from the outer cluster.
KEYCLOAK_ADMIN_USERNAME=""
KEYCLOAK_ADMIN_PASSWORD=""
KEYCLOAK_DEFAULT_USER_PASSWORD=""

# Temp directory for secret payloads; removed on exit. The rendered realm
# JSON with its resolved client secrets lives here on purpose, never in the
# run's output directory.
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

usage() {
    cat <<'EOF'
Usage:
  KUBECONFIG=/path/to/outer-kubeconfig \
      ./scripts/vcluster-run-setup.sh RUN_NAME --realm-json PATH [options]

Options:
  --realm-json PATH           realm.json from the PR's osac monorepo
                              checkout (required): the run imports the
                              PR's realm, so a PR that changes realm.json
                              is tested like any other change
  --recreate                  replace an existing vcluster release
  --output-dir PATH           where to write the run's files
                              (default: <repo>/.vcluster-runs/RUN_NAME)
  --bmh-network NAME          libvirt network the outer SNO node reaches
                              (required for b and c runs: their virtual
                              bare-metal hosts attach to it)
  --keycloak-url URL          outer Keycloak base URL (default:
                              https://keycloak.keycloak.svc.cluster.local,
                              verified with the CI certificate authority)
  --aap-instance NAME         shared AAP instance name (default: osac-aap)
  --aap-namespace NAMESPACE   namespace of the shared AAP instance
                              (default: osac)
  --aap-controller-url URL    external AAP controller URL; by default the
                              script reads the AAP route named
                              <aap-instance> in <aap-namespace>
  -h, --help                  show this help

The run name becomes the vcluster name, its namespace vc-<run name> in the
outer cluster, and the prefix of every shared-service object the run
touches. Keep it at most 12 characters, lowercase letters and digits:
generated names add prefixes to it and must stay within the 63-character
DNS label limit. The last letter before the trailing attempt digits is
the suite letter: v for VMaaS, b for BMaaS, c for CaaS (pr2100v1,
pr2100b2, nightlyc1); it selects the suite's service toggles in the
per-run values overlay.

The b and c suites provision virtual bare-metal hosts, so they also need
--bmh-network: the libvirt network the outer cluster's node reaches.
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

# Read the run's suite from the run name's suite letter. The design's run
# ID convention is <entry><suite letter><attempt>: pr2100v1, pr2100b2,
# nightlyc1. Everything after the last letter (attempt digits, dashes) is
# stripped; what remains must end in the suite letter. A name that does
# not carry one fails loudly: the suite selects the overlay's service
# toggles, and guessing would install the wrong one.
suite_from_run_name() {
    local base="${RUN_NAME}"
    while [[ "${base}" =~ [0-9-]$ ]]; do
        base="${base%?}"
    done
    [[ -n "${base}" ]] \
        || die "Run name '${RUN_NAME}' carries no suite letter: expected v (VMaaS), b (BMaaS) or c (CaaS) as the last letter before the attempt digits."
    case "${base: -1}" in
        v) SUITE="vmaas" ;;
        b) SUITE="bmaas" ;;
        c) SUITE="caas" ;;
        *)
            die "Run name '${RUN_NAME}' does not carry a suite letter: its last letter is '${base: -1}', expected v (VMaaS), b (BMaaS) or c (CaaS)."
            ;;
    esac
    echo "Run ${RUN_NAME} runs the ${SUITE} suite."
}

###############################################################################
# Precondition checks
###############################################################################

# Print the value of one key of a secret. Fails loudly when the secret or the
# key is missing, so callers only handle the empty case.
read_secret_key() {
    local namespace="$1" secret_name="$2" key="$3"
    local jsonpath_key="${key//./\\.}"
    kubectl --namespace "${namespace}" get secret "${secret_name}" \
        -o jsonpath="{.data.${jsonpath_key}}"
}

check_secret_keys() {
    local namespace="$1" secret_name="$2"
    shift 2
    local key value
    for key in "$@"; do
        value="$(read_secret_key "${namespace}" "${secret_name}" "${key}")" \
            || die "Secret ${secret_name} is missing in namespace ${namespace} of the outer cluster. Was make install-infra run with PROFILE=vmaas-ci?"
        [[ -n "${value}" ]] \
            || die "Key '${key}' of secret ${secret_name} in namespace ${namespace} is empty."
    done
}

check_configmap_keys() {
    local namespace="$1" configmap_name="$2"
    shift 2
    local key value
    for key in "$@"; do
        value="$(kubectl --namespace "${namespace}" get configmap "${configmap_name}" -o jsonpath="{.data.${key//./\\.}}")" \
            || die "ConfigMap ${configmap_name} is missing in namespace ${namespace} of the outer cluster. Was make install-infra run with PROFILE=vmaas-ci?"
        [[ -n "${value}" ]] \
            || die "Key '${key}' of configmap ${configmap_name} in namespace ${namespace} is empty."
    done
}

# Fail if the vcluster Helm release for this run already exists, unless
# --recreate was given.
check_release_absent() {
    local existing
    existing="$(helm list --namespace "${HOST_NAMESPACE}" --filter "^${RUN_NAME}$" --short)"
    if [[ -z "${existing}" ]]; then
        return
    fi
    if [[ "${RECREATE}" != "true" ]]; then
        die "vcluster release ${RUN_NAME} already exists in namespace ${HOST_NAMESPACE}. Pass --recreate to replace it."
    fi
    echo "Deleting existing vcluster release ${RUN_NAME} (--recreate)..."
    helm uninstall "${RUN_NAME}" --namespace "${HOST_NAMESPACE}" --wait --timeout 10m
}

# Create the host namespace if needed, then read a UID from its allocated
# UID range into VCLUSTER_UID.
read_namespace_uid_range() {
    if kubectl get namespace "${HOST_NAMESPACE}" -o name | grep -q .; then
        echo "Namespace ${HOST_NAMESPACE} already exists."
    else
        kubectl create namespace "${HOST_NAMESPACE}"
    fi

    # The namespace controller sets the UID range annotation right after
    # creation; poll briefly for it.
    local attempt annotation
    annotation=""
    for attempt in $(seq 1 30); do
        annotation="$(kubectl get namespace "${HOST_NAMESPACE}" \
            -o jsonpath='{.metadata.annotations.openshift\.io/sa\.scc\.uid-range}')"
        if [[ -n "${annotation}" ]]; then
            break
        fi
        echo "  Waiting for UID range annotation on namespace ${HOST_NAMESPACE} (attempt ${attempt}/30)..."
        sleep 2
    done
    [[ -n "${annotation}" ]] \
        || die "Namespace ${HOST_NAMESPACE} has no openshift.io/sa.scc.uid-range annotation. The outer cluster must be OpenShift."
    VCLUSTER_UID="${annotation%%/*}"
    [[ "${VCLUSTER_UID}" =~ ^[0-9]+$ ]] \
        || die "Cannot parse a numeric UID from annotation value '${annotation}'."
    echo "Using UID ${VCLUSTER_UID} from namespace ${HOST_NAMESPACE} range ${annotation}."
}

# Create the orders namespace on the outer cluster when absent (OSAC-8054).
# In remote mode the operator's baremetalworker reconciler creates the
# InfraEnv and its pull secret in the ClusterOrder's namespace on the outer
# cluster and lists Agents there; nothing else creates that namespace, and
# the first order would fail at InfraEnv creation with namespace-not-found.
# The name matches the release namespace the printed make command installs
# into (NS=osac). Created here only when absent: the outer cluster's own
# osac namespace (the shared AAP and the CA bundle live there) must keep
# its labels and must never be deleted by a run's teardown. The instance
# label marks the namespace this run created; the teardown deletes the
# namespace only when the label carries this run's name.
ensure_orders_namespace() {
    if kubectl get namespace "${ORDERS_NAMESPACE}" -o name | grep -q .; then
        echo "Namespace ${ORDERS_NAMESPACE} already exists on the outer cluster; the run reuses it for its ClusterOrders."
        return
    fi
    kubectl apply -f - <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: ${ORDERS_NAMESPACE}
  labels:
    ${INSTANCE_LABEL_KEY}: ${RUN_NAME}
EOF
    echo "Created namespace ${ORDERS_NAMESPACE} on the outer cluster for the run's ClusterOrders."
}

###############################################################################
# The run's virtual bare-metal hosts (b and c suites)
###############################################################################

# The BMaaS and CaaS suites need BareMetalHosts: the BMaaS tests order
# BareMetalInstances against them and CaaS gets its bare-metal workers from
# them. The whole group is per run and lives in scripts/vcluster-run-bmhs.sh,
# which the run teardown calls with --delete; this script only passes the
# run's identity and the network the outer node reaches. A failure here
# stops the setup before the (much longer) vcluster install, and the
# teardown cleans up whatever was already created.
#
# Ironic activation is NOT part of this: .github/scripts/activate-ironic.sh
# is a one-time outer-cluster ops step, and the BMH script fails loudly when
# it has not happened.
setup_run_bmhs() {
    if [[ "${SUITE}" != "bmaas" && "${SUITE}" != "caas" ]]; then
        return
    fi
    if [[ "${SKIP_BMHS}" == "true" ]]; then
        echo "BMH provisioning deferred (--skip-bmhs): run scripts/vcluster-run-bmhs.sh '${RUN_NAME}' on the libvirt host."
        return
    fi
    [[ -n "${BMH_NETWORK}" ]] \
        || die "The ${SUITE} suite provisions virtual bare-metal hosts, so --bmh-network is required: name the libvirt network the outer cluster's node reaches."
    "${SCRIPT_DIR}/vcluster-run-bmhs.sh" "${RUN_NAME}" \
        --network "${BMH_NETWORK}" \
        --output-dir "${OUTPUT_DIR}"
}

read_outer_dns_ip() {
    OUTER_DNS_IP="$(kubectl --namespace "${OUTER_DNS_NAMESPACE}" get service "${OUTER_DNS_SERVICE}" \
        -o jsonpath='{.spec.clusterIP}')" \
        || die "Cannot read service ${OUTER_DNS_SERVICE} in namespace ${OUTER_DNS_NAMESPACE}. Is the outer cluster an OpenShift cluster?"
    [[ -n "${OUTER_DNS_IP}" ]] \
        || die "Service ${OUTER_DNS_SERVICE} in ${OUTER_DNS_NAMESPACE} has no ClusterIP."
    echo "Outer cluster DNS service ${OUTER_DNS_SERVICE} is at ${OUTER_DNS_IP}."
}

read_outer_domain() {
    OUTER_DOMAIN="$(kubectl get ingresses.config/cluster -o jsonpath='{.spec.domain}')" \
        || die "Cannot read ingresses.config/cluster from the outer cluster. Is KUBECONFIG pointing at the outer cluster?"
    [[ -n "${OUTER_DOMAIN}" ]] \
        || die "The outer cluster's ingress config has no domain."
    echo "Outer cluster domain is ${OUTER_DOMAIN}."
}

check_kafka_cluster() {
    kubectl --namespace "${KAFKA_NAMESPACE}" get kafka osac-kafka \
        || die "Strimzi Kafka cluster osac-kafka is missing in namespace ${KAFKA_NAMESPACE}. The KafkaUsers this script creates would never become ready."
}

# Resolve the shared AAP controller URL from its OpenShift route, unless the
# caller passed one. The AAP operator names the platform gateway route after
# the AnsibleAutomationPlatform CR.
resolve_aap_controller_url() {
    if [[ -n "${AAP_CONTROLLER_URL}" ]]; then
        case "${AAP_CONTROLLER_URL}" in
            https://*) ;;
            http://*) die "--aap-controller-url must use https://." ;;
            *) AAP_CONTROLLER_URL="https://${AAP_CONTROLLER_URL}" ;;
        esac
        return
    fi
    local route_host
    route_host="$(kubectl --namespace "${AAP_NAMESPACE}" get route "${AAP_INSTANCE}" -o jsonpath='{.spec.host}')" \
        || die "Cannot read route ${AAP_INSTANCE} in namespace ${AAP_NAMESPACE}. Pass --aap-controller-url, or fix --aap-instance/--aap-namespace."
    [[ -n "${route_host}" ]] \
        || die "Route ${AAP_INSTANCE} in namespace ${AAP_NAMESPACE} has no host. Pass --aap-controller-url."
    AAP_CONTROLLER_URL="https://${route_host}"
    echo "Shared AAP controller URL is ${AAP_CONTROLLER_URL} (route ${AAP_INSTANCE} in ${AAP_NAMESPACE})."
}

read_keycloak_admin_credentials() {
    KEYCLOAK_ADMIN_USERNAME="$(read_secret_key "${KEYCLOAK_NAMESPACE}" \
        "${KEYCLOAK_ADMIN_CREDENTIALS_SECRET}" admin-username | base64 -d)"
    [[ -n "${KEYCLOAK_ADMIN_USERNAME}" ]] \
        || die "Key admin-username of secret ${KEYCLOAK_ADMIN_CREDENTIALS_SECRET} in ${KEYCLOAK_NAMESPACE} is empty."
    KEYCLOAK_ADMIN_PASSWORD="$(read_secret_key "${KEYCLOAK_NAMESPACE}" \
        "${KEYCLOAK_ADMIN_CREDENTIALS_SECRET}" admin-password | base64 -d)"
    [[ -n "${KEYCLOAK_ADMIN_PASSWORD}" ]] \
        || die "Key admin-password of secret ${KEYCLOAK_ADMIN_CREDENTIALS_SECRET} in ${KEYCLOAK_NAMESPACE} is empty."
    KEYCLOAK_DEFAULT_USER_PASSWORD="$(read_secret_key "${KEYCLOAK_NAMESPACE}" \
        "${KEYCLOAK_ADMIN_CREDENTIALS_SECRET}" default-user-password | base64 -d)"
    [[ -n "${KEYCLOAK_DEFAULT_USER_PASSWORD}" ]] \
        || die "Key default-user-password of secret ${KEYCLOAK_ADMIN_CREDENTIALS_SECRET} in ${KEYCLOAK_NAMESPACE} is empty."
}

# Copy the CI CA certificate out of the outer cluster once; both the curl
# calls to Keycloak and the vcluster's cert-manager use it.
fetch_keycloak_ca() {
    KEYCLOAK_CA_FILE="${WORK_DIR}/default-ca.ca.crt"
    read_secret_key "${CERT_MANAGER_NAMESPACE}" default-ca ca.crt | base64 -d >"${KEYCLOAK_CA_FILE}"
    [[ -s "${KEYCLOAK_CA_FILE}" ]] \
        || die "Key ca.crt of secret default-ca in ${CERT_MANAGER_NAMESPACE} is empty."
}

check_keycloak_reachable() {
    # The service URL's certificate is signed by the CI CA and carries the
    # service DNS name, so this both proves reachability and proves the CA
    # copy is the one that signed the running Keycloak's certificate.
    keycloak_curl -s --fail-with-body -o /dev/null \
        "${KEYCLOAK_URL}/realms/master" \
        || die "Cannot reach the outer Keycloak at ${KEYCLOAK_URL} with the CI certificate authority. Is the Keycloak base URL right (see --keycloak-url)?"
    echo "Outer Keycloak at ${KEYCLOAK_URL} is reachable."
}

check_realm_json_source() {
    [[ -n "${REALM_JSON_SOURCE}" ]] \
        || die "--realm-json is required: point it at the PR's osac-installer/charts/osac-infra/files/realm.json."
    [[ -f "${REALM_JSON_SOURCE}" ]] \
        || die "Realm source ${REALM_JSON_SOURCE} is not a file. Point --realm-json at the PR's osac-installer/charts/osac-infra/files/realm.json."
    python3 -c "import json,sys; json.load(open(sys.argv[1]))" "${REALM_JSON_SOURCE}" \
        || die "Realm source ${REALM_JSON_SOURCE} is not valid JSON."
}

check_all_preconditions() {
    require_command kubectl
    require_command helm
    require_command vcluster
    require_command curl
    require_command python3
    require_command openssl

    [[ -n "${KUBECONFIG:-}" ]] \
        || die "KUBECONFIG is not set. Point it at the outer cluster's kubeconfig."
    [[ -f "${KUBECONFIG}" ]] \
        || die "KUBECONFIG (${KUBECONFIG}) is not a file."

    echo "Checking outer cluster connectivity..."
    kubectl get --raw /readyz \
        || die "Cannot reach the outer cluster with KUBECONFIG=${KUBECONFIG}."

    check_realm_json_source
    check_release_absent
    read_namespace_uid_range
    read_outer_dns_ip
    read_outer_domain
    check_kafka_cluster

    # The CI certificate authority the vcluster's cert-manager will issue
    # the database client certificates from. The osac chart creates the
    # Certificate objects itself (workload-db-secrets.yaml); the run setup
    # only provides cert-manager and this CA copy. In the outer cluster the
    # CA is secret default-ca in cert-manager, created by
    # osac-installer/charts/osac-infra/templates/ca-issuer.yaml.
    check_secret_keys "${CERT_MANAGER_NAMESPACE}" default-ca tls.crt tls.key ca.crt

    # The Keycloak admin credentials the realm import and the fixture-user
    # password reset use (mirroring what the osac-infra chart feeds its own
    # resolve-realm-secrets and keycloak-set-passwords hooks).
    check_secret_keys "${KEYCLOAK_NAMESPACE}" "${KEYCLOAK_ADMIN_CREDENTIALS_SECRET}" \
        admin-username admin-password default-user-password

    # AAP admin password, created by the AAP operator for instance
    # ${AAP_INSTANCE}; the osac-aap chart reads <instance>-admin-password,
    # key password, in its bootstrap and create-token jobs.
    check_secret_keys "${AAP_NAMESPACE}" "${AAP_INSTANCE}-admin-password" password

    # Shared CA bundle, written by trust-manager into osacNamespace
    # (osac-installer/charts/osac-infra/files/hooks/apply-ca-bundle.sh).
    check_configmap_keys "${OSAC_SHARED_NAMESPACE}" ca-bundle bundle.pem

    read_keycloak_admin_credentials
    fetch_keycloak_ca
    check_keycloak_reachable
    resolve_aap_controller_url
}

###############################################################################
# vcluster install and kubeconfig
###############################################################################

render_values() {
    VCLUSTER_API_SAN="${RUN_NAME}.${HOST_NAMESPACE}.svc.cluster.local"
    VCLUSTER_HOST_ZONE="${HOST_NAMESPACE}.svc.cluster.local"
    sed \
        -e "s|__VCLUSTER_UID__|${VCLUSTER_UID}|g" \
        -e "s|__VCLUSTER_API_SAN__|${VCLUSTER_API_SAN}|g" \
        -e "s|__VCLUSTER_HOST_ZONE__|${VCLUSTER_HOST_ZONE}|g" \
        -e "s|__OUTER_DNS_IP__|${OUTER_DNS_IP}|g" \
        "${VCLUSTER_VALUES_TEMPLATE}" >"${VALUES_FILE}"
    if grep -n '__[A-Z_]*__' "${VALUES_FILE}"; then
        die "Rendered values still contain placeholders; see ${VALUES_FILE}."
    fi
    echo "Rendered vcluster values to ${VALUES_FILE}."
}

install_vcluster() {
    helm upgrade --install "${RUN_NAME}" "${VCLUSTER_CHART_NAME}" \
        --repo "${VCLUSTER_CHART_REPO}" \
        --version "${VCLUSTER_CHART_VERSION}" \
        --repository-config='' \
        --namespace "${HOST_NAMESPACE}" \
        --values "${VALUES_FILE}" \
        --wait --timeout 10m

    # helm --wait already waits for the statefulset; this wait gives a
    # clearer error than helm's generic timeout if the control plane pod
    # cannot become ready (for example a rejected security context).
    kubectl --namespace "${HOST_NAMESPACE}" wait --for=condition=ready pod \
        --selector "app=vcluster,release=${RUN_NAME}" --timeout=600s
}

write_vcluster_kubeconfig() {
    # vcluster 0.36.1 has no --kubeconfig flag on connect, and
    # --update-current is deprecated. --print writes the kubeconfig to
    # stdout while the CLI logs to stderr, so the redirect captures the
    # kubeconfig alone; -s silences the logs as belt and braces. Because
    # --server is given, the CLI stops its temporary port-forward and exits
    # once the token is minted (the port-forward only serves the token
    # request; nothing is left listening). --cluster-role creates the
    # service account and its binding inside the vcluster;
    # --token-expiration matches the 8760h duration validated in the POC.
    # The server is the vcluster API service DNS name, which the template's
    # extraSANs entry covers.
    vcluster connect "${RUN_NAME}" \
        --namespace "${HOST_NAMESPACE}" \
        --service-account vcluster-admin \
        --cluster-role cluster-admin \
        --token-expiration "${VCLUSTER_TOKEN_EXPIRATION_SECONDS}" \
        --server "https://${RUN_NAME}.${HOST_NAMESPACE}.svc.cluster.local:443" \
        --print -s >"${VCLUSTER_KUBECONFIG}"
    chmod 600 "${VCLUSTER_KUBECONFIG}"

    # Prove the kubeconfig works from this machine before anything depends
    # on it. If this fails with a DNS error, the machine cannot resolve
    # outer-cluster service names; see docs/vcluster-run-setup.md.
    kubectl --kubeconfig "${VCLUSTER_KUBECONFIG}" get namespace -o name
}

create_vcluster_namespaces() {
    # osac holds the OSAC release and the copied secrets; osac-kafka holds
    # the metering Kafka secrets the metering pods' initContainers read by
    # local API (see the VCLUSTER_KAFKA_NAMESPACE constant).
    kubectl --kubeconfig "${VCLUSTER_KUBECONFIG}" create namespace "${VCLUSTER_OSAC_NAMESPACE}" \
        --dry-run=client -o yaml \
        | kubectl --kubeconfig "${VCLUSTER_KUBECONFIG}" apply -f -
    kubectl --kubeconfig "${VCLUSTER_KUBECONFIG}" create namespace "${VCLUSTER_KAFKA_NAMESPACE}" \
        --dry-run=client -o yaml \
        | kubectl --kubeconfig "${VCLUSTER_KUBECONFIG}" apply -f -
}

###############################################################################
# Shared-service credentials
###############################################################################

# Create the run's KafkaUsers in the outer cluster, the Strimzi objects the
# osac and osac-metering charts normally create in-cluster. The ACL shapes
# are copied from the monorepo's chart templates, with every topic, group
# and user name prefixed by the run:
#   - osac-installer/charts/osac/templates/fulfillment-kafka-user.yaml
#   - osac-metering/charts/osac-metering/templates/kafka-user.yaml
#   - osac-metering/charts/osac-metering/templates/echo-adapter-kafka-user.yaml
# as rendered with the charts' default kafka.clusterName/clusterNamespace
# (osac-kafka / osac-kafka). On current osac main the fulfillment topic ACL
# already follows global.eventTopicPrefix; the consumer-group and metering
# prefixes are chart constants there, and the services' own prefixes follow
# with the parallel instanceId work.
create_kafka_users() {
    kubectl --namespace "${KAFKA_NAMESPACE}" apply -f - <<EOF
apiVersion: kafka.strimzi.io/v1
kind: KafkaUser
metadata:
  name: ${KAFKA_USER_NAME}
  namespace: ${KAFKA_NAMESPACE}
  labels:
    strimzi.io/cluster: osac-kafka
spec:
  authentication:
    type: scram-sha-512
  authorization:
    type: simple
    acls:
      - resource:
          type: topic
          name: ${EVENT_TOPIC_PREFIX}
          patternType: prefix
        operations:
          - Create
          - Write
          - Read
          - Describe
          - DescribeConfigs
        host: "*"
      - resource:
          type: group
          name: ${SYSTEM_GROUP_PREFIX}
          patternType: prefix
        operations:
          - Read
          - Describe
        host: "*"
      - resource:
          type: cluster
          name: kafka-cluster
          patternType: literal
        operations:
          - IdempotentWrite
        host: "*"
---
apiVersion: kafka.strimzi.io/v1
kind: KafkaUser
metadata:
  name: ${METERING_KAFKA_USERS[0]}
  namespace: ${KAFKA_NAMESPACE}
  labels:
    strimzi.io/cluster: osac-kafka
spec:
  authentication:
    type: scram-sha-512
  authorization:
    type: simple
    acls:
      - resource:
          type: topic
          name: ${EVENT_TOPIC_PREFIX}
          patternType: prefix
        operations:
          - Read
          - Describe
        host: "*"
      - resource:
          type: topic
          name: ${HEARTBEAT_TOPIC_PREFIX}
          patternType: prefix
        operations:
          - Create
          - Read
          - Write
          - Describe
          - DescribeConfigs
        host: "*"
      - resource:
          type: topic
          name: ${DLQ_TOPIC_PREFIX}
          patternType: prefix
        operations:
          - Create
          - Describe
          - DescribeConfigs
        host: "*"
---
apiVersion: kafka.strimzi.io/v1
kind: KafkaUser
metadata:
  name: ${METERING_KAFKA_USERS[1]}
  namespace: ${KAFKA_NAMESPACE}
  labels:
    strimzi.io/cluster: osac-kafka
spec:
  authentication:
    type: scram-sha-512
  authorization:
    type: simple
    acls:
      - resource:
          type: topic
          name: ${HEARTBEAT_TOPIC_PREFIX}
          patternType: prefix
        operations:
          - Read
          - Describe
        host: "*"
      - resource:
          type: topic
          name: ${INFERENCE_TOPIC}
          patternType: literal
        operations:
          - Read
          - Describe
        host: "*"
      # The adapters consume the literal shared inference topics (the Go
      # constant TopicInference, not configurable): grant them alongside
      # the run-prefixed one so the echo adapter's consumer group is not
      # blocked on the shared topic while heartbeats flow.
      - resource:
          type: topic
          name: osac.metering.inference
          patternType: prefix
        operations:
          - Read
          - Describe
        host: "*"
      - resource:
          type: topic
          name: ${DLQ_TOPIC_PREFIX}
          patternType: prefix
        operations:
          - Write
          - Describe
        host: "*"
      - resource:
          type: topic
          name: ${INFERENCE_DLQ_TOPIC}
          patternType: literal
        operations:
          - Write
          - Describe
        host: "*"
      - resource:
          type: group
          name: ${METERING_KAFKA_USERS[1]}
          patternType: literal
        operations:
          - Read
        host: "*"
EOF
}

# Wait until Strimzi has generated a KafkaUser's secret (Strimzi names it
# after the user, in the Kafka namespace) and its password key is populated.
# Leaves the base64 password in ${WORK_DIR}/strimzi-<user>.b64.
wait_for_strimzi_secret() {
    local secret_name="$1"
    local password_file="${WORK_DIR}/strimzi-${secret_name}.b64"
    local attempt
    for attempt in $(seq 1 60); do
        # The probe is expected to fail with "not found" until Strimzi
        # creates the secret; the loop reports its own progress, so the
        # repeated probe error is quieted here on purpose.
        if kubectl --namespace "${KAFKA_NAMESPACE}" get secret "${secret_name}" \
            -o jsonpath='{.data.password}' >"${password_file}" 2>/dev/null && [[ -s "${password_file}" ]]; then
            echo "Strimzi secret ${secret_name} is ready."
            return 0
        fi
        echo "  Waiting for Strimzi to generate secret ${secret_name} in ${KAFKA_NAMESPACE} (attempt ${attempt}/60, 300s total)..."
        sleep 5
    done
    die "Strimzi did not generate secret ${secret_name} in ${KAFKA_NAMESPACE} within 300s. Check the KafkaUser status and the Strimzi cluster operator logs."
}

# Create the connection secret in the vcluster's osac namespace with the
# keys the osac chart's service.kafka.connection expects: brokers, user,
# password (shape copied from osac-installer/charts/osac/templates/kafka/
# copy-fulfillment-kafka-secret.yaml).
create_kafka_connection_secret() {
    local password
    wait_for_strimzi_secret "${KAFKA_USER_NAME}"
    password="$(base64 -d "${WORK_DIR}/strimzi-${KAFKA_USER_NAME}.b64")"

    kubectl --kubeconfig "${VCLUSTER_KUBECONFIG}" --namespace "${VCLUSTER_OSAC_NAMESPACE}" \
        create secret generic "${KAFKA_CONNECTION_SECRET}" \
        --from-literal="brokers=${KAFKA_BROKERS}" \
        --from-literal="user=${KAFKA_USER_NAME}" \
        --from-literal="password=${password}" \
        --dry-run=client -o yaml \
        | kubectl --kubeconfig "${VCLUSTER_KUBECONFIG}" apply -f -
    echo "Created secret ${KAFKA_CONNECTION_SECRET} in vcluster namespace ${VCLUSTER_OSAC_NAMESPACE}."
}

# Copy the metering KafkaUsers' Strimzi secrets into the vcluster's
# osac-kafka namespace under their run-prefixed names, so the metering
# pods' initContainers find them once the chart derives those names from
# global.instanceId (parallel osac work; see the overlay).
copy_metering_kafka_secrets() {
    local kafka_user
    for kafka_user in "${METERING_KAFKA_USERS[@]}"; do
        wait_for_strimzi_secret "${kafka_user}"
        copy_secret_verbatim "${KAFKA_NAMESPACE}" "${kafka_user}" "${VCLUSTER_KAFKA_NAMESPACE}" "${kafka_user}"
    done
}

# Install cert-manager inside the vcluster, so the osac chart can issue the
# database client certificates (its Certificate objects in
# workload-db-secrets.yaml) from the shared CA. The invocation is copied
# verbatim from the osac-installer Makefile's kind path, plus the vcluster
# kubeconfig.
install_vcluster_cert_manager() {
    helm --kubeconfig "${VCLUSTER_KUBECONFIG}" upgrade --install cert-manager \
        oci://quay.io/jetstack/charts/cert-manager \
        --version v1.20.0 --namespace cert-manager --create-namespace \
        --set crds.enabled=true --wait --timeout 5m
}

# Copy the outer cluster's CI certificate authority into the vcluster's
# cert-manager namespace under the same name, then expose it as the
# ClusterIssuer the osac chart's service.certs.issuerRef names (default-ca),
# in the same shape osac-installer/charts/osac-infra/templates/ca-issuer.yaml
# creates in the outer cluster.
prepare_vcluster_certificate_authority() {
    copy_secret_verbatim "${CERT_MANAGER_NAMESPACE}" default-ca "${CERT_MANAGER_NAMESPACE}" default-ca

    kubectl --kubeconfig "${VCLUSTER_KUBECONFIG}" apply -f - <<'EOF'
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: default-ca
spec:
  ca:
    secretName: default-ca
EOF

    # Prove the issuer actually works before OSAC depends on it: cert-manager
    # marks it Ready only when it can sign with the copied CA secret.
    kubectl --kubeconfig "${VCLUSTER_KUBECONFIG}" wait --for=condition=Ready \
        clusterissuer/default-ca --timeout=120s
}

# Copy a secret from the outer cluster into the vcluster's osac namespace,
# key for key, through files in WORK_DIR.
copy_secret_files() {
    local source_namespace="$1" source_secret="$2" destination_secret="$3"
    shift 3
    local key key_file create_args=()
    for key in "$@"; do
        key_file="${WORK_DIR}/${source_secret}.${key}"
        read_secret_key "${source_namespace}" "${source_secret}" "${key}" | base64 -d >"${key_file}"
        [[ -s "${key_file}" ]] \
            || die "Key '${key}' of secret ${source_secret} in ${source_namespace} is empty."
        create_args+=(--from-file="${key}=${key_file}")
    done
    kubectl --kubeconfig "${VCLUSTER_KUBECONFIG}" --namespace "${VCLUSTER_OSAC_NAMESPACE}" \
        create secret generic "${destination_secret}" "${create_args[@]}" \
        --dry-run=client -o yaml \
        | kubectl --kubeconfig "${VCLUSTER_KUBECONFIG}" apply -f -
    echo "Created secret ${destination_secret} in vcluster namespace ${VCLUSTER_OSAC_NAMESPACE}."
}

# Create a secret in the vcluster's osac namespace from literal key=value
# pairs. The password values reach kubectl through the arguments, not the
# environment or a here-string, so they never appear in a process listing
# beyond the short-lived kubectl invocation itself.
create_vcluster_secret_literals() {
    local destination_secret="$1"
    shift
    kubectl --kubeconfig "${VCLUSTER_KUBECONFIG}" --namespace "${VCLUSTER_OSAC_NAMESPACE}" \
        create secret generic "${destination_secret}" "$@" \
        --dry-run=client -o yaml \
        | kubectl --kubeconfig "${VCLUSTER_KUBECONFIG}" apply -f -
    echo "Created secret ${destination_secret} in vcluster namespace ${VCLUSTER_OSAC_NAMESPACE}."
}

# Copy a secret from the outer cluster into a vcluster namespace, key for
# key, keeping every data key (used for Strimzi secrets, whose key set the
# chart-defined consumers read by name).
copy_secret_verbatim() {
    local source_namespace="$1" source_secret="$2" destination_namespace="$3" destination_secret="$4"
    local keys key key_file
    local create_args=()
    # The go-template below must reach kubectl unexpanded, hence the single
    # quotes; it is not shell, so shellcheck's expansion hint is silenced.
    # shellcheck disable=SC2016
    keys="$(kubectl --namespace "${source_namespace}" get secret "${source_secret}" \
        -o go-template='{{range $key, $value := .data}}{{$key}} {{end}}')" \
        || die "Secret ${source_secret} is missing in namespace ${source_namespace}."
    [[ -n "${keys}" ]] || die "Secret ${source_secret} in ${source_namespace} has no data keys."
    local key_list
    read -r -a key_list <<<"${keys}"
    for key in "${key_list[@]}"; do
        key_file="${WORK_DIR}/${source_secret}.${key}"
        kubectl --namespace "${source_namespace}" get secret "${source_secret}" \
            -o jsonpath="{.data.${key//./\\.}}" | base64 -d >"${key_file}"
        [[ -s "${key_file}" ]] \
            || die "Key '${key}' of secret ${source_secret} in ${source_namespace} is empty."
        create_args+=(--from-file="${key}=${key_file}")
    done
    kubectl --kubeconfig "${VCLUSTER_KUBECONFIG}" --namespace "${destination_namespace}" \
        create secret generic "${destination_secret}" "${create_args[@]}" \
        --dry-run=client -o yaml \
        | kubectl --kubeconfig "${VCLUSTER_KUBECONFIG}" apply -f -
    echo "Created secret ${destination_secret} in vcluster namespace ${destination_namespace}."
}

# Copy the shared CA bundle ConfigMap from the outer cluster into the
# vcluster's osac namespace.
copy_ca_bundle() {
    local bundle_file="${WORK_DIR}/ca-bundle.bundle.pem"
    kubectl --namespace "${OSAC_SHARED_NAMESPACE}" get configmap ca-bundle \
        -o jsonpath='{.data.bundle\.pem}' >"${bundle_file}"
    [[ -s "${bundle_file}" ]] || die "ConfigMap ca-bundle in ${OSAC_SHARED_NAMESPACE} has an empty bundle.pem key."
    kubectl --kubeconfig "${VCLUSTER_KUBECONFIG}" --namespace "${VCLUSTER_OSAC_NAMESPACE}" \
        create configmap ca-bundle --from-file="bundle.pem=${bundle_file}" \
        --dry-run=client -o yaml \
        | kubectl --kubeconfig "${VCLUSTER_KUBECONFIG}" apply -f -
    echo "Created configmap ca-bundle in vcluster namespace ${VCLUSTER_OSAC_NAMESPACE}."
}

copy_outer_secrets_into_vcluster() {
    # AAP admin password for the osac-aap subchart's jobs. The chart derives
    # the secret name from aap.aap.instance.name even with the instance
    # disabled (external mode), so the copy keeps the outer instance's name
    # and the vcluster-ci profile's aap.aap.instance.name must match it.
    copy_secret_files "${AAP_NAMESPACE}" "${AAP_INSTANCE}-admin-password" "${AAP_INSTANCE}-admin-password" password

    # Shared CA bundle, referenced by service.certs.caBundle.configMap.
    copy_ca_bundle
}

###############################################################################
# Keycloak realm
###############################################################################

# Every curl to the outer Keycloak verifies TLS with the CI certificate
# authority the setup copied out of the outer cluster.
keycloak_curl() {
    curl --cacert "${KEYCLOAK_CA_FILE}" "$@"
}

# Escape a string for use inside a JSON string literal: full JSON string
# escaping (control characters, backslash, quotes) via python3's json.dumps,
# minus the surrounding quotes. Copied verbatim from
# osac-installer/charts/osac-infra/files/hooks/resolve-realm-secrets.sh.
json_escape_string() {
    python3 -c "import json,sys; print(json.dumps(sys.argv[1])[1:-1])" "$1"
}

# Escape a string for use in a sed s### replacement: backslash, ampersand
# and the # delimiter. Copied verbatim from the same hook.
escape_sed_replacement() {
    printf '%s' "$1" | sed -e 's/[\&#]/\\&/g'
}

# Get a fresh admin access token from the outer Keycloak's master realm,
# the same grant the osac-infra keycloak-set-passwords hook uses.
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

# Render the run's realm JSON: copy the PR's realm.json with the realm
# renamed to the run and the top-level id dropped, then resolve the seven
# placeholders. The rename and id drop go through python3's json module so
# only the top-level realm name changes; every other occurrence of "osac"
# (client ids, role names, groups) stays as the PR defined it. The id is
# dropped because Keycloak assigns its own: an id that already belongs to
# another realm on the same server is rejected on import.
render_realm_json() {
    python3 - "${REALM_JSON_SOURCE}" "${WORK_DIR}/realm-renamed.json" "${RUN_NAME}" <<'PYEOF'
import json
import sys

source_path, destination_path, run_name = sys.argv[1], sys.argv[2], sys.argv[3]
with open(source_path) as source:
    realm = json.load(source)
realm["realm"] = run_name


def strip_ids(value):
    """Drop every embedded "id" field.

    Keycloak entity ids are globally unique across realms, and the outer
    Keycloak already imported this same file as the shared realm: keeping
    the exported ids makes the import fail with "Duplicate resource
    error". Keycloak assigns fresh ids for anything the payload leaves
    out, and realm JSON references things by name, not by id.
    """
    if isinstance(value, dict):
        value.pop("id", None)
        for item in value.values():
            strip_ids(item)
    elif isinstance(value, list):
        for item in value:
            strip_ids(item)


strip_ids(realm)
with open(destination_path, "w") as destination:
    json.dump(realm, destination)
    destination.write("\n")
PYEOF

    # Per-run client secrets for the four confidential clients realm.json
    # defines (osac-controller, osac-admin, osac-ui-backend,
    # osac-csi-driver), generated exactly like the osac-infra chart's
    # resolve-realm-secrets hook generates its keycloak-client-secrets:
    # openssl rand -base64 18 output cannot contain the sed-significant
    # characters (#, &, backslash), which is why the hook substitutes those
    # raw while the user-supplied values below get both escaping passes.
    local controller_secret admin_secret csi_driver_secret ui_backend_secret aap_secret_reader_secret
    controller_secret="$(openssl rand -base64 18)"
    admin_secret="$(openssl rand -base64 18)"
    csi_driver_secret="$(openssl rand -base64 18)"
    ui_backend_secret="$(openssl rand -base64 18)"
    aap_secret_reader_secret="$(openssl rand -base64 18)"

    # The realm admin user's credentials come from the outer cluster's
    # keycloak-admin-credentials secret, exactly what the osac-infra chart
    # feeds its own resolve-realm-secrets hook. They are user-supplied Helm
    # values upstream and can contain anything, so they get the same two
    # escaping passes as there: full JSON string escaping first (the raw
    # value lands inside a JSON string), then sed replacement escaping.
    sed \
        -e "s#__OSAC_CONTROLLER_CLIENT_SECRET__#${controller_secret}#" \
        -e "s#__OSAC_ADMIN_CLIENT_SECRET__#${admin_secret}#" \
        -e "s#__OSAC_CSI_DRIVER_CLIENT_SECRET__#${csi_driver_secret}#" \
        -e "s#__OSAC_UI_BACKEND_CLIENT_SECRET__#${ui_backend_secret}#" \
        -e "s#__OSAC_AAP_SECRET_READER_CLIENT_SECRET__#${aap_secret_reader_secret}#" \
        -e "s#__OSAC_REALM_ADMIN_USERNAME__#$(escape_sed_replacement "$(json_escape_string "${KEYCLOAK_ADMIN_USERNAME}")")#" \
        -e "s#__OSAC_REALM_ADMIN_PASSWORD__#$(escape_sed_replacement "$(json_escape_string "${KEYCLOAK_ADMIN_PASSWORD}")")#" \
        -e "s#__OSAC_UI_URL__#$(escape_sed_replacement "https://osac-ui-${RUN_NAME}.${OUTER_DOMAIN}")#g" \
        "${WORK_DIR}/realm-renamed.json" >"${WORK_DIR}/realm.json"

    # A placeholder the sed above did not resolve means the PR's realm.json
    # and this script disagree about the contract; the imported realm would
    # carry the literal marker as a secret or URL.
    if grep -n '__OSAC_[A-Z_]*__' "${WORK_DIR}/realm.json"; then
        die "Rendered realm ${WORK_DIR}/realm.json still contains placeholders; the PR's realm.json defines one this script does not resolve."
    fi

    # Keep the controller and UI backend secrets for the vcluster copies;
    # the other two have no consumer in a vcluster run but must still be
    # resolved inside the realm.
    printf '%s' "${controller_secret}" >"${WORK_DIR}/controller-client-secret"
    printf '%s' "${ui_backend_secret}" >"${WORK_DIR}/ui-backend-client-secret"
    echo "Rendered realm ${RUN_NAME} from ${REALM_JSON_SOURCE}."
}

# Import the rendered realm through the outer Keycloak's admin API.
import_keycloak_realm() {
    local token
    token="$(keycloak_admin_token)"
    # The run setup runs this twice by design: once in the setup script's
    # serial flow, once in the caller's parallel lanes (create-run.sh step
    # 3). A plain POST answers 409 on the second pass and kills the lane.
    # Import as an upsert: POST creates the realm (201); when it already
    # exists, PUT replaces it in place. Any other error body surfaces
    # because of --fail-with-body.
    if ! keycloak_curl -s --fail-with-body -X POST "${KEYCLOAK_URL}/admin/realms" \
        -H "Authorization: Bearer ${token}" \
        -H 'Content-Type: application/json' \
        --data-binary "@${WORK_DIR}/realm.json" >/dev/null 2>&1; then
        keycloak_curl -s --fail-with-body -X PUT "${KEYCLOAK_URL}/admin/realms/${RUN_NAME}" \
            -H "Authorization: Bearer ${token}" \
            -H 'Content-Type: application/json' \
            --data-binary "@${WORK_DIR}/realm.json" >/dev/null
    fi
    echo "Imported realm ${RUN_NAME} into the outer Keycloak."
}

# Wait until the imported realm answers its public discovery endpoint, the
# same readiness gate the osac-infra keycloak-set-passwords hook uses. The
# realm must be reachable at the issuer URL the OSAC services will use.
# --fail makes a 404 (realm not yet served) count as not ready.
wait_for_keycloak_realm() {
    local attempt
    for attempt in $(seq 1 60); do
        if keycloak_curl -s --fail -o /dev/null "${KEYCLOAK_URL}/realms/${RUN_NAME}"; then
            echo "Realm ${RUN_NAME} is ready."
            return 0
        fi
        echo "  Waiting for realm ${RUN_NAME} to become ready (attempt ${attempt}/60, 300s total)..."
        sleep 5
    done
    die "Realm ${RUN_NAME} did not become ready within 300s. Check the outer Keycloak's logs."
}

# Set one realm user's password through the admin API. The payload is built
# with python3, not string interpolation, so the password can safely
# contain quotes, backslashes or other JSON-significant characters (the
# same construction as the keycloak-set-passwords hook's set_password).
set_realm_user_password() {
    local username="$1" password="$2"
    local token user_id payload
    token="$(keycloak_admin_token)"
    # exact=true is what makes this precise where the hook's lookup is not:
    # without it Keycloak matches substrings, so tenant1_admin would also
    # answer for tenant1.
    user_id="$(keycloak_curl -s --fail-with-body -G \
        "${KEYCLOAK_URL}/admin/realms/${RUN_NAME}/users" \
        -H "Authorization: Bearer ${token}" \
        --data-urlencode "username=${username}" \
        --data-urlencode "exact=true" \
        | python3 -c "import json,sys; users=json.load(sys.stdin); print(users[0]['id'] if users else '')")"
    [[ -n "${user_id}" ]] \
        || die "User ${username} is missing from realm ${RUN_NAME}. The realm import did not bring the fixture users."
    payload="$(python3 -c "import json,sys; print(json.dumps({'type': 'password', 'value': sys.argv[1], 'temporary': False}))" "${password}")"
    keycloak_curl -s --fail-with-body -X PUT \
        "${KEYCLOAK_URL}/admin/realms/${RUN_NAME}/users/${user_id}/reset-password" \
        -H "Authorization: Bearer ${token}" \
        -H 'Content-Type: application/json' \
        -d "${payload}" >/dev/null
    echo "Set password for user ${username} in realm ${RUN_NAME}."
}

# Set every fixture user's password to the outer cluster's
# default-user-password, mirroring the osac-infra keycloak-set-passwords
# hook: the realm import brings the users without credentials, and the e2e
# suite logs in as them by password (OSAC_JWT_PASSWORD). The user list is
# derived from the rendered realm, not hardcoded, so a PR that adds a
# fixture user to realm.json gets its password set too. Service-account
# users are skipped: they authenticate by client secret, not password.
set_fixture_user_passwords() {
    local username
    while IFS= read -r username; do
        set_realm_user_password "${username}" "${KEYCLOAK_DEFAULT_USER_PASSWORD}"
    done < <(python3 - "${WORK_DIR}/realm.json" <<'PYEOF'
import json
import sys

with open(sys.argv[1]) as source:
    realm = json.load(source)
for user in realm.get("users", []):
    username = user.get("username", "")
    if username and not username.startswith("service-account-"):
        print(username)
PYEOF
    )
}

# Grant the controller client's service-account user the realm-management
# roles the fulfillment service needs to manage organizations, users and
# tenant service-account clients through the admin API: create-client and
# view-clients, with manage-clients explicitly revoked. Mirrors the
# osac-infra keycloak-set-passwords hook's ensure_controller_idp_roles,
# against the run's realm.
grant_controller_realm_management_roles() {
    local token realm_management_id controller_id user_id
    local create_role view_role manage_role payload
    token="$(keycloak_admin_token)"
    realm_management_id="$(keycloak_curl -s --fail-with-body -G \
        "${KEYCLOAK_URL}/admin/realms/${RUN_NAME}/clients" \
        -H "Authorization: Bearer ${token}" \
        --data-urlencode "clientId=realm-management" \
        --data-urlencode "exact=true" \
        | python3 -c "import json,sys; print(next((x['id'] for x in json.load(sys.stdin) if x['clientId']=='realm-management'), ''))")"
    controller_id="$(keycloak_curl -s --fail-with-body -G \
        "${KEYCLOAK_URL}/admin/realms/${RUN_NAME}/clients" \
        -H "Authorization: Bearer ${token}" \
        --data-urlencode "clientId=osac-controller" \
        --data-urlencode "exact=true" \
        | python3 -c "import json,sys; print(next((x['id'] for x in json.load(sys.stdin) if x['clientId']=='osac-controller'), ''))")"
    [[ -n "${realm_management_id}" && -n "${controller_id}" ]] \
        || die "Clients realm-management or osac-controller are missing from realm ${RUN_NAME}; the realm import did not bring them."
    user_id="$(keycloak_curl -s --fail-with-body \
        "${KEYCLOAK_URL}/admin/realms/${RUN_NAME}/clients/${controller_id}/service-account-user" \
        -H "Authorization: Bearer ${token}" \
        | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")"
    [[ -n "${user_id}" ]] \
        || die "The osac-controller client has no service-account user in realm ${RUN_NAME}."

    create_role="$(keycloak_curl -s --fail-with-body \
        "${KEYCLOAK_URL}/admin/realms/${RUN_NAME}/clients/${realm_management_id}/roles/create-client" \
        -H "Authorization: Bearer ${token}")"
    view_role="$(keycloak_curl -s --fail-with-body \
        "${KEYCLOAK_URL}/admin/realms/${RUN_NAME}/clients/${realm_management_id}/roles/view-clients" \
        -H "Authorization: Bearer ${token}")"
    manage_role="$(keycloak_curl -s --fail-with-body \
        "${KEYCLOAK_URL}/admin/realms/${RUN_NAME}/clients/${realm_management_id}/roles/manage-clients" \
        -H "Authorization: Bearer ${token}")"

    payload="$(python3 -c "import json,sys; print(json.dumps([json.loads(sys.argv[1]),json.loads(sys.argv[2])]))" "${create_role}" "${view_role}")"
    keycloak_curl -s --fail-with-body -X POST \
        "${KEYCLOAK_URL}/admin/realms/${RUN_NAME}/users/${user_id}/role-mappings/clients/${realm_management_id}" \
        -H "Authorization: Bearer ${token}" -H 'Content-Type: application/json' \
        -d "${payload}" >/dev/null
    payload="$(python3 -c "import json,sys; print(json.dumps([json.loads(sys.argv[1])]))" "${manage_role}")"
    keycloak_curl -s --fail-with-body -X DELETE \
        "${KEYCLOAK_URL}/admin/realms/${RUN_NAME}/users/${user_id}/role-mappings/clients/${realm_management_id}" \
        -H "Authorization: Bearer ${token}" -H 'Content-Type: application/json' \
        -d "${payload}" >/dev/null
    echo "Granted create-client and view-clients to the osac-controller service account in realm ${RUN_NAME} (manage-clients revoked)."
}

# Extend the authenticated client-registration policies with the protocol
# mappers and the client scope the fulfillment service's dynamically
# registered tenant service-account clients carry (fulfillment-service
# internal/idp/service_account_client.go registers them through
# /clients-registrations/default with oidc-hardcoded-claim-mapper,
# oidc-audience-mapper and the service_account scope). Mirrors the
# osac-infra keycloak-set-passwords hook's
# ensure_service_account_registration_policies.
ensure_service_account_registration_policies() {
    local token components provider component component_id
    token="$(keycloak_admin_token)"
    components="$(keycloak_curl -s --fail-with-body -G \
        "${KEYCLOAK_URL}/admin/realms/${RUN_NAME}/components" \
        -H "Authorization: Bearer ${token}" \
        --data-urlencode "type=org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy")"
    for provider in allowed-protocol-mappers allowed-client-templates; do
        component="$(python3 -c '
import json
import sys

provider, data = sys.argv[1], json.loads(sys.argv[2])
matches = [c for c in data if c.get("providerId") == provider and c.get("subType") == "authenticated"]
if len(matches) != 1:
    raise SystemExit("authenticated client registration policy missing or ambiguous")
component = matches[0]
config = component.setdefault("config", {})
key = "allowed-protocol-mapper-types" if provider == "allowed-protocol-mappers" else "allowed-client-scopes"
required = ("oidc-hardcoded-claim-mapper", "oidc-audience-mapper", "oidc-usersessionmodel-note-mapper") if provider == "allowed-protocol-mappers" else ("service_account",)
config[key] = list(dict.fromkeys(config.get(key, []) + list(required)))
print(json.dumps(component))
' "${provider}" "${components}")"
        component_id="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['id'])" "${component}")"
        keycloak_curl -s --fail-with-body -X PUT \
            "${KEYCLOAK_URL}/admin/realms/${RUN_NAME}/components/${component_id}" \
            -H "Authorization: Bearer ${token}" -H 'Content-Type: application/json' \
            -d "${component}" >/dev/null
    done
    echo "Extended the authenticated client-registration policies of realm ${RUN_NAME} for service-account clients."
}

# Keep registration access tokens reusable across retries, so the
# fulfillment service can recover an ambiguous service-account client
# registration (fulfillment-service's recoverAmbiguousServiceAccountCreation
# re-reads and re-registers). Mirrors the hook's
# ensure_service_account_registration_token_policy: a client profile that
# disables registration-access-token rotation, bound by a policy to clients
# updated by registration token.
ensure_service_account_registration_token_policy() {
    local token profiles policies payload
    token="$(keycloak_admin_token)"
    profiles="$(keycloak_curl -s --fail-with-body -G \
        "${KEYCLOAK_URL}/admin/realms/${RUN_NAME}/client-policies/profiles" \
        -H "Authorization: Bearer ${token}" \
        --data-urlencode "include-global-profiles=false")"
    payload="$(python3 -c '
import json
import sys

profile_name = "osac-service-account-registration-token-reuse"
executor_name = "registration-access-token-rotation-disabled"
response = json.loads(sys.argv[1])
profiles = response.setdefault("profiles", [])
if not isinstance(profiles, list):
    raise SystemExit("invalid Keycloak client profile response")
profile = next((item for item in profiles if item.get("name") == profile_name), None)
if profile is None:
    profile = {"name": profile_name, "executors": []}
    profiles.append(profile)
profile["description"] = "Keep OSAC service-account registration tokens reusable for retries"
executors = profile.setdefault("executors", [])
executor = next((item for item in executors if item.get("executor") == executor_name), None)
if executor is None:
    executors.append({"executor": executor_name, "configuration": {}})
else:
    executor["configuration"] = {}
print(json.dumps({"profiles": profiles}))
' "${profiles}")"
    keycloak_curl -s --fail-with-body -X PUT \
        "${KEYCLOAK_URL}/admin/realms/${RUN_NAME}/client-policies/profiles" \
        -H "Authorization: Bearer ${token}" -H 'Content-Type: application/json' \
        -d "${payload}" >/dev/null

    policies="$(keycloak_curl -s --fail-with-body -G \
        "${KEYCLOAK_URL}/admin/realms/${RUN_NAME}/client-policies/policies" \
        -H "Authorization: Bearer ${token}" \
        --data-urlencode "include-global-profiles=false")"
    payload="$(python3 -c '
import json
import sys

profile_name = "osac-service-account-registration-token-reuse"
policy_name = "osac-service-account-registration-token-reuse"
response = json.loads(sys.argv[1])
policies = response.setdefault("policies", [])
if not isinstance(policies, list):
    raise SystemExit("invalid Keycloak client policy response")
policy = next((item for item in policies if item.get("name") == policy_name), None)
if policy is None:
    policy = {"name": policy_name}
    policies.append(policy)
policy.update({
    "description": "Keep OSAC service-account registration tokens reusable for retries",
    "enabled": True,
    "conditions": [{
        "condition": "client-updater-context",
        "configuration": {"update-client-source": ["ByRegistrationAccessToken"]},
    }],
    "profiles": [profile_name],
})
print(json.dumps({"policies": policies}))
' "${policies}")"
    keycloak_curl -s --fail-with-body -X PUT \
        "${KEYCLOAK_URL}/admin/realms/${RUN_NAME}/client-policies/policies" \
        -H "Authorization: Bearer ${token}" -H 'Content-Type: application/json' \
        -d "${payload}" >/dev/null
    echo "Ensured reusable registration tokens for service-account clients in realm ${RUN_NAME}."
}

# Write the per-run Keycloak client credentials into the vcluster, in the
# shape the osac-infra chart's create-controller-credentials and
# create-ui-backend-credentials hooks create in a normal install: keys
# client-id and client-secret. Unlike v0, which copied the outer realm's
# shared credentials, each run's realm has its own client secrets.
write_keycloak_credentials_into_vcluster() {
    local controller_secret ui_backend_secret
    controller_secret="$(cat "${WORK_DIR}/controller-client-secret")"
    ui_backend_secret="$(cat "${WORK_DIR}/ui-backend-client-secret")"

    create_vcluster_secret_literals "${RUN_NAME}-fulfillment-controller-credentials" \
        --from-literal="client-id=osac-controller" \
        --from-literal="client-secret=${controller_secret}"
    create_vcluster_secret_literals "${RUN_NAME}-osac-ui-backend-credentials" \
        --from-literal="client-id=osac-ui-backend" \
        --from-literal="client-secret=${ui_backend_secret}"
}

###############################################################################
# AAP service account
###############################################################################

# Create the service account the shared AAP uses to launch this run's job
# pods inside the vcluster, bind it to cluster-admin there, and store a
# one-year token as the secret the osac chart's aap.vcluster.tokenSecret
# names. The AAP controller reaches the vcluster API by the service DNS
# name in aap.vcluster.apiServerUrl (POC-verified).
create_aap_service_account() {
    kubectl --kubeconfig "${VCLUSTER_KUBECONFIG}" --namespace "${VCLUSTER_OSAC_NAMESPACE}" \
        create serviceaccount "${RUN_NAME}-aap" \
        --dry-run=client -o yaml \
        | kubectl --kubeconfig "${VCLUSTER_KUBECONFIG}" apply -f -

    kubectl --kubeconfig "${VCLUSTER_KUBECONFIG}" \
        create clusterrolebinding "${RUN_NAME}-aap" \
        --clusterrole=cluster-admin \
        --serviceaccount="${VCLUSTER_OSAC_NAMESPACE}:${RUN_NAME}-aap" \
        --dry-run=client -o yaml \
        | kubectl --kubeconfig "${VCLUSTER_KUBECONFIG}" apply -f -

    local aap_token
    aap_token="$(kubectl --kubeconfig "${VCLUSTER_KUBECONFIG}" \
        --namespace "${VCLUSTER_OSAC_NAMESPACE}" create token "${RUN_NAME}-aap" \
        --duration "${AAP_TOKEN_DURATION}")"
    [[ -n "${aap_token}" ]] \
        || die "Could not mint a token for service account ${RUN_NAME}-aap in the vcluster."

    create_vcluster_secret_literals "${RUN_NAME}-aap-vcluster-token" \
        --from-literal="token=${aap_token}"

    precreate_aap_identity "${aap_token}"
}

# Create the run's AAP organization and vcluster credential directly, before
# the chart's config-as-code runs. The config-as-code dispatches its creates
# asynchronously and collects failures for the end of the play, so under AAP
# load a failed cold create turns into a half-installed run whose
# instance-group verification spins for minutes while helm times out on the
# pre-install hook. Creating both here, synchronously and fail-loud, means
# the config-as-code only performs idempotent updates on existing objects.
# A re-run with a fresh vcluster also repairs a credential that still holds
# a previous incarnation's token (the orphan-adoption failure mode).
precreate_aap_identity() {
    local vcluster_token="$1"
    local api_token org_id cred_id
    api_token="$(kubectl --namespace "${AAP_NAMESPACE}" get secret osac-aap-api-token \
        -o jsonpath='{.data.token}' | base64 -d)" \
        || die "Cannot read the osac-aap-api-token Secret in ${AAP_NAMESPACE}: the AAP lane needs it to pre-create the run's organization and credential."
    [[ -n "${api_token}" ]] || die "The osac-aap-api-token Secret in ${AAP_NAMESPACE} is empty."

    local api="${AAP_CONTROLLER_URL}/api/controller/v2"
    # -k: the AAP route's certificate is signed by the lab's CA, which is
    # not in this process's trust store; every other AAP client on this lab
    # (the teardown, the CI tooling) skips verification the same way.
    local auth=(-H "Authorization: Bearer ${api_token}" -H "Content-Type: application/json" -s -k --fail-with-body)

    # Organization: create when missing. The API answers 400 with "already
    # exists" in the body on a duplicate, which --fail-with-body surfaces.
    org_id="$(curl "${auth[@]}" "${api}/organizations/?name=${RUN_NAME}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["results"][0]["id"] if d["count"] else "")')" \
        || die "Cannot query the AAP for organization ${RUN_NAME}."
    if [[ -z "${org_id}" ]]; then
        org_id="$(curl "${auth[@]}" -X POST -d "{\"name\":\"${RUN_NAME}\",\"description\":\"vcluster E2E run ${RUN_NAME}\"}" \
            "${api}/organizations/" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')" \
            || die "Cannot create AAP organization ${RUN_NAME}."
        echo "Pre-created AAP organization ${RUN_NAME} (id ${org_id})."
    fi

    # Credential: create when missing, refresh the token when it exists, so
    # both a cold run and a re-run over a stale incarnation converge.
    cred_id="$(curl "${auth[@]}" "${api}/credentials/?name=${RUN_NAME}-vcluster" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["results"][0]["id"] if d["count"] else "")')" \
        || die "Cannot query the AAP for credential ${RUN_NAME}-vcluster."
    local cred_json
    cred_json="$(RUN_NAME="${RUN_NAME}" ORG_ID="${org_id}" \
        HOST="https://${RUN_NAME}.${HOST_NAMESPACE}.svc.cluster.local" \
        TOKEN="${vcluster_token}" python3 -c '
import json, os
print(json.dumps({"name": os.environ["RUN_NAME"] + "-vcluster",
                  "description": "Bearer token for the vcluster API of " + os.environ["RUN_NAME"],
                  "organization": int(os.environ["ORG_ID"]),
                  "credential_type": 17,
                  "inputs": {"host": os.environ["HOST"], "verify_ssl": False,
                             "bearer_token": os.environ["TOKEN"]}}))')"
    if [[ -z "${cred_id}" ]]; then
        curl "${auth[@]}" -X POST -d "${cred_json}" "${api}/credentials/" >/dev/null \
            || die "Cannot create AAP credential ${RUN_NAME}-vcluster."
        echo "Pre-created AAP credential ${RUN_NAME}-vcluster."
    else
        curl "${auth[@]}" -X PATCH -d "${cred_json}" "${api}/credentials/${cred_id}/" >/dev/null \
            || die "Cannot refresh AAP credential ${RUN_NAME}-vcluster (id ${cred_id})."
        echo "Refreshed AAP credential ${RUN_NAME}-vcluster (id ${cred_id})."
    fi
}

###############################################################################
# Per-run values overlay
###############################################################################

# Write the per-run values overlay the printed `make install-osac` command
# passes to the osac chart. It carries only what is named after the run and
# the run's suite; everything every vcluster run shares stays in the
# vcluster-ci profile. The suite keys mirror the blocks documented in the
# profile (osac-installer/values/vcluster-ci/instance.yaml, "Suite
# selection"); keep the two in sync.
#
# The database secret names (<run>-db-config, <run>-db-client-cert,
# <run>-db-metering-config, <run>-db-metering-client-cert) are derived by
# the chart from global.instanceId.
#
# operator.aap.organization and operator.aap.templatePrefix are NOT derived
# from global.instanceId: the operator chart renders OSAC_AAP_ORGANIZATION
# and OSAC_AAP_TEMPLATE_PREFIX from its values alone, and both default to
# "osac" (osac-operator/charts/operator/values.yaml; the binary's own
# fallback is "osac" too). An overlay that left them unset would point this
# run's job templates and organization at the shared install's, so the
# overlay below sets both to the run name.
#
# global.eventTopicPrefix must stay non-empty. Every osac chart renders the
# Kafka topic prefix with `default "osac.events."`, and Helm's default treats
# the empty string as unset. An empty value here would therefore point the
# run's publisher, gRPC server and metering consumer at the shared
# osac.events. topics while the run's KafkaUser ACLs (create_kafka_users)
# cover only <run>.events. The publisher would then fail every topic
# Describe with "not authorized" and the changes outbox would stop draining.
write_instance_values() {
    [[ -n "${EVENT_TOPIC_PREFIX}" ]] \
        || die "EVENT_TOPIC_PREFIX is empty; refusing to share osac.events."

    cat >"${INSTANCE_VALUES_FILE}" <<EOF
# Per-run values overlay for the osac chart, generated by
# scripts/vcluster-run-setup.sh for run ${RUN_NAME} (suite: ${SUITE}).
# Pass it to 'make install-osac' through EXTRA_HELM_ARGS="--values <this
# file>". The suite keys mirror the suite blocks documented in
# osac-installer/values/vcluster-ci/instance.yaml.
global:
  instanceId: ${RUN_NAME}
  eventTopicPrefix: "${EVENT_TOPIC_PREFIX}"
EOF
    write_suite_global_values
    cat >>"${INSTANCE_VALUES_FILE}" <<EOF
service:
  auth:
    issuerUrl: https://keycloak-keycloak.${OUTER_DOMAIN}/realms/${RUN_NAME}
    controllerCredentials:
      - secret:
          name: ${RUN_NAME}-fulfillment-controller-credentials
          items:
            - key: client-id
              param: client-id
            - key: client-secret
              param: client-secret
  idp:
    credentials:
      - secret:
          name: ${RUN_NAME}-fulfillment-controller-credentials
          items:
            - key: client-id
              param: client-id
            - key: client-secret
              param: client-secret
  database:
    connection:
      - secret:
          name: ${RUN_NAME}-db-config
          items:
            - key: url
              param: url
      - secret:
          name: ${RUN_NAME}-db-client-cert
          items:
            - key: tls.crt
              param: sslcert
            - key: tls.key
              param: sslkey
            - key: ca.crt
              param: sslrootcert
  kafka:
    connection:
      - secret:
          name: ${KAFKA_CONNECTION_SECRET}
          items:
            - key: brokers
              param: brokers
            - key: user
              param: user
            - key: password
              param: password
  vault:
    namespace: ${RUN_NAME}
    keycloakIssuerUrl: https://keycloak-keycloak.${OUTER_DOMAIN}/realms/${RUN_NAME}
    # The vault client authenticates with the same run client as the auth
    # and idp blocks; the profile names the shared secret, so the overlay
    # points it at the run's.
    credentials:
    - secret:
        name: ${RUN_NAME}-fulfillment-controller-credentials
        items:
        - key: client-secret
          param: client-secret
metering:
  database:
    connection:
      - secret:
          name: ${RUN_NAME}-db-metering-config
          items:
            - key: url
              param: url
      - secret:
          name: ${RUN_NAME}-db-metering-client-cert
          items:
            - key: tls.crt
              param: sslcert
            - key: tls.key
              param: sslkey
            - key: ca.crt
              param: sslrootcert
ui:
  auth:
    oidcIssuerURL: https://keycloak-keycloak.${OUTER_DOMAIN}/realms/${RUN_NAME}
    serviceAccount:
      credentialsSecret: ${RUN_NAME}-osac-ui-backend-credentials
operator:
  fulfillment:
    # The operator reads the same Keycloak client credentials as the
    # fulfillment service (operator.fulfillment.credentials in
    # values/vmaas-ci/instance.yaml, which names the unprefixed secret);
    # a run's operator must read this run's client.
    credentials:
      secretName: ${RUN_NAME}-fulfillment-controller-credentials
  aap:
    templatePrefix: ${RUN_NAME}
    # Jobs launch in the run's own AAP organization (created by the
    # config-as-code hook from global.instanceId); the chart default
    # "osac" is the outer install's shared organization. Labels and
    # job templates are per run.
    organization: ${RUN_NAME}
    # The shared AAP by its outer route: the chart's in-cluster default
    # (http://osac-aap/api/controller) does not resolve inside a vcluster,
    # and the operator's client wants the /api/controller path.
    url: ${AAP_CONTROLLER_URL}/api/controller
EOF
    write_suite_operator_values
    cat >>"${INSTANCE_VALUES_FILE}" <<EOF
aap:
  vcluster:
    apiServerUrl: https://${RUN_NAME}.${HOST_NAMESPACE}.svc.cluster.local
    tokenSecret:
      name: ${RUN_NAME}-aap-vcluster-token
      key: token
EOF
    write_suite_aap_values
    write_suite_tail_values
    echo "Wrote per-run values overlay to ${INSTANCE_VALUES_FILE}."
}

# The suite's keys under global: services and, for BMaaS, the cleared
# Kubernetes Manager (BMaaS has no VM overlay networking and its
# SecurityGroups keep their ICMP rules; see values/bmaas-ci).
write_suite_global_values() {
    case "${SUITE}" in
        vmaas)
            cat >>"${INSTANCE_VALUES_FILE}" <<EOF
  services:
    vmaas:
      enabled: true
EOF
            ;;
        bmaas)
            cat >>"${INSTANCE_VALUES_FILE}" <<EOF
  services:
    vmaas:
      enabled: false
    bmaas:
      enabled: true
  networking:
    k8sManager: ""
EOF
            ;;
        caas)
            # vmaas stays enabled: CaaS registers its RHCOS DiskImage
            # through the VMaaS-owned DiskImages API, but provisions no
            # VMs. agentless_net is the logical fabric manager CaaS BMI
            # validation needs (see values/caas-ci).
            cat >>"${INSTANCE_VALUES_FILE}" <<EOF
  services:
    caas:
      enabled: true
    bmaas:
      enabled: true
    vmaas:
      enabled: true
  networking:
    fabricManager: agentless_net
EOF
            ;;
    esac
}

# The suite's keys under operator:. CaaS disables the ComputeInstance
# controller (vmaas is on for the DiskImages API only) and trusts the
# outer cluster's ingress CA for MCE's Assisted Service ignition URL,
# read through the remote kubeconfig.
write_suite_operator_values() {
    if [[ "${SUITE}" == "caas" ]]; then
        cat >>"${INSTANCE_VALUES_FILE}" <<'EOF'
  controllers:
    computeInstance: false
  ignition:
    trustIngressCA: true
EOF
    fi
}

# The suite's keys under aap: the cluster fulfillment instance group the
# bmaas-ci and caas-ci profiles enable. CaaS also passes the hosted
# cluster settings: SingleReplica availability (one hosted cluster per
# test, on shared machines) and the base domain -- the chart's default
# derives it from the cluster's ingress lookup, which a vcluster has no
# API for, and hosted clusters live in the outer cluster.
write_suite_aap_values() {
    if [[ "${SUITE}" == "bmaas" ]]; then
        cat >>"${INSTANCE_VALUES_FILE}" <<'EOF'
  instanceGroups:
    clusterFulfillment:
      enabled: true
EOF
    elif [[ "${SUITE}" == "caas" ]]; then
        cat >>"${INSTANCE_VALUES_FILE}" <<EOF
  instanceGroups:
    clusterFulfillment:
      enabled: true
      config:
        HOSTED_CLUSTER_CONTROLLER_AVAILABILITY_POLICY: SingleReplica
        HOSTED_CLUSTER_INFRASTRUCTURE_AVAILABILITY_POLICY: SingleReplica
        HOSTED_CLUSTER_BASE_DOMAIN: hosted.${OUTER_DOMAIN#apps.}
        # The guest ingress VIP range the external-access step reserves
        # from. The chart key defaults to empty and the job refuses to run
        # without it. Both addresses must be spelled in full: the chart
        # schema and the claim module's parse_range reject the short
        # 192.168.161.240-250 form. One shared range serves every parallel
        # CaaS run on this lab: claims live in one ConfigMap keyed by
        # ClusterOrder UID, and eleven addresses cover three runs.
        CI_GUEST_INGRESS_IP_RANGE: 192.168.161.240-192.168.161.250
        # The RFC 2136 DNS driver: the BIND server on the lab host serves
        # the hosted-cluster zone and accepts signed updates. The job pods
        # reach it from the outer cluster's network; the workstation does
        # not, which is fine -- only the jobs talk to it. Proven by the
        # first CaaS validation run.
        DNS_CLASS: dns.rfc2136.dns
        DNS_ZONE: hosted.${OUTER_DOMAIN#apps.}
        EXTERNAL_ACCESS_BASE_DOMAIN: hosted.${OUTER_DOMAIN#apps.}
        EXTERNAL_ACCESS_SUPPORTED_BASE_DOMAINS: hosted.${OUTER_DOMAIN#apps.}
        DNS_RFC2136_SERVER: 10.1.155.16
        DNS_RFC2136_PORT: "5333"
        DNS_RFC2136_PROTOCOL: udp
        DNS_RFC2136_KEY_NAME: osac-caas
        DNS_RFC2136_KEY_ALGORITHM: hmac-sha256
        DNS_SERVER: 10.1.155.16
      secret:
        DNS_RFC2136_KEY_SECRET: G4Jghff4HhsEx8sRnlKk+PiJaNbPPtEgZlgbdzr9qIo=
EOF
    fi
}

# The suite's top-level blocks: the BMF operator wiring for the b and c
# suites (CaaS runs the BMF operator too, for its bare-metal workers) and
# the cluster versions CaaS orders reference.
write_suite_tail_values() {
    if [[ "${SUITE}" != "bmaas" && "${SUITE}" != "caas" ]]; then
        return
    fi
    cat >>"${INSTANCE_VALUES_FILE}" <<EOF
bmf:
  metal3:
    enabled: true
    namespace: ${RUN_NAME}-hosts
  env:
    # The BMF operator reaches the shared AAP by its outer route, like the
    # osac-operator above, and launches this run's job templates: the
    # run's AAP organization prefixes their names.
    aapUrl: ${AAP_CONTROLLER_URL}/api/controller
    aapInsecureSkipVerify: "true"
    aapTemplatePrefix: ${RUN_NAME}
  secretAPI:
    issuerUrl: https://keycloak-keycloak.${OUTER_DOMAIN}/realms/${RUN_NAME}
    credentialsSecret: ${RUN_NAME}-fulfillment-controller-credentials
EOF
    if [[ "${SUITE}" == "caas" ]]; then
        cat >>"${INSTANCE_VALUES_FILE}" <<'EOF'
clusterVersions:
  enabled: true
  versions:
  - version: "4.20.0"
    image: "quay.io/openshift-release-dev/ocp-release:4.20.0-multi"
  - version: "4.22.0"
    image: "quay.io/openshift-release-dev/ocp-release:4.22.0-multi"
    default: true
    diskImage:
      name: rhcos-4-22
      sourceRef: oci://quay.io/osac-project/rhcos:4.22.0
EOF
    fi
}

###############################################################################
# Output
###############################################################################

print_next_steps() {
    # The same settings 'make install-osac' derives and passes for a normal
    # install (osac-installer/Makefile, install-osac target): DOMAIN from
    # oc get ingresses.config/cluster, then service.externalHostname,
    # service.internalHostname and ui.externalHostname. The vcluster has no
    # OpenShift ingress config for the Makefile to read, so CLUSTER_DOMAIN
    # (the Makefile override landing with the osac chart package) carries
    # the domain and the route hostnames are passed through EXTRA_HELM_ARGS,
    # run-prefixed so concurrent runs do not collide once the run routes are
    # published. The issuer URLs and credential references live in the
    # overlay instead.
    #
    # The AAP settings: the umbrella aliases the osac-aap dependency as
    # `aap`, so the external-instance values are aap.controllerUrl (the
    # shared AAP's controller URL) and
    # aap.bootstrap.adminPasswordSecret.name (the run setup copies the
    # outer cluster's <aap-instance>-admin-password secret into the
    # vcluster under that name; the key defaults to password). Both exist
    # on the unified osac branch vcluster-e2e (PR osac#2566), not on osac
    # main: with aap.aap.instance.enabled=false the chart requires them.
    #
    # REMOTE_KUBECONFIG is the outer cluster's kubeconfig, the one this
    # script ran with: PROFILE=vcluster-ci runs the install in remote mode
    # and make install-osac fails without it, because the operator, the
    # AAP config-as-code job and the BMF operator all reach the outer
    # cluster through that kubeconfig.
    local extra_helm_args
    extra_helm_args="--values ${INSTANCE_VALUES_FILE}"
    extra_helm_args="${extra_helm_args} --set service.externalHostname=fulfillment-api-${RUN_NAME}.${OUTER_DOMAIN}"
    extra_helm_args="${extra_helm_args} --set service.internalHostname=fulfillment-internal-api-${RUN_NAME}.${OUTER_DOMAIN}"
    extra_helm_args="${extra_helm_args} --set ui.externalHostname=osac-ui-${RUN_NAME}.${OUTER_DOMAIN}"
    extra_helm_args="${extra_helm_args} --set aap.controllerUrl=${AAP_CONTROLLER_URL}"
    extra_helm_args="${extra_helm_args} --set aap.bootstrap.adminPasswordSecret.name=${AAP_INSTANCE}-admin-password"

    echo
    echo "Run setup complete."
    echo
    echo "vcluster kubeconfig: ${VCLUSTER_KUBECONFIG}"
    echo
    echo "Install OSAC into the vcluster by running this from the osac"
    echo "monorepo's osac-installer directory. AAP_LICENSE_FILE is required"
    echo "for PLATFORM=openshift; point it at the AAP license zip. After"
    echo "the install, publish the run's routes with"
    echo "scripts/vcluster-publish-routes.sh (design step 6):"
    echo
    echo "  KUBECONFIG=${VCLUSTER_KUBECONFIG} \\"
    echo "  REMOTE_KUBECONFIG=${KUBECONFIG} \\"
    echo "  CLUSTER_DOMAIN=${OUTER_DOMAIN} \\"
    echo "  AAP_LICENSE_FILE=/path/to/license.zip \\"
    echo "  make install-osac PLATFORM=openshift PROFILE=vcluster-ci NS=osac \\"
    echo "      EXTRA_HELM_ARGS='${extra_helm_args}'"
    echo
    echo "Run the suite with the run's realm and routes:"
    echo "  OSAC_KEYCLOAK_REALM=${RUN_NAME}"
    if [[ "${SUITE}" == "bmaas" || "${SUITE}" == "caas" ]]; then
        echo "  OSAC_BMH_NAMESPACE=${BMH_NAMESPACE}"
    fi
    echo
    echo "The overlay selects the ${SUITE} suite from the run name's suite"
    echo "letter; the suite keys it sets are documented in the vcluster-ci"
    echo "profile (osac-installer/values/vcluster-ci/instance.yaml)."
    echo "PROFILE=vcluster-ci, the chart's vcluster support and the"
    echo "CLUSTER_DOMAIN Makefile override land with the osac chart work"
    echo "(the unified osac branch vcluster-e2e, PR osac#2566), which also"
    echo "carries global.instanceId, aap.vcluster.* and the external-AAP"
    echo "values aap.controllerUrl and aap.bootstrap.adminPasswordSecret."
    echo "Until it merges, the umbrella schema on osac main rejects the"
    echo "overlay's unknown keys. Tear the run down with"
    echo "scripts/vcluster-run-teardown.sh ${RUN_NAME}."
}

###############################################################################
# Main
###############################################################################

main() {
    local positional_args=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --realm-json)
                [[ $# -ge 2 ]] || die "--realm-json needs a value."
                REALM_JSON_SOURCE="$2"
                shift 2
                ;;
            --recreate)
                RECREATE="true"
                shift
                ;;
            --output-dir)
                [[ $# -ge 2 ]] || die "--output-dir needs a value."
                OUTPUT_DIR="$2"
                shift 2
                ;;
            --bmh-network)
                [[ $# -ge 2 ]] || die "--bmh-network needs a value."
                BMH_NETWORK="$2"
                shift 2
                ;;
            --skip-bmhs)
                # Defer the run's BareMetalHost provisioning to the machine
                # that runs libvirt (the BMH script drives virsh, sushy-tools
                # and the virtual-media cache, which must sit where the VMs
                # live). The caller runs scripts/vcluster-run-bmhs.sh there
                # with this run's name; the suites only need the hosts
                # available before they start, not before the vcluster
                # installs.
                SKIP_BMHS="true"
                shift
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
            --aap-controller-url)
                [[ $# -ge 2 ]] || die "--aap-controller-url needs a value."
                AAP_CONTROLLER_URL="$2"
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
        || die "Exactly one run name is required. Usage: $0 RUN_NAME --realm-json PATH [--recreate] [options]."
    RUN_NAME="${positional_args[0]}"
    [[ "${RUN_NAME}" =~ ^[a-z0-9]([-a-z0-9]{0,10}[a-z0-9])?$ ]] \
        || die "Run name '${RUN_NAME}' must be a DNS label of lowercase letters, digits and '-', at most 12 characters."
    suite_from_run_name

    # Every shared-service object this run touches, named after the run.
    KAFKA_USER_NAME="${RUN_NAME}-fulfillment-service"
    KAFKA_CONNECTION_SECRET="${RUN_NAME}-fulfillment-service-kafka"
    METERING_KAFKA_USERS=("${RUN_NAME}-osac-metering" "${RUN_NAME}-osac-metering-echo-adapter")
    EVENT_TOPIC_PREFIX="${RUN_NAME}.events."
    SYSTEM_GROUP_PREFIX="${RUN_NAME}.system."
    HEARTBEAT_TOPIC_PREFIX="${RUN_NAME}.metering.heartbeat."
    DLQ_TOPIC_PREFIX="${RUN_NAME}.metering.dlq."
    INFERENCE_TOPIC="${RUN_NAME}.metering.inference"
    INFERENCE_DLQ_TOPIC="${RUN_NAME}.metering.inference.dlq"

    HOST_NAMESPACE="vc-${RUN_NAME}"
    BMH_NAMESPACE="${RUN_NAME}-hosts"
    OUTPUT_DIR="${OUTPUT_DIR:-${REPO_ROOT}/.vcluster-runs/${RUN_NAME}}"
    VCLUSTER_KUBECONFIG="${OUTPUT_DIR}/kubeconfig"
    VALUES_FILE="${OUTPUT_DIR}/vcluster-values.yaml"
    INSTANCE_VALUES_FILE="${OUTPUT_DIR}/instance-values.yaml"

    check_all_preconditions
    ensure_orders_namespace
    mkdir -p "${OUTPUT_DIR}"
    setup_run_bmhs
    render_values
    install_vcluster
    write_vcluster_kubeconfig
    create_vcluster_namespaces
    install_vcluster_cert_manager
    prepare_vcluster_certificate_authority
    create_kafka_users
    create_kafka_connection_secret
    copy_metering_kafka_secrets
    render_realm_json
    import_keycloak_realm
    wait_for_keycloak_realm
    set_fixture_user_passwords
    grant_controller_realm_management_roles
    ensure_service_account_registration_policies
    ensure_service_account_registration_token_policy
    write_keycloak_credentials_into_vcluster
    create_aap_service_account
    copy_outer_secrets_into_vcluster
    write_instance_values
    print_next_steps
}

main "$@"
