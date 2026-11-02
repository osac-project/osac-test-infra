# netris-test-infra

OSAC test infrastructure that deploys and tests OpenShift Assisted Cluster on a simulated Netris Spectrum-X GPU cluster. Tests VMaaS, BMaaS, CaaS, and MaaS service models.

Uses [netris-lab](netris-lab/) as a git submodule for the underlying network infrastructure.

## Project Structure

```
roles/                          # Ansible roles (each has tasks/main.yml)
  setup-infra/                  # Prerequisites, cache, OCP/OSAC tool installs
  deploy-infra/                 # Orchestrates netris-lab submodule roles
  configure-netris/             # Create VPC/VNet/Subnet/NAT via Netris API
  configure-dns/                # Route 53 + dnsmasq DNS for OCP
  restore-ocp/                  # Deploy OCP from snapshot: CoW disks, recert, boot, health check
  cache-snapshot/               # Pull + cache flavor OCI image (skopeo)
  prep-osac/                    # Clone mono-repo, generate netris-runtime.yaml (fresh install)
  prepare-caas-network/         # remove legacy CaaS DNS overrides; guest network readiness
  prepare-caas-bmaas/           # Reuse BMaaS roles to register CaaS/MaaS worker BMHs
  setup-caas/                   # Register worker profiles, DiskImage, and catalog
  create-caas/                  # Create CaaS/MaaS cluster via fulfillment API
  destroy-infra/                # Teardown netris-lab
  destroy-caas/                 # Delete the OSAC cluster; retain BMaaS hosts
  force-destroy-caas/           # Force-clean stuck orders/namespaces + Netris orphans
playbooks/                      # Ansible playbooks (one per workflow phase)
inventory/
  local.yml                     # Inventory (localhost, local connection)
  group_vars/all.yml            # All configuration variables
netris-lab/                     # Git submodule — see its own CLAUDE.md
vendor/                         # Vendored Ansible collections
```

## Commands

```
make deploy                 # Full pipeline: deploy-infra + deploy-ocp + deploy-osac (fresh Helm install)
make setup-infra            # Install prerequisites, cache images + snapshot flavor
make deploy-infra           # Deploy netris-lab
make deploy-ocp             # Configure Netris networking + restore OCP SNO from snapshot
make deploy-osac            # Deploy OSAC (fresh Helm install)
make connectivity           # Re-run lab connectivity (VPN, BGP, softgate agents)
make setup-caas             # Prepare BMaaS workers and register CaaS prerequisites
make deploy-caas            # CaaS: create cluster
make setup-maas             # Prepare BMaaS workers and MaaS prerequisites (three g5 hosts)
make deploy-maas            # MaaS: create a cluster using two g5 BMaaS workers
make destroy-full           # Teardown all infrastructure
make destroy-osac           # Teardown OSAC only
make destroy-ocp            # Reset OCP for reinstall
make destroy-infra          # Teardown netris-lab
make destroy-caas           # Delete the CaaS/MaaS cluster; retain reusable BMaaS hosts
make force-destroy-caas     # Force-clean stuck orders, worker BMHs/VMs, and Netris orphans
make destroy-maas           # Delete the MaaS cluster; retain reusable BMaaS hosts
make setup-bmaas            # BMaaS setup: sushy-tools, BMHs, default BMIT, catalog items
make destroy-bmaas          # Teardown BMaaS (BMHs, sushy-tools, BMC network)
make setup-bmc              # BMC network + sushy-tools only (subset of setup-bmaas)
make destroy-setup          # Revert setup-infra (caches, bridges, tools)
make force-destroy-maas     # Force-clean the MaaS order, worker BMHs/VMs, and Netris orphans
make redeploy-fresh         # destroy-full + full BM pipeline (DEPLOY_SUITE)
make vendor-update          # Refresh vendored Ansible collections
make gather-infra           # Gather diagnostic info from the cluster
# Override variables: make <target> EXTRA_VARS="key=value"
# Image overrides: make deploy-osac EXTRA_VARS="fulfillment_service_image=quay.io/..."
# Deploy suite: make redeploy-fresh DEPLOY_SUITE=maas (see targets.md)
```

## Workflow Order

**Full deploy:** deploy (deploy-infra → deploy-ocp → deploy-osac). OSAC is always a fresh Helm install; only the OCP SNO cluster is restored from a snapshot.

**CaaS:** deploy → setup-caas → deploy-caas

**MaaS:** deploy → setup-maas → deploy-maas
(or `make redeploy-fresh DEPLOY_SUITE=maas`)

**VMaaS:** `make e2e INFRA=netris DEPLOY_SUITE=vmaas EXTRA_VARS="$IMG"`. prep-osac writes `values/vmaas-ci/netris-runtime.yaml`; `values/vmaas-ci/netris.yaml` selects the managers; the installer chart renders the registrations and the NetworkClass. See README "VMaaS with the Netris Fabric Manager". Select VMaaS tests with `TEST_SUITE=vmaas`.

**Stretched networking:** deploy with `DEPLOY_SUITE=full`, run `setup-full`, and select
only the shared-subnet tests with `TEST_SUITE=stretched`.

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
- **OSAC**: `osac_repo/branch`, `osac_namespace`, `osac_values_file`
- **Netris networking contract**: `netris_k8s_manager_suites`, `netris_extra_excluded_cidrs`
- **Component images**: `osac_operator_image`, `fulfillment_service_image` (empty = defaults)
- **Snapshot**: `snapshot_flavor_image`, `snapshot_flavor_dir`, `snapshot_recert_image` (OCP SNO restore only)
- **CaaS / MaaS**: BMaaS-backed workers via `caas_worker_vm_patterns`, `caas_baremetal_instance_type`, `caas_worker_count`, and `caas_cluster_name`; VM sizing uses `caas_worker_vcpu` / `caas_worker_memory_mb`; MaaS defaults to three identical 10 vCPU / 20 GiB g5 hosts with initial cluster size two

## External Dependencies

- **osac** — mono-repo cloned to `/opt/osac` during `prep-osac` (installer at `/opt/osac/osac-installer`)
- **fulfillment-service** — cloned to `$HOME/.local/src/fulfillment-service` (job-scoped, non-root); `osac` CLI built from its Go code to `$HOME/.local/bin/osac`
- **aicli** — CLI for Red Hat Assisted Installer (pip install)
- **Credentials**: pull secret at `/root/pull-secret`, SSH key at `/root/.ssh/id_rsa.pub`

## Conventions

- Ansible roles follow standard structure: `tasks/main.yml`, `templates/`, `defaults/`
- Templates use Jinja2 (`.j2` extension)
- VM operations use `virsh`, `virt-xml`, `qemu-img` via shell/command modules
- Kubernetes resources applied via `kubernetes.core.k8s` or `oc`/`kubectl` CLI
- Waits/retries use `until` loops with `retries` and `delay`
