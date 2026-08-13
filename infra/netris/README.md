# netris-test-infra

Ansible automation to deploy OCP SNO on a [Netris Spectrum-X simulated lab](https://github.com/danmanor/netris-lab) and run OSAC VMaaS/CaaS/BMaaS end-to-end tests.

## Architecture

The [netris-lab](https://github.com/danmanor/netris-lab) deploys a full simulated Spectrum-X GPU cluster network on a single bare-metal host using KVM/libvirt:

- **Netris controller** on K3s — manages all network devices via REST/gRPC API
- **~13 switch VMs** (Cumulus Linux) — leaf/spine fabric for North-South connectivity
- **4 softgate VMs** — provide NAT/L4LB and BGP peering for internet access
- **4 server VMs** (hgx-00 to hgx-03) — simulated GPU servers, managed by Netris

This repo takes the first server (hgx-00), resizes it for OCP, configures Netris networking (VPC, VNet, Subnet), and installs OpenShift SNO on it using the Assisted Installer. For CaaS testing, the remaining three servers (hgx-01 to hgx-03) are booted with a discovery ISO and registered as agents for cluster provisioning.

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
    └── hgx-01..03 (CaaS only: 4 vCPU, 16G RAM, 100G disk)
        ├── Booted with discovery ISO from InfraEnv
        ├── Registered as agents with resource_class + netris.server/name
        └── Used to provision a CaaS cluster via fulfillment API
```

Internet access for OCP image pulls flows through: hgx-00 → NS VNet → softgate SNAT → host iptables masquerade → internet.

## Prerequisites

- **Bare-metal host** running RHEL 9.x or Rocky Linux 9.x with KVM support
- **System packages** — `dnf install -y git make python3-pip ansible-core && pip3 install ansible` (all other tools are installed automatically by `make setup-infra`)
- **Resources**: ~32+ CPU cores, 128+ GB RAM (lab VMs + OCP SNO VM)
- **Storage**: ~400GB+ for OCP disk images, K3s, and containers. If the root partition is smaller, use a secondary data disk and enable automatic provisioning with `disk_setup_enabled=true` (via `EXTRA_VARS` or in the config). Run `make disk-setup` standalone or pass the variable during setup: `make setup-infra EXTRA_VARS="disk_setup_enabled=true"`
- **Netris license key** — place at repo root as `license.key`
- **OSAC/AAP license** — place at repo root as `license.zip`
- **OpenShift pull secret** — place at `/root/pull-secret` (or set `pull_secret_path`; download from [console.redhat.com](https://console.redhat.com/openshift/downloads))
- **Config file** — place a `config` file at the repo root (INI format, gitignored) with lab name and AWS credentials. Credentials can be obtained from the [CI vault](https://vault.ci.openshift.org/ui/vault/secrets/kv/kv/selfservice%2Fosac%2Fpacket-osac). See Quick Start below. The IAM user needs `route53:ChangeResourceRecordSets`, `route53:ListHostedZones`, and `route53:GetChange` permissions on the hosted zone.

All system packages, tools, and SSH keys are installed automatically by `make setup-infra`. A pre-flight check validates all required files, KVM support, and minimum memory before deploying.

## Quick Start

```bash
git clone --recurse-submodules https://github.com/danmanor/netris-test-infra.git
cd netris-test-infra

# Place prerequisites
cp /path/to/license.key ./license.key
cp /path/to/license.zip ./license.zip
cp /path/to/pull-secret /root/pull-secret

# Create config file (unique lab name + AWS credentials for Route 53 DNS)
# lab_name becomes a subdomain under the shared hosted zone (e.g., jsmith.ecoeng-osac-ci.devcluster.openshift.com)
cat > config << EOF
[default]
lab_name = <unique-lab-name>
aws_access_key_id = <your-key>
aws_secret_access_key = <your-secret>
aws_region = us-east-1
EOF

# Install prerequisites and cache images (shared, run once)
make setup-infra

# Full deployment — installs OCP + OSAC from scratch
make deploy

# OR: Fast deployment (~25 min) — uses a pre-built snapshot with recert.
# Faster but doesn't test the OCP or OSAC installer flows.
make deploy-fast

# Then run a test flow
make setup-caas    # CaaS setup: discover hosts, label agents, register host type
make deploy-caas   # CaaS: create cluster
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

### CaaS (run after deploy)

| Target | Description | Time |
|--------|-------------|------|
| `make setup-caas` | Discover hosts, label agents, register host type, configure osac CLI | ~30 min |
| `make deploy-caas` | Create CaaS cluster using `ocp_ci_small` template | ~60 min |

### Destroy

| Target | Description |
|--------|-------------|
| `make destroy-full` | Tear down everything: OSAC + OCP artifacts + netris-lab |
| `make destroy-osac` | Tear down OSAC: helm releases, operators, CRDs, namespaces |
| `make destroy-ocp` | Reset OCP for reinstall: delete cluster, recreate disk, boot VM |
| `make destroy-infra` | Tear down netris-lab (VMs, K3s, topology) |
| `make destroy-caas` | CaaS teardown: stop discovery VMs, remove disks/ISO, delete namespace, clean DNS |

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
make setup-caas     # discover hosts, label agents, register host type
make deploy-caas    # create cluster
```

**Rebuild from scratch:**
```bash
make destroy        # tear down everything
make deploy-fast    # full redeploy (snapshot path)
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
6. Runs the full pipeline: setup-infra → deploy-infra → deploy-ocp → deploy-osac → setup-caas → deploy-caas → post-install

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
| `make redeploy-fresh` | Destroy + wipe progress + full fresh deploy |
| `make disk-setup` | Auto-detect and mount data disk |
| `make post-install` | Fix Keycloak/UI + generate access doc |
| `make access-doc` | Generate handover documentation only |
| `make health-check` | Quick status verification |

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

1. **`configure-netris`** — creates VPC, VNet (DHCP disabled), subnet, SNAT/DNAT rules via Netris API
2. **`configure-dns`** — creates Route 53 DNS records and local dnsmasq config for the cluster domain
3. **`restore-ocp`** — creates copy-on-write disk overlays backed by the cached flavor, mounts the OS disk via qemu-nbd to write pre-boot config (hostname, nodeip hint, dnsmasq overrides, nmstate config for static IP, OVN/OVS cleanup), runs recert (via JSON config file, matching LCA's approach) to regenerate all TLS certificates and cluster identity, then waits for cluster health

The snapshot flavor is pulled and cached during `make setup-infra` (one-time ~60GB download). Subsequent deploys use copy-on-write overlays, so only changed blocks are written.

## How deploy-osac Works

`make deploy-osac` runs in three phases:

1. **`prep-osac`** (Ansible) — clones the osac mono-repo, resolves the OCP release CLI image, patches the Helm values file with all CI-specific settings (Netris controller URL/credentials, DNS/AWS settings, SSH keys, instance group config for cluster/network fulfillment), enables required operators (LVMS, MetalLB, CNV, MCE, bundled PostgreSQL), copies license and pull secret to the values directory, and sets up a socat forwarder for OCP ingress on port 9444.

2. **`run-osac-setup`** (shell) — runs `make install` in the osac-installer directory, which executes three Helm install phases:
   - **Phase 1** (`install-operators`) — installs OLM subscriptions for cert-manager, AAP, LVMS, MetalLB, CNV, and MCE via the `osac-operators` chart
   - **Phase 2** (`install-prereqs`) — deploys Keycloak, CA certificates, trust-manager bundles, and operator CRD instances via the `osac-prereqs` chart
   - **Phase 3** (`install-osac`) — deploys the OSAC umbrella chart (osac-operator, fulfillment-service, osac-aap, osac-ui) with post-install hooks for hub creation and template publishing

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
| `netris_password` | `netris` | Netris API password |
| `ew_fabric_enable` | `0` | East-West fabric (0=NS only) |
| `caas_cluster_template` | `osac.templates.ocp_ci_small` | Cluster template for CaaS cluster creation |
| `caas_cluster_name` | `caas-ci-cluster` | CaaS cluster name |
| `caas_host_type_id` | `ci-worker` | Resource class for CaaS agents |
| `snapshot_flavor_image` | `quay.io/osac-project/cluster-flavors:caas` | OCI image containing the snapshot flavor |
| `snapshot_osac_namespace` | `osac-e2e-ci` | OSAC namespace baked into the snapshot |
| `snapshot_osac_values_file` | `values/caas-ci/values.yaml` | Helm values file for OSAC refresh |

```bash
make deploy-ocp EXTRA_VARS="ocp_version=4.18"
make setup-caas EXTRA_VARS="caas_cluster_name=my-cluster caas_discovery_vcpu=8"
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
| `ocp_install_disk_gb` | `100` | OCP install disk size in GB | defaults only |
| `ocp_lvm_disk_gb` | `200` | LVM storage disk size in GB | defaults only |

#### Netris Networking

| Variable | Default | Description | Tested |
|----------|---------|-------------|--------|
| `ocp_vpc_name` | `ocp-sno` | Netris VPC name | defaults only |
| `ocp_subnet_cidr` | `192.168.40.0/24` | OCP VNet subnet | defaults only |
| `ocp_node_ip` | `192.168.40.2` | OCP node static IP | defaults only |
| `ocp_snat_ip` | `198.51.100.1` | SNAT translated IP | defaults only |
| `ocp_dnat_ip` | `198.51.100.2` | DNAT IP for API/apps access | defaults only |
| `netris_username` | `netris` | Netris API username | defaults only |
| `netris_password` | `netris` | Netris API password | defaults only |

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

#### Snapshot Deployment (fast path)

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
| `caas_cluster_template` | `osac.templates.ocp_ci_small` | Cluster template for CaaS | defaults only |
| `caas_host_type_id` | `ci-worker` | Resource class label for CaaS agents | defaults only |
| `caas_discovery_vcpu` | `4` | Discovery VM vCPUs | yes (8) |
| `caas_discovery_memory_mb` | `16384` | Discovery VM memory in MB | yes (32768) |
| `caas_discovery_disk_gb` | `100` | Discovery VM disk in GB | yes (150) |
| `caas_discovery_vm_patterns` | `[hgx-pod00-su0-h01..03]` | VM names for CaaS discovery | defaults only |

## Testing OSAC Components

All components live in the osac mono-repo. Override `osac_branch` to test a specific branch, and use image variables to override container images.

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
