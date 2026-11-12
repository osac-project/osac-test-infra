# OSAC Test Infrastructure

> [!IMPORTANT]
> **The E2E suites moved (OSAC-3593).** Test suites now live in the
> [osac mono-repo](https://github.com/osac-project/osac). Do not add, modify,
> or delete tests in this repository; make test changes in the mono-repo.

This repository provides infrastructure backends and orchestration for running
OSAC E2E suites. A backend provisions a cluster and deploys OSAC. The suites
consume deployment configuration and do not depend on which backend created it.

**AI coding agents:** See [AGENTS.md](AGENTS.md) for repository constraints and
[the OSAC E2E guide](https://github.com/osac-project/osac/blob/main/tests/e2e/AGENTS.md)
for test conventions.

## CaaS Failure Diagnostics

The CaaS full-install workflow runs MCE diagnostics only on failure, before
normal log collection and teardown (not on success or cancellation). It uses
the runner's management kubeconfig, not guest credentials, and does not require
a HostedCluster to exist. Collection failures are non-fatal and preserve the
original CI failure. Other workflows are unchanged.

The helper reads the `multicluster-engine` Subscription's `status.installedCSV`
in namespace `multicluster-engine`, then uses that CSV's
`operators.openshift.io/must-gather-image` annotation unchanged. There is no
hardcoded version or fallback image. Missing kubeconfig, tools, Subscription,
CSV, or annotation produces a visible skip. The annotation is present in the
[MCE 2.17 bundle](https://github.com/stolostron/backplane-operator/blob/backplane-2.17/bundle/manifests/multicluster-engine.clusterserviceversion.yaml)
and the current bundle; source-tree development image references are not defaults.

Discovery is capped at two 15-second requests, MCE gather at 300 seconds,
HyperShift namespace inspection at 60 seconds, and management-side HCP/webhook
resource inspection at 30 seconds, with bounded kill grace periods and an
eight-minute step cap. The existing normal collection's 900-second timeout is
unchanged. No additional OpenShift must-gather or guest dump runs. Default MCE
coverage varies by release; supplemental inspection captures available operator resources/logs, HostedClusters,
NodePools and webhook configuration, not a guaranteed complete HCP or guest dump.

Output, partial results, logs, selected image and exit status are staged under
`mgmt-cluster/mce-must-gather/` in the existing `osac-logs-caas-full-install-*`
artifact. Raw collector logs are not printed to Actions logs. All retained files
pass existing recursive redaction, a mandatory private MCE pattern scan, and the
known-secret release gate before upload. Archives (including unexpected
`hypershift-dump.tar.gz`), binary/oversized files, links and special files are
withheld rather than allowed to bypass member scanning. The MCE scan limits files
to 32 MiB and the payload to 256 MiB; it has a 180-second cap. Missing/failed scans,
invalid/missing reports, or remaining findings withhold the MCE payload, even if
the ordinary artifact pattern scan is disabled. A safe withholding notice remains
where possible. The cluster secret inventory is namespace-scoped, so this is
defense in depth, not a claim that collector logs cannot contain credentials.

Related to [#802](https://github.com/osac-project/osac-test-infra/issues/802): this
collects diagnostic evidence and does not fix the webhook TLS failure.

## Architecture

The repository connects two layers:

- **Infrastructure backends** in `infra/<backend>/` provision a cluster,
  deploy OSAC, and provide the environment needed by selected tests.
- **OSAC E2E suites** in the OSAC mono-repo exercise the deployed services.
  This repository's `tests/` directory is legacy scaffolding only.

Backends connected to the root Makefile implement the interface in
[infra/contract.md](infra/contract.md). The root Makefile selects one of those
backends and coordinates its contract targets. Backends declare supported
deployment scenarios in a `capabilities` file and can provide
`contracts/<test-selection>` files listing environment variables required by
particular test selections. Some backend flows can also run through a dedicated
workflow before they implement the root contract.

### Backend inventory

| Backend | Implementation and root integration | Entry point |
|---|---|---|
| Netris | Implemented and wired to the root orchestration contract | [Netris backend guide](infra/netris/README.md) |
| Agentless-Net | Implemented CaaS flow; its dedicated workflow calls backend targets directly, and it is not yet wired to root `make e2e` | [Backend Makefile](infra/agentless-net/Makefile), [CaaS workflow](.github/workflows/e2e-caas-agentless-net-full-install.yml) |

## Deployment and test selection

Run the complete pipeline from this repository root:

This command applies to backends marked as wired to the root contract in the
inventory. Standalone backend workflows document their own invocation.

```bash
make e2e INFRA=<backend> [DEPLOY_SUITE=<deployment-scenario>] [TEST_SUITE=<test-selection>]
```

The selectors serve different purposes:

- **INFRA** chooses the backend directory, such as `infra/<backend>`.
- **DEPLOY_SUITE** chooses a deployment scenario supported by that backend. The backend
  uses it for deployment and scenario-specific preparation.
- **TEST_SUITE** chooses the OSAC test target independently. It defaults to
  **DEPLOY_SUITE**, but can differ when the deployment and test selections are not the
  same.

The pipeline validates the deployment scenario against the backend's
`SUPPORTED_DEPLOY_SUITES`, then runs:

1. Backend setup and infrastructure deployment.
2. Cluster and OSAC deployment. The backend writes its connection details to
   `.env.infra`.
3. Backend preparation for the selected deployment scenario.
4. Validation of `.env.infra` against the backend's optional
   `contracts/<test-selection>`.
5. The root Makefile target `test-<test-selection>`.

`SUITE` and `OSAC_DEPLOY_MODE` stop `make` with an error; see the
[old names](infra/netris/targets.md#old-names).

Use individual orchestration targets when the deployment already exists:
`setup-suite` prepares the selected deployment scenario, and `run-tests`
validates and runs the selected test target.

| Root target | Backend operation |
|---|---|
| `setup-infra` | Install backend prerequisites |
| `deploy-infra` | Provision backend infrastructure |
| `deploy-ocp` | Deploy the cluster |
| `deploy-osac` | Deploy OSAC and write `.env.infra` |
| `setup-suite` | Run `setup-<DEPLOY_SUITE>` |
| `prepare-suite` | Run the backend's preparation for `DEPLOY_SUITE`; `make e2e` uses it |
| `deploy-suite` | Create persistent workloads on an installed lab; unrelated to the `DEPLOY_SUITE` variable |
| `run-tests` | Validate `contracts/<TEST_SUITE>` and run `test-<TEST_SUITE>` |
| `destroy-suite` | Ask the backend to safely tear down the selected deployment scenario |
| `destroy-setup` | Remove host-side setup installed by `setup-infra`, or no-op if none is needed |
| `destroy-ocp` | Tear down the cluster |
| `destroy-osac` | Tear down OSAC |
| `destroy-infra` | Tear down backend infrastructure |
| `gather-infra` | Collect backend and cluster diagnostics |
| `gather-suite` | Run `gather-<DEPLOY_SUITE>` |

Backend-specific deployment choices, profiles, host preparation, credentials, and
configuration belong in that backend's guide. The root interface does not assign
meaning to backend-specific `EXTRA_VARS` values.

## OSAC E2E targets

The test targets run pytest from the OSAC mono-repo checkout. Set
`OSAC_MONO_DIR` to that checkout; it defaults to `/opt/osac`.

| Target | OSAC test path | Selection |
|---|---|---|
| `test-caas` | `tests/e2e/caas/` | CaaS E2E tests |
| `test-vmaas` | `tests/e2e/networking/vmaas/` | VMaaS networking tests; one ordered module, then the billing files, never filtered with `TEST` |
| `test-bmaas` | `tests/e2e/networking/bmaas/` | BMaaS networking tests |
| `test-stretched` | `tests/e2e/networking/stretched/` | Stretched networking tests |
| `test-full` | `tests/e2e/` | Serial run of the full profile; ignores `networking/vmaas` and `networking/bmaas` and filters the external-IP cases |
| `test-maas` | Dedicated OSAC path, when it exists | MaaS tests; fails when the path is absent |
| `test-caas_networking`, `test-maas_networking` | Dedicated OSAC paths, when they exist | CaaS and MaaS networking tests; fail when the path is absent |
| `test-storage` | `tests/e2e/storage/` | Storage E2E tests |
| `test-metering` | `tests/e2e/` | Tests marked `metering` |
| `test-references` | `tests/e2e/references/` | Cross-resource reference tests |

To run tests against an existing deployment, use a target directly:

```bash
make test-vmaas OSAC_MONO_DIR=/path/to/osac
```

Direct test targets do not provision infrastructure or install OSAC. Test
filters use pytest's `-k` option through the `TEST` variable. The root
`make test` target only checks the legacy placeholder; it does not run OSAC
E2E suites.

For suite-specific environment variables and fixtures, see the
[OSAC E2E guide](https://github.com/osac-project/osac/tree/main/tests/e2e).

## Configuration

`INFRA`, `DEPLOY_SUITE`, and `TEST_SUITE` apply to infrastructure orchestration.
Direct `test-*` targets run against an existing deployment and do not need
those selectors.

| Variable | Purpose |
|---|---|
| `INFRA` | Required backend selector; choose a backend under `infra/`. |
| `DEPLOY_SUITE` | Deployment scenario, validated against the selected backend's capabilities. It defaults to that backend's `DEFAULT_DEPLOY_SUITE` when declared; otherwise specify a supported scenario. |
| `TEST_SUITE` | Test target; defaults to `DEPLOY_SUITE` and may be selected independently. |
| `OSAC_MONO_DIR` | OSAC mono-repo checkout used by the E2E targets; defaults to `/opt/osac`. |
| `TEST` | Optional pytest `-k` filter for a direct test target; not for `test-vmaas`. |
| `KUBECONFIG` | Optional hub-cluster kubeconfig override forwarded to the backend; otherwise it uses the backend's default. |
| `OSAC_VM_KUBECONFIG` | Optional workload-cluster kubeconfig override; otherwise it uses the backend's default. It may equal `KUBECONFIG` for a single-cluster deployment. |
| `OSAC_PULL_SECRET_PATH` | Optional path for a pull secret needed by deployment or tests; a backend may write it to `.env.infra`. |
| `EXTRA_VARS` | Backend-specific deployment variables passed through by the root Makefile. |

The root Makefile treats these as orchestration inputs. `EXTRA_VARS` is an
opaque backend-specific payload; it does not automatically become environment
for pytest. Each backend decides how to map Make inputs to its tools and writes
test runtime values to `.env.infra`, which the root loads before `run-tests`.
Follow the corresponding guide or workflow in the inventory for exact
variable mappings, credentials, prerequisites, and defaults.

## Adding a backend

Create `infra/<name>/` with:

- A Makefile implementing the targets in [infra/contract.md](infra/contract.md).
- A shell-sourceable `capabilities` file declaring `SUPPORTED_DEPLOY_SUITES` and, optionally, `DEFAULT_DEPLOY_SUITE`.
- Optional shell-sourceable `contracts/<test-selection>` files declaring
  required `.env.infra` variables.

Test code and test fixtures belong in the OSAC mono-repo. Add a root
`test-<selection>` target only when introducing a new OSAC test selection that
the root Makefile does not already provide.
