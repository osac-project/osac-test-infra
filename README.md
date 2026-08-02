# OSAC Test Infrastructure

Unified repo for OSAC end-to-end testing. Provisions infrastructure via pluggable backends and runs pytest test suites against it.

**AI coding agents**: see [AGENTS.md](AGENTS.md) for test framework architecture, fixtures, and conventions. Claude Code additionally reads [CLAUDE.md](CLAUDE.md) for pytest configuration and environment variables.

## Architecture

The repo has two layers:

- **`infra/`** — infrastructure backends that provision a cluster and deploy OSAC. Each backend lives in its own directory and can use any technology (Ansible, shell, Go, etc.).
- **`tests/`** — pytest E2E test suites that validate OSAC functionality. Tests are infrastructure-agnostic — they consume environment variables and don't know which backend provisioned the cluster.

A **contract** connects the two layers. Each backend implements a standard set of Makefile targets (`setup-infra`, `deploy-infra`, `deploy-osac`, etc.) and produces a `.env.infra` file with the configuration tests need. The top-level Makefile orchestrates the full flow.

```
infra/<backend>/                    tests/<suite>/
┌──────────────────────┐            ┌──────────────────────┐
│ setup-infra          │            │                      │
│ deploy-infra         │            │ pytest test_*.py     │
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

| Suite | Description |
|-------|-------------|
| **vmaas** | Compute instance lifecycle, restart, networking, public IPs, security groups, console, JWT auth |
| **caas** | Cluster lifecycle, credentials, template immutability, API fields |
| **catalog** | Catalog item lifecycle |
| **storage** | Tenant storage lifecycle |

### Compatibility Matrix

Not every backend supports every test suite. Each backend declares its supported suites in a `capabilities` file. The system validates this before any deployment starts.

| Backend | vmaas | caas | catalog | storage |
|---------|:-----:|:----:|:-------:|:-------:|
| netris  | no    | yes  | no      | no      |

Running an unsupported combination (e.g., `make e2e INFRA=netris SUITE=storage`) fails immediately with a clear error message — no time wasted on provisioning.

### Backend Setup: Netris

The Netris backend requires the following files placed in `infra/netris/` before running:

| File | Description | How to obtain |
|------|-------------|---------------|
| `license.key` | Netris controller license | Obtain from Netris |
| `license.zip` | OSAC/AAP license (base64-encoded zip) | Obtain from Red Hat |
| `config` | INI file with lab name and AWS credentials | Create manually (see below) |

The `config` file format:

```ini
[default]
lab_name = <unique-name>
aws_access_key_id = <your-key>
aws_secret_access_key = <your-secret>
aws_region = us-east-1
```

- `lab_name` — unique identifier for your lab to avoid DNS collisions in Route 53
- AWS credentials — used for Route 53 DNS record management

Additionally, an OCP pull secret must be present at `/root/pull-secret` on the host.

All secret files are gitignored.

## How It Works

When you run `make e2e INFRA=netris SUITE=caas`, the following happens:

1. **Validate** — checks that the `netris` backend exists and supports the `caas` suite
2. **`setup-infra`** — installs prerequisites, caches images (`ansible-playbook playbooks/setup.yml`)
3. **`deploy-infra`** — deploys the Netris lab, OCP cluster from snapshot (`make deploy-fast`)
4. **`deploy-osac`** — refreshes OSAC on the restored cluster, writes `.env.infra` with cluster access credentials
5. **`setup-suite`** — runs CaaS-specific infrastructure setup (creates InfraEnv, boots discovery VMs, registers agents)
6. **`run-tests`** — validates `.env.infra` has the required variables, sources it, runs `pytest tests/caas/`

Each step can be run independently — you don't have to run the full pipeline every time.

## Quick Start

### Run tests against an existing cluster

```bash
uv sync

OSAC_NAMESPACE=osac-devel OSAC_VM_KUBECONFIG=~/.kube/config make test-vmaas

TEST=test_cluster_order_lifecycle make test-caas
```

### Full E2E with infrastructure provisioning

```bash
# Full pipeline: provision + deploy + test
make e2e INFRA=netris SUITE=caas

# Or step by step
make setup-infra INFRA=netris
make deploy-infra INFRA=netris
make deploy-osac INFRA=netris
make setup-suite INFRA=netris SUITE=caas
make run-tests INFRA=netris SUITE=caas
```

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
- `contract.mk` — Makefile implementing the contract targets (see `infra/contract.md`)
- `capabilities` — shell-sourceable file declaring `SUPPORTED_SUITES="suite1 suite2"`

No changes to test code or the top-level Makefile are needed.
