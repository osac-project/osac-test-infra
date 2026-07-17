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
- **System packages** — `dnf install -y git make ansible-core python3-pip && pip3 install ansible`
- **Resources**: ~32+ CPU cores, 128+ GB RAM (lab VMs + OCP SNO VM)
- **Netris license key** — place at repo root as `license.key`
- **OSAC/AAP license** — place at repo root as `license.zip`
- **OpenShift pull secret** — place at `/root/pull-secret` (or set `pull_secret_path`; download from [console.redhat.com](https://console.redhat.com/openshift/downloads))
- **Config file** — place a `config` file at the repo root (INI format, gitignored) with lab name and AWS credentials. Credentials can be obtained from the [CI vault](https://vault.ci.openshift.org/ui/vault/secrets/kv/kv/selfservice%2Fosac%2Fpacket-osac). See Quick Start below. The IAM user needs `route53:ChangeResourceRecordSets`, `route53:ListHostedZones`, and `route53:GetChange` permissions on the hosted zone.

All system packages, tools, and SSH keys are installed automatically by `make setup`. A pre-flight check validates all required files, KVM support, and minimum memory before deploying.

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
make setup

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
| `make deploy` | Full pipeline: deploy-lab → deploy-ocp → deploy-osac | ~2-3 hrs |
| `make deploy-fast` | Snapshot pipeline: deploy-lab → deploy-ocp-snapshot | ~25 min |
| `make setup` | Install prerequisites, cache images + snapshot flavor, build tools | ~10 min |
| `make deploy-lab` | Deploy netris-lab (K3s, topology, VMs, connectivity) | ~12 min |
| `make deploy-ocp` | Resize OCP VM + Netris networking + Assisted Service + OCP SNO | ~35-65 min |
| `make deploy-ocp-snapshot` | Deploy OCP+OSAC from snapshot (recert + OSAC refresh) | ~13 min |
| `make deploy-osac` | Prepare OSAC overlay + run setup.sh + filter OS images | ~30-60 min |

### CaaS (run after deploy)

| Target | Description | Time |
|--------|-------------|------|
| `make setup-caas` | Discover hosts, label agents, register host type, configure osac CLI | ~30 min |
| `make deploy-caas` | Create CaaS cluster using `ocp_ci_small` template | ~60 min |

### Other flows (run after deploy)

| Target | Description | Time |
|--------|-------------|------|
| `make deploy-vmaas` | VMaaS flow (not yet implemented) | — |
| `make deploy-bmaas` | BMaaS flow (not yet implemented) | — |

### Destroy

| Target | Description |
|--------|-------------|
| `make destroy` | Tear down everything: OSAC + OCP artifacts + netris-lab |
| `make destroy-osac` | Tear down OSAC: helm releases, operators, CRDs, namespaces (live output) |
| `make destroy-ocp` | Reset OCP for reinstall: delete cluster, recreate disk, boot VM |
| `make destroy-caas` | CaaS teardown (not yet implemented) |
| `make destroy-vmaas` | VMaaS teardown (not yet implemented) |
| `make destroy-bmaas` | BMaaS teardown (not yet implemented) |

### Recovery and Utilities

| Target | Description |
|--------|-------------|
| `make connectivity` | Re-run lab connectivity (VPN, BGP, softgates) without full redeploy |
| `make run-osac-setup` | Re-run just setup.sh with live output (after prep-osac has run) |
| `make prep-osac` | Ansible-only OSAC prep (clone, overlay, secrets, env file) — no setup.sh |
| `make post-osac` | Scale down MCE operators and filter OS images to target version |
| `make vendor-update` | Refresh vendored Ansible collections |
| `make lint` | Run ansible-lint |
| `make gather` | Gather diagnostic info from the cluster |

### Typical Workflows

**First deploy on a fresh server (fast path — snapshot):**
```bash
make setup          # install prerequisites, cache images + snapshot flavor, build tools
make deploy-fast    # deploy lab + OCP+OSAC from snapshot (~25 min total)
```

**First deploy on a fresh server (full path — from scratch):**
```bash
make setup          # install prerequisites, cache images, build tools
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

## How deploy-ocp-snapshot Works

`make deploy-ocp-snapshot` deploys OCP+OSAC in ~13 minutes from a pre-built VM snapshot instead of installing from scratch (~1.5 hours). It uses a golden qcow2 disk image from `quay.io/osac-project/cluster-flavors:caas` containing a fully deployed OCP+OSAC cluster, then regenerates the cluster's identity (certificates, hostname, IP, domain) using [recert](https://github.com/rh-ecosystem-edge/recert).

The flow runs five Ansible roles in sequence:

1. **`netris_configure`** — creates VPC, VNet (DHCP disabled), subnet, SNAT/DNAT rules via Netris API
2. **`ocp_dns`** — creates Route 53 DNS records and local dnsmasq config for the cluster domain
3. **`snapshot_restore`** — creates copy-on-write disk overlays backed by the cached flavor, mounts the OS disk via qemu-nbd to write pre-boot config (hostname, nodeip hint, dnsmasq overrides, nmstate config for static IP, OVN/OVS cleanup), then boots the VM
4. **`snapshot_recert`** — verifies br-ex has the correct IP, stops kubelet/crio, runs a standalone etcd container, deletes stale hypershift secrets, runs recert to regenerate all TLS certificates and cluster identity, then restarts services and waits for cluster health
5. **`osac_refresh`** — clones osac-installer and runs `refresh-after-snapshot.py` which patches routes/certs for the new domain, runs helm upgrade, and configures AAP/fulfillment/tenants

The snapshot flavor is pulled and cached during `make setup` (one-time ~60GB download). Subsequent deploys use copy-on-write overlays, so only changed blocks are written.

## How deploy-osac Works

`make deploy-osac` runs in three phases:

1. **`prep-osac`** (Ansible) — clones osac-installer, copies the development overlay to a working overlay (`osac-devel`), patches the operator chart to read the AAP token from a Kubernetes secret, creates placeholder secrets (AAP API token, bmf-operator configs), writes secrets (license, pull secret, SSH keys), configures env files with Netris integration settings, and disables unused components (bmf-operator).

2. **`run-osac-setup`** (shell) — runs `setup.sh` directly in the terminal with live output. This installs OCP operators (LVMS, MetalLB, CNV, cert-manager, Authorino, Keycloak, AAP), deploys OSAC via Helm, applies AAP configuration, and runs post-install setup (AAP token, hub registration, tenant creation). Retries up to 10 times with a 3-minute delay.

3. **`post-osac`** (Ansible) — scales down MCE operators (infrastructure-operator, multicluster-engine-operator) to prevent them from resetting OS images, then filters `OS_IMAGES` in the assisted-service ConfigMap and `RHCOS_VERSIONS` in the assisted-image-service StatefulSet to only the target OCP version (`caas_ocp_version`, x86_64). Verifies the image-service pod contains only the expected version.

## Configuration

All parameters are in [`inventory/group_vars/all.yml`](inventory/group_vars/all.yml). Override any variable via `EXTRA_VARS`:

```bash
make deploy-ocp EXTRA_VARS="ocp_version=4.21"
make deploy-osac EXTRA_VARS='{"osac_installer_branch": "feature-x"}'
```

### Key Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ocp_version` | `4.21` | OpenShift version |
| `ocp_cluster_name` | `ocp-sno` | OCP cluster name |
| `lab_name` | `$LAB_NAME` or `default` | Per-lab subdomain prefix (avoids DNS collisions) |
| `dns_hosted_zone` | `ecoeng-osac-ci.devcluster.openshift.com` | Route 53 hosted zone |
| `ocp_base_domain` | `<lab_name>.<dns_hosted_zone>` | DNS base domain (derived) |
| `ocp_server_vcpu` | `20` | OCP VM vCPUs |
| `ocp_server_memory_gb` | `64` | OCP VM RAM (GB) |
| `ocp_subnet_cidr` | `192.168.40.0/24` | OCP VNet subnet |
| `ocp_dnat_ip` | `198.51.100.2` | DNAT IP for OCP API/apps access |
| `osac_namespace` | `osac-devel` | OSAC Kubernetes namespace |
| `osac_kustomize_overlay` | `osac-devel` | OSAC overlay (copied from development) |
| `osac_values_file` | `values/development/values.yaml` | Helm values file |
| `osac_installer_branch` | `main` | osac-installer branch |
| `osac_aap_branch` | `main` | osac-aap project branch (synced to AAP controller) |
| `netris_username` | `netris` | Netris API username |
| `netris_password` | `netris` | Netris API password |
| `ew_fabric_enable` | `0` | East-West fabric (0=NS only) |
| `caas_ocp_version` | `4.21` | OCP version for CaaS discovery ISO and release image |
| `caas_cluster_template` | `ocp_ci_small` | Cluster template for CaaS cluster creation |
| `caas_cluster_name` | `caas-ci-cluster` | CaaS cluster name |
| `caas_host_type_id` | `ci-worker` | Resource class for CaaS agents |
| `snapshot_flavor_image` | `quay.io/osac-project/cluster-flavors:caas` | OCI image containing the snapshot flavor |
| `snapshot_osac_namespace` | `osac-e2e-ci` | OSAC namespace baked into the snapshot |
| `snapshot_osac_values_file` | `values/caas-ci/values.yaml` | Helm values file for OSAC refresh |

See [`inventory/group_vars/all.yml`](inventory/group_vars/all.yml) for the full list.

> **Note:** Variables other than `lab_name`, `dns_hosted_zone`, and the component overrides have not been tested with non-default values. Changing OCP version, cluster name, namespace, subnet, or VM sizing may require additional adjustments to the playbooks.

## Testing OSAC Components

Each OSAC component can be tested by setting its branch and/or runtime image via `EXTRA_VARS`. When a branch is set, the repo is cloned and overlaid into the installer's submodule. For osac-aap, the branch sets `AAP_PROJECT_GIT_BRANCH` instead (AAP syncs from git directly).

### Component Variables

| Component | Branch | Image | Effect |
|-----------|--------|-------|--------|
| **osac-installer** | `osac_installer_branch` | — | Installer repo cloned at this branch |
| **osac-operator** | `osac_operator_branch` | `osac_operator_image` | Cloned into installer `base/osac-operator` |
| **fulfillment-service** | `fulfillment_service_branch` | `fulfillment_service_image` | Cloned into installer `base/osac-fulfillment-service` + CLI build |
| **osac-aap** | `osac_aap_branch` | `osac_aap_image` | Sets `AAP_PROJECT_GIT_BRANCH` (no code overlay) |

All variables default to empty — installer submodule pins and Helm defaults are used.

### Examples

**Test an osac-operator version (code + image):**
```bash
make destroy-osac
make deploy-osac EXTRA_VARS='{"osac_operator_branch": "feature-x", "osac_operator_image": "quay.io/osac-project/osac-operator:feature-x"}'
```

**Test a fulfillment-service version (code + CLI + image):**
```bash
make destroy-osac
make setup EXTRA_VARS='{"fulfillment_service_branch": "feature-x"}'
make deploy-osac EXTRA_VARS='{"fulfillment_service_branch": "feature-x", "fulfillment_service_image": "quay.io/osac-project/fulfillment-service:feature-x"}'
```

**Test an osac-aap branch (AAP syncs from git):**
```bash
make destroy-osac
make deploy-osac EXTRA_VARS='{"osac_aap_branch": "dns-hypervisor-backend"}'
```

**Test multiple components at once:**
```bash
make destroy-osac
make deploy-osac EXTRA_VARS='{"osac_operator_branch": "pr-42", "osac_operator_image": "quay.io/osac-project/osac-operator:pr-42", "osac_aap_branch": "pr-99", "osac_aap_image": "quay.io/osac-project/osac-aap-ee:pr-99"}'
```

**Override only the installer:**
```bash
make destroy-osac
make deploy-osac EXTRA_VARS='{"osac_installer_branch": "feature-y"}'
```
