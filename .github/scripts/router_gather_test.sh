#!/usr/bin/env bash
# Tests the router hop evidence in scripts/gather-osac-logs.sh with a stubbed oc.
#
# A test that fails with RemoteDisconnected on an in-cluster Route leaves no
# record of the router hop. The gather must therefore write, next to the
# existing pods-describe.txt and events.txt:
#
#   namespaces/<ns>/routes.yaml          the Routes, annotations included
#   namespaces/<ns>/endpoints.yaml       whether a Service had a backend
#   namespaces/<ns>/endpointslices.yaml
#   namespaces/<ns>/routes.txt           route, host and HAProxy timeout
#   namespaces/<ns>/router-access.log    the router's own lines for those hosts
#
# and it must bound the access log to the last ROUTER_LOG_MAX_LINES lines, drop
# lines for other hosts, redact a credential in a query string, and say why it
# has no lines instead of silently writing nothing.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COLLECTOR="${SCRIPT_DIR}/../../scripts/gather-osac-logs.sh"
HOST="fulfillment-api-osac-e2e-ci.apps.canary.test"
OTHER_HOST="unrelated.apps.canary.test"
MAX_LINES=2000  # must match ROUTER_LOG_MAX_LINES in gather-osac-logs.sh
CAP=2600        # more matching lines than the cap, so the cap has to drop some

command -v jq >/dev/null || { echo "jq is required to run the collector"; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf -- "${WORK}"' EXIT
mkdir -p "${WORK}/bin" "${WORK}/fixtures"
: > "${WORK}/kubeconfig"

KEY_BEGIN="-----BEGIN"
KEY_END="PRIVATE KEY-----"
KEY_MARKER="${KEY_BEGIN} ${KEY_END}"
cat > "${WORK}/fixtures/routes.json" <<JSON
{"items": [
  {"metadata": {"name": "fulfillment-api", "namespace": "osac-e2e-ci",
                "annotations": {"haproxy.router.openshift.io/timeout": "30s",
                                "kubectl.kubernetes.io/last-applied-configuration":
                "{\"spec\":{\"tls\":{\"key\":\"${KEY_MARKER}SEKRIT${KEY_MARKER}\"}}}"}},
   "spec": {"host": "${HOST}", "tls": {"termination": "edge", "certificate": "CERT",
            "key": "${KEY_MARKER}SEKRIT${KEY_MARKER}"}}},
  {"metadata": {"name": "echo-adapter", "namespace": "osac-e2e-ci", "annotations": {}},
   "spec": {"host": "echo-adapter-osac-e2e-ci.apps.canary.test"}}
]}
JSON
cat > "${WORK}/fixtures/endpoints.yaml" <<'YAML'
apiVersion: v1
items:
- apiVersion: v1
  kind: Endpoints
  metadata:
    name: fulfillment-api
    namespace: osac-e2e-ci
  subsets:
  - addresses:
    - ip: 10.128.0.42
    ports:
    - name: grpc
      port: 8080
kind: List
YAML
cat > "${WORK}/fixtures/endpointslices.yaml" <<'YAML'
apiVersion: v1
items:
- apiVersion: discovery.k8s.io/v1
  kind: EndpointSlice
  metadata:
    name: fulfillment-api-abc12
    namespace: osac-e2e-ci
  endpoints:
  - addresses: ["10.128.0.42"]
    conditions:
      ready: true
kind: List
YAML

# Matching lines for the observed host on both router pods, an unrelated host,
# a query-string credential and a JWT, so the filter, the per-pod cap and the
# redaction each have something to prove.
{
  for i in $(seq 1 "${CAP}"); do
    printf '2026-11-03T10:00:00.000000Z router-default-aaa %s GET /api/v1/x HTTP/1.1 200 req-aaa-%05d\n' "${HOST}" "${i}"
  done
  printf '2026-11-03T10:00:01.000000Z router-default-aaa %s GET /other HTTP/1.1 200 not-ours\n' "${OTHER_HOST}"
  printf '2026-11-03T10:00:02.000000Z router-default-aaa %s GET /api/v1/x?token=SEKRIT-QUERY HTTP/1.1 502 req-aaa-token\n' "${HOST}"
  printf '2026-11-03T10:00:03.000000Z router-default-aaa %s GET /api/v1/x HTTP/1.1 502 req-aaa-jwt auth=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl\n' "${HOST}"
} > "${WORK}/fixtures/router-aaa.log"
{
  for i in $(seq 1 "${CAP}"); do
    printf '2026-11-03T10:00:00.500000Z router-default-bbb %s GET /api/v1/y HTTP/1.1 503 req-bbb-%05d\n' "${HOST}" "${i}"
  done
  printf '2026-11-03T10:00:01.500000Z router-default-bbb %s GET /api/v1/y HTTP/1.1 503 req-bbb-extra\n' "${HOST}"
  printf '2026-11-03T10:00:01.600000Z router-default-bbb %s GET /api/v1/z?id_token=SEKRIT-ID HTTP/1.1 200 req-bbb-id\n' "${HOST}"
} > "${WORK}/fixtures/router-bbb.log"

cat > "${WORK}/bin/oc" <<'STUB'
#!/usr/bin/env bash
echo "oc $*" >> "${STUB_LOG}"
case "$*" in
  *"get route -n osac-e2e-ci -o json"*) cat "${FIXTURES}/routes.json" ;;
  *"get endpoints -n osac-e2e-ci -o yaml"*) cat "${FIXTURES}/endpoints.yaml" ;;
  *"get endpointslices -n osac-e2e-ci -o yaml"*) cat "${FIXTURES}/endpointslices.yaml" ;;
  *"get ingresscontroller default"*)
    [[ -n "${STUB_NO_ACCESS_LOGGING:-}" ]] || printf 'Container' ;;
  *"get pods -n openshift-ingress -l"*)
    [[ -n "${STUB_NO_ROUTER_PODS:-}" ]] || printf 'router-default-aaa router-default-bbb' ;;
  *"logs router-default-aaa"*) cat "${FIXTURES}/router-aaa.log" ;;
  *"logs router-default-bbb"*) cat "${FIXTURES}/router-bbb.log" ;;
esac
exit 0
STUB
chmod +x "${WORK}/bin/oc"

run_collector() {
  local out="$1"
  local log="$2"
  shift 2
  if ! env -i \
    PATH="${WORK}/bin:/usr/local/bin:/usr/bin:/bin" \
    HOME="${WORK}" \
    KUBECONFIG="${WORK}/kubeconfig" \
    FIXTURES="${WORK}/fixtures" \
    STUB_LOG="${WORK}/oc.log" \
    OSAC_E2E_OUTPUT_DIR="${WORK}/no-e2e-output" \
    "$@" \
    bash "${COLLECTOR}" "${out}" > "${log}" 2>&1; then
    echo "FAIL: the collector failed"
    tail -n 30 "${log}"
    exit 1
  fi
}

NS_DIR="mgmt-cluster/namespaces/osac-e2e-ci"

echo "collecting with router pods"
run_collector "${WORK}/collected" "${WORK}/collected.log"
ART="${WORK}/collected"

for f in routes.yaml endpoints.yaml endpointslices.yaml routes.txt router-access.log; do
  [[ -f "${ART}/${NS_DIR}/${f}" ]] || { echo "FAIL: ${NS_DIR}/${f} was not collected"; exit 1; }
done

grep -q "${HOST}" "${ART}/${NS_DIR}/routes.yaml" \
  || { echo "FAIL: routes.yaml does not hold the observed route"; exit 1; }
grep -q '"haproxy.router.openshift.io/timeout": "30s"' "${ART}/${NS_DIR}/routes.yaml" \
  || { echo "FAIL: routes.yaml does not hold the timeout annotation"; exit 1; }
grep -q 'PRIVATE KEY' "${ART}/${NS_DIR}/routes.yaml" \
  && { echo "FAIL: routes.yaml leaked a TLS private key"; exit 1; }
grep -q '"key":' "${ART}/${NS_DIR}/routes.yaml" \
  && { echo "FAIL: routes.yaml kept spec.tls.key"; exit 1; }
grep -q 'last-applied-configuration' "${ART}/${NS_DIR}/routes.yaml" \
  && { echo "FAIL: routes.yaml kept the last-applied annotation"; exit 1; }
grep -q '10.128.0.42' "${ART}/${NS_DIR}/endpoints.yaml" \
  || { echo "FAIL: endpoints.yaml does not hold the ready address"; exit 1; }
grep -q 'fulfillment-api-abc12' "${ART}/${NS_DIR}/endpointslices.yaml" \
  || { echo "FAIL: endpointslices.yaml does not hold the slice"; exit 1; }
grep -q $'^fulfillment-api\t.*\t30s$' "${ART}/${NS_DIR}/routes.txt" \
  || { echo "FAIL: routes.txt does not hold the route timeout"; exit 1; }
grep -q $'^echo-adapter\t.*\tnone$' "${ART}/${NS_DIR}/routes.txt" \
  || { echo "FAIL: routes.txt does not mark a missing timeout"; exit 1; }

LOG="${ART}/${NS_DIR}/router-access.log"
grep -q 'accessLogging=Container' "${LOG}" \
  || { echo "FAIL: the access-logging setting is not recorded"; exit 1; }
grep -q 'pods: router-default-aaa router-default-bbb' "${LOG}" \
  || { echo "FAIL: the router pods read are not recorded"; exit 1; }
grep -q 'not-ours' "${LOG}" \
  && { echo "FAIL: a line for another host was kept"; exit 1; }
grep -q "${OTHER_HOST}" "${LOG}" \
  && { echo "FAIL: a line for another host was kept"; exit 1; }
grep -q 'req-bbb' "${LOG}" \
  || { echo "FAIL: the second router pod's lines were dropped"; exit 1; }
grep -q 'req-aaa-token' "${LOG}" \
  || { echo "FAIL: the 502 line was dropped"; exit 1; }
grep -q 'token=REDACTED' "${LOG}" \
  || { echo "FAIL: the query-string credential was not redacted"; exit 1; }
grep -q 'SEKRIT-QUERY' "${LOG}" \
  && { echo "FAIL: the query-string credential leaked"; exit 1; }
grep -q 'id_token=REDACTED' "${LOG}" \
  || { echo "FAIL: id_token was not redacted"; exit 1; }
grep -q 'SEKRIT-ID' "${LOG}" \
  && { echo "FAIL: id_token leaked"; exit 1; }
grep -q 'REDACTED_JWT' "${LOG}" \
  || { echo "FAIL: the JWT was not redacted"; exit 1; }

# The cap is per pod and keeps the last lines, not the first: the newest
# request from each router pod survives, the oldest does not, and neither pod
# crowds the other out.
aaa=$(grep -c 'req-aaa-' "${LOG}" || true)
bbb=$(grep -c 'req-bbb-' "${LOG}" || true)
[[ "${aaa}" -eq "${MAX_LINES}" ]] \
  || { echo "FAIL: the first router pod kept ${aaa} lines, expected ${MAX_LINES}"; exit 1; }
[[ "${bbb}" -eq "${MAX_LINES}" ]] \
  || { echo "FAIL: the second router pod kept ${bbb} lines, expected ${MAX_LINES}"; exit 1; }
[[ $((aaa + bbb)) -lt $((2 * CAP)) ]] \
  || { echo "FAIL: the access log is not capped: $((aaa + bbb)) lines"; exit 1; }
grep -q 'req-aaa-02600$' "${LOG}" \
  || { echo "FAIL: the newest line of the first router pod was dropped"; exit 1; }
grep -q 'req-bbb-02600$' "${LOG}" \
  || { echo "FAIL: the newest line of the second router pod was dropped"; exit 1; }
grep -q 'req-aaa-00001$' "${LOG}" \
  && { echo "FAIL: the cap kept the oldest lines instead of the newest"; exit 1; }
grep -q 'req-bbb-00001$' "${LOG}" \
  && { echo "FAIL: the cap kept the oldest lines instead of the newest"; exit 1; }

grep -q 'logs router-default-aaa -n openshift-ingress' "${WORK}/oc.log" \
  || { echo "FAIL: the first router pod's logs were never read"; exit 1; }
grep -q 'logs router-default-bbb -n openshift-ingress' "${WORK}/oc.log" \
  || { echo "FAIL: the second router pod's logs were never read"; exit 1; }
grep -q 'get ingresscontroller default -n openshift-ingress-operator' "${WORK}/oc.log" \
  || { echo "FAIL: the access-logging setting was never read"; exit 1; }

echo "collecting with no router pods and no access logging"
run_collector "${WORK}/no-pods" "${WORK}/no-pods.log" \
    STUB_NO_ROUTER_PODS=1 STUB_NO_ACCESS_LOGGING=1
NO_PODS_LOG="${WORK}/no-pods/${NS_DIR}/router-access.log"
[[ -f "${NO_PODS_LOG}" ]] || { echo "FAIL: router-access.log missing without router pods"; exit 1; }
grep -q 'pods: none' "${NO_PODS_LOG}" \
  || { echo "FAIL: the absence of router pods is not recorded"; exit 1; }
grep -q 'accessLogging=disabled' "${NO_PODS_LOG}" \
  || { echo "FAIL: a missing access-logging setting is not recorded as disabled"; exit 1; }

echo "router_gather_test.sh: ok"
