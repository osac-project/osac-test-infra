# Explore the demo environment yourself

The environment stays running after the demo. Here is how to check everything.

## Setup

```bash
# Log into the outer cluster
export KUBECONFIG=~/.kube/poc.kubeconfig

# The two run kubeconfigs (for kubectl inside each vcluster)
DEMOA=~/.kube/demoav1.kubeconfig-local
DEMOB=~/.kube/demobv1.kubeconfig-local
```

## Two vclusters in one outer cluster

```bash
oc get ns | grep vc-
# namespace/vc-demoav1    Active
# namespace/vc-demobv1    Active
```

## Each vcluster contains ONLY OSAC pods

```bash
# Run A: only OSAC and system pods
kubectl --kubeconfig $DEMOA get pods -A

# Run B: same, but no VM-related pods
kubectl --kubeconfig $DEMOB get pods -A
```

## Shared services, separate data

```bash
# One PostgreSQL, four databases (two per run)
oc exec -n osac-infra deploy/postgres -- psql -U postgres -Atc \
  "SELECT datname FROM pg_database WHERE datname LIKE 'demo%';"

# Six KafkaUsers (three per run)
oc get kafkausers -n osac-kafka

# Two Keycloak realms
curl -sk https://keycloak-keycloak.apps.test-infra-cluster-poc.redhat.com/realms/demoav1/.well-known/openid-configuration | python3 -c "import json,sys; print(json.load(sys.stdin)['issuer'])"
curl -sk https://keycloak-keycloak.apps.test-infra-cluster-poc.redhat.com/realms/demobv1/.well-known/openid-configuration | python3 -c "import json,sys; print(json.load(sys.stdin)['issuer'])"
```

## The isolation proof

```bash
# Run A sees the VM
kubectl --kubeconfig $DEMOA get computeinstances -A

# Run B sees nothing
kubectl --kubeconfig $DEMOB get computeinstances -A

# The VM exists on the outer cluster, labeled with run A's ID
oc get vm -A --show-labels

# The owning run's VMs (the label lives on the VMI, copied from the
# VM spec template that the AAP playbook stamps with the run's
# instance ID):
oc get vmi -A -l osac.openshift.io/instance=demoav1

# Run B has no VMs:
oc get vmi -A -l osac.openshift.io/instance=demobv1
```

## Same code, same images

```bash
# Both runs use the same image digests
kubectl --kubeconfig $DEMOA get pods -n osac -o jsonpath='{range .items[*]}{.status.containerStatuses[0].imageID}{"\n"}{end}' | sort
kubectl --kubeconfig $DEMOB get pods -n osac -o jsonpath='{range .items[*]}{.status.containerStatuses[0].imageID}{"\n"}{end}' | sort
# The two lists are identical
```

## Through each run's front door (API)

```bash
# Run A's fulfillment API
curl -sk https://fulfillment-api-demoav1.apps.test-infra-cluster-poc.redhat.com/healthz

# Run B's fulfillment API
curl -sk https://fulfillment-api-demobv1.apps.test-infra-cluster-poc.redhat.com/healthz
```

## How long the environment lives

Until the next nightly cluster recycle. The outer cluster is the `poc` cluster on the `rdu` server.
