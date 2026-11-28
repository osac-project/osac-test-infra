#!/bin/bash
# run-suite.sh RUN_NAME [SUITE_DIR]
#
# Runs an e2e suite against one vcluster run with all the vcluster-specific
# environment wired. The suite must run against the checkout that matches
# the run's installed code (/tmp/osac-impl).
set -euo pipefail

RUN_ARG="${1:?Usage: run-suite.sh RUN_NAME [SUITE_DIR]}"
SUITE="${2:-vmaas/sanity}"
DOMAIN="apps.test-infra-cluster-poc.redhat.com"
TI=/tmp/ti-impl
OSAC=/tmp/osac-impl
OUT=/tmp/opencode/poc/parallel-e2e
KC="${TI}/.vcluster-runs/${RUN_ARG}/kubeconfig-local"
mkdir -p "${OUT}"

# Ensure the port-forward to the vcluster API is alive; earlier detached
# loops die with their parent shells. Idempotent: does nothing when the
# port answers. The port comes from the kubeconfig's server URL.
if ! kubectl --kubeconfig "${KC}" get ns default >/dev/null 2>&1; then
    PORT="$(python3 -c "
import yaml
kc = yaml.safe_load(open('${KC}'))
print(kc['clusters'][0]['cluster']['server'].rsplit(':', 1)[1])")"
    KUBECONFIG="$HOME/.kube/poc.kubeconfig" oc --namespace "vc-${RUN_ARG}" \
        port-forward "svc/${RUN_ARG}" "${PORT}:443" >/dev/null 2>&1 &
    for _ in $(seq 1 30); do
        kubectl --kubeconfig "${KC}" get ns default >/dev/null 2>&1 && break
        sleep 2
    done
fi

# The venv is shared between concurrent suite runs: sync once here, then
# every run uses --no-sync so they cannot race on .venv creation.
cd "${OSAC}"
uv sync >/dev/null 2>&1

# The master-realm admin password comes from the outer cluster's secret:
# the suite's organization-membership fixture logs into the master realm
# with it. OSAC_JWT_PASSWORD is the realm users' password (foobar), which
# is a different thing. Read it BEFORE switching KUBECONFIG to the run.
KC_ADMIN_PASS="$(KUBECONFIG="$HOME/.kube/poc.kubeconfig" oc get secret -n keycloak keycloak-admin-credentials -o jsonpath='{.data.admin-password}' 2>/dev/null | base64 -d)"
[[ -n "${KC_ADMIN_PASS}" ]] || KC_ADMIN_PASS="admin"

# Python-based probes (metering URL checks) verify TLS: give them the lab
# CA (fetched from the outer cluster's cert-manager default-ca). The osac
# CLI must be the branch build, not the released binary: the released one
# predates fields the branch's server returns. Read the CA BEFORE the
# KUBECONFIG switch: it lives on the outer cluster.
CA_FILE="${TI}/.vcluster-runs/${RUN_ARG}/default-ca.ca.crt"
KUBECONFIG="$HOME/.kube/poc.kubeconfig" oc get secret -n cert-manager default-ca \
    -o jsonpath='{.data.ca\.crt}' 2>/dev/null | base64 -d >"${CA_FILE}"
[[ -s "${CA_FILE}" ]] || echo "WARNING: could not fetch the lab CA; TLS probes may fail"

export KUBECONFIG="${KC}"
export OSAC_HUB_KUBECONFIG="${KC}"
export OSAC_VM_KUBECONFIG="$HOME/.kube/poc.kubeconfig"
export OSAC_NAMESPACE=osac
export OSAC_CLUSTER_DOMAIN="${DOMAIN}"
export OSAC_FULFILLMENT_ADDRESS="fulfillment-api-${RUN_ARG}.${DOMAIN}:443"
export OSAC_FULFILLMENT_PRIVATE_ADDRESS="fulfillment-internal-api-${RUN_ARG}.${DOMAIN}:443"
export OSAC_KEYCLOAK_URL="https://keycloak-keycloak.${DOMAIN}"
export OSAC_KEYCLOAK_REALM="${RUN_ARG}"
export OSAC_KEYCLOAK_CLIENT_ID=osac-cli
export OSAC_KEYCLOAK_ADMIN_PASSWORD="${KC_ADMIN_PASS}"
export OSAC_JWT_PASSWORD=foobar
export OSAC_IMPERSONATE_USER=
export METERING_ADAPTER_URL="https://osac-metering-echo-adapter-${RUN_ARG}.${DOMAIN}"
export SSL_CERT_FILE="${CA_FILE}"
export OSAC_CLI_PATH="/tmp/osac-impl/.bin/osac"

# The E2E workflows apply the install-namespace and shared Tenant CRs
# between install and test (see the cluster_tenants fixture note in
# tests/e2e/conftest.py). Without this, four xdist workers race the same
# two creates and the losers fail their whole session. Apply them once,
# serially, before pytest starts.
for TNAME in osac shared; do
    printf 'apiVersion: osac.openshift.io/v1alpha1\nkind: Tenant\nmetadata:\n  name: %s\n  namespace: osac\nspec: {}\n' \
        "${TNAME}" | oc --kubeconfig "${KC}" apply -f - >/dev/null
done

exec uv run --no-sync pytest "tests/e2e/${SUITE}/" \
    --junitxml="${OUT}/${RUN_ARG}-junit.xml" \
    -q --tb=line
