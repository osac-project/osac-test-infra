# Collector-first local investigation

This prototype uses standard OpenTelemetry collection to investigate existing OSAC tests locally.
The application and individual tests keep their current source and behavior.

## Scope

The first milestone collects test phases, Go service requests and selected cluster evidence.
It saves standard telemetry files near the workload, then imports them into a local Grafana development stack.
GitHub artifact delivery and automatic CI activation follow local acceptance.

The local backend is Grafana's `otel-lgtm` development image. Beyla observes Go requests.
A stock Pytest plugin supplies test context. OpenTelemetry Collector Contrib collects and replays the data.
There is no new database schema, custom exporter or per-test registration.

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

The capture action verifies delivery after the test: it scrapes the gateway
collector's self-metrics (`:8888`) and fails the job when telemetry was
requested but never reached the central backend (deployment failed, receiver
refused data, exporter send failures, sent less than accepted, or nothing
collected at all). The failure message always says the test result above is
unaffected — the test's own exit code stays the run step's outcome.

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
