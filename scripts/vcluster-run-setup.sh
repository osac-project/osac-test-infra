#!/usr/bin/env bash
# vcluster-run-setup.sh -- set up one OSAC E2E run inside a vcluster on a
# shared outer OpenShift cluster.
#
# v0 scope: a single run, no run-ID prefixing of shared-service objects.
#
# The script:
#   1. checks every precondition against the outer cluster and fails loudly;
#   2. renders scripts/vcluster-values.yaml.template with the discovered
#      parameters and installs the vcluster Helm release (version pinned);
#   3. mints a long-lived admin kubeconfig for the vcluster;
#   4. installs cert-manager inside the vcluster and copies the shared CI
#      certificate authority into it, so the osac chart can issue the
#      database client certificates itself;
#   5. creates the fulfillment and metering KafkaUsers in the outer
#      cluster's Kafka, waits for Strimzi to generate their secrets, and
#      copies them into the vcluster (the fulfillment connection secret into
#      osac, the metering secrets into an osac-kafka namespace the pods'
#      initContainers read by local API);
#   6. copies the outer-cluster secrets the OSAC chart expects to find in the
#      vcluster's osac namespace;
#   7. prints the `make install-osac` command that installs OSAC into the
#      vcluster, plus the path of the vcluster kubeconfig.
#
# Usage:
#   KUBECONFIG=/path/to/outer-kubeconfig ./scripts/vcluster-run-setup.sh RUN_NAME [options]
#
# Options:
#   --recreate                  replace an existing vcluster release
#   --output-dir PATH           where to write the run's files
#                               (default: <repo>/.vcluster-runs/RUN_NAME)
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
# The run name becomes the vcluster name and its namespace vc-<run name> in
# the outer cluster. Keep it at most 12 characters, lowercase letters and
# digits: generated names add prefixes to it and must stay within the
# 63-character DNS label limit.
#
# See docs/vcluster-run-setup.md for the end-to-end flow and the POC-derived
# troubleshooting notes.
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

# One year in seconds: the 8760h token duration the POC validated.
VCLUSTER_TOKEN_EXPIRATION_SECONDS=31536000

# Outer-cluster namespaces, fixed by `make install-infra PLATFORM=openshift
# PROFILE=vmaas-ci NS=osac` in the osac monorepo:
CERT_MANAGER_NAMESPACE="cert-manager" # shared CI certificate authority
OSAC_SHARED_NAMESPACE="osac"          # controller credentials and the CA bundle
KAFKA_NAMESPACE="osac-kafka"          # Strimzi Kafka cluster osac-kafka
OUTER_DNS_NAMESPACE="openshift-dns"
OUTER_DNS_SERVICE="dns-default"

# Destination namespace inside the vcluster. The printed make command uses
# NS=osac, so this is not configurable.
VCLUSTER_OSAC_NAMESPACE="osac"

# The metering chart's Deployments fetch their Kafka SASL credentials
# through the LOCAL API of whatever cluster OSAC runs in: their
# initContainers read Secrets from metering.kafka.clusterNamespace, which
# defaults to osac-kafka (osac-metering/charts/osac-metering/templates/
# _helpers.tpl, osac-metering.kafkaSecretsInitContainer). The vcluster-ci
# profile sets metering.kafka.strimziResources=false, so the run setup
# creates the users in the outer cluster and mirrors their secrets into an
# osac-kafka namespace inside the vcluster, under their natural names.
VCLUSTER_KAFKA_NAMESPACE="osac-kafka"
METERING_KAFKA_USERS=("osac-metering" "osac-metering-echo-adapter")

# Kafka user and connection secret, copied verbatim from the osac chart
# contract (osac-installer/charts/osac/templates/_helpers.tpl:
# osac.fulfillmentKafkaUserName = fulfillment-service,
# osac.fulfillmentKafkaSecretName = fulfillment-service-kafka,
# osac.fulfillmentKafkaBrokers = osac-kafka-kafka-bootstrap.osac-kafka.svc:9093).
KAFKA_USER_NAME="fulfillment-service"
KAFKA_CONNECTION_SECRET="fulfillment-service-kafka"
KAFKA_BROKERS="osac-kafka-kafka-bootstrap.osac-kafka.svc:9093"

# Default shared AAP instance, matching aap.aap.instance.name in
# osac-installer values (vmaas-ci/instance.yaml). Overridable because the
# outer cluster's AAP placement is not pinned by any chart yet.
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
RECREATE="false"
OUTPUT_DIR=""
AAP_INSTANCE="${AAP_INSTANCE_DEFAULT}"
AAP_NAMESPACE="${AAP_NAMESPACE_DEFAULT}"
AAP_CONTROLLER_URL=""

HOST_NAMESPACE=""
VCLUSTER_KUBECONFIG=""
VALUES_FILE=""

# Temp directory for secret payloads; removed on exit.
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

usage() {
    cat <<'EOF'
Usage:
  KUBECONFIG=/path/to/outer-kubeconfig ./scripts/vcluster-run-setup.sh RUN_NAME [options]

Options:
  --recreate                  replace an existing vcluster release
  --output-dir PATH           where to write the run's files
                              (default: <repo>/.vcluster-runs/RUN_NAME)
  --aap-instance NAME         shared AAP instance name (default: osac-aap)
  --aap-namespace NAMESPACE   namespace of the shared AAP instance
                              (default: osac)
  --aap-controller-url URL    external AAP controller URL; by default the
                              script reads the AAP route named
                              <aap-instance> in <aap-namespace>
  -h, --help                  show this help

The run name becomes the vcluster name and its namespace vc-<run name> in
the outer cluster. Keep it at most 12 characters, lowercase letters and
digits: generated names add prefixes to it and must stay within the
63-character DNS label limit.
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

check_all_preconditions() {
    require_command kubectl
    require_command helm
    require_command vcluster

    [[ -n "${KUBECONFIG:-}" ]] \
        || die "KUBECONFIG is not set. Point it at the outer cluster's kubeconfig."
    [[ -f "${KUBECONFIG}" ]] \
        || die "KUBECONFIG (${KUBECONFIG}) is not a file."

    echo "Checking outer cluster connectivity..."
    kubectl get --raw /readyz \
        || die "Cannot reach the outer cluster with KUBECONFIG=${KUBECONFIG}."

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

    # AAP admin password, created by the AAP operator for instance
    # ${AAP_INSTANCE} (the osac-aap chart reads <instance>-admin-password,
    # key password, in its bootstrap and create-token jobs).
    check_secret_keys "${AAP_NAMESPACE}" "${AAP_INSTANCE}-admin-password" password

    # Keycloak client credentials for the fulfillment controller, created by
    # the osac-infra create-controller-credentials hook in osacNamespace
    # (osac-installer/charts/osac-infra/files/hooks/create-controller-credentials.sh).
    check_secret_keys "${OSAC_SHARED_NAMESPACE}" fulfillment-controller-credentials client-id client-secret
    check_secret_keys "${OSAC_SHARED_NAMESPACE}" osac-ui-backend-credentials client-id client-secret

    # Shared CA bundle, written by trust-manager into osacNamespace
    # (osac-installer/charts/osac-infra/files/hooks/apply-ca-bundle.sh).
    check_configmap_keys "${OSAC_SHARED_NAMESPACE}" ca-bundle bundle.pem

    resolve_aap_controller_url
}

###############################################################################
# vcluster install and kubeconfig
###############################################################################

render_values() {
    VCLUSTER_API_SAN="${RUN_NAME}.${HOST_NAMESPACE}.svc.cluster.local"
    sed \
        -e "s|__VCLUSTER_UID__|${VCLUSTER_UID}|g" \
        -e "s|__VCLUSTER_API_SAN__|${VCLUSTER_API_SAN}|g" \
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

# Create the KafkaUsers in the outer cluster, the Strimzi objects the osac
# and osac-metering charts normally create in-cluster. Copied verbatim from:
#   - osac-installer/charts/osac/templates/fulfillment-kafka-user.yaml
#   - osac-metering/charts/osac-metering/templates/kafka-user.yaml
#   - osac-metering/charts/osac-metering/templates/echo-adapter-kafka-user.yaml
# as rendered with the charts' default kafka.clusterName/clusterNamespace
# (osac-kafka / osac-kafka). The vcluster-ci profile sets
# metering.kafka.strimziResources=false and kafka.enabled=false, so the
# charts will not create them. v0 keeps the charts' fixed user names and ACL
# prefixes; run-ID prefixing comes later.
create_kafka_users() {
    kubectl --namespace "${KAFKA_NAMESPACE}" apply -f - <<'EOF'
apiVersion: kafka.strimzi.io/v1
kind: KafkaUser
metadata:
  name: fulfillment-service
  namespace: osac-kafka
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
          name: osac.events.
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
          name: osac.system.
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
  name: osac-metering
  namespace: osac-kafka
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
          name: osac.events.
          patternType: prefix
        operations:
          - Read
          - Describe
        host: "*"
      - resource:
          type: topic
          name: osac.metering.heartbeat.
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
          name: osac.metering.dlq.
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
  name: osac-metering-echo-adapter
  namespace: osac-kafka
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
          name: osac.metering.heartbeat.
          patternType: prefix
        operations:
          - Read
          - Describe
        host: "*"
      - resource:
          type: topic
          name: osac.metering.inference
          patternType: literal
        operations:
          - Read
          - Describe
        host: "*"
      - resource:
          type: topic
          name: osac.metering.dlq.
          patternType: prefix
        operations:
          - Write
          - Describe
        host: "*"
      - resource:
          type: topic
          name: osac.metering.inference.dlq
          patternType: literal
        operations:
          - Write
          - Describe
        host: "*"
      - resource:
          type: group
          name: osac-metering-echo-adapter
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
# osac-kafka namespace under their natural names, so the metering pods'
# initContainers find them unchanged.
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
    # AAP admin password for the osac-aap subchart's external-instance mode
    # (bootstrap and create-token read the admin-password secret when
    # aap.aap.instance.enabled is false). Kept under its outer-cluster name.
    copy_secret_files "${AAP_NAMESPACE}" "${AAP_INSTANCE}-admin-password" "${AAP_INSTANCE}-admin-password" password

    # Keycloak client credentials for the fulfillment controller, referenced
    # by service.auth.controllerCredentials and service.idp.credentials.
    copy_secret_files "${OSAC_SHARED_NAMESPACE}" fulfillment-controller-credentials fulfillment-controller-credentials client-id client-secret

    # Backend API credentials for the UI, created by the osac-infra chart's
    # create-ui-backend-credentials hook in the outer cluster; the UI pod in
    # the vcluster mounts them and stays in CreateContainerConfigError
    # without them.
    copy_secret_files "${OSAC_SHARED_NAMESPACE}" osac-ui-backend-credentials osac-ui-backend-credentials client-id client-secret

    # Shared CA bundle, referenced by service.certs.caBundle.configMap.
    copy_ca_bundle
}

###############################################################################
# Output
###############################################################################

print_next_steps() {
    # The same settings `make install-osac` derives and passes for a normal
    # install (osac-installer/Makefile, install-osac target): DOMAIN from
    # oc get ingresses.config/cluster, then service.externalHostname,
    # service.internalHostname and ui.externalHostname. The vcluster has no
    # OpenShift ingress config for the Makefile to read, so CLUSTER_DOMAIN
    # (the Makefile override landing with the osac chart package) carries
    # the domain and the route hostnames are passed through EXTRA_HELM_ARGS,
    # with the OSAC namespace as the hostname prefix exactly as the Makefile
    # does.
    #
    # The AAP settings: the umbrella aliases the osac-aap dependency as
    # `aap`, so the subchart's top-level controllerUrl is aap.controllerUrl
    # (aap.aap.controllerUrl lands in a block nothing reads). In external
    # mode the chart reads the admin password from
    # aap.bootstrap.adminPasswordSecret.name, whose default is the fixed
    # osac-aap-admin-password; the run setup copies the secret under
    # <aap-instance>-admin-password, so the name must always be passed
    # explicitly.
    local extra_helm_args
    extra_helm_args="--set service.externalHostname=fulfillment-api-${VCLUSTER_OSAC_NAMESPACE}.${OUTER_DOMAIN}"
    extra_helm_args="${extra_helm_args} --set service.internalHostname=fulfillment-internal-api-${VCLUSTER_OSAC_NAMESPACE}.${OUTER_DOMAIN}"
    extra_helm_args="${extra_helm_args} --set ui.auth.oidcIssuerURL=https://keycloak-keycloak.${OUTER_DOMAIN}/realms/osac"
    extra_helm_args="${extra_helm_args} --set ui.externalHostname=osac-ui-${VCLUSTER_OSAC_NAMESPACE}.${OUTER_DOMAIN}"
    extra_helm_args="${extra_helm_args} --set aap.controllerUrl=${AAP_CONTROLLER_URL}"
    extra_helm_args="${extra_helm_args} --set aap.bootstrap.adminPasswordSecret.name=${AAP_INSTANCE}-admin-password"

    echo
    echo "Run setup complete."
    echo
    echo "vcluster kubeconfig: ${VCLUSTER_KUBECONFIG}"
    echo
    echo "Install OSAC into the vcluster by running this from the osac"
    echo "monorepo's osac-installer directory. AAP_LICENSE_FILE is required"
    echo "for PLATFORM=openshift; point it at the AAP license zip:"
    echo
    echo "  KUBECONFIG=${VCLUSTER_KUBECONFIG} \\"
    echo "  CLUSTER_DOMAIN=${OUTER_DOMAIN} \\"
    echo "  AAP_LICENSE_FILE=/path/to/license.zip \\"
    echo "  make install-osac PLATFORM=openshift PROFILE=vcluster-ci NS=osac \\"
    echo "      EXTRA_HELM_ARGS='${extra_helm_args}'"
    echo
    echo "PROFILE=vcluster-ci, the chart's vcluster support and the"
    echo "CLUSTER_DOMAIN Makefile override land with the osac chart work"
    echo "package; until then they do not exist."
}

###############################################################################
# Main
###############################################################################

main() {
    local positional_args=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --recreate)
                RECREATE="true"
                shift
                ;;
            --output-dir)
                [[ $# -ge 2 ]] || die "--output-dir needs a value."
                OUTPUT_DIR="$2"
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
        || die "Exactly one run name is required. Usage: $0 RUN_NAME [--recreate] [options]."
    RUN_NAME="${positional_args[0]}"
    [[ "${RUN_NAME}" =~ ^[a-z0-9]([-a-z0-9]{0,10}[a-z0-9])?$ ]] \
        || die "Run name '${RUN_NAME}' must be a DNS label of lowercase letters, digits and '-', at most 12 characters."

    HOST_NAMESPACE="vc-${RUN_NAME}"
    OUTPUT_DIR="${OUTPUT_DIR:-${REPO_ROOT}/.vcluster-runs/${RUN_NAME}}"
    VCLUSTER_KUBECONFIG="${OUTPUT_DIR}/kubeconfig"
    VALUES_FILE="${OUTPUT_DIR}/vcluster-values.yaml"

    check_all_preconditions
    mkdir -p "${OUTPUT_DIR}"
    render_values
    install_vcluster
    write_vcluster_kubeconfig
    create_vcluster_namespaces
    install_vcluster_cert_manager
    prepare_vcluster_certificate_authority
    create_kafka_users
    create_kafka_connection_secret
    copy_metering_kafka_secrets
    copy_outer_secrets_into_vcluster
    print_next_steps
}

main "$@"
