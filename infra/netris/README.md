# Netris Backend

Ansible automation to deploy OCP SNO on a [Netris Spectrum-X simulated lab](https://github.com/danmanor/netris-lab) and exercise the deployment scenarios advertised by the backend.

Unless a section says otherwise, backend-only Make targets in this guide run
from `infra/netris/`. Repository orchestration targets such as `make e2e`,
`make setup-suite`, and `make run-tests` run from the `osac-test-infra` root.

> **Using this backend from `osac-test-infra`:** run a complete suite from the
> repository root with `make e2e INFRA=netris SUITE=<suite>`. `SUITE` chooses
> the deployment scenario and OSAC profile; `TEST_SUITE` chooses the test
> target independently. Netris-specific overlays live in the OSAC checkout at
> `osac-installer/values/<profile>/netris.yaml`. See the root README's [deployment scenario
> and test selection overview](../../README.md#deployment-and-test-selection)
> for the generic orchestration contract. The [Netris scenario table below](#netris-deployment-scenarios-test-targets-and-profiles)
> lists this backend's profiles, targets, and defaults. The `make deploy*`
> commands in this backend guide are provisioning building blocks; they do not
> run the OSAC pytest suite.

## Architecture

The [netris-lab](https://github.com/danmanor/netris-lab) deploys a full simulated Spectrum-X GPU cluster network on a single bare-metal host using KVM/libvirt:

- **Netris controller** on K3s — manages all network devices via REST/gRPC API
- **~13 switch VMs** (Cumulus Linux) — leaf/spine fabric for North-South connectivity
- **4 softgate VMs** — provide NAT/L4LB and BGP peering for internet access
- **4 server VMs** (hgx-00 to hgx-03) — simulated GPU servers, managed by Netris

This repo takes the first server (hgx-00), resizes it for OCP, configures Netris networking (VPC, VNet, Subnet), and installs OpenShift SNO on it using the Assisted Installer. The remaining three servers (hgx-01 to hgx-03) form a scenario-dependent host pool: CaaS uses them as discovery agents, while BMaaS setup repurposes them as bare-metal hosts.

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
    └── hgx-01..03 (scenario-dependent host pool)
        ├── CaaS: booted with discovery ISO and registered as fulfillment agents
        └── BMaaS: repurposed as bare-metal hosts for the provisioning flow
```

Internet access for OCP image pulls flows through: hgx-00 → NS VNet → softgate SNAT → host iptables masquerade → internet.

## Prerequisites

- **Bare-metal host** running a RHEL-family distribution (RHEL, Rocky Linux, CentOS Stream, or Fedora) with KVM support
- **System packages** — `dnf install -y git make python3-pip ansible-core && /usr/bin/python3 -m pip install --ignore-installed ansible`. `make setup-infra` then installs the rest of the BM bootstrap (`sshpass`, `tmux`, `policycoreutils-python-utils`), EPEL from the Fedora URL (required on RHEL), and the Python dependencies into the Ansible interpreter's environment.
- **Resources**: ~32+ CPU cores, 128+ GB RAM (lab VMs + OCP SNO VM)
- **Storage**: ~400GB+ for OCP disk images, K3s, and containers. If the root partition is smaller, use a secondary data disk and enable automatic provisioning with `disk_setup_enabled=true` (via `EXTRA_VARS` or in the config). Run `make disk-setup` standalone or pass the variable during setup: `make setup-infra EXTRA_VARS="disk_setup_enabled=true"`
- **Netris license key** — place at `infra/netris/license.key`
- **OSAC/AAP license** — place at `infra/netris/license.zip`
- **OpenShift pull secret** — place at `/root/pull-secret` (or set `pull_secret_path`; download from [console.redhat.com](https://console.redhat.com/openshift/downloads))
- **Config file** — place `infra/netris/config` (INI format, gitignored) with your lab name, AWS credentials, and Quay username/password. The Quay account must have pull access to `quay.io/osac-project/cluster-flavors` for the snapshot flavor download to work. AWS credentials can be obtained from the [CI vault](https://vault.ci.openshift.org/ui/vault/secrets/kv/kv/selfservice%2Fosac%2Fpacket-osac). See Quick Start below. The IAM user needs `route53:ChangeResourceRecordSets`, `route53:ListHostedZones`, and `route53:GetChange` permissions on the hosted zone.

All system packages, tools, and SSH keys are installed automatically by `make setup-infra`. A pre-flight check validates all required files, KVM support, and minimum memory before deploying.

## Quick Start

This section describes the standalone Netris backend workflow: it deploys the
lab and OSAC, but does not run the OSAC mono-repo test suites. To provision and
run tests in one pipeline, use `make e2e INFRA=netris SUITE=<suite>` from the
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

# Fresh install — installs OCP + OSAC with the CaaS profile
make deploy SUITE=caas EXTRA_VARS="bare_metal_services=true"

# OR: Fast deployment (~25 min) — uses a pre-built snapshot with recert.
# Faster but doesn't test the OCP or OSAC installer flows.
make deploy-fast SUITE=caas EXTRA_VARS="bare_metal_services=true"

# Then run a test flow
make setup-caas    # CaaS setup: discover hosts, label agents, configure resource-class map
make deploy-caas   # CaaS: create cluster
```

After deployment, the kubeconfig is at `/root/.kube/config`. The default
`SUITE=caas` deploys the CaaS OSAC profile. Then `make setup-caas` and
`make deploy-caas` prepare and create a CaaS cluster. This backend-only flow
does not run pytest; use the root `make e2e` command for the complete pipeline.

## Make Targets

### Deploy

| Target | Description | Time |
|--------|-------------|------|
| `make deploy` | Fresh-install pipeline: deploy-infra → deploy-ocp → deploy-osac | ~2-3 hrs |
| `make deploy-fast` | Snapshot-refresh pipeline: deploy-infra → deploy-ocp → deploy-osac | ~25 min |
| `make setup-infra` | Install prerequisites, cache images + snapshot flavor, build tools | ~10 min |
| `make deploy-infra` | Deploy netris-lab (K3s, topology, VMs, connectivity) | ~12 min |
| `make deploy-ocp` | Configure Netris networking and deploy OCP SNO from the selected fresh-install or snapshot flavor | ~15 min |
| `make deploy-osac` | Prepare OSAC values + Helm install or snapshot refresh (based on `OSAC_DEPLOY_MODE`) | ~30-60 min |

### CaaS (run after deploy)

| Target | Description | Time |
|--------|-------------|------|
| `make setup-caas` | Discover hosts, label agents, configure the Netris resource-class map and osac CLI | ~30 min |
| `make deploy-caas` | Create CaaS cluster using `ocp_ci_small` template | ~60 min |
| `make setup-maas` | Same as setup-caas with MaaS resource-class / sizing; labels only `MAAS_LABEL_HOSTNAMES` (default h02+h03) with `g5` | ~30 min |
| `make deploy-maas` | Create MaaS cluster (`ocp_4_20_ai_maas` + `-p` template params) | ~60 min |

### Netris deployment scenarios, test targets, and profiles

The following commands are run from the `osac-test-infra` repository root.
`make e2e` performs provisioning, deployment, test prerequisite setup, and tests. A
`make test-*` target skips provisioning and runs tests against an already
prepared cluster.

`SUITE` names the deployment scenario. It drives the Netris setup path,
Ansible behavior, and derived OSAC profile. `TEST_SUITE` selects the OSAC test
path and defaults to `SUITE`. Netris networking tests live under
`tests/e2e/networking/` in the OSAC mono-repo.

| Deployment `SUITE` | Derived OSAC profile and Netris overlay in the OSAC checkout | Target run by `make setup-suite` | Default `TEST_SUITE` / test target |
|---|---|---|---|
| `caas` | `caas-ci` (no Netris overlay) | `setup-caas` | `caas` / `test-caas` |
| `vmaas` | `vmaas-ci` + `osac-installer/values/vmaas-ci/netris.yaml` | `setup-vmaas` | `vmaas` / `test-vmaas` |
| `bmaas` | `bmaas-ci` + `osac-installer/values/bmaas-ci/netris.yaml` | `setup-bmaas` | `bmaas` / `test-bmaas` |
| `full` | `full-ci` + `osac-installer/values/full-ci/netris.yaml` | `setup-full` | `full` / `test-full` |

The test-suite targets are:

| `TEST_SUITE` | Make target | OSAC test path |
|---|---|---|
| `caas` | `test-caas` | `tests/e2e/caas/` |
| `vmaas` | `test-vmaas` | `tests/e2e/networking/vmaas/` |
| `bmaas` | `test-bmaas` | `tests/e2e/networking/bmaas/` |
| `full` | `test-full` | `tests/e2e/networking/` with the documented exclusions |
| `stretched` | `test-stretched` | `tests/e2e/networking/stretched/` |

The PR E2E commands use the same split:

| PR command | Deployment `SUITE` | `TEST_SUITE` | OSAC profile | Check |
|---|---|---|---|---|
| `/test vmaas-netris` | `vmaas` | `vmaas` | `vmaas-ci` | `E2E / VMaaS Netris Full Install` |
| `/test bmaas-netris` | `bmaas` | `bmaas` | `bmaas-ci` | `E2E / BMaaS Netris Full Install` |
| `/test stretched-netris` | `full` | `stretched` | `full-ci` | `E2E / Stretched Netris Full Install` |

This table describes the root orchestration targets. Later backend-only target
examples in this guide assume the working directory is `infra/netris/`; from
the repository root, use orchestration targets such as `make e2e`,
`make setup-suite`, and `make run-tests` with `INFRA=netris`.

`TEST_SUITE` may differ from the deployment `SUITE`. For example,
`SUITE=full TEST_SUITE=stretched` deploys the full-ci profile and runs only the
shared-subnet test at `tests/e2e/networking/stretched/`. Do not set
`OSAC_PROFILE`; the backend derives it from `SUITE`.

Run the whole pipeline with one command:

```bash
make e2e INFRA=netris SUITE=full \
  OSAC_DEPLOY_MODE=fresh EXTRA_VARS="bare_metal_services=true"

# Use the same full-ci deployment and run only stretched tests
make e2e INFRA=netris SUITE=full TEST_SUITE=stretched \
  OSAC_DEPLOY_MODE=fresh EXTRA_VARS="bare_metal_services=true"
```

Or choose a focused networking flow:

```bash
make e2e INFRA=netris SUITE=vmaas
make e2e INFRA=netris SUITE=bmaas EXTRA_VARS="bare_metal_services=true"
```

The variables select different parts of the run:

- `INFRA=netris` selects this backend.
- `SUITE=...` selects the deployment scenario and therefore backend setup,
  Ansible behavior, and OSAC profile. The profile is derived from the suite
  (`vmaas` → `vmaas-ci`, `bmaas` → `bmaas-ci`, `full` → `full-ci`).
- `TEST_SUITE=...` selects the test target independently. It defaults to
  `SUITE`; use `TEST_SUITE=stretched` with `SUITE=full` to run only the
  shared-subnet VMaaS↔BMaaS test against a full-ci deployment.
- `OSAC_DEPLOY_MODE=fresh|snapshot` chooses a fresh install or snapshot-based
  OSAC refresh. `fresh` is the default. Snapshot mode supports only
  `SUITE=caas`; VMaaS, BMaaS, and full
  require fresh mode so the Netris overlays are applied.
- `EXTRA_VARS=...` passes Ansible variables, such as enabling BMaaS BMC
  services. Combine DNS and other overrides in the same value.

To run only tests after the cluster and OSAC are ready, use the target mapped
from `TEST_SUITE`. For example, `make test-vmaas` selects Netris VMaaS
networking tests. In the pipeline, use:

```bash
make run-tests INFRA=netris SUITE=vmaas
```

This loads the environment saved by `deploy-osac` and invokes `test-vmaas`.
For stretched tests against a full-ci deployment, use
`make run-tests INFRA=netris SUITE=full TEST_SUITE=stretched`.

The VMaaS Netris profile configures CUDN EVPN on single-node OCP 4.22 with
OVNKubernetes and platform type `None`. It reuses h00's Netris-managed leaf-1
link (`eth10` ↔ `swp1s0`) for the `/31` underlay; leaf-0's `eth9` remains the
`br-ex` data path. The deployment correlates interface MAC addresses across
Netris, the leaf, and libvirt, configures the EVPN peer, and advertises routes
from OCP. It does not install or rely on the Netris host agent.

Netris/CUDN EVPN settings are overlays inside the existing OSAC service profile
directories: `osac-installer/values/vmaas-ci/netris.yaml`,
`osac-installer/values/bmaas-ci/netris.yaml`, and
`osac-installer/values/full-ci/netris.yaml`. The backend layers the selected
overlay after the base profile and generates `netris-runtime.yaml` with lab
controller, site, VPC, and credential values. The stretched test suite runs
against the `full-ci` deployment and overlay. BMaaS
retains its per-BMH MAC mapping. `setup-bmaas` publishes the BMI guest interface
as `OSAC_BMI_NETWORK_INTERFACE`; BMaaS and stretched tests consume that value
instead of hardcoding a server-port name.

`full-ci` enables all four OSAC services (CaaS, VMaaS, BMaaS, and MaaS), but
`setup-full` currently prepares only BMaaS and VMaaS networking. CaaS/MaaS
tenant-cluster preparation is separate because those flows reuse the Netris
lab's discovery VMs. `TEST_SUITE=full` excludes CaaS lifecycle tests, the slow
cross-tenant BMaaS networking class, and ExternalIP tests that require a
separately provisioned return path.

### Bare-metal prerequisites

The `bare_metal_services` Ansible variable defaults to `false`. Set it to
`true` when the selected profile uses bare-metal services (CaaS and BMaaS do)
and the cluster does not already have the Metal3 `Provisioning` resource
configured with `watchAllNamespaces: true`, or when the lab needs Netris BMC
support. It prepares that Metal3 setting, starts `sushy-tools`, and reserves
the OCP VM's BMC address. The VMaaS, BMaaS, and stretched full-install PR
workflows currently set it explicitly.

VM-backed tests require `KUBECONFIG`, `OSAC_NAMESPACE`, and
`OSAC_VM_KUBECONFIG` in `.env.infra`; the VMaaS, full, and stretched test
contracts check these variables. For a single-cluster profile, the VM kubeconfig
may point to the hub kubeconfig. Configure a separate path when tests use a
separate workload cluster.

`setup-bmaas` adds `OSAC_BMH_SSH_HOSTS`, `OSAC_BMI_INSTANCE_TYPE`, and
`OSAC_BMI_NETWORK_INTERFACE` to `.env.infra`. The BMaaS, full, and stretched
contracts require these values; the root `run-tests` target checks them after
the selected deployment scenario has been prepared.

Suite fixtures create their own tenant `VirtualNetwork`, `Subnet`, `SecurityGroup`, and `ExternalIPPool` resources. Lab setup owns the cluster/fabric prerequisites and must not pre-create fixture-owned tenant resources. PodNetwork route advertisements support shared-subnet VMaaS/BMaaS traffic. ExternalIP and EgressIP dataplane tests require a separately provisioned and acknowledged route/return path for `172.27.0.0/16`; enable `ocp_vmaas_external_ip_tests_enabled` only after verifying that path.

To collect diagnostics from an existing deployment, run `make gather-suite INFRA=netris SUITE=vmaas`. The CUDN EVPN captures are written under `infra/netris/logs/cudn-evpn/`. Collection is read-only and best-effort; unavailable APIs or peers get an explicit marker in the output. Captured Kubernetes YAML, FRR logs, and Netris responses are scrubbed for credential-like values before they are written.

### Destroy

| Target | Description |
|--------|-------------|
| Root `make destroy-suite SUITE=<scenario>` | Safely dispatch a deployment-scenario cleanup; rejects `SUITE=full` because that would destroy the entire lab |
| `make destroy-full` | Tear down everything: OSAC + OCP artifacts + netris-lab |
| `make destroy-osac` | Tear down OSAC: helm releases, operators, CRDs, namespaces |
| `make destroy-ocp` | Reset OCP for reinstall: delete cluster, recreate disk, boot VM |
| `make destroy-infra` | Tear down netris-lab (VMs, K3s, topology) |
| `make destroy-caas` | CaaS teardown: stop discovery VMs, remove disks/ISO, delete namespace, clean DNS |
| `make force-destroy-caas` | destroy-caas + strip stuck orders/namespaces, wipe VMs, Netris orphans |
| `make destroy-maas` | destroy-caas with MaaS cluster/resource-class overrides |
| `make force-destroy-maas` | force-destroy-caas with MaaS overrides |
| `make destroy-bmaas` | BMaaS teardown: remove BMH/K8s resources, stop BMaaS VMs, clean disks, delete BMH namespace |

### Recovery and Utilities

| Target | Description |
|--------|-------------|
| `make connectivity` | Re-run lab connectivity (VPN, BGP, softgates) without full redeploy |
| `make run-osac-setup` | Re-run just `make install` in osac-installer (after prep-osac has run) |
| `make prep-osac` | Ansible-only OSAC prep (clone, patch values, copy secrets) — no Helm install |
| `make post-osac` | Scale down MCE operators and filter OS images to target version |
| `make vendor-update` | Refresh vendored Ansible collections |
| `make lint` | Run ansible-lint |
| `make gather-infra` | Gather diagnostic info from the cluster |

### Typical Workflows

**First deploy on a fresh server (fast path — snapshot):**
```bash
make setup-infra          # install prerequisites, cache images + snapshot flavor, build tools
make deploy-fast    # deploy lab + OCP+OSAC from snapshot (~25 min total)
```

**First deploy on a fresh server (full path — from scratch):**
```bash
make setup-infra          # install prerequisites, cache images, build tools
make deploy         # deploy lab + OCP + OSAC (~2-3 hrs)
```

**Fast deploy with image overrides (test a PR build):**
```bash
make deploy-fast EXTRA_VARS="fulfillment_service_image=quay.io/osac/fulfillment-service:pr-123"
```

**Deploy OSAC with BMaaS values and image overrides:**
```bash
make deploy-ocp SUITE=bmaas
make deploy-osac SUITE=bmaas \
  EXTRA_VARS="osac_operator_image=quay.io/dmanor/osac-operator:tag bmf_operator_image=quay.io/dmanor/bare-metal-fulfillment-operator:tag bare_metal_services=true aap_project_git_branch=my-feature-branch"
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
make setup-caas     # discover hosts, label agents, configure resource-class map
make deploy-caas    # create cluster
```

**Deploy MaaS after OSAC is up:**
```bash
make setup-maas     # same discovery path; labels only MAAS_LABEL_HOSTNAMES (default h02+h03) with g5
make deploy-maas    # create cluster with ocp_4_20_ai_maas + enable_* params
```

**Rebuild from scratch:**
```bash
make destroy-full   # tear down everything
make deploy-fast    # full redeploy (snapshot path)
```

**Bare-metal host redeployment pipeline (`redeploy-server.sh`):**
```bash
# CaaS + fresh OSAC install (default)
make redeploy-fresh

# CaaS + snapshot OSAC refresh (same mode as deploy-fast)
make redeploy-fresh OSAC_DEPLOY_MODE=snapshot

# MaaS + snapshot refresh (grows h00 disk; setup-maas / deploy-maas)
make redeploy-fresh SUITE=maas OSAC_DEPLOY_MODE=snapshot
```

### Bare-Metal Lab Deployment (from laptop)

For persistent bare-metal servers behind NAT (e.g., Red Hat lab infrastructure), use the remote deploy workflow. This handles image caching, bootstrapping, and resilient multi-step deploys from your laptop.

**One-time setup:**
```bash
git clone --recurse-submodules https://github.com/osac-project/osac-test-infra.git
cd osac-test-infra/infra/netris

# Create env file from template (one per server, gitignored)
cp scripts/env.sh.example scripts/env.sh
# Edit scripts/env.sh with: SERVER IP, PASSWORD, LAB_NAME, secrets paths, AWS keys
```

**Deploy:**
```bash
source scripts/env.sh && make deploy-jump
```

This single command:
1. Pre-caches container images on your laptop (with retries, avoids rate limits)
2. Rsyncs the repo + cached images to the server
3. Bootstraps packages (EPEL, Ansible, pip deps)
4. Sets up data disk (partition, mount, symlinks, SELinux)
5. Destroys any previous deployment
6. Runs the complete pipeline: setup-infra → deploy-infra → deploy-ocp → deploy-osac → setup-caas → deploy-caas → post-install

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
| `make redeploy-fresh` | Destroy + wipe progress + complete deployment sequence (`SUITE`, `OSAC_DEPLOY_MODE`) |
| `make redeploy-continue` | Resume failed redeploy from last incomplete step |
| `make disk-setup` | Auto-detect and mount data disk |
| `make post-install` | Fix Keycloak/UI + generate access doc |
| `make access-doc` | Generate handover documentation only |
| `make health-check` | Quick status verification |

`redeploy-server.sh` steps: `setup-infra` → `deploy-infra` → `deploy-ocp` → `deploy-osac` → `setup-caas|setup-maas` → `deploy-caas|deploy-maas` → `post-install`. Pass `OSAC_DEPLOY_MODE=snapshot` (or `--snapshot`) for refresh instead of Helm install; default is `fresh`.

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

`make deploy-ocp` deploys OCP SNO from a pre-built VM snapshot. It uses a golden qcow2 disk image (cached during `make setup-infra`) and regenerates the cluster's identity (certificates, hostname, IP, domain) using [recert](https://github.com/rh-ecosystem-edge/recert). In `fresh` mode, it uses the `sno-4-22` flavor (OCP only, no OSAC); in `snapshot` mode, the `caas` flavor (OCP+OSAC baked in).

The flow runs three Ansible roles in sequence:

1. **`configure-netris`** — discovers h00's Netris server ports by correlating Netris links, leaf-interface MAC addresses, and libvirt tunnel endpoints. It preserves a verified OCP VNet attachment or chooses an unused verified data port, excluding underlay links. It then configures the VPC/VNet and SNAT/DNAT rules.
2. **`configure-dns`** — creates Route 53 DNS records and local dnsmasq config for the cluster domain
3. **`restore-ocp`** — creates copy-on-write disk overlays, grows the OS and LVM disks, and writes pre-boot config. At boot, the recert service finds each guest NIC by its discovered MAC, applies the OCP address and default route to the data NIC, and verifies gateway reachability through the management path before continuing over DNAT SSH. It then waits for cluster health.

The server-side Netris port names (for example, `eth9` for data and `eth10` for underlay) are not Linux interface names inside h00. The deployment discovers the guest MAC for each port from Netris, the leaf interface, and libvirt, then resolves the current guest interface name by MAC. It also resolves the management interface from its IP before applying reverse-path filtering. It fails closed if a port, tunnel, or guest MAC mapping is ambiguous. Cloudsim's `/root/.cloudsim_aliases` provides the management IP; set `ocp_vm_mgmt_ip` only if those aliases are unavailable.

The snapshot flavor is pulled and cached during `make setup-infra` (one-time ~60GB download). Subsequent deploys use copy-on-write overlays, so only changed blocks are written.

OSAC itself is handled by `make deploy-osac` (not part of `deploy-ocp`). In `OSAC_DEPLOY_MODE=snapshot`, that runs `refresh-osac` → `post-refresh-osac` → **`patch-osac-refresh`**, which wires Netris/SSH/socat and then applies `config-as-code-ig`’s `AAP_PROJECT_GIT_*` pin onto the live AAP Project and launches `osac-config-as-code` once (snapshot AAP DB keeps the bake-time `scm_branch`; waiting for the hourly CaC schedule is too late for `setup-maas` / `publish-templates`).

## How deploy-osac Works

`make deploy-osac` runs in three phases:

1. **`prep-osac`** (Ansible) — clones the OSAC mono-repo, validates the selected profile and its Netris overlay, resolves the OCP release CLI image, and writes the generated lab-specific values to `values/<profile>/netris-runtime.yaml`. That file includes credentials, is mode `0600`, and is ignored by Git. The role also copies the license and pull secret into the profile directory and sets up a socat forwarder for OCP ingress on port 9444.

2. **`run-osac-setup`** (shell) — runs `make install-infra` and then `make install-osac` in `osac-installer`. It passes `OSAC_PROFILE` plus the profile's static `netris.yaml` and generated `netris-runtime.yaml` overlays. The infrastructure install deploys the required operators and prerequisites; the OSAC install deploys the application chart and runs its hooks.

3. **`post-osac`** (Ansible) — skipped in fresh mode. In snapshot mode, scales down MCE operators and filters `OS_IMAGES` to only the target OCP version.

## Configuration

All parameters are in [`inventory/group_vars/all.yml`](inventory/group_vars/all.yml). Override any variable via `EXTRA_VARS`:

```bash
make deploy-ocp EXTRA_VARS="ocp_version=4.22"
make deploy-osac EXTRA_VARS='{"osac_branch": "feature-x"}'
```

### Key Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ocp_version` | `4.22` | OpenShift version |
| `ocp_cluster_name` | `ocp-sno` | OCP cluster name |
| `lab_name` | `$LAB_NAME` or `default` | Per-lab subdomain prefix (avoids DNS collisions) |
| `dns_hosted_zone` | `ecoeng-osac-ci.devcluster.openshift.com` | Route 53 hosted zone |
| `ocp_base_domain` | `<lab_name>.<dns_hosted_zone>` | DNS base domain (derived) |
| `ocp_server_vcpu` | `20` | OCP VM vCPUs |
| `ocp_server_memory_gb` | `64` | OCP VM RAM (GB) |
| `ocp_subnet_cidr` | `192.168.40.0/24` | OCP VNet subnet |
| `ocp_dnat_ip` | `198.51.100.2` | DNAT IP for OCP API/apps access |
| `osac_namespace` | `osac-e2e-ci` | OSAC Kubernetes namespace |
| `osac_values_file` | `values/$(OSAC_PROFILE)/instance.yaml` | Base values file selected by the backend Makefile from `OSAC_PROFILE` |
| `osac_branch` | `main` | osac mono-repo branch |
| `netris_username` | `netris` | Netris API username |
| `netris_password` | *(required)* | Netris API password — set in your local config, validated at deploy time |
| `ew_fabric_enable` | `0` | East-West fabric (0=NS only) |
| `dns_server` | `8.8.8.8` | Upstream DNS server for the lab (override in environments where public DNS is blocked) |
| `caas_cluster_template` | `osac.templates.ocp_ci_small` | Cluster template for CaaS cluster creation |
| `caas_cluster_name` | `caas-ci-cluster` | CaaS cluster name |
| `caas_host_type_id` | `ci-worker` | Resource class for CaaS agents |
| `snapshot_flavor_image` | `quay.io/osac-project/cluster-flavors:caas` | OCI image containing the snapshot flavor |
| `snapshot_osac_namespace` | `osac-e2e-ci` | OSAC namespace baked into the snapshot |
| `snapshot_osac_values_file` | `values/caas-ci/instance.yaml` | Base values file staged for snapshot refresh |

```bash
make deploy-ocp EXTRA_VARS="ocp_version=4.18"
make setup-caas EXTRA_VARS="caas_cluster_name=my-cluster caas_discovery_vcpu=8"

# MaaS: default 10 vCPU / 20GiB for h02–h03; h01 stays 4 vCPU / 16GiB via overrides.
# Grow workers only (e.g. real model headroom) without resizing h01:
make setup-maas MAAS_DISCOVERY_MEMORY_MB=49152
```

#### Lab & Identity

| Variable | Default | Description | Tested |
|----------|---------|-------------|--------|
| `lab_name` | `$LAB_NAME` or `default` | Per-lab subdomain prefix (avoids DNS collisions) | yes |
| `dns_hosted_zone` | `ecoeng-osac-ci.devcluster.openshift.com` | Route 53 hosted zone | yes |
| `ew_fabric_enable` | `0` | East-West fabric (0=NS only, 1=full EW+NS) | no |

#### OCP Installation

| Variable | Default | Description | Tested |
|----------|---------|-------------|--------|
| `ocp_version` | `4.22` | OpenShift version | default |
| `ocp_cluster_name` | `ocp-sno` | OCP cluster name | defaults only |
| `ocp_server_vcpu` | `20` | OCP VM vCPUs | yes (16) |
| `ocp_server_memory_gb` | `64` | OCP VM RAM in GB | yes (48) |
| `ocp_install_disk_gb` | `300` | OCP install disk size in GB | defaults only |
| `ocp_lvm_disk_gb` | `300` | LVM storage disk size in GB | defaults only |
| `ocp_snapshot_disk_gb` | `300` | OCP snapshot OS disk size in GB | defaults only |

#### Netris Networking

| Variable | Default | Description | Tested |
|----------|---------|-------------|--------|
| `ocp_vpc_name` | `ocp-sno` | Netris VPC name | defaults only |
| `ocp_subnet_cidr` | `192.168.40.0/24` | OCP VNet subnet | defaults only |
| `ocp_node_ip` | `192.168.40.2` | OCP node static IP | defaults only |
| `ocp_snat_ip` | `198.51.100.1` | SNAT translated IP | defaults only |
| `ocp_dnat_ip` | `198.51.100.2` | DNAT IP for API/apps access | defaults only |
| `osac_external_ip_pool_cidr` | `198.51.100.24/29` | OSAC tenant ExternalIPPool CIDR; host routes/masquerades it toward the softgate. Must not overlap the mgmt nat/l4lb/bgp pools. OSAC creates the Netris allocation itself. | defaults only |
| `netris_username` | `netris` | Netris API username | defaults only |
| `netris_password` | *(required)* | Netris API password — no default, set in local config | yes |
| `dns_server` | `8.8.8.8` | Upstream DNS server threaded through cloud-init, Netris topology, dnsmasq/DHCP, and the OCP NMState resolver. Override in environments where public DNS (8.8.8.8, 1.1.1.1) is unreachable | yes |

Set `dns_server` when the lab host cannot reach public DNS resolvers. Override per-run via `EXTRA_VARS`, or set it permanently in [`inventory/group_vars/all.yml`](inventory/group_vars/all.yml):

```bash
# Per-run override (highest precedence)
make deploy EXTRA_VARS="dns_server=10.0.0.1"
```

```yaml
# Persistent: inventory/group_vars/all.yml
dns_server: "10.0.0.1"
```

#### OSAC Deployment

| Variable | Default | Description | Tested |
|----------|---------|-------------|--------|
| `osac_namespace` | `osac-e2e-ci` | OSAC Kubernetes namespace | yes |
| `osac_values_file` | `values/$(OSAC_PROFILE)/instance.yaml` | Base values file selected by the backend Makefile from `OSAC_PROFILE` | default |
| `osac_repo` | `https://github.com/osac-project/osac.git` | osac mono-repo git URI | defaults only |
| `osac_branch` | `main` | osac mono-repo git branch | defaults only |

#### Container Image Overrides

| Variable | Default | Description | Tested |
|----------|---------|-------------|--------|
| `osac_operator_image` | `""` | osac-operator container image override | no |
| `fulfillment_service_image` | `""` | fulfillment-service container image override | no |
| `osac_aap_image` | `""` | osac-aap bootstrap image override | no |
| `osac_ui_image` | `""` | osac-ui container image override | no |
| `bmf_operator_image` | `""` | bare-metal-fulfillment-operator container image override | no |
| `aap_project_git_uri` | `""` | AAP config-as-code project git URI override (defaults to installer value) | no |
| `aap_project_git_branch` | `""` | AAP config-as-code project git branch override (test PR playbooks/collections) | no |

#### Snapshot Deployment (CaaS only)

Snapshot refresh currently supports only `SUITE=caas`, which maps to `caas-ci`.
The VMaaS, BMaaS, and full deployments require generated Netris runtime
values and per-profile overlays, which are applied by the fresh-install path.
Use `OSAC_DEPLOY_MODE=fresh` for those scenarios.

The account configured with `quay_username` and `quay_password` must have pull access to
`quay.io/osac-project/cluster-flavors`. Setup logs in with these credentials when it downloads
the flavor image; it reports an actionable error if the credentials are missing or lack access.

| Variable | Default | Description | Tested |
|----------|---------|-------------|--------|
| `snapshot_flavor_image` | `quay.io/osac-project/cluster-flavors:{caas,sno}-4-22` | OCI image containing the snapshot flavor (derived from `osac_deploy_mode`) | defaults only |
| `snapshot_recert_image` | `quay.io/osac-project/recert:latest` | Recert container image | defaults only |
| `snapshot_osac_namespace` | `osac-e2e-ci` | OSAC namespace baked into the snapshot | defaults only |
| `snapshot_osac_values_file` | `values/caas-ci/instance.yaml` | Base values file staged for OSAC refresh | default |

#### CaaS Configuration

| Variable | Default | Description | Tested |
|----------|---------|-------------|--------|
| `caas_cluster_name` | `caas-ci-cluster` | CaaS cluster name | yes (custom) |
| `caas_cluster_template` | `osac.templates.ocp_ci_small` | Cluster template for CaaS | defaults only |
| `caas_host_type_id` | `ci-worker` | Resource class label for CaaS agents | defaults only |
| `caas_discovery_vcpu` | `4` | Discovery VM vCPUs | yes (8) |
| `caas_discovery_vcpu_overrides` | `{}` | Per-VM vCPU map (VM name → count); empty = use `caas_discovery_vcpu` | defaults only |
| `caas_discovery_memory_mb` | `16384` | Discovery VM memory in MB | yes (32768) |
| `caas_discovery_memory_mb_overrides` | `{}` | Per-VM memory map (VM name → MB); empty = use `caas_discovery_memory_mb` | defaults only |
| `caas_discovery_disk_gb` | `100` | Discovery VM disk in GB | yes (150) |
| `caas_discovery_vm_patterns` | `[hgx-pod00-su0-h01..03]` | VM names for CaaS discovery | defaults only |

## Testing OSAC Components

All components live in the osac mono-repo. Override `osac_branch` to test a specific branch, and use image variables to override container images.

The backend Makefile selects `values/<OSAC_PROFILE>/instance.yaml` as the base
values file and layers the Netris overlay from the same profile directory when
one exists. `SUITE` supplies the profile default shown in the scenario table.

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
make deploy-osac SUITE=bmaas \
  EXTRA_VARS="osac_operator_image=quay.io/dmanor/osac-operator:tag bmf_operator_image=quay.io/dmanor/bare-metal-fulfillment-operator:tag bare_metal_services=true"
```
