# ci-observe: what it is and how to read it

ci-observe records what happens inside an OSAC E2E test run and shows it in Grafana. Use it to answer "why did this run fail" and "where did the time go" without reading raw logs.

You need two things:

1. Access to Grafana at `https://<central-host>:3000`. Sign in with GitHub; only members of the `osac-project` org get in.
2. A run with ci-observe turned on. The person who starts an E2E workflow sets the `ci-observe` input. Without it, no data is captured.

If Grafana will not load, or you see no data for a run you know finished, read [What to do when data is missing](#what-to-do-when-data-is-missing).

## Start here

Open the **OSAC CI** folder in Grafana and pick **CI Observe — Test Runs**. This page lists recent runs. Every row is one capture, and the run id links to its detail page.

A capture is one E2E workflow run that had ci-observe enabled. Its id has three parts: `bmaas-main-41318528583-a1` is the suite, the GitHub run id, and the run attempt.

There are three dashboards. They answer different questions, and you normally read them in this order.

| Dashboard | Question it answers | When to open it |
| --- | --- | --- |
| **CI Observe — Test Runs** | Which runs exist, which failed, which are missing data? | Always start here. |
| **CI Observe — Run Investigation** | What happened inside one run? | After you pick a run. |
| **CI Observe — Compare runs** | Did these runs get slower, and which tests dominate the time? | When you have two or more runs of the same revision. |

## Read one run in four steps

1. **Find the run.** On **Test Runs**, look at the three cards at the top. `Runs with a session span` is the number of complete runs. `Failed runs` counts runs that ended in failure. `Missing session span` counts captures that sent test data but no run summary. The tables below list the runs themselves.
2. **Open the detail page.** Click a run id. The top row of the detail page is the summary: `Tests`, `Failed`, `Result`, `Session`, `Duration`, `Suite`, `PR`. Read `Result` first. If it says `Incomplete`, the run summary never arrived and the other cards describe only what did arrive.
3. **Read the timeline.** `Test timeline` lists every test with its start time and duration. `Slowest tests` and `Failed tests, longest first` put the expensive and broken tests at the top. `Longest waits` shows the named waits inside tests, so a 16-minute test can turn into "instance RUNNING 282 s, metering heartbeats 60 s, deletion 243 s".
4. **Compare, if you can.** If a second run has the same source revision, harness revision and test selection, open **Compare runs** and pick that cohort. The page shows the sample size before any table, because one run is not a comparison.

## Words you will see

- **Capture**: one E2E run with telemetry enabled. The run id is its name.
- **Session span**: the single `test run` span that wraps the whole pytest session. It carries the run's start, end and result.
- **Incomplete**: test spans exist, but the session span does not. You can still see which tests ran. You cannot see the run's overall duration or result.
- **Not collected**: this capture does not have the field. Older captures predate identity stamping, so fields such as the resolved harness SHA are empty for them.
- **Cohort**: the set of runs that are comparable, meaning the same resolved source commit, harness commit, workflow commit and test selection.
- **No baseline**: the cohort has fewer than two captures. The tables show one run's numbers, and there is nothing to compare against.
- **Wait span**: one named wait inside a test, for example polling until an instance is `RUNNING`. Wait spans are how ci-observe explains slow tests.
- **Service call**: an RPC from one OSAC service to another. A service call can fail while the test still passes, so service errors are not run failures.

## What ci-observe does not do

These gaps are known and deliberate for now.

- **It does not show what happened inside the test cluster.** Logs, events, node metrics and pod metrics are collected and stored, but there is no readable event view yet. This is the deferred Q5 item.
- **It does not make tests faster or slower.** ci-observe is opt-in telemetry. A delivery failure shows a red step marker and an error annotation, and leaves the job result unchanged.
- **It does not cover production.** It runs only in the disposable E2E test cluster.

## Does it run inside the OSAC services?

No. Nothing is injected into the fulfillment service, the operator, or any other OSAC container. Those services run unchanged.

The telemetry comes from two separate components that the capture action deploys into the test cluster, in the `ci-observability-canary` namespace:

- A **collector gateway** Deployment. It receives test spans from a pytest plugin, plus pod logs, Kubernetes events and kubelet metrics from the test namespace, and forwards them to the central backend on `osac-ci-1`.
- A **Beyla** DaemonSet. Beyla reads network requests from the node's kernel. It runs as root and can see every process on its node, so it needs extra permissions in the test cluster. It watches processes from outside and never attaches to them.

The bare-metal runner gets no privileged observer at all. So the production answer is: ci-observe is not deployed to production, and it does not alter any OSAC service. The thing to keep in mind is that Beyla is a privileged observer on the nodes of the disposable test cluster.

## Data and privacy

For debugging, ci-observe stores more than numbers: raw request parameters, exception text and command lines. It scrubs the registry pull secret and the AAP license before sending. Everything stays in the test cluster and the central backend; nothing is uploaded to GitHub.

## Limits that change what you see

- **Traces appear late.** Tempo hides freshly written data from search for up to 15 minutes. A run that just finished can be missing from a query and appear later.
- **JUnit evidence expires.** The workflow exporter keeps native JUnit results for 7 days. Older captures have traces but no native pass/fail detail.
- **One query per panel.** Each panel runs a single trace query. A page can take up to two minutes on a cold backend. A blank panel while loading is not an empty result.

## What to do when data is missing

| What you see | Likely cause | What to do |
| --- | --- | --- |
| No run on **Test Runs** | ci-observe was not enabled, or the data is still inside the 15-minute window | Check the workflow inputs, then wait and refresh. |
| `Missing session span` includes the run | The session span never reached the backend | Open the run. The test data is still usable. Check the workflow's capture step for a delivery error. |
| All identity fields say `not collected` | The capture predates identity stamping | Use a capture from after 2026-11-01, or ask the monitoring owner. |
| **Compare runs** says `No baseline` | Fewer than two comparable captures | Run the same suite again with the same source and harness revisions. |
| A panel shows a red error | The trace backend timed out | Refresh. If it keeps failing, tell the monitoring owner. |

## Where the details are

- [`monitoring/ci-observe.md`](../ci-observe.md): the operator reference. Architecture, deployment, validation and known backend gaps.
- [`monitoring/README.md`](../README.md): the whole monitoring stack, including how to deploy it and how to reach Grafana.
- [`monitoring/config/ci-observe/`](../config/ci-observe/): the cluster manifests for the gateway and Beyla.
