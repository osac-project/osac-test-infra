# Sources

Each question has one source.

| Question | Source | Kept |
|---|---|---|
| Did the run pass? Which tests failed? | GitHub job result, the test report, and the saved artifact | 30 days |
| Which tests ran, and how long did each take? | Tempo trace for the run | 30 days |
| Is a test failing more often or getting slower? | Daily JUnit rollup in `trends.db` | 30 days |
| How long did a pull request spend in review and the queue? | `prs.db` | 30 days |
| Which code, workflow, and pull request did a run use? | workflow exporter SQLite | 30 days |
| What did the pod log? | GitHub artifact; Loki does not receive pod logs | 30 days |

## Rules

1. GitHub owns pass or fail. A recording explains a result but never overrides it.
2. A test report that is missing is missing data, not a pass.
3. A pull request that left the queue is inferred from its failed queue run and a GitHub comment. GitHub keeps no queue history.
