# Infrastructure Backend Contract

Each backend lives in `infra/<name>/` and must provide:

## Required Files

- `Makefile` — implements the contract targets below
- `capabilities` — shell-sourceable file declaring `SUPPORTED_SUITES`

## Contract Targets

| Target | Purpose |
|---|---|
| `setup-infra` | Install prerequisites and dependencies |
| `deploy-infra` | Provision the lab infrastructure |
| `deploy-ocp` | Deploy an OpenShift cluster |
| `deploy-osac` | Deploy OSAC on the cluster, write `.env.infra` |
| `setup-<suite>` | Suite-specific infra prep (can be no-op) |
| `prepare-suite` | Suite-specific setup before pytest; tests create their own tenant prerequisites |
| `deploy-suite` | Prepare the suite, then create persistent tenants, suite networking, and workloads |
| `destroy-ocp` | Tear down the OpenShift cluster |
| `destroy-osac` | Tear down OSAC only |
| `destroy-infra` | Tear down everything |
| `gather-infra` | Collect infrastructure diagnostics |
| `gather-<suite>` | Collect suite-specific diagnostics |

## .env.infra

After `deploy-osac`, write `.env.infra` in the backend directory with the environment needed by its supported suites:

```
KUBECONFIG=<path>
OSAC_NAMESPACE=<namespace>
OSAC_VM_KUBECONFIG=<path>          # required by the vmaas and vmaas_networking suite contracts
OSAC_PULL_SECRET_PATH=<path>       # commonly required by caas
```

`OSAC_VM_KUBECONFIG` is mandatory for VMaaS and full. For a single-cluster deployment it may point to the same kubeconfig as `KUBECONFIG`; set a separate path when the workload cluster has its own kubeconfig.

A backend may define a shell-sourceable contract at `infra/<backend>/contracts/<suite>`. Set `REQUIRED_VARS` there to the `.env.infra` variables that suite needs, for example:

```sh
REQUIRED_VARS="KUBECONFIG OSAC_NAMESPACE OSAC_VM_KUBECONFIG"
```

Before `run-tests`, the top-level Makefile sources `.env.infra` and checks each variable listed by that backend contract. The Netris `bmaas_networking`, `full`, and stretched-networking contracts also require the BMaaS SSH host map, instance type, and guest-visible interface name published by setup. The contract is optional for suites that do not need this validation. Keeping contracts beside a backend lets each backend describe its own connection requirements without placing infrastructure contracts in the moved test suites.

## Variables

Backends receive `EXTRA_VARS` and `OSAC_DEPLOY_MODE` from the top-level Makefile.
