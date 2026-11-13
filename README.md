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

## E2E diagnostic artifacts

All nine E2E execution families collect the default management-cluster
`oc adm must-gather` before cluster or runner teardown: existing-cluster VMaaS,
VMaaS/CaaS/BMaaS full installs, full regression, VMaaS/BMaaS/stretched Netris,
and CaaS agentless-net. No guest-cluster or additional plugin gather is added.

The native hierarchy survives under `mgmt-cluster/must-gather/`, including
image, node and resource context, extensionless `version`, and
`host_service_logs/masters/{kubelet,crio}_service.log`. Command stdout/stderr is
captured there, not printed raw to CI. The existing shared recursive redaction,
gitleaks scan, known-secret release gate and public/private upload checks apply;
`.redaction-complete` still authorizes only a completed shared redaction pass.

Collection uses the existing 900-second normal / 120-second cancellation budgets.
Native gathering uses at most 600 seconds of the remaining time, reserving 60
seconds for ordinary diagnostics and shared redaction after the short Secret
inventory snapshot. An external timeout also bounds the command. Failures may
leave only locally downloaded partial output or none; no separate recovery or
namespace cleanup is attempted. Diagnostic failures do not replace test results.

A post-command 128-MiB total-size check, including the captured command log,
withholds an oversized must-gather subtree without discarding other diagnostics.
This is a retained-payload cap, **not an instantaneous disk quota**. The final
shared release gate withholds recognizable archives/compressed files within
must-gather, including Insights archives and representative base64-wrapped archive
headers (a bounded 8-KiB prefix per candidate, including whitespace wrapping).
It does not unpack or repack them. Symlinks and unreadable/oversized files remain
subject to the shared gate. Other diagnostics, including public telemetry's
optional pre-scan contract, are unchanged; private publication always requires a
successful scan. Native names are not anonymized, and content scanners do not
promise detection of arbitrary encodings or credentials embedded in filenames.

This improves diagnostic coverage for [#470](https://github.com/osac-project/osac-test-infra/issues/470),
not the underlying garbage-collection failure. Infrastructure consumer tests run
with `python3 -I .github/scripts/must_gather_test.py` and
`python3 -I .github/scripts/upload_artifacts_test.py` (PyYAML required, as in CI).

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
