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

All nine E2E execution families collect a shared management-cluster baseline:
VMaaS against an existing cluster; VMaaS, CaaS, and BMaaS full installs; full
regression; VMaaS, BMaaS, and stretched Netris; and CaaS agentless-net. Their
scheduled, PR, merge-queue, build, and manual callers inherit this behavior.
The shared `collect-artifacts` action runs before cluster/EC2 teardown;
`upload-artifacts` needs only the collected files. No guest-cluster or additional
plugin must-gather is added.

Artifacts include `mgmt-cluster/must-gather/`, collected with the default
`oc adm must-gather` image selected for the cluster release. Standard master
service logs include `host_service_logs/masters/kubelet_service.log` and
`crio_service.log` (also applicable to a single-node
management cluster).
This improves diagnostic coverage for [#470](https://github.com/osac-project/osac-test-infra/issues/470);
it is not a garbage-collection fix.

This is a **sanitized plaintext subset, not a complete unmodified must-gather
archive**. Before recursive redaction, the collector withholds archives and
compressed content (including Insights archives), binary/opaque or unsupported
files, links, unreadable files, and files above 32 MiB. Content checks inspect
magic bytes, tar headers, UTF-8 validity, and control characters, not just suffixes.
Recognizable archives inside base64 text are also withheld (up to three encoding
layers, withholding still-encoded data at that limit); archives are never
extracted or decompressed for public publication.
ASCII-whitespace-wrapped base64 blocks are also inspected, including blocks
following journal text. The bounded check tries four base64 alignments, matches
specific embedded format signatures, and validates embedded tar headers by
checksum. Excessive tar-header candidates are withheld as opaque. Encoded
candidates retain the 32-character recognition floor. This is not a general
decoder for arbitrary encodings, encryption, or fragmented/interleaved
payloads; no archive contents are extracted or published in decoded form.
Supported text includes logs, YAML, JSON/JSONL, HTML, stderr, and the standard
extensionless `version` and `timestamp` files. A narrow literal allowlist preserves
only the public structural directory names `host_service_logs`, `masters`, and
`workers`, and the fixed filenames `kubelet_service.log` and `crio_service.log`.
Every other source directory and filename is replaced with a deterministic
SHA-256 identifier: credentials can occur in names as well as content, and content
scanners do not sanitize names. No raw-name mapping is published. Service-log
suffixes remain navigable, but dynamic image/node wrapper names are hashed; this
preserves grouping while losing node identity and other dynamic path context.
Files outside the existing redaction suffixes receive `.txt`. Normalization stages
outside the artifact tree. The unchanged 32-MiB per-file bound also applies to
kubelet and CRI-O journals: oversized journals are omitted entirely, not truncated,
so this subset does not promise full service history. Total retained input is
capped at 128 MiB and 20,000 entries. `omissions.json` contains only fixed reason codes and counts,
never omitted names, paths, or values. Existing raw Netris must-gather/inspect
exclusions remain in effect.

`collection-status.json` reports `complete`, `partial`, `timed-out`, or `skipped`.
Missing/empty kubeconfig and an unreachable API are explicit skips. Collection
failures retain safe partial output without changing the test result. The
existing 900-second normal and 120-second cancellation collection budgets cover
custom diagnostics, must-gather, normalization, secret inventory, and recursive
redaction; collection subprocess groups are terminated before sanitization.
Budget is reserved for sanitization. A cancellation during collection is followed
by a fresh short collection; failed/interrupted sanitization never writes
`.redaction-complete`.

The host runner's actual `oc version --client` is recorded as a bounded numeric
release (or `unknown`), not inferred from the test container. The client runs
with a native gather `--timeout` below its external process-group cap. Native
timeout alone is insufficient: supported clients follow remote logs outside that
timeout, and gather errors return before downloading pod output. `--keep`
preserves those resources for a separately bounded recovery `oc rsync` from the
standard `copy` container's `/must-gather/`. A successful native download is not
copied twice; recovered output does not promote a failed/timed-out collection to
`complete`.

After custom collection (at most 30% of the total budget) and readiness checking,
30% of the total remains reserved for normalization, inventory and redaction.
Of the remaining must-gather window, recovery receives 35%, cleanup receives
15% (capped at 10 seconds), and the client receives the rest; its native timeout
is 80% of its remaining cap. Version and identity API requests, process shutdown
and copying consume their respective windows, never the sanitization reserve.
All subprocess groups, including copy and deletion, have external hard bounds.

Cancellation prioritizes cleanup over optional recovery: it stops the active
subprocess, skips new recovery copies, and stops before copying additional pods.
Later signals can stop newly active subprocesses without recursively handling
signals during a stop. Cancellation is rechecked after subprocess registration
and while waiting. Signal handlers only latch requests and signal the active
group; normal execution owns bounded termination grace, escalation and reaping,
so a signal cannot recursively wait on CPython's process-wait lock.
This covers signals during spawn. Ownership proof and UID-preconditioned deletion retain
their independent cleanup deadline, never a shortened copy allowance or the
sanitization reserve. An interrupted collection exits without authorizing upload.
Cleanup is best effort: failed or interrupted API proofs withhold deletion, and
runner hard termination or Kubernetes finalizers can still leave resources.

Recovery never selects namespaces by listing a prefix. A private, bounded
in-memory stream parser captures only the exact client's timestamped namespace
and role-binding creation records; plugin-prefixed, duplicate, stale or
ambiguous records cannot authorize recovery. Live API metadata must match the
command, generated names, creation window, namespace UID and binding ownership
and role. Cleanup rechecks the namespace UID and sends a UID-preconditioned
delete only for that proven identity, after copying (also after native success).
`collection-status.json` separately reports recovery and cleanup; accepted
deletion is only `deletion-requested`, not proof that resources disappeared.
Ownership/API failure withholds copy/deletion rather than touching other runs.
Server/API failure, an unsupported client output format, unavailable copy pods,
exhausted copy time, or hard runner death can still lose remote diagnostics or
leave resources until infrastructure teardown. An interrupted attempt does not
authorize upload; cancellation's fresh collection and the existing gates remain
required. No extra image, custom gather command, or cluster-admin binding is
created by the recovery helper.

`--keep` disables the client's own cleanup, including on success. There is no
unsafe cleanup fallback when the host client rejects flags, changes its output
format, or fails the identity checks: the helper never broadens namespace
selection or starts a replacement gather. Unknown version strings alone do not
disable recovery when the exact creation records and API proof still match.
Unsupported flags can prevent gathering; unsupported output or lost API access
can leave the privileged namespace and its cluster-admin binding behind.
Existing-cluster jobs do not destroy their cluster afterward, so an operator
must remove retained resources there; infrastructure teardown is not a fallback
for those jobs. Cleanup status is diagnostic evidence, not a retention guarantee.

Uploads require the redaction marker, a successful must-gather content guard,
successful gitleaks scanning, and the mandatory release gate, which rechecks the
subset and fixed-schema metadata immediately before upload. Gitleaks cannot be
disabled for this subtree. Scanner failure, missing reports, or remaining
findings withhold the upload. The release gate also censors known pull-secret and
namespace Secret values. Diagnostics steps are best effort; their failure does
not replace the original test outcome.

The infrastructure-only regression commands are
`python3 -I .github/scripts/must_gather_test.py` and
`python3 -I .github/scripts/upload_artifacts_test.py` (PyYAML is required, as in CI).
They use synthetic data and stubbed commands, not live clusters.

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
