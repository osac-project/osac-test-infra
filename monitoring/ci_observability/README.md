# CI observability ledger (W01a)

Append-only PostgreSQL storage for CI execution records and journal events,
plus a read-only HTTP API over it. This is the storage layer of the CI
observability work: it keeps exact history for run investigation, comparison,
and reporting, and it never interprets what it stores.

[Bundle ingestion](INGESTION.md) adds private source receipts, quarantine and
verified publication. Producer claims remain separate from GitHub-owned identity
and conclusions.

The user-facing overview of the monitoring stack stays in
[`../README.md`](../README.md). The CI source inventory is in
[`CI-INVENTORY.md`](CI-INVENTORY.md).

## What is stored

| Table | Contents |
| --- | --- |
| `ledger_execution` | One row per job execution: `execution_id`, `schema_version`, `repository_id` (required, positive), `run_id`, `run_attempt`, `logical_job_key`, `github_job_id` (nullable while the binding is unknown), `executed` (nullable while the execution state is unknown), authoritative `github_run_conclusion` and `github_job_conclusion`, `registered_at`. |
| `ledger_journal` | Per-journal contiguity checkpoint: `(execution_id, journal_id)` and `prefix_sequence`, the length of the stored contiguous prefix. |
| `ledger_event` | One row per journal record: `execution_id`, `journal_id`, `sequence`, `schema_version`, the indexed `recorded_at`/`source_at` timestamptz plus the original `recorded_at_text`/`source_at_text`, `mono_ns`, `invocation_id`, `clock_id`, `clock_uncertainty_ns`, `event`, `data` (canonical JSON text in a json column, at most 1 MiB), `data_sha256`, `ingested_at`. |
| `ledger_environment` | Public view of sanitized metadata from committed receipts, with separate revision verdicts. |
| `schema_migrations` | Applied migration versions. |

Migration 0002 adds private source receipts, execution ownership, GitHub
observations and redaction proofs. The reader role cannot select these raw
source tables. See [ingestion storage](INGESTION.md) for their lifecycle.

Migration 0003 adds `ledger_environment`, a `security_barrier` view owned by
the ingest role. It exposes one row per committed receipt with a full proof
and a verified GitHub observation matching the execution's established job.
A rejected replay for another job cannot replace its revision evidence.
Reader access to the view uses the deployment's existing default grants;
the private source tables keep their access restrictions.

Columns prefixed `claimed_` contain sanitized producer claims. Source roles
stay separate, unresolved revisions stay null, and `tested_sha` has its own
`revision_verdict`, `revision_reason` and `revision_provenance`. The source's
`created_at` text and the central `first_seen_at` timestamp remain distinct.
The view publishes neither raw manifests nor private host labels or errors.

`repository_id` is a required positive integer: the v1 manifest always
carries it, so there is no unknown-repository path. `github_job_id` stays
nullable while the binding is unknown, and `executed` keeps three states:
True when the job ran in this attempt, False when it is known not to have
run (its result carries over from an earlier attempt or it was skipped;
the reference to that attempt is reconciliation metadata, W01b), and NULL
while the state is unknown, for example for a queued job. Each of
`executed`, `github_job_id`, and the two conclusions is learned at most
once, from unknown to a value, and never changes afterwards; a
registration that omits a value that was learned later (a stale producer
replay) does not conflict. A numeric `github_job_id` is claimed by at most
one record with `executed IS TRUE`; not-executed and unknown rows claim
nothing, so they never count as executions.

The event name is a bounded string, never an interpreted enum. The event
vocabulary belongs to the frozen v1 journal contract and its future
versions; storage does not interpret it.

Timestamps keep two forms on purpose: the indexed `timestamptz` serves
filters at microsecond precision, and the original text form preserves the
delivered UTC-Z text with up to nine fractional digits, because silently
truncated evidence cannot be replayed as the original. Inputs are
original Z-ending strings, validated once by one parser; served events
carry the original text back. `mono_ns` zero is a measured value; NULL is
unknown, and the same rule holds for `clock_uncertainty_ns` and every
count-like field inside `data`.

Event payloads are serialized exactly once, before any database write, to
a canonical JSON text. That one representation is both hashed for
identical-replay checks and stored verbatim in a `json` column, so a
mutable input dict cannot change between hashing and insertion and
PostgreSQL never normalizes the evidence: `1e23` stays a float, `-0.0`
keeps its sign, and the size bound is the bound of the delivered payload.
`jsonb` is deliberately not used: it normalizes numbers and its text form
can be orders of magnitude larger than the delivered payload (215 floats
of `1.5e300` are about 2 KiB canonical and about 65 KiB as jsonb text),
which would both corrupt evidence fidelity and disagree with the size
bound. The `json` type still supports the `->` and `->>` operators. Values
that are not JSON, including non-string keys, tuples, objects, NaN,
Infinity, excessive nesting and cycles, are rejected before any write,
with value-free error messages.

Delivery bounds agree with the contract's 1 MiB JSONL line: one event
payload is at most 1 MiB of canonical text, one batch is at most 16 MiB
and 10 000 events, so a full-size batch cannot allocate unbounded memory.

## Durability and transaction ownership

The client connection runs in autocommit mode. Reads are single committed
statements, and every write method opens exactly one explicit transaction
and returns only after it committed, so an acknowledgement always follows a
durable commit that other connections can see immediately. (A read on a
default-mode psycopg connection would open an implicit transaction that a
nested `transaction()` block can only turn into a savepoint, leaving the
write uncommitted; that failure mode is covered by a regression test.)

A cached connection that the server dropped is probed and replaced when the
next operation acquires it; this is the single reconnect point. An
operation that fails mid-statement reports the failure, and a database that
stays unreachable fails the operation instead of being masked. Writes are
idempotent, so a caller that received an error for a possibly-committed
batch can safely redeliver it.

Every connection carries explicit connect, statement, and lock timeouts,
which override any options in the DSN. The defaults suit the ingest path;
the read API passes tighter ones.

## Invariants

The database enforces these, not just the Python client
(`migrations/0001_ledger.sql`):

- Events are append-only: `UPDATE` and `DELETE` on `ledger_event` raise
  from triggers. The journal checkpoint only ever moves forward, and its
  identity is immutable.
- Execution identity is immutable. The `github_job_id` binding, the
  `executed` state, and the conclusions may each change only once, from
  `NULL` to a value.
- One numeric `github_job_id` is claimed by at most one executed record.
- `(execution_id, journal_id, sequence)` is the event identity.

`ledger.Ledger` adds the delivery rules on top:

- `register_execution`: inserting the same identity again is a no-op; a
  different identity for the same `execution_id` fails without changes;
  learning `github_job_id` or `executed` from unknown happens once.
- `append_events`: one transaction per batch, bounded by the batch size
  in events and bytes. Identical replay is a no-op
  (`AppendResult.duplicates`). Conflicting content, including the
  original timestamp text, envelope fields and payload hash,
  raises `ConflictError` and the whole batch rolls back, events and
  checkpoint together. The stored sequences of each journal stay a
  contiguous prefix from 1, so a partial batch can only append a
  consistent prefix and a lost record stays a visible gap
  (`SequenceError`). Contiguity is carried by the `ledger_journal`
  checkpoint under a row lock, so appending costs one read of the
  delivered keys instead of a scan of the journal's history; the tests
  assert this bound through `pg_stat_user_tables`.
- `record_authoritative_conclusion`: the only writer of the GitHub-owned
  conclusion columns, for the trusted reconciler. Journal events carry
  producer claims and never reach these columns; the read API serves the
  two separately.

## Schema and roles

The production database, schema, and ingest role are all named
`ci_observability`; the read API reads through `ci_observability_reader`.
The migration creates no roles and issues no grants: role bootstrap and
`GRANT SELECT` for the reader are owned by the monitoring deployment, so a
deviation from the deployment defaults is impossible to hide in a
migration.

Migration 0002 revokes reader access to the private source tables, including
PUBLIC, column-level and inherited grants. Ledger tables keep their grants.
The container bootstrap also denies reader defaults on bootstrap-user-created
tables, so backup restoration preserves private-table access rules.

Bootstrap runs only when the data volume is empty. Before restoring into a
local pilot initialized with the old defaults, rerun the current bootstrap
inside its container. Updating the checkout alone does not change existing
database privileges. From the repository root:

```bash
podman exec -i ci-observability-postgres bash \
    < monitoring/config/observability/postgres-init-roles.sh
```

Use the pilot's container name if it differs. This step reads its existing
protected environment; do not pass passwords on the command line.

Every query is parameterized. Table and schema names are composed with
`psycopg.sql.Identifier` from code-controlled, validated values, never from
request input.

The triggers and write-once rules constrain ordinary clients. They are not
a security guarantee against the database owner, who can always change
history through DDL. Retention and any bounded maintenance path therefore
stay deployment-owned follow-up work (W05/W13); this package implements no
retention.

## CLI

Run from the repository root, with the DSN in the environment:

```bash
CI_OBSERVABILITY_DSN=postgresql://ci_observability@host/db \
    python -m monitoring.ci_observability.api migrate
CI_OBSERVABILITY_DSN=postgresql://ci_observability_reader@host/db \
    python -m monitoring.ci_observability.api serve
```

The DSN is read from `CI_OBSERVABILITY_DSN` and never accepted as a
command-line argument, because argv is world-readable through `/proc` and
the DSN can carry a password; a `--dsn` argument does not exist, and the
argument parser never echoes argument values back. `migrate` applies
pending migrations and is idempotent. `serve` starts the read-only HTTP
API on `127.0.0.1:9185` (override with `--port`) and answers `GET` only.
CLI failures are reported at the boundary with curated or type-only
messages, never a traceback or the DSN. The server refuses to bind any
non-loopback address.

## HTTP API

| Endpoint | Purpose |
| --- | --- |
| `GET /healthz` | Connectivity and migrated-schema check. `200 {"status":"ok"}` or `503`. |
| `GET /metrics` | Prometheus gauge for the ledger health check. A failed check emits zero; diagnostics expose no credentials or queries. |
| `GET /api/v1/runs` | Registered runs. `execution_count` counts records known to have executed; `registration_count` counts every stored record. Filters: `repository_id`. |
| `GET /api/v1/executions` | Bounded execution list. Filters: `repository_id`, `run_id`, `run_attempt`, `logical_job_key`, `github_job_id`, `executed`, `registered_after`, `registered_before`. |
| `GET /api/v1/executions/{execution_id}` | One execution with event and journal counts. |
| `GET /api/v1/executions/{execution_id}/environment` | Published claims and revision verdicts. |
| `GET /api/v1/executions/{execution_id}/events` | Events in timeline order: byte-capped cursor pages by default, the legacy offset window when `offset` is given. Filters: `journal_id`, `recorded_after`, `recorded_before`. |

The environment route returns `environments`, `count`, `limit` and `offset`,
ordered by receipt ID under the existing page bounds. An empty list means no
published environment evidence, not an empty environment. Duplicate delivery
keeps one receipt. Different receipts keep their own completeness claims.

### Bounded event pages

The events route never materializes a history. Without `offset` it serves
one keyset page with the same response fields as the legacy window
(`offset` is always 0 there) plus `next_cursor` and `byte_cap`: the server
reads the candidate timeline keys with their stored payload sizes first,
fetches exactly those planned `(journal_id, sequence)` pairs — rows
appended between the two queries cannot join the payload fetch, so the
page's cost is bounded by its plan even under concurrent inserts — and
cuts the page on the real serialized bytes, data, metadata, and JSON
escaping all counted, against a server-chosen cap (`byte_cap`, 8 MiB: at
least seven maximal 1 MiB events plus envelopes). A page always contains
at least one event, `count` is the exact number of returned rows, and
`next_cursor` continues the walk; a cursor page is never silently
truncated, it just ends at the cap and the rest returns on the next page.
The suffix after the events array is reserved at its derived legal worst
case (a maximal cursor token over a 128-character journal id, a
30-character timestamp, a 19-digit sequence, and the context digest), so
the advertised cap holds for adversarial legal field maxima too. The
keyset predicate `(recorded_at, journal_id, sequence) > (cursor)` is
served by the `idx_event_execution_recorded` index, so each page costs a
bounded index range, never a scan of the history, and a walk traverses any
number of rows — the 10 000-row `offset` cap does not apply to it. The
cursor carries each event's original timestamp text, up to nine fractional
digits; the SQL ordering index stores `timestamptz` microseconds, so two
events whose texts differ only below microseconds tie on the index and are
ordered by `(journal_id, sequence)`, which is unique per execution.

The cursor token is base64 JSON binding the position to the exact
execution and filters: reusing it against a different query is a fixed
`400 cursor does not match this query`, and malformed, oversized, or
mixed tokens get `400 cursor is malformed`, never an echo of the token.
`limit` (1 to 500) caps rows per page and may change mid-walk; `cursor`
and `offset` cannot be combined.

Pages are live views, not snapshots: each page reflects the rows visible
when its two queries ran, rows appended after the cursor position appear
on later pages, and rows that sort before the cursor are never returned.
There is no frozen cross-request snapshot, no server-side cursor state,
no temporary tables, and no read transaction held across requests or
while a response streams.

The legacy `offset` window (same rows and response fields as before,
including an additive `next_cursor`) is streamed in fetch rounds planned
against a 4 MiB byte budget — the budget sizes how many rows each
database fetch materializes, not a wire chunk size — so even a
`limit=500` window of 1 MiB events, roughly 500 MiB of response, is
delivered with server memory bounded by that planning budget. The cursor
page response itself stays within the 8 MiB `byte_cap`. An
`offset` beyond the matching rows is an empty window (`count` 0,
`next_cursor` null), never a wrap-around to the start. A database failure
after the stream started ends the response — the client sees an
incomplete chunked body — rather than corrupting it with a second status
line or an error body.

One response per request, always. The handler marks a response as started
at its first wire write; after that, any failure — a slow client whose
write times out mid-body, a dropped database mid-stream — only closes the
connection and logs the exception type. The client sees a short response,
never a second status line or an error body appended to the one in
flight. Pre-response validation and database failures still get their own
honest 400 or 500, including on a keepalive connection whose earlier
request completed successfully.

All list endpoints take `limit` (1 to 500, default 100); the
non-events lists also take `offset` (0 to 10 000). Every endpoint rejects
unknown or repeated query parameters and validates values before any
query runs. Request paths are capped at 2048 bytes, query strings at 16
fields, and every response body carries the stored facts: the original
timestamp text (nine fractional digits preserved, including inside
cursors), the exact payload types, authoritative conclusions and producer
claims as separate fields, and no derived outcome or cause.

Concurrency is bounded at two levels. Accepted connections are capped
(default 16) before a handler thread is spawned, so idle, slow, or hostile
sockets cannot grow the thread count; a connection arriving at the cap is
closed immediately, and every socket carries a finite header, body, and
write timeout (default 15 s) that expires idle connections and releases
their slot. Concurrent request processing is capped separately (default 8,
which also bounds database connections); a request that waits longer than
10 seconds for a slot gets `503 {"error":"server busy"}`.

Invalid input is `400` with a fixed message that echoes no request value;
server-side failures are `500 {"error":"internal error"}`. Diagnostics log
exception types only, because exception text can carry SQL and connection
strings; the socketserver error hook is overridden for the same reason.
There is no write endpoint and the server makes no outbound requests.

## Dependencies

`runtime-requirements.txt` pins `psycopg[binary]==3.3.6` and
`jsonschema==4.23.0`. Ingestion uses the frozen contract validators locally;
it does not fetch schemas or executable code from the network.

## Tests

Real PostgreSQL only, no SQLite and no mocked database, selected by
`CI_OBSERVABILITY_TEST_DSN`:

```bash
CI_OBSERVABILITY_TEST_DSN=postgresql://ci_observability@localhost:5433/ci_observability \
python -m pytest monitoring/tests/observability --no-cov
```

Environment policy: when the variable is unset and no driver is installed
the modules skip clearly; once the variable is set, a missing driver or an
unreachable database fails the run, so configured coverage cannot quietly
disappear. The tests create their own schemas and a throwaway reader role
and drop them afterwards. Use a disposable database with permission to
create schemas and roles. Covered: the durability regression (mixed
read/write/conclusion sequences verified through second connections, after
writer close, and after backend termination), concurrent duplicate and
conflicting delivery, transaction and checkpoint rollback, reconnect
behavior that does not mask persistent failure, unknown versus zero,
original timestamp text through nine fractional digits, payload type and
number fidelity across replay (exponents, negative zero, the jsonb
inflation case), envelope fields, inherited and unknown execution states,
two attempts of one run, late events, query injection, pagination and
offset caps, and the actual read-only HTTP surface including the restricted
reader role, fixed validation messages, value-free server logs, the
connection and request caps with idle, partial-header, and excess sockets,
and the CLI environment-DSN contract including the `/proc` cmdline check.

## Scope and gaps

The ledger stores validated records. Ingestion validates frozen bundle bytes
and publishes only verified, attested deliveries. Still missing:

- Native report parsing and workflow handoff. The v1 contract, validators and
  ingestion are implemented; native adapters remain separate W03 work.
- The trusted ingest boundary: batch manifests, redaction checks, spooling,
  and acknowledgements over the transport (W06). The storage-side hooks it
  needs (register, append, bind, resolve, conclusions) are in place.
- GitHub polling, artifact delivery, stale-job handling and historical backfill.
  Reconciliation of explicitly supplied trusted metadata is implemented;
  ingestion itself performs no network fetch.
- Live rollout and approved storage and retention limits. Opt-in backend
  packaging, backup tools and required PostgreSQL CI tests exist; none of this
  enables collection or deploys to the fleet.
- Consumer wiring (Grafana dashboards, the W06/W10 read paths) chooses
  between cursor pages and the offset window per query; nothing here
  decides for them.
- Envelope fields added by a future contract version need a migration
  before that version is accepted.
