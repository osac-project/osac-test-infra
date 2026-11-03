# Where each answer comes from

ci-observe reads several stores. Each question has one authoritative source. When two stores can answer, the table says which one wins.

The split is deliberate:

- **Overall** numbers come from the job and test populations that cover every CI run.
- **A single run** comes from its capture and its artifact.
- A capture never overrides the GitHub result. JUnit and the job conclusion decide pass or fail. Traces explain it.

## Sources

| Question | Authoritative source | Kept for | Scope |
|---|---|---|---|
| Did one run pass, and which tests failed? | GitHub job conclusion and the JUnit artifact, through the workflow exporter | 60 days job history, 7 days artifact | single run |
| What did one test do: spans, phases, waits? | Tempo capture | 30 days | single run, captured only |
| What did the services do? | Tempo, Beyla spans | 30 days | single run, captured only |
| What did the cluster do: pods, CPU, memory? | ci-observe Prometheus, labelled by capture id | 15 days | single run, captured only |
| What happened in the cluster: events? | Loki, Kubernetes events labelled by capture id | Loki image default | single run, captured only |
| What did a pod log? | The run artifact (GitHub) | 7 days | single run |
| How long did each job and step take? | Workflow exporter SQLite | 60 days | single run |
| Overall job pass rate, duration, MTTR | Central Prometheus `github_actions_*` and the exporter SQLite | 30 days metrics, 60 days SQLite | overall, every run |
| Overall per-test pass rate, flake and duration | Per-run JUnit artifacts, rolled up daily into `trends.db` by `ci_observe_trends.py` | as long as each day is rolled up while its 7-day artifact exists | overall, every run |
| The life of one pull request | Exporter `pr_merges` plus `pr_events` (not built yet) | 60 days | single PR |
| Merge-queue attempts and ejections | `merge_group` workflow runs, linked by squash trailers; ejections derived from a failed run and the PR comment | GitHub retention | single PR |
| Which hop dropped a connection? | Router access log (to be gathered) for the router hop; the adapter request log for the server hop | 7 days artifact | single run |
| Runner, machine and queue health | CI Fleet: central Prometheus and the runner exporters | 30 days | overall, fleet |

## Rules

1. **A rate names its population and its window.** "Pass rate, all jobs, 60 days" and "pass rate, captured runs, 30 days" are different numbers. They never share an axis.
2. **Flake comes from all jobs.** A failure without telemetry still counts. Per-test flake reads `trends.db`, the daily JUnit rollup, because the exporter stores job results, not test results. A day that was never rolled up while its artifact existed is a permanent gap, never a green zero.
3. **A single run's detail comes from its capture and artifact.** If the capture is missing, the artifact and the job history still answer what failed and when.
4. **The session span is a cross-check, not the verdict.** A missing session span means "incomplete", never "failed".
5. **The artifact is the only pod-log source today.** The collector is configured for `/var/log/pods` but cannot read it as an unprivileged user. When that changes, Loki becomes the source and the artifact stays the snapshot.
6. **The clock bridge is the capture's `started_at` and `ended_at`.** A link from a trend or a PR to a run carries that range.

## What each store is not

- Tempo is not the pass/fail record. It holds the run's own spans.
- Prometheus is not a test record. It holds the cluster at 60-second resolution.
- The exporter SQLite is not a per-test record. It holds jobs and steps; test outcomes come from JUnit.
- `trends.db` is not a run record. It holds daily counts per test and source SHA, with no run id and no spans.
- The GitHub artifact is not long-term history. It disappears after 7 days, so anything needed beyond that must be rolled up first.

## Gaps this table exposes

- **Per-test history across all jobs**: the daily rollup exists and `ci-observe-trends.timer` runs it, but the first run can only cover the 7 days GitHub still holds. Older history cannot be reconstructed.
- **PR events and ejections**: `pr_events` does not exist, and GitHub keeps no merge-queue history.
- **Router access logs**: not gathered, so the network hop has no recorded evidence.
- **Pod logs in Loki**: blocked by the collector's permissions.
