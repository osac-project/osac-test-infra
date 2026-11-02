#!/usr/bin/env bash
# fabric-snapshot.sh - read-only snapshot of the Netris fabric state.
#
# The OSAC E2E tests run this (OSAC_E2E_FABRIC_SNAPSHOT_CMD) at each failure with
# a 30 second limit and store stdout as fabric.json. stdout is one JSON object,
# stderr carries errors, and the exit status is 1 when no snapshot could be taken.
#
#   {"vpc": ..., "vnet": ..., "subnets": ..., "nat": ...}
#
# A section is null when its endpoint failed. Only GET requests are made after the
# login. The controller URL is found as gather-infra does (the k3s traefik "web"
# nodePort on localhost); the credentials come from the Secret the installer
# writes, network-fulfillment-ig, so no new credential is stored. The password
# travels on stdin and in the environment of one child process, never in argv or
# stdout, and the cookie jar is a 0600 file in a temporary directory that a trap
# removes.
#
# Env: KUBECONFIG (hub, for the Secret), OSAC_NAMESPACE (default osac-e2e-ci),
#      NETRIS_K3S_KUBECONFIG (default /etc/rancher/k3s/k3s.yaml, readable by the
#      user that runs the tests), NETRIS_CREDENTIALS_SECRET.
set -uo pipefail

# The tests give this command 30 seconds and then kill it, which would skip the
# cleanup trap. Stop earlier (TERM, so the trap runs) and cap every call by what
# is left of that budget.
DEADLINE="${FABRIC_SNAPSHOT_DEADLINE:-27}"
if [[ -z "${FABRIC_SNAPSHOT_INNER:-}" ]]; then
    FABRIC_SNAPSHOT_INNER=1 exec timeout "${DEADLINE}" bash "$0" "$@"
fi

K3S_KUBECONFIG="${NETRIS_K3S_KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
NAMESPACE="${OSAC_NAMESPACE:-osac-e2e-ci}"
SECRET="${NETRIS_CREDENTIALS_SECRET:-network-fulfillment-ig}"
MAX_TIME=8

# Seconds a call may take: the given cap, or what is left of the deadline.
budget() {
    local left=$((DEADLINE - SECONDS))
    ((left >= 1)) || fail "deadline exceeded"
    echo $((left < $1 ? left : $1))
}

fail() {
    echo "fabric-snapshot: $*" >&2
    exit 1
}

if command -v kubectl >/dev/null 2>&1; then
    KUBE=kubectl
elif command -v oc >/dev/null 2>&1; then
    KUBE=oc
else
    fail "neither kubectl nor oc is available"
fi

[[ -r "${K3S_KUBECONFIG}" ]] || fail "${K3S_KUBECONFIG} is not readable by $(id -un)"

WORK="$(mktemp -d)" || fail "cannot create a temporary directory"
trap 'rm -rf -- "${WORK}"' EXIT
chmod 700 "${WORK}"
(umask 077 && : > "${WORK}/jar")

node_port="$(KUBECONFIG="${K3S_KUBECONFIG}" "${KUBE}" --request-timeout="$(budget 5)s" get svc traefik -n kube-system \
    -o jsonpath='{.spec.ports[?(@.name=="web")].nodePort}' 2>/dev/null)"
[[ "${node_port}" =~ ^[0-9]+$ ]] || fail "cannot find the traefik web nodePort"
URL="http://localhost:${node_port}"

# Secret data is base64 in the API; ConfigMap data is plain text.
secret_value() {
    "${KUBE}" --request-timeout="$(budget 5)s" get secret "${SECRET}" -n "${NAMESPACE}" \
        -o "jsonpath={.data.$1}" 2>/dev/null | base64 -d 2>/dev/null
}

configmap_value() {
    "${KUBE}" --request-timeout="$(budget 5)s" get configmap "${SECRET}" -n "${NAMESPACE}" \
        -o "jsonpath={.data.$1}" 2>/dev/null
}

password="$(secret_value NETRIS_PASSWORD)"
[[ -n "${password}" ]] || fail "no NETRIS_PASSWORD in Secret ${NAMESPACE}/${SECRET}"
user="$(configmap_value NETRIS_USERNAME)"
user="${user:-netris}"

# The login body goes to curl on stdin; the password is in the environment of the
# python process only.
if ! NETRIS_USER="${user}" NETRIS_PW="${password}" python3 -c '
import json, os
print(json.dumps({"user": os.environ["NETRIS_USER"], "password": os.environ["NETRIS_PW"], "auth_scheme_id": 1}))
' | curl --fail --silent --max-time "$(budget "${MAX_TIME}")" -c "${WORK}/jar" "${URL}/api/auth" \
        -H 'Content-Type: application/json' --data @- -o /dev/null 2>/dev/null; then
    unset password
    fail "login to the Netris controller failed"
fi
unset password

declare -A ENDPOINTS=([vpc]=/api/v2/vpc [vnet]=/api/v2/vnet [subnets]=/api/v2/ipam/subnets [nat]=/api/v2/nat)
for key in "${!ENDPOINTS[@]}"; do
    (
        curl --fail --silent --max-time "$(budget "${MAX_TIME}")" -b "${WORK}/jar" "${URL}${ENDPOINTS[${key}]}" \
            > "${WORK}/${key}.json" 2>/dev/null || { echo "fabric-snapshot: GET ${ENDPOINTS[${key}]} failed" >&2; rm -f "${WORK}/${key}.json"; }
    ) &
done
wait

python3 - "${WORK}" <<'PY'
import json, os, sys

work = sys.argv[1]
out = {}
for key in ("vpc", "vnet", "subnets", "nat"):
    try:
        with open(os.path.join(work, key + ".json"), encoding="utf-8") as handle:
            out[key] = json.load(handle)
    except (OSError, ValueError):
        out[key] = None
json.dump(out, sys.stdout, sort_keys=True)
print()
if all(value is None for value in out.values()):
    print("fabric-snapshot: no section could be read", file=sys.stderr)
    sys.exit(1)
PY
