# CI Observe

CI Observe shows why an E2E run failed and where it spent time.

## Open it

Sign in to Grafana with GitHub. Open **ci-observe** and choose **Start here**.

- **Start here**: recent E2E runs.
- **E2E run**: one run.
- **Pull requests**: review and merge-queue time.
- **Trends**: one test over time.

## Start here

The table shows one row per E2E run:

| Column | Meaning |
|---|---|
| Run | GitHub run id. Opens the E2E run page. |
| Job | Workflow name. |
| Repo | `osac` or `osac-test-infra`. |
| PR | Pull request number. Opens GitHub. |
| Branch | Branch or merge-queue ref. |
| Trigger | Scheduled, manual, push, PR or merge queue. |
| Result | GitHub pass or fail. |
| Recorded | Yes when test data arrived, No when it did not. |
| Created | When GitHub created the run. |
| GitHub | Opens the workflow run. |

`No` under Recorded means the job sent no test data. Netris, full-regression and agentless jobs have no capture wiring yet, so they cannot record.

## E2E run

The page opens from a **Run** link and covers that run's time range.

The summary shows **Result**, **Tests run**, and **Test time**. Result comes from GitHub. A live run shows **Still running** and refreshes every minute.

Open a row for detail:

- **Failures**: failed tests, error messages, test time, and a link to the full artifact.
- **Test list**: every test, result, start time, and duration.
- **Run info**: GitHub run, pull request, workflow, commit, and test paths. Every identifier is a link.
- **Cluster**: pods, CPU by pod, memory by pod, node CPU, and node memory during the run.

Cluster graphs are flat at zero before the test cluster is created.

A run without detailed test data shows **Not finished** and no test count. A run whose code details were not saved says **Run details not saved**. A trace with no session end stays **Not finished** until the session span arrives.

## Pull requests

- **Recent pull requests**: PR, repository, title, last stage, age, queue runs, and left-queue count.
- **Stage times**: typical and slow-end time for each stage.
- **Selected pull request**: dates and total time. Enter a PR number above.

"Slow end" means 95% of pull requests finished within that time.

## Trends

Enter a test name above and open **Test history**. The charts show passes, failures, and test run time for the selected days.

## Data

Run details, test reports, cluster data, and pull request history are kept for 30 days. Tempo searches are limited to 7 days. Pod logs stay in the GitHub artifact.

## Missing data

| What you see | What it means |
|---|---|
| **No recording** | The run has no detailed test data. |
| **Run details not saved** | The run did not save its code or test settings. |
| **Not finished** | The trace has no end of session. Reload after the run ends. |
| No test history | No readable test report exists for the selected days. |
| No pod samples | The capture has no cluster metrics for this time range. |

## More detail

- [`observability.md`](observability.md): why the system looks like this, what is missing, and the roadmap.
- [`model.md`](model.md): the three dashboard units and their sections.
- [`sources.md`](sources.md): where each answer comes from.
- [`../ci-observe.md`](../ci-observe.md): deployment and backend checks.
