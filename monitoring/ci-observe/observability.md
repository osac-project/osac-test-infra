# CI Observe observability model

This page explains why CI Observe collects what it collects, what exists today, and what is still missing.

The short version: one run, one trace, every test and cluster signal attached to it, and pages that answer one question at a time.

## Why this shape

Observability lets you ask a new question without adding new instrumentation. Monitoring answers questions you predicted in advance. CI Observe aims at the first.

The design follows a small set of rules:

| Rule | What it means here |
|---|---|
| Keep wide events, not narrow metrics | One run and one test carry many attributes. Test name, commit and PR are high cardinality, so they belong in traces and tables, not metric labels. |
| Use traces as the backbone | A run trace connects the session, tests, setup, waits and service calls. |
| Use shared names | OpenTelemetry defines CI/CD and test attribute names. They make data joinable across tools and years. |
| Keep failures, sample successes | A failed or slow run is always worth keeping. A healthy run is not always worth keeping. |
| Show symptoms first | Pass rate and feedback time first. Pods, waits and service calls come after a drill-down. |
| Use targets | Pass rate, feedback time, flake rate and queue time need SLOs to become decisions. |
| Make the page clear | Overview first, zoom and filter, details on demand. One question per page. |

## The unit: one run, one trace

```
run 42163948768
├── queue and provision
├── build and install
├── test session
│   ├── test_cluster_create
│   │   ├── setup: cluster, users, CLI
│   │   ├── wait: ComputeInstance Ready
│   │   └── call: POST /clusters
│   └── test_cluster_delete
├── teardown
└── artifact upload
```

Every span should carry the same identity. The table shows the shared name first and the field we use today second.

| Shared name | Field today | Use |
|---|---|---|
| `cicd.pipeline.name` | `workflow` | Name of the workflow |
| `cicd.pipeline.run.id` | `run_id` | GitHub run id |
| `cicd.pipeline.run.url.full` | `run_url` | Link to the run |
| `cicd.pipeline.result` | GitHub `conclusion` | Pass, fail, cancel, timeout |
| `cicd.pipeline.task.name` | job and step names | Not yet spans |
| `test.case.name` | `tests[].name` | Test id |
| `test.case.result.status` | `tests[].status` | Pass or fail |
| `test.suite.run.status` | `session.status` | Session result |
| `vcs.repository.url.full` | `repo` | Repository |
| `vcs.ref.head.revision` | `test_sha` | Commit under test |
| `osac.ci.capture_id` | `capture_id` | Joins Tempo, Prometheus and Loki |

The custom `osac.ci.*` names work. New signals should emit the shared name next to them, then dashboards can move over without breaking history.

## What we record today

| Signal | Source | Where it appears |
|---|---|---|
| Run identity, result, trigger, branch, PR | Workflow exporter SQLite, archived in `trends.db` | Every page |
| Runner wait, job and step start and end | GitHub job timeline stored by the workflow exporter | Where the time goes, E2E run |
| Phase of every step, cause of every failure | `ci_observe_phases.py` over the stored timeline | Overview, Where the time goes, Why runs fail |
| Test results, durations and error messages | JUnit artifact, read once into `trends.db` | Why runs fail, Test history |
| Flaky tests | `trends.db`: passed and failed on one commit | Overview, Why runs fail, Test history |
| A test's setup, waits, actions and teardown | Tempo trace, rolled up once into `trends.db` | Where the time goes, Test history, E2E run |
| What the tests wait for in the product | `poll_until` wait spans, grouped by signature | Where the time goes, Test history, E2E run |
| Test list, failures, error messages of one run | Tempo trace and JUnit artifact | E2E run |
| Cluster pods, CPU, memory, nodes | ci-observe Prometheus | Cluster row |
| Pull request stages, rebases, reviews, queue time | `prs.db` | Pull requests, Overview |
| Every E2E run of a pull request, one code version per commit | Workflow exporter and the `trends.db` archive | Pull requests |
| Pod logs and full failure detail | GitHub artifact | Failures link |

The archive keeps 90 days; traces, cluster data and the artifact keep 30. Tempo accepts a search window of 7 days at a time.

## What is still missing

| Gap | Why it matters |
|---|---|
| Custom attribute names | Data is harder to join with other CI tools and future telemetry. |
| No Kubernetes events on the run page | A pod that was evicted or OOMKilled is not explained. |
| No per-test trace and log links | You cannot jump from a failing test row to its spans and logs. |
| No service call table | API errors and slow calls are not summarized. Beyla samples service spans at 10%. |
| Product time only through waits | The pages see how long a test waited for OSAC, not which controller step took it. |
| No SLO targets or alerts | Pass rate, time to result, flake rate and runner wait are shown, but have no targets. |
| No capture coverage target | Missing recordings are visible but not measured. |
| No telemetry pipeline health | Dropped spans or ingest lag are not shown. |
| No sampling policy | Volume is low today, but there is no rule for when it grows. |
| Runs before job timelines | Runs stored before the exporter kept job timelines have no runner wait and no step start times; their phases come from step durations. |

## Roadmap

**Done**

- Run timeline: the runner wait and every phase of every run (Where the time goes, E2E run).
- Waits table and the per-test time split (Where the time goes, Test history, E2E run).
- Flake view: flaky tests by commit (Why runs fail, Test history).
- Failure causes by first failed phase (Why runs fail).
- Rollups beyond 30 days: a 90-day archive.

**P0, traceability**

1. Emit OpenTelemetry CI/CD and test names next to the current fields.
2. Link every test row to its trace and logs.
3. Keep GitHub as the only source of pass or fail.

**P1, explanation**

4. Add Kubernetes events as a collapsed row.
5. Add spans around the OSAC CLI and API calls the tests make, so test actions split further.
6. Add capture coverage and ingest health.

**P2, decisions**

7. Set CI SLO targets on pass rate, time to result, flake rate and runner wait, with error budget alerts.
8. Add outcome-based sampling.

## Rules we keep

1. GitHub owns pass or fail. A recording explains a result, it never changes it.
2. Missing data is missing, never zero.
3. One page answers one question.
4. Identifiers are links, not raw text.
5. A panel names its population, its time range and its unit.
6. Do not add a panel that cannot change a decision.

## Sources

- OpenTelemetry CI/CD spans: https://opentelemetry.io/docs/specs/semconv/cicd/cicd-spans/
- OpenTelemetry CI/CD metrics: https://opentelemetry.io/docs/specs/semconv/cicd/cicd-metrics/
- OpenTelemetry CI/CD resources: https://opentelemetry.io/docs/specs/semconv/resource/cicd/
- OpenTelemetry test attributes: https://opentelemetry.io/docs/specs/semconv/registry/attributes/test/
- OpenTelemetry sampling: https://opentelemetry.io/docs/concepts/sampling/
- Google SRE, monitoring distributed systems: https://sre.google/sre-book/monitoring-distributed-systems/
- Google SRE workbook, implementing SLOs: https://sre.google/workbook/implementing-slos/
- Grafana dashboard best practices: https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/best-practices/
- Honeycomb, what is observability: https://www.honeycomb.io/blog/what-is-observability-key-components-best-practices
- Shneiderman, the visual information-seeking mantra: https://www.cs.umd.edu/~ben/papers/Shneiderman1996eyes.pdf
- CNCF, OpenTelemetry CI/CD observability: https://www.cncf.io/blog/2024/11/04/opentelemetry-is-expanding-into-ci-cd-observability/
