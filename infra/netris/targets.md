# Netris Make Targets and Options

This is the detailed input reference for `infra/netris/Makefile`. It describes
the Make variables, environment/configuration inputs, Ansible extra variables,
and which targets consume them. The root README remains the generic guide for
all backends; the [Netris README](README.md) gives the Netris workflow and
common options.

## Run from the right directory

For an end-to-end run, use the repository root. `INFRA` is required there. The
root pipeline runs `setup-infra` → `deploy-infra` → `deploy-ocp` →
`deploy-osac` → `prepare-suite` (which runs `setup-<DEPLOY_SUITE>`) →
`run-tests` (`test-<TEST_SUITE>`). `IMG` is the same in every command:

```bash
IMG='osac_branch=<branch> fulfillment_service_image=<reg>/fulfillment-service:<tag> osac_operator_image=<reg>/osac-operator:<tag> osac_aap_image=<reg>/osac-aap:<tag>'
make e2e INFRA=netris DEPLOY_SUITE=vmaas EXTRA_VARS="$IMG"                         # VMaaS networking
make e2e INFRA=netris DEPLOY_SUITE=bmaas EXTRA_VARS="$IMG"                         # BMaaS networking
make e2e INFRA=netris DEPLOY_SUITE=full TEST_SUITE=stretched EXTRA_VARS="$IMG"     # stretched VM/BM
```

The image overrides are required for `vmaas` and `full` (the guard in
`check_images.yml`); pass them for `bmaas` too, since the PR branch needs its
own images. `DEPLOY_SUITE=bmaas` runs the slow cross-tenant class unless
`TEST='not TestCrossTenantBmaasNetworking'` is added, as CI does. Every OSAC
install is fresh.

To run backend targets directly, change to `infra/netris/`. The `deploy` target
runs only `deploy-infra` → `deploy-ocp` → `deploy-osac`; it does not prepare a
test suite or run pytest.

## How target arguments work

- `KEY=value` after `make` sets a Make variable. Setting `KEY=value` in the
  shell environment also supplies a Make variable unless the Makefile overrides
  it. The backend Makefile decides whether and how to translate that value.
- `EXTRA_VARS='key=value other_key=value'` or
  `EXTRA_VARS='{"key": "value"}'` passes Ansible extra variables to the
  target's playbook. Only variables read by that playbook and its roles affect
  the target. On the composite `deploy` target, the same `EXTRA_VARS` is passed
  to all three deployment steps.
- The backend adds deployment context and path mappings to Ansible-backed
  targets. OCP targets additionally receive the underlay setting for
  `DEPLOY_SUITE=vmaas` and `DEPLOY_SUITE=full`.
- The backend's inventory defaults are in
  [`inventory/group_vars/all.yml`](inventory/group_vars/all.yml). `EXTRA_VARS`
  can override those defaults for a target that consumes them. Values defined
  by the backend as part of its contract are supplied after `EXTRA_VARS`; use
  their public Make variable instead of colliding with the generated Ansible
  variable.

## Input channels, mappings, and precedence

The channel describes who consumes a value; it is not a naming convention.
For example, a GitHub Actions step may put `EXTRA_VARS` in `env:` so the shell
can read it, then pass it to Make. In the Netris Makefile, that payload becomes
Ansible `-e` arguments. The GitHub step environment itself does not make those
keys Ansible variables or test-process environment variables.

| Channel | Consumer | Use it for | Netris examples |
|---|---|---|---|
| Shell/process environment | A workflow step, local script, or an explicit Ansible `lookup('env', ...)` | Credentials and settings that the process or a lookup reads | `NETRIS_PASSWORD`, `AWS_ACCESS_KEY_ID`, `PULL_SECRET` for `deploy-jump` |
| Make variables | Root/backend Makefiles and commands they launch | Scenario selection and paths that the backend maps to its consumers | `DEPLOY_SUITE`, `KUBECONFIG`, `OSAC_NAMESPACE`, `OSAC_PULL_SECRET_PATH` |
| `EXTRA_VARS` | Ansible playbooks as `-e` variables | Playbook/role configuration overrides | `dns_server`, `osac_branch`, image overrides |
| `.env.infra` | Root `run-tests`, then the OSAC test process | The test runtime connection contract | `KUBECONFIG`, `OSAC_NAMESPACE`, `OSAC_VM_KUBECONFIG`, `OSAC_PULL_SECRET_PATH` |

Ansible's task-level `environment:` setting exports a variable to the command
or module process for that task; it does not declare an Ansible variable. The
backend maps `KUBECONFIG` to the Ansible variable `kubeconfig_path`, and tasks
that run `oc`/`kubectl` set their process environment from that value. Ansible
does not automatically turn every shell environment variable into a playbook
variable; inventory values only read environment variables where they
explicitly use `lookup('env', ...)`.

| Public input | Backend mapping | Consumers |
|---|---|---|
| `DEPLOY_SUITE` | Ansible `deploy_suite`; derives `OSAC_PROFILE` / Ansible `osac_profile`, `osac_values_file` and `bare_metal_services` | Deployment playbooks, OSAC installer, and setup dispatch |
| `BARE_METAL_SERVICES` | Ansible `bare_metal_services` | Derived: true for `caas`, `maas`, `bmaas` and `full`, false for `vmaas` |
| `TEST_SUITE` | No Ansible mapping | Root Make test-target selection only |
| `OSAC_NAMESPACE` | Ansible `osac_namespace` | Deployment/setup tasks and `.env.infra` |
| `KUBECONFIG` | Ansible `kubeconfig_path` | All Netris Ansible targets, installer commands, health check, and `.env.infra`; task commands receive it as `KUBECONFIG` |
| `OSAC_VM_KUBECONFIG` | No Ansible mapping | `.env.infra` only; selects the workload-cluster kubeconfig for tests |
| `OSAC_PULL_SECRET_PATH` | Ansible `pull_secret_path` | Deployment roles and `.env.infra`; `deploy-jump` copies the local source file here on the remote host |
| `EXTRA_VARS` | Ansible `-e` payload | Target-specific role variables; it is not passed to pytest as environment |

Precedence is deliberate:

1. For inventory settings that use the `config` INI lookup, a non-empty
   `[default]` value wins over its environment fallback. A literal inventory
   default is used only where that variable defines one.
2. `EXTRA_VARS` becomes Ansible `-e`; Ansible gives extra variables higher
   precedence than inventory values and variables resolved from inventory
   lookups. See [Ansible variable precedence](https://docs.ansible.com/projects/ansible/latest/user_guide/playbooks_variables.html).
3. The Netris Makefile appends its deployment selectors and canonical path
   mappings after user `EXTRA_VARS`. These backend-owned names therefore come
   from their public Make inputs. For example, set `OSAC_PULL_SECRET_PATH`, not
   `pull_secret_path` in `EXTRA_VARS`. `bare_metal_services` is on the same
   list: setting it in `EXTRA_VARS` has no effect; use the Make variable
   `BARE_METAL_SERVICES`.
4. The `CAAS_*` and `MAAS_*` Ansible values come before `EXTRA_VARS`, so
   `EXTRA_VARS` wins over them. Use the `CAAS_*` and `MAAS_*` Make variables for
   the normal interface and `EXTRA_VARS` for deliberate low-level overrides.

Keep credentials in the ignored `config` file or the supported environment
fallbacks, not in `EXTRA_VARS`: Ansible extra variables are command-line
arguments and may appear in process listings or logs. `PULL_SECRET` has a
different meaning from `OSAC_PULL_SECRET_PATH`: it is the local source file
used by `deploy-jump`; the latter is the remote destination and the path
exported to tests.

Examples:

```bash
# Ansible role options
make e2e INFRA=netris DEPLOY_SUITE=full TEST_SUITE=stretched \
  EXTRA_VARS="$IMG dns_server=<reachable-dns-server>"

# One canonical path used by Ansible, the remote bootstrap, and test runtime
OSAC_PULL_SECRET_PATH=/root/osac-pull-secret make e2e INFRA=netris DEPLOY_SUITE=full EXTRA_VARS="$IMG"
```

## Deployment and test suites

`DEPLOY_SUITE` selects what is deployed: the OSAC profile, the Netris overlay
that profile layers, the setup target and the EVPN underlay. `TEST_SUITE`
selects the OSAC tests and defaults to `DEPLOY_SUITE`. Test code lives in the
OSAC mono-repo and is selected by directory, never by markers.

| `DEPLOY_SUITE` | Base profile and Netris overlay | k8s manager | EVPN underlay | `BARE_METAL_SERVICES` | Setup target | Default `TEST_SUITE` |
|---|---|---|---|---|---|---|
| `caas` (default) | `caas-ci`, no overlay | `cudn_evpn` (profile) | no | true | `setup-caas` | `caas` |
| `maas` | `caas-ci`, no overlay | `cudn_evpn` (profile) | no | true | `setup-maas` | `maas` |
| `bmaas` | `bmaas-ci` + `values/bmaas-ci/netris.yaml` | none (`""`) | no | true | `setup-bmaas` | `bmaas` |
| `vmaas` | `vmaas-ci` + `values/vmaas-ci/netris.yaml` | `cudn_evpn` | yes | false | `setup-vmaas` | `vmaas` |
| `full` | `full-ci` + `values/full-ci/netris.yaml` | `cudn_evpn` | yes | true | `setup-full` | `full` |

`setup-full` runs `setup-caas`, `setup-bmaas` and `setup-vmaas`. The overlay
paths are in the OSAC checkout under `osac-installer/`. Agentless-Net
(`infra/agentless-net`) supports only `caas`; it is a standalone CaaS flow that
the root `make e2e` does not wire up.

| `TEST_SUITE` | Selects | Valid on |
|---|---|---|
| `vmaas` | `tests/e2e/networking/vmaas/` | `vmaas` |
| `bmaas` | `tests/e2e/networking/bmaas/` | `bmaas` only: its `PROTOCOL_ICMP` rules are rejected with cudn_evpn |
| `stretched` | `tests/e2e/networking/stretched/` | `full` |
| `full` | `tests/e2e/` minus `networking/vmaas` and `networking/bmaas`, serial | `full` |
| `caas` | `tests/e2e/caas/` | `caas`, `full` |

`maas`, `caas_networking`, `maas_networking`, `storage`, `metering` and
`references` also exist as `test-<suite>` targets. `tests/e2e/networking/vmaas/`
is one ordered module: never pass `TEST=` (`-k`) to slice it.

The PR E2E commands use the same split:

| PR command | `DEPLOY_SUITE` | `TEST_SUITE` | OSAC profile | Check |
|---|---|---|---|---|
| `/test vmaas-netris` | `vmaas` | `vmaas` | `vmaas-ci` | `E2E / VMaaS Netris Full Install` |
| `/test bmaas-netris` | `bmaas` | `bmaas` | `bmaas-ci` | `E2E / BMaaS Netris Full Install` |
| `/test stretched-netris` | `full` | `stretched` | `full-ci` | `E2E / Stretched Netris Full Install` |

## Make variables

| Variable | Default | Targets and meaning |
|---|---|---|
| `INFRA` | none | Required at the root; no default. Selects the backend under `infra/`. |
| `DEPLOY_SUITE` | `caas` (backend default) | `deploy`, `deploy-infra`, `deploy-ocp`, `deploy-osac`, and suite targets. Supported scenarios are `caas`, `maas`, `bmaas`, `vmaas`, and `full`; selects setup behavior and derives the OSAC profile. |
| `TEST_SUITE` | `$(DEPLOY_SUITE)` | Root orchestration only: selects the contract file and the `test-<suite>` target. |
| `TEST` | none | pytest `-k` filter for `test-<suite>`; never with `test-vmaas`, an ordered module. |
| `EXTRA_VARS` | empty | Ansible-backed targets and the `deploy`/redeploy composites. Passes Ansible variables to the playbook(s) used by the target. |
| `SUITE_STAGE` | `all` | `deploy-suite` stage: `all`, `setup` or `workload`. |
| `OSAC_NAMESPACE` | `osac-e2e-ci` | `deploy-osac`, `destroy-osac`, and the `.env.infra` connection settings. Passed to Ansible and the OSAC installer. |
| `KUBECONFIG` | `/root/.kube/config` | Mapped to Ansible `kubeconfig_path` for every Netris Ansible target, passed to installer and health-check commands, and written to `.env.infra`. |
| `OSAC_VM_KUBECONFIG` | same as `KUBECONFIG` | Written to `.env.infra` for VMaaS/full deployments; for the other scenarios, the backend records the hub kubeconfig as the VM kubeconfig. |
| `OSAC_PULL_SECRET_PATH` | `/root/pull-secret` | Remote pull-secret path used by deployment roles and written to `.env.infra`; `deploy-jump` copies the local source file to this path. |
| `OSAC_PROFILE` | derived | Not an override. The backend derives `caas-ci`, `vmaas-ci`, `bmaas-ci`, or `full-ci` from `DEPLOY_SUITE` and rejects a command-line override. |
| `OSAC_MONO_DIR` | `/opt/osac` | Root `test-<suite>` targets: the OSAC mono-repo checkout that pytest runs in. |
| `BARE_METAL_SERVICES` | derived | Ansible `bare_metal_services`: true for `caas`, `maas`, `bmaas` and `full`, false for `vmaas`. |
| `OSAC_TEST_TENANT_NAME` | `tenant1` | Tenant that the CaaS, MaaS and BMaaS setup and deploy targets use. |
| `CAAS_*`, `MAAS_*` | see below | CaaS and MaaS worker, image and template settings. |

The CaaS variables:

| Variable | Default | Used by |
|---|---|---|
| `CAAS_BAREMETAL_INSTANCE_TYPE` | `ci-worker` | `setup-caas`, `deploy-caas` |
| `CAAS_DISK_IMAGE_NAME` | `rhcos-4-22-amd64` | `setup-caas`, `deploy-caas`; MaaS default |
| `CAAS_DISK_IMAGE_SOURCE_REF` | `oci://quay.io/osac-project/rhcos:4.22.0` | `setup-caas`; MaaS default |
| `CAAS_WORKER_COUNT` | `1` | `setup-caas`, `deploy-caas` |
| `CAAS_WORKER_VCPU` | `4` | `setup-caas` |
| `CAAS_WORKER_MEMORY_MB` | `16384` | `setup-caas` |

The MaaS wrappers have additional Make variables:

| Variable | Default | Used by |
|---|---|---|
| `MAAS_CLUSTER_NAME` | `maas-cluster` | `setup-maas`, `deploy-maas`, and destroy wrappers |
| `MAAS_BAREMETAL_INSTANCE_TYPE` | `g5` | MaaS setup/deploy/destroy wrappers |
| `MAAS_WORKER_COUNT` | `2` | MaaS setup/deploy/destroy wrappers |
| `MAAS_CLUSTER_TEMPLATE` | `osac.templates.ocp_4_20_ai_maas` | MaaS setup/deploy/destroy wrappers |
| `MAAS_CATALOG_ITEM` | `maas-ocp` | MaaS setup/deploy/destroy wrappers |
| `MAAS_DISK_IMAGE_NAME` | `$(CAAS_DISK_IMAGE_NAME)` | MaaS setup/deploy/destroy wrappers |
| `MAAS_DISK_IMAGE_SOURCE_REF` | `$(CAAS_DISK_IMAGE_SOURCE_REF)` | `setup-maas` |
| `MAAS_WORKER_VCPU` | `10` | MaaS setup/deploy/destroy wrappers |
| `MAAS_WORKER_MEMORY_MB` | `20480` | MaaS setup/deploy/destroy wrappers |

`setup-maas` and `deploy-maas` are backend wrappers over the CaaS playbooks;
`maas` is a supported `DEPLOY_SUITE`.

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
| OpenShift pull secret | `OSAC_PULL_SECRET_PATH` (default `/root/pull-secret`) | OCP restore, OSAC install, and tests that mount this path |
| SSH public key | `/root/.ssh/id_rsa.pub` | OCP restore |

`config_path` can also be overridden as an Ansible variable. `netris_password`,
`quay_username`, and `quay_password` should use the config file or their
documented environment fallback rather than `EXTRA_VARS`. `pull_secret_path`
is the internal Ansible name mapped from `OSAC_PULL_SECRET_PATH`; set the
public Make variable instead. `ssh_public_key_path` remains an Ansible
variable.

The shell-script targets have additional environment inputs:

| Variable | Default | Target | Meaning |
|---|---|---|---|
| `SERVER` | none | `deploy-jump` | Required remote server IP/hostname. |
| `LAB_NAME` | none in `deploy-jump` | `deploy-jump`; backend config fallback | Required by `deploy-jump`; selects the per-lab DNS prefix. |
| `PULL_SECRET` | none | `deploy-jump` | Required local path to the OpenShift pull-secret file; copied to the remote `OSAC_PULL_SECRET_PATH`. |
| `OSAC_PULL_SECRET_PATH` | `/root/pull-secret` | `deploy-jump` | Remote path for the copied pull secret; the same value is passed to the server-side Make deployment and exported for tests. |
| `NETRIS_PASSWORD` | none | `deploy-jump` | Required; written to the remote `config` file over SSH, not placed in the SSH command. |
| `QUAY_USERNAME`, `QUAY_PASSWORD` | empty | `deploy-jump` | Set both or neither; written to the remote `config` for private snapshot flavor pulls. |
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
| `prep-osac` | `playbooks/prep-osac.yml` | `DEPLOY_SUITE`, `KUBECONFIG`, `EXTRA_VARS`; prepares OSAC and generates `values/<profile>/netris-runtime.yaml`. |
| `run-osac-setup` | OSAC installer (`install-infra`, then `install-osac`) | `DEPLOY_SUITE`, `OSAC_NAMESPACE`, `KUBECONFIG`; consumes the OCP EVPN values from `deploy-ocp`, the profile's `netris.yaml`, and the runtime values. |
| `setup-vmaas` | `playbooks/setup-vmaas.yml` | `KUBECONFIG`, `EXTRA_VARS`; uses the EVPN validation and VMaaS return-path variables below. |
| `gather-vmaas` | `playbooks/gather-infra.yml` | `KUBECONFIG`, `EXTRA_VARS`; collects infrastructure/EVPN state. |
| `destroy-vmaas` | No-op | VMaaS setup creates no tenant resources. Cluster prerequisites are removed by `destroy-ocp`. |
| `setup-caas` | `playbooks/setup-caas.yml` | `CAAS_*`, `OSAC_TEST_TENANT_NAME`, `EXTRA_VARS`; prepares BMaaS worker VMs, registers worker types, the DiskImage, the Netris ExternalIPPool, and the catalog. Task commands use `kubeconfig_path` (default `/root/.kube/config`). |
| `deploy-caas` | `playbooks/deploy-caas.yml` | `CAAS_*`, `OSAC_TEST_TENANT_NAME`, `EXTRA_VARS`; creates the CaaS catalog cluster with one `ci-worker` BareMetalInstanceType. Task commands use `kubeconfig_path` (default `/root/.kube/config`). |
| `setup-bmc` | `playbooks/setup-bmc.yml` | `EXTRA_VARS`; uses `bare_metal_services`, `bmc_net_*`, and `sushy_tools_*`. |
| `setup-bmaas` | `playbooks/setup-bmaas.yml` | `DEPLOY_SUITE=bmaas`, `EXTRA_VARS`; uses BMC, BMaaS, and BM provisioning-network variables. Appends the BMaaS keys to `.env.infra`. Task commands use `kubeconfig_path` (default `/root/.kube/config`). |
| `deploy-bmaas` | `playbooks/deploy-bmaas.yml` | `EXTRA_VARS`; creates one persistent BareMetalInstance in each test tenant. Task commands use `kubeconfig_path` (default `/root/.kube/config`). |
| `setup-full` | Calls `setup-caas`, `setup-bmaas`, then `setup-vmaas` | `DEPLOY_SUITE=full`, `EXTRA_VARS`; prepares the CaaS, BMaaS and VMaaS prerequisites. |
| `setup-maas` | `playbooks/setup-caas.yml` with `MAAS_EXTRA` | `MAAS_*`, `OSAC_TEST_TENANT_NAME`, `EXTRA_VARS`; configures all three BMaaS worker VMs as 10 vCPU / 20GiB `g5` hosts. Task commands use `kubeconfig_path` (default `/root/.kube/config`). |
| `deploy-maas` | `playbooks/deploy-caas.yml` with `MAAS_EXTRA` | `MAAS_*`, `OSAC_TEST_TENANT_NAME`, `EXTRA_VARS`; creates the MaaS catalog cluster with two `g5` BareMetalInstanceTypes. Task commands use `kubeconfig_path` (default `/root/.kube/config`). |
| `destroy-maas` / `force-destroy-maas` | CaaS destroy playbooks with `MAAS_EXTRA` | MaaS Make variables above plus `EXTRA_VARS`. Task commands use `kubeconfig_path` (default `/root/.kube/config`). |
| `destroy-suite` | Backend target; runs `destroy-<DEPLOY_SUITE>` | `DEPLOY_SUITE`, `EXTRA_VARS`; the backend rejects `full` because that scenario shares the whole lab. |
| `destroy-osac` | OSAC installer uninstall | `DEPLOY_SUITE`, `OSAC_NAMESPACE`, `KUBECONFIG`; removes `.env.infra`. |
| `destroy-ocp` | `playbooks/destroy-ocp.yml` | `KUBECONFIG`, `EXTRA_VARS`; resets OCP for reinstall. It removes the default kubeconfig only when `KUBECONFIG` is `/root/.kube/config`, preserving a custom path supplied by the caller. |
| `destroy-infra` / `destroy-setup` | `playbooks/destroy-infra.yml` / `destroy-setup.yml` | `EXTRA_VARS`; teardown variables from the matching playbook/role. |
| `destroy-caas` / `force-destroy-caas` | CaaS destroy playbooks | `OSAC_TEST_TENANT_NAME`, `EXTRA_VARS`; `destroy-caas` deletes the named OSAC cluster and retains reusable BMaaS hosts, `force-destroy-caas` force-cleans stuck orders, removes worker BMHs, wipes worker VMs, and sweeps Netris orphans. Task commands use `kubeconfig_path` (default `/root/.kube/config`). |
| `destroy-bmaas` | `playbooks/destroy-bmaas.yml` | `EXTRA_VARS`; uses BMaaS and BMC variables. Task commands use `kubeconfig_path` (default `/root/.kube/config`). |
| `prepare-suite` | Runs `setup-<DEPLOY_SUITE>` | `DEPLOY_SUITE`, `EXTRA_VARS`; the suite preparation that `make e2e` runs before pytest. Tenants are created by the pytest fixtures. |
| `deploy-suite` | `prepare-suite`, then the persistent deploy playbook | `DEPLOY_SUITE` (`caas`, `maas` or `bmaas`), `SUITE_STAGE`, `EXTRA_VARS`; prepares pytest tenants, Netris tenant networking and the persistent workloads on an installed lab. Unrelated to the `DEPLOY_SUITE` variable. |
| `test-caas-networking`, `test-maas-networking` | Root `run-tests` with `TEST_SUITE=caas_networking` or `maas_networking` | Run dedicated OSAC suites when the OSAC path exists; fail otherwise. |
| `gather-infra` | `playbooks/gather-infra.yml` | `DEPLOY_SUITE`, `KUBECONFIG`, `EXTRA_VARS`; collects backend and cluster diagnostics. |
| `gather-mgmt-cluster` / `gather-caas` / `gather-bmaas` | Corresponding gather playbook | `EXTRA_VARS`; uses the matching cluster/service variables. These commands use `kubeconfig_path` (default `/root/.kube/config`). |
| `gather-full` | Calls `gather-caas`, `gather-bmaas`, `gather-infra` | `EXTRA_VARS`; collects the full deployment's diagnostics. |
| `connectivity` | `playbooks/connectivity.yml` | `EXTRA_VARS`; uses connectivity and Netris lab variables. |
| `disk-setup` | `playbooks/disk-setup.yml` | `EXTRA_VARS`; use `disk_setup_enabled` with `setup-infra` to make this step automatic. |
| `cleanup-dns` | `playbooks/cleanup-dns.yml` | `EXTRA_VARS`; uses `lab_name`, DNS zone, OCP identity, and AWS credentials. |
| `destroy-full` | `playbooks/destroy-full.yml` | `EXTRA_VARS`; uses lab, DNS, and AWS settings. This tears down the complete lab. |
| `access-doc` / `post-install` | Access-document playbook | `EXTRA_VARS`; uses OCP identity and OSAC namespace. `post-install` generates the access document. Task commands use `kubeconfig_path` (default `/root/.kube/config`). |
| `redeploy-fresh` / `redeploy-continue` | `scripts/redeploy-server.sh` | `DEPLOY_SUITE`, `EXTRA_VARS`; the script supports the `caas`, `maas`, and `bmaas` flows. `redeploy-fresh` destroys the lab and starts over; `redeploy-continue` resumes. |
| `deploy-jump` | `scripts/deploy-jump.sh` | Requires the settings shown in [`scripts/env.sh.example`](scripts/env.sh.example); see the remote deployment section in the README. |
| `vendor-update`, `lint`, `health-check` | Local command/script | No Ansible `EXTRA_VARS` inputs. |

The Makefile passes `EXTRA_VARS` to `deploy-infra`, and that playbook includes
the Netris lab submodule's roles. Options defined by that submodule are also
available there; see
[`netris-lab/group_vars/all.yml`](netris-lab/group_vars/all.yml) for its
defaults. They are separate from the backend variables listed below.

## Ansible `EXTRA_VARS` catalog

Every key of [`inventory/group_vars/all.yml`](inventory/group_vars/all.yml)
is listed below with its default and the roles, playbooks or scripts that read
it. Pass an override with `EXTRA_VARS` on a target that consumes the key.
Defaults and comments in that file are authoritative. CI fails when this
catalog and `all.yml` list different keys.

### Lab, connectivity, and credentials

| Key | Default | Consumed by |
|---|---|---|
| `netris_lab_dir` | `{{ inventory_dir }}/../netris-lab` | `connectivity playbook`, `deploy-infra`, `deploy-ocp playbook`, `destroy-full playbook`, `destroy-infra`, `destroy-setup playbook`, `gather-caas playbook`, `gather-infra`, `setup-infra` |
| `ew_fabric_enable` | `0` | `deploy-infra`, `netris-lab` |
| `dns_server` | `8.8.8.8` | `configure-netris`, `netris-lab`, `prep-osac`, `prepare-caas-network`, `restore-ocp` |
| `netris_username` | `netris` | `access-doc`, `netris-lab`, `prep-osac` |
| `netris_password` | `config` `netris_password`, then `NETRIS_PASSWORD` | `access-doc`, `deploy-infra`, `force-destroy-caas`, `gather-infra`, `netris-lab`, `prep-osac` |
| `netris_validate_certs` | `false` | `configure-caas-tenant-network`, `configure-netris`, `destroy-bmaas`, `gather-infra`, `netris-lab`, `setup-bmaas` |
| `quay_username` | `config` `quay_username`, then `QUAY_USERNAME` | `cache-snapshot` |
| `quay_password` | `config` `quay_password`, then `QUAY_PASSWORD` | `cache-snapshot` |
| `vm_root_password` | `netris` | `netris-lab` |
| `config_path` | `{{ inventory_dir }}/../config` | other keys in `all.yml` only |
| `lab_name` | `config` `lab_name`, then `LAB_NAME` | `access-doc`, `configure-dns`, `deploy-infra`, `deploy-jump.sh` |
| `dns_hosted_zone` | `config` `dns_hosted_zone`, then `DNS_HOSTED_ZONE` | `cleanup-dns playbook`, `configure-dns`, `destroy-full playbook`, `prep-osac` |
| `aws_access_key_id` | `config` `aws_access_key_id`, then `AWS_ACCESS_KEY_ID` | `cleanup-dns playbook`, `configure-dns`, `deploy-infra`, `deploy-jump.sh`, `destroy-full playbook`, `prep-osac` |
| `aws_secret_access_key` | `config` `aws_secret_access_key`, then `AWS_SECRET_ACCESS_KEY` | `cleanup-dns playbook`, `configure-dns`, `deploy-infra`, `deploy-jump.sh`, `destroy-full playbook`, `prep-osac` |
| `aws_region` | `config` `aws_region`, then `AWS_DEFAULT_REGION` | `cleanup-dns playbook`, `configure-dns`, `destroy-full playbook` |

### Common deployment paths

| Key | Default | Consumed by |
|---|---|---|
| `pull_secret_path` | `/root/pull-secret` | `Makefile`, `create-caas`, `deploy-infra`, `prep-osac`, `restore-ocp` |
| `kubeconfig_path` | `/root/.kube/config` | `Makefile`, `access-doc`, `configure-cudn-evpn`, `destroy-ocp playbook`, `gather-infra`, `prep-osac`, `restore-ocp`, `setup-vmaas playbook` |

### Netris networking contract

| Key | Default | Consumed by |
|---|---|---|
| `netris_tenant_network_cidr` | `192.168.101.0/24` | `configure-caas-tenant-network`, `create-caas`, `prep-osac` |
| `netris_tenant_subnet_cidr` | `192.168.101.0/24` | `configure-caas-tenant-network`, `prep-osac` |
| `netris_k8s_manager_suites` | `[vmaas, full]` | `deploy-ocp playbook`, `prep-osac` |
| `netris_private_allocation_cidr` | `10.0.0.0/8` | `prep-osac` |
| `netris_extra_excluded_cidrs` | `[]` | `prep-osac` |

### VMaaS and CUDN EVPN

| Key | Default | Consumed by |
|---|---|---|
| `osac_vmaas_external_ip_pool_cidr` | `172.27.0.0/16` | `deploy-infra` |
| `ocp_evpn_required_version` | `4.22` | `configure-cudn-evpn`, `setup-vmaas playbook` |
| `ocp_vmaas_external_ip_return_path_acknowledged` | `false` | `configure-cudn-evpn`, `deploy-infra`, `setup-vmaas playbook` |
| `ocp_vmaas_external_ip_tests_enabled` | `false` | `configure-cudn-evpn`, `deploy-infra`, `setup-vmaas playbook` |
| `ocp_evpn_vtep_cidr` | `198.18.0.0/24` | `configure-cudn-evpn` |
| `ocp_evpn_vtep_node_ip_cidr` | `198.18.0.1/32` | `configure-cudn-evpn` |
| `ocp_evpn_vtep_dummy_interface` | `evpn-vtep0` | `configure-cudn-evpn` |

### OCP restore, DNS, and Netris

| Key | Default | Consumed by |
|---|---|---|
| `ocp_vm_name_pattern` | `hgx-pod00-su0-h00` | `configure-netris`, `deploy-infra`, `destroy-ocp playbook`, `gather-infra`, `ocp_uplink.py`, `reserve-ocp-bmc` |
| `ocp_server_vcpu` | `20` | `restore-ocp` |
| `ocp_server_memory_gb` | `64` | `restore-ocp` |
| `ocp_install_disk_gb` | `300` | `destroy-ocp playbook` |
| `ocp_lvm_disk_gb` | `300` | `destroy-ocp playbook`, `restore-ocp` |
| `ocp_vpc_name` | `ocp-sno` | `configure-netris`, `prep-osac` |
| `ocp_vnet_name` | `ocp-sno-vnet` | `configure-netris` |
| `ocp_subnet_cidr` | `192.168.40.0/24` | `configure-netris`, `deploy-infra`, `prep-osac`, `restore-ocp` |
| `ocp_gateway` | `192.168.40.1/24` | `configure-netris`, `restore-ocp` |
| `ocp_node_ip` | `192.168.40.2` | `configure-netris`, `restore-ocp` |
| `ocp_vm_mgmt_ip` | empty | `configure-netris`, `ocp_uplink.py` |
| `ocp_snat_ip` | `198.51.100.1` | `configure-netris`, `prep-osac`, `setup-bmaas` |
| `ocp_dnat_ip` | `198.51.100.2` | `access-doc`, `cleanup-dns playbook`, `configure-dns`, `configure-netris`, `destroy-full playbook`, `gather-mgmt-cluster`, `prep-osac`, `restore-ocp` |
| `ocp_underlay_enabled` | `false` | `Makefile`, `configure-cudn-evpn`, `configure-netris`, `deploy-infra`, `netris-lab`, `setup-vmaas playbook` |
| `ocp_underlay_server_port` | `eth10` | `configure-netris`, `deploy-infra` |
| `ocp_underlay_leaf_index` | `1` | `configure-cudn-evpn`, `deploy-infra`, `netris-lab` |
| `ocp_underlay_leaf_port` | `swp1s0` | `deploy-infra` |
| `ocp_underlay_p2p_cidr` | `192.0.2.0/31` | `configure-cudn-evpn`, `deploy-infra`, `netris-lab`, `setup-vmaas playbook` |
| `ocp_underlay_ocp_asn` | `4209999999` | `configure-cudn-evpn`, `deploy-infra`, `netris-lab` |
| `osac_external_ip_pool_cidr` | `198.51.100.24/29` | `create-caas`, `deploy-infra`, `setup-caas` |
| `osac_external_ip_pool_name` | `netris-lab-public` | `setup-caas` |
| `ocp_version` | `4.22` | other keys in `all.yml` only |
| `ocp_cluster_name` | `ocp-sno` | `access-doc`, `cleanup-dns playbook`, `configure-dns`, `destroy-full playbook`, `destroy-ocp playbook`, `gather-infra`, `restore-ocp` |
| `ocp_base_domain` | `{{ lab_name }}.{{ dns_hosted_zone }}` | `access-doc`, `cleanup-dns playbook`, `configure-dns`, `destroy-full playbook`, `gather-infra`, `prep-osac`, `restore-ocp` |
| `ssh_public_key_path` | `/root/.ssh/id_rsa.pub` | `configure-netris`, `create-caas`, `deploy-bmaas`, `prep-osac`, `restore-ocp` |
| `snapshot_flavor_image` | `quay.io/osac-project/cluster-flavors:sno-4-22` | `cache-snapshot` |
| `snapshot_flavor_dir` | `/var/cache/netris-lab/cluster-flavors/sno-4-22` | `cache-snapshot`, `configure-cudn-evpn`, `gather-mgmt-cluster`, `restore-ocp` |
| `snapshot_recert_image` | `quay.io/osac-project/recert:latest` | `restore-ocp` |
| `ocp_snapshot_disk_gb` | `300` | `restore-ocp` |

### OSAC install

| Key | Default | Consumed by |
|---|---|---|
| `osac_repo` | `https://github.com/osac-project/osac.git` | `prep-osac`, `setup-infra` |
| `osac_branch` | `main` | `prep-osac`, `setup-infra` |
| `osac_dir` | `/opt/osac` | `prep-osac`, `prepare-osac-e2e-tenant` |
| `osac_installer_dir` | `{{ osac_dir }}/osac-installer` | `prep-osac` |
| `osac_namespace` | `osac-e2e-ci` | `Makefile`, `access-doc`, `configure-caas-tenant-network`, `create-caas`, `destroy-bmaas`, `destroy-caas`, `force-destroy-caas`, `gather-bmaas`, `gather-caas`, `gather-mgmt-cluster`, `prep-osac`, `prepare-caas-bmaas`, `prepare-caas-network`, `prepare-osac-e2e-tenant`, `setup-bmaas`, `setup-caas`, `setup-vmaas playbook` |
| `osac_test_tenant_name` | `tenant1` | `Makefile`, `configure-caas-tenant-network`, `create-caas`, `destroy-caas`, `force-destroy-caas` |
| `osac_values_file` | `values/caas-ci/instance.yaml` | `Makefile`, `prep-osac` |
| `osac_license_path` | `{{ inventory_dir }}/../license.zip` | `deploy-infra`, `prep-osac` |
| `osac_operator_image` | empty | `prep-osac` |
| `fulfillment_service_image` | empty | `prep-osac` |
| `osac_aap_image` | empty | `prep-osac` |
| `osac_ui_image` | empty | `prep-osac` |
| `bmf_operator_image` | empty | `prep-osac` |
| `aap_project_git_uri` | empty | `prep-osac` |
| `aap_project_git_branch` | empty | `prep-osac` |

### CaaS setup and cluster creation

| Key | Default | Consumed by |
|---|---|---|
| `caas_cluster_version` | `{{ ocp_version }}.0` | `create-caas`, `setup-caas` |
| `caas_worker_vm_patterns` | list, see `all.yml` | `create-caas`, `force-destroy-caas`, `prepare-caas-bmaas`, `setup-caas` |
| `caas_worker_vcpu` | `4` | `Makefile`, `prepare-caas-bmaas` |
| `caas_worker_memory_mb` | `16384` | `Makefile`, `prepare-caas-bmaas` |
| `caas_worker_disk_gb` | `100` | `prepare-caas-bmaas` |
| `caas_server_cluster_template_name` | `ns-only` | `configure-netris`, `prep-osac` |
| `caas_server_cluster_template_gateway` | `192.168.101.1/24` | `configure-netris` |
| `caas_netris_ingress_ip` | `192.168.101.28` | `configure-caas-tenant-network` |
| `caas_tenant_dhcp_start` | `192.168.101.100` | `configure-caas-tenant-network` |
| `caas_tenant_dhcp_end` | `192.168.101.254` | `configure-caas-tenant-network` |
| `caas_baremetal_instance_type` | `ci-worker` | `Makefile`, `create-caas`, `prepare-caas-bmaas`, `setup-caas` |
| `caas_baremetal_instance_type_profiles` | list, see `all.yml` | `prep-osac`, `prepare-caas-bmaas`, `setup-caas` |
| `caas_baremetal_instance_type_fabric_port` | list, see `all.yml` | `setup-caas` |
| `caas_netris_server_cluster_template_id` | `2` | `setup-caas` |
| `caas_netris_mgmt_interface` | `ens4` | `prep-osac`, `setup-caas` |
| `caas_netris_vpc_interfaces` | list, see `all.yml` | `prep-osac`, `setup-caas` |
| `caas_cluster_template` | `osac.templates.ocp_small` | `Makefile`, `setup-caas` |
| `caas_catalog_item` | `netris-ocp-small` | `Makefile`, `create-caas`, `setup-caas` |
| `caas_catalog_item_title` | `Netris OpenShift Small` | `Makefile`, `setup-caas` |
| `caas_catalog_item_description` | `OpenShift cluster for CaaS testing on Netris` | `Makefile`, `setup-caas` |
| `caas_cluster_name` | `caas-ci-cluster` | `Makefile`, `create-caas`, `destroy-caas`, `force-destroy-caas` |
| `caas_worker_count` | `1` | `Makefile`, `create-caas`, `setup-caas` |

### Bare-metal services and BMaaS

| Key | Default | Consumed by |
|---|---|---|
| `bare_metal_services` | `false` | `Makefile`, `deploy-infra`, `destroy-ocp playbook`, `netris-lab`, `prep-osac`, `prepare-caas-bmaas`, `restore-ocp`, `setup-bmc` |
| `bmc_net_name` | `bmc-net` | `destroy-infra`, `netris-lab`, `prepare-caas-bmaas`, `reserve-ocp-bmc`, `setup-bmaas`, `setup-bmc` |
| `bmc_net_bridge` | `br-bmc` | `setup-bmc` |
| `bmc_net_host_ip` | `10.99.0.1` | other keys in `all.yml` only |
| `bmc_net_ocp_ip` | `10.99.0.254` | `prep-osac`, `reserve-ocp-bmc`, `setup-bmaas`, `setup-bmc` |
| `bmc_net_cidr` | `10.99.0.0/24` | `prep-osac` |
| `bmc_net_dhcp_start` | `10.99.0.10` | none (kept as an override point) |
| `bmc_net_dhcp_end` | `10.99.0.50` | none (kept as an override point) |
| `sushy_tools_ip` | `{{ bmc_net_host_ip }}` | `setup-bmaas`, `setup-bmc` |
| `sushy_tools_port` | `8000` | `setup-bmaas`, `setup-bmc` |
| `sushy_tools_config_dir` | `/etc/sushy-tools` | `gather-bmaas`, `setup-bmc` |
| `sushy_tools_disk_dir` | `/tmp/virtual-bmh-disks` | `destroy-bmaas`, `force-destroy-caas`, `prepare-caas-bmaas`, `setup-bmaas`, `setup-bmc` |
| `sushy_tools_storage_pool` | `bmh-disks` | `setup-bmc` |
| `bmaas_vm_patterns` | list, see `all.yml` | `deploy-bmaas`, `destroy-bmaas`, `prepare-caas-bmaas`, `setup-bmaas` |
| `bmaas_ssh_hosts` | list, see `all.yml` | `setup-bmaas` |
| `bmaas_vm_vcpu` | `4` | `prepare-caas-bmaas`, `setup-bmaas` |
| `bmaas_vm_memory_mb` | `8192` | `prepare-caas-bmaas`, `setup-bmaas` |
| `bmaas_vm_disk_gb` | `50` | `prepare-caas-bmaas`, `setup-bmaas` |
| `bmaas_bmh_namespace` | `host-inventory` | `destroy-bmaas`, `force-destroy-caas`, `gather-bmaas`, `gather-caas`, `gather-mgmt-cluster`, `prep-osac`, `prepare-caas-bmaas`, `setup-bmaas`, `setup-caas` |
| `bmaas_bmit_name` | `default` | `deploy-bmaas`, `prepare-caas-bmaas`, `setup-bmaas` |
| `bmaas_create_catalog_items` | `true` | `deploy-bmaas`, `prepare-caas-bmaas`, `setup-bmaas` |
| `bmaas_vm_interfaces` | list, see `all.yml` | `setup-bmaas` |
| `bmaas_bmi_template` | `osac.templates.bm_host_provisioning` | `setup-bmaas` |
| `bmaas_catalog_item` | `ci-bm-default` | `deploy-bmaas`, `destroy-bmaas`, `setup-bmaas` |
| `bmaas_catalog_item_title` | `CI Default Bare Metal Instance` | `setup-bmaas` |
| `bmaas_catalog_item_description` | `Bare metal instance for CI testing` | `setup-bmaas` |
| `bmaas_auto_eip_catalog_item` | `ci-bm-auto-eip` | `destroy-bmaas`, `setup-bmaas` |
| `bmaas_auto_eip_catalog_item_title` | `CI Bare Metal Instance with Auto External IP` | `setup-bmaas` |
| `bmaas_auto_eip_catalog_item_description` | `Bare metal instance with automatic ExternalIP attachment for CI testing` | `setup-bmaas` |
| `bmaas_deploy_disk_image_name` | `test-infra-fedora-bmi-44` | `deploy-bmaas`, `setup-bmaas` |
| `bmaas_deploy_disk_image_source_ref` | `oci://quay.io/osac-project/fedora-cloud-bmi:44` | `setup-bmaas` |
| `bmaas_deploy_tenants` | list, see `all.yml` | `deploy-bmaas`, `destroy-bmaas` |
| `bmaas_deploy_bmi_name_prefix` | `test-infra-bmi` | `deploy-bmaas`, `destroy-bmaas` |
| `bmaas_provisioning_enabled` | `true` | `destroy-bmaas`, `setup-bmaas` |
| `bmaas_provisioning_vnet_name` | `bm-provisioning` | `destroy-bmaas`, `prep-osac`, `setup-bmaas` |
| `bmaas_provisioning_vpc_name` | `bm-provisioning` | `destroy-bmaas`, `setup-bmaas` |
| `bmaas_provisioning_cidr` | `10.98.0.0/24` | `setup-bmaas` |
| `bmaas_provisioning_gateway` | `10.98.0.1/24` | `setup-bmaas` |
| `bmaas_provisioning_dhcp_start` | `10.98.0.2` | `setup-bmaas` |
| `bmaas_provisioning_dhcp_end` | `10.98.0.4` | `setup-bmaas` |
| `bmaas_provisioning_fabric_iface` | `eth9` | `setup-bmaas` |
| `bmaas_provisioning_snat_ip` | `198.51.100.33` | `setup-bmaas` |
| `bmaas_provisioning_snat_cidr` | `198.51.100.32/29` | `deploy-infra`, `prep-osac`, `setup-bmaas` |
| `bmaas_provisioning_snat_vpc_id` | `1` | `destroy-bmaas`, `setup-bmaas` |

`disk_setup_enabled` is an opt-in role variable used by `setup-infra`; it is
not a key in `group_vars/all.yml`. `EXTRA_VARS` may also contain variables
defined by an included role or the Netris lab submodule. Those variables are
target-specific and should be checked at the referenced playbook/role.

## Lab facts

Tenant CIDRs: prep-osac always adds all of `10.0.0.0/8` (the lab's Netris private allocation, `netris_private_allocation_cidr`) to `global.networking.excludedIpv4Cidrs` and asserts that the default tenant network and subnet are outside it. The lab sets no 10.x tenant network; the E2E suites use 172.16-172.31 except 172.27 (ExternalIP test pools) and 172.30 (OpenShift services). Keep any you add inside that set.

The VMaaS and stretched tests reach guests through a helper Pod, not `virtctl`. The pytest host needs `kubectl` or `oc`, `ssh` and `ssh-keygen`; the cluster must be able to pull `OSAC_E2E_SSH_HELPER_IMAGE` (default: the digest-pinned `ghcr.io/osac-project/osac-aap` image the cluster already pulls for AAP jobs; the lab exports nothing, set it to the deployed tag or a mirror only when ghcr.io is unreachable).

`prep-osac` patches the cloned aap chart (`osac-aap/charts/aap/templates/config-as-code-secret.yaml`) to accept the lab's plain-HTTP Netris controller URL. There are no DNS settings: VMs use the cluster DNS that OVN-K DHCP gives, and bare metal uses the Netris default DHCP option set.

`deploy-osac` writes `infra/netris/.env.infra` with `KUBECONFIG`, `OSAC_NAMESPACE`, `OSAC_VM_KUBECONFIG` and
`OSAC_PULL_SECRET_PATH`. `setup-bmaas` appends the BMaaS keys (`OSAC_BMH_SSH_HOSTS`, `OSAC_BMI_INSTANCE_TYPE`,
`OSAC_BMI_NETWORK_INTERFACE`) that the contracts in `infra/netris/contracts/<test-suite>` require (`bmaas`,
`stretched`, `full`; `vmaas` needs only `KUBECONFIG`, `OSAC_NAMESPACE`, `OSAC_VM_KUBECONFIG`). `setup-vmaas` adds `OSAC_NETRIS_VM_EXTERNAL_PATH_ENABLED=true` only when the `172.27.0.0/16`
return path is acknowledged.

## Old names

| Old | New |
|---|---|
| `SUITE=vmaas_networking` | `DEPLOY_SUITE=vmaas` |
| `SUITE=bmaas_networking` | `DEPLOY_SUITE=bmaas` |
| `SUITE=stretched_networking` | `DEPLOY_SUITE=full TEST_SUITE=stretched` |
| `SUITE=vmaas` (generic tree) | `DEPLOY_SUITE=full` |
| `OSAC_PROFILE=cudn-evpn-netris-test` | removed; `DEPLOY_SUITE=vmaas` derives `vmaas-ci` and its `netris.yaml` |
| `netris-private.local.yaml` | `netris-runtime.yaml` |
| `OSAC_DEPLOY_MODE=fresh` | removed (every install is fresh) |
| `OSAC_DEPLOY_MODE=snapshot`, `make deploy-fast` | removed; the OCP SNO snapshot restore stays |

`make` stops with an error when `SUITE` or `OSAC_DEPLOY_MODE` is set, at the root and in `infra/netris`.
