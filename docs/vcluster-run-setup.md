# vcluster run setup

How a VMaaS E2E run gets its own virtual cluster on a shared outer cluster,
and how to fix the failures the POC hit.

The setup script is `scripts/vcluster-run-setup.sh`. The vcluster Helm values
template is `scripts/vcluster-values.yaml.template`. Everything below was
verified on a live POC on 2026-11-01 against vcluster 0.36.1 on an OpenShift
SNO running the shared infrastructure (`make install-infra PLATFORM=openshift
PROFILE=vmaas-ci NS=osac` from the osac monorepo). The full design is the
"OSAC E2E on shared infrastructure: one vcluster per test run" design
document (osac-shared-infra-e2e-design.md).

## The flow end to end

One outer cluster per CI machine holds the infrastructure: Keycloak, Strimzi
Kafka, bundled PostgreSQL, OpenBao, AAP. Each test run gets a vcluster that
holds OSAC. The script:

1. **Checks preconditions** against the outer cluster and fails loudly:
   `vcluster`, `helm` and `kubectl` installed, `KUBECONFIG` set and reachable,
   no existing Helm release for the run (unless `--recreate`), the run
   namespace's UID range readable, the outer DNS service IP and ingress
   domain readable, the Strimzi Kafka cluster present, the CI certificate
   authority present, and every outer-cluster secret the run copies present
   with the right keys.
2. **Creates namespace `vc-<run name>`** and renders
   `scripts/vcluster-values.yaml.template` with the namespace's allocated
   UID, the vcluster API service DNS name, and the outer DNS service IP.
3. **Installs the vcluster Helm release** (chart `vcluster` from
   `https://charts.loft.sh`, version pinned to `0.36.1` in the script) and
   waits for the control plane pod.
4. **Writes a kubeconfig** for the vcluster with `vcluster connect
   --service-account`: a service account bound to `cluster-admin` inside the
   vcluster, with a one-year (8760h) token. The kubeconfig's server is the
   vcluster API service, `https://<run>.vc-<run>.svc.cluster.local:443`.
5. **Creates the `osac` and `osac-kafka` namespaces** inside the vcluster.
6. **Installs cert-manager inside the vcluster** (pinned exactly as
   osac-installer's Makefile does) and copies the outer cluster's CI
   certificate authority, secret `default-ca` from namespace
   `cert-manager`, into the vcluster, exposed as ClusterIssuer `default-ca`.
   The osac chart issues the database client certificates itself, with this
   CA, so the run setup provides only the issuer — not the certificates.
7. **Creates the KafkaUsers in the outer cluster's `osac-kafka` namespace**
   (the Strimzi objects the osac and osac-metering charts normally create
   in-cluster): `fulfillment-service`, `osac-metering` and
   `osac-metering-echo-adapter`. It waits for Strimzi to generate each
   user's secret, then copies the fulfillment credentials into the vcluster
   as `fulfillment-service-kafka` (keys `brokers`, `user`, `password`, the
   shape the chart's `service.kafka.connection` expects) and mirrors the
   metering secrets into the vcluster's `osac-kafka` namespace under their
   natural names.
8. **Copies the outer-cluster secrets** the OSAC chart expects to find in the
   vcluster's `osac` namespace (table below).
9. **Prints the `make install-osac` command** for the run and the kubeconfig
   path.

Run it like this:

```bash
KUBECONFIG=/path/to/outer-kubeconfig ./scripts/vcluster-run-setup.sh pr2100v1
```

Files land in `.vcluster-runs/<run name>/`: `kubeconfig` (the vcluster
kubeconfig) and `vcluster-values.yaml` (the rendered values, for debugging).

The printed command is:

```bash
KUBECONFIG=<repo>/.vcluster-runs/<run>/kubeconfig \
CLUSTER_DOMAIN=<outer domain> \
AAP_LICENSE_FILE=/path/to/license.zip \
make install-osac PLATFORM=openshift PROFILE=vcluster-ci NS=osac \
    EXTRA_HELM_ARGS="--set service.externalHostname=fulfillment-api-osac.<outer domain> \
--set service.internalHostname=fulfillment-internal-api-osac.<outer domain> \
--set ui.auth.oidcIssuerURL=https://keycloak-keycloak.<outer domain>/realms/osac \
--set ui.externalHostname=osac-ui-osac.<outer domain> \
--set aap.controllerUrl=<outer AAP route> \
--set aap.bootstrap.adminPasswordSecret.name=osac-aap-admin-password"
```

Run it from the osac monorepo's `osac-installer` directory. The vcluster has
no OpenShift ingress config for the Makefile to read, so `CLUSTER_DOMAIN`
(the Makefile override landing with the osac chart package) carries the
domain and the route hostnames are passed through `EXTRA_HELM_ARGS`, the
same settings `make install-osac` derives for a normal install.
`AAP_LICENSE_FILE` is required for `PLATFORM=openshift`. The AAP URL set is
`aap.controllerUrl`: the umbrella aliases the osac-aap dependency as `aap`,
so the subchart's top-level `controllerUrl` is `aap.controllerUrl`, and
`aap.aap.controllerUrl` would land in a block nothing reads. In external
mode the chart reads the admin password from
`aap.bootstrap.adminPasswordSecret.name`, whose default is the fixed
`osac-aap-admin-password`; the run setup copies the secret under
`<aap-instance>-admin-password`, so that set must always be passed
explicitly. The `vcluster-ci` profile and the chart's vcluster support
(`service.variant`, `preflight.skipChecks`, external Kafka and AAP wiring)
also land with the osac chart work package; until then they do not exist.

## Why Kafka objects go through the run setup

The osac-metering chart fetches its Kafka SASL credentials through the LOCAL
API of whatever cluster OSAC runs in: its Deployments run initContainers
that read Secrets from `metering.kafka.clusterNamespace` (default
`osac-kafka`), and the chart creates a Role in that namespace. A vcluster
has no `osac-kafka` namespace and no Strimzi, so the vcluster-ci profile
sets `metering.kafka.strimziResources=false` and the run setup owns the
Kafka objects instead: it creates the KafkaUsers in the outer cluster's real
`osac-kafka`, and mirrors the generated secrets into an `osac-kafka`
namespace inside the vcluster so the initContainers find them unchanged.
This is the design's placement rule in action: the vcluster holds OSAC and
what OSAC owns; the outer cluster holds the infrastructure and every object
OSAC asks the infrastructure to create.

## What gets copied where

| Outer cluster | Namespace | Keys | Vcluster destination | Why |
| --- | --- | --- | --- | --- |
| Secret `default-ca` | `cert-manager` | `tls.crt`, `tls.key`, `ca.crt` | Secret of the same name in `cert-manager`, exposed as ClusterIssuer `default-ca` | The CI certificate authority. The osac chart issues the database client certificates (`osac-db-client-cert`, `osac-db-metering-client-cert`) and the db-init certificate itself, with this CA, so the run setup provides the issuer rather than the certificates |
| Strimzi secret `fulfillment-service` | `osac-kafka` | `password` | Secret `fulfillment-service-kafka` (`brokers`, `user`, `password`) in `osac` | Kafka connection for the fulfillment service |
| Strimzi secrets `osac-metering`, `osac-metering-echo-adapter` | `osac-kafka` | all keys | Secrets of the same names in an `osac-kafka` namespace | SASL credentials the metering pods' initContainers read by local API |
| Secret `<aap-instance>-admin-password` | AAP namespace | `password` | Secret of the same name | Admin password for the osac-aap subchart's external-instance mode |
| Secret `fulfillment-controller-credentials` | `osac` | `client-id`, `client-secret` | Secret of the same name | Keycloak client credentials (`service.auth.controllerCredentials`, `service.idp.credentials`) |
| Secret `osac-ui-backend-credentials` | `osac` | `client-id`, `client-secret` | Secret of the same name | Backend API credentials for the UI pod, created by the osac-infra chart's hook in the outer cluster |
| ConfigMap `ca-bundle` | `osac` | `bundle.pem` | ConfigMap of the same name | Shared CA bundle (`service.certs.caBundle.configMap`), includes the Kafka listener CA |

The database connection secrets (`osac-db-config`,
`osac-db-metering-config`) and client certificates are NOT copied: the osac
chart creates them itself in the vcluster, pointing at
`postgres.osac-infra.svc.cluster.local` (reachable through the forwarded
CoreDNS zone) and issuing the certificates from the copied CA. The AAP
instance defaults to `osac-aap` in namespace `osac`; override with
`--aap-instance` and `--aap-namespace` if the outer cluster places it
elsewhere. The AAP controller URL comes from the OpenShift route named after
the AnsibleAutomationPlatform CR, or from `--aap-controller-url`.

## Troubleshooting (POC-verified failure modes)

### The control plane pod is rejected by the restricted-v2 SCC

Symptom: the vcluster pod never starts; `oc describe pod` in `vc-<run>` shows
SCC admission errors about the security context.

The chart's default security context runs as UID 0, which restricted-v2
rejects. The vcluster docs suggest a fixed UID (12345); that only works where
12345 happens to fall inside the namespace's allocated UID range. An
explicitly set `runAsUser`/`fsGroup` outside the range is rejected, because
OpenShift does not override UIDs the manifest already set.

Fix (already in the values template): the setup script reads the run
namespace's `openshift.io/sa.scc.uid-range` annotation (format
`1000880000/10000`) and renders the range's first UID into
`controlPlane.statefulSet.security.podSecurityContext.fsGroup` and
`containerSecurityContext.runAsUser`.

### The syncer hangs logging "waiting for DNS service IP"

Cause: CoreDNS was customized with `controlPlane.coredns.overwriteManifests`.
That knob replaces the whole CoreDNS manifest set the syncer generates
(ServiceAccount, ClusterRole, Deployment, ConfigMap and the `kube-dns`
Service). A hand-written replacement leaves out the `kube-dns` Service, so it
never appears and the syncer waits forever. Verified failure in the POC.

Fix (already in the values template): customize DNS with
`controlPlane.coredns.overwriteConfig`, which replaces only the Corefile
inside the generated manifest set.

### DNS inside the vcluster does not resolve outer services

Two separate requirements, both in the values template:

- Every CoreDNS server block must bind port **1053**, not 53. The coredns
  pod syncs to the host namespace and runs as a non-root UID, so it cannot
  bind a privileged port. The `kube-dns` Service maps 53 to 1053.
- The outer service zones must be forwarded explicitly:
  `osac-infra.svc.cluster.local` and `osac-kafka.svc.cluster.local` blocks
  that `forward` to the ClusterIP of `dns-default.openshift-dns`. Without
  them the `kubernetes` plugin answers authoritatively for all of
  `cluster.local` and returns NXDOMAIN for the outer namespaces, because
  they do not exist inside the vcluster.

The setup script reads the `dns-default` ClusterIP at setup time and renders
it into the Corefile.

### The AAP controller cannot reach the vcluster API

The vcluster proxy certificate must include the API service DNS name
(`<run>.vc-<run>.svc.cluster.local`) in `controlPlane.proxy.extraSANs`;
otherwise TLS verification fails for in-cluster clients such as the AAP
controller launching job pods inside the vcluster. Already in the values
template.

### `kubectl --kubeconfig <run>/kubeconfig` fails to resolve the server

The kubeconfig's server is
`https://<run>.vc-<run>.svc.cluster.local:443`, which resolves only through
the outer cluster's DNS. It works from inside the outer cluster (AAP job
pods, test pods) and from machines that use the outer cluster's DNS. The
run-route work package will publish an externally reachable vcluster API
route; until then, run the setup and `make install-osac` from a machine that
can resolve outer-cluster service names.

### Re-running a run

`--recreate` uninstalls the Helm release and reinstalls it. The chart's
persistent volume claim uses the `Retain` policy, so a recreated vcluster
reuses the previous claim and its data; delete the claim in `vc-<run>`
manually if a clean slate is needed.

## Known gaps in v0

- Single run only. Nothing is prefixed with a run ID yet: the KafkaUsers are
  always `fulfillment-service`, `osac-metering` and
  `osac-metering-echo-adapter`, and the databases are always the shared
  PostgreSQL's `service` and `metering` ones. Two runs on one outer cluster
  collide. Run-ID prefixing is the next work package.
- No teardown script yet.
- The AAP bootstrap still applies the license manifest on every run; the
  follow-up to skip it is tracked in OSAC-7084.
- `make install-osac` against a vcluster still needs the parallel chart work:
  the `vcluster-ci` profile, the `CLUSTER_DOMAIN` Makefile override, and the
  preflight skips (`db-init-host-endpoints`, `default-storageclass`). Until
  those land, the Makefile hard-fails against a vcluster where it runs `oc
  get ingresses.config/cluster`. Neither `kafka-access` nor the OpenShift
  version check needs skipping: with `kafka.enabled=false` the kafka-access
  check verifies the `service.kafka.connection` secrets this script creates,
  and `ocp-version` is a non-blocking warning.
