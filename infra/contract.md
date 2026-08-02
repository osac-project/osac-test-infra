# Infrastructure Backend Contract

Each backend lives in `infra/<name>/` and must provide:

## Required Files

- `contract.mk` — Makefile implementing the contract targets
- `capabilities` — shell-sourceable file declaring `SUPPORTED_SUITES`

## Contract Targets

| Target | Purpose |
|---|---|
| `setup-infra` | Install prerequisites and dependencies |
| `deploy-infra` | Provision the lab and cluster |
| `deploy-osac` | Deploy OSAC, write `.env.infra` |
| `setup-<suite>` | Suite-specific infra prep (can be no-op) |
| `destroy-osac` | Tear down OSAC only |
| `destroy-infra` | Tear down everything |
| `gather-infra` | Collect infrastructure diagnostics |
| `gather-<suite>` | Collect suite-specific diagnostics |

## .env.infra

After `deploy-osac`, write `.env.infra` in the backend directory:

```
KUBECONFIG=<path>
OSAC_NAMESPACE=<namespace>
OSAC_VM_KUBECONFIG=<path>          # if supporting vmaas
OSAC_PULL_SECRET_PATH=<path>       # if supporting caas
```

The top-level Makefile sources this file before running tests.

## Variables

Backends receive `EXTRA_VARS` from the top-level Makefile.
