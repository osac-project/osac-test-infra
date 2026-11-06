# CI Observe

CI Observe shows why an E2E run failed and where it spent time.

## Open it

Sign in to Grafana with GitHub. Open **ci-observe** and choose **Start here**.

- **Start here**: recent E2E runs.
- **E2E run**: one run.
- **Pull requests**: review and merge-queue time.
- **Trends**: one test over time.

## E2E run

The summary shows **Result**, **Tests run**, and **Test time**. Open a row for detail.

- **Failures**: failed tests and error messages.
- **Test list**: every test, its result, start time, and run time.
- **Run info**: GitHub run, workflow, pull request, branch, and test paths.

A run without detailed test data says **No recording**. A run whose code details were not saved says **Run details not saved**.

## Pull requests

- **Recent pull requests**: PR, repository, title, last stage, age, queue runs, and left-queue count.
- **Stage times**: typical and slow-end time for each stage.
- **Selected pull request**: dates and total time. Enter a PR number above.

“Slow end” means 95% of pull requests finished within that time.

## Trends

Enter a test name above and open **Test history**. The charts show passes, failures, and test run time for the selected days.

## Data

Run details, test reports, cluster data, and pull request history are kept for 30 days. Tempo searches are limited to 7 days. Pod logs stay in the GitHub artifact.

## Missing data

| What you see | What it means |
|---|---|
| **No recording** | The run has no detailed test data. |
| **Run details not saved** | The run did not save its code or test settings. |
| No test history | No readable test report exists for the selected days. |

## More detail

- [`model.md`](model.md): the three dashboard units.
- [`sources.md`](sources.md): where each answer comes from.
- [`../ci-observe.md`](../ci-observe.md): deployment and backend checks.
