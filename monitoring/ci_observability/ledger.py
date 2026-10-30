"""PostgreSQL append-only attempt/event ledger for CI observability (W01a).

Storage layer for validated execution records and journal events. W01b
owns ingestion of the frozen v1 bundle contract. Execution
identity is immutable, the job-id binding, execution state, and GitHub
conclusions are each learned once from unknown, journal events deduplicate
by (execution_id, journal_id, sequence) over a contiguous prefix. Journal
events are append-only. Producer outcome claims live in events;
authoritative GitHub conclusions live in their own columns. Event payloads
are serialized once to canonical JSON text that is both hashed for replay
checks and stored verbatim in a json column, so PostgreSQL never
normalizes the evidence.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeAlias, cast

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

JSONValue: TypeAlias = bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"] | None
_ExecutionRowValues: TypeAlias = (
    "tuple[str, int, int, int, int, str, int | None, bool | None, str | None, str | None, datetime]"
)
# Column order of every full ledger_event SELECT: execution_id, journal_id,
# sequence, schema_version, recorded_at_text, source_at_text, mono_ns,
# invocation_id, clock_id, clock_uncertainty_ns, event, data, ingested_at.
_EventRowValues: TypeAlias = (
    "tuple[str, str, int, int, str, str | None, int | None, str | None, str | None, int | None, str,"
    " dict[str, JSONValue], datetime]"
)

DEFAULT_SCHEMA = "ci_observability"
DEFAULT_LIST_ROWS = 100
MAX_LIST_ROWS = 500
MAX_OFFSET = 10_000
MAX_BATCH_EVENTS = 10_000
MAX_EVENT_DATA_BYTES = 1_048_576
MAX_BATCH_BYTES = 16 * 1_048_576
MAX_JSON_DEPTH = 64
# The default byte budget of one event page: enough for seven maximal
# events plus their envelopes, so a page always advances.
DEFAULT_EVENT_PAGE_BYTES = 8 * 1_048_576
# Fixed JSON envelope allowance per event when a page is planned from
# stored payload sizes; the serving layer counts the real wire bytes.
_EVENT_ENVELOPE_ALLOWANCE_BYTES = 512

EXECUTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
JOURNAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
LOGICAL_JOB_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/#=-]{0,255}$")
SCHEMA_NAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
# The envelope carries original UTC-Z timestamp text; the indexed
# timestamptz columns are derived from it for filters.
TIMESTAMP_TEXT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z$")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

__all__ = [
    "DEFAULT_EVENT_PAGE_BYTES",
    "DEFAULT_LIST_ROWS",
    "DEFAULT_SCHEMA",
    "MAX_BATCH_BYTES",
    "MAX_BATCH_EVENTS",
    "MAX_EVENT_DATA_BYTES",
    "MAX_LIST_ROWS",
    "MAX_OFFSET",
    "AppendResult",
    "ConflictError",
    "EventCursor",
    "EventPage",
    "ExecutionRecord",
    "ExecutionRow",
    "JSONValue",
    "JournalEvent",
    "Ledger",
    "LedgerError",
    "NotFoundError",
    "OffsetStart",
    "RunSummary",
    "SequenceError",
    "StoredEvent",
    "ValidationError",
    "apply_migrations",
    "canonical_payload",
    "format_utc",
    "parse_utc_text",
]


class LedgerError(Exception):
    """Base class of every error this module raises."""


class ValidationError(LedgerError):
    """A record or filter is malformed or out of bounds. The message names the field and the rule, never the value."""


class ConflictError(LedgerError):
    """Stored content contradicts the delivered content."""


class SequenceError(LedgerError):
    """A batch would leave a journal's sequence numbers non-contiguous."""


class NotFoundError(LedgerError):
    """A referenced execution is not registered."""


@dataclass(frozen=True, kw_only=True)
class ExecutionRecord:
    """Identity of one job execution, as validated by the trusted ingest boundary.

    github_job_id is None while the binding is unknown. executed is True
    when the job ran in this attempt, False when it is known not to have
    run (carried over or skipped; the reference is W01b reconciliation
    metadata), and None while unknown, for example for a queued job.
    """

    execution_id: str
    schema_version: int
    repository_id: int
    run_id: int
    run_attempt: int
    logical_job_key: str
    executed: bool | None
    github_job_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.execution_id, str) or not EXECUTION_ID_PATTERN.fullmatch(self.execution_id):
            raise ValidationError("execution_id must be a bounded filename-safe identifier")
        if not _is_int(value=self.schema_version) or self.schema_version < 1:
            raise ValidationError("schema_version must be an integer >= 1")
        if not _is_int(value=self.repository_id) or self.repository_id < 1:
            raise ValidationError("repository_id must be an integer >= 1")
        if not _is_int(value=self.run_id) or self.run_id < 1:
            raise ValidationError("run_id must be an integer >= 1")
        if not _is_int(value=self.run_attempt) or self.run_attempt < 1:
            raise ValidationError("run_attempt must be an integer >= 1")
        if not isinstance(self.logical_job_key, str) or not LOGICAL_JOB_KEY_PATTERN.fullmatch(self.logical_job_key):
            raise ValidationError("logical_job_key must be a bounded job key")
        if self.executed is not None and not isinstance(self.executed, bool):
            raise ValidationError("executed must be a boolean or None")
        if self.github_job_id is not None and (not _is_int(value=self.github_job_id) or self.github_job_id < 1):
            raise ValidationError("github_job_id must be None or an integer >= 1")


@dataclass(frozen=True, kw_only=True)
class ExecutionRow:
    """A stored execution record, including authoritative conclusions."""

    execution_id: str
    schema_version: int
    repository_id: int
    run_id: int
    run_attempt: int
    logical_job_key: str
    github_job_id: int | None
    executed: bool | None
    github_run_conclusion: str | None
    github_job_conclusion: str | None
    registered_at: datetime


@dataclass(frozen=True, kw_only=True)
class RunSummary:
    """Registration facts of one run: executions known to have run, and all registrations."""

    repository_id: int
    run_id: int
    run_attempts: tuple[int, ...]
    execution_count: int
    registration_count: int


@dataclass(frozen=True, kw_only=True)
class JournalEvent:
    """One validated journal record.

    Timestamps are the original UTC-Z text (at most nine fractional
    digits): stored as text for fidelity and as timestamptz for filters.
    mono_ns and clock_uncertainty_ns zero is measured, None is unknown.
    invocation_id and clock_id are the stable W00 envelope fields.
    """

    schema_version: int
    execution_id: str
    journal_id: str
    sequence: int
    recorded_at: str
    event: str
    data: dict[str, JSONValue]
    source_at: str | None = None
    invocation_id: str | None = None
    clock_id: str | None = None
    clock_uncertainty_ns: int | None = None
    mono_ns: int | None = None

    def __post_init__(self) -> None:
        if not _is_int(value=self.schema_version) or self.schema_version < 1:
            raise ValidationError("schema_version must be an integer >= 1")
        if not isinstance(self.execution_id, str) or not EXECUTION_ID_PATTERN.fullmatch(self.execution_id):
            raise ValidationError("execution_id must be a bounded filename-safe identifier")
        if not isinstance(self.journal_id, str) or not JOURNAL_ID_PATTERN.fullmatch(self.journal_id):
            raise ValidationError("journal_id must be a bounded identifier")
        if not _is_int(value=self.sequence) or self.sequence < 1:
            raise ValidationError("sequence must be an integer >= 1")
        parse_utc_text(value=self.recorded_at)
        if not isinstance(self.event, str) or not 1 <= len(self.event) <= 128:
            raise ValidationError("event must be a string of 1 to 128 characters")
        if not isinstance(self.data, dict) or not all(isinstance(key, str) for key in self.data):
            raise ValidationError("data must be a JSON object with string keys")
        if self.source_at is not None:
            parse_utc_text(value=self.source_at)
        if self.invocation_id is not None and (
            not isinstance(self.invocation_id, str) or not JOURNAL_ID_PATTERN.fullmatch(self.invocation_id)
        ):
            raise ValidationError("invocation_id must be a bounded identifier")
        if self.clock_id is not None and (
            not isinstance(self.clock_id, str) or not JOURNAL_ID_PATTERN.fullmatch(self.clock_id)
        ):
            raise ValidationError("clock_id must be a bounded identifier")
        if self.clock_uncertainty_ns is not None and (
            not _is_int(value=self.clock_uncertainty_ns) or self.clock_uncertainty_ns < 0
        ):
            raise ValidationError("clock_uncertainty_ns must be None or an integer >= 0")
        if self.mono_ns is not None and (not _is_int(value=self.mono_ns) or self.mono_ns < 0):
            raise ValidationError("mono_ns must be None or an integer >= 0")


@dataclass(frozen=True, kw_only=True)
class StoredEvent:
    """A stored journal event, with the time the ledger committed it."""

    schema_version: int
    execution_id: str
    journal_id: str
    sequence: int
    recorded_at: str
    event: str
    data: dict[str, JSONValue]
    ingested_at: datetime
    source_at: str | None = None
    invocation_id: str | None = None
    clock_id: str | None = None
    clock_uncertainty_ns: int | None = None
    mono_ns: int | None = None


@dataclass(frozen=True, kw_only=True)
class AppendResult:
    """Acknowledgement of one committed batch: newly stored and duplicate events."""

    appended: int
    duplicates: int


@dataclass(frozen=True, kw_only=True)
class EventCursor:
    """A keyset position in one execution's timeline: the key of the last
    returned event. The next page continues strictly after it, so pages
    never repeat or skip a row, and the original timestamp text keeps the
    position exact to the delivered nine fractional digits."""

    journal_id: str
    sequence: int
    recorded_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.journal_id, str) or not JOURNAL_ID_PATTERN.fullmatch(self.journal_id):
            raise ValidationError("journal_id must be a bounded identifier")
        if not _is_int(value=self.sequence) or self.sequence < 1:
            raise ValidationError("sequence must be an integer >= 1")
        parse_utc_text(value=self.recorded_at)


@dataclass(frozen=True, kw_only=True)
class EventPage:
    """One bounded page of an execution's event timeline.

    Pages are live, not frozen snapshots: each page reflects the rows
    visible when its queries ran, rows appended after the cursor position
    appear on later pages, and rows before it are never returned.
    next_cursor is set when more matching rows are known or planned;
    a page always contains at least one event unless the timeline is
    exhausted."""

    events: tuple[StoredEvent, ...]
    next_cursor: EventCursor | None


@dataclass(frozen=True, kw_only=True)
class OffsetStart:
    """Where an offset window begins.

    cursor is None with exhausted False for offset 0 (begin at the first
    row). exhausted True means fewer than offset rows match, so the window
    is empty rather than wrapping around to the start."""

    cursor: EventCursor | None
    exhausted: bool


def parse_utc_text(*, value: str) -> datetime:
    """Validate original UTC-Z timestamp text (at most 9 fractional digits) and parse it for the indexed columns."""

    if not isinstance(value, str) or not TIMESTAMP_TEXT_PATTERN.fullmatch(value):
        raise ValidationError("timestamp must be a UTC ISO-8601 text ending in Z with at most 9 fractional digits")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValidationError("timestamp text is not a valid calendar date") from None
    return parsed.astimezone(UTC)


def format_utc(*, value: datetime) -> str:
    """Render a datetime as an ISO-8601 UTC timestamp ending in Z."""

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _validate_json_value(*, value: JSONValue, depth: int) -> None:
    if value is None or isinstance(value, str):
        return
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError("event data contains NaN or Infinity, which is not JSON") from None
        return
    if depth >= MAX_JSON_DEPTH:
        raise ValidationError("event data is nested deeper than the storage bound") from None
    if isinstance(value, list):
        for item in value:
            _validate_json_value(value=item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValidationError("event data contains an object key that is not a string") from None
            _validate_json_value(value=item, depth=depth + 1)
        return
    raise ValidationError("event data contains a value that is not JSON, such as a tuple or an object") from None


def canonical_payload(*, data: dict[str, JSONValue]) -> str:
    """Canonical JSON text of an event payload, computed once before any database write.

    Its SHA-256 is the identical-replay check and its exact text is stored
    in the json column, so a mutable input dict cannot change between
    hashing and insertion and PostgreSQL never normalizes the evidence.
    """

    _validate_json_value(value=data, depth=0)
    try:
        return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"event data is not serializable to JSON: {type(exc).__name__}") from None


def _is_int(*, value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_naive(*, value: datetime) -> bool:
    return value.tzinfo is None or value.utcoffset() is None


def _validate_schema_name(*, schema: str) -> None:
    if not isinstance(schema, str) or not SCHEMA_NAME_PATTERN.fullmatch(schema):
        raise ValueError("schema name must be a lowercase identifier of at most 63 characters")


def _validate_page(*, limit: object, offset: object) -> None:
    if not _is_int(value=limit) or not 1 <= limit <= MAX_LIST_ROWS:
        raise ValidationError(f"limit must be an integer between 1 and {MAX_LIST_ROWS}")
    if not _is_int(value=offset) or not 0 <= offset <= MAX_OFFSET:
        raise ValidationError(f"offset must be an integer between 0 and {MAX_OFFSET}")


def _discovered_migrations() -> list[tuple[int, Path]]:
    """Migration files in order of their version prefix."""

    found: list[tuple[int, Path]] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(path.name.split("_", 1)[0])
        found.append((version, path))
    return found


def apply_migrations(*, dsn: str, schema: str = DEFAULT_SCHEMA) -> list[int]:
    """Create the schema if needed and apply pending migrations, returning the versions applied."""

    _validate_schema_name(schema=schema)
    applied: list[int] = []
    with (
        psycopg.connect(
            dsn,
            autocommit=True,
            application_name="ci-observability-migrate",
            connect_timeout=10,
            options="-c statement_timeout=30000 -c lock_timeout=10000",
        ) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (schema,))
        # PostgreSQL checks database CREATE even with IF NOT EXISTS.
        cur.execute("SELECT to_regnamespace(%s)", (schema,))
        if cur.fetchone()[0] is None:
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        cur.execute("SELECT to_regclass(%s)", (f"{schema}.schema_migrations",))
        recorded: set[int] = set()
        if cur.fetchone()[0] is not None:
            migrations_table = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier("schema_migrations"))
            cur.execute(sql.SQL("SELECT version FROM {}").format(migrations_table))
            recorded = {row[0] for row in cur.fetchall()}
        for version, path in _discovered_migrations():
            if version in recorded:
                continue
            cur.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
            cur.execute(path.read_text())
            migrations_table = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier("schema_migrations"))
            cur.execute(sql.SQL("INSERT INTO {} (version) VALUES (%s)").format(migrations_table), (version,))
            applied.append(version)
    return applied


class Ledger:
    """Append-only ledger client bound to one schema, for use by one thread.

    Autocommit mode keeps reads from opening transactions, so every write
    commits exactly one explicit transaction that other connections see
    immediately. A dropped cached connection is replaced on the next
    operation; a database that stays unreachable fails it. Explicit
    connect, statement, and lock timeouts override the DSN.
    """

    def __init__(
        self,
        *,
        dsn: str,
        schema: str = DEFAULT_SCHEMA,
        application_name: str = "ci-observability-ledger",
        connect_timeout_seconds: float = 10.0,
        statement_timeout_ms: int = 30_000,
        lock_timeout_ms: int = 10_000,
    ) -> None:
        _validate_schema_name(schema=schema)
        if connect_timeout_seconds < 1 or statement_timeout_ms < 1 or lock_timeout_ms < 1:
            raise ValueError("connection timeouts must be positive")
        self._dsn = dsn
        self._schema = schema
        self._application_name = application_name
        self._connect_timeout_seconds = connect_timeout_seconds
        self._statement_timeout_ms = statement_timeout_ms
        self._lock_timeout_ms = lock_timeout_ms
        self._conn: psycopg.Connection | None = None

    @property
    def schema(self) -> str:
        return self._schema

    def close(self) -> None:
        self._drop_connection()

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(
            self._dsn,
            autocommit=True,
            application_name=self._application_name,
            connect_timeout=int(self._connect_timeout_seconds),
            options=f"-c statement_timeout={self._statement_timeout_ms} -c lock_timeout={self._lock_timeout_ms}",
        )

    def _connection(self) -> psycopg.Connection:
        """Return a live connection, replacing a cached one the server dropped.

        The one reconnect point: an operation that fails mid-statement
        reports the failure, and a database that stays down fails here.
        """

        if self._conn is None or self._conn.closed:
            self._conn = self._connect()
            return self._conn
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        except psycopg.OperationalError:
            self._drop_connection()
            self._conn = self._connect()
        return self._conn

    def _drop_connection(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(psycopg.Error):
                self._conn.close()
            self._conn = None

    def _table(self, *, name: str) -> sql.Composed:
        return sql.SQL("{}.{}").format(sql.Identifier(self._schema), sql.Identifier(name))

    def register_execution(self, *, record: ExecutionRecord) -> bool:
        """Insert an execution record; True when a new row was inserted.

        Re-registering the same identity is a no-op, an omitted
        still-unknown github_job_id or executed state does not conflict,
        and each learned value never changes: a different one raises
        ConflictError and changes nothing.
        """

        conn = self._connection()
        try:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    sql.SQL(
                        "INSERT INTO {table} (execution_id, schema_version, repository_id, run_id, run_attempt,"
                        " logical_job_key, github_job_id, executed)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                        " ON CONFLICT (execution_id) DO NOTHING"
                    ).format(table=self._table(name="ledger_execution")),
                    (
                        record.execution_id,
                        record.schema_version,
                        record.repository_id,
                        record.run_id,
                        record.run_attempt,
                        record.logical_job_key,
                        record.github_job_id,
                        record.executed,
                    ),
                )
                inserted = cur.rowcount == 1
                cur.execute(
                    sql.SQL(
                        "SELECT schema_version, repository_id, run_id, run_attempt, logical_job_key,"
                        " github_job_id, executed FROM {table} WHERE execution_id = %s FOR UPDATE"
                    ).format(table=self._table(name="ledger_execution")),
                    (record.execution_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise LedgerError("execution vanished during registration")
                differences: list[str] = []
                if row[0] != record.schema_version:
                    differences.append("schema_version")
                if row[1] != record.repository_id:
                    differences.append("repository_id")
                if row[2] != record.run_id:
                    differences.append("run_id")
                if row[3] != record.run_attempt:
                    differences.append("run_attempt")
                if row[4] != record.logical_job_key:
                    differences.append("logical_job_key")
                if row[5] is not None and record.github_job_id is not None and row[5] != record.github_job_id:
                    differences.append("github_job_id")
                if row[6] is not None and record.executed is not None and row[6] != record.executed:
                    differences.append("executed")
                if differences:
                    raise ConflictError(f"execution is already registered with different {' and '.join(differences)}")
                if row[5] is None and record.github_job_id is not None:
                    cur.execute(
                        sql.SQL("UPDATE {table} SET github_job_id = %s WHERE execution_id = %s").format(
                            table=self._table(name="ledger_execution")
                        ),
                        (record.github_job_id, record.execution_id),
                    )
                if row[6] is None and record.executed is not None:
                    cur.execute(
                        sql.SQL("UPDATE {table} SET executed = %s WHERE execution_id = %s").format(
                            table=self._table(name="ledger_execution")
                        ),
                        (record.executed, record.execution_id),
                    )
                return inserted
        except psycopg.errors.UniqueViolation:
            raise ConflictError("github_job_id is already claimed by another executed record") from None
        except psycopg.errors.CheckViolation:
            raise ValidationError("execution record rejected by a storage check") from None

    def record_authoritative_conclusion(
        self, *, execution_id: str, github_run_conclusion: str | None = None, github_job_conclusion: str | None = None
    ) -> None:
        """Record the GitHub-owned conclusions; the only writer of those columns.

        Each is written at most once, so a failed first attempt can never
        be overwritten by a later pass.
        """

        for name, value in (
            ("github_run_conclusion", github_run_conclusion),
            ("github_job_conclusion", github_job_conclusion),
        ):
            if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 64):
                raise ValidationError(f"{name} must be None or a string of 1 to 64 characters")
        if github_run_conclusion is None and github_job_conclusion is None:
            raise ValidationError("at least one conclusion is required")
        conn = self._connection()
        try:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    sql.SQL(
                        "UPDATE {table}"
                        " SET github_run_conclusion = COALESCE(github_run_conclusion, %s),"
                        " github_job_conclusion = COALESCE(github_job_conclusion, %s)"
                        " WHERE execution_id = %s"
                        " RETURNING github_run_conclusion, github_job_conclusion"
                    ).format(table=self._table(name="ledger_execution")),
                    (github_run_conclusion, github_job_conclusion, execution_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise NotFoundError("execution is not registered")
                if github_run_conclusion is not None and row[0] != github_run_conclusion:
                    raise ConflictError("github_run_conclusion is already recorded differently")
                if github_job_conclusion is not None and row[1] != github_job_conclusion:
                    raise ConflictError("github_job_conclusion is already recorded differently")
        except psycopg.errors.CheckViolation:
            raise ValidationError("conclusion rejected by a storage check") from None
        except psycopg.errors.ReadOnlySqlTransaction:
            raise ConflictError("execution conclusion is immutable") from None

    def append_events(self, *, events: Sequence[JournalEvent]) -> AppendResult:
        """Append a batch of journal events in one transaction.

        Returns only after the commit, so the acknowledgement follows a
        durable commit visible to other connections. Identical replay
        counts as duplicates, conflicting content rolls the whole batch
        back, and a sequence gap commits nothing. The per-journal
        checkpoint bounds the work by the batch size in events and bytes.
        """

        if not events:
            raise ValidationError("append_events requires at least one event")
        if len(events) > MAX_BATCH_EVENTS:
            raise ValidationError(f"append_events accepts at most {MAX_BATCH_EVENTS} events per batch")

        grouped: dict[tuple[str, str], list[JournalEvent]] = {}
        seen_sequences: dict[tuple[str, str], set[int]] = {}
        payloads: dict[tuple[str, str, int], str] = {}
        payload_hashes: dict[tuple[str, str, int], str] = {}
        batch_bytes = 0
        for event in events:
            key = (event.execution_id, event.journal_id)
            group = grouped.setdefault(key, [])
            sequences = seen_sequences.setdefault(key, set())
            if event.sequence in sequences:
                raise SequenceError(f"batch repeats sequence {event.sequence}")
            sequences.add(event.sequence)
            canonical = canonical_payload(data=event.data)
            payload_bytes = len(canonical.encode("utf-8"))
            if payload_bytes > MAX_EVENT_DATA_BYTES:
                raise ValidationError("event data exceeds the storage payload bound")
            batch_bytes += payload_bytes
            if batch_bytes > MAX_BATCH_BYTES:
                raise ValidationError("event batch exceeds the storage batch byte bound")
            payloads[(event.execution_id, event.journal_id, event.sequence)] = canonical
            payload_hashes[(event.execution_id, event.journal_id, event.sequence)] = hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest()
            group.append(event)
        for group in grouped.values():
            group.sort(key=lambda event: event.sequence)
        # One deterministic journal order for every concurrent writer, so
        # overlapping batches cannot deadlock on the checkpoint or the
        # primary key.
        ordered_journals = sorted(grouped)

        conn = self._connection()
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    inserted = 0
                    for journal_key in ordered_journals:
                        group = grouped[journal_key]
                        inserted += self._append_journal_events(
                            cur=cur,
                            journal_key=journal_key,
                            group=group,
                            payloads=payloads,
                            payload_hashes=payload_hashes,
                        )
                return AppendResult(appended=inserted, duplicates=len(events) - inserted)
        except psycopg.errors.ForeignKeyViolation:
            raise NotFoundError("event batch references an unregistered execution") from None
        except psycopg.errors.CheckViolation:
            raise ValidationError("event rejected by a storage check") from None

    def _append_journal_events(
        self,
        *,
        cur: psycopg.Cursor,
        journal_key: tuple[str, str],
        group: list[JournalEvent],
        payloads: dict[tuple[str, str, int], str],
        payload_hashes: dict[tuple[str, str, int], str],
    ) -> int:
        """Append one journal's slice of a batch, under its checkpoint lock."""

        execution_id, journal_id = journal_key
        sequences = [event.sequence for event in group]
        cur.execute(
            sql.SQL(
                "INSERT INTO {table} (execution_id, journal_id, prefix_sequence) VALUES (%s, %s, 0)"
                " ON CONFLICT (execution_id, journal_id) DO NOTHING"
            ).format(table=self._table(name="ledger_journal")),
            (execution_id, journal_id),
        )
        cur.execute(
            sql.SQL(
                "SELECT prefix_sequence FROM {table} WHERE execution_id = %s AND journal_id = %s FOR UPDATE"
            ).format(table=self._table(name="ledger_journal")),
            (execution_id, journal_id),
        )
        checkpoint = cur.fetchone()
        if checkpoint is None:
            raise LedgerError("journal lost its checkpoint")
        prefix = checkpoint[0]

        new_sequences = [sequence for sequence in sequences if sequence > prefix]
        if new_sequences != list(range(prefix + 1, prefix + 1 + len(new_sequences))):
            raise SequenceError("a batch must replay stored sequences or continue the prefix")
        if new_sequences:
            new_events = [event for event in group if event.sequence > prefix]
            cur.executemany(
                sql.SQL(
                    "INSERT INTO {table} (execution_id, journal_id, sequence, schema_version,"
                    " recorded_at, recorded_at_text, source_at, source_at_text, mono_ns,"
                    " invocation_id, clock_id, clock_uncertainty_ns, event, data, data_sha256)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::json, %s)"
                    " ON CONFLICT (execution_id, journal_id, sequence) DO NOTHING"
                ).format(table=self._table(name="ledger_event")),
                [
                    (
                        event.execution_id,
                        event.journal_id,
                        event.sequence,
                        event.schema_version,
                        parse_utc_text(value=event.recorded_at),
                        event.recorded_at,
                        None if event.source_at is None else parse_utc_text(value=event.source_at),
                        event.source_at,
                        event.mono_ns,
                        event.invocation_id,
                        event.clock_id,
                        event.clock_uncertainty_ns,
                        event.event,
                        payloads[(event.execution_id, event.journal_id, event.sequence)],
                        payload_hashes[(event.execution_id, event.journal_id, event.sequence)],
                    )
                    for event in new_events
                ],
            )
            inserted = cur.rowcount
            cur.execute(
                sql.SQL("UPDATE {table} SET prefix_sequence = %s WHERE execution_id = %s AND journal_id = %s").format(
                    table=self._table(name="ledger_journal")
                ),
                (prefix + len(new_sequences), execution_id, journal_id),
            )
        else:
            inserted = 0

        cur.execute(
            sql.SQL(
                "SELECT sequence, schema_version, recorded_at_text, source_at_text, mono_ns,"
                " invocation_id, clock_id, clock_uncertainty_ns, event, data_sha256"
                " FROM {table} WHERE execution_id = %s AND journal_id = %s AND sequence = ANY(%s)"
            ).format(table=self._table(name="ledger_event")),
            (execution_id, journal_id, sequences),
        )
        stored = {row[0]: row for row in cur.fetchall()}
        for event in group:
            row = stored.get(event.sequence)
            if row is None:
                raise LedgerError(f"journal lost sequence {event.sequence} while appending")
            if (
                row[1] != event.schema_version
                or row[2] != event.recorded_at
                or row[3] != event.source_at
                or row[4] != event.mono_ns
                or row[5] != event.invocation_id
                or row[6] != event.clock_id
                or row[7] != event.clock_uncertainty_ns
                or row[8] != event.event
                or row[9] != payload_hashes[(event.execution_id, event.journal_id, event.sequence)]
            ):
                raise ConflictError(f"journal already stores different content at sequence {event.sequence}")
        return inserted

    def get_execution(self, *, execution_id: str) -> ExecutionRow | None:
        """Return one execution record, or None when it is not registered."""

        if not isinstance(execution_id, str) or not EXECUTION_ID_PATTERN.fullmatch(execution_id):
            raise ValidationError("execution_id must be a bounded filename-safe identifier")
        with self._connection().cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT execution_id, schema_version, repository_id, run_id, run_attempt, logical_job_key,"
                    " github_job_id, executed, github_run_conclusion, github_job_conclusion, registered_at"
                    " FROM {table} WHERE execution_id = %s"
                ).format(table=self._table(name="ledger_execution")),
                (execution_id,),
            )
            row = cur.fetchone()
            return None if row is None else _execution_row(row=row)

    def execution_counts(self, *, execution_id: str) -> tuple[int, int]:
        """Return (event_count, journal_count) of one execution, from the journal checkpoints."""

        if not isinstance(execution_id, str) or not EXECUTION_ID_PATTERN.fullmatch(execution_id):
            raise ValidationError("execution_id must be a bounded filename-safe identifier")
        with self._connection().cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT coalesce(sum(prefix_sequence), 0)::bigint, count(*) FROM {table} WHERE execution_id = %s"
                ).format(table=self._table(name="ledger_journal")),
                (execution_id,),
            )
            row = cur.fetchone()
            return (row[0], row[1])

    def list_executions(
        self,
        *,
        repository_id: int | None = None,
        run_id: int | None = None,
        run_attempt: int | None = None,
        logical_job_key: str | None = None,
        github_job_id: int | None = None,
        executed: bool | None = None,
        registered_after: datetime | None = None,
        registered_before: datetime | None = None,
        limit: int = DEFAULT_LIST_ROWS,
        offset: int = 0,
    ) -> list[ExecutionRow]:
        """List executions matching every provided filter, bounded by limit and deterministically ordered."""

        clauses: list[sql.Composed] = []
        params: list[object] = []
        if repository_id is not None:
            _require_positive_int(name="repository_id", value=repository_id)
            clauses.append(sql.SQL("repository_id = %s"))
            params.append(repository_id)
        if run_id is not None:
            _require_positive_int(name="run_id", value=run_id)
            clauses.append(sql.SQL("run_id = %s"))
            params.append(run_id)
        if run_attempt is not None:
            _require_positive_int(name="run_attempt", value=run_attempt)
            clauses.append(sql.SQL("run_attempt = %s"))
            params.append(run_attempt)
        if logical_job_key is not None:
            if not isinstance(logical_job_key, str) or not LOGICAL_JOB_KEY_PATTERN.fullmatch(logical_job_key):
                raise ValidationError("logical_job_key must be a bounded job key")
            clauses.append(sql.SQL("logical_job_key = %s"))
            params.append(logical_job_key)
        if github_job_id is not None:
            _require_positive_int(name="github_job_id", value=github_job_id)
            clauses.append(sql.SQL("github_job_id = %s"))
            params.append(github_job_id)
        if executed is not None:
            if not isinstance(executed, bool):
                raise ValidationError("executed must be a boolean")
            clauses.append(sql.SQL("executed = %s"))
            params.append(executed)
        if registered_after is not None:
            if not isinstance(registered_after, datetime) or _is_naive(value=registered_after):
                raise ValidationError("registered_after must be a timezone-aware datetime")
            clauses.append(sql.SQL("registered_at >= %s"))
            params.append(registered_after)
        if registered_before is not None:
            if not isinstance(registered_before, datetime) or _is_naive(value=registered_before):
                raise ValidationError("registered_before must be a timezone-aware datetime")
            clauses.append(sql.SQL("registered_at <= %s"))
            params.append(registered_before)
        _validate_page(limit=limit, offset=offset)
        where = sql.SQL(" AND ").join(clauses) if clauses else sql.SQL("TRUE")
        query = sql.SQL(
            "SELECT execution_id, schema_version, repository_id, run_id, run_attempt, logical_job_key,"
            " github_job_id, executed, github_run_conclusion, github_job_conclusion, registered_at"
            " FROM {table} WHERE {where}"
            " ORDER BY repository_id NULLS LAST, run_id DESC, run_attempt, logical_job_key, execution_id"
            " LIMIT %s OFFSET %s"
        ).format(table=self._table(name="ledger_execution"), where=where)
        with self._connection().cursor() as cur:
            cur.execute(query, (*params, limit, offset))
            return [_execution_row(row=row) for row in cur.fetchall()]

    def list_runs(
        self, *, repository_id: int | None = None, limit: int = DEFAULT_LIST_ROWS, offset: int = 0
    ) -> list[RunSummary]:
        """List registered workflow runs.

        execution_count counts only records known to have executed;
        registration_count counts every stored record, including
        inherited, skipped, and unknown ones.
        """

        clauses: list[sql.Composed] = []
        params: list[object] = []
        if repository_id is not None:
            _require_positive_int(name="repository_id", value=repository_id)
            clauses.append(sql.SQL("repository_id = %s"))
            params.append(repository_id)
        _validate_page(limit=limit, offset=offset)
        where = sql.SQL(" AND ").join(clauses) if clauses else sql.SQL("TRUE")
        query = sql.SQL(
            "SELECT repository_id, run_id, array_agg(DISTINCT run_attempt),"
            " count(*) FILTER (WHERE executed IS TRUE), count(*)"
            " FROM {table} WHERE {where}"
            " GROUP BY repository_id, run_id"
            " ORDER BY repository_id, run_id DESC"
            " LIMIT %s OFFSET %s"
        ).format(table=self._table(name="ledger_execution"), where=where)
        with self._connection().cursor() as cur:
            cur.execute(query, (*params, limit, offset))
            return [
                RunSummary(
                    repository_id=row[0],
                    run_id=row[1],
                    run_attempts=tuple(sorted(row[2])),
                    execution_count=row[3],
                    registration_count=row[4],
                )
                for row in cur.fetchall()
            ]

    def list_environments(
        self, *, execution_id: str, limit: int = DEFAULT_LIST_ROWS, offset: int = 0
    ) -> list[dict[str, JSONValue]]:
        """Read published environment claims and their separate revision verdicts."""

        if not isinstance(execution_id, str) or not EXECUTION_ID_PATTERN.fullmatch(execution_id):
            raise ValidationError("execution_id must be a bounded filename-safe identifier")
        _validate_page(limit=limit, offset=offset)
        with self._connection().cursor(row_factory=dict_row) as cur:
            cur.execute(
                sql.SQL(
                    "SELECT receipt_id, execution_id, manifest_sha256, repository_id, run_id, run_attempt,"
                    " github_job_id, source_created_at, first_seen_at, claimed_workflow_path,"
                    " claimed_workflow_revision, claimed_event, claimed_cohort, claimed_suite, claimed_sources,"
                    " claimed_image_digests, claimed_cluster_snapshot, claimed_config_hash, claimed_complete,"
                    " claimed_end_reason, claimed_dropped_records, tested_sha, revision_verdict, revision_reason,"
                    " revision_provenance FROM {table} WHERE execution_id = %s ORDER BY receipt_id LIMIT %s OFFSET %s"
                ).format(table=self._table(name="ledger_environment")),
                (execution_id, limit, offset),
            )
            rows = cur.fetchall()
        for row in rows:
            row["first_seen_at"] = format_utc(value=row["first_seen_at"])
        return cast(list[dict[str, JSONValue]], rows)

    def list_events(
        self,
        *,
        execution_id: str,
        journal_id: str | None = None,
        recorded_after: datetime | None = None,
        recorded_before: datetime | None = None,
        limit: int = DEFAULT_LIST_ROWS,
        offset: int = 0,
    ) -> list[StoredEvent]:
        """List the events of one execution in timeline order, serving the original timestamp text."""

        clauses, params = self._event_filter_clauses(
            execution_id=execution_id,
            journal_id=journal_id,
            recorded_after=recorded_after,
            recorded_before=recorded_before,
        )
        _validate_page(limit=limit, offset=offset)
        where = sql.SQL(" AND ").join(clauses)
        query = sql.SQL(
            "SELECT execution_id, journal_id, sequence, schema_version, recorded_at_text, source_at_text,"
            " mono_ns, invocation_id, clock_id, clock_uncertainty_ns, event, data, ingested_at"
            " FROM {table} WHERE {where}"
            " ORDER BY recorded_at, journal_id, sequence"
            " LIMIT %s OFFSET %s"
        ).format(table=self._table(name="ledger_event"), where=where)
        with self._connection().cursor() as cur:
            cur.execute(query, (*params, limit, offset))
            return [_stored_event(row=row) for row in cur.fetchall()]

    def list_event_page(
        self,
        *,
        execution_id: str,
        after: EventCursor | None = None,
        journal_id: str | None = None,
        recorded_after: datetime | None = None,
        recorded_before: datetime | None = None,
        limit: int = DEFAULT_LIST_ROWS,
        byte_budget: int = DEFAULT_EVENT_PAGE_BYTES,
    ) -> EventPage:
        """Return one bounded page of an execution's event timeline.

        The page continues strictly after the cursor, applies the same
        filters as list_events, and is planned from stored payload sizes
        so no more rows are fetched than the byte budget and row cap can
        hold: the candidate keys are read first as (journal_id, sequence,
        timestamp text, payload bytes) without payloads, then only the
        chosen key range is fetched. A page always contains at least one
        event when any matching row exists, so a walk always advances.
        next_cursor is set when more matching rows are known or planned;
        pages are live views, not frozen snapshots."""

        if after is not None and not isinstance(after, EventCursor):
            raise ValidationError("after must be an EventCursor or None")
        clauses, params = self._event_filter_clauses(
            execution_id=execution_id,
            journal_id=journal_id,
            recorded_after=recorded_after,
            recorded_before=recorded_before,
        )
        _validate_page(limit=limit, offset=0)
        if not _is_int(value=byte_budget) or byte_budget < 1:
            raise ValidationError("byte_budget must be a positive integer")
        metadata = self._event_key_metadata(clauses=clauses, params=params, after=after, limit=limit)
        if not metadata:
            return EventPage(events=(), next_cursor=None)

        chosen = 0
        planned_bytes = 0
        for _, _, _, payload_bytes in metadata:
            if chosen > 0 and planned_bytes + payload_bytes + _EVENT_ENVELOPE_ALLOWANCE_BYTES > byte_budget:
                break
            planned_bytes += payload_bytes + _EVENT_ENVELOPE_ALLOWANCE_BYTES
            chosen += 1
        more_rows = chosen < len(metadata) or len(metadata) == limit
        # Fetch exactly the planned keys: rows appended inside the planned
        # span between the metadata query and this fetch cannot join the
        # payload fetch, so the materialized bytes stay bounded by the plan
        # and the page never needs trimming or a premature terminal cursor.
        events = self._fetch_selected_events(clauses=clauses, params=params, keys=metadata[:chosen])
        if not events:
            return EventPage(events=(), next_cursor=None)
        last = events[-1]
        next_cursor = (
            EventCursor(journal_id=last.journal_id, sequence=last.sequence, recorded_at=last.recorded_at)
            if more_rows
            else None
        )
        return EventPage(events=tuple(events), next_cursor=next_cursor)

    def event_cursor_after_offset(
        self,
        *,
        execution_id: str,
        journal_id: str | None = None,
        recorded_after: datetime | None = None,
        recorded_before: datetime | None = None,
        offset: int = 0,
    ) -> OffsetStart:
        """Where an offset window begins: the position after the skipped rows.

        cursor is None with exhausted False for offset 0. exhausted True
        means fewer than offset rows match, so the window is empty rather
        than wrapping around to the start of the timeline."""

        clauses, params = self._event_filter_clauses(
            execution_id=execution_id,
            journal_id=journal_id,
            recorded_after=recorded_after,
            recorded_before=recorded_before,
        )
        if not _is_int(value=offset) or offset < 0:
            raise ValidationError("offset must be an integer >= 0")
        if offset == 0:
            return OffsetStart(cursor=None, exhausted=False)
        rows = self._event_key_metadata(clauses=clauses, params=params, after=None, limit=1, offset=offset - 1)
        if not rows:
            return OffsetStart(cursor=None, exhausted=True)
        journal_id_value, sequence, recorded_at_text, _ = rows[0]
        return OffsetStart(
            cursor=EventCursor(journal_id=journal_id_value, sequence=sequence, recorded_at=recorded_at_text),
            exhausted=False,
        )

    def _event_filter_clauses(
        self,
        *,
        execution_id: str,
        journal_id: str | None,
        recorded_after: datetime | None,
        recorded_before: datetime | None,
    ) -> tuple[list[sql.Composed], list[object]]:
        """Validated WHERE clauses and parameters shared by every event read."""

        if not isinstance(execution_id, str) or not EXECUTION_ID_PATTERN.fullmatch(execution_id):
            raise ValidationError("execution_id must be a bounded filename-safe identifier")
        clauses = [sql.SQL("execution_id = %s")]
        params: list[object] = [execution_id]
        if journal_id is not None:
            if not isinstance(journal_id, str) or not JOURNAL_ID_PATTERN.fullmatch(journal_id):
                raise ValidationError("journal_id must be a bounded identifier")
            clauses.append(sql.SQL("journal_id = %s"))
            params.append(journal_id)
        if recorded_after is not None:
            if not isinstance(recorded_after, datetime) or _is_naive(value=recorded_after):
                raise ValidationError("recorded_after must be a timezone-aware datetime")
            clauses.append(sql.SQL("recorded_at >= %s"))
            params.append(recorded_after)
        if recorded_before is not None:
            if not isinstance(recorded_before, datetime) or _is_naive(value=recorded_before):
                raise ValidationError("recorded_before must be a timezone-aware datetime")
            clauses.append(sql.SQL("recorded_at <= %s"))
            params.append(recorded_before)
        return clauses, params

    def _event_key_metadata(
        self,
        *,
        clauses: list[sql.Composed],
        params: list[object],
        after: EventCursor | None,
        limit: int,
        offset: int = 0,
    ) -> list[tuple[str, int, str, int]]:
        """Candidate timeline keys with payload sizes, without the payloads.

        The index-backed keyset predicate keeps this query and the range
        fetch that follows it bounded to the page, never the history."""

        predicates = list(clauses)
        query_params = list(params)
        if after is not None:
            predicates.append(sql.SQL("(recorded_at, journal_id, sequence) > (%s, %s, %s)"))
            query_params.extend([parse_utc_text(value=after.recorded_at), after.journal_id, after.sequence])
        where = sql.SQL(" AND ").join(predicates)
        query = sql.SQL(
            "SELECT journal_id, sequence, recorded_at_text, octet_length(data::text)"
            " FROM {table} WHERE {where}"
            " ORDER BY recorded_at, journal_id, sequence"
            " LIMIT %s OFFSET %s"
        ).format(table=self._table(name="ledger_event"), where=where)
        with self._connection().cursor() as cur:
            cur.execute(query, (*query_params, limit, offset))
            return [(row[0], row[1], row[2], row[3]) for row in cur.fetchall()]

    def _fetch_selected_events(
        self, *, clauses: list[sql.Composed], params: list[object], keys: list[tuple[str, int, str, int]]
    ) -> list[StoredEvent]:
        """Fetch the full rows for exactly the planned keys, in page order.

        (journal_id, sequence) is unique within an execution, so an IN over
        the planned key pairs returns the planned rows and nothing else:
        rows appended between the metadata query and this fetch cannot join
        the payload fetch, keeping the materialized bytes bounded by the
        plan. The key list is bounded by the page row cap."""

        if not keys:
            return []
        predicates = list(clauses)
        predicates.append(
            sql.SQL("(journal_id, sequence) IN ({})").format(sql.SQL(", ").join(sql.SQL("(%s, %s)") for _ in keys))
        )
        where = sql.SQL(" AND ").join(predicates)
        query = sql.SQL(
            "SELECT execution_id, journal_id, sequence, schema_version, recorded_at_text, source_at_text,"
            " mono_ns, invocation_id, clock_id, clock_uncertainty_ns, event, data, ingested_at"
            " FROM {table} WHERE {where}"
            " ORDER BY recorded_at, journal_id, sequence"
        ).format(table=self._table(name="ledger_event"), where=where)
        query_params: list[object] = list(params)
        for key in keys:
            query_params.extend([key[0], key[1]])
        with self._connection().cursor() as cur:
            cur.execute(query, query_params)
            return [_stored_event(row=row) for row in cur.fetchall()]

    def health_check(self) -> None:
        """Raise when the ledger is not queryable, so missing grants surface here, not on the first query."""

        try:
            with self._connection().cursor() as cur:
                cur.execute(
                    sql.SQL("SELECT 1 FROM {table} LIMIT 1").format(table=self._table(name="schema_migrations"))
                )
                if cur.fetchone() is None:
                    raise LedgerError("ledger schema has no applied migration")
        except psycopg.errors.UndefinedTable:
            raise LedgerError("ledger schema is not migrated") from None


def _stored_event(*, row: Sequence[object]) -> StoredEvent:
    values = cast(_EventRowValues, tuple(row))
    return StoredEvent(
        schema_version=values[3],
        execution_id=values[0],
        journal_id=values[1],
        sequence=values[2],
        recorded_at=values[4],
        event=values[10],
        data=values[11],
        ingested_at=values[12],
        source_at=values[5],
        invocation_id=values[7],
        clock_id=values[8],
        clock_uncertainty_ns=values[9],
        mono_ns=values[6],
    )


def _execution_row(*, row: Sequence[object]) -> ExecutionRow:
    values = cast(_ExecutionRowValues, tuple(row))
    return ExecutionRow(
        execution_id=values[0],
        schema_version=values[1],
        repository_id=values[2],
        run_id=values[3],
        run_attempt=values[4],
        logical_job_key=values[5],
        github_job_id=values[6],
        executed=values[7],
        github_run_conclusion=values[8],
        github_job_conclusion=values[9],
        registered_at=values[10],
    )


def _require_positive_int(*, name: str, value: object) -> None:
    if not _is_int(value=value) or value < 1:
        raise ValidationError(f"{name} must be an integer >= 1")
