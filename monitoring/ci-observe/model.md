# CI Observe model

CI Observe covers the VMaaS, CaaS and BMaaS E2E suites. Four pages look across runs, three look at one thing.

| Page | Unit | Question |
|---|---|---|
| **Overview** (entry page) | All suite runs in a window | Is E2E CI healthy, and where should I look? |
| **Where the time goes** | All suite runs in a window | Which phases, steps, tests and product waits take the time? What got slower? |
| **Why runs fail** | All failed suite runs in a window | Tests, build, install or CI infrastructure? Which tests fail or flake, and with what error? |
| **Test history** | One test over a window | How long does it take, is it getting slower, does it fail, what does it wait for? |
| **Pull requests** | All pull requests in a window, then one pull request | How long do review, approval and the merge queue take? How often are pull requests rebased and rerun? |
| **All runs** | Every run | Which runs ran, and which one do I open? |
| **E2E run** | One GitHub Actions run | Did it pass, what failed, and where did its time go? |

The principles, the gaps and the roadmap are in [`observability.md`](observability.md).

## Filters

The four across-runs pages share four filters, and keep them when you move between them:

| Filter | Values |
|---|---|
| Window | 7, 14, 30 or 90 days. **vs before** compares with the same span before it, inside the 90-day archive. |
| Suite | All suites, VMaaS, CaaS, BMaaS. |
| Started by | Any trigger, pull requests, merge queue, scheduled (main), manual. |
| Repository | Both, `osac`, `osac-test-infra`: the repository whose CI started the run. |

A suite is its full-install workflows, its pull-request suite runs and its merge-queue rows:

| Suite | Workflows |
|---|---|
| VMaaS | E2E VMaaS, E2E VMaaS Full Install, E2E VMaaS PR suite, `E2E suite · vmaas · …` runs, E2E vmaas merge-queue rows |
| CaaS | E2E CaaS Full Install, E2E CaaS PR suite, `E2E suite · caas · …` runs, E2E caas merge-queue rows |
| BMaaS | E2E BMaaS Full Install, E2E BMaaS PR suite, `E2E suite · bmaas · …` runs, E2E bmaas merge-queue rows |

Netris, stretched, CaaS agentless-net, full regression and the named installs (**E2E VMaaS Production Install**, **E2E VMaaS Staging Install**, **E2E VMaaS Chart Packaging Smoke**, **OSAC CI Large**) test other scenarios and are never counted.

## Definitions

| Term | Definition |
|---|---|
| Pass rate | Passed ÷ (passed + failed). Cancelled runs are left out. GitHub's conclusion decides. |
| Time to result | First job created → last job completed, on passing runs. Includes the wait for a runner. Runs stored before job timelines were kept fall back to their stored duration. |
| Wait for a runner | The E2E job's `created_at` → `started_at`. Not measured for runs stored before job timelines were kept. |
| Phase | Every step of the three suites' workflows maps to one phase (`ci_observe_phases.py`, pinned by a test against the workflow files): queue, CI setup, cluster and hosts, build, install OSAC, tests, logs and teardown, other. |
| Cause | The phase of the first failed step decides; a blocked step (fork authorization, refused inputs) is **Blocked**; a timed-out conclusion or a cancelled step in a failed job is **Timed out**; no failed step at all is **Runner lost**. Causes roll up to five areas: Tests, Build, OSAC install, CI infrastructure, Blocked. |
| Flaky test | Passed and failed on the same commit in the window. |
| A test's time | From the trace: setup minus its waits, all `poll_until` waits, the call phase minus its waits (test actions), teardown minus its waits. The four add up to setup + call + teardown. A session or module fixture is charged to the first test that sets it up. |
| Percentiles | Nearest rank over the raw per-run values of the window. |
| Rebased | At least one `head_ref_force_pushed` event on the pull request. |
| E2E reruns | E2E runs of a pull request beyond the first of each suite. Not measured when the stores start after the pull request was opened. |

## Overview

| Section | Contents |
|---|---|
| Numbers | Pass rate and its change, typical and slow (p90) time to result, typical wait for a runner, share of failures caused by CI, runs, failed runs, runner hours, share of hours on failed runs, flaky tests, tests with a failure. |
| Suite health | One row per suite and all three: runs, pass rate, time to result and its change, runner wait, the top failure cause, links to the suite's time and failure pages. |
| Two bars | The average passing run by phase, failed runs by area. |
| Two lines | Pass rate per day, time to result per day. |
| Inside the tests | The tests' time split and what they wait for, from recorded runs. |
| Tests | The tests that fail most and the tests that take the most time. |
| Latest failed runs | Cause, failed step, run time, link to the run. |
| Pull requests | The window's pull request numbers. |

## Where the time goes

| Section | Contents |
|---|---|
| Numbers | Time to result (median, p90, mean), the runner wait, runner hours, share of hours on failed runs. |
| Phases | The average passing run by phase per suite, the phase table, phase time per day, the runner wait per day. |
| Steps | Every step of a passing run: median, p90, share of a run, change. |
| Inside the tests | The tests' time split, waits by resource kind, every test by total time, every wait signature, shared fixtures. |

## Why runs fail

| Section | Contents |
|---|---|
| Numbers | Failed runs, failure rate, and the failed runs per area; cancelled runs apart. |
| Causes | Per day by area, the cause table with its change, per suite. |
| Steps that failed | The step, its cause, how often, which suites, the newest such run. |
| Tests | Every failing test with fail rate, flaky commits and its last error; the flaky tests; error messages grouped after replacing names, ids and numbers. |
| Failed runs | Every failed run with its cause, failed step, first failed test and error. |

## Test history

| Section | Contents |
|---|---|
| Numbers | Runs, failure rate, typical and slow duration, change against the window before, flaky commits. |
| Charts | The duration of every run (green passed, red failed) with the daily median; passes and failures per day; where its time goes, on average and per day. |
| Tables | What it waits for, the fixtures it sets up, why it fails, every run. |

A test is keyed by its canonical name, `tests.e2e.vmaas.sanity.test_vm::TestVm::test_create`. The node id (`tests/e2e/vmaas/sanity/test_vm.py::TestVm::test_create`) and the JUnit id resolve to the same key.

## Pull requests

| Section | Contents |
|---|---|
| All pull requests | The window's medians (open to merge, wait for first review, review to approval, approval to queue, in the queue), ejections, commits, rebases, force pushes, changes requested, E2E runs, reruns and failed E2E runs; stage medians per week; every pull request. |
| One pull request | Identity line, the total and four long stages, the stacked stage bar, queue attempts with the ejection reason, every E2E run with the commit it tested, status. |

Authoring runs from opening to the first human review, review to the first approval, waiting to the merge queue, queue to the end of the last attempt. An **ejection** is a failed queue run, explained by the GitHub comment when one exists: GitHub keeps no queue history. An open pull request is read from GitHub when the page loads.

## All runs

One table of runs with repository, suite and result filters and a filter on every column. **Cause** is the cause Why runs fail counts. A run with no recording opens GitHub.

## E2E run

| Section | Contents |
|---|---|
| Summary | Result, tests run, test time, where the run's time went (one bar per suite), time to result, wait for a runner. |
| Failures | Failed tests, error messages, test time, the artifact. |
| Jobs and steps | Every job and step with its duration. A job name opens its suite's Where the time goes. |
| Test list | Every test with its setup, waiting, actions and teardown. |
| Run info | Run, pull request, workflow, commit and test paths, as links. |
| Cluster | Pods, CPU and memory by pod, node CPU and memory. |
| Waits, fixtures and the step timeline | Every wait and fixture setup of the trace; every step with its start after the first job was created. |

## Keys

A run is keyed by its GitHub run id; a recording id adds the suite and rerun number. A pull request is keyed by repository and number. A test is keyed by its canonical name. A capture id joins a run to its trace, span rollup, logs and cluster metrics.

## Data limits

- The run archive (suite runs and their job timelines), the test results, the span rollup and the pull request history are kept for 90 days (`CI_OBSERVE_KEEP_DAYS`).
- The workflow exporter keeps its own job history for 30 days. GitHub keeps the JUnit artifact for 30 days, so a run's tests are read within that window.
- Tempo keeps traces for 30 days; the span rollup reads a run once, 20 minutes after it finished.
- Prometheus and Loki keep cluster data for 30 days.

## Known gaps

No Kubernetes events, no per-test trace links, no service call summary, no CI SLO targets or alerts, no capture coverage target and no telemetry pipeline health. The product's own work is seen through the tests' waits, not from OSAC's controllers. [`observability.md`](observability.md) lists the impact and the planned order.

## Out of scope

Fleet health (runners, machines, queue infrastructure) belongs to the **CI Fleet** folder. Netris, stretched, full regression, unit and integration tests are not ci-observe.
