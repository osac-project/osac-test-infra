# Source-facts outbox (workflow exporter, W01c)

An explicit opt-in, bounded, append-only outbox of raw GitHub source facts,
written by the existing flat `monitoring/scripts/workflow-exporter.py`
(standard library only — no new package, image, or mount). It is a shadow
source for the CI observability ledger: the exporter already polls the
authoritative GitHub API, so this records what it sees before any
flattening, filtering, or derivation, letting the ledger's reconciler
rebuild immutable attempt history that the exporter's SQLite table (latest
attempt only) cannot hold.

This feature is **off by default and is not activated anywhere** — not in
the shipped quadlet, not on the shared runner host. `SOURCE_FACTS_OUTBOX_DIR`
unset means entirely off: no directory is created, nothing is written, and
the exporter performs zero extra IO or API calls. It may only be enabled
later, by explicit user request, on an approved trusted non-runner host.

There is no consumer in this work unit. The consumer/transport (W01b/W06)
reads what this document specifies.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `SOURCE_FACTS_OUTBOX_DIR` | unset | Root directory of the outbox. Unset = feature entirely off. |
| `SOURCE_FACTS_MAX_SEGMENT_BYTES` | 8388608 (8 MiB) | Rotate to a new segment at this size. |
| `SOURCE_FACTS_MAX_SEGMENTS` | 64 | Refuse new segments past this count (never delete). |
| `SOURCE_FACTS_MAX_RECORD_BYTES` | 1048576 (1 MiB) | Records over this are dropped, counted, and noted as a loss state. |
| `SOURCE_FACTS_ATTEMPT_LOOKBACK` | 2 | Earlier attempts observed per re-run, max. On a gate decline, every remaining owed identity in this range is queued as pending. |
| `SOURCE_FACTS_ATTEMPT_CALL_BUDGET` | 25 | Extra history/retry requests per collect cycle, max. |
| `SOURCE_FACTS_ARTIFACTS` | off (`1`/`true`/`yes` to enable) | Artifacts listing subflag, off by default. The run-wide listing is observed once per newly processed run attempt. |
| `SOURCE_FACTS_ARTIFACT_CALL_BUDGET` | 10 | Extra artifacts requests per collect cycle, max. |
| `SOURCE_FACTS_RATE_RESERVE` | 500 | Extra requests stop when rate-limit remaining is at or below this. |

All are read at process start, like the exporter's other configuration.
Every extra request the outbox causes goes through the exporter's existing
`_get` (same token, same rate-limit accounting). The only added requests
are: jobs Link pages beyond the first (only when a run actually has more
than one page of jobs), historical-attempt run objects and jobs, artifact
listings (subflag), and pending-obligation retries by known identity. No
second poller exists: emission happens only inside the existing ingestion
funnels plus the bounded retry pass.

## API behavior

- `_fetch_run_jobs` (the legacy entrypoint, also used by the zombie
  recheck) is **always exactly one `per_page=30` page**, whatever the
  outbox state — same URL, same call count as before this feature existed.
- With the outbox on, ingestion sites pass the run's actual attempt, so
  jobs use the same `attempts/{n}/jobs` endpoint the zombie check already
  used (one call per page), and Link pages are followed to the end.
- Any failure — first page, later page, or a refused pagination target —
  returns the unchanged `None` (retry later; nothing partial or foreign is
  ingested into SQLite) and records an honest coverage gap with a bounded
  reason (`fetch_failed` / `pagination_refused`), never a fake empty list.
- The zombie recheck never paginates and never emits, even with the outbox
  on.

## Pagination target validation

A `rel=next` target is response data. The outbox's jobs/artifacts
pagination follows a target only when it names the **exact same listing**
as the first request:

- the same origin (scheme and host) as the configured `API_URL` — anything
  else would send the token to another host (credential SSRF);
- a **byte-identical resource path** — org, repo, run id, attempt, and
  resource kind may not change, so a Link pointing at another run, another
  attempt, another repo, or another endpoint (e.g. artifacts while paging
  jobs) is refused;
- the **same query context** except for the page cursor (`page`/`cursor`):
  `per_page`, `filter`, or any other parameter added, dropped, or changed
  makes it a different listing and it is refused.

Loops (a target already visited in this fetch) and page caps (40 jobs /
10 artifacts pages, above GitHub's own ~1000-result list limits) are
refused the same way. A refusal is counted
(`workflow_exporter_source_facts_dropped_total{reason="pagination_refused"}`),
logged value-free, and reported in the fetch's coverage gap. The
pre-existing pagination loops (repo lists, active-run lists, backfill
pages) are deliberately unchanged.

## Per-request budget gating

Every request beyond what the legacy path already makes is checked against
its per-cycle budget **and** the remaining GitHub rate limit immediately
before it is made — including each Link tail page of the current-attempt
ingestion fetch, historical attempts, artifacts, and pending retries. The
budget can therefore never go negative. A decline mid-list returns the
pages already fetched with the bounded deferral reason (`budget` /
`rate_limit`): the fetch is incomplete (an honest gap, `page_complete`
never true on a truncated listing), nothing partial is ingested, the
legacy row is not stored, and the owed jobs fact is kept as a pending
obligation — the run can leave the ingestion windows before the budget
recovers. The one exemption is the legacy first page of the
current-attempt ingestion fetch: it is the call the legacy path already
made, so it is fetched even at the rate floor or a zero budget, and the
tail pages are then declined (exactly one call, the tail never
requested). Historical attempts and pending retries gate the first page
too.

## Envelope, version 1

One JSON object per line (JSON Lines). This envelope is separate from the
frozen canonical telemetry contract (`monitoring/ci_observability/contracts`)
and must stay compatible independently.

```json
{
  "schema": "osac.ci-observability.source-facts",
  "version": 1,
  "kind": "run | jobs | run_attempt | artifacts | coverage",
  "repo": "osac",
  "run_id": 32265159432,
  "run_attempt": 2,
  "fetched_at": "2026-10-28T12:00:00Z",
  "page_complete": true,
  "payload": { "...": "kind-specific, see below" }
}
```

- `repo`, `run_id`, `run_attempt` are the GitHub API identity from the
  request context — never derived from payload contents.
- `fetched_at` is UTC, second precision, when this process saw the fact.
- `page_complete` is a **fetch-completeness fact only**: `true` when every
  Link page of that one fetch succeeded, `false` on a gap, `null` when not
  applicable. It says nothing about delivery, durability, or window
  coverage — those are the coverage records' job.
- Payloads pass a strict JSON boundary (below): anything that cannot be
  written as valid JSON — unserializable, over the record cap — is
  dropped, counted, and noted as a loss state.

Kinds and payloads:

| Kind | Payload | Recorded when |
| --- | --- | --- |
| `run` | `{"window": <funnel>, "run": <raw run object>}` | First sighting of a (repo, run id, attempt) in each observation bucket (active / completed), at any ingestion funnel. Includes runs the legacy pipeline ignores (`event` in `IGNORED_EVENTS`) — recorded before the filter, while legacy rows stay excluded. |
| `jobs` | `{"window": <funnel>, "jobs": <raw jobs list>}` | Jobs of a run's current attempt at an ingestion funnel, all Link pages when `page_complete`. An in-progress first sighting and the completed fetch are separate observations. |
| `run_attempt` | `{"window": "attempt_history", "run": <raw attempt run object>, "jobs": <raw jobs list>}` | Terminal state of an earlier attempt (n < current) of a newly-ingested re-run, bounded by lookback and per-cycle budget. |
| `artifacts` | `{"window": <funnel>, "artifacts": <raw artifacts list>}` | A run's artifact listing, only with the artifacts subflag on. |
| `coverage` | bounded enums and counts, see below | Window heartbeat per fetch, explicit gaps, deferrals, unknowns, outbox loss states, and pending-obligation expiries. |

Funnel names (`window`) are a bounded set: `active_runs`,
`recent_completed`, `catchup`, `backfill`, `initial_load`,
`attempt_history`, `artifacts`, `outbox_health`.

## Strict JSON boundary

Only values a well-formed JSON document can round-trip are accepted:
`null`, booleans, integers, finite floats, strings, lists, and
string-keyed dicts, nested at most `JSON_MAX_DEPTH` (24) deep and acyclic.
Everything `json.dumps` would silently normalize is refused instead and
counted as `serialize_error`:

- `NaN`, `Infinity`, `-Infinity` (also `json.loads("1e999")` overflow) —
  `json.dumps(allow_nan=False)` would emit tokens that are **not JSON**;
- tuples (which would become arrays), sets, objects, bytes;
- non-string dict keys (which would become strings);
- cyclic or over-deep structures.

Consequences: an invalid record is never written mid-segment — every
complete persisted line parses under a strict consumer (`parse_constant`
rejecting NaN/Infinity, `object_pairs_hook` rejecting duplicate keys).
The only incomplete line that can ever exist is a torn **trailing** line
of an abandoned segment, from a write that failed partway.

Identity and run status are validated before any dedupe or pending key
is built: the repo must be a string, each run id / attempt a plain
integer or null, and the run status a string or null. A malformed API
value — a list id, NaN, a string id, a non-string repo, a list or dict
status — is refused, never coerced into an invented id, and stated as a
loss record with the invalid part as an explicit unknown (valid sibling
fields are kept); nothing is marked seen, so a later valid observation
still writes, and the observer can never raise into the legacy collect
loop. Jobs pending keys carry the derived completed/active bucket, never
the raw status: an in-progress or None sighting normalizes to the active
bucket once, at the add boundary, so the obligation settles on recovery —
and the already-retained early path settles it too, instead of
re-fetching every cycle until a false expiry.

## Coverage semantics

Coverage records are facts about what was actually observed, never
completeness claims the query cannot support:

- `active_runs`: `complete` true only when every Link page of both status
  lists was fetched.
- `recent_completed` / `initial_load`: one page (50 / 100). `complete` is
  true only when a **known valid** `total_count` says the page held every
  completed run; a known larger total states the truncation
  (`runs_seen` < `runs_total`). A missing or malformed `total_count` is an
  explicit unknown: `complete: null` with `event: "total_unknown"` — never
  `runs_seen` in disguise claiming completeness. A failed fetch is
  `fetch_failed`, complete false. These windows never imply a full
  30/90-day history.
- `backfill_range`: one record per queried range (bisected sub-ranges
  included), `complete` meaning the range probe answered; a failed
  follow-up page emits its own `backfill_page` gap record. The existing
  below-1000 bisection cap is unchanged.
- Gaps: `jobs_unknown` / `attempt_unknown` / `artifacts_unknown` with a
  bounded `reason` (`fetch_failed` / `pagination_refused` / `budget` /
  `rate_limit`) and `pages_fetched` — an honest unknown, never a fake
  empty list.
- Deferrals: `event: "deferred"` with reason `budget` or `rate_limit`
  (an extra call was bounded out this cycle); the obligation is kept for
  retry (below). When the decline hits during historical-attempt
  ingestion, every remaining owed identity in the configured lookback
  range is queued the same way, with no further API calls.
- Loss states: `window: "outbox_health"`, `event: "record_dropped"` with
  the dropped record's kind and reason, carrying the run identity the
  consumer needs to know which fact is missing.
- Pending expiries: `window: "outbox_health"`, `event: "pending_expired"`
  with the pending kind — the fact will not arrive from this producer
  (still replayable from the API).

## Trust model

- GitHub owns CI results and identity. These records are central API
  responses, not producer claims: producer telemetry payloads never enter
  this outbox, and suite-derived rows are never recorded as facts.
- Artifact names and URLs are plain data. They never establish attempt or
  job authority, and the observation attempt is context only — never a
  claim that every artifact belongs to that attempt; the envelope
  identity always comes from the API request context.
- Payloads contain user-controlled strings (workflow names, branches,
  actor logins). They are data, never executed, and stay private: the root
  directory is 0700, segments are 0600, and the writer refuses to write
  (counting degraded health) if it cannot enforce that.
- Errors are value-free: drop reasons and deferral reasons are bounded
  enums, and log lines never contain tokens, DSNs, URLs, or raw record
  contents.

## Writer semantics and consumer handoff

- Segments are `sf-<10-digit-sequence>.jsonl`, created exclusively
  (`O_EXCL`, mode 0600), append-only. The sequence continues above the
  highest existing one at startup, so segment names sort in write order
  within a directory. Two writers on one root cannot share a segment
  (exclusive create); there is meant to be exactly one producer.
- **Durability**: every record is `fsync`ed on the segment file before it
  is acknowledged (before "written" is returned or the dedupe key is
  marked seen), and the parent directory is `fsync`ed once when a new
  segment is created, before its first record is acknowledged. When the
  outbox root (or any missing ancestor) is first created, each new
  directory's parent entry is `fsync`ed before the first acknowledgement,
  so the chain itself cannot silently vanish after a crash. An fsync
  failure is a retryable failure: the segment is abandoned (never appended
  to again), counted as `fsync_failed`, and the record is re-observed
  later. Bytes of an un-acked record may remain in the abandoned segment —
  at-least-once, the consumer deduplicates.
- **Segment cleanup safety**: the parent directory is pinned as a
  descriptor for the whole create/cleanup span of a segment: the segment
  is created and, on failure, removed relative to that exact directory
  (`os.open`/`os.unlink` with `dir_fd`), so a root swapped in between can
  never redirect cleanup at a replacement writer's same-named file. This
  writer never deletes a file it did not create; a detected replacement
  is a counted refusal with the foreign bytes untouched.
- **Root safety**: directory components are opened with `O_DIRECTORY` and
  `O_NOFOLLOW` — a symlinked leaf or component is refused (counted as a
  permission drop), never followed or chmod'ed into an external target,
  and privacy is enforced with `fchmod` through the pinned fd. The root's
  device/inode identity is pinned at adoption and verified on every
  reopened root: a root that was renamed away and replaced is a
  retryable, counted (`root_replaced`) refusal — never an fsync, an
  acknowledgement, or a seen key. Cleanup removes only the caller's own
  empty segment file.
- Rotation starts a new segment when the current one reaches
  `SOURCE_FACTS_MAX_SEGMENT_BYTES`. If the directory cannot be listed, the
  segment cap cannot be checked honestly and the write is **refused**
  (counted), never guessed — an unreadable directory never bypasses the
  cap.
- Bounds: records over the record cap are dropped, counted, and noted as a
  durable loss-state record (small, enum-only, exempt from the data-record
  cap by construction); once the segment count reaches its cap, new writes
  are refused and counted. **Unacknowledged data is never deleted by this
  producer** — bounding disk means refusing to write more, which shows up
  as degraded source health, not as silent data loss. Deletion after
  acknowledgement is the consumer's job.
- Failure containment: disk-full, permission, fsync, and serialization
  failures are counted
  (`workflow_exporter_source_facts_dropped_total{reason=...}`) and never
  break the legacy collect loop or the API budget. A failed write abandons
  the segment, so a torn record can only ever be a segment's trailing
  line.
- How the consumer learns about drops: deterministic drops
  (unserializable, over the record cap) leave a durable
  `outbox_health`/`record_dropped` record in the outbox, carrying the
  dropped fact's kind and a sanitized run identity (an invalid identity
  becomes an explicit unknown, never a faked one; the whole loss path is
  guarded so it can count but never raise). Disk-full and segment-cap
  refusals cannot be recorded in the outbox (no space by definition):
  those are visible through the Prometheus drop counters only, and recover
  through the pending retries below. Recovery never fakes completeness:
  the re-observed fact carries a new `fetched_at`, and the gap was already
  stated.
- **Pending obligations (producer retry)**: a jobs, historical-attempt, or
  artifacts fact that could not be written (retryable write failure) or
  not fetched (budget/rate deferral or fetch failure) is kept as a bounded
  in-memory obligation keyed by (kind, repo, run id, attempt) — at most
  `PENDING_MAX` (200), each retried for at most `PENDING_MAX_AGE` (6 h).
  When a gate declines during ingestion, every remaining owed identity in
  the configured lookback range is queued before requests stop. Each
  collect cycle retries the obligations by known identity only (never a
  second poller), through the same per-request budget gating, **without
  touching legacy rows, counters, or metrics** — independent of the
  legacy upsert gate, which never re-observes an already-stored run. An obligation that
  expires (age or set overflow) is stated as a `pending_expired` coverage
  record. The pending set is in-memory: a restart loses un-retried
  obligations — the durable trail is whatever gap, loss, and expiry
  records were written before the restart, and the Prometheus counters are
  explicitly **not** a durable restart gap. This is a stated limitation,
  not a claim.
- Metrics (bounded labels, never run ids):
  `workflow_exporter_source_facts_records_total{kind}`,
  `workflow_exporter_source_facts_dropped_total{reason}`,
  `workflow_exporter_source_facts_segments`,
  `workflow_exporter_source_facts_written_bytes_total`.
- Delivery is **at-least-once**: the same fact can be delivered more than
  once (restarted process, un-acked bytes in an abandoned segment, a
  pending retry after the normal path already wrote it). The consumer
  deduplicates by **exact content or transport identity** — the record's
  bytes, or its (segment, byte offset) — so a re-delivered identical
  record is a no-op, while a genuinely changed observation (same logical
  identity, different content or `fetched_at`) is preserved as a new fact.
  The logical identity (kind, repo, run id, run attempt, window, bucket)
  is provenance for grouping and querying, **not** a deduplication key:
  collapsing by it would silently discard evolving snapshots and distinct
  same-window observations. Duplicate delivery changes no counts (contract
  rule 12).
- Consumer checkpoint: (segment name, byte offset). Segments are immutable
  once rotated; the active segment only grows. Ignore only a final suffix
  without its newline. Every complete line must parse strictly. Acknowledge
  and delete only after a durable commit of the records read (W01b/W06 own
  the acknowledgement protocol).

## Explicitly out of scope here

- The consumer, transport, and acknowledgement protocol (W01b/W06).
- Bulk historical-attempt backfill (backfill/initial_load never fetch
  attempt history; separate work unit).
- Read-only dedicated credentials (still the exporter's existing token).
- Any activation in the quadlet or on the shared runner host; rollout to
  an approved trusted non-runner host is a separate, user-approved step.
