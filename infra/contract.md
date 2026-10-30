# Infrastructure Backend Contract

Each backend lives in `infra/<name>/` and must provide:

## Required Files

- `Makefile` — implements the contract targets below
- `capabilities` — shell-sourceable file declaring `SUPPORTED_DEPLOY_SUITES` and,
  optionally, `DEFAULT_DEPLOY_SUITE`

When set, `DEFAULT_DEPLOY_SUITE` must be one of `SUPPORTED_DEPLOY_SUITES`. The root Makefile
uses it when `DEPLOY_SUITE` is omitted; backends without a default require callers to
select a deployment scenario explicitly.

## Contract Targets

| Target | Purpose |
|---|---|
| `setup-infra` | Install prerequisites and dependencies |
| `deploy-infra` | Provision the lab infrastructure |
| `deploy-ocp` | Deploy an OpenShift cluster |
| `deploy-osac` | Deploy OSAC on the cluster, write `.env.infra` |
| `setup-<deploy-suite>` | Prepare the selected deployment scenario (can be no-op) |
| `prepare-suite` | Suite-specific setup before pytest; tests create their own tenant prerequisites |
| `deploy-suite` | Prepare the suite, then create persistent tenants, suite networking, and workloads |
| `destroy-ocp` | Tear down the OpenShift cluster |
| `destroy-osac` | Tear down OSAC only |
| `destroy-suite` | Tear down resources owned by the selected deployment scenario; a backend may reject aggregate scenarios that would destroy shared infrastructure |
| `destroy-setup` | Remove host-side setup installed by `setup-infra`, or no-op if none is needed |
| `destroy-infra` | Tear down everything |
| `gather-infra` | Collect infrastructure diagnostics |
| `gather-<deploy-suite>` | Collect diagnostics for the selected deployment scenario |

## .env.infra

After `deploy-osac`, write `.env.infra` in the backend directory with the environment needed by its supported suites:

```
KUBECONFIG=<path>
OSAC_NAMESPACE=<namespace>
OSAC_VM_KUBECONFIG=<path>          # when selected tests need a separate workload cluster
OSAC_PULL_SECRET_PATH=<path>       # when deployment or tests need the pull secret
```

The backend writes base connection variables to `.env.infra` during
`deploy-osac`. `setup-<suite>` may add values needed by the selected tests;
the root pipeline runs it before validating test contracts. The variables
depend on the backend and selected tests. For a single-cluster deployment,
`OSAC_VM_KUBECONFIG` may point to the same file as `KUBECONFIG`; set a separate
path when tests use another cluster.

A backend may define a shell-sourceable contract at
`infra/<backend>/contracts/<test-selection>`. Set `REQUIRED_VARS` there to the
`.env.infra` variables required by that test selection, for example:

```sh
REQUIRED_VARS="KUBECONFIG OSAC_NAMESPACE OSAC_VM_KUBECONFIG"
```

Before `run-tests`, the top-level Makefile sources `.env.infra` and checks each
variable listed by `contracts/<test-selection>`. The Netris `bmaas`, `full`, and
`stretched` contracts also require the BMaaS SSH host map, instance type, and
guest-visible interface name published by setup. A backend owns these
contracts because required connection details depend on its deployment. Keeping
them beside the backend avoids coupling the infrastructure interface to test
implementation details in the OSAC mono-repo.

## Variables

The top-level Makefile forwards caller-supplied `EXTRA_VARS`, kubeconfig
overrides, and `OSAC_PULL_SECRET_PATH` when set. `EXTRA_VARS` is an opaque,
backend-specific payload; it does not define test-process environment by
itself. Each backend documents how its Make inputs map to deployment tools and
which variables its targets consume. Backends publish test runtime values
through `.env.infra`. When an input is unset, the backend uses its own defaults.

`TEST_SUITE` stays in the root Makefile: it selects the contract file above and the
`test-<selection>` target, and defaults to `DEPLOY_SUITE`.
