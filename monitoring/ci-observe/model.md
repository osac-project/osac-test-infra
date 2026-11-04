# The ci-observe model

ci-observe answers questions about one unit of CI work, on that unit's timeline.

That sentence decides where every panel, query and dataset belongs. Without it, each new question becomes a new dashboard, and the result is a pile.

The readers are test owners, the monitoring owner and release managers. They ask different questions, and they share the same three units.

## What a dashboard is

The `ci-observe` folder holds three content dashboards: **Capture**, **Pull requests** and **Trends**. It also holds **Start here**, the entry page, which is a router, not a fourth dashboard.

A new question becomes a section on one of the three, not a fourth dashboard. Compare is a section of Trends, not a page of its own.

## Three units

| Unit | What it is | Key | Dashboard |
|---|---|---|---|
| **Capture** | One CI run with telemetry | capture id, for example `vmaas-main-41554302161-a1` | Capture |
| **Pull request** | One proposed change, from opened to merged or closed | pull request number | Pull requests |
| **Population** | Many CI runs of a test, a suite, a ref, or all jobs, over calendar time | test id, suite, ref, or all | Trends |

A run belongs to a **ref**. A ref is a branch or a pull request. `main` is a ref, and a pull request is a ref with a lifecycle. This keeps scheduled failures from becoming orphans: they belong to the `main` ref.

## Starting a task

The entry page takes a run id, a capture id, a pull request number, a test id, or nothing. Each input resolves to one dashboard in one step. A run id and a capture id open their capture; a pull request number opens Pull requests; a test id opens Trends.

| You have | Start at | It opens |
|---|---|---|
| A run id from Slack, for example `41554302161` | the entry page | that run's capture, or its row in the index marked `not collected` with a GitHub link |
| A capture id, for example `vmaas-main-41554302161-a1` | the entry page | that capture |
| A test id | the entry page | that test's history in Trends |
| A pull request number | the entry page | that pull request's stage funnel |
| A question about CI overall | Trends, Overview | pass rate, duration and failure mix |
| Nothing | the entry page | the run index, searchable by run id |

## The keys

Only three keys cross stores. Everything else is a fact owned by one store and reached by link.

| Key | Where it exists today | What is missing |
|---|---|---|
| capture id | Tempo, Loki, Prometheus | not stored in the exporter's SQLite |
| run id | SQLite `jobs.id`, every span as `osac.ci.run_id`, and inside the capture id | nothing: a trace joins its job row by `osac.ci.run_id` without parsing the capture id |
| PR number | SQLite `pr_display`, Loki and Prometheus `osac_ci_pr`, Tempo `osac.ci.pr` | queue runs carry no PR number at all |

The three SHAs are facts, not keys: source (`test_sha`, and the exporter's `head_sha` for the commit a job really ran), harness (`test_infra_sha`) and workflow (`test_infra_workflow_sha`). Suite has three different meanings in three stores and must not be a join key.

The exporter is the workflow-exporter service on the central machine. It polls GitHub and keeps job and step history in SQLite for 60 days.

## The sources

Each question has one authoritative store. [sources.md](sources.md) maps every question to its source, its retention and whether it covers a single run or the whole population. When two stores can answer, that table says which one wins.

## Where the code lives

`monitoring/` owns the central stack: the backend, the Grafana provisioning and the cluster manifests for the in-cluster collectors. Two CI hooks sit outside it:

- **The capture action**, `.github/actions/ci-observe-capture/action.yml`. A workflow calls a local action by path, so it lives beside the workflows that use it. It deploys the collectors from `monitoring/config/ci-observe/`, runs the test command, and verifies that telemetry reached the backend.
- **The gather script**, `scripts/gather-osac-logs.sh`, wrapped by `.github/actions/collect-artifacts/`. It runs on the runner against the live test cluster and collects the failure evidence that becomes the GitHub artifact.

Both run on the runner as part of a workflow or its failure path, not on the central machine. `monitoring/` still owns the configs they deploy and the backend they push to.

## The two populations

Trends reads two populations, and they are never drawn on one axis:

| Population | Source | Retention | Counts |
|---|---|---|---|
| All CI jobs | workflow exporter (SQLite) | 60 days | Every run, captured or not |
| Captured runs | Tempo, Prometheus, Loki | 30 days traces, 15 days metrics | Runs with telemetry, for detail |

A rate, count or duration names its population and its window in the panel title: `pass rate, all jobs, 60 days` or `pass rate, captured runs, 30 days`.

Captures are opt-in, so a per-test trend over captures describes the runs someone instrumented, not the test population.

## Capture: one CI run

The unit is one capture. The axis is time inside the run, bounded by the capture's `started_at` and `ended_at`. A capture states which layers arrived before it shows their contents.

| Layer | Data | Question |
|---|---|---|
| Verdict | passed, failed, cancelled, or not collected; collection coverage; then session span, failed tests, JUnit | Did it pass, what failed, and did we record it? |
| CI | job, steps, artifacts | Where did CI spend its time, and what did it gather? |
| Test | pytest spans, phases, waits, polls | What was the test doing? |
| Service | Beyla spans | What did the OSAC services do? |
| Environment | pod and node metrics, events, pod lifecycle | What was the cluster doing? |

The Capture page holds one section per layer: Run overview and Collection coverage for the verdict, CI evidence for the CI layer (GitHub jobs, steps, the JUnit result, the failed tests and the artifact link), Run identity for the SHAs and image refs, Tests and workers, Fixture work and Waits for the test layer, Service requests for the service layer, and Environment for the pod and node metrics. The Events row reads the Kubernetes events stored in Loki.

The moment of failure is one vertical marker on every chart. It is derived from the end of the failed span, not stored, so it is exact on trace panels and within one minute on cluster panels.

The CI evidence section carries the failed tests, the JUnit result and a link to the run's artifact. The artifact is the evidence bundle: the per-test `failure.json`, the pod logs and the cluster snapshots. GitHub deletes it after 7 days, so the page links to it rather than copying it. Pod logs exist only in that artifact: the collector cannot read `/var/log/pods`, so Loki holds no pod logs.

Runtime image IDs come from every Deployment in the observed namespace. A capture proves the running bytes when it lists an ID for every Deployment container and `osac_image_ids_complete` is `true`. Captures recorded before the namespace fix keep `unknown`; they are never rewritten.

A run that produced no capture still appears in the index with verdict `not collected` and a link to GitHub. Otherwise the front door is blind exactly when ingest is broken.

## Pull requests: the life of a change

The unit is one pull request. The axis is calendar time from open to merge or close.

| Stage | From | To | Data |
|---|---|---|---|
| Authoring | PR opened | first review | PR timeline, commits |
| Review | first review | approval | reviews, review comments |
| Waiting | approval | queue entry | labels, required checks |
| Queue | enqueued | merge or ejection | merge_group runs, queue events, ejection reasons |
| Merged | last attempt | merge | PR merge event |

`Enqueued` means the start of the pull request's first `merge_group` run. GitHub exposes no durable enqueue timestamp, so the run start is the only reproducible choice. Runner queue wait is a different thing: it belongs to Fleet and is linked, not folded into this stage.

An **ejection** is a pull request leaving the merge queue without merging. It is derived from a failed `merge_group` run plus the comment GitHub leaves on the PR.

For each stage: duration, the attempts inside it, and why an attempt was ejected. Every queue attempt links to its run. The link opens the Capture dashboard when a capture exists, and the GitHub job when it does not.

The dashboard opens on an index: recent pull requests with their last stage and age. Without it the page would be reachable only by already knowing the PR number.

The queue stage needs its own unit. One `merge_group` run can carry up to four pull requests, and a queue run has neither a PR number nor a capture. The run is the unit; the PR links come from the `gh-readonly-queue` branch name and from queue comments that name a run. A separate queue-workflow change would give a queue run its PR number and a capture, which means enabling `ci-observe` there.

## Trends: test history, compare, overview

| Section | Population | Question |
|---|---|---|
| Test history | all jobs for outcomes, captured runs for duration | Is this test flaky, slower, or newly broken? |
| Compare | a cohort of captured runs | Did these runs differ, and by how much? |
| Overview | all jobs for a suite or a ref | What is the pass rate, duration and failure mix? |

The cohort is chosen in the Compare section, and its tables rank tests by observed test-span time and list what failed.

**Flaky** means the same test passed and failed on the same source SHA. Test history counts distinct source SHAs with both outcomes over 60 days, from all jobs. Reruns of the same SHA do not add to that count. **Slower** is a duration trend over the same window, from captured runs. **Newly broken** is consecutive failures on the most recent SHAs with no pass between them.

## Compare is an action in Trends

Compare is a section of Trends, not a dashboard. It is an action on a capture (`compare with its baseline`), on a pull request (`compare this attempt with the last green one`) and on a test row (`compare the last two runs`).

The **baseline** is the most recent cohort with the same source, harness and workflow SHAs and at least two captures. When none exists, the baseline is the last three captures of the same suite before this one. The action seeds the cohort from the unit's own identity fields, so no one types SHAs by hand, and lands the reader in the Compare section of Trends.

## Entry and navigation

The entry page is the router described under [Starting a task](#starting-a-task). Every link between dashboards carries the unit and the time range, so the reader never loses their place:

- Capture to its ref, and to `compare with baseline`.
- Capture back to the entry page with the run's time range, not "now".
- Pull request to its attempts, and each attempt to its run or its GitHub job.
- Trends to the runs behind a point, and back to the trend that sent them.

The clock bridge is the capture's `started_at` and `ended_at`. It is the only mapping between time inside a run and calendar time.

## Missing and partial telemetry

These are normal states, not errors:

- A run with no capture: shown as `not collected`, links to GitHub.
- A capture missing a layer: its coverage strip says which layers arrived.
- A cancelled run: its own verdict, not a failure.
- Tempo lag for a fresh capture: shown as "not indexed yet", not "missing".
- A pull request whose queue history predates the data: shown as "no queue history", not "no ejections".

## Attempts, pushes and reruns

Three different things, and each has a key:

| Event | Key | New capture? | Counts toward flake? |
|---|---|---|---|
| A push | new source SHA | yes, a new run | yes, a new SHA |
| A queue attempt | new workflow run | yes, a new run | yes, if it has a new SHA |
| A rerun | same run id, new attempt | yes, a new capture, `-a2` | no, same SHA |

## The rule

A panel belongs to exactly one dashboard, names its unit and its population, and sits on that unit's timeline or time range.

If a panel cannot name its unit and its axis, it is not ready to be built.

## The pile test

Before adding a panel or a dataset, answer four questions:

1. Which unit is this about?
2. What is its time axis?
3. Which dashboard does it join?
4. Does it link to the other units?

Four answers, or it is not admitted.

## What is out of scope

Fleet health: runners, machines, queue infrastructure and exporter health. It is a different product with a different unit. It stays in its own Grafana folder, `CI Fleet`, and may be linked from ci-observe. Runner queue wait stays in Fleet; the Pull requests dashboard links to it rather than copying it.

In scope, and easily confused with it: the merge queue of a pull request. That is a stage of the Pull requests dashboard.

## Constraints the model must respect

- **Pod logs are not delivered to Loki.** The collector is configured for `/var/log/pods`, but the unprivileged UID cannot read it, so pod logs exist only in the 7-day artifact.
- **Runtime image IDs are `unknown` in current captures.** The requested image refs are recorded; the running image bytes are not. A capture that lists an ID for every OSAC Deployment container with `osac_image_ids_complete=true` is what proves the path works.
- **GitHub keeps no merge-queue history.** An ejected entry disappears. An ejection is derived from a failed `merge_group` run plus the pull request comment. Exact events need a webhook receiver.
- **Tempo keeps 30 days and rejects queries over 7 days.** A 30-day per-test trend needs a daily rollup of counts and percentiles, not a longer query.
- **The exporter is a 90-second poller with a 30-PR window.** PR history needs a daily backfill with a sentinel, not re-polling every cycle.
- **Label and review-request history exists only in the PR timeline.** The PR object carries current state only.

## How new data is admitted

A new dataset enters only if it joins a unit by an existing key, or by a key that the same change introduces on both sides.

- Adapter request logs join a capture by capture id and a time.
- Router logs join a capture by capture id and a time, if the gather step stamps them.
- Pod restart history joins a capture by capture id and a time.
- Kubernetes events join a capture by capture id and a time.
- PR reviews join a pull request by PR number.
- Queue events join a pull request by PR number and a workflow run.

Data that only describes the fleet is not admitted.

## What exists, and what is next

1. The Grafana folder is split: `CI Fleet` and `ci-observe`.
2. The entry page routes a run id, a capture id, a pull request number or a test id, with the run index behind an empty input.
3. Capture is one page: one section per layer, one timeline, no new data.

Next: runtime image IDs that resolve in a live capture, router access logs when the ingress controller writes them to Container, and the queue-workflow change that gives a `merge_group` run its PR number and capture.
