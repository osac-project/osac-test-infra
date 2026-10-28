# netris-test-infra

Ansible automation to deploy OCP SNO on a [Netris Spectrum-X simulated lab](https://github.com/danmanor/netris-lab) and exercise the suites currently advertised by the backend.

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
- **Storage**: ~400GB+ for OCP disk images, K3s, and containers. If the root partition is smaller, use a secondary data disk and enable automatic provisioning with `disk_setup_enabled=true` (via `EXTRA_VARS` or in the config). Run `make disk-setup` standalone or pass the variable during setup: `make setup-infra EXTRA_VARS="disk_setup_enabled=true"`
- **Netris license key** — place at repo root as `license.key`
- **OSAC/AAP license** — place at repo root as `license.zip`
- **OpenShift pull secret** — place at `/root/pull-secret` (or set `pull_secret_path`; download from [console.redhat.com](https://console.redhat.com/openshift/downloads))
- **Config file** — place `infra/netris/config` (INI format, gitignored) with your lab name, AWS credentials, and Quay username/password. The Quay account must have pull access to `quay.io/osac-project/cluster-flavors` for the snapshot flavor download to work. AWS credentials can be obtained from the [CI vault](https://vault.ci.openshift.org/ui/vault/secrets/kv/kv/selfservice%2Fosac%2Fpacket-osac). See Quick Start below. The IAM user needs `route53:ChangeResourceRecordSets`, `route53:ListHostedZones`, and `route53:GetChange` permissions on the hosted zone.

All system packages, tools, and SSH keys are installed automatically by `make setup-infra`. A pre-flight check validates all required files, KVM support, and minimum memory before deploying.

## Quick Start

```bash
git clone --recurse-submodules https://github.com/danmanor/netris-test-infra.git
cd netris-test-infra

# Place prerequisites
cp /path/to/license.key ./license.key
cp /path/to/license.zip ./license.zip
cp /path/to/pull-secret /root/pull-secret

# Create infra/netris/config (unique lab name, AWS credentials, and Quay credentials)
# lab_name becomes a subdomain under the shared hosted zone (e.g., jsmith.ecoeng-osac-ci.devcluster.openshift.com)
cat > infra/netris/config << EOF
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

# Full deployment — installs OCP + OSAC from scratch
make deploy

# OR: Fast deployment (~25 min) — uses a pre-built snapshot with recert.
# Faster but doesn't test the OCP or OSAC installer flows.
make deploy-fast

# Or leave a CaaS cluster running
make deploy-suite SUITE=caas  # setup, pytest tenants, Netris tenant network, persistent cluster
```

After deployment, the kubeconfig is at `/root/.kube/config`.

## Make Targets

### Deploy

| Target | Description | Time |
|--------|-------------|------|
| `make deploy` | Full pipeline: deploy-infra → deploy-ocp → deploy-osac (fresh Helm install) | ~2-3 hrs |
| `make deploy-fast` | Snapshot pipeline: deploy-infra → deploy-ocp → deploy-osac (snapshot refresh) | ~25 min |
| `make setup-infra` | Install prerequisites, cache images + snapshot flavor, build tools | ~10 min |
| `make deploy-infra` | Deploy netris-lab (K3s, topology, VMs, connectivity) | ~12 min |
| `make deploy-ocp` | Configure Netris networking + restore OCP SNO from snapshot | ~15 min |
| `make deploy-osac` | Prepare OSAC values + Helm install or snapshot refresh (based on `OSAC_DEPLOY_MODE`) | ~30-60 min |

The `caas` and `maas` suites use BMaaS-backed workers and the persistent
cluster roles.

## CaaS (run after deploy)

| Target | Description | Time |
|--------|-------------|------|
| `make setup-caas` | Prepare BMaaS worker VMs, register worker types, DiskImage, Netris ExternalIPPool, and catalog | ~30 min |
| `make deploy-caas` | Create the CaaS catalog cluster with one `ci-worker` BareMetalInstanceType | ~60 min |
| `make setup-maas` | Configure all three BMaaS worker VMs as 10 vCPU / 20GiB `g5` hosts; deploy-maas requests two | ~30 min |
| `make deploy-maas` | Create the MaaS catalog cluster with two `g5` BareMetalInstanceTypes and template defaults | ~60 min |
| `make deploy-suite SUITE=caas` or `SUITE=maas` | Run setup, shared pytest tenant fixtures, tenant Netris networking, and persistent cluster deployment | setup + deploy |
| `make deploy-suite SUITE=bmaas` | Run BMaaS setup, shared pytest tenant fixtures, and create one persistent BMI in each tenant | setup + provisioning |
| `make e2e INFRA=netris SUITE=caas_networking` or `maas_networking` (from repo root) | Run setup and then the OSAC pytest suite when it exists; the suite fixtures will create tenants | varies |

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

### VMaaS and BMaaS shared-subnet networking

The Netris VMaaS and full paths use the single-node OCP 4.22 OVNKubernetes cluster with the `None` platform type. For `SUITE=vmaas`, `SUITE=vmaas_networking`, `SUITE=stretched_networking`, or `SUITE=full`, `deploy-infra` reuses h00's Netris-managed leaf-1 link (`eth10` ↔ `swp1s0`) for the `/31` underlay and enables underlay on that link; leaf-0's `eth9` link remains the `br-ex` data path. h00's Linux interface names are resolved from the MAC addresses correlated across Netris, the leaf interface, and the libvirt tunnel, so the guest NIC enumeration may change without changing which path carries data or underlay. It sets h00's server ASN and EVPN-VTEP inventory role so Netris can generate the leaf-side peer from the managed link. `deploy-ocp` validates the OpenShift inputs and writes an ignored, site-specific `infra/netris/.evpn-values.yaml`; the fresh `deploy-osac` path passes those values to `osac-installer` `install-infra`, which configures OVN route advertisements, NMState, the unmanaged VTEP, node uplink addresses, and OCP-side FRR. The separate Netris deployment configures the fabric side; this flow does not install or rely on the Netris host agent. `make deploy-fast` uses a snapshot refresh and does not run the installer, so it does not exercise this configuration path; use a fresh install to test it. No standalone VLAN-backed BGP object is created for the routed `/31`. The OSAC `cudn-evpn-netris-test` profile enables both VMaaS and BMaaS and selects Netris plus CUDN EVPN, allowing both instance types to attach to one subnet. BMaaS continues to use the Netris server-port names and its separate per-BMH MAC mapping for `eth9`/`eth10`; assigning h00's `eth10` to the underlay does not change h01–h03 provisioning NIC selection. `setup-bmaas` publishes the configured BMI guest interface name as `OSAC_BMI_NETWORK_INTERFACE`; BMaaS and stretched networking tests consume that value instead of embedding a server-port name in the test code.

The focused VMaaS Netris networking E2E invocation is:

```bash
make e2e INFRA=netris SUITE=vmaas_networking OSAC_DEPLOY_MODE=fresh
```

`SUITE=vmaas_networking` selects the `cudn-evpn-netris-test` OSAC profile by default and runs only `tests/e2e/networking/vmaas` with the `requires_netris` and `requires_netris_vmaas` markers. `SUITE=vmaas` uses the same profile and runs the VMaaS test tree while excluding the common `requires_netris` marker. Set `OSAC_PROFILE=cudn-evpn-netris-test` explicitly to pin the profile. It selects the CUDN EVPN and Netris managers; `deploy-infra` provisions the fabric side, `deploy-ocp` validates inputs and generates the installer values, and a fresh `deploy-osac` run configures the OpenShift side through `install-infra`.

The focused BMaaS Netris networking invocation is:

`make e2e INFRA=netris SUITE=bmaas_networking OSAC_PROFILE=bmaas-ci OSAC_DEPLOY_MODE=fresh`

`SUITE=bmaas_networking` runs only `tests/e2e/networking/bmaas` with the `requires_netris` and `requires_netris_bmaas` markers. The standard BMaaS suite excludes all Netris-marked tests.

The stretched VMaaS↔BMaaS Netris invocation is:

```bash
make e2e INFRA=netris SUITE=stretched_networking OSAC_PROFILE=full-ci OSAC_DEPLOY_MODE=fresh
```

`SUITE=stretched_networking` uses the `full-ci` profile, prepares both CaaS and BMaaS prerequisites plus the CUDN EVPN underlay, and runs only `tests/e2e/networking/stretched` with the generic `requires_stretched_networking` marker. The same test is also included in `SUITE=full`. Both contracts require the BMI guest interface name published by BMaaS setup.

### Full-profile E2E

```bash
make e2e INFRA=netris SUITE=full OSAC_PROFILE=full-ci OSAC_DEPLOY_MODE=fresh
```

`SUITE=full` uses the `full-ci` OSAC profile and configures the same h00 underlay and CUDN EVPN prerequisites. `setup-full` prepares BMaaS-backed CaaS prerequisites, reapplies BMaaS defaults, then runs the VMaaS prerequisite check. `test-full` runs the serial OSAC mono-repo E2E set with its configured exclusions; it includes only the generic `requires_stretched_networking` shared-subnet test and excludes the dedicated VMaaS and BMaaS Netris cases. The Netris PR workflow exposes `bmaas-netris`, `vmaas-netris`, and `stretched-netris`; the stretched check runs only the shared-subnet BMI/ComputeInstance test.

VMaaS requires `KUBECONFIG`, `OSAC_NAMESPACE`, and `OSAC_VM_KUBECONFIG` in `.env.infra`. For the single-cluster profile, the VM kubeconfig may default to the hub kubeconfig; configure a separate path when a separate workload cluster is used.

Suite fixtures create their own tenant `VirtualNetwork`, `Subnet`, `SecurityGroup`, and `ExternalIPPool` resources. Lab setup owns the cluster/fabric prerequisites and must not pre-create fixture-owned tenant resources. PodNetwork route advertisements support shared-subnet VMaaS/BMaaS traffic. ExternalIP and EgressIP dataplane tests require a separately provisioned and acknowledged route/return path for `172.27.0.0/16`; enable `ocp_vmaas_external_ip_tests_enabled` only after verifying that path.

To collect diagnostics from an existing deployment, run `make gather-suite INFRA=netris SUITE=vmaas_networking`. The CUDN EVPN captures are written under `infra/netris/logs/cudn-evpn/`. Collection is read-only and best-effort; unavailable APIs or peers get an explicit marker in the output. Captured Kubernetes YAML, FRR logs, and Netris responses are scrubbed for credential-like values before they are written.

### Destroy

| Target | Description |
|--------|-------------|
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

**Deploy with BMaaS values and image overrides:**
```bash
make deploy-ocp
make deploy-osac OSAC_VALUES_FILE=values/bmaas-ci/values.yaml \
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
make deploy-fast    # full redeploy (snapshot path)
```

**BM full pipeline (redeploy-server.sh):**
```bash
# CaaS + fresh OSAC install (default)
make redeploy-fresh

# CaaS + snapshot OSAC refresh (same mode as deploy-fast)
make redeploy-fresh OSAC_DEPLOY_MODE=snapshot

# MaaS + snapshot refresh (setup-maas / deploy-maas)
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
# Edit scripts/env.sh with: SERVER IP, PASSWORD, LAB_NAME, NETRIS_PASSWORD, secrets paths, AWS keys
# For private snapshot flavor pulls, also set QUAY_USERNAME and QUAY_PASSWORD
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
| `make redeploy-fresh` | Destroy + wipe progress + full pipeline (`SUITE`, `OSAC_DEPLOY_MODE`) |
| `make redeploy-continue` | Resume failed redeploy from last incomplete step |
| `make disk-setup` | Auto-detect and mount data disk |
| `make post-install` | Fix Keycloak/UI + generate access doc |
| `make access-doc` | Generate handover documentation only |
| `make health-check` | Quick status verification |

`redeploy-server.sh` steps: `setup-infra` → `deploy-infra` → `deploy-ocp` → `deploy-osac` → `post-install` → `setup-caas|setup-maas|setup-bmaas` → `deploy-caas|deploy-maas|deploy-bmaas` → `access-doc`. Pass `OSAC_DEPLOY_MODE=snapshot` (or `--snapshot`) for refresh instead of Helm install; default is `fresh`.

`post-install` applies the OSAC access fixes and generates the initial access document before workload setup. The final `access-doc` step refreshes it with workload-cluster details.

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
3. **`restore-ocp`** — creates copy-on-write disk overlays backed by the cached flavor, grows the h00 OS disk to `ocp_snapshot_disk_gb` for every suite, then mounts it via qemu-nbd and grows the root partition and XFS filesystem before writing pre-boot config (hostname, nodeip hint, dnsmasq overrides, nmstate config for static IP, OVN/OVS cleanup). It runs recert (via JSON config file, matching LCA's approach) to regenerate all TLS certificates and cluster identity, then waits for cluster health.

The snapshot flavor is pulled and cached during `make setup-infra` (one-time ~60GB download). Subsequent deploys use copy-on-write overlays, so only changed blocks are written.

OSAC itself is handled by `make deploy-osac` (not part of `deploy-ocp`). In `OSAC_DEPLOY_MODE=snapshot`, that runs `refresh-osac` → `post-refresh-osac` → **`patch-osac-refresh`**, which wires Netris/SSH/socat and then applies `config-as-code-ig`’s `AAP_PROJECT_GIT_*` pin onto the live AAP Project and launches `osac-config-as-code` once (snapshot AAP DB keeps the bake-time `scm_branch`; waiting for the hourly CaC schedule is too late for `setup-maas` / `publish-templates`).

## How deploy-osac Works

`make deploy-osac` runs in three phases:

1. **`prep-osac`** (Ansible) — clones the osac mono-repo, resolves the OCP release CLI image, patches the Helm values file with all CI-specific settings (Netris controller URL/credentials, DNS/AWS settings, SSH keys, instance group config for cluster/network fulfillment), enables LVMS and bundled PostgreSQL while preserving the other operator choices in the selected profile, copies license and pull secret to the values directory, and sets up a socat forwarder for OCP ingress on port 9444.

2. **`run-osac-setup`**:
   - Runs the installer's `make install-infra` for the `osac-deps` and `osac-infra` releases.
   - Creates the external `netris-credentials` Secret.
   - Runs the installer's `make install-osac` for the application release, including template publishing and resource-seeding hooks.

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
| `osac_values_file` | `values/caas-ci/values.yaml` | Helm values file |
| `osac_branch` | `main` | osac mono-repo branch |
| `netris_username` | `netris` | Netris API username |
| `netris_password` | *(required)* | Netris API password — set in your local config, validated at deploy time |
| `ew_fabric_enable` | `0` | East-West fabric (0=NS only) |
| `dns_server` | `8.8.8.8` | Upstream DNS server for the lab (override in environments where public DNS is blocked) |
| `caas_cluster_template` | `osac.templates.ocp_small` | Cluster template for BMaaS-backed CaaS; avoids reattaching a worker port already used by its BMI |
| `caas_cluster_name` | `caas-ci-cluster` | CaaS cluster name |
| `caas_baremetal_instance_type` | `ci-worker` | BMaaS BareMetalInstanceType selected for CaaS workers |
| `snapshot_flavor_image` | `quay.io/osac-project/cluster-flavors:caas` | OCI image containing the snapshot flavor |
| `snapshot_osac_namespace` | `osac-e2e-ci` | OSAC namespace baked into the snapshot |
| `snapshot_osac_values_file` | `values/caas-ci/values.yaml` | Helm values file for OSAC refresh |

```bash
make deploy-ocp EXTRA_VARS="ocp_version=4.18"

# On an installed OSAC lab, deploy-suite runs setup, pytest tenant fixtures,
# suite networking, and the persistent workload in the required order.
make deploy-suite SUITE=caas CAAS_WORKER_COUNT=1
make deploy-suite SUITE=maas MAAS_WORKER_COUNT=2
make deploy-suite SUITE=bmaas
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
| `ocp_version` | `4.21` | OpenShift version (e.g., `4.18`, `4.19`, `4.21`) | yes (4.18) |
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
| `osac_external_ip_pool_cidr` | `198.51.100.24/29` | Shared OSAC ExternalIPPool CIDR; host routes/masquerades it toward the softgate. Must not overlap the mgmt nat/l4lb/bgp pools. OSAC creates the Netris allocation itself. | defaults only |
| `osac_external_ip_pool_name` | `netris-lab-public` | Shared pool name created by setup-caas when the CIDR is not already registered | defaults only |
| `netris_username` | `netris` | Netris API username | defaults only |
| `netris_password` | *(required)* | Netris API password — no default, set in local config | yes |
| `dns_server` | `8.8.8.8` | Upstream DNS server threaded through cloud-init, Netris topology, dnsmasq/DHCP, and the OCP NMState resolver. Override in environments where public DNS (8.8.8.8, 1.1.1.1) is unreachable | yes |

`deploy-suite` runs setup, then the deploy playbook runs a small OSAC monorepo
pytest to activate the shared E2E fixtures. They ensure tenant1 and tenant2,
their test users, memberships, and default subnets. The CaaS/MaaS deploy playbook
then configures tenant1's Netris network before creating the cluster. BMaaS
instead deploys one BMI in each tenant with a reusable shared DiskImage and a
BMI type matched to its BMHs. `destroy-bmaas` deletes these named BMIs and waits
for their BMHs to be released before tearing down BMaaS; it keeps the shared
DiskImage for reuse. The tenants remain after pytest exits.
To retry the deployment after setup, run
`make deploy-suite SUITE=maas SUITE_STAGE=workload` (choose the suite in use).
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

CaaS tenant network preparation uses tenant1's default network. Fresh and
snapshot Helm values set `netris_tenant_network_cidr` and `netris_tenant_subnet_cidr` to
`192.168.101.0/24`. These defaults also apply to newly onboarded BMaaS tenants;
they do not change the management or BMaaS provisioning networks, or migrate
existing tenant networks.

During `deploy-caas` or `deploy-maas`, `configure-caas-tenant-network` reserves
ingress `192.168.101.28` by setting the tenant V-Net DHCP range to
`192.168.101.100`–`192.168.101.254`, before workers are attached. The settings
are `caas_netris_ingress_ip`, `caas_tenant_dhcp_start`, and
`caas_tenant_dhcp_end`. It refuses to change DHCP on an already populated V-Net.

The CaaS/MaaS deploy playbook resolves the tenant Subnet and VirtualNetwork to
the associated Netris VPC when configuring DHCP. Separate isolated VPCs can reuse `.28`, but two clusters
in the same subnet need distinct ingress VIPs.

Existing tenants with a different subnet require network recreation or a fresh
deployment. Merely rerunning setup does not migrate their CIDR.

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
| `osac_values_file` | `values/caas-ci/values.yaml` | Helm values file for OSAC | defaults only |
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

#### Snapshot Deployment (fast path)

The account configured with `quay_username` and `quay_password` must have pull access to
`quay.io/osac-project/cluster-flavors`. Setup logs in with these credentials when it downloads
the flavor image; it reports an actionable error if the credentials are missing or lack access.

| Variable | Default | Description | Tested |
|----------|---------|-------------|--------|
| `snapshot_flavor_image` | `quay.io/osac-project/cluster-flavors:{caas,sno}-4-22` | OCI image containing the snapshot flavor (derived from `osac_deploy_mode`) | defaults only |
| `snapshot_recert_image` | `quay.io/osac-project/recert:latest` | Recert container image | defaults only |
| `snapshot_osac_namespace` | `osac-e2e-ci` | OSAC namespace baked into the snapshot | defaults only |
| `snapshot_osac_values_file` | `values/caas-ci/values.yaml` | Helm values file for OSAC refresh | defaults only |

#### CaaS Configuration

| Variable | Default | Description | Tested |
|----------|---------|-------------|--------|
| `caas_cluster_name` | `caas-ci-cluster` | CaaS cluster name | yes (custom) |
| `caas_cluster_template` | `osac.templates.ocp_small` | Cluster template for BMaaS-backed CaaS | defaults only |
| `caas_baremetal_instance_type` | `ci-worker` | BMaaS BareMetalInstanceType selected for CaaS workers | defaults only |
| `caas_worker_vcpu` | `4` | BMaaS worker VM vCPUs | yes (8) |
| `caas_worker_memory_mb` | `16384` | BMaaS worker VM memory in MB | yes (32768) |
| `caas_worker_disk_gb` | `100` | BMaaS worker VM disk in GB | yes (150) |
| `caas_worker_vm_patterns` | `[hgx-pod00-su0-h01..03]` | BMaaS worker VM names | defaults only |
| `caas_worker_count` | `1` | Number of CaaS BMaaS workers requested; MaaS defaults to `2` | defaults only |

## Testing OSAC Components

All components live in the osac mono-repo. Override `osac_branch` to test a specific branch, and use image variables to override container images.

> **Note**: `OSAC_VALUES_FILE` is a **Make-level** variable (not an Ansible `EXTRA_VARS` key). It controls which Helm values file is used for both `prep-osac` and `run-osac-setup`. Pass it directly to `make`, not inside `EXTRA_VARS`. Default: `values/caas-ci/values.yaml`.

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
make deploy-osac OSAC_VALUES_FILE=values/bmaas-ci/values.yaml \
  EXTRA_VARS="osac_operator_image=quay.io/dmanor/osac-operator:tag bmf_operator_image=quay.io/dmanor/bare-metal-fulfillment-operator:tag bare_metal_services=true"
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
