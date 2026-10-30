# CI telemetry sources

This inventory identifies the jobs that execute tests and the result files available to monitoring.
Workflow triggers and effective test selection define separate cohorts, even when the jobs share a name.

The inventory was checked against osac commit `3311deb11607949a8fb13f664ee3f0668afe59ee`
and osac-test-infra commit `94cdbc7af22b6696c3407826a060832fee6b6de2` on 2026-10-27.
The workflow files remain authoritative when selection changes.

## End-to-end suites

The three standard full-install workflows use pytest and produce `junit.xml` under `/tmp/test-output`.
Their callers live in osac; their reusable runners live in this repository.

| Suite | Pull requests and merge queue | Scheduled osac runs |
| --- | --- | --- |
| VMaaS | Sanity, regression, and applicable references | Same selection |
| CaaS | Sanity; an explicit manual full variant adds regression | Sanity, regression, and CaaS references |
| BMaaS | Full suite, excluding Netris networking | Serial inventory-exhaustion suite and references |

Manual dispatch can select a narrower tier or filter.
Record the final pytest paths and expressions, including empty-tier fallback.
The trigger alone does not establish the selection.
The default is four xdist workers with file scheduling. CaaS, full BMaaS, and networking runs force serial execution.
Record the effective worker count after both workflow options and pytest hooks apply.

Additional entry points need their own collection coverage:

| Entry point | Source workflow in this repository | Native report |
| --- | --- | --- |
| BMaaS Netris | `e2e-bmaas-netris-full-install.yml` | Workspace `reports/` JUnit |
| VMaaS Netris | `e2e-vmaas-netris-full-install.yml` | Workspace `reports/` JUnit |
| Stretched Netris | `e2e-stretched-netris-full-install.yml` | Workspace `reports/` JUnit |
| Full regression | `e2e-full-regression.yml` | Combined serial pytest JUnit |
| AgentlessNet lab | `e2e-caas-agentless-net-full-install.yml` | Backend test script output |
| VMaaS snapshot | `e2e-vmaas.yml` | `/tmp/test-output/junit.xml` |

The Netris entries are manual-only in osac's PR policy.
This repository also has independently scheduled Netris callers; those are a different cohort.
The full-regression caller uses one installation for multiple service suites and runs every other day.
AgentlessNet and snapshot workflows are manually dispatched.

Netris test targets write JUnit, but the current gather invocation omits its path.
Collection must retain those files before workspace cleanup.
The AgentlessNet backend needs a native result adapter; a passing infrastructure step is not a testcase result.

## Unit, Envtest, and component integration

Osac's `.github/ci-tests.yml` declares the main unit and integration matrix.
Two additional operator unit jobs live in image-build workflows.

| Execution | Framework or command | Current report |
| --- | --- | --- |
| Fulfillment unit | Recursive Ginkgo suites under `internal` | Console only |
| Metering service, adapters, schema | Ginkgo per module | Console only |
| UI unit | Recursive pnpm test, executing frontend Vitest | Console only |
| Operator unit and Envtest | `make test` from `build-image.yaml` | Go coverage file; console results |
| BMF unit and Envtest | `make test` from `build-bmf-image.yaml` | Go coverage file; console results |
| Fulfillment integration | Four Ginkgo invocations through the installer | Four JSON reports |
| Operator integration | Ginkgo on Kind | Console only |
| BMF integration | NetBox lookup check and Ginkgo on Kind | Console only |
| AAP integration job | Pytest unit suites, then Ansible integration targets | Console and temporary target logs |
| Installer integration | Build, Helm install, and repeat-upgrade smoke | Diagnostic files |
| Darwin keychain | Focused Go tests on macOS | Console only |

Fulfillment's JSON reports are `service-only`, `manager-capability`, `manager-readiness`, and `bmaas-disabled`.
They include timestamps, process identity, attempts, and spec events.
Suite setup and teardown entries must stay separate from testcase counts.
Filtered spec entries must not count as executed tests.

An Ansible fatal message can be an expected, rescued failure. Use the target's final outcome and callback events.
A job's display name does not establish its tier. Record real dependencies such as Envtest, PostgreSQL, and Kind.

## Campaigns and nightly runs

Flake campaigns resolve one tested commit and repeat both unit and integration groups.
Identify campaign, group, repetition, workflow attempt, actual job execution, and individual test attempt separately.
The existing report counts suite executions; it does not calculate an individual-test flake rate.

Nightly runs use a temporary branch and previously built images selected by chart values.
Their test selection differs from the standard suite callers.
Retain that branch's resolved commit and deployed digests; a provisional tag alone does not identify an image.

## Identity and retention

| Source | Current retention or identity issue |
| --- | --- |
| Standard E2E artifacts | Seven days; names contain run ID but omit attempt |
| Netris artifacts | Seven days; fixed names omit both run and attempt |
| Fulfillment integration artifact | Seven days; ordinary runs use the fixed name `logs` |
| Installer integration artifact | One day |
| Flake plan and report | Thirty days; attempt-aware names |
| Most native unit and integration jobs | No result artifact |
| Workflow exporter history | Keeps the latest result when a workflow is rerun |

`GITHUB_JOB` is a logical string, not GitHub's numeric job execution ID.
Unresolved numeric IDs stay unbound until trusted reconciliation supplies an actual job.
Producer manifests cannot establish their own authority over GitHub conclusions.

Osac calls this repository's runners at floating `main` by project policy.
The reusable workflow revision and the script checkout revision can differ if `main` moves during startup.
Record both actual revisions. Osac-ci decision and verifier workflows use release commit pins.
The workflow's SHA, the tested SHA, the suite source, and the installer source remain distinct fields.

## Existing tests outside current CI selection

These are inventory gaps, not successful or skipped CI executions:

- Osac root `tests/unit/` helper and workflow-policy tests.
- Osac flake-report Python unit tests and several installer render/runtime checks.
- CSI driver Go tests and UI proxy Go tests.
- E2E storage, projects, enablement, and top-level regression directories outside selected pytest paths.

This observability work records these gaps. Adding a new merge gate needs a separate review.

## Sources

- Osac `.github/CI.md`, `.github/ci-tests.yml`, `.github/e2e-policy.yml`, and `.github/workflows/tests.yml`.
- Osac standard, Netris, nightly, unit, integration, and flake workflow files.
- Osac `tests/e2e/conftest.py` and the installer `Makefile` integration targets.
- This repository's `.github/workflows/`, shared artifact actions, `Makefile`, and `monitoring/scripts/`.
