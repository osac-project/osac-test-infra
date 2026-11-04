# ci-observe operator reference

This is the operator reference for ci-observe, the telemetry path that records OSAC E2E runs.
For an introduction, start with [ci-observe/README.md](ci-observe/README.md).

The application and the individual tests keep their current source and behavior.

## Status at the 2026-11-01 cutover

The collector-first prototype became the live CI path. What works now, all verified
against the central backend on `osac-ci-1`:

- Live push is the delivery path. A `ci-observe` run streams pytest traces, cluster
  logs and kubelet metrics to the central backend while the test runs. Nothing is
  uploaded to GitHub, so the capture step needs no artifact and no gitleaks scan.
- The dashboard shows real numbers for a finished capture: 17 tests, 24.1 minutes
  run duration, the slowest-test table with per-test durations. Run duration
  uses the single `test run` session span, not the separate xdist worker spans.
- Every E2E wait is a named span (`poll_until` in the osac repo). The 16-minute
  bmaas metering test breaks down into named waits: instance RUNNING 282 s,
  metering heartbeats 60 s, instance deletion 243 s.
- Kubelet metrics arrive: the cluster panels show node and pod CPU and memory.
- Capture ids are readable (`bmaas-main-<run-id>-a1`), with suite and PR promoted
  to Loki and Prometheus labels.
- Delivery is verified after each test: the capture action reports a loud,
  non-gating error when telemetry was requested but never reached the backend.
  The job result and the test's own exit code stay unchanged.
- Traces from the batch era stay queryable through the archive datasource. The
  31 batch session traces were migrated into Tempo 2.10.

What is missing or known gaps:

- Beyla service-request spans from the batch era were not migrated, only session
  traces. Old captures show test spans in the service-request panels.
- The Traces Drilldown app (v3.1.0) is installed and served but not yet confirmed
  rendering in a user's browser. Explore with query type Trace ID is the working
  path to a trace waterfall today.
- Count stat panels render unitless (`47`, not `47 ns`); `Run duration` keeps its
  time unit. Verified in Grafana 13.2.4.
- The router keeps its sending queues in memory. A router restart while a backend
  is down discards already accepted batches. Persistent queue storage is a
  follow-up, not part of the cutover.
- Collectors use `insecure_skip_verify` for the internal CA. Mounting the CA into
  the gateway pod for real verification is a follow-up.
- Freshly flushed Tempo blocks are invisible to TraceQL search for up to 15
  minutes (the backend search sharder skips young blocks). A just-finished run's
  spans appear with that delay; this is Tempo 2.10 behavior, not a bug.
- GitHub OAuth users may see a reduced sidebar without Explore. The admin account
  sees the full navigation. Granting the OAuth users the needed org role is an
  open task.

## Scope

ci-observe collects test phases, Go service requests and selected cluster evidence for E2E runs.
A pytest plugin supplies test context, Beyla observes Go requests, and OpenTelemetry Collector Contrib collects and forwards the data.
There is no new database schema, custom exporter or per-test registration.

The central backend is Grafana's `otel-lgtm` image: Grafana with Tempo, Loki and Prometheus.
It runs on `osac-ci-1`, next to the Tempo 2.10 instance that holds the newer traces.

## Current validation

The supplied OpenShift cluster is reachable through `~/.kube/full.kubeconfig`.
It runs OSAC in `osac-e2e-ci`. Its RHEL CoreOS 5.14 kernel exposes readable BTF, tracefs and disabled lockdown.
Those checks establish prerequisites; only a successful Beyla attachment proves runtime collection.

The unmodified invalid-token E2E passes against that cluster.
It intentionally receives an unauthenticated RPC, so request errors and native test results must stay separate.

## Ownership and safety

Collectors use the dedicated `ci-observability-canary` namespace and their own service accounts.
They read selected workloads and never restart OSAC or alter node security settings.
Host process access is limited to the approved eBPF collector inside the OpenShift guest.
The bare-metal runner receives no privileged observer.

Local listeners bind to loopback. Raw captures and authentication material stay outside Git.
Application logs stay private until the approved redaction and scanning path accepts them for publication.
Exactly two long-lived secrets are scrubbed from telemetry (the registry pull secret and the
AAP license); raw parameter values, exception text and command lines are kept on purpose for
debuggability, per the user decision recorded in the collector config.

## Evidence rules

- GitHub owns job conclusions. A local capture has its own identity and no invented GitHub job.
- Native reports own test outcomes. An RPC error can be an expected assertion in a passing test.
- A capture groups an observation window. Trace context or explicit resource IDs establish causal links.
- Missing sources, interrupted intervals and refused exports are visible gaps.
- Shared fixture work is counted once. Overlapping phases are never added to wall-clock time.
- Human durations use fixed minutes with two decimals. JSON preserves unrounded values.
- An interrupted import remains partial. Restarting a backend does not justify silently importing everything twice.

## Cancellation semantics

A forwarded SIGINT or SIGTERM decides the observed run's exit code: 128 plus that signal,
even when the test command handled the signal and exited zero. CI therefore sees the
cancellation instead of a fake native success. The child's own signal mask and dispositions
stay untouched. A child still running past the escalation delay after the first forwarded
signal is killed with SIGKILL; descendants that ignore a forwarded signal while their
leader already exited are not reaped, and that gap is accepted.


## Live telemetry push

An E2E workflow run with the `ci-observe` input deploys the collector
gateway into the cluster it tests, and the gateway streams every signal
(pytest and Beyla traces, cluster logs, kubelet metrics, stamped with the
capture identity) live to the central backend while the test runs:

- The runners push to `https://osac-ci-1.redhat.com` (port 443): an HAProxy
  SNI route for that hostname forwards to Caddy, which terminates TLS with
  its internal CA and enforces HTTP basic auth, and proxies the host
  otelcol-router service on `127.0.0.1:14318`. The router
  (`otelcol-router.service`, a plain systemd system service running
  `otelcol-contrib` — not a container) splits the pushes by signal: traces
  to the Tempo 2.10 backend on `127.0.0.1:14418`, logs and metrics to the
  LGTM bundle's OTLP receiver on `127.0.0.1:4318`. It runs on the host on
  purpose: rootless podman (pasta) cross-container networking silently ate
  OTLP in earlier container-based router attempts, so the whole routing
  path stays on loopback TCP. Its files live in this repository
  (`monitoring/config/ci-observe/otelcol-router/`) and are deployed as root
  by `monitoring/scripts/deploy-otelcol-router.sh`; central Prometheus
  scrapes its self-metrics on `127.0.0.1:18888` so a dropped signal is
  visible. Collectors set `insecure_skip_verify` for the
  internal CA certificate; mounting the CA into the gateway pod for real
  verification is a follow-up.
- The credential (username `ci-observe`) lives in the
  `CI_OBSERVE_OTLP_PASSWORD` secret of the `osac` and `osac-test-infra`
  repositories; the callers pass it to the reusable workflows as
  `ci-observe-otlp-password`, and the capture action renders it into the
  gateway as a precomputed `Authorization` header. An empty password or
  endpoint degrades to running the test without telemetry — never an
  unauthenticated push.
- Nothing touches GitHub: no capture artifact, no upload gate, no scan.
  Telemetry is visible in the CI Grafana during the run.

Every captured signal carries run identity and test selection as `osac.ci.*`
resource attributes. The fields distinguish requested refs, resolved source
SHAs, deployment image refs, runtime image IDs and pytest selection:

| Attribute | Meaning |
| --- | --- |
| `test_ref` | The osac ref the caller requested. It can be a branch name (`main` on schedules) or a commit SHA; a moving ref can advance between dispatch and checkout, so this alone does not prove what ran. |
| `test_sha` | The exact commit the osac test source checkout resolved (the overlay step's `git rev-parse HEAD` after `checkout FETCH_HEAD`). A 40/64 lowercase hex SHA or `unknown`; never the requested ref. |
| `test_infra_ref` | The test-infra ref the caller requested. `unspecified` when the caller did not request one. |
| `test_infra_sha` | The workspace commit whose five harness files (the capture action, the capture supervisor and the three collector configs) are tracked at HEAD and byte-identical to the copies this job runs. `unknown` when any check fails. |
| `test_infra_workflow_sha` | `job.workflow_sha`: the commit of the reusable workflow file that defines the `e2e` job. |
| `test_suite` | The exact pytest path arguments the test command ran, e.g. `tests/e2e/vmaas/ tests/e2e/references/`; `tests/e2e/` when no suite was selected. `unknown` when the caller reported no selection. |
| `test_filter` | The exact pytest `-k` expression, or `none` when the run passed no filter. |
| `test_marker` | The exact pytest `-m` expression, or `none` when the run passed no marker. |
| `osac_images` | The Deployment spec image refs (including mutable tags) requested for the OSAC workloads. This existing attribute is unchanged. |
| `osac_image_ids` | Runtime IDs from the Running pods of the namespace's Deployments, plus `=unknown` entries for expected containers without enough IDs. A truncated list ends in `...[truncated]`. |
| `osac_image_ids_complete` | `true` only when every desired replica had an image ID and the list was not truncated. `false` marks missing IDs or truncation; `unknown` marks an unavailable query. |
| `workflow`, `run_attempt` | GitHub workflow name and attempt number. |

The source- and image-identity values answer different questions, and none
stands in for another:

| Value | Kind | What it proves | What it does not prove |
| --- | --- | --- | --- |
| `test_ref` | requested ref | What the caller asked to test | Which commit actually ran |
| `test_sha` | resolved SHA | Which osac commit the overlay checkout resolved and the tests ran from | Which test-infra harness wrapped the run |
| `test_infra_ref` | requested ref | Which harness ref the caller asked for (often `unspecified`) | Which harness commit ran |
| `test_infra_sha` | resolved SHA | The harness files this job runs are byte-identical to this commit's | That the whole checkout matches (the overlay replaces `tests/`); which workflow file called the action |
| `test_infra_workflow_sha` | resolved SHA | The commit of the reusable workflow file that defines this job | The checkout SHA of any repository |
| `osac_images` | image refs | What the Deployment specs asked the cluster to run | What image bytes the kubelet actually pulled |
| `osac_image_ids` | runtime IDs | What the kubelet reports running, per deployment container | That the spec refs were honored (a mutated tag can differ) |

GitHub defines [`job.workflow_sha`](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#job-context)
as the SHA of the workflow file that defines the current job. The same
documentation shows a reusable workflow using `job.workflow_sha` and
`job.workflow_repository` to check out its own source. These full-install
files define the `e2e` job and declare `workflow_call`, so this is the called
reusable workflow's SHA, not the caller's SHA. GitHub also documents that a
same-repository `./.github/workflows/...` call uses the caller's commit for
the called file. In this repo the checkout step uses `job.workflow_sha` as
its same-repository fallback. `github.workflow_sha` and `GITHUB_WORKFLOW_SHA`
describe the workflow run; `GITHUB_SHA` describes the triggering event. None
proves which alternate test-infra checkout ran.

The action records `git rev-parse HEAD` from the workspace its local
action and scripts run in, but only after `git cat-file -e HEAD:<path>`
proves every one of the five harness files is tracked at HEAD and `git
diff --quiet HEAD` proves the worktree copies are byte-identical: `git
diff` alone reports no difference for a path HEAD does not contain, which
would claim a commit that never had the running file. The overlay step's
`tests/` replacement never matters because only the five harness paths
are checked. Missing git, a dirty worktree or an untracked replacement
all keep the conservative `unknown`. (Precision note: `git update-index
--assume-unchanged` or `--skip-worktree` could make a modified harness file
look clean to `git diff`; the CI checkout never sets those flags, so the
proof holds there.) On scheduled runs the checkout
defaults to `job.workflow_sha`, so `test_infra_sha` and
`test_infra_workflow_sha` are expected to match there; on this repo's PR
flows the checkout is the test-infra PR head, and the two SHAs differ by
design.

`osac_images` and `osac_image_ids` answer different questions. The first
keeps the requested Deployment spec refs, including mutable tags. The second
reads `.status.containerStatuses[].imageID` only from Running pods owned by a
Deployment in the observed namespace, the same Deployments `osac_images`
lists. The namespace is the population because OSAC sets no label shared by
all of its Deployments: the fulfillment services carry `app=<name>`, the
operator workloads carry `app.kubernetes.io/instance=osac`, and the Ansible
Automation Platform workloads carry `app.kubernetes.io/name`. It compares
those IDs with every declared Deployment container and its desired replica
count. Init containers are not entries: they exit before the main containers
run, so their IDs describe a start step, not the workload. Missing IDs appear
as `deployment/container=unknown`; the completeness attribute is false. The
helper preserves underscores, rejects unsafe image ID characters and hostile
deployment names (degrading the
whole value to `unknown`) instead of rewriting them, sorts the mappings
and caps the value at 4 KiB. If the cap cuts the list, `...[truncated]`
marks the end and completeness stays false. A scaled-to-zero Deployment
records `=unknown` for its containers: it declares no running replica,
so no runtime image evidence exists. Kubernetes formats `imageID` by runtime; it is
often a pullable digest reference, but the attribute preserves the reported
ID rather than claiming every runtime returns a registry digest.

The suite, filter and marker stay unindexed resource attributes. The
recorded suite is the exact path list pytest received, computed in each
workflow's resolve step from the same effective suite and marker the test
steps read, including the `references/` addition a marker triggers for
vmaas, bmaas and the caas/caas-regression directories (the
`monitoring/tests` suite pins the recorded selection to the test steps'
own computation). Values are rendered as JSON-quoted YAML strings with
`$` defused, so a custom expression remains exact without changing the
test command. Suite values over 256 UTF-8 bytes and filters or markers
over 512 UTF-8 bytes become `unknown`; CI emits a warning. Empty filter
or marker values become `none` (the wired callers pass them only when
the command really ran without `-k`/`-m`); an empty suite selection
means no selection was reported and records `unknown`. Local captures
use `local` for the suite, `none` for filter and marker, and `unknown`
for `test_sha` and the workflow SHA: a local run wraps an arbitrary
command with no osac checkout to resolve.

The standard full-install flows record:

| Flow | `test_suite` | `test_filter` | `test_marker` |
| --- | --- | --- | --- |
| VMaaS PR, merge queue and schedule | `tests/e2e/vmaas/ tests/e2e/references/` | `none` | `not requires_caas and not requires_bmaas` |
| CaaS PR and merge queue | `tests/e2e/caas/sanity/` | `none` | `sanity` |
| CaaS schedule | `tests/e2e/caas/ tests/e2e/references/` | `none` | `sanity or regression or requires_caas or reference_common` |
| BMaaS PR and merge queue | `tests/e2e/bmaas/sanity/` | `not TestBmaasNetworking` | `none` |
| BMaaS schedule | `tests/e2e/bmaas/serial/` | `not TestBmaasNetworking` | `none` |

Workflow dispatch records the effective suite and exact filter and marker
inputs selected for that run. VMaaS records the resolved tier fallback, if a
requested tier contains no tests. If the coordinator cannot determine a
value, it uses a non-empty `unknown` or `none` value because the collector
rejects empty resource attributes. Invalid refs and SHAs become `unknown`
whole, never a mangled value.

These identity attributes are wired end to end, but only a live capture
exercises the full chain: the workflow context, the overlay step output,
the workspace git metadata and the cluster queries. A post-merge canary
run with the `ci-observe` input or label must show, in the CI Grafana
for its capture id: `test_ref` equal to the requested ref, `test_sha`
matching the osac commit the run's overlay step logged, `test_infra_sha`
matching the merged test-infra commit (equal to
`test_infra_workflow_sha` on a scheduled run), `test_suite` naming every
path the pytest log collected, and `osac_image_ids` populated with
`osac_image_ids_complete=true`.

The `ci-observe-sync.yml` workflow is no longer scheduled: live pushes made
it unnecessary for new runs, and its 15-minute backend restart would drop
in-flight live-ingest batches. It remains dispatchable by hand to backfill
historical `ci-observe-capture-*` artifacts:

1. `ci-observe.py up` ensures the pinned LGTM stack is running.
2. `ci-observe.py sync` downloads new artifacts from `osac-project/osac` and
   `osac-project/osac-test-infra` through the authenticated `gh` CLI and
   imports them.
3. Each repository's sync report is posted to the job summary.

With the schedule gone, the backend's own config
(`monitoring/config/ci-observe/lgtm-overrides/`) had no deploy path left;
`ci-observe-backend.yml` fills that gap: on a push to main touching
`monitoring/config/ci-observe/**` (or a manual dispatch) it runs exactly the
sync workflow's vault-auth and `ci-observe.py up --quadlet` steps on the
monitoring-central runner, and nothing else. It deliberately has no
schedule, for the same batch-dropping reason.

The capture action verifies delivery after the test: it reads the gateway
collector's self-metrics through the same port-forward the deploy step
opened (one forward for `:4317` and `:8888`), and reports a warning when
telemetry was requested but never reached the central backend (deployment
failed, input was refused, batches could not be queued, or the exporter
queue was still busy when a bounded wait expired). Send failures are
information: the exporter retries them. Residue in the queue is reported as
in flight, not as a verdict. The step always exits 0 and has
`continue-on-error: true`, so it is loud but never gating: the job result
and the test's own exit code stay unchanged.

The action also posts one `osac_ci_capture_info` metric after deploy and
again during verify, but only when the gateway actually deployed: both
posts require `CI_OBSERVE_DEPLOYED=1` and no deploy-failure marker. A failed
deploy posts nothing, because the run is a no-telemetry run and a registry
capture with no data would read as a capture that lost its telemetry. The
payload is value 1, the capture id as the resource attribute, and the run
identity (`ci_run_id`, `ci_run_attempt`, `ci_suite`, `ci_pr`,
`ci_workflow`, the refs and SHAs, and the test selection) as datapoint
attributes. It posts to `https://osac-ci-1.redhat.com` on 443, the runner-reachable HAProxy route to the central ingest vhost; runners cannot reach the vhost's own port. Those identity attributes stay on this one series; the resource
processor never promotes them globally. A failed post is a warning. The
central registry then knows the capture exists even when its traces and
logs arrive late or not at all.

The backend uses the persistent state directory `~/monitoring/ci-observe` and
fixed loopback ports: UI 3300, OTLP 4318, Tempo 3200, Loki 3100 and Prometheus
9092. The other ports on that machine are taken (3000 deployed Grafana, 3443
Caddy, 9090 cockpit, 9091 central Prometheus, 9100-9104 exporters). The
deployed Grafana runs with host networking, so its provisioned `CI Observe
Traces`, `CI Observe Logs` and `CI Observe Metrics` datasources reach those
ports directly.

Traces are the exception to that single-backend picture: since the
2026-11-01 cutover, new traces go to a separate Tempo 2.10 container
(`monitoring/quadlet/ci-observe-tempo210.container`, API on 3210, OTLP on
14418, deployed by `monitoring-setup.sh`), because Tempo 3.1.1 in the LGTM
bundle has a TraceQL search bug (grafana/tempo#8054) that returns only
about two thirds of the matching spans. The `CI Observe Traces` datasource
points at Tempo 2.10 (3210); the bundle's Tempo 3.1.1 keeps serving port
3200 as the read-only `CI Observe Traces Archive` datasource for traces
ingested before the cutover. The deployed Grafana must stay on v13 or
newer: v12's Tempo datasource plugin does not support backend TraceQL
search against Tempo 2.10, which the CI Observe dashboard's panels depend
on.

Sync failures fail the workflow. A silent broken sync is worse than a red
schedule. Cached artifact ZIPs and extracted captures older than 14 days are
pruned after each run: the data already lives in the backend, and the sync
window is 24 hours, so nothing a sync still matches is ever deleted.

## Acceptance checkpoints

| Checkpoint | Required evidence |
| --- | --- |
| L0 | Approved namespace, exact privileges, pinned tools and recorded baseline. |
| L1 | Real serial and xdist Pytest processes preserve outcomes and report accurate supported intervals. |
| L2 | Actual Go service telemetry and cluster records replay locally with original timestamps. |
| L3 | An existing OSAC test links explicitly to its request and a resource where applicable. |
| L4 | A short runbook captures, opens and reopens evidence without changing a test. |

The implementation is incomplete until these boundaries have executed successfully.
Component checks against synthetic data do not establish deployed collection or the CI artifact path.
