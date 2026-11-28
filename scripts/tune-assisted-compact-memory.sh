#!/usr/bin/env bash
# Reserve less memory for the three small installer-provided static pods on
# each compact master. This leaves room for the 8-GiB VMaaS resize test VM
# without changing the captured 24-GiB-per-node flavor.
set -euo pipefail

: "${KUBECONFIG:?KUBECONFIG is required}"
for node in master-0-0 master-0-1 master-0-2; do
  oc get "node/${node}" >/dev/null
  oc debug "node/${node}" -t=false -- chroot /host bash -ceu '
    for name in coredns haproxy keepalived; do
      file="/etc/kubernetes/manifests/${name}.yaml"
      test -f "$file"
      old=$(grep -c "memory: 200Mi" "$file" || true)
      new=$(grep -c "memory: 64Mi" "$file" || true)
      if [ "$old" -eq 2 ]; then
        sed -i "s/memory: 200Mi/memory: 64Mi/g" "$file"
      elif [ "$new" -ne 2 ]; then
        echo "Unexpected memory requests in ${file}" >&2
        exit 1
      fi
    done
  '

  ready=false
  for ((attempt = 1; attempt <= 60; attempt++)); do
    if oc get pod "coredns-${node}" "haproxy-${node}" "keepalived-${node}" \
        -n openshift-kni-infra -o json 2>/dev/null | jq -e '
          (.items | length) == 3 and
          ([.items[].spec.containers[].resources.requests.memory] | length) == 6 and
          ([.items[].spec.containers[].resources.requests.memory] | all(. == "64Mi")) and
          ([.items[].status.containerStatuses[]?.ready] | length) == 6 and
          ([.items[].status.containerStatuses[]?.ready] | all)
        ' >/dev/null; then
      ready=true
      break
    fi
    sleep 5
  done
  [[ "$ready" == true ]] || {
    echo "Compact infrastructure pods on ${node} did not become Ready" >&2
    exit 1
  }
  echo "Compact infrastructure pods on ${node} are Ready with 64Mi requests."
done

oc wait --for=condition=Ready nodes --all --timeout=300s
