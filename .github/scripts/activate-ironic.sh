#!/usr/bin/env bash
# Activate Ironic: patch the metal3 Provisioning CR, wait for the metal3
# pods and the BareMetalHost admission webhook.
#
# CR settings: no provisioning network; virtual-media TLS off, so the
# local nginx cache (setup-sushy-tools.sh) can serve the boot ISOs over
# HTTP; watchAllNamespaces, so BMHs may live outside openshift-machine-api.
# The webhook wait matters to every consumer: any inventory backend's
# BMHs go through it.
#
# Needs oc on PATH (KUBECONFIG or the default kubeconfig). Idempotent.
set -euo pipefail

echo "==> Activating Ironic (Provisioning CR)..."
oc apply -f - <<'EOF'
apiVersion: metal3.io/v1alpha1
kind: Provisioning
metadata:
  name: provisioning-configuration
spec:
  provisioningNetwork: "Disabled"
  # The job-local nginx cache handles HTTP; publish ephemeral boot ISOs on 6180.
  disableVirtualMediaTLS: true
  watchAllNamespaces: true
EOF

echo "Waiting for metal3 pods to appear..."
oc wait --for=create pods \
  -l baremetal.openshift.io/cluster-baremetal-operator=metal3-state \
  -n openshift-machine-api --timeout=300s
echo "Waiting for metal3 pods to be ready..."
oc wait --for=condition=Ready pods \
  -l baremetal.openshift.io/cluster-baremetal-operator=metal3-state \
  -n openshift-machine-api --timeout=600s

# No BMH can be created until the webhook serves.
echo "==> Waiting for the metal3 BareMetalHost webhook endpoint to be ready..."
WEBHOOK_NS="openshift-machine-api"
WEBHOOK_SVC="baremetal-operator-webhook-service"
WEBHOOK_RETRIES=60
WEBHOOK_DELAY=5
for attempt in $(seq 1 "${WEBHOOK_RETRIES}"); do
  ENDPOINT_IPS=$(oc get endpoints "${WEBHOOK_SVC}" -n "${WEBHOOK_NS}" \
    -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null || echo "")
  if [[ -n "${ENDPOINT_IPS}" ]]; then
    echo "  Webhook endpoint ready (${WEBHOOK_SVC}: ${ENDPOINT_IPS})."
    break
  fi
  if [[ "${attempt}" -eq "${WEBHOOK_RETRIES}" ]]; then
    echo "ERROR: ${WEBHOOK_SVC} in ${WEBHOOK_NS} has no ready endpoints after $((WEBHOOK_RETRIES * WEBHOOK_DELAY))s" >&2
    oc get endpoints "${WEBHOOK_SVC}" -n "${WEBHOOK_NS}" -o yaml >&2 || true
    oc get pods -n "${WEBHOOK_NS}" >&2 || true
    exit 1
  fi
  echo "    attempt ${attempt}/${WEBHOOK_RETRIES}: no endpoints yet"
  sleep "${WEBHOOK_DELAY}"
done
echo "Ironic is active and ready to accept BareMetalHosts."
