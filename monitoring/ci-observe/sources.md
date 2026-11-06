# Sources

Each question has one authoritative source.

| Question | Source | Kept |
|---|---|---|
| Did the run pass? | GitHub job conclusion in the workflow exporter | 30 days |
| Which tests failed, and with what error? | JUnit artifact, parsed on demand | 30 days |
| Which tests ran, and how long did each take? | Tempo trace for the run | 30 days |
| How many pods, how much CPU and memory? | ci-observe Prometheus kubelet metrics | 30 days |
| Which run, pull request and commit is this? | Workflow exporter, joined by run id | 30 days |
| How much did each job and step take? | Workflow exporter job history | 30 days |
| Which E2E runs did this pull request make? | Workflow exporter job history, joined by pull request number | 30 days |
| When was this pull request approved or queued? | `prs.db`; for an open pull request, GitHub read when the page loads | 30 days |
| Why did this pull request leave the queue? | Failed queue run plus the GitHub comment in `prs.db` | 30 days |
| Is a test failing more often or getting slower? | Daily JUnit rollup plus the raw per-run rows in `trends.db` | 30 days |
| Is a job failing more often or getting slower? | Workflow exporter job history, grouped by repository and display name | 30 days |
| How long did a pull request spend in review and the queue? | `prs.db` | 30 days |
| What did the pod log? | GitHub artifact; Loki does not receive pod logs | 30 days |

## Rules

1. GitHub owns pass or fail. A recording explains a result but never overrides it.
2. A test report that is missing is missing data, not a pass.
3. A pull request that left the queue is inferred from its failed queue run and a GitHub comment. GitHub keeps no queue history.
4. Cluster graphs show the cluster under test, not the runner. A flat zero before the cluster is created is correct.
5. Every link is built from the stored repository, run id, pull request number or commit. Do not guess a URL from a workflow name.
