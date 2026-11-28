# vcluster run setup

How an OSAC E2E run gets its own virtual cluster on a shared outer cluster,
how every shared-service object is named after the run, and how to fix the
failures the POC hit.

The setup script is `scripts/vcluster-run-setup.sh`, the teardown script is
`scripts/vcluster-run-teardown.sh`, and the vcluster Helm values template is
`scripts/vcluster-values.yaml.template`. Everything below was verified on a
live POC on 2026-11-01 against vcluster 0.36.1 on an OpenShift SNO running
the shared infrastructure (`make install-infra PLATFORM=openshift
PROFILE=vmaas-ci NS=osac` from the osac monorepo). The full design is the
"OSAC E2E on shared infrastructure: one vcluster per test run" design
document (osac-shared-infra-e2e-design.md).

## The flow end to end

One outer cluster per CI machine holds the infrastructure: Keycloak, Strimzi
Kafka, bundled PostgreSQL, OpenBao, AAP. Each test run gets a vcluster that
holds OSAC, and its own share of every shared service, named after the run.
The setup script:

1. **Checks preconditions** against the outer cluster and fails loudly:
   `vcluster`, `helm`, `kubectl`, `curl`, `python3` and `openssl` installed,
   `KUBECONFIG` set and reachable, the PR's `realm.json` passed and valid
   JSON, no existing Helm release for the run (unless `--recreate`), the run
   namespace's UID range readable, the outer DNS service IP and ingress
   domain readable, the Strimzi Kafka cluster present, the CI certificate
   authority present, the Keycloak admin credentials secret present with
   its three keys, the Keycloak service reachable with that CA, the AAP
   admin-password secret and the shared AAP's route present (or
   `--aap-controller-url` given), and the shared CA bundle present.
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
7. **Creates the run's KafkaUsers in the outer cluster's `osac-kafka`
   namespace** (the Strimzi objects the osac and osac-metering charts
   normally create in-cluster), with run-prefixed names and ACL prefixes:
   `<run>-fulfillment-service`, `<run>-osac-metering` and
   `<run>-osac-metering-echo-adapter`, ACLs on `<run>.events.*` topics,
   `<run>.system.*` consumer groups and `<run>.metering.*` topics. It waits
   for Strimzi to generate each user's secret, then copies the fulfillment
   credentials into the vcluster as `<run>-fulfillment-service-kafka` (keys
   `brokers`, `user`, `password`, the shape the chart's
   `service.kafka.connection` expects) and mirrors the metering secrets
   into the vcluster's `osac-kafka` namespace under their run-prefixed
   names.
8. **Imports the run's Keycloak realm** `<run>` through the outer
   Keycloak's admin API, from the PR's `realm.json` (see the next section).
9. **Creates the AAP service account** `<run>-aap` with `cluster-admin` in
   the vcluster and stores its one-year token as secret
   `<run>-aap-vcluster-token` (key `token`) in the vcluster's `osac`
   namespace, so the shared AAP can launch the run's job pods inside the
   vcluster.
10. **Copies the AAP admin password and the shared CA bundle** into the
    vcluster (table below).
11. **Creates the orders namespace `osac` on the outer cluster when
    absent** (OSAC-8054): remote-mode ClusterOrders need their namespace
    there. A pre-existing namespace is reused untouched and never deleted
    by the teardown; a namespace the run created is labelled
    `osac.openshift.io/instance=<run>` and deleted by the run's teardown.
12. **Writes the per-run values overlay** `.vcluster-runs/<run>/
    instance-values.yaml` -- the run's ID plus the suite's service
    toggles selected by the run name's suite letter (see "The run's
    suite") -- and **prints the `make install-osac` command** for the run,
    plus the kubeconfig path.

Run it like this (the realm source is the PR's own monorepo checkout, so a
PR that changes `realm.json` is tested like any other change):

```bash
KUBECONFIG=/path/to/outer-kubeconfig \
    ./scripts/vcluster-run-setup.sh pr2100v1 \
    --realm-json /path/to/osac/osac-installer/charts/osac-infra/files/realm.json
```

Files land in `.vcluster-runs/<run name>/`: `kubeconfig` (the vcluster
kubeconfig), `vcluster-values.yaml` and `instance-values.yaml` (the rendered
values, for debugging). The rendered realm JSON with its client secrets
stays in the setup script's temp directory and is deleted on exit.

## What is named after the run

Everything the run touches in a shared service carries the run name, so two
runs on one outer cluster do not collide:

| Thing | Name in run `pr2100v1` | Lives in |
| --- | --- | --- |
| vcluster | `pr2100v1`, in namespace `vc-pr2100v1` | outer cluster |
| OSAC namespace | `osac` | vcluster |
| Orders namespace | `osac` — created when absent, labelled `osac.openshift.io/instance=<run>` (OSAC-8054); the teardown deletes it only when the run created it | outer cluster |
| Bare-metal hosts namespace (b/c suites) | `pr2100b1-hosts`, passed as `bmf.metal3.namespace` | outer cluster |
| Kafka users | `pr2100v1-fulfillment-service`, `pr2100v1-osac-metering`, `pr2100v1-osac-metering-echo-adapter` | outer cluster |
| Kafka topics (ACLs) | `pr2100v1.events.*`, `pr2100v1.metering.heartbeat.*`, `pr2100v1.metering.dlq.*` | outer cluster |
| Kafka consumer groups (ACLs) | `pr2100v1.system.*`, `pr2100v1-osac-metering-echo-adapter` | outer cluster |
| Fulfillment Kafka connection secret | `pr2100v1-fulfillment-service-kafka` | vcluster |
| Keycloak realm | `pr2100v1` | outer cluster |
| Keycloak client credentials | `pr2100v1-fulfillment-controller-credentials`, `pr2100v1-osac-ui-backend-credentials` | vcluster |
| Databases (chart-derived, see below) | `pr2100v1_service`, `pr2100v1_metering`; secrets `pr2100v1-db-config`, `pr2100v1-db-client-cert`, `pr2100v1-db-metering-config`, `pr2100v1-db-metering-client-cert` | outer cluster / vcluster |
| AAP service account and token secret | `pr2100v1-aap`, `pr2100v1-aap-vcluster-token` | vcluster |
| AAP job template prefix | `pr2100v1` (operator and bare-metal operator), via `operator.aap.templatePrefix`, `bmf.env.aapTemplatePrefix` and `global.instanceId` | vcluster |
| AAP organization | `pr2100v1` (derived by the chart from `global.instanceId`) | outer cluster |
| OpenBao namespace | `pr2100v1` (via `service.vault.namespace`) | outer cluster |
| Run routes (not created yet) | `fulfillment-api-pr2100v1.<domain>`, `fulfillment-internal-api-pr2100v1.<domain>`, `osac-ui-pr2100v1.<domain>` | outer cluster |

## The run's suite

The run name's last letter before the attempt digits is the suite letter:
`v` for VMaaS (`pr2100v1`), `b` for BMaaS (`pr2100b2`), `c` for CaaS
(`nightlyc1`). The setup fails loudly when a run name carries none. The
letter selects the suite's service toggles in the per-run values overlay,
with the keys documented in the vcluster-ci profile
(`osac-installer/values/vcluster-ci/instance.yaml`, "Suite selection");
the BMaaS and CaaS suites additionally get the BMF operator wiring (the
Metal3 namespace `<run>-hosts`, the shared AAP's URL for the BMF
operator, the run's job template prefix and credential secret), and CaaS
gets the hosted-cluster settings and the cluster versions its orders
reference. Without the suite keys the profile renders VMaaS; the VMaaS
overlay only restates that default.

## The Keycloak realm

The run's realm is created through the outer Keycloak's admin API, not by
the Keycloak server's startup import (the outer cluster's own realm is
already imported; the server only imports at boot):

1. The admin credentials come from the outer cluster's
   `keycloak-admin-credentials` secret in namespace `keycloak` (keys
   `admin-username`, `admin-password`, `default-user-password`) — the same
   secret the osac-infra chart feeds its own realm hooks.
2. The PR's `realm.json` is copied with the realm renamed to the run and
   the top-level `id` dropped (Keycloak assigns its own; an id that already
   belongs to another realm on the same server is rejected). The rename
   goes through python3's json module so only the top-level realm name
   changes; client ids, roles and groups keep the PR's names.
3. The seven placeholders are resolved exactly as
   `osac-installer/charts/osac-infra/files/hooks/resolve-realm-secrets.sh`
   resolves them, escape handling copied verbatim: the four client secrets
   (`osac-controller`, `osac-admin`, `osac-ui-backend`, `osac-csi-driver`)
   are generated per run with `openssl rand -base64 18` and substituted
   raw; the realm admin username and password get the two escaping passes
   (full JSON string escaping, then sed replacement escaping of backslash,
   ampersand and `#`); the UI URL is the run's own
   `https://osac-ui-<run>.<domain>`.
4. The realm is POSTed to `/admin/realms` with a master-realm admin token
   (the same `admin-cli` password grant the keycloak-set-passwords hook
   uses), and the script waits until the realm answers its public
   discovery endpoint.
5. Every fixture user's password is set to the outer cluster's
   `default-user-password` through `/users/<id>/reset-password`, mirroring
   the keycloak-set-passwords hook: the realm import brings the users
   without credentials, and the e2e suite logs in as them by password
   (`OSAC_JWT_PASSWORD`). The user list is derived from the rendered realm,
   so a PR that adds a fixture user gets its password set too; the lookup
   uses `exact=true` (the hook's lookup matches substrings, which would
   answer `tenant1_admin` for `tenant1`).
6. The controller and UI backend client secrets are written into the
   vcluster as `<run>-fulfillment-controller-credentials` and
   `<run>-osac-ui-backend-credentials` (keys `client-id`, `client-secret`),
   the shape the osac-infra chart's create-controller-credentials and
   create-ui-backend-credentials hooks create in a normal install. Unlike
   v0, which copied the outer realm's shared credentials, each run's realm
   has its own.

The script talks to the Keycloak service URL
(`https://keycloak.keycloak.svc.cluster.local`, the default of
`--keycloak-url`) with the CI certificate authority: the service
certificate's dnsNames include that name and the CA that signed it is the
one the script already copies. The keycloak zone is not forwarded into the
vcluster, so OSAC itself keeps using the route URL
(`https://keycloak-keycloak.<domain>/realms/<run>`) — that is what the
overlay's issuer URLs carry.

### What runs after the realm import

The osac-infra chart configures its realm with a post-install hook
(keycloak-set-passwords). The run setup mirrors the steps the fulfillment
service needs at runtime, against the run's realm:

- **The fixture users' passwords** (step 5 above).
- **The controller client's realm-management roles** (
  `grant_controller_realm_management_roles`, the hook's
  `ensure_controller_idp_roles`): the `osac-controller` client's
  service-account user receives `create-client` and `view-clients`, and
  `manage-clients` is revoked. The fulfillment service manages
  organizations and users through the admin API with that client
  (`service.idp.credentials`).
- **The authenticated client-registration policies** (
  `ensure_service_account_registration_policies`, same name in the hook):
  the `allowed-protocol-mappers` and `allowed-client-templates` policies
  gain the protocol mappers (`oidc-hardcoded-claim-mapper`,
  `oidc-audience-mapper`, `oidc-usersessionmodel-note-mapper`) and the
  `service_account` scope the fulfillment service's dynamically registered
  tenant service-account clients carry (it registers them through
  `/realms/<run>/clients-registrations/default`).
- **The registration-token reuse policy** (
  `ensure_service_account_registration_token_policy`, same name in the
  hook): a client profile that disables registration-access-token
  rotation, bound by a policy to clients updated by registration token,
  so the service can recover an ambiguous registration by retrying.

The hook's two MCP steps are deliberately not mirrored; see the known
gaps.

## The AAP service account

The shared AAP launches each run's job pods inside that run's vcluster. For
that it needs credentials for the vcluster API: the setup creates service
account `<run>-aap` in the vcluster's `osac` namespace, binds it to
`cluster-admin` there, mints a one-year (8760h) token with
`kubectl create token`, and stores it as secret
`<run>-aap-vcluster-token` (key `token`) in the same namespace. The
overlay's `aap.vcluster.tokenSecret` names the secret and
`aap.vcluster.apiServerUrl` names the vcluster API service URL
(`https://<run>.vc-<run>.svc.cluster.local`), which the AAP controller
resolves from inside the outer cluster (POC-verified).

The run's AAP organization (`<run>`) and job templates are created by the
chart's bootstrap job against the shared AAP, derived from
`global.instanceId`; the run setup only copies the AAP admin password
secret into the vcluster so those jobs can log in. The chart's
external-instance mode reads the shared AAP's controller URL from
`aap.controllerUrl` and the admin password from
`aap.bootstrap.adminPasswordSecret` (the run setup copies the outer
cluster's `<aap-instance>-admin-password` into the vcluster under that
name, and the printed make command passes both values). The osac-aap chart
derives the in-cluster secret name from `aap.aap.instance.name`; with the
instance disabled the external values apply, both from the unified osac
branch (PR osac#2566).

## The per-run values overlay

The setup writes `.vcluster-runs/<run>/instance-values.yaml`, carrying only
what is named after the run (everything every vcluster run shares stays in
the `vcluster-ci` profile):

```yaml
global:
  instanceId: pr2100v1
  eventTopicPrefix: "pr2100v1.events."
service:
  auth:
    issuerUrl: https://keycloak-keycloak.<outer domain>/realms/pr2100v1
    controllerCredentials:
      - secret:
          name: pr2100v1-fulfillment-controller-credentials
          items:
            - key: client-id
              param: client-id
            - key: client-secret
              param: client-secret
  idp:
    credentials:
      - secret:
          name: pr2100v1-fulfillment-controller-credentials
          items: [same shape as controllerCredentials]
  database:
    connection:
      - secret:
          name: pr2100v1-db-config
          items: [{key: url, param: url}]
      - secret:
          name: pr2100v1-db-client-cert
          items: [{key: tls.crt, param: sslcert}, {key: tls.key, param: sslkey}, {key: ca.crt, param: sslrootcert}]
  kafka:
    connection:
      - secret:
          name: pr2100v1-fulfillment-service-kafka
          items: [{key: brokers, param: brokers}, {key: user, param: user}, {key: password, param: password}]
  vault:
    namespace: pr2100v1
    keycloakIssuerUrl: https://keycloak-keycloak.<outer domain>/realms/pr2100v1
metering:
  database:
    connection:
      - secret:
          name: pr2100v1-db-metering-config
          items: [{key: url, param: url}]
      - secret:
          name: pr2100v1-db-metering-client-cert
          items: [{key: tls.crt, param: sslcert}, {key: tls.key, param: sslkey}, {key: ca.crt, param: sslrootcert}]
ui:
  auth:
    oidcIssuerURL: https://keycloak-keycloak.<outer domain>/realms/pr2100v1
    serviceAccount:
      credentialsSecret: pr2100v1-osac-ui-backend-credentials
operator:
  fulfillment:
    credentials:
      secretName: pr2100v1-fulfillment-controller-credentials
  aap:
    templatePrefix: pr2100v1
aap:
  vcluster:
    apiServerUrl: https://pr2100v1.vc-pr2100v1.svc.cluster.local
    tokenSecret:
      name: pr2100v1-aap-vcluster-token
      key: token
```

`aap.organization` and `aap.templatePrefix` are both set to the run name:
the operator chart defaults each to `osac` and derives nothing, so without
these keys every run would launch its AAP jobs in the shared outer
organization under the `osac-` prefix. The operator's credential
reference overrides `operator.fulfillment.credentials.secretName` (the
vmaas-ci and vcluster-ci profiles name the unprefixed
`fulfillment-controller-credentials`; the run's operator must read the
run's own client, and the `client-id`/`client-secret` keys already match).

The printed command is:

```bash
KUBECONFIG=<repo>/.vcluster-runs/<run>/kubeconfig \
CLUSTER_DOMAIN=<outer domain> \
AAP_LICENSE_FILE=/path/to/license.zip \
make install-osac PLATFORM=openshift PROFILE=vcluster-ci NS=osac \
    EXTRA_HELM_ARGS="--values <repo>/.vcluster-runs/<run>/instance-values.yaml \
--set service.externalHostname=fulfillment-api-<run>.<outer domain> \
--set service.internalHostname=fulfillment-internal-api-<run>.<outer domain> \
--set ui.externalHostname=osac-ui-<run>.<outer domain> \
--set aap.controllerUrl=<shared AAP controller URL> \
--set aap.bootstrap.adminPasswordSecret.name=osac-aap-admin-password"
```

Run it from the osac monorepo's `osac-installer` directory. The vcluster has
no OpenShift ingress config for the Makefile to read, so `CLUSTER_DOMAIN`
(the Makefile override landing with the osac chart package) carries the
domain and the run-prefixed route hostnames are passed through
`EXTRA_HELM_ARGS`, the same settings `make install-osac` derives for a
normal install. The AAP URL is the shared AAP's controller URL, resolved
from the OpenShift route named after the AnsibleAutomationPlatform CR
(`--aap-controller-url` overrides), and
`aap.bootstrap.adminPasswordSecret.name` names the admin-password secret
the run setup copied into the vcluster (the key defaults to `password`).
Run the suite with `OSAC_KEYCLOAK_REALM=<run>` so the JWT fixtures use the
run's realm.

**Dependency on the unified osac branch (PR osac#2566).** `global.instanceId`,
`aap.vcluster.*`, `aap.controllerUrl` and
`aap.bootstrap.adminPasswordSecret` do not exist on osac main; they live on
the unified osac branch `vcluster-e2e` (PR osac#2566), and the umbrella
schema rejects unknown keys, so installing with the overlay and the AAP
flags requires that branch. The same branch derives, from `instanceId`, the
database secret names the overlay references (`<run>-db-config` and
friends, today the fixed prefix `osac`) and the metering Kafka secret names
(`<run>-osac-metering` and `<run>-osac-metering-echo-adapter`, today the
fixed names `osac-metering` and `osac-metering-echo-adapter`). Until it
merges, the profile, the `CLUSTER_DOMAIN` override and the schema keys do
not exist on main.

## Why Kafka objects go through the run setup

The osac-metering chart fetches its Kafka SASL credentials through the LOCAL
API of whatever cluster OSAC runs in: its Deployments run initContainers
that read Secrets from `metering.kafka.clusterNamespace` (default
`osac-kafka`), and the chart creates a Role in that namespace. A vcluster
has no `osac-kafka` namespace and no Strimzi, so the run setup owns the
Kafka objects instead: it creates the run's KafkaUsers in the outer
cluster's real `osac-kafka`, and mirrors the generated secrets into an
`osac-kafka` namespace inside the vcluster so the initContainers find them
by local API. This is the design's placement rule in action: the vcluster
holds OSAC and what OSAC owns; the outer cluster holds the infrastructure
and every object OSAC asks the infrastructure to create.

The ACL shapes mirror the monorepo's chart templates, prefixed by the run:
the fulfillment user gets `<run>.events.` topics (create, write, read,
describe, describe-configs), `<run>.system.` consumer groups (read,
describe) and idempotent writes; the metering user reads `<run>.events.`
and owns `<run>.metering.heartbeat.` and `<run>.metering.dlq.` topics; the
echo adapter reads the heartbeat and inference topics and writes the
dead-letter ones. On current osac main the fulfillment topic ACL already
follows `global.eventTopicPrefix`; the consumer-group and metering topic
prefixes are constants in the services there, and the services' own
prefixes follow with the parallel instanceId work.

## What gets copied where

| Outer cluster | Namespace | Keys | Vcluster destination | Why |
| --- | --- | --- | --- | --- |
| Secret `default-ca` | `cert-manager` | `tls.crt`, `tls.key`, `ca.crt` | Secret of the same name in `cert-manager`, exposed as ClusterIssuer `default-ca` | The CI certificate authority. The osac chart issues the database client certificates and the db-init certificate itself, with this CA, so the run setup provides the issuer rather than the certificates |
| Strimzi secret `<run>-fulfillment-service` | `osac-kafka` | `password` | Secret `<run>-fulfillment-service-kafka` (`brokers`, `user`, `password`) in `osac` | Kafka connection for the fulfillment service |
| Strimzi secrets `<run>-osac-metering`, `<run>-osac-metering-echo-adapter` | `osac-kafka` | all keys | Secrets of the same names in an `osac-kafka` namespace | SASL credentials the metering pods' initContainers read by local API |
| Keycloak client secrets of the run's realm | — (generated) | — | Secrets `<run>-fulfillment-controller-credentials`, `<run>-osac-ui-backend-credentials` (`client-id`, `client-secret`) in `osac` | The run's own realm's `osac-controller` and `osac-ui-backend` clients (`service.auth.controllerCredentials`, `service.idp.credentials`, `ui.auth.serviceAccount.credentialsSecret`) |
| Secret `<aap-instance>-admin-password` | AAP namespace | `password` | Secret of the same name | Admin password for the osac-aap subchart's bootstrap and token jobs, which read `<aap.aap.instance.name>-admin-password` |
| Service account `<run>-aap` token | — (minted) | — | Secret `<run>-aap-vcluster-token` (`token`) in `osac` | The AAP controller's credentials for the vcluster API (`aap.vcluster.tokenSecret`) |
| ConfigMap `ca-bundle` | `osac` | `bundle.pem` | ConfigMap of the same name | Shared CA bundle (`service.certs.caBundle.configMap`), includes the Kafka listener CA |

The database connection secrets and client certificates are NOT copied: the
osac chart creates them itself in the vcluster, pointing at
`postgres.osac-infra.svc.cluster.local` (reachable through the forwarded
CoreDNS zone) and issuing the certificates from the copied CA, named after
the instanceId-derived prefix. The AAP instance defaults to `osac-aap` in
namespace `osac`; override with `--aap-instance` and `--aap-namespace` if
the outer cluster places it elsewhere. The AAP controller URL comes from
the OpenShift route named after the AnsibleAutomationPlatform CR, or from
`--aap-controller-url`.

## Teardown

After CI reports the result, remove the run with:

```bash
KUBECONFIG=/path/to/outer-kubeconfig ./scripts/vcluster-run-teardown.sh pr2100v1
```

The teardown deletes, in order:

1. the run's AAP schedules (three, named `<run>-sync`,
   `<run>-config-as-code`, `<run>-publish-templates`) through the shared
   AAP's API, so a torn-down run's organization stops firing error jobs
   every ten minutes; the organization itself is left in place (see the
   known gaps below); failures are warnings, never fatal;
2. the run's vcluster Helm release — before the namespace deletion, so helm
   itself removes the chart's cluster-scoped RBAC (a ClusterRole and
   ClusterRoleBinding the syncer needs), which deleting only the namespace
   would leave behind;
3. namespace `vc-<run>` (its deletion removes the vcluster's pods and every
   object the syncer copied there, which keep their inner names and so
   cannot be found by a name-anchored scan), waiting until it is gone;
4. the run's virtual BMHs for b/c suites (VM group, BMHs with finalizer
   stripping, sushy emulator, storage pool, cache, the `<run>-hosts`
   namespace and its RBAC), best-effort; leftover hosts fail the run's
   leftover verification instead of passing silently;
5. the orders namespace `osac` on the outer cluster, but only when this run
   created it: the instance label the setup wrote at creation carries the
   run's name, and a namespace without this run's label — a pre-existing
   one, or one another run created — is left in place;
6. the run's outer KafkaUsers, then their Strimzi secrets (the users go
   first: while a KafkaUser exists, the Strimzi user operator recreates its
   secret), waiting until each is gone;
7. the run's Keycloak realm through the admin API;
8. the run's output files (`.vcluster-runs/<run>` by default; pass
   `--output-dir` if the setup was given one).

It then verifies that nothing named after the run remains among the kinds
the run setup and the vcluster create — namespaces, KafkaUsers, secrets,
cluster roles and bindings, Helm releases, and the Keycloak realm — and
says so on success, or fails loudly listing what is left. The name match is
anchored (`/<run>($|-)`), so tearing down `vcrun1` does not match objects of
`vcrun10`. The claim is scoped to exactly those kinds: objects the TESTS
create in the outer cluster through OSAC are not covered (below).

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
manually if a clean slate is needed. `--recreate` does not touch the run's
KafkaUsers or Keycloak realm; if the realm import must run again, tear the
run down first (the Keycloak admin API rejects importing a realm that
already exists).

## Known gaps

- The AAP organization is left behind by the teardown. It carries the run's
  job templates and inventories in the shared AAP; deleting it safely (an
  organization other runs' objects may reference) is follow-up work. The
  nightly fresh outer cluster drops it.
- Objects the TESTS create in the outer cluster through OSAC are not torn
  down: subnet namespaces and ClusterUserDefinedNetworks, VirtualMachines
  and their disks, StorageClasses, MetalLB pools. They carry the instance
  label (`osac.openshift.io/instance=<run>`); deleting by that label is
  follow-up work, as is the OSAC-side labeling for the objects that do not
  carry it yet.
- The run routes are published by `scripts/vcluster-publish-routes.sh`
  after the install (design step 6): passthrough routes for the
  fulfillment APIs under the run's hostnames, an edge route for the UI,
  all in the host namespace so the teardown removes them with it. The
  vcluster API itself still has no route: reaching it from outside the
  outer cluster's network needs a port-forward until that lands.
- The Keycloak steps mirror the password-setting, controller role-grant
  and registration-policy parts of the osac-infra keycloak-set-passwords
  hook. The hook's two MCP steps are deliberately not mirrored: the MCP
  exchange client and the `osac-mcp-client` registration serve the MCP
  HTTP API, which the vmaas-ci and vcluster-ci profiles leave disabled
  (`service.mcp.enabled` defaults to false), and the MCP resource URL they
  are built from (the run's MCP route) does not exist until the run-route
  work lands. Neither client is one of realm.json's seven placeholders:
  the exchange client is created by the hook from the
  `osac-mcp-exchange` secret, and the MCP client from
  `files/mcp-client.json`.
- The AAP bootstrap still applies the license manifest on every run; the
  follow-up to skip it is tracked in OSAC-7084.
- `make install-osac` against a vcluster still needs the unified osac
  branch (PR osac#2566): the `vcluster-ci` profile, the `CLUSTER_DOMAIN`
  Makefile override, `global.instanceId`, `aap.vcluster.*`, the
  external-AAP values (`aap.controllerUrl`,
  `aap.bootstrap.adminPasswordSecret`) and the preflight skips
  (`db-init-host-endpoints`, `default-storageclass`). Until it merges, the Makefile hard-fails against
  a vcluster where it runs `oc get ingresses.config/cluster`. Neither
  `kafka-access` nor the OpenShift version check needs skipping: with
  `kafka.enabled=false` the kafka-access check verifies the
  `service.kafka.connection` secrets this script creates, and `ocp-version`
  is a non-blocking warning.
