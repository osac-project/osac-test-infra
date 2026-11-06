# CI Observe

CI Observe shows why an E2E run failed and where it spent time.

## Open it

Sign in to Grafana with GitHub. Open **ci-observe** and choose **Start here**.

- **Start here**: recent E2E runs.
- **E2E run**: one run.
- **Pull requests**: review and merge-queue time.
- **Trends**: one test or one job over time.

## Start here

Filter by **Repository**, **Suite** and **Result** above the table. The filters narrow the data, so the rows stay honest. Every column also has a funnel filter.

The table shows one row per E2E run:

| Column | Meaning |
|---|---|
| Run | GitHub run id. Opens the run page when test data was recorded, and GitHub otherwise. |
| Workflow | Workflow name. |
| Repository | `osac` or `osac-test-infra`. |
| PR | Pull request number. Opens the Pull requests page. |
| Branch | Branch or merge-queue ref. |
| Started by | Scheduled, manual, push, PR or merge queue. |
| Result | GitHub pass or fail. |
| Recorded | Yes when test data arrived, No when the check proved it did not, Unknown when the check could not judge. |
| Started | When GitHub created the run. |
| GitHub | Opens the workflow run. |

`No` under Recorded means the job sent no test data. `Unknown` means the recording check could not judge: the run just ended, the capture list is stale, or the run is older than its coverage. A run with no recording opens GitHub from the Run cell; only a recorded run opens the run page. Netris, full-regression and agentless jobs have no capture wiring yet, so they cannot record.

## E2E run

The page opens from a **Run** link and covers that run's time range.

The summary shows **Result**, **Tests run**, and **Test time**. Result comes from GitHub. A live run shows **Still running** and refreshes every minute.

Open a row for detail:

- **Failures**: failed tests, error messages, test time, and a link to the full artifact.
- **Jobs and steps**: every job that ran, its result and duration, then each job's steps with their durations.
- **Test list**: every test, result, start time, and duration.
- **Run info**: GitHub run, pull request, workflow, commit, and test paths. Every identifier is a link.
- **Cluster**: pods, CPU by pod, memory by pod, node CPU, and node memory during the run.

Cluster graphs are flat at zero before the test cluster is created.

A run without detailed test data shows **Not finished** and no test count. A run whose code details were not saved says **Run details not saved**. A trace with no session end stays **Not finished** until the session span arrives.

## Pull requests

The page starts with one pull request. Choose one from the list, or type its number. The **Repository** control picks `osac` or `osac-test-infra`. The list holds pull requests that merged or closed in the last 30 days; an open pull request can still be typed.

- **Selected pull request**: its identity and dates. Opened, first review, approved, entered the queue, last queue attempt, merged or closed.
- **Time in each stage**: where the time went, with the reason a stage could not be measured.
- **E2E runs**: every run the pull request made, oldest first. One row per suite. A pull-request run shows the commit it tested, so a rebase appears as a new code version. A merge-queue run tests a temporary merge commit and shows no code version. Each row opens the run page when test data was recorded, and the GitHub run always.
- **Queue attempts**: each merge-queue entry, how long it lasted, and why it left.
- **Recent pull requests** and **Stage times**: the browse tables for all pull requests in the selected days.

A pull request that is still open is read from GitHub when the page loads, so its dates are current. The saved history starts after it merges or closes.

## Trends

The page answers "is this test or this job failing more often or getting slower?" for one unit over the last 7 or 30 days. Choose **Days**, choose **Unit** (Test or Job), then type a name or open a row from **Matching tests** or **Matching jobs**. A test is a pytest node id such as `tests/e2e/vmaas/test_vm.py::test_create`. A job is a repository and display name such as `osac / E2E VMaaS Full Install`, so retries and suites of one job count together.

| Section | Contents |
|---|---|
| Summary | Runs, Passed, Failed, pass rate, p50, p95, p99 and mean duration, plus the number of measured runs and the measurement basis. Pass rate is passes divided by all stored runs, including cancelled. A value the rollup never measured says **not measured**, never 0. |
| Daily passes and failures | One point per day for the selected unit. |
| Duration by day | One point per day: for a test, the median of that day's stored rows for p50, p95 and p99 and a run-weighted mean; for a job, true percentiles over that day's records. Neither is a percentile over the whole window; the Summary row is. |
| Recent runs | The newest 50 runs: when, result, duration, commit, and a link to the run. |
| Matching tests and Matching jobs | The units whose name contains what you typed, so a blank box is never the only choice. Opening a row selects that unit. |

The last row keeps the broad search: every test whose id matches the Name box, per day, summed over source runs.

A test's history is the daily JUnit rollup in `trends.db` plus the raw per-run rows behind it. A job's history is the workflow exporter's job records. GitHub owns pass or fail in both.

## Data

Run details, test reports, cluster data, and pull request history are kept for 30 days. Tempo searches are limited to 7 days. Pod logs stay in the GitHub artifact.

## Missing data

| What you see | What it means |
|---|---|
| **No recording** | The run has no detailed test data. |
| **Run details not saved** | The run did not save its code or test settings. |
| **Not finished** | The trace has no end of session. Reload after the run ends. |
| **No test history** | No readable test report exists for the selected days. |
| **not measured** | The rollup stored no duration for that day, so p99 or mean is unknown. It is not zero. Older days predate the raw per-run rows, so their window percentile is not measured either. |
| No pod samples | The capture has no cluster metrics for this time range. |

## More detail

- [`observability.md`](observability.md): why the system looks like this, what is missing, and the roadmap.
- [`model.md`](model.md): the three dashboard units and their sections.
- [`sources.md`](sources.md): where each answer comes from.
- [`../ci-observe.md`](../ci-observe.md): deployment and backend checks.
