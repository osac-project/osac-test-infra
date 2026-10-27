# OSAC Test Infrastructure

> [!IMPORTANT]
> **The e2e test suite has moved (OSAC-3593).** Test suites now live in the
> [osac mono-repo](https://github.com/osac-project/osac). **Do not add, modify,
> or delete tests in this repository** — make those changes in the mono-repo
> instead. The `tests/` directory here is retained only for the infrastructure
> backend contract and a placeholder; new tests added here will not be
> maintained.

Unified repository for OSAC end-to-end infrastructure and test orchestration. Pluggable backends provision clusters; active pytest suites run from the OSAC mono-repo.

**AI coding agents**: see [AGENTS.md](AGENTS.md) for repository constraints and infrastructure workflows. E2E test guidance is maintained in the [OSAC mono-repo](https://github.com/osac-project/osac/blob/main/tests/e2e/AGENTS.md).

## Architecture

The repo has two layers:

- **`infra/`** — infrastructure backends that provision a cluster and deploy OSAC. Each backend lives in its own directory and can use any technology (Ansible, shell, Go, etc.).
- **OSAC mono-repo** — the active pytest E2E suites live in the [osac mono-repo](https://github.com/osac-project/osac), under `tests/e2e/`. They are infrastructure-agnostic and consume environment variables rather than knowing which backend provisioned the cluster. This repository's `tests/` directory is legacy scaffolding; do not add or change E2E tests here.

A **contract** connects the layers. Each backend implements standard Makefile targets (`setup-infra`, `deploy-infra`, `deploy-osac`, etc.) and writes `.env.infra` with the configuration its suites need. Optional shell-sourceable suite contracts live at `infra/<backend>/contracts/<suite>` and list required environment variables. The top-level Makefile orchestrates the full flow.

```
infra/<backend>/                    osac/tests/e2e/<suite>/
┌──────────────────────┐            ┌──────────────────────┐
│ setup-infra          │            │                      │
│ deploy-infra         │            │ uv run pytest        │
│ deploy-osac ──────────── .env.infra ──▶                │
│ setup-<suite>        │            │                      │
│ destroy-osac         │            │                      │
│ destroy-infra        │            │                      │
│ gather-infra         │
│ gather-<suite>       │            │                      │
└──────────────────────┘            └──────────────────────┘
```

### Infrastructure Backends

| Backend | Technology | Description | Deploy Time |
|---------|-----------|-------------|-------------|
| **netris** | Ansible | Simulated Netris Spectrum-X GPU cluster with OCP and OSAC on KVM/libvirt | ~25 min (snapshot) / ~2h (full) |

### Netris deployment scenarios, test targets, and profiles

Run these commands from the repository root. Use either of these paths:

- `make test-...` runs pytest against an OSAC deployment that already exists.
- `make e2e INFRA=netris SUITE=...` provisions the Netris lab, deploys OCP and
  OSAC, prepares the deployment scenario, and runs its selected tests.

`SUITE` names the deployment scenario. It selects the Netris setup path,
Ansible behavior, default OSAC profile, and corresponding Make test target.
`TEST_SCOPE` chooses service tests, Netris networking tests, or both for
VMaaS/BMaaS. For a networking-only rerun, keep `SUITE=vmaas` and set
`TEST_SCOPE=networking`. Test code and fixtures live in the OSAC mono-repo.

| `SUITE` | Default `OSAC_PROFILE` | `make setup-suite` selects | Test target | Deployment and test behavior |
|---|---|---|---|---|
| `vmaas` | `vmaas-ci` + `values/vmaas-ci/netris.yaml` | `setup-vmaas` | `test-vmaas` | VMaaS with the CUDN/EVPN underlay. `TEST_SCOPE` selects service, networking, or both. |
| `caas` | `caas-ci` | `setup-caas` | `test-caas` | CaaS lifecycle E2E. It is not included in `full`. |
| `bmaas` | `bmaas-ci` + `values/bmaas-ci/netris.yaml` | `setup-bmaas` | `test-bmaas` | BMaaS lifecycle and Netris tests. `TEST_SCOPE` selects service, networking, or both. |
| `stretched` | `full-ci` + `values/full-ci/netris.yaml` | `setup-stretched` | `test-stretched` | Prepares BMaaS and VMaaS, skips CaaS, and tests a shared-subnet VM↔BM flow. |
| `full` | `full-ci` + `values/full-ci/netris.yaml` | `setup-full` | `test-full` | Broad serial E2E plus VMaaS, BMaaS, and stretched Netris networking. Skips CaaS, slow cross-tenant BMaaS, and external-IP cases. |

For `SUITE=vmaas` or `SUITE=bmaas`, `TEST_SCOPE` accepts:

| `TEST_SCOPE` | Tests run |
|---|---|
| `service` (default) | Service lifecycle and API tests; Netris-specific networking tests are skipped. |
| `networking` | Netris-specific connectivity, isolation, and dataplane tests only. |
| `all` | Both service and networking tests. |

The Netris backend lists supported deployment scenarios in
[`infra/netris/capabilities`](infra/netris/capabilities); the top-level Makefile
checks that list before deployment. `full` does not include every scenario: its
setup deliberately skips CaaS, and its test target excludes CaaS tests.

For example, these two commands run only the preparation and test stages after
the infrastructure and OSAC are already deployed:

```bash
make setup-suite INFRA=netris SUITE=full OSAC_PROFILE=full-ci
make run-tests INFRA=netris SUITE=full
```

`SUITE`, `TEST_SCOPE`, and `OSAC_PROFILE` answer different questions:

- `SUITE` chooses the deployment scenario, including infrastructure setup,
  Ansible behavior, and the default OSAC profile.
- `TEST_SCOPE` chooses service tests, Netris networking tests, or both for
  VMaaS and BMaaS. It does not change the deployment.
- `OSAC_PROFILE` chooses the OSAC Helm values preset and enabled services. The
  Netris backend supplies the defaults shown in the table; set
  `OSAC_PROFILE=...` only when you intentionally want a different preset.
- `OSAC_DEPLOY_MODE` chooses a fresh install (`fresh`, the default) or the
  snapshot-based OSAC refresh (`snapshot`).
- `TEST=name` adds a pytest `-k` filter to an existing-cluster test target.
  `MARKER=regression` adds a pytest `-m` filter to the BMaaS service test scope.

The direct `make test-*` targets do not take a `SUITE` argument: the target
name already selects the tests. For example, run `make test-full` against an
existing deployment; use `make run-tests INFRA=netris SUITE=full` when you
want the orchestrator to load `.env.infra` first.

The PR E2E comment currently offers these dedicated Netris checks. The check
title is a workflow display name; `SUITE` selects the deployment and
`TEST_SCOPE` narrows the tests.

| PR command | `SUITE` | `TEST_SCOPE` | `OSAC_PROFILE` | Coverage |
|---|---|---|---|---|
| `/test vmaas-netris` | `vmaas` | `networking` | `vmaas-ci` + `values/vmaas-ci/netris.yaml` | VMaaS Netris networking |
| `/test bmaas-netris` | `bmaas` | `networking` | `bmaas-ci` + `values/bmaas-ci/netris.yaml` | BMaaS Netris networking; the slow cross-tenant class is omitted |
| `/test stretched-netris` | `stretched` | — | `full-ci` + `values/full-ci/netris.yaml` | Shared-subnet VMaaS↔BMaaS connectivity |

There is no dedicated `/test full-netris` command today. To run the broad suite
from GitHub Actions, dispatch **E2E VMaaS Netris Full Install** and choose
`suite=full`; the run uses `full-ci` plus `values/full-ci/netris.yaml`. Locally,
run `SUITE=full` with the Make pipeline below.

The VMaaS EVPN flows (`vmaas`, `stretched`, and `full`) require converged OCP
4.22 with OVNKubernetes and configure CUDN EVPN
with the Netris fabric manager. External-IP dataplane coverage needs a
separately provisioned and acknowledged return path for `172.27.0.0/16`;
`full` filters those cases out.

### Backend Setup: Netris

The Netris backend requires the following files placed in `infra/netris/` before running:

| File | Description | How to obtain |
|------|-------------|---------------|
| `license.key` | Netris controller license | Obtain from Netris |
| `license.zip` | OSAC/AAP license (base64-encoded zip) | Obtain from Red Hat |
| `config` | INI file with lab name, AWS credentials, and Quay credentials | Create manually (see below) |

The `config` file format:

```ini
[default]
lab_name = <unique-name>
aws_access_key_id = <your-key>
aws_secret_access_key = <your-secret>
aws_region = us-east-1
netris_password = <your-netris-password>
quay_username = <your-quay-username>
quay_password = <your-quay-password>
```

- `lab_name` — unique identifier for your lab to avoid DNS collisions in Route 53. The backend uses it as the first DNS label in `ocp_base_domain`. Use a hostname-compatible label of at most 63 characters: ASCII letters, digits, and hyphens only, with a letter or digit at each end. Do not use underscores (for example, `qa-lab-1` is valid).
- AWS credentials — used for Route 53 DNS record management
- Quay credentials — the account must have pull access to `quay.io/osac-project/cluster-flavors`; `setup-infra` logs in automatically when it downloads the snapshot flavor image

The Netris backend's `dns_server` defaults to `8.8.8.8`. Some environments block that resolver. If it is unreachable from your lab and you only need a DNS override, export:

```bash
export EXTRA_VARS="dns_server=<reachable-dns-server>"
```

`OSAC_PROFILE` selects the OSAC Helm values preset; it is separate from
`SUITE`, which selects setup and tests. The profile defaults for each selector
are listed in the suite table above. Both `caas-ci` and `bmaas-ci` enable
Metal3, the OSAC component for bare-metal provisioning. Its installer check
requires a cluster-scoped Kubernetes `Provisioning` resource with
`watchAllNamespaces: true`. Netris defaults `bare_metal_services` to `false`,
so `prep-osac` skips creating or updating that resource unless the flag is
true. If the resource already exists with the required setting, the flag is
not needed just for the installer check.

The same flag enables Netris's BMC (Baseboard Management Controller) support: `deploy-infra` starts `sushy-tools`, which emulates a server's management interface, and `deploy-ocp` reserves the OCP VM's BMC address. Without the flag those conditional setup tasks are skipped, even though the commands can still succeed. Use the flag when you need these bare-metal services as well as the Metal3 prerequisite.

This flag is profile-dependent, not required for every OSAC deployment:

| OSAC profile | When `bare_metal_services=true` is needed |
|--------------|-------------------------------------------|
| `caas-ci` | The current profile enables Metal3; set it if the required `Provisioning` resource is absent. |
| `bmaas-ci` | The profile enables Metal3; set it if the required `Provisioning` resource is absent. |
| `vmaas-ci` | Not needed in the current VMaaS full-install workflow. |

For another profile, check whether its values enable `bmf.metal3.enabled`. For `caas-ci` or `bmaas-ci`, if you also need to override DNS, combine both settings in one export; a second `export EXTRA_VARS=...` would replace the first value:

```bash
export EXTRA_VARS="bare_metal_services=true dns_server=<reachable-dns-server>"
```

The deployment example below exports `EXTRA_VARS` once; the top-level Makefile forwards it to each backend stage. For a profile that does not need bare-metal services, use only the DNS override if required.

Additionally, an OCP pull secret must be present at `/root/pull-secret` on the host.

All secret files are gitignored.

## How It Works

`make e2e INFRA=netris SUITE=full` runs the complete Netris pipeline. The
backend defaults `OSAC_PROFILE` to `full-ci` for this selector. The top-level
Makefile then:

1. Checks that Netris supports the requested `SUITE`.
2. Installs prerequisites and caches images (`setup-infra`).
3. Deploys the Netris lab and restores OCP (`deploy-infra`, `deploy-ocp`). For
   `vmaas`, `stretched`, and `full`, this also
   configures the CUDN/EVPN underlay on converged OCP 4.22 with OVNKubernetes.
4. Installs OSAC using `OSAC_PROFILE` and writes `.env.infra`.
5. Maps each `SUITE` to its Netris setup target and root test target. For
   VMaaS and BMaaS, `TEST_SCOPE` selects service tests, Netris networking tests,
   or both.
   `setup-full` prepares BMaaS and VMaaS networking prerequisites, but skips
   CaaS. `test-full` runs serially and applies the exclusions in the suite
   table above.

Run the individual targets when the lab is already at the required stage. For
example, after OSAC is deployed, `make run-tests INFRA=netris SUITE=full` reads
`infra/netris/.env.infra` and runs `test-full` without provisioning again.

## Quick Start

### Run tests against an existing deployment

These targets run tests only; they do not provision Netris or install OSAC.
The OSAC mono-repo checkout defaults to `/opt/osac` and can be changed with
`OSAC_MONO_DIR`.

```bash
cd /opt/osac && uv sync
cd /path/to/osac-test-infra

export OSAC_NAMESPACE=osac-devel
export KUBECONFIG=~/.kube/config
export OSAC_VM_KUBECONFIG=~/.kube/config
make test-vmaas

# Netris-only networking tests against an already prepared lab
make test-vmaas TEST_SCOPE=networking
make test-bmaas TEST_SCOPE=networking
make test-stretched

# Run both the service and networking tests
make test-vmaas TEST_SCOPE=all

# Optional pytest filters
TEST=test_name make test-vmaas
MARKER=regression make test-bmaas
```

`TEST` becomes pytest's `-k` filter. `MARKER` becomes `-m` for the BMaaS
service test scope. For the environment variables required by a particular suite,
see the OSAC mono-repo's [E2E guide](https://github.com/osac-project/osac/blob/main/tests/e2e/AGENTS.md).

### Provision Netris and run an E2E suite

Run from this repository's root. `make e2e` performs setup, deployment, suite
preparation, and tests. `SUITE` and `OSAC_PROFILE` may be omitted when using
the Netris defaults in the table above.

```bash
# Broad serial run, including implemented VMaaS/BMaaS Netris networking and
# stretched connectivity. `full-ci` is the default profile for SUITE=full.
# bare_metal_services enables the BMaaS BMC prerequisites.
make e2e INFRA=netris SUITE=full OSAC_PROFILE=full-ci \
  OSAC_DEPLOY_MODE=fresh EXTRA_VARS="bare_metal_services=true"

# Provision the VMaaS deployment and run only its Netris networking tests
make e2e INFRA=netris SUITE=vmaas TEST_SCOPE=networking

# Provision the BMaaS deployment and run only its Netris networking tests
make e2e INFRA=netris SUITE=bmaas TEST_SCOPE=networking \
  EXTRA_VARS="bare_metal_services=true"

# Run only the shared-subnet VMaaS↔BMaaS test
make e2e INFRA=netris SUITE=stretched \
  EXTRA_VARS="bare_metal_services=true"
```

To override DNS as well, combine it with the other Ansible variables in the
same `EXTRA_VARS` value, for example
`EXTRA_VARS="bare_metal_services=true dns_server=<reachable-dns-server>"`.
Exporting `EXTRA_VARS` a second time replaces the first value.

### Iterate on OSAC

```bash
# Redeploy OSAC without reprovisioning the lab
make redeploy-osac INFRA=netris

# Then re-run tests
make run-tests INFRA=netris SUITE=full
```

### Tear down

```bash
make destroy-osac INFRA=netris       # OSAC only, keep the lab
make destroy-ocp INFRA=netris        # Reset OCP, keep the Netris lab
make destroy-infra INFRA=netris      # Everything
```

### Gather diagnostics

```bash
make gather-infra INFRA=netris
make gather-suite INFRA=netris SUITE=full
```

## Configuration

All configuration via environment variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `OSAC_NAMESPACE` | `osac-devel` | Namespace where OSAC is deployed |
| `KUBECONFIG` | `~/.kube/config` | Kubeconfig for the hub cluster |
| `OSAC_VM_KUBECONFIG` | **(required for vmaas)** | Kubeconfig for the VM cluster |
| `OSAC_PULL_SECRET_PATH` | **(required for caas)** | Path to OCP pull secret |
| `OSAC_FULFILLMENT_ADDRESS` | auto-derived | Fulfillment API address |
| `OSAC_VM_TEMPLATE` | `osac.templates.ocp_virt_vm` | VM template |
| `OSAC_CLUSTER_TEMPLATE` | `osac.templates.ocp_ci_small` | Cluster template |
| `OSAC_CLI_PATH` | `osac` | Path to the CLI binary |
| `TEST` | (none) | pytest `-k` filter |
| `INFRA` | `netris` | Infrastructure backend |
| `SUITE` | `caas` | Deployment scenario; also selects the matching Make test target |
| `TEST_SCOPE` | `service` | For VMaaS/BMaaS: `service`, `networking`, or `all` |
| `EXTRA_VARS` | (none) | Extra variables passed to the backend |

For E2E fixture and test guidance, see [the OSAC mono-repo E2E documentation](https://github.com/osac-project/osac/tree/main/tests/e2e).

## Adding a New Backend

Create `infra/<name>/` with:
- `Makefile` — Makefile implementing the contract targets (see `infra/contract.md`)
- `capabilities` — shell-sourceable file declaring supported deployment scenarios
- Optional `contracts/<suite>` files — shell-sourceable `REQUIRED_VARS` lists for suite environment validation

Test code belongs in the OSAC mono-repo. Add backend capabilities and contracts as needed; the root Makefile needs a test target when introducing a deployment scenario.
