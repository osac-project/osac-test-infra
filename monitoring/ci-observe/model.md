# The ci-observe model

ci-observe explains what happened to one unit of CI work, on that unit's timeline.

That sentence decides where every panel, query and dataset belongs. Without it, each new question becomes a new dashboard, and the result is a pile.

## Three units, three surfaces

CI work has three units, and each needs its own view.

| Unit | What it is | Key | Surface |
|---|---|---|---|
| **Run** | One CI run with telemetry, a capture | capture id | **Run** |
| **Change** | One proposed change, from opened to merged or closed | pull request number, or commit SHA | **Change** |
| **Population** | Many runs of a test, a suite or a cohort, over calendar time | test id, suite, cohort tuple | **Trends** |

Three surfaces, no more. A new question becomes a section on one of them, not a new dashboard.

## How the units link

```
Change (PR)  ── produces ──▶  Run (capture)  ── sampled by ──▶  Trends
     ▲                            │
     └──────── links back ────────┘
```

- A run belongs to a change when it ran on that PR. A run on `main` belongs to no change.
- A change has many runs: each push, each queue attempt, each retest.
- Trends read the same runs, grouped by test or cohort.
- Every link works in both directions. From a trend point you can reach the runs; from a run you can reach the change; from a change you can reach every attempt.

The join keys exist already: capture id, source SHA, harness SHA, workflow SHA, PR number, test id, suite.

## The one rule

> A panel belongs to exactly one surface, names its unit, and sits on that unit's timeline.

If a panel cannot name its unit and its time axis, it is not ready to be built.

## Run: one CI run

The unit is a capture. The axis is time inside the run. Five layers, all on the same clock.

| Layer | Data | Question |
|---|---|---|
| Verdict | conclusion, session span, failed tests, JUnit | Did it pass, and what failed? |
| CI | job, steps, artifacts | Where did CI spend its time, and what did it gather? |
| Test | pytest spans, phases, waits, polls | What was the test doing? |
| Service | Beyla spans | What did the OSAC services do? |
| Environment | pod and node metrics, events, pod lifecycle | What was the cluster doing? |

The failure second is one vertical marker on every chart. Evidence links to the artifact live in the CI layer, next to the failure that needs them.

## Change: the life of a pull request

The unit is a pull request. The axis is calendar time from open to merge or close. The surface answers "where did this change wait, and why".

| Stage | From | To | Data |
|---|---|---|---|
| Authoring | PR opened | first review | PR timeline, commits |
| Review | first review | approval | reviews, review comments |
| Waiting | approval | merge queue entry | labels, required checks, queue events |
| Queue | enqueued | merge or ejection | merge_group runs, queue events, ejection reasons |
| Merged | last queue attempt | merge | PR merge event |

For each stage: duration, and the runs that happened inside it. Every queue attempt links to its run and capture, so a failed attempt opens the Run surface at that capture.

This surface needs data ci-observe does not collect yet: PR events, reviews, commits, and merge-queue events. GitHub provides all of it. The workflow exporter is the natural place to store it, because it already polls GitHub and keeps job history.

## Trends: comparisons, trends, overall

The unit is a population: a test, a suite, a cohort, or the whole fleet of runs. The axis is calendar time.

| Section | Unit | Question |
|---|---|---|
| Compare | a cohort of captures | Did these runs differ, and by how much? |
| Test history | one test id | Is this test flaky, slower, or newly broken? |
| Overview | a suite or all runs | What is the pass rate, duration and failure mix? |

Trends never show a single run's detail. They show a distribution and link to representative runs.

## What is out of scope

Fleet health, meaning runners, machines, queues and exporter health, is a different product with a different unit. It stays in its own Grafana folder, `CI Fleet`. It may link to ci-observe, but it does not share a surface.

## The pile test

Before adding a panel or a dataset, ask:

1. Which unit is this about?
2. What is its time axis?
3. Which surface does it join?
4. Does it link to the other units?

Four answers, or it is not admitted.

## How new data is admitted

A new dataset enters only if it joins a unit by an existing key, or by a key that the same change introduces on both sides.

- Adapter request logs join a run by capture id and a time.
- Router logs join a run by capture id and a time, if the gather step stamps them.
- Pod restart history joins a run by capture id and a time.
- PR reviews join a change by PR number.

Data that only describes the fleet is not admitted.

## The first three things to build

1. Split the Grafana folder: `CI Fleet` and `ci-observe`.
2. Make the ci-observe entry page the run index, one row per capture.
3. Rebuild Run as one page from the panels that exist today: five sections, one timeline, no new data.

Then add the missing layers one at a time, and build Change once the PR data exists.
