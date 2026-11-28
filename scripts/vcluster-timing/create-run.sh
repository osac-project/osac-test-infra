#!/bin/bash
# Create a vcluster run and complete the setup (working around the
# kubeconfig service-DNS gap with a port-forward).
# Usage: create-run.sh RUN_NAME [port]
set -euo pipefail

export KUBECONFIG="$HOME/.kube/poc.kubeconfig"

RUN_ARG="${1:?Usage: create-run.sh RUN_NAME [port]}"
PORT="${2:-9443}"
DOMAIN="apps.test-infra-cluster-poc.redhat.com"
TI=/tmp/ti-impl
OSAC=/tmp/osac-impl

# Step 1: Run the setup script (fails at kubeconfig verification, expected)
cd "${TI}"
bash scripts/vcluster-run-setup.sh "${RUN_ARG}" \
    --recreate \
    --realm-json "${OSAC}/osac-installer/charts/osac-infra/files/realm.json" \
    --keycloak-url "https://keycloak-keycloak.${DOMAIN}" \
    2>&1 | tail -5 || true

# Step 2: Port-forward and patch the kubeconfig
oc --namespace "vc-${RUN_ARG}" port-forward "svc/${RUN_ARG}" "${PORT}:443" \
    > "/tmp/opencode/poc/${RUN_ARG}-pf.log" 2>&1 &
PF_PID=$!

KC="${TI}/.vcluster-runs/${RUN_ARG}/kubeconfig-local"
cp "${TI}/.vcluster-runs/${RUN_ARG}/kubeconfig" "${KC}"
python3 - "${KC}" "${PORT}" <<'EOF'
import sys, yaml
path, port = sys.argv[1], sys.argv[2]
kc = yaml.safe_load(open(path))
for c in kc['clusters']:
    c['cluster']['server'] = f'https://127.0.0.1:{port}'
    c['cluster'].pop('certificate-authority-data', None)
    c['cluster']['insecure-skip-tls-verify'] = True
yaml.safe_dump(kc, open(path, 'w'))
EOF

# Wait for the port-forward to be ready
for i in $(seq 1 30); do
    if kubectl --kubeconfig "${KC}" get namespace default >/dev/null 2>&1; then
        break
    fi
    sleep 2
done
kubectl --kubeconfig "${KC}" get namespace default >/dev/null

# Step 3: Run the remaining setup functions
# Source the library, then set our variables (the library resets them on source)
sed 's/^main "\$@"$//' "${TI}/scripts/vcluster-run-setup.sh" > /tmp/opencode/poc/run-lib.sh
# shellcheck source=/dev/null
source /tmp/opencode/poc/run-lib.sh

# Override the library's EXIT trap (it would delete our WORK_DIR)
trap 'kill ${PF_PID} 2>/dev/null || true' EXIT

# Set all variables AFTER sourcing (the library resets them)
RUN_NAME="${RUN_ARG}"
HOST_NAMESPACE="vc-${RUN_ARG}"
OUTPUT_DIR="${TI}/.vcluster-runs/${RUN_ARG}"
VCLUSTER_KUBECONFIG="${OUTPUT_DIR}/kubeconfig-local"
VALUES_FILE="${OUTPUT_DIR}/vcluster-values.yaml"
INSTANCE_VALUES_FILE="${OUTPUT_DIR}/instance-values.yaml"
WORK_DIR="${OUTPUT_DIR}"
AAP_INSTANCE="${AAP_INSTANCE_DEFAULT}"
AAP_NAMESPACE="${AAP_NAMESPACE_DEFAULT}"
AAP_CONTROLLER_URL="https://osac-aap-osac.${DOMAIN}"
KEYCLOAK_URL="https://keycloak-keycloak.${DOMAIN}"
REALM_JSON_SOURCE="${OSAC}/osac-installer/charts/osac-infra/files/realm.json"
OUTER_DOMAIN="${DOMAIN}"

read_keycloak_admin_credentials
fetch_keycloak_ca
resolve_aap_controller_url
suite_from_run_name

KAFKA_USER_NAME="${RUN_NAME}-fulfillment-service"
KAFKA_CONNECTION_SECRET="${RUN_NAME}-fulfillment-service-kafka"
METERING_KAFKA_USERS=("${RUN_NAME}-osac-metering" "${RUN_NAME}-osac-metering-echo-adapter")
EVENT_TOPIC_PREFIX="${RUN_NAME}.events."
SYSTEM_GROUP_PREFIX="${RUN_NAME}.system."
HEARTBEAT_TOPIC_PREFIX="${RUN_NAME}.metering.heartbeat."
DLQ_TOPIC_PREFIX="${RUN_NAME}.metering.dlq."
INFERENCE_TOPIC="${RUN_NAME}.metering.inference"
INFERENCE_DLQ_TOPIC="${RUN_NAME}.metering.inference.dlq"

create_vcluster_namespaces

# The four lanes below are mutually independent (vcluster is up, its
# namespaces exist, the CA is fetched): cert-manager deploys inside the
# vcluster, the Kafka lane talks to the outer Strimzi, the Keycloak lane
# to the outer realm, the AAP lane to the shared AAP. Run them in
# parallel (subshells inherit the sourced functions and variables) and
# join before writing the overlay.
lane_cert_manager() {
    install_vcluster_cert_manager
    prepare_vcluster_certificate_authority
}
lane_kafka() {
    create_kafka_users
    create_kafka_connection_secret
    copy_metering_kafka_secrets
}
lane_keycloak() {
    render_realm_json
    import_keycloak_realm
    wait_for_keycloak_realm
    set_fixture_user_passwords
    grant_controller_realm_management_roles
    ensure_service_account_registration_policies
    ensure_service_account_registration_token_policy
    write_keycloak_credentials_into_vcluster
}
lane_aap() {
    create_aap_service_account
    copy_outer_secrets_into_vcluster
}

SETUP_PIDS=()
for lane in lane_cert_manager lane_kafka lane_keycloak lane_aap; do
    ( "${lane}" ) >"${WORK_DIR}/lane-${lane}.log" 2>&1 &
    SETUP_PIDS+=($!)
done

LANE_FAILED=0
for pid in "${SETUP_PIDS[@]}"; do
    if ! wait "${pid}"; then
        LANE_FAILED=1
    fi
done
if [[ "${LANE_FAILED}" != 0 ]]; then
    echo "A setup lane failed; per-lane logs:" >&2
    for f in "${WORK_DIR}"/lane-*.log; do
        echo "--- ${f} (tail)" >&2
        tail -5 "${f}" >&2
    done
    exit 1
fi

write_instance_values

echo ""
echo "=== Run ${RUN_ARG} setup complete"
echo "=== Kubeconfig: ${VCLUSTER_KUBECONFIG}"
