# CI Observe model

CI Observe has three content pages, each answering one question. **Start here** lists recent runs and opens one.

| Page | Unit | Question |
|---|---|---|
| **E2E run** | One GitHub Actions run | Did it pass, what failed, and where did the time go? |
| **Pull requests** | One pull request | How long did review and the merge queue take? |
| **Trends** | One test or one job over time | Is this test or this job failing more often or getting slower? |

The principles behind the pages, the current gaps, and the roadmap are in [`observability.md`](observability.md).

## Start here

One table of recent E2E runs: Run, Workflow, Repository, PR, Branch, Started by, Result, Recorded, Failed in, Started and GitHub. Repository, Suite and Result filter the data; every column also has a filter. Every identifier is a link. `No` under Recorded means no test data. Failed in is derived from the exact failed step: **Test** for a pytest step (`Run E2E tests` or `Run E2E tests (ci-observe capture)`), **Setup / Infra** for any other step, and **Gate** for a gate/precondition job. It is blank for a pass or a cancelled run with no failed step. Most `No` rows are runs that failed before the suite started, so no pytest ran and no test data was expected. Gate-only runs are left out of the table, so Gate is rare. `Unknown` means the check could not judge, and a row with no recording opens GitHub.

The Suite filter uses exact workflow names, not a substring. A suite is the suite's full-install workflow, its PR starter and its merge-queue row:

| Suite | Workflows |
|---|---|
| VMaaS | E2E VMaaS, E2E VMaaS Full Install, E2E VMaaS PR suite, E2E vmaas rows of the pull-request and merge-queue flows |
| CaaS | E2E CaaS Full Install, E2E CaaS PR suite, E2E CaaS Agentless-Net Full Install, E2E caas rows of the pull-request and merge-queue flows |
| BMaaS | E2E BMaaS Full Install, E2E BMaaS PR suite, E2E bmaas rows of the pull-request and merge-queue flows |
| Netris | E2E VMaaS Netris Full Install, E2E BMaaS Netris Full Install, and their PR suites |
| Stretched | E2E Stretched Netris Full Install and its PR suite |

The named installs (**E2E VMaaS Production Install**, **E2E VMaaS Staging Install**, **E2E VMaaS Chart Packaging Smoke**, **OSAC CI Large**) run the same full-install workflow but test another scenario, so they are not suite rows. They still record: their callers pass the capture secret.

## E2E run

| Section | Contents |
|---|---|
| Summary | Result, tests run, and test time. Result comes from GitHub. |
| Failures | Failed tests, error messages, test time, and a link to the full artifact. |
| Jobs and steps | Every job, its result and duration, and each job's steps with their durations. |
| Test list | Every test, result, start time, and duration. |
| Run info | GitHub run, pull request, workflow, commit, and test paths, with links. |
| Cluster | Pods, CPU by pod, memory by pod, node CPU, and node memory for the run window. |

A run has detailed data only when its recording arrived. A missing recording does not change the GitHub result. A trace with no session end shows **Not finished** until the session span arrives.

## Pull requests

Choose the pull request from the list, or type its number, and pick the repository. The list holds pull requests that merged or closed in the last 30 days.

| Section | Contents |
|---|---|
| Selected pull request | Identity, state, and the dates of each stage. |
| Time in each stage | Authoring, review, waiting, queue and merge, with the reason a stage is unknown. |
| E2E runs | Every run the pull request made, oldest first, one row per suite. A pull-request run shows its commit, so a rebase is a new code version. A merge-queue run tests a temporary merge commit and shows no code version. Rows open the run page or GitHub. |
| Queue attempts | Each merge-queue entry, its duration, and whether it was ejected and why. |
| Recent pull requests and stage times | The browse tables for the selected days. |

An open pull request is read from GitHub when the page loads. Its runs are
already stored; the saved stage history starts after it ends.

An **ejection** means a pull request left the merge queue without merging. GitHub does not keep queue history, so the page derives it from a failed queue run and a GitHub comment. For an open pull request, the failed queue run is the only signal.

## Trends

The page answers one question for one unit over 7 or 30 days: is this test or this job failing more often or getting slower?

| Section | Contents |
|---|---|
| Pick a unit | Days, Unit (Test or Job) and Name, then the matching tests and matching jobs lists. Opening a row selects that unit. |
| Summary | Runs, Passed, Failed, pass rate, p50, p95, p99 and mean duration, the measured-run count and the measurement basis. Pass rate is passes divided by all stored runs, including cancelled. Missing values say **not measured**, never 0. |
| Daily passes and failures | One point per day for the selected unit. |
| Duration by day | For a test, the median of that day's stored rows for p50, p95 and p99 and a run-weighted mean; for a job, true percentiles over that day's records. Not a percentile over the whole window. |
| Recent runs | The newest 50 runs: when, result, duration, commit, and the run link. |
| Test history (all matching tests) | The broad search, kept from before: every test whose id contains the Name box, per day. |

A test is a pytest node id (`tests/e2e/vmaas/...::test_x`). Its outcomes and durations come from the daily JUnit rollup and the raw per-run rows behind it in `trends.db`. A job is the workflow exporter's job identity, `repo` + `display_name` (for example "osac / E2E VMaaS Full Install"), not a single row id, so retries and suites of one job aggregate. Its percentiles come from the raw job records in the 30-day window.

A window percentile is computed from raw per-run durations. Days rolled up before those rows existed keep their daily values and counts; their window p99 and mean are not measured, and the page says so. Test reports are kept for 30 days.

The E2E run page's **Jobs** table links each job name to this page with that job already selected.

## Keys

A run is keyed by its GitHub run ID. A recording ID adds the suite and rerun number. A pull request is keyed by its number. A test is keyed by its test ID. A capture ID joins a run to its traces, logs and cluster metrics.

## Data limits

- Tempo keeps traces for 30 days and limits one search to 7 days.
- Prometheus and Loki keep cluster data for 30 days.
- GitHub keeps the redacted run artifact, including pod logs, for 30 days.
- The workflow exporter keeps run and pull request history for 30 days. Prometheus keeps the cluster graphs for 30 days.
- Trend rollups are kept for 30 days today. Longer rollups are on the roadmap.

## Known gaps

No run waterfall or task timeline, no attempts or flake view, no Kubernetes events, no waits table, no per-test trace links, no CI SLOs, no capture coverage target, and no telemetry pipeline health. [`observability.md`](observability.md) lists the impact and the planned order.

## Out of scope

Fleet health (runners, machines, queue infrastructure) belongs to the **CI Fleet** folder.
