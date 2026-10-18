# OSAC Test Infrastructure

> [!IMPORTANT]
> **The e2e test suite has moved (OSAC-3593).** Test suites now live in the
> [osac mono-repo](https://github.com/osac-project/osac). **Do not add, modify,
> or delete tests in this repository** — make those changes in the mono-repo
> instead. The `tests/` directory here is retained only for the infrastructure
> backend contract and a placeholder; new tests added here will not be
> maintained.

Unified repository for OSAC end-to-end infrastructure and test orchestration. Pluggable backends provision clusters; active pytest suites run from the OSAC mono-repo.

**AI coding agents**: see [AGENTS.md](AGENTS.md) for test framework architecture, fixtures, and conventions. Claude Code additionally reads [CLAUDE.md](CLAUDE.md) for pytest configuration and environment variables.

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

### Test Suites

The table describes active suites in the OSAC mono-repo. Their test code is not maintained in this repository.

| Suite | Description |
|-------|-------------|
| **vmaas** | Compute instance lifecycle, restart, networking, public IPs, security groups, console, JWT auth |
| **caas** | Cluster lifecycle, credentials, template immutability, API fields |
| **catalog** | Catalog item lifecycle |
| **storage** | Tenant storage lifecycle |
| **bmaas** | BareMetalInstance lifecycle |
| **bmaas_networking** | BMaaS networking E2E |

### Compatibility Matrix

Not every backend supports every test suite. Each backend declares its supported suites in a `capabilities` file. The system validates this before any deployment starts.

| Backend | vmaas | caas | catalog | storage | bmaas | bmaas_networking |
|---------|:-----:|:----:|:-------:|:-------:|:-----:|:----------------:|
| netris  | no    | yes  | no      | no      | yes   | yes              |

Running an unsupported combination fails immediately with a clear capability error. Netris currently does not advertise `vmaas` because the EVPN peer and `172.27.0.0/16` return path have not yet been verified for the VMaaS workload network. The VMaaS preflight requires OCP 4.22 with OVNKubernetes; it does not require the cluster platform type to be `BareMetal`.

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

`OSAC_PROFILE` selects the OSAC Helm values preset; it is separate from `SUITE`, which selects test setup and tests. Netris defaults `OSAC_PROFILE` to `caas-ci`. Both `caas-ci` and `bmaas-ci` enable Metal3, the OSAC component for bare-metal provisioning. Its installer check requires a cluster-scoped Kubernetes `Provisioning` resource with `watchAllNamespaces: true`. Netris defaults `bare_metal_services` to `false`, so `prep-osac` skips creating or updating that resource unless the flag is true. If the resource already exists with the required setting, the flag is not needed just for the installer check.

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

When you run `make e2e INFRA=netris SUITE=caas` with `EXTRA_VARS` exported as described above, the following happens:

1. **Validate** — checks that the `netris` backend exists and supports the `caas` suite
2. **`setup-infra`** — installs prerequisites, caches images (`ansible-playbook playbooks/setup-infra.yml`)
3. **`deploy-infra`** — deploys the Netris lab (`ansible-playbook playbooks/deploy-infra.yml`)
4. **`deploy-ocp`** — restores the OCP snapshot into the lab and waits for the cluster to become healthy. With `SUITE=vmaas`, it also runs the `configure-cudn-evpn` role, which derives the Netris peer settings and configures the OCP CUDN/EVPN prerequisites. This path requires OCP 4.22 with OVNKubernetes, but does not require `platformStatus.type: BareMetal`.
5. **`deploy-osac`** — installs or refreshes OSAC on the cluster and writes `.env.infra` with cluster access credentials.
6. **`setup-suite`** — runs suite-specific infrastructure setup; for CaaS this creates the InfraEnv, boots discovery VMs, and registers agents. The Netris backend's `setup-vmaas` target is a read-only prerequisite check.
7. **`run-tests`** — checks the backend's suite contract when one is defined, sources `.env.infra`, then runs the selected suite target. The VMaaS target runs `uv run pytest tests/e2e/vmaas/` from the OSAC mono-repo.

Each step can be run independently — you don't have to run the full pipeline every time.

## Quick Start

### Run tests against an existing cluster

```bash
(cd /opt/osac && uv sync)

OSAC_NAMESPACE=osac-devel KUBECONFIG=~/.kube/config \
  OSAC_VM_KUBECONFIG=~/.kube/config make test-vmaas

TEST=test_cluster_order_lifecycle make test-caas
```

### Full E2E with infrastructure provisioning

```bash
# Full pipeline: provision + deploy + test
# Netris defaults to caas-ci; caas-ci and bmaas-ci enable Metal3.
export EXTRA_VARS="bare_metal_services=true"
# If 8.8.8.8 is blocked too, replace the export with the combined value shown above.

make e2e INFRA=netris SUITE=caas

# Intended VMaaS invocation (currently rejected by Netris capability validation)
make e2e INFRA=netris SUITE=vmaas OSAC_DEPLOY_MODE=fresh

# Explicitly select the VMaaS profile; the backend chooses it by default for SUITE=vmaas
make e2e INFRA=netris SUITE=vmaas OSAC_DEPLOY_MODE=fresh \
  OSAC_PROFILE=cudn-evpn-netris-test

# Or step by step
make setup-infra INFRA=netris
make deploy-infra INFRA=netris
make deploy-ocp INFRA=netris SUITE=caas
make deploy-osac INFRA=netris
make setup-suite INFRA=netris SUITE=caas
make run-tests INFRA=netris SUITE=caas
```

For a manual VMaaS network setup, `deploy-ocp` must receive `SUITE=vmaas` so it runs the CUDN/EVPN role. After OSAC is deployed, run `make -C infra/netris setup-vmaas` to check the cluster prerequisites. The top-level `e2e` target still rejects VMaaS until the backend advertises that suite.

### Iterate on OSAC

```bash
# Redeploy OSAC without reprovisioning the lab
make redeploy-osac INFRA=netris

# Then re-run tests
make run-tests INFRA=netris SUITE=caas
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
make gather-suite INFRA=netris SUITE=caas
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
| `SUITE` | `caas` | Test suite |
| `EXTRA_VARS` | (none) | Extra variables passed to the backend |

See [AGENTS.md](AGENTS.md) for the fuller env var reference used by the pytest fixtures themselves (JWT auth, per-suite test patterns, debugging).

## Adding a New Backend

Create `infra/<name>/` with:
- `Makefile` — Makefile implementing the contract targets (see `infra/contract.md`)
- `capabilities` — shell-sourceable file declaring `SUPPORTED_SUITES="suite1 suite2"`
- Optional `contracts/<suite>` files — shell-sourceable `REQUIRED_VARS` lists for suite environment validation

Test code belongs in the OSAC mono-repo. Add backend capability and suite contract files as needed; the root Makefile needs a runner target when introducing a suite.
