#!/usr/bin/env bash
# Put VMaaS local volumes on the compact master with enough memory for the
# suite's largest (8-GiB) VM. TopoLVM binds a disk before its VM is scheduled;
# a disk on a busier master otherwise strands that VM.
set -euo pipefail

: "${KUBECONFIG:?KUBECONFIG is required}"
: "${OSAC_NAMESPACE:?OSAC_NAMESPACE is required}"
[[ ${#OSAC_NAMESPACE} -le 63 && "${OSAC_NAMESPACE}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || {
  echo "Invalid OSAC namespace" >&2
  exit 2
}

# The E2E suite creates this tenant during session setup. Create the same CR
# earlier so the OSAC storage controller makes its StorageClass before tests.
cat <<EOF | oc apply -f -
apiVersion: osac.openshift.io/v1alpha1
kind: Tenant
metadata:
  name: tenant1
  namespace: ${OSAC_NAMESPACE}
  labels:
    osac.openshift.io/tenant-id: tenant1
    osac.openshift.io/tenant-uuid: tenant1
spec: {}
EOF

for ((attempt = 1; attempt <= 120; attempt++)); do
  if oc get storageclass osac-tenant1-local >/dev/null 2>&1; then
    break
  fi
  sleep 5
done
oc get storageclass osac-tenant1-local >/dev/null

node=$(python3 - <<'PY'
import json
import re
import subprocess
from decimal import Decimal


def quantity(value):
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti|k|M|G|T)?", value)
    if not match:
        raise ValueError(f"Unexpected memory quantity: {value}")
    amount = Decimal(match.group(1))
    unit = match.group(2)
    scale = {None: 1, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3,
             "Ti": 1024**4, "k": 1000, "M": 1000**2, "G": 1000**3,
             "T": 1000**4}[unit]
    return int(amount * scale)


nodes = json.loads(subprocess.check_output(["oc", "get", "nodes", "-o", "json"]))["items"]
choices = []
for node in nodes:
    name = node["metadata"]["name"]
    if not re.fullmatch(r"master-0-[0-2]", name):
        continue
    if not any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in node["status"].get("conditions", [])):
        continue
    allocatable = quantity(node["status"]["allocatable"]["memory"])
    description = subprocess.check_output(["oc", "describe", "node", name], text=True)
    allocated = description.split("Allocated resources:", 1)[1]
    request = re.search(r"(?m)^\s*memory\s+(\S+)\s+\(", allocated)
    if not request:
        raise SystemExit(f"Could not read memory requests for {name}")
    free = allocatable - quantity(request.group(1))
    choices.append((free, name))

if len(choices) != 3:
    raise SystemExit("Expected three Ready compact masters")
best_free, best_node = max(choices)
# virt-launcher asks for 8500Mi. Keep a small margin for storage jobs.
minimum = quantity("8628Mi")
if best_free < minimum:
    detail = ", ".join(f"{name}={free // 1024**2}Mi" for free, name in sorted(choices))
    raise SystemExit(f"No master has 8628Mi free requested memory ({detail})")
print(best_node)
PY
)

oc patch storageclass osac-tenant1-local --type=merge -p "$(printf \
  '{"allowedTopologies":[{"matchLabelExpressions":[{"key":"topology.topolvm.io/node","values":["%s"]}]}]}' \
  "$node")"
echo "Pinned tenant1 local volumes to ${node} for the serial compact E2E run."
