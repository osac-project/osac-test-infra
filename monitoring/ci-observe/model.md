# CI Observe model

CI Observe has three content pages, each answering one question. **Start here** lists recent runs and opens one.

| Page | Unit | Question |
|---|---|---|
| **E2E run** | One GitHub Actions run | Did it pass, what failed, and where did the time go? |
| **Pull requests** | One pull request | How long did review and the merge queue take? |
| **Trends** | One test over time | Is this test failing more often or getting slower? |

The principles behind the pages, the current gaps, and the roadmap are in [`observability.md`](observability.md).

## Start here

One table of recent E2E runs: Run, Job, Repo, PR, Branch, Trigger, Result, Recorded, Created and GitHub. Every identifier is a link. `No` under Recorded means the job sent no test data.

## E2E run

| Section | Contents |
|---|---|
| Summary | Result, tests run, and test time. Result comes from GitHub. |
| Failures | Failed tests, error messages, test time, and a link to the full artifact. |
| Test list | Every test, result, start time, and duration. |
| Run info | GitHub run, pull request, workflow, commit, and test paths, with links. |
| Cluster | Pods, CPU by pod, memory by pod, node CPU, and node memory for the run window. |

A run has detailed data only when its recording arrived. A missing recording does not change the GitHub result. A trace with no session end shows **Not finished** until the session span arrives.

## Pull requests

The page shows recent pull requests, typical and slow-end stage times, and the selected pull request.

An **ejection** means a pull request left the merge queue without merging. GitHub does not keep queue history, so the page derives it from a failed queue run and a GitHub comment.

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
