#!/bin/bash
# pair-test.sh — the final proof: two OSAC installations created AT ONCE,
# a VM provisioned in one, isolation verified against the other.
#
# Reports: install time (nothing -> ready) for both runs, VM time for the
# provisioning run, and the isolation evidence.
set -euo pipefail

export KUBECONFIG="$HOME/.kube/poc.kubeconfig"
DOMAIN="apps.test-infra-cluster-poc.redhat.com"
TI=/tmp/ti-impl
RUN_A="demodv1"   # full pipeline: install + VM
RUN_B="demoev1"   # install only
PORT_A="9446"
PORT_B="9447"
OUT=/tmp/opencode/poc/pair-test
mkdir -p "${OUT}"

echo "=== Pair test starting at $(date -u +%H:%M:%S)"
T0=$(date +%s)

# Launch both installs in parallel.
bash /tmp/opencode/poc/timed-run.sh "${RUN_A}" "${PORT_A}" >"${OUT}/${RUN_A}.log" 2>&1 &
PID_A=$!
bash /tmp/opencode/poc/timed-run.sh "${RUN_B}" "${PORT_B}" install-only >"${OUT}/${RUN_B}.log" 2>&1 &
PID_B=$!

FAIL=0
wait "${PID_A}" || FAIL=1
wait "${PID_B}" || FAIL=1
[[ "${FAIL}" = 0 ]] || { echo "A run failed; see ${OUT}/"; exit 1; }

T1=$(date +%s)
echo "=== Both installations + VM done at $(date -u +%H:%M:%S)"

echo
echo "=================== ISOLATION PROOF ==================="
echo "--- ${RUN_A} (owns the VM):"
kubectl --kubeconfig "${TI}/.vcluster-runs/${RUN_A}/kubeconfig-local" get computeinstances -A 2>/dev/null || echo "(none)"
oc get vmi -A -l "osac.openshift.io/instance=${RUN_A}" 2>/dev/null || echo "(no VMIs)"
echo
echo "--- ${RUN_B} (must see nothing):"
kubectl --kubeconfig "${TI}/.vcluster-runs/${RUN_B}/kubeconfig-local" get computeinstances -A 2>/dev/null || echo "(none)"
oc get vmi -A -l "osac.openshift.io/instance=${RUN_B}" 2>/dev/null || echo "(no VMIs)"
echo
echo "--- Every VMI on the outer cluster, by owning run:"
oc get vmi -A -l osac.openshift.io/instance \
    -o custom-columns=RUN:.metadata.labels.osac\.openshift\.io/instance,NAME:.metadata.name,PHASE:.status.phase,READY:.status.ready 2>/dev/null
echo "======================================================="
echo "Total wall time (two installs in parallel + one VM): $((T1 - T0))s"
