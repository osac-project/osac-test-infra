#!/bin/bash
# timed-run.sh RUN_NAME [PORT]
#
# Full end-to-end pipeline: create a vcluster run, install OSAC into it,
# bootstrap a tenant, and run a VM on the outer cluster. Times every phase
# and reports two headline numbers:
#   - INSTALL: nothing -> OSAC installation ready for a test suite
#   - VM:      installation ready -> VMI Running on the outer cluster
#
# Prerequisites (already true on the POC node):
#   - images loaded on the node (localhost/*:vc-e2e-*)
#   - outer cluster reachable via ~/.kube/poc.kubeconfig
#   - /tmp/osac-impl and /tmp/ti-impl checkouts on branch vcluster-e2e
set -euo pipefail

export KUBECONFIG="$HOME/.kube/poc.kubeconfig"

RUN_ARG="${1:?Usage: timed-run.sh RUN_NAME [PORT]}"
TENANT="${TENANT:-tenant1}"
TENANT_USER="${TENANT_USER:-tenant1_user}"
PORT="${2:-9445}"
DOMAIN="apps.test-infra-cluster-poc.redhat.com"
TI=/tmp/ti-impl
OSAC=/tmp/osac-impl
KC="${TI}/.vcluster-runs/${RUN_ARG}/kubeconfig-local"
OUTER_KC="$HOME/.kube/poc.kubeconfig"
RUN_DIR="/tmp/opencode/poc/${RUN_ARG}-timed"
mkdir -p "${RUN_DIR}"

# Image tags: fulfillment-service and osac-operator carry the realm and
# VM-namespace fixes; the rest are unchanged since the first build.
TAG_OLD="vc-e2e-f9b4fcc31"
TAG_FULFILLMENT="vc-e2e-2e35492ea"
TAG_OPERATOR="vc-e2e-c9cb7a2b5"
# The BMF operator image: the profile inherits ghcr.io/...:latest which
# predates the remote-cluster mode and crashloops on the BareMetalHost
# CRD check. b-suite runs need the branch build.
TAG_BMF="vc-e2e-86b327e8d"
SHA="f9b4fcc314ffd2bac02b485fa471984cbfb8ae8f"

T_START=$(date +%s)
T_INSTALL_READY=""
declare -a PHASE_NAMES PHASE_SECS

phase() {
    # phase <name> <start_epoch>
    local name="$1" start="$2"
    local now; now=$(date +%s)
    PHASE_NAMES+=("$name")
    PHASE_SECS+=("$((now - start))")
    printf '>>> %-28s %5ds (elapsed %ds)\n' "$name" "$((now - start))" "$((now - T_START))"
}

die() { echo "FATAL: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
echo "=== Timed run ${RUN_ARG} starting at $(date -u +%H:%M:%S)"

# --- Phase 1: create the run (vcluster + isolation, parallel lanes) --------
if [[ "${3:-}" != "resume" ]]; then
T=$(date +%s)
bash /tmp/opencode/poc/create-run.sh "${RUN_ARG}" "${PORT}" >"${RUN_DIR}/create.log" 2>&1 \
    || die "create-run failed, see ${RUN_DIR}/create.log"
phase "create run" "$T"

# --- Phase 2: install OSAC into the vcluster ------------------------------
T=$(date +%s)

# Persistent port-forward
pkill -f "port-forward.*vc-${RUN_ARG}" 2>/dev/null || true
sleep 1
nohup bash -c "while true; do oc --namespace vc-${RUN_ARG} port-forward svc/${RUN_ARG} ${PORT}:443 >>/tmp/opencode/poc/${RUN_ARG}-pf.log 2>&1; sleep 2; done" >/dev/null 2>&1 &
sleep 3
kubectl --kubeconfig "${KC}" get namespace default >/dev/null

cd "${OSAC}/osac-installer"
# Concurrent runs share this checkout: serialize the chart repacking.
(
    flock 9
    helm dependency update charts/osac >/dev/null 2>&1
) 9>/tmp/opencode/poc/helm-dep.lock

# Routes BEFORE the install: the console-proxy fetches JWKS from the run's
# external hostname and blocks helm --wait until the route answers with the
# backend's certificate. With no route the OpenShift router serves its
# default certificate and the proxy never becomes ready (fresh-run
# deadlock). A route pointing at a not-yet-existing service is fine.
# The echo-adapter route feeds the suite's METERING_ADAPTER_URL.
for spec in "fulfillment-api external-api passthrough" "fulfillment-internal-api internal-api passthrough" "osac-metering-echo-adapter http edge"; do
    read -r name port term <<<"${spec}"
    oc -n "vc-${RUN_ARG}" create route "${term}" "${name}" \
        --port="${port}" --service="${name}-x-osac-x-${RUN_ARG}" \
        --hostname="${name}-${RUN_ARG}.${DOMAIN}" >/dev/null 2>&1 || true
done

IMAGE_ARGS="
--set service.images.service.repository=localhost/fulfillment-service
--set service.images.service.tag=${TAG_FULFILLMENT}
--set service.images.service.pullPolicy=IfNotPresent
--set operator.image.repository=localhost/osac-operator
--set operator.image.tag=${TAG_OPERATOR}
--set operator.image.pullPolicy=IfNotPresent
--set metering.image.repository=localhost/osac-metering
--set metering.image.tag=${TAG_OLD}
--set metering.image.pullPolicy=IfNotPresent
--set metering.echoAdapter.image.repository=localhost/echo-adapter
--set metering.echoAdapter.image.tag=${TAG_OLD}
--set metering.echoAdapter.image.pullPolicy=IfNotPresent
--set aap.bootstrap.image=localhost/osac-aap-ee:${TAG_OLD}
--set aap.configAsCode.eeImage=localhost/osac-aap-ee:${TAG_OLD}
--set aap.configAsCode.eeImagePull=missing
--set bmf.image.repository=localhost/bare-metal-fulfillment-operator
--set bmf.image.tag=${TAG_BMF}
--set bmf.image.pullPolicy=IfNotPresent
--set aap.configAsCode.projectGitUri=https://github.com/omer-vishlitzky/osac
--set aap.configAsCode.projectGitBranch=${SHA}
--set aap.hooks.publishPollTimeout=1800
"

KUBECONFIG="${KC}" \
REMOTE_KUBECONFIG="${OUTER_KC}" \
CLUSTER_DOMAIN="${DOMAIN}" \
CLUSTER_OCP_VERSION=4.22 \
AAP_LICENSE_FILE="/tmp/opencode/poc/license.zip" \
make install-osac PLATFORM=openshift PROFILE=vcluster-ci NS=osac \
    EXTRA_HELM_ARGS="--values ${TI}/.vcluster-runs/${RUN_ARG}/instance-values.yaml \
--set service.externalHostname=fulfillment-api-${RUN_ARG}.${DOMAIN} \
--set service.internalHostname=fulfillment-internal-api-${RUN_ARG}.${DOMAIN} \
--set ui.externalHostname=osac-ui-${RUN_ARG}.${DOMAIN} \
--set ui.auth.oidcIssuerURL=https://keycloak-keycloak.${DOMAIN}/realms/${RUN_ARG} \
--set aap.controllerUrl=https://osac-aap-osac.${DOMAIN} \
--set aap.bootstrap.adminPasswordSecret.name=osac-aap-admin-password \
${IMAGE_ARGS}" >"${RUN_DIR}/install.log" 2>&1 \
    || echo "WARNING: install returned nonzero; hooks may still have run (check ${RUN_DIR}/install.log)"

# Routes: created before the install (see above); recreate only if the
# service name changed (they point at the syncer-suffixed service).
for spec in "fulfillment-api external-api passthrough" "fulfillment-internal-api internal-api passthrough" "osac-metering-echo-adapter http edge"; do
    read -r name port term <<<"${spec}"
    oc -n "vc-${RUN_ARG}" get route "${name}" >/dev/null 2>&1 || \
        oc -n "vc-${RUN_ARG}" create route "${term}" "${name}" \
            --port="${port}" --service="${name}-x-osac-x-${RUN_ARG}" \
            --hostname="${name}-${RUN_ARG}.${DOMAIN}" >/dev/null
done
phase "install OSAC" "$T"

# --- Phase 3: post-install wiring ------------------------------------------
T=$(date +%s)

# 3a. OpenBao namespace with JWT auth for the run's realm.
BAO_POD=$(oc get pod -n osac-infra -l app=openbao -o jsonpath='{.items[0].metadata.name}')
oc exec -i -n osac-infra "${BAO_POD}" -c server -- sh -s "${RUN_ARG}" <<'BAOEOF' >/dev/null
set -e
export BAO_ADDR=https://127.0.0.1:8200
export BAO_SKIP_VERIFY=true
export BAO_TOKEN=$(cat /vault/data/bootstrap.json | grep -o '"root_token": *"[^"]*"' | cut -d\" -f4)
RUN="$1"
ISSUER="https://keycloak-keycloak.apps.test-infra-cluster-poc.redhat.com/realms/${RUN}"
bao namespace create "$RUN" 2>/dev/null || echo "namespace exists"
bao auth enable -namespace="$RUN" -path=jwt jwt 2>/dev/null || echo "jwt already enabled"
cat > /tmp/lifecycle-policy.hcl <<'POLICY'
path "sys/namespaces/*" { capabilities = ["create", "read", "update", "delete", "list", "sudo"] }
path "sys/namespaces" { capabilities = ["list"] }
path "+/sys/mounts/*" { capabilities = ["create", "read", "update", "delete", "sudo"] }
path "+/sys/mounts" { capabilities = ["read", "list"] }
path "+/sys/policies/*" { capabilities = ["create", "read", "update", "delete", "list", "sudo"] }
path "+/sys/auth/*" { capabilities = ["create", "read", "update", "delete", "list", "sudo"] }
path "+/auth/*" { capabilities = ["create", "read", "update", "delete", "list", "sudo"] }
POLICY
bao policy write -namespace="$RUN" lifecycle /tmp/lifecycle-policy.hcl >/dev/null
bao write -namespace="$RUN" auth/jwt/config oidc_discovery_url="$ISSUER" \
  oidc_discovery_ca_pem=@/vault/ca/bundle.pem default_role=lifecycle >/dev/null
bao write -namespace="$RUN" auth/jwt/role/lifecycle role_type=jwt \
  bound_audiences=osac-api user_claim=sub policies=lifecycle token_ttl=1h >/dev/null
BAOEOF

# 3b. Resources the AAP job pods mount (OpenShift conventions the vanilla
# vcluster lacks, plus the un-prefixed credentials name they expect). The
# service-ca ConfigMap often already exists (chart/syncer compat objects);
# apply only when missing, and never carry outer-cluster metadata over —
# a stale resourceVersion conflicts with an existing object.
if ! kubectl --kubeconfig "${KC}" get configmap openshift-service-ca.crt -n osac >/dev/null 2>&1; then
    oc get configmap openshift-service-ca.crt -n osac -o json \
      | python3 -c "
import json, sys
d = json.load(sys.stdin)
d['metadata'] = {'name': d['metadata']['name'], 'namespace': 'osac'}
json.dump(d, sys.stdout)" \
      | kubectl --kubeconfig "${KC}" apply -f - >/dev/null
fi
kubectl --kubeconfig "${KC}" get secret "${RUN_ARG}-fulfillment-controller-credentials" -n osac -o json \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
d['metadata']['name'] = 'fulfillment-controller-credentials'
for k in ['resourceVersion', 'uid', 'creationTimestamp', 'managedFields']:
    d['metadata'].pop(k, None)
json.dump(d, sys.stdout)" \
  | kubectl --kubeconfig "${KC}" apply -f - >/dev/null

# 3c. Verify the install completed: the AAP hooks and the API-dependent
# hooks (publish-templates, register-local-storage). The bootstrap ran as
# a pre-install hook, so it is typically done already. register-local-
# storage deletes itself on success (hook-succeeded), so its effect is
# verified through the storage tier it registers.
ADMIN_JWT_EARLY=$(curl -sk -X POST "https://keycloak-keycloak.${DOMAIN}/realms/${RUN_ARG}/protocol/openid-connect/token" \
    -d grant_type=password -d client_id=osac-cli \
    --data-urlencode "username=admin" --data-urlencode "password=foobar" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])' 2>/dev/null || true)
for i in $(seq 1 90); do
    DONE=$(kubectl --kubeconfig "${KC}" get jobs -n osac --no-headers 2>/dev/null \
        | awk '$1=="osac-aap-bootstrap" || $1=="osac-aap-create-token" || $1=="osac-publish-templates" {if ($2 ~ /1\/1/) c++} END {print c+0}')
    TIER_OK=""
    if [[ -n "${ADMIN_JWT_EARLY}" ]]; then
        TIER_OK=$(grpcurl -insecure -H "authorization: Bearer ${ADMIN_JWT_EARLY}" \
            "fulfillment-internal-api-${RUN_ARG}.${DOMAIN}:443" osac.private.v1.StorageTiers/List 2>/dev/null \
            | python3 -c 'import json,sys; d=json.load(sys.stdin); print("yes" if d.get("items") else "")' 2>/dev/null || true)
    fi
    [[ "${DONE}" -ge 3 && -n "${TIER_OK}" ]] && break
    sleep 5
done
[[ "${DONE:-0}" -ge 3 ]] || die "AAP install hooks did not complete (see jobs -n osac in the vcluster)"
[[ -n "${TIER_OK:-}" ]] || die "storage tier did not register (register-local-storage hook)"
phase "post-install wiring" "$T"

T_INSTALL_READY=$(date +%s)
echo "*** INSTALLATION READY after $((T_INSTALL_READY - T_START))s ***"

# Install-only mode: stop here (a test suite creates its own tenants).
if [[ "${3:-}" == "install-only" ]]; then
    echo
    echo "=============== TIMING SUMMARY (${RUN_ARG}, install only) ==============="
    TOTAL=0
    for i in "${!PHASE_NAMES[@]}"; do
        printf '    %-30s %6ds\n' "${PHASE_NAMES[$i]}" "${PHASE_SECS[$i]}"
        TOTAL=$((TOTAL + PHASE_SECS[$i]))
    done
    printf '    %-30s %6ds\n' "INSTALL (nothing -> ready)" "$((T_INSTALL_READY - T_START))"
    echo "========================================================================"
    exit 0
fi
T_INSTALL_READY=$(date +%s)
fi

# --- Phase 4: tenant, user, catalog bootstrap (parallel tracks) ------------
T=$(date +%s)

# Resume mode: skip everything before this point (install already done).
if [[ "${3:-}" == "resume" ]]; then
    ADMIN_JWT=$(curl -sk -X POST "https://keycloak-keycloak.${DOMAIN}/realms/${RUN_ARG}/protocol/openid-connect/token" \
        -d grant_type=password -d client_id=osac-cli \
        --data-urlencode "username=admin" --data-urlencode "password=foobar" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
    PRIV="fulfillment-internal-api-${RUN_ARG}.${DOMAIN}:443"
    KC="${TI}/.vcluster-runs/${RUN_ARG}/kubeconfig-local"
fi
ADMIN_JWT=$(curl -sk -X POST "https://keycloak-keycloak.${DOMAIN}/realms/${RUN_ARG}/protocol/openid-connect/token" \
    -d grant_type=password -d client_id=osac-cli \
    --data-urlencode "username=admin" --data-urlencode "password=foobar" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

PRIV="fulfillment-internal-api-${RUN_ARG}.${DOMAIN}:443"

# Everything below is gated on the tenant existing; create it (plus the
# user) first, then fan out three independent tracks.
grpcurl -insecure -H "authorization: Bearer ${ADMIN_JWT}" \
    -d '{"object": {"metadata": {"name": "'"${TENANT}"'"}}}' \
    "${PRIV}" osac.private.v1.Tenants/Create >/dev/null 2>&1 || true
grpcurl -insecure -H "authorization: Bearer ${ADMIN_JWT}" \
    -d '{"object": {"metadata": {"name": "'"${TENANT}"'-user", "tenant": "'"${TENANT}"'"}, "spec": {"username": "'"${TENANT_USER}"'", "enabled": true}}}' \
    "${PRIV}" osac.private.v1.Users/Create >/dev/null 2>&1 || true

# Track A: storage marker secret (operator -> AAP backend job creates it;
# fall back to creating it manually if the job context did not cooperate).
track_storage() {
    local i
    for i in $(seq 1 60); do
        kubectl --kubeconfig "${KC}" get secret -n osac -l osac.openshift.io/tenant=${TENANT} \
            --no-headers 2>/dev/null | grep -q lvms-tenant-config && return 0
        sleep 3
    done
    local bid
    bid=$(kubectl --kubeconfig "${KC}" get tenant ${TENANT} -n osac \
        -o jsonpath='{.status.storageBackends[0].name}' 2>/dev/null)
    [[ -n "${bid}" ]] || return 1
    kubectl --kubeconfig "${KC}" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: lvms-tenant-config-${TENANT}-${bid}
  namespace: osac
  labels:
    osac.openshift.io/tenant: ${TENANT}
    osac.openshift.io/storage-provider: lvms
    osac.openshift.io/storage-backend-id: ${bid}
    app.kubernetes.io/managed-by: osac-aap
type: Opaque
stringData:
  provider: lvms
EOF
    echo "(storage marker secret created manually; the backend job did not)"
}
track_storage >"${RUN_DIR}/track-storage.log" 2>&1 &
PID_STORAGE=$!

# Track B: Keycloak org membership (waits for the controller to sync the
# org, then adds the user and the /members group).
track_org() {
    python3 - "${RUN_ARG}" "${TENANT}" "${TENANT_USER}" <<'PYEOF'
import base64, json, os, ssl, subprocess, sys, time
import urllib.parse, urllib.request

run = sys.argv[1]
tenant = sys.argv[2]
tenant_user = sys.argv[3]
DOMAIN = "apps.test-infra-cluster-poc.redhat.com"
KC_URL = f"https://keycloak-keycloak.{DOMAIN}"
CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE
ENV = {**os.environ, "KUBECONFIG": os.path.expanduser("~/.kube/poc.kubeconfig")}

def secret(key):
    out = subprocess.check_output(
        ["oc", "get", "secret", "-n", "keycloak", "keycloak-admin-credentials",
         "-o", f"jsonpath={{.data.{key}}}"], env=ENV)
    return base64.b64decode(out).decode()

def req(method, url, token, data=None, form=False):
    r = urllib.request.Request(url, method=method)
    r.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        if form:
            r.data = urllib.parse.urlencode(data).encode()
            r.add_header("Content-Type", "application/x-www-form-urlencoded")
        else:
            r.data = json.dumps(data).encode()
            r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, context=CTX) as resp:
        return resp.status, resp.read().decode()

form = {"grant_type": "password", "client_id": "admin-cli",
        "username": secret("admin-username"), "password": secret("admin-password")}
r = urllib.request.Request(f"{KC_URL}/realms/master/protocol/openid-connect/token",
                           data=urllib.parse.urlencode(form).encode(),
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
with urllib.request.urlopen(r, context=CTX) as resp:
    tok = json.loads(resp.read())["access_token"]

base = f"{KC_URL}/admin/realms/{run}"

def find(path, key, value):
    try:
        _, body = req("GET", base + path, tok)
        return next((x for x in json.loads(body) if x.get(key) == value), None)
    except Exception:
        return None

org = None
for _ in range(80):
    org = find(f"/organizations?search={tenant}&exact=true", "name", tenant)
    if org:
        break
    time.sleep(3)
if not org:
    sys.exit(f"organization {tenant} never appeared in the realm")

user = None
for _ in range(40):
    user = find(f"/users?username={tenant_user}&exact=true", "username", tenant_user)
    if user:
        break
    time.sleep(3)
if not user:
    sys.exit(f"user {tenant_user} not found in the realm")

try:
    req("POST", f"{base}/organizations/{org['id']}/members", tok, user["id"])
except urllib.error.HTTPError as e:
    if e.code not in (201, 204, 409):
        raise

gid = next((g["id"] for g in json.loads(req("GET", f"{base}/organizations/{org['id']}/groups", tok)[1])
            if g.get("name") == "/members"), None)
if not gid:
    gid = json.loads(req("POST", f"{base}/organizations/{org['id']}/groups", tok, {"name": "/members"})[1])["id"]
req("PUT", f"{base}/organizations/{org['id']}/groups/{gid}/members/{user['id']}", tok)
print("org membership ready")
PYEOF
}
track_org >"${RUN_DIR}/track-org.log" 2>&1 &
PID_ORG=$!

# Track C: catalog objects (instance type + fedora disk image).
track_catalog() {
    grpcurl -insecure -H "authorization: Bearer ${ADMIN_JWT}" -d '{
      "object": {"metadata": {"name": "demo-it"},
        "spec": {"vcpus": 2, "memoryGib": 4}}}' \
        "${PRIV}" osac.private.v1.InstanceTypes/Create >/dev/null 2>&1 || true
    grpcurl -insecure -H "authorization: Bearer ${ADMIN_JWT}" -d '{
      "object": {"metadata": {"name": "fedora-41"},
        "spec": {"sourceType": "SOURCE_TYPE_REGISTRY",
                 "sourceRef": "quay.io/containerdisks/fedora:41",
                 "guestOsFamily": "GUEST_OS_FAMILY_LINUX",
                 "architecture": ["ARCHITECTURE_AMD64"]}}}' \
        "${PRIV}" osac.private.v1.DiskImages/Create >/dev/null 2>&1 || true
}
track_catalog >"${RUN_DIR}/track-catalog.log" 2>&1 &
PID_CATALOG=$!

TRACK_FAILED=0
for pid in "${PID_STORAGE}" "${PID_ORG}" "${PID_CATALOG}"; do
    wait "${pid}" || TRACK_FAILED=1
done
[[ "${TRACK_FAILED}" = 0 ]] || die "a bootstrap track failed; see ${RUN_DIR}/track-*.log"

# The compute template (published by the install hook) must be in the
# catalog; this is normally already true.
for i in $(seq 1 40); do
    grpcurl -insecure -H "authorization: Bearer ${ADMIN_JWT}" \
        -d '{"id": "osac.templates.ocp_virt_vm"}' \
        "fulfillment-api-${RUN_ARG}.${DOMAIN}:443" \
        osac.public.v1.ComputeInstanceTemplates/Get >/dev/null 2>&1 && break
    sleep 3
done
phase "tenant + catalog bootstrap" "$T"

# --- Phase 5: create the VM and wait for Running ---------------------------
T=$(date +%s)
T1_JWT=$(curl -sk -X POST "https://keycloak-keycloak.${DOMAIN}/realms/${RUN_ARG}/protocol/openid-connect/token" \
    -d grant_type=password -d client_id=osac-cli \
    --data-urlencode "username=${TENANT_USER}" --data-urlencode "password=foobar" \
    -d 'scope=openid organization' \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

DISK_ID=$(grpcurl -insecure -H "authorization: Bearer ${ADMIN_JWT}" \
    -d '{"filter": "this.metadata.name == \"fedora-41\""}' \
    "${PRIV}" osac.private.v1.DiskImages/List 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["items"][0]["id"])')

CI_ID=$(grpcurl -insecure -H "authorization: Bearer ${T1_JWT}" -d '{
  "object": {
    "metadata": {"name": "timed-vm-001"},
    "spec": {
      "template": {"id": "osac.templates.ocp_virt_vm"},
      "instanceType": {"name": "demo-it", "tenant": "shared", "shared": true},
      "bootDisk": {"sizeGib": 10, "storageTier": {"id": "local"}},
      "diskImage": {"id": "'"${DISK_ID}"'"},
      "runStrategy": "COMPUTE_INSTANCE_RUN_STRATEGY_ALWAYS"
    }
  }
}' "fulfillment-api-${RUN_ARG}.${DOMAIN}:443" osac.public.v1.ComputeInstances/Create 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["object"]["id"])')

echo "ComputeInstance: ${CI_ID}"

VMI=""
for i in $(seq 1 200); do
    VMI=$(oc get vmi -A -l "osac.openshift.io/instance=${RUN_ARG}" \
        -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.ready}{"\n"}{end}' 2>/dev/null | grep "True" | head -1 || true)
    [[ -n "${VMI}" ]] && break
    sleep 3
done
[[ -n "${VMI}" ]] || die "VMI did not reach Ready within 10 minutes"
phase "VM provisioned + Running" "$T"

# --- Summary ----------------------------------------------------------------
T_END=$(date +%s)
echo
echo "=================== TIMING SUMMARY (${RUN_ARG}) ==================="
TOTAL=0
for i in "${!PHASE_NAMES[@]}"; do
    printf '    %-30s %6ds\n' "${PHASE_NAMES[$i]}" "${PHASE_SECS[$i]}"
    TOTAL=$((TOTAL + PHASE_SECS[$i]))
done
echo "    -------------------------------------------------"
printf '    %-30s %6ds\n' "INSTALL (nothing -> ready)" "$((T_INSTALL_READY - T_START))"
printf '    %-30s %6ds\n' "VM (ready -> running)" "$((T_END - T_INSTALL_READY))"
printf '    %-30s %6ds\n' "TOTAL" "$((T_END - T_START))"
echo "VMI: ${VMI}"
echo "ComputeInstance ID: ${CI_ID}"
echo "==================================================================="
