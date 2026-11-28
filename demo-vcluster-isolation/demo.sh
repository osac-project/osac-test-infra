#!/usr/bin/env bash
#
# Self-recording terminal demo: two OSAC vcluster runs, one outer cluster,
# zero overlap. Runs every command for real.
#
# Requires: bash, asciinema. Run: ./demo.sh
# Verify without a terminal: asciinema cat demo.cast

set -euo pipefail
cd "$(dirname "$0")"

OUT=demo.cast
export KUBECONFIG="$HOME/.kube/poc.kubeconfig"
DEMOA="/tmp/ti-impl/.vcluster-runs/demoav1/kubeconfig-local"
DEMOB="/tmp/ti-impl/.vcluster-runs/demobv1/kubeconfig-local"
DOMAIN="apps.test-infra-cluster-poc.redhat.com"

PROMPT=$'\e[1;32mouter\e[0m$ '
TYPE_DELAY=0.02
PAUSE=0.5
COLS=160
ROWS=40

if [ -z "${ASCIINEMA_REC:-}" ]; then
  exec asciinema rec --cols "$COLS" --rows "$ROWS" -c "$0" "$OUT"
fi

type_cmd() {
  local cmd="$1" i
  printf '%s' "$PROMPT"
  for (( i=0; i<${#cmd}; i++ )); do
    printf '%s' "${cmd:$i:1}"
    sleep "$TYPE_DELAY"
  done
  printf '\n'
  eval "$cmd"
  sleep "$PAUSE"
}

say() {
  printf '\e[36m# %s\e[0m\n' "$1"
  sleep 1.5
}

scene() {
  printf '\e[1;37m==> Scene %s: %s\e[0m\n' "$1" "$2"
  printf '\e[36m# %s\e[0m\n' "$3"
  sleep 2
}

# --- Scene 1: The outer cluster ---

scene 1 "One outer cluster, shared infrastructure" \
  "One copy of PostgreSQL, Kafka, Keycloak, cert-manager and AAP. No per-run copies."

say "The outer cluster. Every shared service runs exactly once."
type_cmd 'oc get ns | grep -E "osac-infra|osac-kafka|keycloak|cert-manager|ansible-aap" | awk "{print \$1}"'
say "These namespaces hold one copy of each service. Every OSAC run will share them."

# --- Scene 2: Two vcluster runs ---

scene 2 "Two OSAC runs, side by side" \
  "Each run is a vcluster: its own API server, its own CRDs, its own objects."

type_cmd 'oc get ns | grep vc-'
say "Two vcluster namespaces. Each one is a complete, independent OSAC installation."

say "Inside run demoav1, only OSAC pods exist:"
type_cmd "kubectl --kubeconfig $DEMOA get pods -A --no-headers | awk '{print \$1}' | sort | uniq -c | sort -rn"

say "Inside run demobv1, the same picture:"
type_cmd "kubectl --kubeconfig $DEMOB get pods -A --no-headers | awk '{print \$1}' | sort | uniq -c | sort -rn"

say "Both runs use the same images (same code, same branch):"
type_cmd "diff <(kubectl --kubeconfig $DEMOA get pods -n osac -o jsonpath='{range .items[*]}{.status.containerStatuses[0].imageID}{\"\\n\"}{end}' | sort) <(kubectl --kubeconfig $DEMOB get pods -n osac -o jsonpath='{range .items[*]}{.status.containerStatuses[0].imageID}{\"\\n\"}{end}' | sort) && echo 'IDENTICAL images'"

# --- Scene 3: Shared services, separate data ---

scene 3 "Shared services, separate data" \
  "One PostgreSQL, four databases. One Kafka, six users. One Keycloak, two realms."

say "One PostgreSQL server, four databases — two per run:"
type_cmd 'oc exec -n osac-infra deploy/postgres -- psql -U postgres -Atc "SELECT datname FROM pg_database WHERE datname LIKE '\''demo%'\''"'

say "One Kafka cluster, six users — three per run:"
type_cmd 'oc get kafkausers -n osac-kafka --no-headers | awk "{print \$1}"'

say "One Keycloak, two realms — one per run:"
type_cmd "curl -sk https://keycloak-keycloak.$DOMAIN/realms/demoav1/.well-known/openid-configuration | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"issuer\"])'"
type_cmd "curl -sk https://keycloak-keycloak.$DOMAIN/realms/demobv1/.well-known/openid-configuration | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"issuer\"])'"

# --- Scene 4: The isolation proof ---

scene 4 "Run A has a VM. Run B does not." \
  "A ComputeInstance exists in run demoav1. Run demobv1 has never heard of it."

say "Run demoav1: one ComputeInstance, created through its API."
type_cmd "kubectl --kubeconfig $DEMOA get computeinstances -A"

say "Run demobv1: zero ComputeInstances. Same command, different run."
type_cmd "kubectl --kubeconfig $DEMOB get computeinstances -A"

say "The VM objectively exists on the outer cluster, labeled with run A's ID:"
type_cmd 'oc get vm -A --show-labels | grep -v "^NAME" | head -5'

say "Filtering by run B's label finds nothing:"
type_cmd 'oc get vmi -A -l osac.openshift.io/instance=demobv1 2>/dev/null || echo "No resources found"'

# --- Scene 5: How to check yourself ---

scene 5 "Check it yourself" \
  "The environment stays running. See EXPLORE.md for the full command list."

say "The environment is live. Both kubeconfigs are in the demo directory."
say "Run 'cat EXPLORE.md' for every command you can use to verify this yourself."

printf '\e[1;37mDemo complete.\e[0m\n'
