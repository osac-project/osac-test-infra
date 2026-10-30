# CI observability contract

Version 1 of the canonical CI observability data contract. Producers (test
plugins, workflow wiring, format adapters) and consumers (ingestion,
transport validation, dashboards) code against the schemas, the validators,
and the sanitized examples in this directory, so their work can proceed in
parallel against one frozen definition. The job inventory that lists what
produces test results lives one level up, in
[`monitoring/ci_observability/CI-INVENTORY.md`](../CI-INVENTORY.md).

## Status and compatibility

Version 1 is frozen for producer and consumer implementation. It is not
deployed in CI. The canonical pytest producer, native adapters, workflow
wiring, transport and cluster observer remain separate work packages.
Fixtures are sanitized captures and tiny local runs, not deployed CI evidence.

Consumers pin a checkout and run these compatibility tests. Runtime
collection never fetches schemas or code. New fields, events or enum
values require a new schema version and a consumer upgrade. Version 1
documents remain supported; its validators reject unknown properties.

## Layout

| Path | Contents |
| --- | --- |
| `v1/common.schema.json` | Shared definitions: timestamps, identifiers, outcomes, suites, job identity, resources, failures. |
| `v1/bundle-manifest.schema.json` | One manifest per delivered bundle. |
| `v1/journal-record.schema.json` | One JSON Lines record per journal line. |
| `validation.py` | The strict validators: `validate_manifest`, `validate_record`, `validate_bundle`. |
| `derivation.py` | The reference attempt-outcome derivation from phase outcomes. |
| `v1/examples/` | Sanitized example bundles and native report fixtures with recorded provenance. |

## Bundles

A bundle is the unit of delivery: everything one job execution produced.
It is a directory with a `bundle-manifest.json` at its root. The manifest
lists every other file with a SHA-256 checksum, a size, and a kind.
Journals also carry a record count and a completeness flag. The manifest
never lists itself. Paths are unique and canonical: no double dots, no dot
segments, no double slashes, no backslashes, no leading slash.

Readers open every path component with `O_NOFOLLOW` and require regular
files with a single link. Symlinks, hard-linked aliases, FIFOs and devices
are rejected before reading. Journals are parsed only after every
file-level check passes. Hashes, sizes and record counts use explicit
byte caps, and a file that grows during the scan is rejected. Boundary
limits are contract values, not silent defaults: at most 128 files, 32 MiB
per file (a real Fulfillment integration Ginkgo report measures about
15.8 MiB), 256 MiB total, a 1 MiB manifest, 200,000 records per journal,
2,000,000 records total, a 1 MiB record line, 4096 directory entries, and
a depth of 16.

A bundle contains:

- One or more journals. Each worker process writes its own append-only
  JSON Lines journal, named `{journal_id}.jsonl`; the controller or main
  process writes `controller.jsonl` or `main.jsonl`. Sequence numbers are
  per journal and contiguous from 1. Blank lines are violations, not
  silently dropped lines. Duplicate JSON keys and non-finite numbers are
  rejected rather than normalized.
- Native reports with outcomes and timing fields preserved. Redaction
  replaces private text before delivery.
- Whatever else the producer declares, listed with checksums.

The examples under `v1/examples/` are all valid bundles: `serial` (single
process), `parallel` (two xdist workers), `rerun` (attempt 2 of a failed
run, with an in-job retry), `campaign` (one campaign job with in-job
retries at a fixed commit), `partial` (an interrupted run with dangling starts),
`unit-ginkgo` (an adapter conversion of two real Ginkgo runs, two native
invocations in one job), `unit-build-fail` (a real go test build failure
that completed before any collection), and `mixed-aap` (one job running a
pytest invocation and an Ansible invocation). `v1/examples/native/` holds
the native report fixtures; the README beside them records how each was
produced.

## Identity and trust

GitHub owns the authoritative CI conclusions. Everything in a bundle is a
producer claim, untrusted until reconciliation. Ingestion performs the
authoritative job binding outside the artifact payload, using its own
GitHub credentials and the run, attempt, and job APIs. The steps:

1. Resolve the run by `repository_id` and `run_id`, then the attempt by
   `run_attempt`.
2. Bind the job. `github_job_id` is GitHub's numeric job database id. When
   the producer did not know it, the manifest carries `null` and the
    logical key can suggest candidates among that attempt's jobs.
    Trusted central mappings or app metadata establish verified binding.
   The `GITHUB_JOB` environment variable is a logical string, never a
   numeric id: it must never be written into `github_job_id` to fill the
   gap.
3. Record the GitHub conclusion as the authoritative result next to the
   producer's claimed outcome. A claim cannot alter another run or job.

`logical_job_key` identifies a job within a workflow independently of the
attempt: `{workflow file name}#{job key}`, plus a matrix discriminator
when needed. It is never a display name. GitHub mints new numeric job ids
on every attempt, so only the logical key joins attempts.

`execution_id` is an opaque unique id minted by the producer at execution
start, recommended as a UUID. It is independent of the numeric job id:
each matrix leg and each called job gets its own, and trusted
reconciliation stores the mapping to run, attempt, and job beside it
without ever rewriting a journal's identity. Native invocations in one job
share its execution id and use separate invocation ids. The manifest's job identity
and suite must be echoed by the single `execution_started` record; the
validators check the echo.

When a failed run is re-run, GitHub re-executes only the failed jobs. A
job that was not re-run has no bundle at all. Trusted reconciliation
records its outcome as `inherited` from the earlier attempt; a producer
journal can never claim `inherited`, and an inherited success never counts
as a new execution.

The workflow `event` is the raw GitHub event name, from a closed enum of
the triggers the inventoried jobs actually use (`pull_request`,
`pull_request_target`, `pull_request_review`, `push`, `merge_group`,
`schedule`, `workflow_dispatch`, `workflow_run`, `issue_comment`,
`issues`, `check_run`, `check_suite`, `release`, `repository_dispatch`).
An unknown event fails validation; it is never silently reinterpreted as
periodic. The reporting `cohort` (`pull-request`, `merge-queue`,
`scheduled`, `nightly`, `manual`, `flake-campaign`) is a separate field,
because one GitHub event can serve several cohorts.

## Environment by source role

The manifest lists every checked-out source with a role, a repository,
and a revision: `tested`, `workflow`, `test-suite`, `installer`,
`infrastructure`, or `orchestration`. The `tested` and `workflow` roles
are required; every role may appear once. The same repository can appear
in several roles at different revisions, which is exactly what happens
when a floating `main` workflow calls a pinned installer. A revision is
`null` when it was never resolved, for example on an early failure before
checkout; a dummy SHA must never stand in for unknown.

## Invocations: one job, several frameworks

One job can run several native processes: AAP runs pytest unit tests and
then Ansible integration targets; operator jobs run go test and Ginkgo.
Each invocation opens with `invocation_started`, carrying its
`invocation_id` (globally unique), its actual `framework`, `tier`,
`component`, `variant`, `selection`, and its real `dependencies` (kind,
postgres, envtest; a job named unit may use a real database, and its
invocation says so), and closes with `invocation_finished`, carrying the
child process exit outcome. The job-level `suite` stays the declared
cohort: with mixed frameworks it uses framework `mixed`, and a mixed
suite without invocation records is rejected. Every invocation record
must name an actual framework; `mixed` is reserved for the job level.

Every semantic map in the validators is keyed by invocation as well as
id, so two invocations can share test names, fixture ids, and operation
ids without merging. A `test_ref` that crosses invocations carries the
target's `invocation_id`; parentage is never inferred from time or
journal placement.

## Journals

Every journal line is one record with this envelope:

| Field | Meaning |
| --- | --- |
| `schema`, `schema_version` | Self-description; both are constants in v1. |
| `execution_id` | Must equal the manifest's execution id. |
| `journal_id` | The journal file this record belongs to. |
| `invocation_id` | The native process invocation this record belongs to. |
| `sequence` | Per-journal counter, contiguous from 1. |
| `recorded_at` | UTC wall-clock time the record was written. Ends in `Z` and is a real calendar date-time; naive stamps, offsets, and impossible dates are rejected. |
| `source_at` | UTC time of the event according to its source, for records converted from a native report. Absent for live records. |
| `mono_ns` | Monotonic clock of the writing process, or `null` when unknown. Native reports carry no monotonic clock: converted records use `null` plus `duration_ns`, never a fabricated clock. Clock domains never merge by default. |
| `clock_id` | Identity of the monotonic clock domain. One journal carries one clock. |
| `clock_uncertainty_ns` | Bound on time uncertainty for cross-host events. |
| `event`, `data` | The event type and its payload. |

The events:

| Event | Payload carries |
| --- | --- |
| `execution_started` | Job identity and suite; must equal the manifest's. |
| `execution_finished` | Producer outcome, exit code (`null` when the process was lost), first failure, duration. |
| `invocation_started` / `invocation_finished` | Invocation id, actual framework context, dependencies; child exit outcome. |
| `worker_started` / `worker_stopped` | Worker id, host, pid; stop outcome `completed`, `interrupted`, or `unknown`. |
| `collection_finished` | Collection outcome (`completed`, `error`, `interrupted`, `unknown`) and, when completed, the counts. |
| `test_collected` | One collected test id and whether selection deselected it. |
| `test_phase_started` / `test_phase_finished` | Test id, attempt index, phase, worker, phase outcome, failure, duration. Live instrumentation only. |
| `test_result` | Native result: `result_scope` attempt or aggregate, attempt index and count (null when unknown), final outcome, optional worker and native duration, optional source start and end. |
| `fixture_started` / `fixture_finished` | Fixture instance, fixture id, scope, worker, work phase, outcome, duration. |
| `fixture_used` | Edge from a fixture instance to one consuming test attempt. |
| `operation_started` / `operation_progress` / `operation_finished` | Operation id, allowlisted name, expected condition, deadline, resource, explicit test link; progress carries elapsed time and a safe observed summary. |
| `resource_observed` | Allowlisted resource identity, metadata, and status. |
| `collection_state` | Source, status, covered interval, dropped-record count, errors, omitted fields. Emitted as a periodic heartbeat and once terminally. |

Starts are recorded immediately, so an interruption leaves useful partial
evidence: a phase, fixture, or operation with no finish record keeps its
last observed state and counts as `interrupted` or `unknown`. A missing
terminal event stays an explicit unknown; it is never inferred as success.

## Test phases, native results, and outcomes

Setup, call, and teardown are separate phases with separate outcomes,
preserved exactly as the native framework produced them. Real frameworks
put outcomes in places a naive model would not expect: pytest can raise
an expected failure or a skip during setup or even teardown, and a
teardown can time out. The schema allows every native outcome in every
phase and forbids only the values that are derived, never observed:
`not_run` and `inherited`.

Native aggregate reports (JUnit, Vitest, Ginkgo) cannot truthfully emit
setup/call/teardown boundaries or per-retry intervals, so adapters emit
`test_result` records instead of fabricating phases: `result_scope`
distinguishes a per-attempt result from the aggregate final result; the
aggregate carries the native attempt count (for example Ginkgo
`NumAttempts`) and the total duration; each attempt record carries its
own measured span. An aggregate duration is never charged to the final
retry, and a null retry count, worker, or time stays unknown. A test
attempt must not be represented both by phase records and by a native
attempt result; the validators reject the double count, and cross-check
attempt indexes against the declared aggregate count.

`attempt_index` counts retries of one `test_id` within one invocation.
Campaign repetitions belong to separate job executions, joined by their
campaign id, group and repetition number.

The canonical outcome space, shared by tests, phases, fixtures,
operations, and executions: `passed`, `failed`, `error`, `timeout`,
`cancelled`, `skipped`, `expected_failure`, `unexpected_pass`, `not_run`,
`inherited`, `interrupted`, `unknown`. Skipped, cancelled, and inherited
stay distinct: skipped is a selection decision, cancelled is an external
stop, inherited is a rerun result assigned only by reconciliation.

`derivation.py` is the reference derivation of an attempt outcome from
its phase outcomes. Explicit interruptions stay interrupted; unknown
endings stay unknown. Neither erases an already observed failure.
A setup skip or expected failure prevents the body from running. A
failing teardown fails an otherwise passing or skipped attempt, even
without failure details. Empty or missing phase evidence is unknown;
not-run classification needs collection and invocation evidence.
Cleanup never replaces the first failure. Consumers must preserve these rules.

## Collection: capture completed is not evidence available

`collection_finished` carries an `outcome`. A completed collection with
`collected_count` 0 is a measured empty selection, not a failure. On a
collection error the outcome is `error` and the counts stay null rather
than being guessed. A bundle may be complete with no collection record at
all, for example a build failure before any test collection (the
`unit-build-fail` example), and nothing may be fabricated to fill the
gap. Count and membership checks run only for completed collections:
executed tests must be collected and not deselected. Selected tests that
never ran are legitimate (fail-fast, runtime deselection) and are not
evidence loss; a native selection that is unknown (JUnit-only adapters)
is stated by the absence of collection records, not by invented ones.

## Fixtures

A fixture instance is one setup-to-teardown lifetime, identified by
`fixture_instance_id`, so two parameterized instances of one fixture stay
separate (they also carry distinct `params_fingerprint` hashes; raw
parameter values are never recorded). Setup work and finalizer work are
separate phase intervals, each measured on its own; the span between them
is the instance lifetime, including idle time while tests consume it.
`fixture_used` edges link the instance to each consuming test attempt, so
shared cost is recorded once and charged once. Every record of one
instance must agree on its identity: fixture id, scope, worker, and
parameter fingerprint echo each other or the bundle is rejected.

## Operations and resources

Operations are bounded waits and tracked service calls. `name` is an
allowlisted helper name, never a command line. An operation links
explicitly to the test attempt that caused it through its `test` field;
`parent_operation_id` is the only dependency link between operations, it
must resolve within the invocation, and cycles are rejected. When an
operation finish repeats the resource of its start, the two must agree.
`operation_progress` records elapsed time and a bounded safe summary.

`resource_observed` records carry only allowlisted identity, metadata,
and status: resource identity (kind, UID, name, apiVersion, cluster UID
and role, namespace, owner chain UIDs, tenant annotation value,
fulfillment API id), generation, finalizers, readiness, restart count, a
status field, conditions, a bounded per-container list (name, init,
ready, restarts, state, reason, exit code, timestamps), controller
replica status, and warning-event classification (reason, type, involved
UID, time, count). Cluster identity is required for Kubernetes
observations. Specs, labels, annotations, IP, MAC, and network data,
Secret and ConfigMap bodies, and log payloads are not representable in
the schema; details the observer could not read stay absent, never
guessed. A deleted observation carries `deleted=true` rather than
disappearing.

## Missing data versus zero

The contract separates "not measured" from "measured as zero":

- `github_job_id: null` means unknown; a number means bound.
- `mono_ns: null` means the source had no monotonic clock.
- A source revision of `null` was never resolved.
- `exit_code: null` means the process was lost.
- A `worker_count` of `null` is unknown; `0` is serial; a positive
  number is a known worker count. Unknown is never the same as serial.
- A null attempt count, worker, or duration on a native result is
  unknown. An attempt count of zero explicitly records that none ran.
- `dropped_records: 0`, `record_count: 0`, `findings: 0`, and a
  completed collection with `collected_count: 0` all mean measured zero.
- A journal's `complete: false` means its end state is unknown.

## Collection health

The manifest states bundle completeness: `complete`, `end_reason`
(`finished`, `cancelled`, `interrupted`, `unknown`), `dropped_records`,
and `collection_errors`. A complete bundle has no drops, no collection
errors, every journal flushed, every started interval finished, its
terminal events present, every started invocation finished, and every
source that reported a `collection_state` heartbeat in a terminal state,
`stopped` or `unavailable`.
`collection_state` records report each source separately. Not every
source is available in every bundle: an absent source is a coverage gap
stated by the producer, not a violation. A drop is counted and
attributed; core fact loss marks the interval incomplete and is never
hidden as ordinary sampling.

## Redaction and checksums

Redaction runs before any remote storage or public artifact upload, and
the manifest records whether it ran and how many replacements it made. A
producer's redaction marker is not a trust boundary: the central ingest
repeats validation and redaction. Every bundle file is checksummed with
SHA-256; the validators recompute checksums, sizes, and journal record
counts before accepting a bundle.
Journal parsing repeats checksum, size and record-count checks on the
bytes it actually parses. Validation does not freeze files. Consumers
reverify any bytes they read later, including native reports.

## Security rules

- No secret, payload, raw environment, command line, or request body
  fields exist in the schemas. Free-text fields are bounded and must be
  redacted.
- File paths in the manifest are relative, canonical, and unique. Readers
  reject traversal, symlinks (including links to internal files), FIFOs,
  devices, duplicates, and aliases; they never read outside the bundle,
  even to checksum a listed file, and never parse before every file
  check has passed.
- Identity claims in a payload establish nothing, and the manifest and
  journal identity echoes must agree. Only the trusted reconciliation
  described under "Identity and trust" binds a bundle to a run and job.
- Unknown or unsupported `schema_version` values are rejected.
- Validator error messages carry fixed codes, file and record ordinals,
  counts, and schema-owned paths only. They never echo
  producer-controlled strings, so a rejected secret cannot leak through
  an error message or its traceback.

## Validators

`validation.py` is the one implementation of the contract checks;
ingestion, transport, and the tests all call it. `derivation.py` holds
the reference attempt-outcome derivation.

```python
from monitoring.ci_observability.contracts.validation import validate_bundle, validate_manifest, validate_record

validate_manifest(manifest=parsed_manifest)
validate_record(record=parsed_journal_line)
validate_bundle(bundle_dir=bundle_path)
```

All three are keyword-only and raise `ContractError` (a `ValueError`)
listing the violations found. The schema is checked first; semantic
checks that assume a valid shape never run against malformed input, and a
record that fails its schema is never semantically accessed.

## OpenTelemetry mapping

Attribute names map to the CI/CD semantic conventions of
`opentelemetry/semantic-conventions` **v1.44.0**, where the CI/CD
conventions currently hold **Release Candidate** status (verified against
the published registry on 2026-10-28; they may still change, which is why
OSAC-specific fields stay primary and the mapping is advisory):

| Contract field | semconv attribute |
| --- | --- |
| job execution | `cicd.pipeline.task.run` span; `execution_id` / `github_job_id` map to `cicd.pipeline.task.run.id` |
| operation name | `cicd.pipeline.task.name` |
| execution / invocation outcome | `cicd.pipeline.task.run.result` and `cicd.pipeline.result` (`success`, `failure`, `timeout`, `skip`, `cancellation`, `error`) |
| failure signature | `error.type` |
| workflow run URL | `cicd.pipeline.task.run.url.full` |

The outcome mapping is: `passed` to `success`, `failed` to `failure`,
`timeout` to `timeout`, `skipped` to `skip`, `cancelled` to
`cancellation`, and `error` to `error`. Unresolved or interrupted outcomes
leave the result attribute unset; OSAC fields preserve their exact state.
Because the conventions are only release candidates, no producer or
store may depend on them; the OSAC fields are authoritative.

## Illustrative reference queries

These queries illustrate future derived views in W01 and W10. The table
names are proposals, not implemented SQL views or executed coverage.

```sql
-- First-attempt job success rate for one suite variant and cohort.
SELECT suite_variant, count(*) FILTER (WHERE outcome = 'passed')::float / count(*) AS first_attempt_rate
FROM job_executions WHERE run_attempt = 1 AND cohort = 'pull-request'
GROUP BY suite_variant;

-- Worker utilization for one execution: active time over lifetime.
SELECT worker_id, sum(phase_duration_ns) AS active_ns, max(finished_at) - min(started_at) AS lifetime
FROM test_attempts WHERE execution_id = :execution_id GROUP BY worker_id;

-- Tests whose teardown failed after a passing body, with the cleanup
-- failure kept beside the first failure.
SELECT test_id, first_failure_signature, cleanup_failure_signature
FROM derived_attempts WHERE cleanup_failure_signature IS NOT NULL;
```

## Q1-Q8 ownership map

| Outcome | Producer | Store | View | Test | Owning packages |
| --- | --- | --- | --- | --- | --- |
| Q1: exact code, tests, config, images, infrastructure of an attempt | W04 manifest, W02/W03 records | W01 | W10 run investigation | A01, A03 | W00-W04 |
| Q2: wall time, worker time, waiting, determining work | W02 journals, W04 phases | W01 | W10 overview | A02 | W02, W04 |
| Q3: overlap, utilization, shared fixtures, serialization | W02 fixture and worker events | W01 | W10 capacity | A13 | W02 |
| Q4: what a slow test waits for | W08 operations | W01 | W10 run investigation | A12 | W08 |
| Q5: pods, operators, resources, events, logs, pressure | W07 observer | W01, W05a | W10b | A06, A07 | W05a, W07 |
| Q6: failure location versus cause | W02/W03 observations, W11 grouping | W01 | W10 failure explorer | A09, A10 | W11 |
| Q7: comparison with a successful attempt | W01 comparisons | W01 | W10 | A08, A09 | W01, W10 |
| Q8: ranked fixes, owners, recurrence | W11 analysis | W01 | W10, weekly report | A10 | W11 |

## Acceptance gaps by work package

These tests cover schema validation and the file-safety portion of A05.
A01 still needs real producers to emit compatible canonical records.
Remaining boundaries have these owners:

| Gap | Owner |
| --- | --- |
| A01: real canonical producers validate against version 1 | W02, W03 |
| A02: real test processes emit accurate phases and exit codes | W02 |
| A03: real PostgreSQL preserves attempts and replay identity | W01 |
| A04: SSH spool resumes after connection loss | W06 |
| A05 transport half: redaction of nested JSON, encoded credentials, no_log data, split records | W06 |
| A06: Kind history survives pod deletion | W07 |
| A07: real backends retain correlated logs, traces, metrics | W05a/W05b, W07 |
| A08: Grafana counts and drill-down match real rows | W10 |
| A09: reruns, missing pages, expired artifacts stay honest | W01 |
| A10: grouping and impact preserve original facts | W11 |
| A11: approval, fork, dispatch behavior unchanged | W12 |
| A12-A14, A16, A17: deployed cross-component journeys and rehearsal | W08, W04, QE |
| A15: each unit and integration format imports a real execution | W03 |
| A18: retention, capacity, backup, restore | W05a |
| The four Fulfillment integration Ginkgo reports (service-only, manager-capability, manager-readiness, bmaas-disabled) are not captured as fixtures | W03 |
| Ansible native report shape | W09c |

## Validating

The contract tests live in `monitoring/tests/observability/` and call the
production validators. They cover every event type, every example bundle,
the derivation rules, and the negative cases: unsupported schema
versions, unknown event types, impossible calendar dates, traversal file
paths, bad checksums, cross-execution references, string job ids,
producer-claimed inherited outcomes, symlinks, FIFOs, sequence gaps,
duplicate JSON keys, non-finite numbers, identity echo mismatches,
collection inconsistencies, and the file-access guarantees (rejected
files are never read).

Focused runs use `--no-cov` because they do not exercise the exporters and
binding module included by `monitoring/tests/pytest.ini`:

```bash
python3 -m pip install -r monitoring/tests/requirements.txt
cd monitoring/tests && python3 -m pytest observability/ --no-cov
```

The full monitoring suite, as CI runs it, includes them with coverage:

```bash
cd monitoring/tests && python3 -m pytest
```
