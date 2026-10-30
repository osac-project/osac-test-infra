# Netris Backend

Ansible automation to deploy OCP SNO on a [Netris Spectrum-X simulated lab](https://github.com/danmanor/netris-lab) and exercise the deployment scenarios advertised by the backend.

Unless a section says otherwise, backend-only Make targets in this guide run
from `infra/netris/`. Repository orchestration targets such as `make e2e`,
`make setup-suite`, and `make run-tests` run from the `osac-test-infra` root.

> **Using this backend from `osac-test-infra`:** run a complete suite from the
> repository root with `make e2e INFRA=netris DEPLOY_SUITE=<suite>`. `DEPLOY_SUITE` chooses
> the deployment scenario and OSAC profile; `TEST_SUITE` chooses the test
> target independently. Netris-specific overlays live in the OSAC checkout at
> `osac-installer/values/<profile>/netris.yaml`. See the root README's [deployment scenario
> and test selection overview](../../README.md#deployment-and-test-selection)
> for the generic orchestration contract. The [Netris scenario table below](#netris-deployment-scenarios-test-targets-and-profiles)
> lists this backend's profiles, targets, and defaults. The `make deploy*`
> commands in this backend guide are provisioning building blocks; they do not
> run the OSAC pytest suite.

For the complete per-target Make inputs, environment variables, `EXTRA_VARS`,
and Ansible variable mapping, see the [Netris target and option reference](targets.md).

## Architecture

The [netris-lab](https://github.com/danmanor/netris-lab) deploys a full simulated Spectrum-X GPU cluster network on a single bare-metal host using KVM/libvirt:

- **Netris controller** on K3s — manages all network devices via REST/gRPC API
- **~13 switch VMs** (Cumulus Linux) — leaf/spine fabric for North-South connectivity
- **4 softgate VMs** — provide NAT/L4LB and BGP peering for internet access
- **4 server VMs** (hgx-00 to hgx-03) — simulated GPU servers, managed by Netris

This repo takes the first server (hgx-00), resizes it for OCP, configures Netris networking (VPC, VNet, Subnet), and installs OpenShift SNO on it using the Assisted Installer. CaaS and MaaS use the remaining three servers as BMaaS-managed worker VMs. BMaaS provides the BareMetalHosts; the OSAC cluster order provisions and assigns only the requested worker count.

```
Bare-metal host
└── netris-lab (~15 VMs)
    ├── Netris controller (K3s)
    ├── Switches (leaf/spine fabric)
    ├── Softgates (NAT → internet)
    ├── hgx-00 (resized: 20 vCPU, 64G RAM)
    │   ├── VPC/VNet/Subnet configured via Netris API
    │   ├── OCP SNO installed via Assisted Installer
    │   └── OSAC deployed on top
    └── hgx-01..03 (BMaaS worker VMs)
        ├── Registered as BareMetalHosts and sized by the CaaS/MaaS setup
        └── Selected by BareMetalInstanceType when the OSAC cluster order runs
```

Internet access for OCP image pulls flows through: hgx-00 → NS VNet → softgate SNAT → host iptables masquerade → internet.

## Prerequisites

- **Bare-metal host** running a RHEL-family distribution (RHEL, Rocky Linux, CentOS Stream, or Fedora) with KVM support
- **System packages** — `dnf install -y git make python3-pip ansible-core && /usr/bin/python3 -m pip install --ignore-installed ansible`. `make setup-infra` then installs the rest of the BM bootstrap (`sshpass`, `tmux`, `policycoreutils-python-utils`), EPEL from the Fedora URL (required on RHEL), and the Python dependencies into the Ansible interpreter's environment.
- **Resources**: ~32+ CPU cores, 128+ GB RAM (lab VMs + OCP SNO VM)
- **Storage**: ~400GB+ for OCP disk images, K3s, and containers. If the root partition is smaller, use a secondary data disk and enable automatic provisioning with `disk_setup_enabled=true` in `EXTRA_VARS`. Run `make disk-setup` standalone or pass the variable during setup: `make setup-infra EXTRA_VARS="disk_setup_enabled=true"`
- **Netris license key** — place at `infra/netris/license.key`
- **OSAC/AAP license** — place at `infra/netris/license.zip`
- **OpenShift pull secret** — place at `/root/pull-secret` (or set `OSAC_PULL_SECRET_PATH` to a path accessible to deployment and tests; download from [console.redhat.com](https://console.redhat.com/openshift/downloads))
- **Config file** — place `infra/netris/config` (INI format, gitignored) with your lab name, AWS credentials, and Quay username/password. The Quay account must have pull access to `quay.io/osac-project/cluster-flavors` for the snapshot flavor download to work. AWS credentials can be obtained from the [CI vault](https://vault.ci.openshift.org/ui/vault/secrets/kv/kv/selfservice%2Fosac%2Fpacket-osac). See Quick Start below. `lab_name` is the first DNS label of `ocp_base_domain`: use a hostname-compatible label of at most 63 characters (ASCII letters, digits and hyphens, a letter or digit at each end; no underscores, for example `qa-lab-1`). The IAM user needs `route53:ChangeResourceRecordSets`, `route53:ListHostedZones`, and `route53:GetChange` permissions on the hosted zone.

All system packages, tools, and SSH keys are installed automatically by `make setup-infra`. A pre-flight check validates all required files, KVM support, and minimum memory before deploying.

## Quick Start

This section describes the standalone Netris backend workflow: it deploys the
lab and OSAC, but does not run the OSAC mono-repo test suites. To provision and
run tests in one pipeline, use `make e2e INFRA=netris DEPLOY_SUITE=<suite>` from the
`osac-test-infra` repository root. The backend targets below are useful when
you want deployment and CaaS provisioning as separate steps.

```bash
git clone --recurse-submodules https://github.com/osac-project/osac-test-infra.git
cd osac-test-infra/infra/netris

# Place prerequisites
cp /path/to/license.key ./license.key
cp /path/to/license.zip ./license.zip
cp /path/to/pull-secret /root/pull-secret

# Create config (unique lab name, AWS credentials, and Quay credentials)
# lab_name becomes a subdomain under the shared hosted zone (e.g., jsmith.ecoeng-osac-ci.devcluster.openshift.com)
cat > config << EOF
[default]
lab_name = <unique-lab-name>
aws_access_key_id = <your-key>
aws_secret_access_key = <your-secret>
aws_region = us-east-1
netris_password = <your-netris-password>
quay_username = <your-quay-username>
quay_password = <your-quay-password>
EOF

# Install prerequisites and cache images (shared, run once)
make setup-infra

# Provision the lab, restore OCP from the SNO snapshot, and install OSAC
# from the selected profile (this does not run pytest)
make deploy DEPLOY_SUITE=caas

# Optional: prepare and create a CaaS cluster directly
make setup-caas    # CaaS setup: prepare BMaaS worker VMs and register the CaaS prerequisites
make deploy-caas   # CaaS: create cluster
```

### Command order

For an end-to-end test run, start in the repository root. `IMG` holds the
branch-built images and the `osac_branch` that the run installs (see
[VMaaS with the Netris Fabric Manager](#vmaas-with-the-netris-fabric-manager)):

```bash
IMG='osac_branch=<branch> fulfillment_service_image=<reg>/fulfillment-service:<tag> osac_operator_image=<reg>/osac-operator:<tag> osac_aap_image=<reg>/osac-aap:<tag>'
```

Then run:

```bash
make e2e INFRA=netris DEPLOY_SUITE=full TEST_SUITE=stretched EXTRA_VARS="$IMG"
```

The root target runs these steps in order:

1. `setup-infra` — install backend prerequisites and cache the OCP SNO image.
2. `deploy-infra` — provision the Netris lab.
3. `deploy-ocp` — configure OCP networking and restore the SNO cluster.
4. `deploy-osac` — install OSAC using the profile derived from `DEPLOY_SUITE`.
5. `prepare-suite` — run `setup-<DEPLOY_SUITE>` for the selected deployment.
6. `run-tests` — validate the test environment and run `test-<TEST_SUITE>`.

To run those steps individually, use the same selectors and options on each
root command: `make setup-infra`, `make deploy-infra`, `make deploy-ocp`,
`make deploy-osac`, `make setup-suite`, then `make run-tests`, each with
`INFRA=netris DEPLOY_SUITE=...` and, when needed, `TEST_SUITE=...`.
Alternatively, the backend-only `make deploy` target runs steps 2–4 from
`infra/netris/`; it does not run `setup-suite` or pytest.

After deployment, the kubeconfig is at `/root/.kube/config` by default; set
`KUBECONFIG` to use another path throughout the backend. The default
`DEPLOY_SUITE=caas` deploys the CaaS OSAC profile. Then `make setup-caas` and
`make deploy-caas` prepare and create a CaaS cluster. This backend-only flow
does not run pytest; use the root `make e2e` command for the complete pipeline.

## Make Targets

### Deploy

| Target | Description | Time |
|--------|-------------|------|
| `make deploy` | Provision the lab, restore OCP from the SNO snapshot, and install OSAC from the selected profile | ~2-3 hrs |
| `make setup-infra` | Install prerequisites, cache images and the OCP SNO snapshot, build tools | ~10 min |
| `make deploy-infra` | Deploy netris-lab (K3s, topology, VMs, connectivity) | ~12 min |
| `make deploy-ocp` | Configure Netris networking and restore OCP SNO from the OCP-only snapshot | ~15 min |
| `make deploy-osac` | Prepare profile values and install OSAC with Helm | ~30-60 min |

### CaaS (run after deploy)

The `caas` and `maas` suites use BMaaS-backed workers and the persistent
cluster roles.

| Target | Description | Time |
|--------|-------------|------|
| `make setup-caas` | Prepare BMaaS worker VMs, register worker types, DiskImage, Netris ExternalIPPool, and catalog | ~30 min |
| `make deploy-caas` | Create the CaaS catalog cluster with one `ci-worker` BareMetalInstanceType | ~60 min |
| `make setup-maas` | Configure all three BMaaS worker VMs as 10 vCPU / 20GiB `g5` hosts; deploy-maas requests two | ~30 min |
| `make deploy-maas` | Create the MaaS catalog cluster with two `g5` BareMetalInstanceTypes and template defaults | ~60 min |
| `make deploy-suite DEPLOY_SUITE=caas` or `DEPLOY_SUITE=maas` | Run setup, shared pytest tenant fixtures, tenant Netris networking, and persistent cluster deployment | setup + deploy |
| `make deploy-suite DEPLOY_SUITE=bmaas` | Run BMaaS setup, shared pytest tenant fixtures, and create one persistent BMI in each tenant | setup + provisioning |
| `make e2e INFRA=netris DEPLOY_SUITE=caas TEST_SUITE=caas_networking` (or `maas`) from the repo root | Run setup and then the OSAC pytest suite when it exists; the suite fixtures will create tenants | varies |

CaaS and MaaS setup do not add a router to worker management DHCP leases.
A full `redeploy-fresh` recreates the management VM and its DHCP file; a partial
redeployment that retains the VM may retain an older injected router line.
The tenant VNet DHCP should supply the worker default gateway. Cluster API and
ingress use the Netris external-access flow configured in AAP; the shared
ExternalIPPool is used for tenant egress. The guest network task restores
`routingViaHost: false` only if an older cluster has it set to true, then waits
for the Network operator. Fresh clusters keep their default setting. The OCP
data uplink is discovered from the live Netris topology; `eth10` remains the
VMaaS EVPN underlay when enabled.

### Netris deployment scenarios, test targets, and profiles

The following commands are run from the `osac-test-infra` repository root.
`make e2e` performs provisioning, deployment, test prerequisite setup, and tests. A
`make test-*` target skips provisioning and runs tests against an already
prepared cluster.

`DEPLOY_SUITE` names the deployment scenario. It drives the Netris setup path,
Ansible behavior, and derived OSAC profile. `TEST_SUITE` selects the OSAC test
path and defaults to `DEPLOY_SUITE`. Netris networking tests live under
`tests/e2e/networking/` in the OSAC mono-repo.

| Deployment `DEPLOY_SUITE` | Derived OSAC profile and Netris overlay in the OSAC checkout | Target run by `make prepare-suite` | Default `TEST_SUITE` / test target |
|---|---|---|---|
| `caas` | `caas-ci` (no Netris overlay) | `setup-caas` | `caas` / `test-caas` |
| `maas` | `caas-ci` (no Netris overlay) | `setup-maas` | `maas` / `test-maas` |
| `vmaas` | `vmaas-ci` + `osac-installer/values/vmaas-ci/netris.yaml` | `setup-vmaas` | `vmaas` / `test-vmaas` |
| `bmaas` | `bmaas-ci` + `osac-installer/values/bmaas-ci/netris.yaml` | `setup-bmaas` | `bmaas` / `test-bmaas` |
| `full` | `full-ci` + `osac-installer/values/full-ci/netris.yaml` | `setup-full` | `full` / `test-full` |

The test-suite targets are:

| `TEST_SUITE` | Make target | OSAC test path |
|---|---|---|
| `caas` | `test-caas` | `tests/e2e/caas/` |
| `maas` | `test-maas` | Dedicated OSAC path, when it exists |
| `vmaas` | `test-vmaas` | `tests/e2e/networking/vmaas/` |
| `bmaas` | `test-bmaas` | `tests/e2e/networking/bmaas/` |
| `full` | `test-full` | `tests/e2e/` minus `networking/vmaas` and `networking/bmaas`, serial |
| `stretched` | `test-stretched` | `tests/e2e/networking/stretched/` |

The PR E2E commands use the same split:

| PR command | Deployment `DEPLOY_SUITE` | `TEST_SUITE` | OSAC profile | Check |
|---|---|---|---|---|
| `/test vmaas-netris` | `vmaas` | `vmaas` | `vmaas-ci` | `E2E / VMaaS Netris Full Install` |
| `/test bmaas-netris` | `bmaas` | `bmaas` | `bmaas-ci` | `E2E / BMaaS Netris Full Install` |
| `/test stretched-netris` | `full` | `stretched` | `full-ci` | `E2E / Stretched Netris Full Install` |

This table describes the root orchestration targets. Later backend-only target
examples in this guide assume the working directory is `infra/netris/`; from
the repository root, use orchestration targets such as `make e2e`,
`make prepare-suite`, and `make run-tests` with `INFRA=netris`.

`TEST_SUITE` may differ from the deployment `DEPLOY_SUITE`. For example,
`DEPLOY_SUITE=full TEST_SUITE=stretched` deploys the full-ci profile and runs only the
shared-subnet test at `tests/e2e/networking/stretched/`. Do not set
`OSAC_PROFILE`; the backend derives it from `DEPLOY_SUITE`.

The variables select different parts of the run:

- `INFRA=netris` selects this backend.
- `DEPLOY_SUITE=...` selects the deployment scenario and therefore backend setup,
  Ansible behavior, and OSAC profile. The profile is derived from the suite
  (`vmaas` → `vmaas-ci`, `bmaas` → `bmaas-ci`, `full` → `full-ci`).
- `TEST_SUITE=...` selects the test target independently. It defaults to
  `DEPLOY_SUITE`; use `TEST_SUITE=stretched` with `DEPLOY_SUITE=full` to run only the
  shared-subnet VMaaS↔BMaaS test against a full-ci deployment.
- `EXTRA_VARS=...` passes Ansible variables, such as the image overrides and
  `osac_branch` below. Common overrides are shown below; the [target reference](targets.md)
  lists all supported variables and which targets consume them.

The commands below run from the repository root of a checkout of this branch on the lab host. `IMG` holds the branch-built images (the same value in all three commands); the guard in `check_images.yml` requires them for `vmaas` and `full`, and the PR branch needs its own images for `bmaas` too.

```bash
IMG='osac_branch=<branch> fulfillment_service_image=<reg>/fulfillment-service:<tag> osac_operator_image=<reg>/osac-operator:<tag> osac_aap_image=<reg>/osac-aap:<tag>'
make e2e INFRA=netris DEPLOY_SUITE=vmaas EXTRA_VARS="$IMG"                      # VMaaS networking
make e2e INFRA=netris DEPLOY_SUITE=bmaas EXTRA_VARS="$IMG"                      # BMaaS networking
make e2e INFRA=netris DEPLOY_SUITE=full TEST_SUITE=stretched EXTRA_VARS="$IMG"  # stretched VM/BM
```

Add `osac_repo=https://github.com/<fork>/osac.git` to `IMG` when the branch lives on a fork. `DEPLOY_SUITE=bmaas` runs the slow cross-tenant class unless `TEST='not TestCrossTenantBmaasNetworking'` is added, as CI does. Never pass `TEST=` (a `-k` filter) to `DEPLOY_SUITE=vmaas`: `tests/e2e/networking/vmaas/` is one ordered module. `SUITE=` and `OSAC_DEPLOY_MODE=` stop `make` with an error; see [`targets.md`](targets.md) for the old-to-new names, the full mapping and every variable.

Use Make variables for backend selectors and paths; use `EXTRA_VARS` for
Ansible role settings. For example, `KUBECONFIG=/path/to/kubeconfig` maps to
the Ansible task environment, while `OSAC_PULL_SECRET_PATH=/path/to/pull-secret`
sets the same path for deployment and `.env.infra`. In CI, a step's `env:` is
only how the shell receives a value; the backend explicitly maps it to Make,
Ansible, or test runtime as needed. The comprehensive [target reference](targets.md)
documents those mappings and precedence.

To run only tests after the cluster and OSAC are ready, use the target mapped
from `TEST_SUITE`. For example, `make test-vmaas` selects Netris VMaaS
networking tests. In the pipeline, use:

```bash
make run-tests INFRA=netris DEPLOY_SUITE=vmaas
```

This loads the environment saved by `deploy-osac` and invokes `test-vmaas`.
For stretched tests against a full-ci deployment, use
`make run-tests INFRA=netris DEPLOY_SUITE=full TEST_SUITE=stretched`.

The Netris VMaaS and full paths use the single-node OCP 4.22 OVNKubernetes cluster with the `None` platform type. For `DEPLOY_SUITE=vmaas` or `DEPLOY_SUITE=full`, `deploy-infra` reuses h00's Netris-managed leaf-1 link (`eth10` ↔ `swp1s0`) for the `/31` underlay and enables underlay on that link; leaf-0's `eth9` link remains the `br-ex` data path. h00's Linux interface names are resolved from the MAC addresses correlated across Netris, the leaf interface, and the libvirt tunnel, so the guest NIC enumeration may change without changing which path carries data or underlay. It sets h00's server ASN and EVPN-VTEP inventory role so Netris can generate the leaf-side peer from the managed link. `deploy-ocp` validates the OpenShift inputs and writes an ignored, site-specific `infra/netris/.evpn-values.yaml`; the fresh `deploy-osac` path passes those values to `osac-installer` `install-infra`, which configures OVN route advertisements, NMState, the unmanaged VTEP, node uplink addresses, and OCP-side FRR. The separate Netris deployment configures the fabric side; this flow does not install or rely on the Netris host agent. No standalone VLAN-backed BGP object is created for the routed `/31`. `vmaas-ci` enables VMaaS only; `full-ci` enables every service, so VM and bare-metal instances can attach to one subnet. BMaaS continues to use the Netris server-port names and its separate per-BMH MAC mapping for `eth9`/`eth10`; assigning h00's `eth10` to the underlay does not change h01–h03 provisioning NIC selection. `setup-bmaas` publishes the configured BMI guest interface name as `OSAC_BMI_NETWORK_INTERFACE`; BMaaS and stretched networking tests consume that value instead of embedding a server-port name in the test code.

Netris/CUDN EVPN settings are overlays inside the existing OSAC service profile
directories: `osac-installer/values/vmaas-ci/netris.yaml`,
`osac-installer/values/bmaas-ci/netris.yaml`, and
`osac-installer/values/full-ci/netris.yaml`. The backend layers the selected
overlay after the base profile and generates `netris-runtime.yaml` with lab
controller, site, VPC, and credential values. The stretched test suite runs
against the `full-ci` deployment and overlay.

`full-ci` enables all four OSAC services (CaaS, VMaaS, BMaaS, and MaaS).
`setup-full` runs `setup-caas`, `setup-bmaas`, and `setup-vmaas`. `TEST_SUITE=full`
runs `tests/e2e/` serially. It ignores `tests/e2e/networking/vmaas` (one ordered
module) and `tests/e2e/networking/bmaas` (its `PROTOCOL_ICMP` rules are rejected
with cudn_evpn), includes the shared-subnet stretched test, and deselects the
cases listed in `FULL_TEST_FILTER` of the root Makefile, among them the
ExternalIP tests that need a separately provisioned return path.

### VMaaS with the Netris Fabric Manager

`vmaas-ci` and `full-ci` run `tests/e2e/networking/vmaas` (selected by directory, not by markers; the target has no `-x`, so one NAT failure does not hide the rest). The VMaaS and stretched tests do not use `virtctl`: they reach guests through a helper Pod (`osac-e2e-ssh-helper`, created per VM namespace) and `kubectl exec`. The pytest host needs `kubectl` or `oc`, `ssh` and `ssh-keygen`. The helper image defaults to the digest-pinned OSAC execution-environment image (`ghcr.io/osac-project/osac-aap`), which the cluster already pulls for AAP jobs. The lab exports nothing; set `OSAC_E2E_SSH_HELPER_IMAGE` to the deployed tag or a mirror (an image with an ssh client and a passwd entry for uid 65534) only when ghcr.io is unreachable. The pytest identity must be allowed to create Pods running as uid 65534 in the VM namespaces.

VMaaS EVPN runs the OSAC Netris Fabric Manager (`osac.templates.netris`) and the `cudn_evpn` Kubernetes Manager from the networking contract of the OSAC installer chart (`global.networking`, `aap.instanceGroups.networkFulfillment`). The installer chart renders both registrations (`osac-network-fabric-manager-netris`, `osac-network-k8s-manager-cudn-evpn`, immutable) and creates the default NetworkClass; nothing is applied by hand. `prep-osac` writes these values to `values/<profile>/netris-runtime.yaml` (mode 0600) and leaves the committed profile alone. `values/<profile>/netris.yaml` selects the managers:

| Value | Source |
|-------|--------|
| `global.networking.provisioningEnabled: true`, `fabricManager: netris` | `netris.yaml` for vmaas, bmaas, full; the runtime file for caas and maas |
| `global.networking.k8sManager` | `netris.yaml`: `cudn_evpn` for vmaas and full, `""` for bmaas; caas-ci's profile value otherwise (prep-osac asserts vmaas, full and bmaas) |
| `global.networking.networkClass.defaults` | `virtualNetworkIPv4CIDR` and `subnetIPv4CIDR` from `netris_tenant_network_cidr` / `netris_tenant_subnet_cidr` (`192.168.101.0/24`, the CaaS tenant default network) |
| `global.networking.excludedIpv4Cidrs` | computed: all of `10.0.0.0/8` (the Netris private allocation; tenant networks must not use 10.x, the E2E suites use 172.16-172.31 except 172.27 and 172.30), the bare-metal resolver `/32` (`dns_server`, handed to bare metal by the Netris default DHCP option set), OCP SNAT/DNAT `/32`, node subnet, `192.168.16.0/20`, BMC network, provisioning SNAT range, then from `.evpn-values.yaml` the VTEP CIDR, the Netris loopbacks (`acceptPrefixes`) and the node uplink networks, plus `netris_extra_excluded_cidrs` |
| `aap.instanceGroups.networkFulfillment.config` | `NETRIS_CONTROLLER_URL` (HTTP NodePort), `NETRIS_USERNAME`, `NETRIS_VALIDATE_CERTS: "false"`, `NETRIS_SITE_ID`, `NETRIS_TENANT_ID: "1"`, `NETRIS_TENANT_NAME`, `NETRIS_MGMT_VPC_ID`, `NETRIS_MGMT_VPC_NAME`, `NETRIS_BM_PROVISIONING_VNET`, `NETRIS_RESOURCE_CLASS_MAP` (the FabricDomain needs it). There is no DNS key: OSAC has no per-resource DNS |
| `aap.instanceGroups.clusterFulfillment.config` | CaaS profiles only: `NETWORK_CLASS: netris` and `NETWORK_STEPS_COLLECTION: netris.steps` (the controller URL, TLS and CA are bridged from `networkFulfillment.config` by the chart). Other keys: `NETRIS_USERNAME`, `NETRIS_SITE_ID`, `NETRIS_TENANT_ID`, `NETRIS_TENANT_NAME`, `NETRIS_MGMT_VPC_ID`, `NETRIS_MGMT_VPC_NAME`, `NETRIS_RESOURCE_CLASS_MAP` |
| `aap.instanceGroups.networkFulfillment.secret.NETRIS_PASSWORD` | `netris-runtime.yaml`, passed to `install-osac` through `INSTANCE_VALUES_EXTRA` |
| `aap.instanceGroups.clusterFulfillment.secret.NETRIS_PASSWORD` | only when the profile enables CaaS: `netris-runtime.yaml`, with the AWS and SSH secrets |
| `aap.configAsCode.projectGitUri` / `projectGitBranch` | `osac_repo` / `osac_branch` unless `aap_project_git_uri` / `aap_project_git_branch` are set |

The AAP project checkout wins over the execution environment image for collections (`osac-aap/ansible.cfg`), so the default execution environment image (`aap.configAsCode.eeImage`) works and no derived image is built. The AAP bootstrap image is different: the bootstrap job runs `osac.config_as_code.configure` from `aap.bootstrap.image`, so a `main` image registers main's job templates. `osac_repo` must point at a repository AAP can clone (for example `EXTRA_VARS="osac_repo=https://github.com/<user>/osac.git osac_branch=<branch>"`).

The lab needs an osac (#2051) commit that has the DNS removal (no `CUDN_EVPN_DNS_RESOLVERS` or Netris DNS SNAT requirement in the NetworkClass step) and the `security_group_icmp` capability (the Netris registration declares `eastWestEthernet,security_group_icmp`, which `setup-vmaas` asserts); use that commit for `osac_branch` and build the operator, fulfillment-service and AAP images from it. `prep-osac` also patches the cloned aap chart to accept the lab's plain-HTTP Netris controller URL (task "Allow HTTP Netris controller URL in the isolated lab", which fails when the chart text changes). `KUBECONFIG` may be non-default: `restore-ocp` also writes `/root/.kube/config`, which many roles read.

Images from the PR branch are required. The profile's operator, fulfillment-service and AAP bootstrap images are ghcr `:latest`/`:main`. For an install of `DEPLOY_SUITE=vmaas` or `full`, `deploy-setup` and `deploy-infra` fail early on a lab host (`_check-osac-images`; it is skipped under GitHub Actions, where the images are built after `deploy-ocp`) and `prep-osac` checks again, unless `fulfillment_service_image`, `osac_operator_image` and `osac_aap_image` are set to a tagged image (a tag, not a `@sha256` digest) other than `:latest` or `:main`. Konflux builds them for each PR commit as `quay.io/redhat-user-workloads/osac-tenant/{osac-operator,fulfillment-service,osac-aap}:on-pr-<full commit sha>` (expire after 5 days); take the sha from `git rev-parse origin/<branch>`. The hub pull secret (`pull_secret_path`) must be able to pull from `quay.io/redhat-user-workloads`; this repository does not configure that registry anywhere, so verify it before the run.

`setup-vmaas` checks the registrations (`immutable`, `ansible_role == osac.templates.netris`, `network_types` containing `evpn`) and waits through the private API until the default NetworkClass is `NETWORK_CLASS_STATE_READY` with `fabric_manager=netris` and `k8s_manager=cudn_evpn`.

DNS: OSAC has no per-resource DNS. VMs on the primary UDN use the cluster DNS that OVN-K DHCP hands out. Bare metal gets an installation-level resolver (`dns_server`, default `8.8.8.8`) from the Netris default DHCP option set. `configure-netris` (run by `deploy-ocp` for every deployment suite) writes it through `/api/v2/dhcp-option-set` and fails when the default option set is missing. This is the lab's copy of the provider install step; it is not OSAC code. Bare metal without internet access needs no DNS reachability.

Prerequisites outside this repository:

- D-1/D-2: the Netris controller reachable from AAP at the HTTP NodePort URL.
- The helper Pod image (`OSAC_E2E_SSH_HELPER_IMAGE`) pullable by the cluster (see above).
- NAT and ExternalIP tests (`test_11` to `test_17`) use the routed `198.51.100.24/29` pool (`connectivity_mode: isolated`) and run without `ocp_vmaas_external_ip_*`. Only the `external_ip/` and `references/` directories use the `172.27.0.0/16` pool: for those, set `ocp_vmaas_external_ip_tests_enabled` and `ocp_vmaas_external_ip_return_path_acknowledged` to true (then `setup-vmaas` exports `OSAC_NETRIS_VM_EXTERNAL_PATH_ENABLED=true` into `.env.infra`) and provide the `172.27.0.0/16` return path.

Release gate: the module `test_vmaas_networking.py` is ordered, `test_00` to `test_21`. The gate equivalent is phases A and B, `test_00` to `test_10`. NAT and ExternalIP (phase C, `test_11` to `test_17`) are not part of the gate until Netris builds the VPC VRF on the SoftGates; they always run and fail with a diagnosis rather than skip. Phase D, `test_18` to `test_21`, are the explicit deletes. Helper Pods, Secrets and NetworkPolicies that an aborted run leaves behind carry the label `osac-e2e/ssh-helper=true`; `make destroy-vmaas` and `make gather-vmaas` delete them (nothing found is fine).

Expected results of `make e2e INFRA=netris DEPLOY_SUITE=vmaas`, by test number in `test_vmaas_networking.py`:

| Tests | Expectation |
|---|---|
| `test_00` to `test_04` | Connectivity and isolation (gate). Should pass once the helper Pod reaches the VMs over SSH; never run automated before |
| `test_05` | ICMP rule rejection; passes with the ICMP capability images, fails on older ones |
| `test_06` | Cluster DNS from OVN-K DHCP; unknown until run on the lab |
| `test_07` to `test_09` | SecurityGroup allow, deny and update; should pass, not lab-tested |
| `test_10` | Cross-tenant isolation; should pass |
| `test_11` | External IP pool; depends on Netris accepting the pool allocation |
| `test_12` to `test_17` | NAT and ExternalIP paths; expect failure until Netris builds the VPC VRF on the SoftGates without member ports (see the probe findings) and imports the `0:<VNI>` route targets (SoftGates import `0:<VNI>`, same as `cudn_evpn`; the leaves auto-generate `<ASN>:<VNI>`). A red `test_12` is the honest result and replaces the earlier skips |
| `test_18` to `test_21` | Explicit deletes; run only when everything before passes (finalizers clean up otherwise) |

The Netris gateway stays on `gateway_ipv4` (`.1`) of the EVPN Subnets (the lab underlay steps are unchanged). The module is ordered and runs about 45-60 minutes when green.

Lab probe findings (2026-10-29, one-off Netris API and FRR probe): the Netris API assigned VNIs (VPC 11, V-Net 14, V-Net with gateway 17) but none of them, nor the probe VPC VRF, appeared on any leaf or SoftGate, with or without a gateway. Netris appears to deploy a V-Net/VRF only where member ports exist (the probe V-Nets had none); a bare-metal V-Net member made the earlier spike work. Open question: with the hub as underlay (no V-Net member port), does Netris ever build the VPC VRF on the leaves and SoftGate? This is needed for routed/NAT/ExternalIP traffic (`test_12` to `test_17`), not for `test_00` to `test_10`. Explicit `vxlanID` values are honoured, 44-character VPC and V-Net names are accepted (47 characters, `osac-natgw-<uuid>`, untested), and Netris exposes no route-target field.

The focused BMaaS Netris networking invocation is:

`make e2e INFRA=netris DEPLOY_SUITE=bmaas EXTRA_VARS="$IMG"`

`DEPLOY_SUITE=bmaas` (profile `bmaas-ci`, `k8sManager: ""`) runs `tests/e2e/networking/bmaas` by directory. The standard BMaaS suite does not select that directory. It is valid only with `DEPLOY_SUITE=bmaas`: its `PROTOCOL_ICMP` rules are rejected with cudn_evpn.

The stretched VMaaS↔BMaaS Netris invocation is:

```bash
make e2e INFRA=netris DEPLOY_SUITE=full TEST_SUITE=stretched EXTRA_VARS="$IMG"
```

`DEPLOY_SUITE=full TEST_SUITE=stretched` uses the `full-ci` profile, prepares CaaS, BMaaS and VMaaS prerequisites plus the CUDN EVPN underlay, and runs only `tests/e2e/networking/stretched`. The same test is also included in `TEST_SUITE=full`. Both contracts require the BMI guest interface name published by BMaaS setup.

The full-profile invocation is:

```bash
make e2e INFRA=netris DEPLOY_SUITE=full EXTRA_VARS="$IMG"
```

`DEPLOY_SUITE=full` uses the `full-ci` OSAC profile and configures the same h00 underlay and CUDN EVPN prerequisites. `setup-full` prepares BMaaS-backed CaaS prerequisites, reapplies BMaaS defaults, then runs the VMaaS prerequisite check. `test-full` runs the serial OSAC mono-repo E2E set with its configured exclusions; it ignores `tests/e2e/networking/vmaas` (an ordered module) and `tests/e2e/networking/bmaas` (its `PROTOCOL_ICMP` rules are rejected with cudn_evpn) and includes the shared-subnet stretched test. The Netris PR workflow exposes `bmaas-netris`, `vmaas-netris`, and `stretched-netris`; the stretched check runs only the shared-subnet BMI/ComputeInstance test.

Suite fixtures create their own tenant `VirtualNetwork`, `Subnet`, `SecurityGroup`, and `ExternalIPPool` resources. Lab setup owns the cluster/fabric prerequisites and must not pre-create fixture-owned tenant resources. PodNetwork route advertisements support shared-subnet VMaaS/BMaaS traffic. ExternalIP and EgressIP dataplane tests require a separately provisioned and acknowledged route/return path for `172.27.0.0/16`; enable `ocp_vmaas_external_ip_tests_enabled` only after verifying that path.

To collect diagnostics from an existing deployment, run `make gather-suite INFRA=netris DEPLOY_SUITE=vmaas`. The CUDN EVPN captures are written under `infra/netris/logs/cudn-evpn/`. Collection is read-only and best-effort; unavailable APIs or peers get an explicit marker in the output. Captured Kubernetes YAML, FRR logs, and Netris responses are scrubbed for credential-like values before they are written.

### Bare-metal prerequisites

`bare_metal_services` is derived from `DEPLOY_SUITE` (`BARE_METAL_SERVICES`): true for `caas`, `maas`, `bmaas`, and `full`, false for `vmaas`. A value in `EXTRA_VARS` has no effect. It prepares the Metal3 `Provisioning` resource with `watchAllNamespaces: true`, starts `sushy-tools`, and reserves the OCP VM's BMC address.

VM-backed tests require `KUBECONFIG`, `OSAC_NAMESPACE`, and
`OSAC_VM_KUBECONFIG` in `.env.infra`; the VMaaS, full, and stretched test
contracts check these variables. For a single-cluster profile, the VM kubeconfig
may point to the hub kubeconfig. Configure a separate path when tests use a
separate workload cluster.

`setup-bmaas` adds `OSAC_BMH_SSH_HOSTS`, `OSAC_BMI_INSTANCE_TYPE`, and
`OSAC_BMI_NETWORK_INTERFACE` to `.env.infra`. The BMaaS, full, and stretched
contracts require these values; the root `run-tests` target checks them after
the selected deployment scenario has been prepared.

### Destroy

| Target | Description |
|--------|-------------|
| Root `make destroy-suite DEPLOY_SUITE=<scenario>` | Safely dispatch a deployment-scenario cleanup; rejects `DEPLOY_SUITE=full` because that would destroy the entire lab |
| `make destroy-full` | Tear down everything: OSAC + OCP artifacts + netris-lab |
| `make destroy-osac` | Tear down OSAC: helm releases, operators, CRDs, namespaces |
| `make destroy-ocp` | Reset OCP for reinstall: delete cluster, recreate disk, boot VM |
| `make destroy-infra` | Tear down netris-lab (VMs, K3s, topology) |
| `make destroy-caas` | Delete the named OSAC cluster and retain reusable BMaaS hosts |
| `make force-destroy-caas` | Force-clean stuck orders, remove worker BMHs, wipe worker VMs, and sweep Netris orphans |
| `make destroy-maas` | destroy-caas with MaaS cluster/host-type overrides |
| `make force-destroy-maas` | force-destroy-caas with MaaS overrides |
| `make destroy-bmaas` | BMaaS teardown: remove BMH/K8s resources, stop BMaaS VMs, clean disks, delete BMH namespace |

### Recovery and Utilities

| Target | Description |
|--------|-------------|
| `make connectivity` | Re-run lab connectivity (VPN, BGP, softgates) without full redeploy |
| `make run-osac-setup` | Run the infrastructure and application Helm installs after prep-osac |
| `make prep-osac` | Ansible-only OSAC prep (clone, generate `netris-runtime.yaml`, copy secrets) — no Helm install |
| `make vendor-update` | Refresh vendored Ansible collections |
| `make lint` | Run ansible-lint |
| `make gather-infra` | Gather diagnostic info from the cluster |

### Typical Workflows

**First deployment on a fresh server:**
```bash
make setup-infra    # install prerequisites, cache images and OCP SNO snapshot
make deploy         # deploy lab + restore OCP + install OSAC (~2-3 hrs)
```

**Deploy with an image override (test a PR build):**
```bash
make deploy EXTRA_VARS="fulfillment_service_image=quay.io/osac/fulfillment-service:pr-123"
```

**Deploy OSAC with BMaaS values and image overrides:**
```bash
make deploy-ocp DEPLOY_SUITE=bmaas
make deploy-osac DEPLOY_SUITE=bmaas \
  EXTRA_VARS="osac_operator_image=quay.io/dmanor/osac-operator:tag bmf_operator_image=quay.io/dmanor/bare-metal-fulfillment-operator:tag aap_project_git_branch=my-feature-branch"
make setup-bmaas
```

> `aap_project_git_branch` (and `aap_project_git_uri`) point the AAP config-as-code project
> at a specific branch, so AAP syncs osac-aap playbooks/collections from that branch at
> runtime. This is separate from `osac_branch`, which only controls the mono-repo clone used
> for the installer Helm charts — to fully test a PR whose changes include osac-aap Ansible,
> set both `osac_branch` and `aap_project_git_branch` to the PR branch.

**Re-deploy OSAC after code changes:**
```bash
make destroy-osac   # tear down OSAC (keeps OCP and lab)
make deploy-osac    # redeploy
```

**Re-install OCP (e.g., different version):**
```bash
make destroy-ocp    # delete cluster, recreate disk
make deploy-ocp     # reinstall
```

**Fix lab connectivity issues (e.g., softgate/E-BGP):**
```bash
make connectivity   # re-runs VPN, socat, ISP FRR, softgate agents
```

**Deploy CaaS after OSAC is up:**
```bash
make setup-caas     # prepare BMaaS workers and register CaaS prerequisites
make deploy-caas    # create cluster
```

**Deploy MaaS after OSAC is up:**
```bash
make setup-maas     # h01–h03 use 10 vCPU / 20GiB and are registered as g5 BMaaS hosts
make deploy-maas    # create a 2-worker cluster; optional template defaults apply
```

**Rebuild from scratch:**
```bash
make destroy-full   # tear down everything
make deploy         # provision again and install OSAC from the selected profile
```

**Bare-metal host redeployment pipeline (`redeploy-server.sh`):**
```bash
# CaaS deployment (default)
make redeploy-fresh

# MaaS deployment
make redeploy-fresh DEPLOY_SUITE=maas

# BMaaS deployment
make redeploy-fresh DEPLOY_SUITE=bmaas
```

### Bare-Metal Lab Deployment (from laptop)

For persistent bare-metal servers behind NAT (e.g., Red Hat lab infrastructure), use the remote deploy workflow. This handles image caching, bootstrapping, and resilient multi-step deploys from your laptop.

**One-time setup:**
```bash
git clone --recurse-submodules https://github.com/osac-project/osac-test-infra.git
cd osac-test-infra/infra/netris

# Create env file from template (one per server, gitignored)
cp scripts/env.sh.example scripts/env.sh
# Edit scripts/env.sh with: SERVER IP, PASSWORD, LAB_NAME, NETRIS_PASSWORD, secrets paths, AWS keys
# For private snapshot flavor pulls, also set QUAY_USERNAME and QUAY_PASSWORD
# OSAC_PULL_SECRET_PATH (default /root/pull-secret) is where the pull secret lands on the server and what the deploy reads
```

**Deploy:**
```bash
source scripts/env.sh && make deploy-jump
```

`NETRIS_PASSWORD` is required by `deploy-jump` and is written to the remote `config` file. Keep it in the gitignored env file; the script sends the generated config over SSH rather than placing the password in the SSH command.

For a private snapshot flavor pull, set both `QUAY_USERNAME` and `QUAY_PASSWORD` in that env file. `deploy-jump` writes them to the remote `config`; the snapshot cache role uses them to create a temporary Skopeo auth file, so a separate Quay `skopeo login` on the worker is unnecessary. They can be left empty when no private flavor pull is needed.

This single command:
1. Pre-caches container images on your laptop (with retries, avoids rate limits)
2. Rsyncs the repo + cached images to the server
3. Bootstraps packages (EPEL, Ansible, pip deps)
4. Sets up data disk (partition, mount, symlinks, SELinux)
5. Destroys any previous deployment
6. Runs the full pipeline: setup-infra → deploy-infra → deploy-ocp → deploy-osac → post-install → suite setup → suite workload (including tenant and network preparation) → access-doc

**Monitor:**
```bash
ssh root@$SERVER -t tmux attach -t deploy
ssh root@$SERVER tail -f /root/deploy.log
```

**Multiple servers:** Create one env file per server (`scripts/env-mylab.sh`), then:
```bash
source scripts/env-mylab.sh && make deploy-jump
```

**BM-specific make targets (run on server):**

| Target | Description |
|--------|-------------|
| `make redeploy-fresh` | Destroy + wipe progress + full pipeline (`DEPLOY_SUITE`) |
| `make redeploy-continue` | Resume failed redeploy from last incomplete step |
| `make disk-setup` | Auto-detect and mount data disk |
| `make post-install` | Generate the access document |
| `make access-doc` | Generate the access document only |
| `make health-check` | Quick status verification |

`redeploy-server.sh` steps: `setup-infra` → `deploy-infra` → `deploy-ocp` → `deploy-osac` → `post-install` → `setup-caas|setup-maas|setup-bmaas` → `deploy-caas|deploy-maas|deploy-bmaas` → `access-doc`. OSAC is always a fresh Helm install; `--snapshot` was removed and exits with an error.

`post-install` generates the initial access document before workload setup. The final `access-doc` step refreshes it with workload-cluster details.

See the PR description for known issues and workarounds specific to BM/RHEL environments.

## Accessing OCP Routes

After `make deploy-osac`, a socat forwarder on port 9444 provides access to OCP routes (AAP UI, OCP console, fulfillment API) from external browsers. Port 443 is intercepted by K3s svclb (Netris controller), so 9444 is used instead.

Add to your local `/etc/hosts`:
```
<server-ip>  osac-aap-osac-devel.apps.ocp-sno.osac.local
<server-ip>  console-openshift-console.apps.ocp-sno.osac.local
<server-ip>  fulfillment-api-osac-devel.apps.ocp-sno.osac.local
```

Then browse to `https://osac-aap-osac-devel.apps.ocp-sno.osac.local:9444` (accept the self-signed cert).

| Service | URL |
|---------|-----|
| AAP UI | `https://osac-aap-osac-devel.apps.ocp-sno.osac.local:9444` |
| OCP Console | `https://console-openshift-console.apps.ocp-sno.osac.local:9444` |
| Assisted Installer UI | `http://<server-ip>:8080` |
| Netris Controller | `http://<server-ip>:9443` |

## How deploy-ocp Works

`make deploy-ocp` restores OCP SNO from the OCP-only `sno-4-22` VM snapshot. It uses a golden qcow2 disk image (cached during `make setup-infra`) and regenerates the cluster's identity (certificates, hostname, IP, domain) using [recert](https://github.com/rh-ecosystem-edge/recert). OSAC is installed separately by `make deploy-osac`.

The flow runs three Ansible roles in sequence:

1. **`configure-netris`** — discovers h00's Netris server ports by correlating Netris links, leaf-interface MAC addresses, and libvirt tunnel endpoints. It preserves a verified OCP VNet attachment or chooses an unused verified data port, excluding underlay links. It then configures the VPC/VNet and SNAT/DNAT rules.
2. **`configure-dns`** — creates Route 53 DNS records and local dnsmasq config for the cluster domain
3. **`restore-ocp`** — creates copy-on-write disk overlays, grows the OS and LVM disks (the h00 OS disk to `ocp_snapshot_disk_gb` for every suite), and writes pre-boot config. At boot, the recert service finds each guest NIC by its discovered MAC, applies the OCP address and default route to the data NIC, and verifies gateway reachability through the management path before continuing over DNAT SSH. It then waits for cluster health.

The server-side Netris port names (for example, `eth9` for data and `eth10` for underlay) are not Linux interface names inside h00. The deployment discovers the guest MAC for each port from Netris, the leaf interface, and libvirt, then resolves the current guest interface name by MAC. It also resolves the management interface from its IP before applying reverse-path filtering. It fails closed if a port, tunnel, or guest MAC mapping is ambiguous. Cloudsim's `/root/.cloudsim_aliases` provides the management IP; set `ocp_vm_mgmt_ip` only if those aliases are unavailable.

The snapshot flavor is pulled and cached during `make setup-infra` (one-time ~60GB download). Subsequent deploys use copy-on-write overlays, so only changed blocks are written.

## How deploy-osac Works

`make deploy-osac` runs in two phases:

1. **`prep-osac`** (Ansible) — clones the OSAC mono-repo, validates the selected profile and its Netris overlay, resolves the OCP release CLI image, and writes the generated lab-specific values to `values/<profile>/netris-runtime.yaml`. That file includes credentials, is mode `0600`, and is ignored by Git. The `Allow HTTP Netris controller URL` task patches the cloned aap chart (`osac-aap/charts/aap/templates/config-as-code-secret.yaml`) to accept the lab's plain-HTTP controller URL. The role also copies the license and pull secret into the profile directory and sets up a socat forwarder for OCP ingress on port 9444.

2. **`run-osac-setup`** (shell) — runs `make install-infra` and then `make install-osac` in `osac-installer`. It passes `OSAC_PROFILE` plus the profile's static `netris.yaml` and generated `netris-runtime.yaml` overlays. The infrastructure install deploys the required operators and prerequisites; the OSAC install deploys the application chart and runs its hooks.

## Configuration

The [Netris target and option reference](targets.md) is the comprehensive
reference for Make targets, Make variables, environment/config inputs, and
Ansible `EXTRA_VARS`. It maps each target to the playbooks and variables it
uses. Defaults are maintained in
[`inventory/group_vars/all.yml`](inventory/group_vars/all.yml).

The deployment and test selectors have separate jobs:

- `DEPLOY_SUITE` chooses the deployment scenario and derives the OSAC profile.
  Supported values are `caas`, `maas`, `bmaas`, `vmaas`, and `full`.
- `TEST_SUITE` chooses the pytest target. It defaults to `DEPLOY_SUITE`, but
  can differ; for example, `DEPLOY_SUITE=full TEST_SUITE=stretched` deploys
  `full-ci` and runs only the stretched networking suite.
- `EXTRA_VARS` passes Ansible variable overrides to the playbook run by each
  target. Use space-separated `key=value` entries or a JSON object.

Common overrides:

| Option | Example | Use |
|---|---|---|
| `dns_server` | `EXTRA_VARS="dns_server=<reachable-dns-server>"` | The bare-metal resolver, which the Netris default DHCP option set hands to bare metal, and the lab resolver. Change it when public DNS is unavailable. Default: `8.8.8.8`. Set it permanently as `dns_server` in [`inventory/group_vars/all.yml`](inventory/group_vars/all.yml); a per-run `EXTRA_VARS` value has the highest precedence. |
| `osac_branch` | `EXTRA_VARS="osac_branch=my-branch"` | Install OSAC charts from a mono-repo branch. |
| `fulfillment_service_image` | `EXTRA_VARS="fulfillment_service_image=quay.io/osac/fulfillment-service:pr-123"` | Override a component image for a build under test. |

For example, combine common options in one `EXTRA_VARS` value:

```bash
make e2e INFRA=netris DEPLOY_SUITE=full TEST_SUITE=stretched \
  EXTRA_VARS="$IMG dns_server=<reachable-dns-server>"
```

The comprehensive reference explains which targets consume each option and how
the config file and environment variables supply credentials. `DEPLOY_SUITE`
derives the OSAC profile; do not set `OSAC_PROFILE` directly, and do not set
`bare_metal_services` in `EXTRA_VARS`, which the Makefile derives.

## CaaS tenant networks and persistent workloads

On an installed OSAC lab, `deploy-suite` runs setup, pytest tenant fixtures,
suite networking, and the persistent workload in the required order:

```bash
make deploy-suite DEPLOY_SUITE=caas CAAS_WORKER_COUNT=1
make deploy-suite DEPLOY_SUITE=maas MAAS_WORKER_COUNT=2
make deploy-suite DEPLOY_SUITE=bmaas
```

`deploy-suite` runs setup, then the deploy playbook runs a small OSAC monorepo
pytest to activate the shared E2E fixtures. They ensure tenant1 and tenant2,
their test users, memberships, and default subnets. The CaaS/MaaS deploy playbook
then configures tenant1's Netris network before creating the cluster. BMaaS
instead deploys one BMI in each tenant with a reusable shared DiskImage and a
BMI type matched to its BMHs. `destroy-bmaas` deletes these named BMIs and waits
for their BMHs to be released before tearing down BMaaS; it keeps the shared
DiskImage for reuse. The tenants remain after pytest exits.
To retry the deployment after setup, run
`make deploy-suite DEPLOY_SUITE=maas SUITE_STAGE=workload` (choose the suite in use).
That stage reruns tenant preparation and, for CaaS/MaaS, the Netris network
check before creating resources. It requires the /opt/osac checkout installed
by deploy-osac and the CI profile's Keycloak test users. `make e2e` runs
setup and then the monorepo suite; its fixtures create tenants when suite tests
exist. Missing CaaS/MaaS Netris test paths cause an error rather than a
successful run.
If an earlier deployment used `test-infra-tenant`, first remove its cluster with
`make force-destroy-maas OSAC_TEST_TENANT_NAME=test-infra-tenant` (or the CaaS
variant). This force cleanup scans all remaining orders and is not isolated
to the named tenant. Switching the default does not delete that tenant or its
cluster.

CaaS tenant network preparation uses tenant1's default network. The
Helm values set `netris_tenant_network_cidr` and `netris_tenant_subnet_cidr` to
`192.168.101.0/24`. These defaults also apply to newly onboarded BMaaS tenants;
they do not change the management or BMaaS provisioning networks, or migrate
existing tenant networks.

During `deploy-caas` or `deploy-maas`, `configure-caas-tenant-network` reserves
ingress `192.168.101.28` by setting the tenant V-Net DHCP range to
`192.168.101.100`–`192.168.101.254`, before workers are attached. The settings
are `caas_netris_ingress_ip`, `caas_tenant_dhcp_start`, and
`caas_tenant_dhcp_end`. It refuses to change DHCP on an already populated V-Net.

The CaaS/MaaS deploy playbook resolves the tenant Subnet and VirtualNetwork to
the Netris V-Net `osac-sn-<Subnet id>` and VPC `osac-vn-<VirtualNetwork id>`
when configuring DHCP. Separate isolated VPCs can reuse `.28`, but two clusters
in the same subnet need distinct ingress VIPs.

Existing tenants with a different subnet require network recreation or a fresh
deployment. Merely rerunning setup does not migrate their CIDR.

## Testing OSAC Components

All components live in the osac mono-repo. Override `osac_branch` to test a specific branch, and use image variables to override container images.

The backend Makefile selects `values/<OSAC_PROFILE>/instance.yaml` as the base
values file and layers the Netris overlay from the same profile directory when
one exists. `DEPLOY_SUITE` supplies the profile default shown in the scenario table.

### Examples

**Test a feature branch:**
```bash
make destroy-osac
make deploy-osac EXTRA_VARS='{"osac_branch": "feature-x"}'
```

**Test with image overrides:**
```bash
make destroy-osac
make deploy-osac EXTRA_VARS='{"osac_operator_image": "quay.io/osac-project/osac-operator:pr-42", "osac_aap_image": "quay.io/osac-project/osac-aap-ee:pr-99"}'
```

**Test BMaaS with custom operator images:**
```bash
make destroy-osac
make deploy-osac DEPLOY_SUITE=bmaas \
  EXTRA_VARS="osac_operator_image=quay.io/dmanor/osac-operator:tag bmf_operator_image=quay.io/dmanor/bare-metal-fulfillment-operator:tag"
```

### Netris server labels for BMaaS workers

`prepare-caas-bmaas` installs `test-infra-netris-agents.service` on the hypervisor.
It maps the configured worker VMs' hardware UUIDs to their Netris server names and
sets `netris.server/name` on matching Agents in the OSAC namespace. It runs after
setup exits, so replacement Agents and later scale-out workers receive the label.

The `test-infra-netris-agent-identity` ValidatingAdmissionPolicy blocks binding
only those configured Agents until their label is correct. This prevents the
OSAC controller and AAP networking job from outrunning the label synchronizer.
The policy uses the stable `admissionregistration.k8s.io/v1` API. If the service
is unavailable, those workers wait; inspect it with
`journalctl -u test-infra-netris-agents -n 50`. Setup must run before deployment.
The service and policy remain across cluster teardown for reuse.

BMaaS owns worker power, disks and discovery; setup only reconfigures VMs whose
persistent hardware/disk configuration differs. Existing claimed, deleting, or
non-available BMHs are rejected before VM mutation. Standalone BMaaS retains its
existing defaults; CaaS passes an explicit list of VMs needing reconfiguration.
