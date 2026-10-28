# Netris Make Targets and Options

This is the detailed input reference for `infra/netris/Makefile`. It describes
the Make variables, environment/configuration inputs, Ansible extra variables,
and which targets consume them. The root README remains the generic guide for
all backends; the [Netris README](README.md) gives the Netris workflow and
common options.

## Run from the right directory

For an end-to-end run, use the repository root. The root pipeline runs
`setup-infra` → `deploy-infra` → `deploy-ocp` → `deploy-osac` →
`setup-<DEPLOY_SUITE>` → `test-<TEST_SUITE>`:

```bash
make e2e INFRA=netris DEPLOY_SUITE=full TEST_SUITE=stretched \
  EXTRA_VARS="bare_metal_services=true"
```

To run backend targets directly, change to `infra/netris/`. The `deploy` target
runs only `deploy-infra` → `deploy-ocp` → `deploy-osac`; it does not prepare a
test suite or run pytest.

## How target arguments work

- `KEY=value` after `make` sets a Make variable. The Make variables below are
  read directly by the backend Makefile.
- `EXTRA_VARS='key=value other_key=value'` or
  `EXTRA_VARS='{"key": "value"}'` passes Ansible extra variables to the
  target's playbook. Only variables read by that playbook and its roles affect
  the target. On the composite `deploy` target, the same `EXTRA_VARS` is passed
  to all three deployment steps.
- The backend adds `deploy_suite`, the derived `osac_profile`, `osac_namespace`,
  and `osac_values_file` to Ansible-backed targets. OCP targets also receive
  `ocp_evpn_kubeconfig_path`; `deploy-infra` and `deploy-ocp` automatically
  enable the OCP underlay for `DEPLOY_SUITE=vmaas` and `DEPLOY_SUITE=full`.
- The backend's inventory defaults are in
  [`inventory/group_vars/all.yml`](inventory/group_vars/all.yml). `EXTRA_VARS`
  can override those defaults for a target that consumes them. Values defined
  by the Makefile itself, such as `DEPLOY_SUITE`, `OSAC_NAMESPACE`, and the
  derived profile, are supplied after `EXTRA_VARS` and take precedence over a
  colliding extra variable.

## Make variables

| Variable | Default | Targets and meaning |
|---|---|---|
| `DEPLOY_SUITE` | `caas` | `deploy`, `deploy-infra`, `deploy-ocp`, `deploy-osac`, and suite targets. Supported backend scenarios are `caas`, `vmaas`, `bmaas`, and `full`; selects setup behavior and derives the OSAC profile. |
| `EXTRA_VARS` | empty | Ansible-backed targets and the `deploy`/redeploy composites. Passes Ansible variables to the playbook(s) used by the target. |
| `OSAC_NAMESPACE` | `osac-e2e-ci` | `deploy-osac`, `destroy-osac`, and the `.env.infra` connection settings. Passed to Ansible and the OSAC installer. |
| `KUBECONFIG` | `/root/.kube/config` | Used by the OCP restore/EVPN tasks, OSAC installer/uninstaller, and `.env.infra`; root orchestration forwards an explicitly supplied value. Several service, gather, and access-document playbooks still hardcode `/root/.kube/config` and do not honor an override. |
| `OSAC_VM_KUBECONFIG` | same as `KUBECONFIG` | Written to `.env.infra` for VMaaS/full deployments; for CaaS/BMaaS, the backend records the hub kubeconfig as the VM kubeconfig. |
| `OSAC_PULL_SECRET_PATH` | `/root/pull-secret` | Path written to `.env.infra` for tests that need the pull secret. The OCP restore's Ansible variable is `pull_secret_path`. |
| `OSAC_PROFILE` | derived | Not an override. The backend derives `caas-ci`, `vmaas-ci`, `bmaas-ci`, or `full-ci` from `DEPLOY_SUITE` and rejects a command-line override. |

`TEST_SUITE` is a root orchestration variable, not a Netris Makefile variable.
It selects the pytest target independently of `DEPLOY_SUITE` and defaults to
`DEPLOY_SUITE`.

The MaaS wrappers have additional Make variables:

| Variable | Default | Used by |
|---|---|---|
| `MAAS_CLUSTER_NAME` | `maas-cluster` | `setup-maas`, `deploy-maas`, and destroy wrappers |
| `MAAS_HOST_TYPE_ID` | `g5` | MaaS setup/deploy/destroy wrappers |
| `MAAS_CLUSTER_TEMPLATE` | `osac.templates.ocp_4_20_ai_maas` | MaaS setup/deploy/destroy wrappers |
| `MAAS_CATALOG_ITEM` | `maas-ocp` | MaaS setup/deploy/destroy wrappers |
| `MAAS_DISCOVERY_VCPU` | `10` | `setup-maas` |
| `MAAS_DISCOVERY_MEMORY_MB` | `20480` | `setup-maas` |
| `MAAS_DISCOVERY_VCPU_OVERRIDES` | `{"hgx-pod00-su0-h01":4}` | `setup-maas`; JSON map from VM name to vCPU count |
| `MAAS_DISCOVERY_MEMORY_MB_OVERRIDES` | `{"hgx-pod00-su0-h01":16384}` | `setup-maas`; JSON map from VM name to memory in MiB |
| `MAAS_LABEL_HOSTNAMES` | `hgx-pod00-su0-h02,hgx-pod00-su0-h03` | MaaS setup/deploy/destroy wrappers; comma-separated Netris inventory hostnames |
| `MAAS_CLUSTER_PARAMS` | `enable_maas=true,enable_keycloak=true,enable_observability=true,enable_dashboard=true` | `deploy-maas`; comma-separated `key=value` template parameters |

`setup-maas` and `deploy-maas` are backend-only wrappers. `maas` is not a
supported `DEPLOY_SUITE` value for the root E2E pipeline.

## Environment, config file, and local files

Ansible reads `infra/netris/config` as an INI file. For these values, a
non-empty `[default]` entry in the config file takes precedence; the listed
environment variable is the fallback.

| Config key | Environment fallback | Used by |
|---|---|---|
| `netris_password` | `NETRIS_PASSWORD` | Netris API operations, including OCP and BMaaS setup |
| `quay_username` | `QUAY_USERNAME` | `setup-infra`, when caching the OCP SNO image |
| `quay_password` | `QUAY_PASSWORD` | `setup-infra`, when caching the OCP SNO image |
| `lab_name` | `LAB_NAME` | Lab DNS name; defaults to `default` |
| `dns_hosted_zone` | `DNS_HOSTED_ZONE` | Route 53 zone; defaults to `ecoeng-osac-ci.devcluster.openshift.com` |
| `aws_access_key_id` | `AWS_ACCESS_KEY_ID` | Route 53 record management |
| `aws_secret_access_key` | `AWS_SECRET_ACCESS_KEY` | Route 53 record management |
| `aws_region` | `AWS_DEFAULT_REGION` | Route 53 record management; defaults to `us-east-1` |

The backend also expects these local files at their configured/default paths:

| File | Default path | Used by |
|---|---|---|
| Netris license | `infra/netris/license.key` | Lab setup |
| OSAC/AAP license | `infra/netris/license.zip` | OSAC install |
| OpenShift pull secret | `/root/pull-secret` | OCP restore and OSAC install |
| SSH public key | `/root/.ssh/id_rsa.pub` | OCP restore |

`config_path`, `netris_password`, `quay_username`, and `quay_password` can also
be overridden as Ansible variables. `pull_secret_path` and `ssh_public_key_path`
are Ansible variables; `OSAC_PULL_SECRET_PATH` only selects the path exported
for tests in `.env.infra`.

The shell-script targets have additional environment inputs:

| Variable | Default | Target | Meaning |
|---|---|---|---|
| `SERVER` | none | `deploy-jump` | Required remote server IP/hostname. |
| `LAB_NAME` | none in `deploy-jump` | `deploy-jump`; backend config fallback | Required by `deploy-jump`; selects the per-lab DNS prefix. |
| `PULL_SECRET` | none | `deploy-jump` | Required local path to the OpenShift pull-secret file. |
| `LICENSE_KEY` | none | `deploy-jump` | Required local path to the Netris license key. |
| `LICENSE_ZIP` | none | `deploy-jump` | Required local path to the OSAC/AAP license archive. |
| `PASSWORD` | empty | `deploy-jump` | Optional SSH password; SSH key authentication is used when empty. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | empty | `deploy-jump` | Optional Route 53 credentials. If both are set, the remote config uses Route 53; otherwise it uses local DNS mode. |
| `DEPLOY_TARGET` | `redeploy-fresh` | `deploy-jump` | Remote Make command started in tmux; may include Make assignments, for example `redeploy-fresh DEPLOY_SUITE=maas`. |
| `CACHE_DIR` | `$HOME/.cache/netris-lab/k3s-images` | `deploy-jump` / `cache-images.sh` | Local cache directory for Netris lab container images. |
| `MAX_RETRIES` | `10` | `cache-images.sh` (called by `deploy-jump`) | Maximum image download attempts. |
| `BASE_DELAY` | `30` seconds | `cache-images.sh` (called by `deploy-jump`) | Initial retry delay. |
| `MAX_DELAY` | `300` seconds | `cache-images.sh` (called by `deploy-jump`) | Maximum retry delay. |
| `DEPLOY_PROGRESS_FILE` | `/root/.deploy-progress` | `redeploy-fresh`, `redeploy-continue` | Progress file used to resume the server-side redeploy sequence. |
| `OSAC_NAMESPACE` | `osac-devel` | `health-check` | Namespace checked by the health script; this default is independent of the Makefile default. |
| `KUBECONFIG` | `/root/.kube/config` | `health-check` | Kubeconfig used by the health script. |
| `OCP_VM_PATTERN` | `hgx-pod00-su0-h00` | `health-check` | VM name pattern checked by the health script. |

## Target-by-target inputs

Unless a row says otherwise, an Ansible-backed target accepts `EXTRA_VARS` and
the inventory variables listed for its target family in the next section.
The playbook shown is the direct backend Makefile entry point.

| Target | Playbook or action | Inputs used by this target |
|---|---|---|
| `deploy` | Composite: `deploy-infra`, `deploy-ocp`, `deploy-osac` | `DEPLOY_SUITE`, `EXTRA_VARS`; same extra vars reach each phase. |
| `deploy-setup` / `setup-infra` | `playbooks/deploy-setup.yml` | `EXTRA_VARS`; `disk_setup_enabled` opts into automatic data-disk setup. Snapshot registry credentials are read from config/environment while caching the OCP image. |
| `deploy-infra` | `playbooks/deploy-infra.yml` | `DEPLOY_SUITE`, `EXTRA_VARS`; `vmaas` and `full` automatically enable the OCP underlay. Uses the infrastructure/network variables below and variables from the Netris lab submodule. |
| `deploy-ocp` | `playbooks/deploy-ocp.yml` | `DEPLOY_SUITE`, `KUBECONFIG`, `EXTRA_VARS`; `vmaas` and `full` automatically enable the underlay/EVPN setup. Uses OCP, DNS, Netris, and snapshot-restore variables below. |
| `deploy-osac` | `install-osac`; writes `.env.infra` after a successful install | `DEPLOY_SUITE`, `OSAC_NAMESPACE`, `KUBECONFIG`, `OSAC_VM_KUBECONFIG`, `OSAC_PULL_SECRET_PATH`, `EXTRA_VARS`; installs the profile derived from `DEPLOY_SUITE`. |
| `install-osac` | Composite: `prep-osac` then `run-osac-setup` | `DEPLOY_SUITE`, `OSAC_NAMESPACE`, `KUBECONFIG`, `EXTRA_VARS`; requires OCP to have been restored by `deploy-ocp`. |
| `prep-osac` | `playbooks/prep-osac.yml` | `DEPLOY_SUITE`, `KUBECONFIG`, `EXTRA_VARS`; prepares OSAC and generates the profile's runtime values overlay. |
| `run-osac-setup` | OSAC installer plus Netris credential playbook | `DEPLOY_SUITE`, `OSAC_NAMESPACE`, `KUBECONFIG`, `EXTRA_VARS` (for the credential playbook); consumes the OCP EVPN values from `deploy-ocp` and runtime values from `prep-osac`. |
| `setup-vmaas` | `playbooks/setup-vmaas.yml` | `KUBECONFIG`, `EXTRA_VARS`; uses the EVPN validation and VMaaS return-path variables below. |
| `gather-vmaas` | `playbooks/gather-infra.yml` | `KUBECONFIG`, `EXTRA_VARS`; collects infrastructure/EVPN state. |
| `destroy-vmaas` | No-op | VMaaS setup creates no tenant resources. Cluster prerequisites are removed by `destroy-ocp`. |
| `setup-caas` | `playbooks/setup-caas.yml` | `EXTRA_VARS`; uses CaaS discovery, agent, resource-class, and catalog variables. `bare_metal_services=true` enables the BMC/Metal3 pieces when needed. Tasks use `/root/.kube/config`. |
| `deploy-caas` | `playbooks/deploy-caas.yml` | `EXTRA_VARS`; uses CaaS cluster name/template/catalog and `caas_cluster_params`. Tasks use `/root/.kube/config`. |
| `setup-caas-agents` | `playbooks/setup-caas-agents.yml` | `EXTRA_VARS`; requires `caas_cluster_order_namespace`. Tasks use `/root/.kube/config`. |
| `setup-bmc` | `playbooks/setup-bmc.yml` | `EXTRA_VARS`; uses `bare_metal_services`, `bmc_net_*`, and `sushy_tools_*`. |
| `setup-bmaas` | `playbooks/setup-bmaas.yml` | `DEPLOY_SUITE=bmaas`, `EXTRA_VARS`; uses BMC, BMaaS, and BM provisioning-network variables. Tasks use `/root/.kube/config`. |
| `setup-full` | Calls `setup-bmaas`, then `setup-vmaas` | `DEPLOY_SUITE=full`, `EXTRA_VARS`; prepares BMaaS and VMaaS networking prerequisites. |
| `setup-maas` | `playbooks/setup-caas.yml` with `MAAS_EXTRA` | MaaS Make variables above plus `EXTRA_VARS`; configures discovery sizing and resource classes for MaaS. Tasks use `/root/.kube/config`. |
| `deploy-maas` | `playbooks/deploy-caas.yml` with `MAAS_EXTRA` | MaaS Make variables above plus `EXTRA_VARS`; creates the MaaS cluster with the selected template parameters. Tasks use `/root/.kube/config`. |
| `destroy-maas` / `force-destroy-maas` | CaaS destroy playbooks with `MAAS_EXTRA` | MaaS Make variables above plus `EXTRA_VARS`. Tasks use `/root/.kube/config`. |
| `destroy-suite` | Dispatches `destroy-<DEPLOY_SUITE>` | `DEPLOY_SUITE`, `EXTRA_VARS`; rejects `full` because that scenario shares the whole lab. |
| `destroy-osac` | OSAC installer uninstall | `DEPLOY_SUITE`, `OSAC_NAMESPACE`, `KUBECONFIG`; removes `.env.infra`. |
| `destroy-ocp` | `playbooks/destroy-ocp.yml` | `KUBECONFIG`, `EXTRA_VARS`; resets OCP for reinstall. |
| `destroy-infra` / `destroy-setup` | `playbooks/destroy-infra.yml` / `destroy-setup.yml` | `EXTRA_VARS`; teardown variables from the matching playbook/role. |
| `destroy-caas` / `force-destroy-caas` | CaaS destroy playbooks | `EXTRA_VARS`; uses `osac_namespace` and CaaS discovery/agent variables. Tasks use `/root/.kube/config`. |
| `destroy-bmaas` | `playbooks/destroy-bmaas.yml` | `EXTRA_VARS`; uses BMaaS and BMC variables. Tasks use `/root/.kube/config`. |
| `gather-infra` | `playbooks/gather-infra.yml` | `DEPLOY_SUITE`, `KUBECONFIG`, `EXTRA_VARS`; collects backend and cluster diagnostics. |
| `gather-mgmt-cluster` / `gather-caas` / `gather-bmaas` | Corresponding gather playbook | `EXTRA_VARS`; uses the matching cluster/service variables. These tasks use `/root/.kube/config`. |
| `gather-full` | Calls `gather-caas`, `gather-bmaas`, `gather-infra` | `EXTRA_VARS`; collects the full deployment's diagnostics. |
| `connectivity` | `playbooks/connectivity.yml` | `EXTRA_VARS`; uses connectivity and Netris lab variables. |
| `disk-setup` | `playbooks/disk-setup.yml` | `EXTRA_VARS`; use `disk_setup_enabled` with `setup-infra` to make this step automatic. |
| `cleanup-dns` | `playbooks/cleanup-dns.yml` | `EXTRA_VARS`; uses `lab_name`, DNS zone, OCP identity, and AWS credentials. |
| `destroy-full` | `playbooks/destroy-full.yml` | `EXTRA_VARS`; uses lab, DNS, and AWS settings. This tears down the complete lab. |
| `access-doc` / `post-install` | Access-document playbook | `EXTRA_VARS`; uses OCP identity and OSAC namespace. `post-install` runs the access-document step. Tasks use `/root/.kube/config`. |
| `redeploy-fresh` / `redeploy-continue` | `scripts/redeploy-server.sh` | `DEPLOY_SUITE`, `EXTRA_VARS`; the script supports its operational `caas` and `maas` flows. `redeploy-fresh` destroys the lab and starts over; `redeploy-continue` resumes. |
| `deploy-jump` | `scripts/deploy-jump.sh` | Requires the settings shown in [`scripts/env.sh.example`](scripts/env.sh.example); see the remote deployment section in the README. |
| `vendor-update`, `lint`, `health-check` | Local command/script | No Ansible `EXTRA_VARS` inputs. |

The Makefile passes `EXTRA_VARS` to `deploy-infra`, and that playbook includes
the Netris lab submodule's roles. Options defined by that submodule are also
available there; see
[`netris-lab/group_vars/all.yml`](netris-lab/group_vars/all.yml) for its
defaults. They are separate from the backend variables listed below.

## Ansible `EXTRA_VARS` catalog

The following variables are defined in
[`inventory/group_vars/all.yml`](inventory/group_vars/all.yml). Pass an
override with `EXTRA_VARS` on a target that consumes its group. Defaults and
comments in that file are authoritative.

| Target family | Variables |
|---|---|
| Lab, connectivity, credentials | `netris_lab_dir`, `ew_fabric_enable`, `dns_server`, `netris_username`, `netris_password`, `netris_validate_certs`, `quay_username`, `quay_password`, `vm_root_password`, `config_path`, `lab_name`, `dns_hosted_zone`, `aws_access_key_id`, `aws_secret_access_key`, `aws_region` |
| OCP restore, DNS, and Netris | `ocp_vm_name_pattern`, `ocp_server_vcpu`, `ocp_server_memory_gb`, `ocp_install_disk_gb`, `ocp_lvm_disk_gb`, `ocp_vpc_name`, `ocp_vnet_name`, `ocp_subnet_cidr`, `ocp_gateway`, `ocp_node_ip`, `ocp_vm_mgmt_ip`, `ocp_snat_ip`, `ocp_dnat_ip`, `ocp_underlay_enabled`, `ocp_underlay_server_port`, `ocp_underlay_leaf_index`, `ocp_underlay_leaf_port`, `ocp_underlay_p2p_cidr`, `ocp_underlay_ocp_asn`, `ocp_version`, `ocp_cluster_name`, `ocp_base_domain`, `pull_secret_path`, `ssh_public_key_path`, `snapshot_flavor_image`, `snapshot_flavor_dir`, `snapshot_recert_image`, `ocp_snapshot_disk_gb`, `osac_external_ip_pool_cidr` |
| OSAC install | `osac_repo`, `osac_branch`, `osac_dir`, `osac_installer_dir`, `osac_namespace`, `osac_values_file`, `osac_license_path`, `osac_operator_image`, `fulfillment_service_image`, `osac_aap_image`, `osac_ui_image`, `bmf_operator_image`, `aap_project_git_uri`, `aap_project_git_branch` |
| CaaS setup and cluster creation | `caas_cluster_version`, `caas_discovery_vm_patterns`, `caas_discovery_vcpu`, `caas_discovery_vcpu_overrides`, `caas_discovery_memory_mb`, `caas_discovery_memory_mb_overrides`, `caas_discovery_disk_gb`, `caas_discovery_infraenv_name`, `caas_discovery_infraenv_namespace`, `caas_agent_flow`, `caas_agent_namespace`, `caas_server_cluster_template_name`, `caas_server_cluster_template_gateway`, `caas_host_type_id`, `caas_netris_server_cluster_template_id`, `caas_netris_mgmt_interface`, `caas_netris_vpc_interfaces`, `caas_cluster_template`, `caas_catalog_item`, `caas_catalog_item_title`, `caas_catalog_item_description`, `caas_cluster_name`, `caas_cluster_params`, `caas_resource_class_hostnames`, `caas_agents`; `caas_cluster_order_namespace` for `setup-caas-agents` |
| Bare-metal services and BMaaS | `bare_metal_services`, `bmc_net_name`, `bmc_net_bridge`, `bmc_net_host_ip`, `bmc_net_ocp_ip`, `bmc_net_cidr`, `bmc_net_dhcp_start`, `bmc_net_dhcp_end`, `sushy_tools_ip`, `sushy_tools_port`, `sushy_tools_config_dir`, `sushy_tools_disk_dir`, `sushy_tools_storage_pool`, `bmaas_vm_patterns`, `bmaas_ssh_hosts`, `bmaas_vm_vcpu`, `bmaas_vm_memory_mb`, `bmaas_vm_disk_gb`, `bmaas_bmh_namespace`, `bmaas_instance_type`, `bmaas_instance_type_description`, `bmaas_instance_type_network_ports`, `bmaas_bmi_template`, `bmaas_catalog_item`, `bmaas_catalog_item_title`, `bmaas_catalog_item_description`, `bmaas_auto_eip_catalog_item`, `bmaas_auto_eip_catalog_item_title`, `bmaas_auto_eip_catalog_item_description`, `bmaas_provisioning_enabled`, `bmaas_provisioning_vnet_name`, `bmaas_provisioning_vpc_name`, `bmaas_provisioning_cidr`, `bmaas_provisioning_gateway`, `bmaas_provisioning_dhcp_start`, `bmaas_provisioning_dhcp_end`, `bmaas_provisioning_fabric_iface`, `bmaas_provisioning_snat_ip`, `bmaas_provisioning_snat_cidr`, `bmaas_provisioning_snat_vpc_id` |
| VMaaS / CUDN EVPN | `osac_vmaas_external_ip_pool_cidr`, `ocp_evpn_kubeconfig_path`, `ocp_evpn_required_version`, `ocp_vmaas_external_ip_return_path_acknowledged`, `ocp_vmaas_external_ip_tests_enabled`, `ocp_evpn_vtep_cidr`, `ocp_evpn_vtep_node_ip_cidr`, `ocp_evpn_vtep_dummy_interface` |

`disk_setup_enabled` is an opt-in role variable used by `setup-infra`; it is
not a key in `group_vars/all.yml`. `EXTRA_VARS` may also contain variables
defined by an included role or the Netris lab submodule. Those variables are
target-specific and should be checked at the referenced playbook/role.
