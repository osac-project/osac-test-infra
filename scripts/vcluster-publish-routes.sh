#!/usr/bin/env bash
# vcluster-publish-routes.sh -- publish a run's routes in the outer cluster
# (design step 6, run after 'make install-osac' into the vcluster).
#
# The chart renders no routes for the vcluster variant: the services it
# creates sync into the host namespace under their own names, so passthrough
# routes there publish them under the run's hostnames. The certificates the
# chart issues cover exactly these names (the install's externalHostname and
# internalHostname values), so passthrough terminates nothing and the envoy
# frontend presents the CI-CA certificate. Namespace deletion removes the
# routes with everything else, so the teardown needs no route step.
#
# Usage:
#   KUBECONFIG=/path/to/outer-kubeconfig ./scripts/vcluster-publish-routes.sh RUN_NAME
#
# The run name must be the same one vcluster-run-setup.sh used: it names the
# host namespace (vc-<run name>) and the route hostnames.
#
# See docs/vcluster-run-setup.md for the end-to-end flow.
set -euo pipefail

usage() {
    echo "Usage: $0 RUN_NAME" >&2
    echo "Publishes the run's fulfillment and UI routes in the outer cluster." >&2
    exit 2
}

[[ $# -eq 1 ]] || usage
RUN_NAME="$1"
[[ "${RUN_NAME}" =~ ^[a-z0-9]([-a-z0-9]{0,10}[a-z0-9])?$ ]] || {
    echo "ERROR: run name '${RUN_NAME}' is not a lowercase DNS label of at most 12 characters" >&2
    exit 1
}

: "${KUBECONFIG:?ERROR: KUBECONFIG (the outer cluster) is required}"

HOST_NAMESPACE="vc-${RUN_NAME}"

OUTER_DOMAIN="$(kubectl get ingresses.config/cluster -o jsonpath='{.spec.domain}')"
[[ -n "${OUTER_DOMAIN}" ]] || { echo "ERROR: could not read the outer cluster's domain" >&2; exit 1; }

# Fail loudly when a service is missing: a route to a nonexistent service
# publishes a hostname that never answers, which is worse than no route.
for service_port in fulfillment-api:external-api fulfillment-internal-api:internal-api osac-ui:http; do
    service="${service_port%%:*}"
    kubectl --namespace "${HOST_NAMESPACE}" get service "${service}" >/dev/null
done

kubectl --namespace "${HOST_NAMESPACE}" create route passthrough fulfillment-api \
    --port=external-api --service=fulfillment-api \
    --hostname="fulfillment-api-${RUN_NAME}.${OUTER_DOMAIN}" --insecure-policy=None
kubectl --namespace "${HOST_NAMESPACE}" create route passthrough fulfillment-internal-api \
    --port=internal-api --service=fulfillment-internal-api \
    --hostname="fulfillment-internal-api-${RUN_NAME}.${OUTER_DOMAIN}" --insecure-policy=None
kubectl --namespace "${HOST_NAMESPACE}" create route edge osac-ui \
    --port=http --service=osac-ui \
    --hostname="osac-ui-${RUN_NAME}.${OUTER_DOMAIN}" --insecure-policy=Redirect

echo "Published the run's routes in namespace ${HOST_NAMESPACE}:"
kubectl --namespace "${HOST_NAMESPACE}" get routes
