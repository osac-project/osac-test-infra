# ci-observe: what it is and how to read it

ci-observe records what happens inside an OSAC E2E test run and shows it in Grafana. Use it to answer "why did this run fail" and "where did the time go" without reading raw logs.

You need two things:

1. Access to Grafana at `https://<central-host>:3000`. Sign in with GitHub; only members of the `osac-project` org get in.
2. A run with ci-observe turned on. The person who starts an E2E workflow sets the `ci-observe` input. Without it, no data is captured.

If Grafana will not load, or you see no data for a run you know finished, read [What to do when data is missing](#what-to-do-when-data-is-missing).

## Start here

Open the **ci-observe** folder in Grafana and pick **CI Observe — Start here**. The page lists recent runs. A run with a capture links to it. A run with no capture appears as `not collected` with a link to GitHub, so an ingest failure is visible instead of hidden.

The page's rule is that it takes a run id, a capture id, a pull request number, a test id, or nothing. Run id and capture id work in the **Find** box. The pull request and test id lookups sit next to it and open the selected pull request or test.

A capture is one E2E workflow run that had ci-observe enabled. Its id has three parts: `bmaas-main-41318528583-a1` is the suite, the GitHub run id, and the run attempt.

Four pages are in use: the entry page and three content dashboards. The [model](model.md) explains the three units behind them: one run, one pull request, and many runs over time. Compare is a section of Trends, not a page of its own.

| Dashboard | Question it answers | When to open it |
| --- | --- | --- |
| **CI Observe — Start here** | Which runs exist, which failed, which are missing data? | Always start here. |
| **CI Observe — Capture** | What happened inside one run? | After you pick a run. |
| **CI Observe — Pull requests** | How long did this change take, and why did it leave the queue? | When you have a pull request number. |
| **CI Observe — Trends** | Is this test flaky or slower, and is CI passing overall? | When you have a population question. |

On **Pull requests**, a pull request with no stored queue history says `no queue history`, which is not the same as `no ejections`. An ejection is derived from a failed `merge_group` run plus the comment GitHub leaves on the pull request, because GitHub keeps no merge-queue history.

The capture page holds the whole run, in collapsed sections:

| Section | Question it answers |
| --- | --- |
| **Run overview** | Did it pass, how many tests ran, and did the run summary arrive? |
| **Collection coverage** | Which layers arrived? |
| **CI evidence** | Where did the run's time go, what failed, and what did CI gather? GitHub jobs, step timing, the JUnit result, the failed tests and the artifact link. |
| **Run identity** | Which source commit, harness commit, workflow and image refs did the run use? |
| **Tests and workers** | Which tests ran, in what order, and on which worker? |
| **Fixture work** | Which fixtures cost the most setup time? |
| **Waits** | Which named waits dominated a slow test? |
| **Service requests** | What did the OSAC services do? |
| **Environment** | How many pods existed, and what did they use in CPU and memory? |
| **Events** | What did the cluster report? |

**Environment** reads metrics, not spans. Set the time range to cover the capture, because a finished capture has no samples in "now". Samples are one minute apart and kept for 15 days.

The **Events** row reads the Kubernetes events already stored in Loki, labelled by capture id. The row reads them. Pod logs are not there: the collector cannot read `/var/log/pods`, so pod logs exist only in the GitHub artifact.

## Read one run in five steps

1. **Find the run.** On **Start here**, the three cards at the top count the runs. `Runs with a session span` counts complete runs. `Failed runs` counts runs that ended in failure. `Missing session span` counts captures that sent test data but no run summary. The tables below list the runs, including `not collected` rows for runs with no capture.
2. **Open the detail page.** Click a run id. The top row of the detail page is the summary: `Tests`, `Failed`, `Result`, `Session`, `Test time`, `Suite`, `PR`. Read `Result` first. If it says `Incomplete`, the run summary never arrived and the other cards describe only what did arrive.
3. **Read the timeline.** `Test timeline` lists every test with its start time and duration. `Slowest tests` and `Failed tests, longest first` put the expensive and broken tests at the top. `Longest waits` shows the named waits inside tests, so a 16-minute test can turn into "instance RUNNING 282 s, metering heartbeats 60 s, deletion 243 s".
4. **Open the evidence and the cluster.** Expand **CI evidence** for the GitHub job, step timings, the JUnit result, the failed tests and the artifact link. Expand **Environment** for the pods, their CPU and memory, and the node totals. The **Events** row shows what the cluster reported.
5. **Compare, if you can.** If a second run has the same source revision, harness revision and test selection, open **Trends** and pick that cohort in **Compare**. The section shows the sample size before any table, because one run is not a comparison.

## How the data is structured

Four words carry the whole design: span, trace, capture and cohort.

A **span** is one timed step. When the test code starts something, it records the name, the start time, the duration, a status and a few labels. That record is a span. Spans nest like a recipe: "cook dinner" is the outer span, and "boil water" sits inside it. The outer duration includes the inner ones, so you can always drill down from a 90-minute run to the 53-second wait that dominated one test.

A **trace** is all the spans for one operation, linked by a trace id and parent ids. One test run is one trace, or a few traces when the work is split across four parallel workers.

A **capture** is one CI run with recording on. Its name carries the suite, the branch or PR, the GitHub run id and the attempt: `caas-main-41446977693-a1`. The attempt matters because a rerun of the same run gets `-a2`.

A **cohort** is a group of captures that are fair to compare. Think of lap times: same track, same car, same driver, different days. If the track, the car or the number of laps changes, the times stop being comparable.

| Cohort field | What it controls | If it changes |
| --- | --- | --- |
| `test_sha` | The OSAC code under test | You are comparing two products |
| `test_infra_sha` | The test scripts and actions | The test itself changed |
| `test_infra_workflow_sha` | The workflow that launches the run | Timeouts, retries and setup change |
| `test_suite`, `test_filter`, `test_marker` | Which tests ran | The sample is different |

A cohort is not stored anywhere. The **Compare** section of Trends builds it at query time from those fields, so any merge to either repo starts a new cohort.

The data flows from two producers to three stores:

```
pytest ──OTLP──┐
               ├── gateway ──OTLP/HTTP── osac-ci-1 ──┬── Tempo      (spans)
Beyla ──OTLP───┘                                    ├── Prometheus (metrics)
                                                    └── Loki       (logs)
                                                         │
                                                      Grafana
```

Tempo stores spans, Prometheus stores metrics, and Loki stores logs.

`pytest` writes one span per run, per test, per test phase (setup, call, teardown), per fixture and per named wait. `Beyla` writes one span per network call between services. The gateway stamps every span with the capture identity, so one field keeps two runs apart on a shared backend.

The dashboards read those labels with Tempo queries, written in TraceQL. This is the query behind the tests table in the Compare section of Trends:

```
{ resource."osac.ci.capture_id" = "caas-main-41446977693-a1"
  && .pytest.span_type = "test"
  && name !~ ".*::(setup|call|teardown)" }
```

In words: spans from this capture, of type test, whose name is not one of the three phases. That leaves one span per test, which carries the test's whole duration.

## Words you will see

The four core words are defined under [How the data is structured](#how-the-data-is-structured). These are the rest.

- **Attribute**: a label on a span, such as `pytest.nodeid` or `osac.ci.test_sha`.
- **Session span**: the single `test run` span that wraps the whole pytest session. It carries the run's start, end and result.
- **Incomplete**: test spans exist, but the session span does not. You can still see which tests ran. You cannot see the run's overall duration or result.
- **Not collected**: the value is missing. A field says `not collected` when the capture predates its collection or the query returned nothing; a run with no capture at all appears in the index as `not collected` with a GitHub link.
- **Exact cohort**: a cohort with all three commits present.
- **Source-revision cohort**: a fallback for old captures that lack the harness commits. The page labels it "not exact".
- **No baseline**: the cohort has fewer than two captures. The Compare tables then show one run's numbers only.
- **Wait span**: one named wait inside a test, for example polling until an instance is `RUNNING`. Wait spans are how ci-observe explains slow tests.
- **Service call**: an RPC from one OSAC service to another. A service call can fail while the test still passes, so service errors are not run failures.
- **Worker**: one of the four parallel pytest processes, labelled `osac.ci.xdist_worker` such as `gw3`.

## What ci-observe does not do

These gaps are known and deliberate.

- **It does not show pod logs.** The collector cannot read `/var/log/pods`, so pod logs are not delivered to Loki; after a failure they are in the GitHub artifact.
- **It does not make tests faster or slower.** ci-observe is opt-in telemetry. A delivery failure shows a red step marker and an error annotation, and leaves the job result unchanged.
- **It does not cover production.** It runs only in the disposable E2E test cluster.

## Does it run inside the OSAC services?

No. Nothing is injected into the fulfillment service, the operator, or any other OSAC container. Those services run unchanged.

The telemetry comes from two separate components that the capture action deploys into the test cluster, in the `ci-observability-canary` namespace:

- A **collector gateway** Deployment. It receives test spans from a pytest plugin, Kubernetes events and kubelet metrics from the test namespace, and forwards them to the central backend on `osac-ci-1`. It is configured for pod logs, but the unprivileged collector cannot read `/var/log/pods`.
- A **Beyla** DaemonSet. Beyla reads network requests from the node's kernel. It runs as root and can see every process on its node, so it needs extra permissions in the test cluster. It watches processes from outside and never attaches to them.

The bare-metal runner gets no privileged observer at all. So the production answer is: ci-observe is not deployed to production, and it does not alter any OSAC service. The thing to keep in mind is that Beyla is a privileged observer on the nodes of the disposable test cluster.

## Data and privacy

For debugging, ci-observe stores more than numbers: raw request parameters, exception text and command lines. It scrubs the registry pull secret and the AAP license before sending. Traces, logs and metrics stay in the test cluster and the central backend, and the capture step uploads nothing to GitHub. When a run fails, the workflow's gather step uploads a redacted artifact to GitHub, kept for 7 days. The artifact holds the failed tests, the JUnit XML, the per-test `failure.json`, the pod logs and the cluster snapshots.

## Two CI hooks outside `monitoring/`

`monitoring/` owns the central stack and the in-cluster collector manifests. Two hooks run on the CI runner, so they live in the workflow side of the repository:

- **The capture action**, `.github/actions/ci-observe-capture/action.yml`. A workflow can only call a local action by path from its own repository, so the action sits beside the workflows that use it. It deploys the collectors, runs the test command and verifies telemetry delivery.
- **The gather script**, `scripts/gather-osac-logs.sh`, wrapped by `.github/actions/collect-artifacts/`. It runs against the live test cluster on the runner and collects the failure evidence that the workflow uploads. It is part of every E2E failure path, with or without ci-observe.

The reason is the same for both: they are steps in a GitHub workflow, not services on the central machine. The configs they deploy and the backend they push to still live under `monitoring/`.

## Limits that change what you see

- **Traces appear late.** Tempo hides freshly written data from search for up to 15 minutes. A run that just finished can be missing from a query and appear later.
- **JUnit evidence expires.** GitHub keeps the run artifact, which holds the failed tests, the JUnit XML, `failure.json`, the pod logs and the cluster snapshots, for 7 days. Older captures have traces but no native pass/fail detail.
- **One query per panel.** Each panel runs a single query, so a page can take up to two minutes on a cold backend. A blank panel while loading is not an empty result.
- **Tables below the fold load when you scroll to them.** An empty table you have not scrolled into view is not an empty result. Scroll the panel into view and wait.

## What to do when data is missing

| What you see | Likely cause | What to do |
| --- | --- | --- |
| No row on **Start here** | ci-observe was not enabled, or the data is still inside the 15-minute window | Check the workflow inputs, then wait and refresh. |
| A run says `not collected` | The run produced no capture, or its capture never arrived | Open the GitHub link on the row, and check the workflow's capture step for a delivery error. |
| `Missing session span` includes the run | The session span never reached the backend | Open the run. The test data is still usable. Check the workflow's capture step for a delivery error. |
| All identity fields say `not collected` | The capture predates identity stamping | Use a capture from after 2026-11-01, or ask the monitoring owner. |
| **Trends**, **Compare** says `No baseline` | Fewer than two comparable captures | Run the same suite again with the same source and harness revisions. |
| A panel shows a red error | The trace backend timed out | Refresh. If it keeps failing, tell the monitoring owner. |

## Where the details are

- [`monitoring/ci-observe/model.md`](model.md): the product model. Three units, three content dashboards and the entry page, the shared keys and the rule for new panels.
- [`monitoring/ci-observe/sources.md`](sources.md): which store answers which question, its retention, and whether it covers one run or the whole population.
- [`monitoring/ci-observe.md`](../ci-observe.md): the operator reference. Architecture, deployment, validation and known backend gaps.
- [`monitoring/README.md`](../README.md): the whole monitoring stack, including how to deploy it and how to reach Grafana.
- [`monitoring/config/ci-observe/`](../config/ci-observe/): the cluster manifests for the gateway and Beyla.
- [`.github/actions/ci-observe-capture/`](../../.github/actions/ci-observe-capture/): the workflow hook that deploys the collectors and runs the test under telemetry.
- [`scripts/gather-osac-logs.sh`](../../scripts/gather-osac-logs.sh): the workflow hook that collects the failure artifact.
