# CI Observe model

CI Observe has three content pages, each answering one question. **Start here** lists recent runs and opens one.

| Page | Unit | Question |
|---|---|---|
| **E2E run** | One GitHub Actions run | Did it pass, what failed, and where did the time go? |
| **Pull requests** | One pull request | How long did review and the merge queue take? |
| **Trends** | One test over time | Is this test failing more often or getting slower? |

The principles behind the pages, the current gaps, and the roadmap are in [`observability.md`](observability.md).

## Start here

One table of recent E2E runs: Run, Workflow, Repository, PR, Branch, Started by, Result, Recorded, Started and GitHub. Repository, Suite and Result filter the data; every column also has a filter. Every identifier is a link. `No` under Recorded means the job sent no test data; `Unknown` means the check could not judge, and a row with no recording opens GitHub.

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

The page reads GitHub test reports from E2E runs. A run without a readable report is missing data, not a pass. Test reports are kept for 30 days.

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
