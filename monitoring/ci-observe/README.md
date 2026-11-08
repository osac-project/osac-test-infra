# CI Observe

CI Observe shows how healthy the VMaaS, CaaS and BMaaS E2E runs are, where their time goes, why they fail, how each test behaves over time, and how long pull requests wait.

## Open it

Sign in to Grafana with GitHub. Open **ci-observe** and choose **Overview**.

| Page | Question |
|---|---|
| **Overview** | Is E2E CI healthy, and where should I look? |
| **Where the time goes** | Which phases, steps, tests and product waits take the time, and what got slower? |
| **Why runs fail** | Did runs fail on tests, the build, the OSAC install or CI infrastructure? Which tests fail or flake? |
| **Test history** | How long does one test take, is it getting slower, does it fail, and what does it wait for? |
| **Pull requests** | How long do pull requests wait for review, approval and the merge queue? How often are they rebased and rerun? |
| **All runs** | Every E2E run, newest first. |
| **E2E run** | One run: what failed and where its time went. |

The bar at the top of every page opens the others. **Window**, **Suite**, **Started by** and **Repository** carry over between Overview, Where the time goes, Why runs fail and Test history.

## How to read a number

- Hover the **ⓘ** of a panel. Every description ends with **Population** (what is counted), **Time** (which window) and **Unit**.
- **vs before** compares the window with the same span just before it. Red means worse: slower, or failing more. It stays empty (–) until that whole earlier span is stored; About these numbers then says from which day it will be.
- A duration reads `hh:mm:ss`, or `1 d hh:mm:ss` past a day.
- **not measured**, **no earlier data** or **–** means the data cannot say. It is never zero.
- **About these numbers**, at the bottom of a page, says what the page counts, the exact window, what is missing, and **Data as of**: the newest stored run and when the hourly test-result and span rollups last ran. A rollup more than 3 hours old, or no new run for 12 hours, is listed under missing data with the timer to check.
- Names are links. A suite opens its time or failure page, a test opens its Test history, a run opens its run page (or GitHub when the run was not recorded), a pull request opens it.

## Definitions

| Term | Meaning |
|---|---|
| Suite run | One VMaaS, CaaS or BMaaS suite run, from any trigger. Netris, stretched, agentless and the named installs are other scenarios and are not counted. |
| Pass rate | Passed runs ÷ (passed + failed). A cancelled run (usually replaced by a newer push) is left out. |
| First-try pass rate | Runs that passed without a re-run ÷ (passed + failed). GitHub keeps one result per run and a re-run replaces it, so Pass rate shows only the last attempt; a run someone re-ran counts here as not passing the first time. The gap between the two is the failures re-runs hid. |
| Runs re-run | Runs someone re-ran on GitHub (Re-run jobs). A new run started by a new push or a `/test` comment is a run of its own, not a re-run. |
| Time to result | From GitHub creating the run's first job to its last job finishing. It includes the wait for a runner, because a developer waits for it too. Measured on passing runs: a failed run stops early. |
| Wait for a runner | The time the run's E2E job waited in GitHub's queue for a self-hosted runner. |
| Phase | A group of workflow steps: Waiting for a runner, CI setup, Cluster and hosts, Build, Install OSAC, Tests, Logs and teardown. Every step of the three suites' workflows has a phase. |
| Cause | The phase of a failed run's **first** failed step. Areas: **Tests**, **Build**, **OSAC install**, **CI infrastructure** (cluster, hosts, CI setup, teardown, a timeout, a lost runner) and **Blocked** (an unapproved fork or refused inputs). |
| Flaky test | A test that passed and failed on the same commit in the window. |
| A test's runs | The test's results from counted suite runs only. Staging, Production, Chart Packaging Smoke and Full Regression run the same tests but are not counted suite runs, so their results are left out. |
| A wait | One `poll_until` wait, grouped across runs by what it waits for: resource names that differ per run (`vm-ssvtr`, UUIDs, numbers) are replaced, so `vm-ssvtr Running` and `vm-9btxs Running` are one wait, `<id> Running`. |
| A test's time | **Setup** (its fixtures), **waiting for the product** (every `poll_until` wait: OSAC reconciling a resource), **test actions** (the rest: CLI and API calls, assertions) and **teardown**. The four add up to the test's duration. |
| Rebased | A pull request force-pushed at least once. |
| E2E reruns | E2E runs of a pull request beyond the first of each suite. |

## Overview

Two rows of numbers: pass rate, first-try pass rate, the pass rate's change, runs re-run, typical and slow time to result, the typical wait for a runner, the share of failures caused by CI, runs, failed runs, runner hours, the share of hours spent on failed runs, flaky tests and tests with a failure. Below them:

- **Suite health**: the same numbers per suite, with links to that suite's time and failure pages.
- **Where a passing run spends its time** and **Why runs failed**: one bar per suite.
- **Pass rate per day** and **Typical time to result per day**.
- **Where the tests' time goes** and **What the tests wait for in the product**: from the recorded runs.
- **Tests that fail most** and **Tests that take the most time**.
- **Latest failed runs**, then the pull request numbers of the window.

## Where the time goes

Numbers for the run as a whole, then the average passing run phase by phase, the phase table (average, median, p90, share, change), phase time per day, the wait for a runner per day and every step. **Inside the tests** splits the tests' own time, lists the tests by total time, and lists each `poll_until` wait grouped by what it waits for, with how long it took and how often it timed out, plus the shared fixtures.

## Why runs fail

Failed runs per cause, per day and per suite; the steps runs failed in; every failing test with its fail rate, flaky commits and last error; the flaky tests; the error messages grouped after replacing names, ids and numbers; and every failed run with its cause, failed step and first failed test.

## Test history

Pick the **Test** (type to search; the list starts with the tests that take the most time). The page shows the runs, failure rate, typical and slow duration, the change against the window before, flaky commits, the duration of every run (green passed, red failed) with the daily median, passes and failures per day, where the test's time goes and its change per day, what it waits for, the fixtures it sets up, why it fails, and every run.

## Pull requests

The top half covers every pull request merged or closed in the window, in the chosen **Repository**. It shows the median time to merge, the wait for a first review, review to approval, approval to the queue, time in the merge queue, queue ejections and re-queues, commits, rebases, force pushes, changes requested, E2E runs, reruns and failed E2E runs, then the stage medians per week and every pull request, newest first. The E2E numbers describe the pull requests that ran at least one suite run: most change nothing a suite tests. Click a number to open that pull request below. An attempt is **ejected** when its own checks failed or timed out, and **re-queued** when it was cancelled or replaced by a later attempt: every entry behind a failing one is re-queued, so a re-queue is not the pull request's failure.

The bottom half covers one pull request. Its identity line, the total and four long stages (Authoring, Review, Waiting and Queue), the stacked stage bar, the queue attempts with how each one ended (ejected, re-queued or passed), and every E2E run with the commit it tested. Queue time ends at the last merge-queue attempt; for an open pull request still in the queue it runs to now. An open pull request is read from GitHub when the page loads.

## All runs

One row per run, newest first: run, workflow, repository, PR, branch, trigger, result, recorded, suite, cause, failed step, run time and start. **Repository**, **Suite** and **Result** filter the data, and every column has a funnel filter. `No` under Recorded means no test data: most such runs failed before the suite ran. `Unknown` means the check could not judge. A run with no recording opens GitHub.

## E2E run

The summary shows the result, tests run and test time, then **Where this run's time went** (one bar per suite), the time to result and the wait for a runner. Open a row for detail: failures, jobs and steps, the test list (with each test's setup, waiting, actions and teardown), run info, cluster graphs, and **Waits, fixtures and the step timeline**. A live run shows **Still running** and refreshes every minute.

## Data

- The suite runs, their tests, their time split and the pull requests are kept for 90 days.
- Traces, cluster data and the GitHub artifact (pod logs) are kept for 30 days.
- New runs reach the pages within the hour: the test rollup runs at 25 past, the time split at 45 past.

## Missing data

| What you see | What it means |
|---|---|
| **not measured** | The data cannot give this value, for example the runner wait of a run stored before job timelines were kept. |
| **No recording** | The run has no detailed test data. |
| **Run details not saved** | The run did not save its code or test settings. |
| **Not finished** | The trace has no end of session. Reload after the run ends. |
| Empty *Inside the tests* | No recorded run of the window has a complete trace yet. |
| No pod samples | The capture has no cluster metrics for this time range. |

## More detail

- [`model.md`](model.md): the pages, what each counts, and the keys that join them.
- [`sources.md`](sources.md): where each answer comes from and how long it is kept.
- [`observability.md`](observability.md): why it looks like this, what is missing, and the roadmap.
- [`../ci-observe.md`](../ci-observe.md): deployment and backend checks.
