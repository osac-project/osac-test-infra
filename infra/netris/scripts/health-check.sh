#!/usr/bin/env bash
# health-check.sh — Verify netris-test-infra deployment health
# Run on the BM server:  scripts/health-check.sh
# Or remotely:           make health-check SERVER=<ip> PASSWORD=<pw>

set -uo pipefail

KUBECONFIG="${KUBECONFIG:-/root/.kube/config}"
export KUBECONFIG

K3S_KUBECONFIG="/etc/rancher/k3s/k3s.yaml"
OSAC_NS="${OSAC_NAMESPACE:-osac-devel}"
KEYCLOAK_NS="keycloak"
OCP_VM_PATTERN="${OCP_VM_PATTERN:-hgx-pod00-su0-h00}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BOLD='\033[1m'
NC='\033[0m'

PASS=0
FAIL=0
WARN=0

pass() { ((PASS++)); echo -e "  ${GREEN}✓${NC} $1"; }
fail() { ((FAIL++)); echo -e "  ${RED}✗${NC} $1"; }
warn() { ((WARN++)); echo -e "  ${YELLOW}!${NC} $1"; }

section() { echo -e "\n${BOLD}── $1 ──${NC}"; }

# ── KVM Virtual Machines ──

section "KVM Virtual Machines"

if command -v virsh &>/dev/null; then
    OCP_VM=$(virsh list --all --name 2>/dev/null | grep "$OCP_VM_PATTERN" | head -1)
    if [[ -n "$OCP_VM" ]]; then
        VM_STATE=$(virsh domstate "$OCP_VM" 2>/dev/null || echo "unknown")
        if [[ "$VM_STATE" == "running" ]]; then
            pass "OCP VM $OCP_VM is running"
        else
            fail "OCP VM $OCP_VM state: $VM_STATE"
        fi
    else
        fail "OCP VM matching '$OCP_VM_PATTERN' not found"
    fi

    TOTAL_VMS=$(virsh list --name 2>/dev/null | grep -c . || echo 0)
    FABRIC_VMS=$(virsh list --name 2>/dev/null | grep -cE '^ns-' || echo 0)
    pass "Fabric VMs running: $FABRIC_VMS (total running: $TOTAL_VMS)"
else
    warn "virsh not found"
fi

# ── Netris Controller (k3s) ──

section "Netris Controller (k3s)"

if [[ -f "$K3S_KUBECONFIG" ]]; then
    NOT_RUNNING=$(KUBECONFIG="$K3S_KUBECONFIG" kubectl get pods -n netris-controller --no-headers 2>/dev/null \
        | grep -v -E 'Running|Completed' || true)
    RUNNING_COUNT=$(KUBECONFIG="$K3S_KUBECONFIG" kubectl get pods -n netris-controller --no-headers 2>/dev/null \
        | grep -c 'Running' || echo 0)

    if [[ -z "$NOT_RUNNING" && "$RUNNING_COUNT" -gt 0 ]]; then
        pass "netris-controller pods: $RUNNING_COUNT running"
    elif [[ "$RUNNING_COUNT" -gt 0 ]]; then
        BAD=$(echo "$NOT_RUNNING" | awk '{printf "%s(%s) ", $1, $3}')
        warn "netris-controller: $RUNNING_COUNT running, unhealthy: $BAD"
    else
        fail "netris-controller: no running pods"
    fi
else
    warn "k3s kubeconfig not found at $K3S_KUBECONFIG"
fi

# ── OCP Cluster ──

section "OCP Cluster"

if ! oc whoami &>/dev/null; then
    fail "Cannot authenticate to cluster (KUBECONFIG=$KUBECONFIG)"
    echo -e "\n${RED}Cannot continue without cluster access.${NC}"
    exit 1
fi
pass "Cluster authentication ($(oc whoami))"

NODE_STATUS=$(oc get nodes --no-headers 2>/dev/null | awk '{print $2}')
if [[ "$NODE_STATUS" == "Ready" ]]; then
    pass "Node is Ready"
else
    fail "Node status: $NODE_STATUS"
fi

CV=$(oc get clusterversion version -o jsonpath='{.status.history[0].state}' 2>/dev/null)
CV_VER=$(oc get clusterversion version -o jsonpath='{.status.history[0].version}' 2>/dev/null)
if [[ "$CV" == "Completed" ]]; then
    pass "ClusterVersion $CV_VER installed"
else
    fail "ClusterVersion state: $CV ($CV_VER)"
fi

DEGRADED_COS=$(oc get co --no-headers 2>/dev/null | awk '$5=="True" {print $1}')
if [[ -z "$DEGRADED_COS" ]]; then
    pass "All cluster operators healthy"
else
    fail "Degraded cluster operators: $DEGRADED_COS"
fi

# ── OSAC Pods ──

section "OSAC Pods ($OSAC_NS)"

EXPECTED_DEPLOYMENTS=(
    "osac-operator"
    "osac-ui"
    "fulfillment-controller"
    "fulfillment-grpc-server"
    "fulfillment-rest-gateway"
    "fulfillment-ingress-proxy"
    "fulfillment-console-proxy"
    "osac-aap-gateway"
    "postgres"
)

for dep in "${EXPECTED_DEPLOYMENTS[@]}"; do
    READY=$(oc get deployment "$dep" -n "$OSAC_NS" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
    DESIRED=$(oc get deployment "$dep" -n "$OSAC_NS" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "?")
    if [[ "$READY" == "$DESIRED" && "$READY" != "0" ]]; then
        pass "$dep ($READY/$DESIRED ready)"
    elif [[ "$DESIRED" == "?" ]]; then
        fail "$dep: deployment not found"
    else
        fail "$dep ($READY/$DESIRED ready)"
    fi
done

AAP_TASK_READY=$(oc get deployment osac-aap-controller-task -n "$OSAC_NS" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
AAP_WEB_READY=$(oc get deployment osac-aap-controller-web -n "$OSAC_NS" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
if [[ "$AAP_TASK_READY" -ge 1 && "$AAP_WEB_READY" -ge 1 ]]; then
    pass "AAP Controller (task=$AAP_TASK_READY, web=$AAP_WEB_READY)"
else
    fail "AAP Controller (task=$AAP_TASK_READY, web=$AAP_WEB_READY)"
fi

# ── Keycloak ──

section "Keycloak ($KEYCLOAK_NS)"

KC_READY=$(oc get deployment keycloak-service -n "$KEYCLOAK_NS" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
if [[ "$KC_READY" -ge 1 ]]; then
    pass "keycloak-service ($KC_READY ready)"
else
    fail "keycloak-service ($KC_READY ready)"
fi

KC_DB_READY=$(oc get statefulset keycloak-database -n "$KEYCLOAK_NS" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
if [[ "$KC_DB_READY" -ge 1 ]]; then
    pass "keycloak-database ($KC_DB_READY ready)"
else
    fail "keycloak-database ($KC_DB_READY ready)"
fi

# ── Service Endpoints ──

section "Service Endpoints"

APPS_DOMAIN=$(oc get ingresses.config/cluster -o jsonpath='{.spec.domain}' 2>/dev/null)

check_url() {
    local name=$1
    local url=$2
    local expected=${3:-200}
    local code
    code=$(curl -sk -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 "$url" 2>/dev/null || echo "000")
    if [[ "$code" == "$expected" ]]; then
        pass "$name (HTTP $code)"
    elif [[ "$code" == "000" ]]; then
        fail "$name: connection failed"
    else
        fail "$name: HTTP $code (expected $expected)"
    fi
}

check_url "OSAC UI" "https://osac-ui-${OSAC_NS}.${APPS_DOMAIN}"
check_url "Keycloak realm" "https://keycloak-keycloak.${APPS_DOMAIN}/realms/osac"
check_url "OCP Console" "https://console-openshift-console.${APPS_DOMAIN}"
check_url "AAP Gateway" "https://osac-aap-${OSAC_NS}.${APPS_DOMAIN}"

HEALTHZ_CODE=$(curl -sk -o /dev/null -w '%{http_code}' --connect-timeout 5 \
    "https://fulfillment-internal-api-${OSAC_NS}.${APPS_DOMAIN}/healthz" 2>/dev/null || echo "000")
if [[ "$HEALTHZ_CODE" == "200" ]]; then
    pass "Fulfillment /healthz (HTTP $HEALTHZ_CODE)"
elif [[ "$HEALTHZ_CODE" == "000" ]]; then
    warn "Fulfillment /healthz: connection failed (internal route)"
else
    warn "Fulfillment /healthz: HTTP $HEALTHZ_CODE"
fi

# ── Authentication ──

section "Authentication"

KC_URL="https://keycloak-keycloak.${APPS_DOMAIN}"

TOKEN_RESP=$(curl -sk -X POST "${KC_URL}/realms/osac/protocol/openid-connect/token" \
    -d "client_id=admin-cli" \
    -d "username=admin" \
    -d "password=admin" \
    -d "grant_type=password" 2>/dev/null)
TOKEN_LEN=$(echo "$TOKEN_RESP" | python3 -c "import sys,json; t=json.load(sys.stdin); print(len(t.get('access_token','')))" 2>/dev/null || echo "0")

if [[ "$TOKEN_LEN" -gt 100 ]]; then
    pass "Keycloak token (admin-cli password grant, ${TOKEN_LEN} chars)"
else
    ERROR=$(echo "$TOKEN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('error_description','unknown'))" 2>/dev/null)
    fail "Keycloak token failed: $ERROR"
fi

# ── OSAC CLI ──

section "OSAC CLI"

if command -v osac &>/dev/null; then
    pass "osac CLI installed ($(osac version 2>/dev/null | head -1 || echo 'unknown version'))"

    if osac login --insecure \
        --issuer="${KC_URL}/realms/osac" \
        --flow=password --user=admin --password=admin \
        "https://fulfillment-api-${OSAC_NS}.${APPS_DOMAIN}" &>/dev/null; then
        pass "osac login (password flow)"
    else
        fail "osac login failed"
    fi

    WHOAMI=$(osac whoami 2>/dev/null | head -1)
    if [[ -n "$WHOAMI" ]]; then
        pass "osac whoami ($WHOAMI)"
    else
        warn "osac whoami: no response"
    fi

    TENANTS=$(osac get tenants 2>/dev/null | grep -v '^PROJECT' | grep -v '^-' | wc -l)
    if [[ "$TENANTS" -ge 1 ]]; then
        pass "osac get tenants ($TENANTS found)"
    else
        warn "osac get tenants: none found"
    fi
else
    warn "osac CLI not installed"
fi

# ── OSAC CRDs ──

section "OSAC CRDs"

EXPECTED_CRDS=(
    "tenants.osac.openshift.io"
    "clusterorders.osac.openshift.io"
    "computeinstances.osac.openshift.io"
    "virtualnetworks.osac.openshift.io"
    "subnets.osac.openshift.io"
    "securitygroups.osac.openshift.io"
)

for crd in "${EXPECTED_CRDS[@]}"; do
    if oc get crd "$crd" &>/dev/null; then
        pass "$crd"
    else
        fail "$crd: not found"
    fi
done

# ── Helm Releases ──

section "Helm Releases"

for ns in "$OSAC_NS" osac-prereqs osac-operators; do
    RELEASE=$(helm list -n "$ns" --short 2>/dev/null)
    if [[ -n "$RELEASE" ]]; then
        STATUS=$(helm list -n "$ns" -o json 2>/dev/null | python3 -c "import sys,json; r=json.load(sys.stdin); print(r[0]['status'])" 2>/dev/null)
        if [[ "$STATUS" == "deployed" ]]; then
            pass "helm/$RELEASE ($ns) — deployed"
        else
            fail "helm/$RELEASE ($ns) — $STATUS"
        fi
    else
        warn "No Helm release in $ns"
    fi
done

# ── CaaS / HostedCluster ──

section "CaaS (HostedCluster)"

HC_JSON=$(oc get hostedclusters.hypershift.openshift.io --all-namespaces -o json 2>/dev/null)
HC_COUNT=$(echo "$HC_JSON" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('items',[])))" 2>/dev/null || echo "0")

if [[ "$HC_COUNT" -gt 0 ]]; then
    while IFS='|' read -r hc_ns hc_name hc_version hc_state hc_available; do
        _label="HostedCluster $hc_ns/$hc_name"
        [[ -n "$hc_version" ]] && _label="$_label ($hc_version)"
        if [[ "$hc_state" == "Completed" && "$hc_available" == "True" ]]; then
            pass "$_label — Completed, available"
        elif [[ "$hc_available" == "True" ]]; then
            warn "$_label — state=$hc_state, available"
        else
            fail "$_label — state=$hc_state, available=$hc_available"
        fi
    done < <(echo "$HC_JSON" | python3 -c "
import sys, json
items = json.load(sys.stdin).get('items', [])
for i in items:
    ns = i['metadata']['namespace']
    name = i['metadata']['name']
    hist = i.get('status',{}).get('version',{}).get('history',[])
    version = hist[0]['version'] if hist else ''
    state = hist[0].get('state','Unknown') if hist else 'Unknown'
    avail = next((c['status'] for c in i.get('status',{}).get('conditions',[]) if c['type']=='Available'), 'Unknown')
    print(f'{ns}|{name}|{version}|{state}|{avail}')
" 2>/dev/null)

    # Check CaaS worker nodes via guest kubeconfig
    HC_NS=$(echo "$HC_JSON" | python3 -c "import sys,json; i=json.load(sys.stdin)['items'][0]; print(i['metadata']['namespace'])" 2>/dev/null)
    HC_NAME=$(echo "$HC_JSON" | python3 -c "import sys,json; i=json.load(sys.stdin)['items'][0]; print(i['metadata']['name'])" 2>/dev/null)
    SECRET_NAME="${HC_NAME}-admin-kubeconfig"
    GUEST_KC=$(oc get secret -n "$HC_NS" "$SECRET_NAME" -o jsonpath='{.data.kubeconfig}' 2>/dev/null | base64 -d 2>/dev/null)

    if [[ -n "$GUEST_KC" ]]; then
        CAAS_NODES=$(echo "$GUEST_KC" | KUBECONFIG=/dev/stdin oc get nodes --no-headers 2>/dev/null | wc -l)
        CAAS_READY=$(echo "$GUEST_KC" | KUBECONFIG=/dev/stdin oc get nodes --no-headers 2>/dev/null | grep -c ' Ready' || echo 0)
        if [[ "$CAAS_NODES" -gt 0 ]]; then
            pass "CaaS worker nodes: $CAAS_READY/$CAAS_NODES Ready"
        else
            warn "CaaS cluster has no worker nodes"
        fi
    else
        warn "Could not retrieve CaaS guest kubeconfig"
    fi
else
    warn "No HostedClusters found (CaaS not deployed or not yet ready)"
fi

# ── DNS ──

section "DNS"

if [[ -f /etc/dnsmasq.d/ocp-sno.conf ]]; then
    pass "dnsmasq OCP config present"
    API_DNS=$(grep 'api\.' /etc/dnsmasq.d/ocp-sno.conf 2>/dev/null | head -1)
    if [[ -n "$API_DNS" ]]; then
        pass "API DNS: $API_DNS"
    else
        warn "API DNS entry not found in dnsmasq config"
    fi
else
    warn "dnsmasq OCP config not found at /etc/dnsmasq.d/ocp-sno.conf"
fi

# ── Netris Networking ──

section "Netris Networking"

if [[ -f "$K3S_KUBECONFIG" ]]; then
    NETRIS_WEB=$(KUBECONFIG="$K3S_KUBECONFIG" kubectl get pods -n netris-controller --no-headers 2>/dev/null \
        | grep 'web-service-backend' | awk '{print $3}')
    if [[ "$NETRIS_WEB" == "Running" ]]; then
        pass "Netris web-service-backend is Running"
    else
        warn "Netris web-service-backend: $NETRIS_WEB"
    fi

    NETRIS_FRONTEND=$(KUBECONFIG="$K3S_KUBECONFIG" kubectl get pods -n netris-controller --no-headers 2>/dev/null \
        | grep 'web-service-frontend' | awk '{print $3}')
    if [[ "$NETRIS_FRONTEND" == "Running" ]]; then
        pass "Netris web-service-frontend is Running"
    else
        warn "Netris web-service-frontend: $NETRIS_FRONTEND"
    fi
fi

# ── Summary ──

echo ""
echo -e "${BOLD}══════════════════════════════════════${NC}"
TOTAL=$((PASS + FAIL + WARN))
echo -e "${BOLD}  Results: ${GREEN}${PASS} passed${NC}, ${RED}${FAIL} failed${NC}, ${YELLOW}${WARN} warnings${NC}  (${TOTAL} checks)"
if [[ $FAIL -eq 0 ]]; then
    echo -e "${BOLD}  ${GREEN}Deployment is healthy${NC}"
else
    echo -e "${BOLD}  ${RED}Deployment has issues${NC}"
fi
echo -e "${BOLD}══════════════════════════════════════${NC}"

exit $FAIL
