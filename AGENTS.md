# osac-test-infra

End-to-end test infrastructure for OSAC. pytest-based test suite for testing ComputeInstance lifecycle, cluster provisioning, networking, and PublicIP management through the OSAC fulfillment service.

## Critical Rules

- **Type annotations required** on all variables in pytest tests
- **Keyword-only arguments** (`*`) in all test helper methods
- **Never hardcode sleep** — use `poll_until` for waiting
- **Clean up resources in tests** — delete in reverse creation order, verify deletion from both K8s and API
- **Run `ruff check` and `ruff format`** before committing

## Dev Environment

```bash
# Setup
uv sync

# Run all pytest tests
make test

# Run VmaaS tests only
make test-vmaas

# Run CaaS tests only
make test-caas

# Run a specific test
make test TEST="test_create_compute_instance"

# Lint and format
make lint
make format

# Pre-commit hooks
pre-commit run --all-files
```

## Repository Structure

```text
osac-test-infra/
├── tests/                     # pytest E2E test suite
│   ├── conftest.py            # Shared fixtures (grpc, k8s clients, cli, namespace)
│   ├── core/                  # Test infrastructure helpers
│   │   ├── runner.py          # Test runner utilities
│   │   ├── k8s_client.py      # Kubernetes client wrapper
│   │   ├── grpc_client.py     # gRPC client wrapper
│   │   ├── osac_cli.py        # osac CLI wrapper
│   │   ├── keycloak.py        # Keycloak integration
│   │   └── helpers.py         # Shared test helpers
│   ├── vmaas/                 # VM-as-a-Service tests (compute, networking, security)
│   └── caas/                  # Cluster-as-a-Service tests
├── Makefile                   # Test and lint targets
├── pyproject.toml             # Python 3.11+, pytest, ruff config
└── .pre-commit-config.yaml    # yamllint, ansible-lint, pre-commit hooks
```

## Test Configuration

Tests are configured via environment variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `OSAC_NAMESPACE` | Yes | Target namespace for tests |
| `KUBECONFIG` | Yes | Hub cluster kubeconfig |
| `OSAC_VM_KUBECONFIG` | Yes | VM/workload cluster kubeconfig |
| `OSAC_FULFILLMENT_ADDRESS` | No | Fulfillment service address |
| `OSAC_VM_TEMPLATE` | No | VM template to use |
| `OSAC_SERVICE_ACCOUNT` | No | Service account for token creation |
| `OSAC_CLI_PATH` | No | Path to osac CLI binary |

Two-kubeconfig design: hub cluster (API, operators) and VM cluster (workloads) are separate.

## Test Conventions

- Use `uuid4().hex[:8]` for random resource name suffixes
- gRPC API packages: `osac.public.v1` and `osac.private.v1`
- K8s label convention: `osac.openshift.io/<resource>-uuid`
- CRD shortnames: `computeinstance`, `virtualnetwork`, `subnet`, `securitygroup`, `clusterorder`, `publicip`, `publicippool`, `tenant`

## Code Style

- **Ruff** for linting and formatting (line-length 120, strict type annotations)
- **Ruff lint rules**: E, F, W, I, UP, ANN, B, SIM, RUF
- **Pre-commit hooks**: trailing-whitespace, yamllint (strict), ansible-lint

## CI

GitHub Actions (`.github/workflows/`):
- **pre-commit.yaml** — runs pre-commit hooks on PRs

## gRPC API Operations

| Service | RPC | Purpose |
|---------|-----|---------|
| `private.v1.Hubs/Get` | Get hub details | Hub verification |
| `private.v1.ComputeInstances/List` | List compute instances | Test discovery |
| `private.v1.ComputeInstances/Get` | Get compute instance | Status verification |
| `private.v1.ComputeInstances/Delete` | Delete compute instance | Cleanup |

All gRPC calls use insecure connections with Bearer token authentication.
