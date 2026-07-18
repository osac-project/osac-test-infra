# netris-test-infra

OSAC test infrastructure that deploys and tests OpenShift Assisted Cluster on a simulated Netris Spectrum-X GPU cluster. Tests three service models: VMaaS, BMaaS (planned), and CaaS.

Uses [netris-lab](netris-lab/) as a git submodule for the underlying network infrastructure.

## Project Structure

```
roles/                          # Ansible roles (each has tasks/main.yml)
  lab_setup/                    # Prerequisites, cache, OCP/OSAC tool installs
  lab_deploy/                   # Orchestrates netris-lab submodule roles
  vm_resize/                    # Resize hgx-00 VM for OCP (virsh)
  netris_configure/             # Create VPC/VNet/Subnet/NAT via Netris API
  ocp_dns/                      # Route 53 + dnsmasq DNS for OCP
  assisted_service/             # Deploy Assisted Installer (includes ocp_dns)
  ocp_install/                  # Install OCP SNO via aicli
  osac_install/                 # Deploy OSAC (Helm + setup.sh)
  snapshot_pull/                # Pull + cache flavor OCI image (skopeo)
  restore-snapshot/             # Deploy OCP from snapshot: CoW disks, pre-boot config, recert service, boot, health check
  osac_refresh/                 # Refresh OSAC via refresh-after-snapshot.py
  caas_discovery/               # Boot discovery VMs with InfraEnv ISO
  caas_setup/                   # Label agents, register host type, create cluster
  destroy/                      # Teardown everything
playbooks/                      # Ansible playbooks (one per workflow phase)
inventory/
  local.yml                     # Inventory (localhost, local connection)
  group_vars/all.yml            # All configuration variables
netris-lab/                     # Git submodule — see its own CLAUDE.md
vendor/                         # Vendored Ansible collections
```

## Commands

```
make deploy                 # Full pipeline: deploy-lab + deploy-ocp + deploy-osac (~2h)
make deploy-fast            # Snapshot pipeline: deploy-lab + deploy-ocp-snapshot (~15min for OCP+OSAC)
make setup                  # Install prerequisites, cache images + snapshot flavor
make deploy-lab             # Deploy netris-lab
make connectivity           # Re-run lab connectivity (VPN, BGP, softgate agents)
make deploy-ocp             # Resize OCP VM + configure Netris networking + install OCP SNO
make deploy-ocp-snapshot    # Deploy OCP+OSAC from snapshot (recert + refresh)
make deploy-osac            # Deploy OSAC + fulfillment-service + filter OS images
make post-osac              # Scale down MCE operators + filter OS images (runs in deploy-osac)
make setup-caas             # CaaS setup: discover hosts, label agents, register host type
make deploy-caas            # CaaS: create cluster
make deploy-vmaas           # VMaaS flow (not yet implemented)
make deploy-bmaas           # BMaaS flow (not yet implemented)
make destroy                # Teardown all infrastructure
make destroy-osac           # Teardown OSAC only
make destroy-ocp            # Reset OCP for reinstall
make vendor-update          # Refresh vendored Ansible collections
make gather                 # Gather diagnostic info from the cluster
# Override variables: make <target> EXTRA_VARS="key=value"
# Image overrides: make deploy-ocp-snapshot EXTRA_VARS="fulfillment_service_image=quay.io/..."
```

## Workflow Order

**Full deploy (all flows):** deploy (setup → deploy-lab → deploy-ocp → deploy-osac)

**Snapshot deploy (fast):** deploy-fast (setup → deploy-lab → deploy-ocp-snapshot)

**CaaS:** deploy or deploy-fast → setup-caas → deploy-caas

**VMaaS:** deploy → deploy-vmaas (not yet implemented)

**BMaaS:** deploy → deploy-bmaas (not yet implemented)

## Ansible Configuration

- Inventory: `inventory/local.yml`
- Roles path: `roles:netris-lab/roles` (both local and submodule roles)
- Collections: `vendor:netris-lab/collections`
- Netris API calls use `netris.controller.*` collection modules

## Configuration

All variables in `inventory/group_vars/all.yml`. Key sections:

- **Lab**: `netris_lab_dir`, `ew_fabric_enable`
- **OCP VM sizing**: `ocp_server_vcpu`, `ocp_server_memory_gb`, disk sizes
- **Netris networking**: `ocp_vpc_name`, `ocp_subnet_cidr`, SNAT/DNAT IPs
- **OCP install**: `ocp_version`, `ocp_cluster_name`, `ocp_base_domain`
- **OSAC**: `osac_installer_repo/branch`, `osac_namespace`, `osac_values_file`, `osac_aap_branch`
- **Component images**: `osac_operator_image`, `fulfillment_service_image` (empty = defaults)
- **Snapshot**: `snapshot_flavor_image`, `snapshot_flavor_dir`, `snapshot_recert_image`, `snapshot_osac_namespace`, `snapshot_osac_values_file`
- **CaaS**: `caas_discovery_vm_patterns`, `caas_host_type_id`, `caas_cluster_name`, `caas_agents`

## External Dependencies

- **osac-installer** — cloned to `/opt/osac-installer` during `prep-osac`
- **fulfillment-service** — cloned to `$HOME/.local/src/fulfillment-service` (job-scoped, non-root); `osac` CLI built from its Go code to `$HOME/.local/bin/osac`
- **aicli** — CLI for Red Hat Assisted Installer (pip install)
- **Credentials**: pull secret at `/root/pull-secret`, SSH key at `/root/.ssh/id_rsa.pub`

## Conventions

- Ansible roles follow standard structure: `tasks/main.yml`, `templates/`, `defaults/`
- Templates use Jinja2 (`.j2` extension)
- VM operations use `virsh`, `virt-xml`, `qemu-img` via shell/command modules
- Kubernetes resources applied via `kubernetes.core.k8s` or `oc`/`kubectl` CLI
- Waits/retries use `until` loops with `retries` and `delay`
