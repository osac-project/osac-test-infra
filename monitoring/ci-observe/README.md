# ci-observe: what it is and how to read it

ci-observe records what happens inside an OSAC E2E test run and shows it in Grafana. Use it to answer "why did this run fail" and "where did the time go" without reading raw logs.

You need two things:

1. Access to Grafana at `https://<central-host>:3000`. Sign in with GitHub; only members of the `osac-project` org get in.
2. A run with ci-observe turned on. The person who starts an E2E workflow sets the `ci-observe` input. Without it, no data is captured.

If Grafana will not load, or you see no data for a run you know finished, read [What to do when data is missing](#what-to-do-when-data-is-missing).

## Start here

Open the **ci-observe** folder in Grafana and pick **CI Observe — Start here**. This page lists recent runs. Every row is one capture, and the run id links to its detail page. To find one run, type its GitHub run id into the **Find** box; it narrows the cards and tables to matching captures.

A capture is one E2E workflow run that had ci-observe enabled. Its id has three parts: `bmaas-main-41318528583-a1` is the suite, the GitHub run id, and the run attempt.

The [model](model.md) explains the three units behind these pages: one run, one pull request, and many runs over time.

There are five dashboards today. The [model](model.md) sets the target of three; until that change lands, use this table.

| Dashboard | Question it answers | When to open it |
| --- | --- | --- |
| **CI Observe — Start here** | Which runs exist, which failed, which are missing data? | Always start here. |
| **CI Observe — Run Investigation** | What happened inside one run? | After you pick a run. |
| **CI Observe — Run evidence** | Where did the run's time go, and what did CI gather? | When you need step timings or the JUnit result. |
| **CI Observe — Compare runs** | Did these runs get slower, and which tests dominate the time? | When you have two or more runs of the same revision. |
| **CI Observe — Cluster** | How many pods existed, and what did they use in CPU and memory? | When a run looks slow and you suspect the cluster. |

**CI Observe — Cluster** reads metrics, not spans. Set its time range to cover the capture, because a finished capture has no samples in "now". Samples are one minute apart and kept for 15 days.

## Read one run in four steps

1. **Find the run.** On **Start here**, the three cards at the top count the runs. `Runs with a session span` counts complete runs. `Failed runs` counts runs that ended in failure. `Missing session span` counts captures that sent test data but no run summary. The tables below list the runs.
2. **Open the detail page.** Click a run id. The top row of the detail page is the summary: `Tests`, `Failed`, `Result`, `Session`, `Test time`, `Suite`, `PR`. Read `Result` first. If it says `Incomplete`, the run summary never arrived and the other cards describe only what did arrive.
3. **Read the timeline.** `Test timeline` lists every test with its start time and duration. `Slowest tests` and `Failed tests, longest first` put the expensive and broken tests at the top. `Longest waits` shows the named waits inside tests, so a 16-minute test can turn into "instance RUNNING 282 s, metering heartbeats 60 s, deletion 243 s".
4. **Compare, if you can.** If a second run has the same source revision, harness revision and test selection, open **Compare runs** and pick that cohort. The page shows the sample size before any table, because one run is not a comparison.

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

A cohort is not stored anywhere. The comparison page builds it at query time from those fields, so any merge to either repo starts a new cohort.

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

The dashboards read those labels with Tempo queries, written in TraceQL. This is the query behind the tests table on the comparison page:

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
- **Not collected**: this capture does not have the field. Older captures predate identity stamping, so fields such as the resolved harness SHA are empty for them.
- **Exact cohort**: a cohort with all three commits present.
- **Source-revision cohort**: a fallback for old captures that lack the harness commits. The page labels it "not exact".
- **No baseline**: the cohort has fewer than two captures. The tables show one run's numbers, and there is nothing to compare against.
- **Wait span**: one named wait inside a test, for example polling until an instance is `RUNNING`. Wait spans are how ci-observe explains slow tests.
- **Service call**: an RPC from one OSAC service to another. A service call can fail while the test still passes, so service errors are not run failures.
- **Worker**: one of the four parallel pytest processes, labelled `osac.ci.xdist_worker` such as `gw3`.

## What ci-observe does not do

These gaps are known and deliberate for now.

- **It does not show a cluster event timeline yet.** The Cluster dashboard shows pods, CPU and memory. Kubernetes events are stored but have no readable view. Pod logs are not collected; after a failure they are in the GitHub artifact.
- **It does not make tests faster or slower.** ci-observe is opt-in telemetry. A delivery failure shows a red step marker and an error annotation, and leaves the job result unchanged.
- **It does not cover production.** It runs only in the disposable E2E test cluster.

## Does it run inside the OSAC services?

No. Nothing is injected into the fulfillment service, the operator, or any other OSAC container. Those services run unchanged.

The telemetry comes from two separate components that the capture action deploys into the test cluster, in the `ci-observability-canary` namespace:

- A **collector gateway** Deployment. It receives test spans from a pytest plugin, Kubernetes events and kubelet metrics from the test namespace, and forwards them to the central backend on `osac-ci-1`. It is configured to read pod logs, but the unprivileged collector cannot read them today.
- A **Beyla** DaemonSet. Beyla reads network requests from the node's kernel. It runs as root and can see every process on its node, so it needs extra permissions in the test cluster. It watches processes from outside and never attaches to them.

The bare-metal runner gets no privileged observer at all. So the production answer is: ci-observe is not deployed to production, and it does not alter any OSAC service. The thing to keep in mind is that Beyla is a privileged observer on the nodes of the disposable test cluster.

## Data and privacy

For debugging, ci-observe stores more than numbers: raw request parameters, exception text and command lines. It scrubs the registry pull secret and the AAP license before sending. Traces, logs and metrics stay in the test cluster and the central backend, and the capture step uploads nothing to GitHub. When a run fails, the workflow's gather step uploads a redacted log artifact to GitHub, kept for 7 days.

## Limits that change what you see

- **Traces appear late.** Tempo hides freshly written data from search for up to 15 minutes. A run that just finished can be missing from a query and appear later.
- **JUnit evidence expires.** GitHub keeps the run artifact, which holds the JUnit file, for 7 days. Older captures have traces but no native pass/fail detail.
- **One query per panel.** Each panel runs a single query, so a page can take up to two minutes on a cold backend. A blank panel while loading is not an empty result.
- **Tables below the fold load when you scroll to them.** An empty table you have not scrolled into view is not an empty result. Scroll the panel into view and wait.

## What to do when data is missing

| What you see | Likely cause | What to do |
| --- | --- | --- |
| No run on **Start here** | ci-observe was not enabled, or the data is still inside the 15-minute window | Check the workflow inputs, then wait and refresh. |
| `Missing session span` includes the run | The session span never reached the backend | Open the run. The test data is still usable. Check the workflow's capture step for a delivery error. |
| All identity fields say `not collected` | The capture predates identity stamping | Use a capture from after 2026-11-01, or ask the monitoring owner. |
| **Compare runs** says `No baseline` | Fewer than two comparable captures | Run the same suite again with the same source and harness revisions. |
| A panel shows a red error | The trace backend timed out | Refresh. If it keeps failing, tell the monitoring owner. |

## Where the details are

- [`monitoring/ci-observe/model.md`](model.md): the product model. Three units, three dashboards, the shared keys and the rule for new panels.
- [`monitoring/ci-observe/sources.md`](sources.md): which store answers which question, its retention, and whether it covers one run or the whole population.
- [`monitoring/ci-observe.md`](../ci-observe.md): the operator reference. Architecture, deployment, validation and known backend gaps.
- [`monitoring/README.md`](../README.md): the whole monitoring stack, including how to deploy it and how to reach Grafana.
- [`monitoring/config/ci-observe/`](../config/ci-observe/): the cluster manifests for the gateway and Beyla.
