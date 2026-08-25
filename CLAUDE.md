@AGENTS.md

# CLAUDE.md

> [!IMPORTANT]
> **The e2e test suite has moved (OSAC-3593).** Test suites now live in the
> [osac mono-repo](https://github.com/osac-project/osac). **Do not add, modify,
> or delete tests in this repository** — make those changes in the mono-repo
> instead.

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This repository contains pytest-based E2E test infrastructure for OSAC. Tests exercise the fulfillment API (gRPC/REST), Kubernetes CRs, and multi-cluster provisioning flows. Focus areas: VMaaS (ComputeInstance), CaaS (ClusterOrder), storage, and catalog APIs.

## Architecture

### Directory Structure

- `tests/` - Pytest test suites
  - `vmaas/` - ComputeInstance lifecycle tests
  - `caas/` - ClusterOrder lifecycle tests
  - `storage/` - Tenant storage tests
  - `catalog/` - CatalogItem tests
  - `bmaas/` - BareMetalInstance lifecycle tests
  - `bmaas_networking/` - BMaaS networking E2E
  - `core/` - Client wrappers (grpc_client, k8s_client, osac_cli) and helpers
  - `conftest.py` - Session fixtures (cli, grpc, k8s_hub_client, jwt tokens)

### Core Components

**Pytest Test Framework**
- Session-scoped fixtures for clients (gRPC, K8s, CLI) and authentication tokens
- Client abstractions: GRPCClient (grpcurl wrapper), K8sClient (kubectl wrapper), OsacCLI
- Polling utilities: `poll_until`, `wait_for_*` helpers for state transitions
- Multi-cluster support: hub cluster (CRs) + workload cluster (VMs)

**Communication Methods**
- `osac` CLI for resource creation operations (automatic token refresh)
- `grpcurl` for direct gRPC API calls (public/private fulfillment API)
- `kubectl` for Kubernetes CR inspection and status verification
- Bearer token authentication via ServiceAccount tokens or Keycloak JWT

## Development Commands

### Code Quality
```bash
# Run linters
make lint    # ruff check + ruff format --check

# Format code
make format  # ruff format

# Run pre-commit hooks
pre-commit run --all-files
```

### Running Tests

```bash
# Run all tests (parallel, 4 workers)
make test

# Run specific test suites
make test-vmaas
make test-caas
make test-storage
make test-bmaas
make test-bmaas-networking

# Run single test by name
TEST=test_compute_instance_lifecycle make test-vmaas

# Sequential execution (for debugging; -n 0 overrides the default 4-worker parallelism)
uv run pytest tests/vmaas/ -v --tb=short -n 0
```

## Configuration

All configuration via environment variables. Works identically in local dev and CI.

| Variable | Default | Description |
|----------|---------|-------------|
| `OSAC_NAMESPACE` | `osac-devel` | Namespace where OSAC is deployed |
| `KUBECONFIG` | `~/.kube/config` | Kubeconfig for the hub (management cluster) |
| `OSAC_VM_KUBECONFIG` | *(required for vmaas tests)* | Kubeconfig for the VM cluster (where VirtualMachines run). In single-cluster setups, set this to the same value as `KUBECONFIG`. Only used by vmaas test suite. |
| `OSAC_FULFILLMENT_ADDRESS` | auto-derived | Fulfillment API address (`host:port`) |
| `OSAC_VM_TEMPLATE` | `ocp-virt-vm` | ComputeInstance template to use |
| `OSAC_SERVICE_ACCOUNT` | `admin` | ServiceAccount for token generation |
| `OSAC_CLI_PATH` | `osac` | Path to the CLI binary |
| `TEST` | (none) | pytest `-k` filter — run only tests matching this name substring |

### Two-Kubeconfig Design

Tests access two clusters:
- **Hub** (`KUBECONFIG`) — where ComputeInstance CRs, jobs, and the fulfillment service live
- **VM cluster** (`OSAC_VM_KUBECONFIG`) — where VirtualMachine and VirtualMachineInstance resources live

In single-cluster dev setups (VMs run on the hub): set `OSAC_VM_KUBECONFIG` to the same value as `KUBECONFIG`.

In two-cluster setups: set `OSAC_VM_KUBECONFIG` to the virt cluster kubeconfig. The hub kubeconfig manages CRs, the VM kubeconfig verifies VM state.

## Test Execution Pattern

Standard pytest test flow:

1. **Create**: Use CLI (`cli.create_compute_instance(...)`) or gRPC (`grpc.create_compute_instance(...)`) to create resource
2. **Wait for CR**: `wait_for_cr(k8s=k8s_hub_client, uuid=resource_id)` (from `tests.core.helpers`)
3. **Wait for provisioning**: `wait_for_provision(k8s=k8s_hub_client, name=name)`, then `wait_for_running(k8s=k8s_hub_client, name=name)`
4. **Verify**: Check CR status via K8s (e.g. `k8s_hub_client.get_compute_instance_phase(name=name)`) and gRPC API (e.g. `grpc.get_compute_instance(ci_id=id)`)
5. **Delete**: Use CLI or gRPC to delete resource
6. **Verify removal**: `wait_for_deletion(k8s=k8s_hub_client, name=name)`

All state transitions use polling utilities (`poll_until` in `tests/core/runner.py`, and the `wait_for_*` helpers in `tests/core/helpers.py` built on it) to handle async provisioning.

## gRPC API Operations

**Common operations** (via `GRPCClient` fixture — resource-specific methods, not a generic verb-based call):
- `grpc.list_compute_instance_ids()` — List resource IDs
- `grpc.get_compute_instance(ci_id=id)` — Get specific resource
- `grpc.create_compute_instance(catalog_item=..., subnet_ids=[...])` — Create resource
- `grpc.delete_compute_instance(ci_id=id)` — Delete resource
- Equivalent method sets exist for VirtualNetworks, Subnets, SecurityGroups, ClusterOrders, ExternalIPs/Pools, catalog items, and InstanceTypes
- `grpc.call(service="osac.public.v1.<Resource>/<Verb>", data={...})` — Lower-level escape hatch used internally by the methods above

All gRPC calls use insecure connections (`-insecure` flag) and require Bearer token authentication; the token is bound to the `GRPCClient` instance at construction (see the `grpc`/`jwt_grpc_tenant*` fixtures), not passed per call.

## Cross-Cutting Concern Testing

OSAC features like metering, storage, networking, and catalog span multiple domains (VMaaS, CaaS, BMaaS). Instead of duplicating verification logic, we use a **composable fixture pattern**:

### Pattern

1. **Verifier class** in `tests/core/<concern>.py` — encapsulates the verification logic (e.g., HTTP client for metering, PV checker for storage)
2. **Fixture + marker** in `tests/conftest.py` — lifecycle management (start/yield/verify/stop) + `@pytest.mark.<concern>` for filtering + fail-fast when infrastructure is missing
3. **Tests inject the fixture** — any test in any domain can add the concern by requesting the fixture and registering expectations

### Example: Metering

```python
@pytest.mark.metering
def test_compute_instance_lifecycle(grpc, cli, k8s_hub_client, default_subnet, metering):
    uuid = cli.create_compute_instance(...)
    metering.expect("osac.resource.created.v1", resource_id=uuid)

    # ... normal lifecycle test ...

    cli.delete_compute_instance(uuid=uuid)
    metering.expect("osac.resource.deleted.v1", resource_id=uuid)
    # metering.verify() runs automatically on fixture teardown
```

### Filtering and skipping

```bash
pytest -m metering tests/           # only metering-annotated tests
pytest -m "not metering" tests/     # skip all metering tests
pytest -m "metering and storage"    # tests with both concerns
```

Tests **fail fast** when concern infrastructure is missing (e.g., no `METERING_ADAPTER_URL` → pytest fails at collection). Use `-m "not metering"` to exclude when the test adapter is unavailable.

### Adding a new concern

1. Create `tests/core/<concern>.py` with a collector/verifier class
2. Add fixture + marker + skip logic in `tests/conftest.py`
3. Inject the fixture into any test that should verify the concern

### Directory structure

```
tests/
  conftest.py           # Concern fixtures, markers, conditional skips (alongside session fixtures)
  core/
    metering.py         # MeteringCollector (HTTP client + expect/verify)
  vmaas/                # Tests inject metering fixture here
  caas/                 # Same pattern when CaaS metering arrives
```

## Error Handling

- Pytest fixtures handle cleanup via teardown hooks
- `assert_grpc_rejected(exc_info, code)` validates expected gRPC failures, used with `pytest.raises(subprocess.CalledProcessError)`
- `poll_until` includes configurable `retries`/`delay` (keyword-only, requires a `description`) for transient errors
- Tests clean up resources explicitly via delete steps or fixture teardown
- Failed tests leave resources in place for debugging (manual cleanup required)
