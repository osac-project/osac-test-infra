# Sources

Each question has one authoritative source.

| Question | Source | Kept |
|---|---|---|
| Did the run pass? | GitHub job conclusion in the workflow exporter, archived in `trends.db` (`suite_runs`) | 90 days |
| How long did it wait for a runner, and how long did each phase and step take? | GitHub job and step timestamps, stored by the workflow exporter (`timeline_json`) and archived in `trends.db` | 90 days |
| Why did it fail: tests, build, install or infrastructure? | The first failed step of the stored timeline, classified by `ci_observe_phases.py` | 90 days |
| Which tests failed, with what error, and how long did each test take? | JUnit artifact, read once per run into `trends.db` (`test_runs`) | 90 days |
| Is a test flaky? | `test_runs`: passed and failed on one commit | 90 days |
| Where does a test's own time go, and what does it wait for? | Tempo trace, rolled up once per run into `trends.db` (`test_spans`, `wait_spans`, `fixture_spans`) | 90 days |
| Which tests ran in one run, and their spans? | Tempo trace for the run | 30 days |
| How many pods, how much CPU and memory? | ci-observe Prometheus kubelet metrics | 30 days |
| Which run, pull request and commit is this? | Workflow exporter, joined by run id | 30 days |
| Which E2E runs did this pull request make? | Workflow exporter and the `trends.db` archive, joined by pull request number | 90 days |
| When was this pull request ready, reviewed, approved, queued, rebased? | `prs.db`; for an open pull request, GitHub read when the page loads | 90 days |
| Why did this pull request leave the queue? | Failed queue run plus the GitHub comment in `prs.db` | 90 days |
| What did the pod log? | GitHub artifact; Loki does not receive pod logs | 30 days |

## Rules

1. GitHub owns pass or fail. A recording explains a result but never overrides it.
2. A test report that is missing is missing data, not a pass.
3. A pull request that left the queue is inferred from its failed queue run and a GitHub comment. GitHub keeps no queue history.
4. Cluster graphs show the cluster under test, not the runner. A flat zero before the cluster is created is correct.
5. Every link is built from the stored repository, run id, pull request number or commit. Do not guess a URL from a workflow name.
6. A run is read once. The JUnit rollup and the span rollup record what they read, so an hourly pass costs only the new runs.
