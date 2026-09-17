#!/usr/bin/env bash
# Assert the brownfield install used the standalone AAP instead of deploying
# one in the OSAC namespace.
#
# Required env:
#   KUBECONFIG
#   OSAC_NAMESPACE
#   EXTERNAL_AAP_URL
# Optional env:
#   EXTERNAL_AAP_NAMESPACE   default aap-acm
#   EXTERNAL_AAP_NAME        default external-aap
#   EXTERNAL_AAP_TOKEN_SECRET default my-aap-token
set -euo pipefail

: "${KUBECONFIG:?KUBECONFIG is required}"
: "${OSAC_NAMESPACE:?OSAC_NAMESPACE is required}"
: "${EXTERNAL_AAP_URL:?EXTERNAL_AAP_URL is required}"

EXTERNAL_AAP_NAMESPACE="${EXTERNAL_AAP_NAMESPACE:-aap-acm}"
EXTERNAL_AAP_NAME="${EXTERNAL_AAP_NAME:-external-aap}"
EXTERNAL_AAP_TOKEN_SECRET="${EXTERNAL_AAP_TOKEN_SECRET:-my-aap-token}"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

log() {
  echo "$*"
}

status=$(helm status osac -n "${OSAC_NAMESPACE}" -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["status"])')
[[ "${status}" == "deployed" ]] || fail "helm release osac status is '${status}', expected deployed"
log "helm release osac is deployed"

osac_aap_count=$(oc get ansibleautomationplatform -n "${OSAC_NAMESPACE}" -o json 2>/dev/null |
  python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("items") or []))' || echo 0)
[[ "${osac_aap_count}" == "0" ]] || fail "expected 0 AnsibleAutomationPlatform in ${OSAC_NAMESPACE}, found ${osac_aap_count}"
log "no AnsibleAutomationPlatform CR in ${OSAC_NAMESPACE}"

oc get ansibleautomationplatform "${EXTERNAL_AAP_NAME}" -n "${EXTERNAL_AAP_NAMESPACE}" >/dev/null \
  || fail "missing AnsibleAutomationPlatform/${EXTERNAL_AAP_NAME} in ${EXTERNAL_AAP_NAMESPACE}"
log "standalone AAP ${EXTERNAL_AAP_NAME} is in ${EXTERNAL_AAP_NAMESPACE}"

deploys=$(mktemp)
trap 'rm -f "${deploys}"' EXIT
oc get deploy -n "${OSAC_NAMESPACE}" -o json >"${deploys}"
python3 - "${deploys}" "${EXTERNAL_AAP_URL}" "${EXTERNAL_AAP_TOKEN_SECRET}" <<'PY'
import json, sys

with open(sys.argv[1], encoding="utf-8") as fh:
    doc = json.load(fh)
expected_url = sys.argv[2]
expected_secret = sys.argv[3]
matches = []
for deploy in doc.get("items") or []:
    name = deploy["metadata"]["name"]
    for container in deploy["spec"]["template"]["spec"].get("containers") or []:
        url = None
        secret = None
        for env in container.get("env") or []:
            if env.get("name") == "OSAC_AAP_URL":
                url = env.get("value")
            if env.get("name") == "OSAC_AAP_TOKEN":
                secret = (env.get("valueFrom") or {}).get("secretKeyRef", {}).get("name")
        if url or secret:
            matches.append((name, url, secret))

if not matches:
    raise SystemExit("no Deployment in the OSAC namespace has OSAC_AAP_URL")

bad = []
for name, url, secret in matches:
    if url != expected_url:
        bad.append(f"{name} OSAC_AAP_URL={url!r} expected {expected_url!r}")
    if secret != expected_secret:
        bad.append(f"{name} OSAC_AAP_TOKEN secret={secret!r} expected {expected_secret!r}")
if bad:
    raise SystemExit("; ".join(bad))
print(f"operator AAP wiring ok ({len(matches)} deployment(s))")
PY

log "external AAP installer path verified"
