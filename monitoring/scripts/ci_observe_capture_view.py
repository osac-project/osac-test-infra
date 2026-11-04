"""The Capture document for ci-observe: one capture, read complete from Tempo.

A capture is one CI run's telemetry. The run index and the registry answer
which runs exist. This module answers what one capture recorded, and is the
only ci-observe read path that calls Tempo for spans.

The document is built from the trace-by-id endpoint, never from a TraceQL
span search. Tempo caps a search at 100 spans per span set and silently
collapses duplicates, so a search cannot prove a count. Trace-by-id returns
the whole trace. Every span is classified by ``pytest.span_type``; the
classified and unclassified counts must add up to the raw span count, and a
trace that does not add up is ``unknown``, never a short table.

Finding the trace still needs one tight search: the capture id is a span
resource attribute, and Tempo has no trace-by-capture-id index. That search
is limited to the run's own window and a handful of results, so a busy day
does not change it. The discovered trace id is stored in the registry
(``captures.session_trace_id``) so later loads skip the search entirely.

The registry schema belongs to the run-index workstream. This module reads
it when present and never creates it: a missing table means empty identity,
not an error.

The response is cached for ten minutes and capped at 64 MB. Concurrent
readers of the same capture share one Tempo lookup. ``not_indexed`` and
``busy`` are cached for ten seconds only, because both are expected to
clear.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

# The exporter reaches Tempo on the host's loopback (see the quadlet and
# ci_observe_runs.py). A different layout sets CI_OBSERVE_TEMPO_URL.
DEFAULT_TEMPO_URL = "http://127.0.0.1:3210"
TEMPO_URL_ENV = "CI_OBSERVE_TEMPO_URL"

CAPTURE_ATTRIBUTE = "osac.ci.capture_id"

# Current capture ids are "<suite>-<prN|main>-<run id>-a<attempt>".
# Older captures used "run-<run id>-attempt-<n>". Both carry the run id,
# which is the only join key the exporter's job history has.
CAPTURE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*-(?:pr\d+|main)-(\d+)-a(\d+)$")
LEGACY_CAPTURE_ID_RE = re.compile(r"^run-(\d+)-attempt-(\d+)$")

# A pytest-xdist worker span is named "test worker gw0". Newer spans also
# carry osac.ci.xdist_worker; the parent span name covers older captures.
WORKER_SPAN_RE = re.compile(r"^test worker (\S+)$")

# A test span is emitted four times: once for the test and once for each
# phase. The parent span is the one the test tables show; the phases are
# attempts inside it.
PHASE_SUFFIXES = ("::setup", "::call", "::teardown")
PYTEST_CLASSES = ("test", "fixture", "fixture_use", "wait", "session")

# The six classification buckets. "other" is the unclassified bucket:
# Beyla service calls and any span a future pytest plugin adds without a
# span_type this module knows. It is reported as unclassified, never as a
# short table.
SPAN_CLASSES = (*PYTEST_CLASSES, "other")

REGISTRY_FIELDS = (
    "run_id",
    "run_attempt",
    "suite",
    "pr",
    "workflow",
    "test_ref",
    "test_sha",
    "test_infra_ref",
    "test_infra_sha",
    "test_infra_workflow_sha",
    "test_suite",
    "test_filter",
    "test_marker",
    "session_trace_id",
    "identity_source",
    "capture_started_at",
)


class TempoBusy(Exception):
    """Tempo refused the request with 429; retry shortly."""


class TempoMissing(Exception):
    """Tempo has no such trace."""


class TempoTooLarge(Exception):
    """The answer is larger than this view is willing to read."""


class TempoError(Exception):
    """Tempo failed in a way the reader cannot fix."""


def _http_get(url, *, timeout, max_bytes):
    """Read at most max_bytes from url, or raise the matching Tempo error.

    The cap is enforced while reading, not from Content-Length: a forged or
    missing length must not make the reader buffer an unbounded body.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read(max_bytes + 1)
    except urllib.error.HTTPError as error:
        detail = ""
        with contextlib.suppress(OSError):
            detail = error.read().decode("utf-8", "replace").strip()[:200]
        if error.code == 404:
            raise TempoMissing(f"HTTP 404: {detail or error.reason}") from None
        if error.code == 429:
            raise TempoBusy(f"HTTP 429: {detail or error.reason}") from None
        raise TempoError(f"HTTP {error.code}: {detail or error.reason}") from None
    except (OSError, ValueError) as error:
        raise TempoError(f"the request failed: {error}") from None
    if len(body) > max_bytes:
        raise TempoTooLarge(f"the answer is larger than {max_bytes} bytes")
    return body


def _attribute_value(value):
    """A plain Python value from an OTLP AnyValue."""
    if not isinstance(value, dict):
        return None
    if "stringValue" in value:
        return str(value["stringValue"])
    if "intValue" in value:
        raw = value["intValue"]
        try:
            return int(raw)
        except (TypeError, ValueError):
            return raw
    if "doubleValue" in value:
        try:
            return float(value["doubleValue"])
        except (TypeError, ValueError):
            return None
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "arrayValue" in value:
        array = value["arrayValue"]
        values = array.get("values") if isinstance(array, dict) else None
        return [_attribute_value(entry) for entry in values or []]
    if "kvlistValue" in value:
        entries = value["kvlistValue"]
        values = entries.get("values") if isinstance(entries, dict) else None
        return {
            entry.get("key"): _attribute_value(entry.get("value")) for entry in values or [] if isinstance(entry, dict)
        }
    return None


def _attributes(entries):
    """A flat dict from an OTLP KeyValue list."""
    if not isinstance(entries, list):
        return {}
    result = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not isinstance(key, str) or not key:
            continue
        result[key] = _attribute_value(entry.get("value"))
    return result


def _status_name(value):
    """OTLP status code as "ok", "error" or "unset"."""
    if isinstance(value, str):
        text = value.upper()
        if "ERROR" in text:
            return "error"
        if text == "OK" or "STATUS_CODE_OK" in text:
            return "ok"
        return "unset"
    if not isinstance(value, dict):
        return "unset"
    code = value.get("code")
    if isinstance(code, str):
        if "ERROR" in code.upper():
            return "error"
        if code.upper() == "OK" or "STATUS_CODE_OK" in code.upper():
            return "ok"
        return "unset"
    if code == 2:
        return "error"
    if code == 1:
        return "ok"
    return "unset"


def _parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso_from_ns(value):
    try:
        seconds = int(value) / 1_000_000_000
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_int(value):
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


class CIObserveCaptureView:
    """The /api/ci-observe/capture document.

    The exporter argument provides the SQLite registry and job history
    (``_db``) and, when present, the cache of already-parsed JUnit evidence
    (``_run_evidence._cache``). This view never downloads an artifact and
    never calls GitHub. Tempo is reached over HTTP with the standard library,
    read-only.
    """

    # A complete trace is stable, so a positive answer is cached for ten
    # minutes. not_indexed and busy are expected to clear, so they are held
    # for ten seconds: an open page retries soon without hammering Tempo.
    CACHE_TTL_SECONDS = 600
    NEGATIVE_CACHE_TTL_SECONDS = 10
    # A capture with no "test run" span yet may still be exporting spans: the
    # session span is written last. Its tables are partial, so re-read it
    # within a minute instead of pinning a partial count for ten.
    INCOMPLETE_CACHE_TTL_SECONDS = 60
    CACHE_MAX_BYTES = 64 * 1024 * 1024

    # A trace larger than this is not read into memory. The live traces
    # measured 175-1854 spans and under 2 MB; the cap is a safety net.
    MAX_TRACE_BYTES = 32 * 1024 * 1024
    MAX_SEARCH_BYTES = 4 * 1024 * 1024
    MAX_ROWS_PER_BUCKET = 20000

    # The search is tight on purpose: one capture id, one run window, ten
    # trace results and ten spans per span set. A search is only used to
    # learn the trace id, never to count spans.
    SEARCH_LIMIT = 10
    SEARCH_SPSS = 10
    TEMPO_TIMEOUT_SECONDS = 20

    # A capture that arrived seconds ago may not be searchable yet. If the
    # registry has no row and the run is younger than this, the trace is
    # "not_indexed", never "missing".
    FRESH_CAPTURE_SECONDS = 900

    # Tempo keeps 30 days. A registered capture older than this cannot be
    # found by any search, so it is missing rather than endlessly
    # "not_indexed".
    TEMPO_RETENTION_DAYS = 30

    # The search window around a run's start. Five minutes covers clock
    # skew and a job that starts before its run row; eight hours covers a
    # full-install run with provisioning and teardown.
    WINDOW_BEFORE = timedelta(minutes=5)
    WINDOW_AFTER = timedelta(hours=8)

    def __init__(self, exporter, tempo_url=None):
        self.exporter = exporter
        self.tempo_url = (tempo_url or os.getenv(TEMPO_URL_ENV) or DEFAULT_TEMPO_URL).rstrip("/")
        # OrderedDict is the LRU: the oldest entry is at the front.
        self._cache = OrderedDict()
        # One Event per capture id in flight. Readers of the same id wait
        # for the leader instead of starting a second Tempo lookup.
        self._inflight = {}
        self._lock = threading.Lock()

    # -- public ------------------------------------------------------------
    def get_json(self, params):
        """The Capture document for ?capture_id=.

        Returns capture_id, state, session, coverage, identity, tests,
        waits, fixtures, fixture_uses, failed_tests, transient and notes.
        Every state returns the full shape: a failed lookup has empty buckets
        and a note saying why, never a partial table. `transient` marks a
        failure of the source (Tempo unreachable or 5xx), which is cached for
        the short negative TTL; a stable result is cached for the full TTL.
        """
        capture_id = self._param(params, "capture_id").strip()
        if not capture_id:
            document = self._empty_document("")
            document["state"] = "unknown"
            document["notes"] = ["capture_id query parameter is missing or empty"]
            return document
        return self._cached_document(capture_id)

    # -- cache and single flight ------------------------------------------
    def _cached_document(self, capture_id):
        while True:
            with self._lock:
                entry = self._cache.get(capture_id)
                if entry is not None:
                    if entry["expires_at"] > time.monotonic():
                        self._cache.move_to_end(capture_id)
                        return copy.deepcopy(entry["document"])
                    del self._cache[capture_id]
                event = self._inflight.get(capture_id)
                if event is None:
                    event = threading.Event()
                    self._inflight[capture_id] = event
                    leader = True
                else:
                    leader = False
            if not leader:
                event.wait(timeout=self.TEMPO_TIMEOUT_SECONDS * 2)
                continue
            try:
                document = self._build_document(capture_id)
                # Store before waking waiters: a waiter that finds neither a
                # cache entry nor an in-flight leader would start a second
                # Tempo lookup.
                self._store(capture_id, document)
            finally:
                with self._lock:
                    self._inflight.pop(capture_id, None)
                event.set()
            return copy.deepcopy(document)

    def _store(self, capture_id, document) -> None:
        if document["state"] in ("not_indexed", "busy") or document.get("transient"):
            ttl = self.NEGATIVE_CACHE_TTL_SECONDS
        elif document["state"] == "ok" and not document["coverage"]["session_span"]:
            ttl = self.INCOMPLETE_CACHE_TTL_SECONDS
        else:
            ttl = self.CACHE_TTL_SECONDS
        size = len(json.dumps(document, separators=(",", ":")).encode("utf-8"))
        with self._lock:
            self._cache[capture_id] = {"expires_at": time.monotonic() + ttl, "document": document, "size": size}
            self._cache.move_to_end(capture_id)
            total = sum(entry["size"] for entry in self._cache.values())
            while total > self.CACHE_MAX_BYTES and len(self._cache) > 1:
                _, evicted = self._cache.popitem(last=False)
                total -= evicted["size"]

    # -- document ----------------------------------------------------------
    def _build_document(self, capture_id):
        notes = []
        identity, registry = self._registry(capture_id)
        parsed = self._parse_capture_id(capture_id)
        if parsed["format"] == "invalid":
            notes.append(
                f"capture id {capture_id!r} matches no capture-id format; "
                "the run is taken from the registry when it has one"
            )
        run_id = parsed["run_id"]
        run_attempt = parsed["run_attempt"]
        if run_id is None and registry:
            run_id = _as_int(registry.get("run_id"))
        if run_attempt is None and registry:
            run_attempt = _as_int(registry.get("run_attempt"))

        window = self._run_window(run_id, run_attempt, registry)
        document = self._empty_document(capture_id)
        document["identity"] = identity
        if registry is None:
            notes.append(f"the capture registry has no captures row for {capture_id!r}; identity is empty")
        if window is None:
            notes.append(
                "the capture id names no run in the exporter's job history and "
                "the registry has no start time, so no Tempo window is available"
            )
        elif window["source"].startswith("captures."):
            notes.append(f"the run window comes from {window['source']}, not the job history")

        trace_id = ""
        trace_source = ""
        if registry:
            trace_id = str(registry.get("session_trace_id") or "").strip()
            if trace_id:
                trace_source = "registry"
                notes.append("the registry already has the session trace id; Tempo search was skipped")

        if not trace_id:
            if window is None:
                document["state"] = "missing"
                document["notes"] = notes
                return document
            trace_id, state, search_notes = self._discover_trace(
                capture_id, window, registry_present=registry is not None
            )
            notes.extend(search_notes)
            if not trace_id:
                document["state"] = state
                # A failed search is the source being unavailable, not a
                # property of the capture: retry it soon, not in 10 minutes.
                document["transient"] = state == "unknown"
                document["notes"] = notes
                return document
            trace_source = "search"

        try:
            payload = self._fetch_trace(trace_id)
        except TempoBusy as error:
            notes.append(f"Tempo is busy reading trace {trace_id}: {error}")
            document["state"] = "busy"
            document["notes"] = notes
            return document
        except TempoMissing as error:
            if trace_source == "search":
                notes.append(f"the search returned trace {trace_id}, but Tempo cannot read it yet: {error}")
                document["state"] = "not_indexed"
            else:
                notes.append(f"the registry's session trace id {trace_id} is not in Tempo: {error}")
                document["state"] = "missing"
            document["notes"] = notes
            return document
        except TempoTooLarge as error:
            notes.append(f"trace {trace_id} is too large to read: {error}")
            document["state"] = "too_large"
            document["notes"] = notes
            return document
        except TempoError as error:
            notes.append(f"Tempo could not read trace {trace_id}: {error}")
            document["state"] = "unknown"
            document["transient"] = True
            document["notes"] = notes
            return document

        # Persist only a trace that was actually read. A search can return a
        # trace the ingester has not flushed yet; storing it then would turn
        # the next transient 404 into a cached `missing`.
        if trace_source == "search":
            notes.extend(self._remember_trace_id(capture_id, trace_id, "search"))

        session, buckets, coverage = self._classify_trace(payload)
        if coverage["spans_classified"] + coverage["spans_unclassified"] != coverage["spans_total"]:
            notes.append(
                "the trace did not classify completely "
                f"({coverage['spans_classified']} classified + "
                f"{coverage['spans_unclassified']} unclassified != "
                f"{coverage['spans_total']} spans); no partial table is shown"
            )
            document["state"] = "unknown"
            document["notes"] = notes
            return document

        coverage["service_calls"] = coverage["spans_unclassified"]
        coverage["service_calls_complete"] = False
        session["trace_id"] = trace_id
        session["span_total"] = coverage["spans_total"]
        if not session["workers"]:
            notes.append("the trace records no xdist worker; the worker column stays empty")
        if coverage["session_span"] == 0:
            notes.append(
                "the trace has no 'test run' session span; the session duration is "
                "the trace wall clock, and the capture is incomplete"
            )
        if coverage["spans_unclassified"]:
            notes.append(
                f"{coverage['spans_unclassified']} spans are outside the pytest classes "
                "(service calls and other spans); service calls are sampled, not counted "
                "as a test result"
            )

        document.update(
            state="ok",
            session=session,
            coverage=coverage,
            tests=self._cap(buckets["tests"], "tests", notes),
            waits=self._cap(buckets["waits"], "waits", notes),
            fixtures=self._cap(buckets["fixtures"], "fixtures", notes),
            fixture_uses=self._cap(buckets["fixture_uses"], "fixture_uses", notes),
        )
        document["identity"]["session_trace_id"] = trace_id
        document["failed_tests"] = self._cached_failed_tests(run_id, run_attempt, notes)
        document["notes"] = notes
        return document

    def _empty_document(self, capture_id):
        return {
            "capture_id": capture_id,
            "state": "unknown",
            "transient": False,
            "session": {
                "trace_id": "",
                "started_at": None,
                "duration_seconds": None,
                "status": None,
                "tests": 0,
                "test_attempts": 0,
                "workers": [],
                "span_total": 0,
            },
            "coverage": {
                "spans_total": 0,
                "spans_classified": 0,
                "spans_unclassified": 0,
                "session_span": 0,
                "service_calls": 0,
                "service_calls_complete": False,
            },
            "identity": {field: "" for field in REGISTRY_FIELDS},
            "tests": [],
            "waits": [],
            "fixtures": [],
            "fixture_uses": [],
            "failed_tests": [],
            "notes": [],
        }

    # -- registry and job history -----------------------------------------
    def _registry(self, capture_id):
        """(identity, raw row) from captures, or (empty identity, None).

        The table belongs to the run-index workstream. A database that does
        not have it yet reads as "no registry row": empty identity, and the
        capture is still readable from Tempo.
        """
        connection = self._connection()
        if connection is None:
            return {field: "" for field in REGISTRY_FIELDS}, None
        try:
            if "captures" not in self._tables(connection):
                return {field: "" for field in REGISTRY_FIELDS}, None
            row = connection.execute("SELECT * FROM captures WHERE capture_id = ?", (capture_id,)).fetchone()
        except sqlite3.Error as error:
            logger.debug("capture registry read failed: %s", error)
            return {field: "" for field in REGISTRY_FIELDS}, None
        finally:
            connection.close()
        if row is None:
            return {field: "" for field in REGISTRY_FIELDS}, None
        raw = dict(row)
        identity = {field: "" if raw.get(field) is None else str(raw.get(field, "")) for field in REGISTRY_FIELDS}
        return identity, raw

    def _job_rows(self, run_id, run_attempt):
        """(rows for the run, source label), newest attempt preferred.

        The run-index workstream adds jobs.run_id. Before that column exists,
        a job row's url is the only stored trace of its parent run. Both are
        read: the run id is authoritative when present, the url is the
        fallback for old rows.
        """
        if run_id is None:
            return [], ""
        connection = self._connection()
        if connection is None:
            return [], ""
        rows = []
        source = ""
        try:
            tables = self._tables(connection)
            if "jobs" not in tables:
                return [], ""
            columns = self._columns(connection, "jobs")
            if "run_id" in columns:
                rows = connection.execute("SELECT * FROM jobs WHERE run_id = ?", (str(run_id),)).fetchall()
                if rows:
                    source = "jobs.run_id"
            if not rows:
                rows = connection.execute(
                    "SELECT * FROM jobs WHERE id = ? OR url LIKE ?", (run_id, f"%/actions/runs/{run_id}/job/%")
                ).fetchall()
                if rows:
                    source = "jobs.url"
        except sqlite3.Error as error:
            logger.debug("job history read failed for run %s: %s", run_id, error)
            return [], ""
        finally:
            connection.close()
        by_id = {}
        for row in rows:
            record = dict(row)
            by_id[record.get("id")] = record
        records = list(by_id.values())
        if run_attempt is not None:
            matching = [record for record in records if str(record.get("run_attempt") or "") == str(run_attempt)]
            if matching:
                records = matching
        return records, source

    def _run_window(self, run_id, run_attempt, registry) -> dict | None:
        """The Tempo search window for a run, or None.

        The window is the run start minus five minutes to the run start plus
        eight hours. `created_at` is fixed across re-runs, so a re-run that
        starts more than eight hours later would fall outside it; for attempt
        2 and later the start is `updated_at - duration_s`, which is that
        attempt's `run_started_at`. A registry row is the fallback when the
        job history has no row: capture_started_at is the run start,
        first_seen_at is only when discovery saw the capture and is labelled
        as such.
        """
        rows, source = self._job_rows(run_id, run_attempt)
        attempt = None if run_attempt is None else str(run_attempt)
        starts = []
        for record in rows:
            if attempt in (None, "1"):
                created = _parse_time(record.get("created_at"))
                if created is not None:
                    starts.append(created)
                continue
            ended = _parse_time(record.get("updated_at"))
            duration = _as_int(record.get("duration_s")) or 0
            if str(record.get("run_attempt") or "") == attempt and ended is not None and duration > 0:
                starts.append(ended - timedelta(seconds=duration))
        if not starts:
            # No stored row for the requested attempt (or no duration): the
            # run still existed at created_at, so search from there rather
            # than declare the capture missing.
            starts = [
                start
                for start in (_parse_time(record.get("created_at")) for record in rows)
                if start is not None
            ]
        if starts:
            start = min(starts)
            return {
                "start": start,
                "from": start - self.WINDOW_BEFORE,
                "to": start + self.WINDOW_AFTER,
                "source": source or "jobs",
            }
        if registry:
            started = _parse_time(registry.get("capture_started_at"))
            if started is not None:
                return {
                    "start": started,
                    "from": started - self.WINDOW_BEFORE,
                    "to": started + self.WINDOW_AFTER,
                    "source": "captures.capture_started_at",
                }
            seen = _parse_time(registry.get("first_seen_at"))
            if seen is not None:
                return {
                    "start": seen,
                    "from": seen - self.WINDOW_BEFORE,
                    "to": seen + self.WINDOW_AFTER,
                    "source": "captures.first_seen_at",
                }
        return None

    def _persist_session_trace_id(self, capture_id, trace_id):
        """Store a discovered trace id in captures when the schema allows it.

        Never creates the table or the column: the run-index workstream owns
        the migration. A missing table or column means the id stays in this
        process's cache only.
        """
        connection = self._connection()
        if connection is None:
            return False
        try:
            if "captures" not in self._tables(connection):
                return False
            if "session_trace_id" not in self._columns(connection, "captures"):
                return False
            cursor = connection.execute(
                "UPDATE captures SET session_trace_id = ? WHERE capture_id = ? AND COALESCE(session_trace_id, '') = ''",
                (trace_id, capture_id),
            )
            connection.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as error:
            logger.debug("capture trace id update failed: %s", error)
            return False
        finally:
            connection.close()

    def _connection(self):
        get_db = getattr(self.exporter, "_db", None)
        if get_db is None:
            return None
        try:
            return get_db()
        except sqlite3.Error as error:
            logger.debug("exporter database unavailable: %s", error)
            return None

    @staticmethod
    def _tables(connection):
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    @staticmethod
    def _columns(connection, table):
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}

    # -- capture id --------------------------------------------------------
    @staticmethod
    def _parse_capture_id(capture_id):
        """(format, run id, attempt). No run association is invented."""
        match = CAPTURE_ID_RE.match(capture_id)
        if match:
            return {"format": "exact", "run_id": int(match.group(1)), "run_attempt": int(match.group(2))}
        match = LEGACY_CAPTURE_ID_RE.match(capture_id)
        if match:
            return {"format": "legacy", "run_id": int(match.group(1)), "run_attempt": int(match.group(2))}
        return {"format": "invalid", "run_id": None, "run_attempt": None}

    # -- Tempo -------------------------------------------------------------
    def _discover_trace(self, capture_id, window, *, registry_present):
        """(trace id, state, notes) for the capture's session trace.

        The primary search asks for the one span every capture has, the
        "test run" session span. The fallback asks for a test span, because
        an interrupted run can end before the session span is exported.
        """
        notes = []
        quoted = json.dumps(capture_id)
        window_start = int(window["from"].timestamp())
        window_end = int(window["to"].timestamp())
        search_window = {"start": window_start, "end": window_end}

        primary = f'resource."{CAPTURE_ATTRIBUTE}"={quoted} && name="test run"'
        try:
            trace_ids = self._search(primary, search_window)
        except TempoBusy as error:
            return "", "busy", [f"Tempo is busy searching for capture {capture_id!r}: {error}"]
        except (TempoMissing, TempoTooLarge, TempoError) as error:
            return "", "unknown", [f"Tempo could not search for capture {capture_id!r}: {error}"]
        if trace_ids:
            trace_id = trace_ids[0]
            if len(trace_ids) > 1:
                notes.append(f"the session search returned {len(trace_ids)} traces; using the first ({trace_id})")
            return trace_id, "ok", notes

        fallback = f'resource."{CAPTURE_ATTRIBUTE}"={quoted} && .pytest.span_type = "test"'
        try:
            trace_ids = self._search(fallback, search_window)
        except TempoBusy as error:
            return "", "busy", [f"Tempo is busy searching for capture {capture_id!r}: {error}"]
        except (TempoMissing, TempoTooLarge, TempoError) as error:
            return "", "unknown", [f"Tempo could not search for capture {capture_id!r}: {error}"]
        if trace_ids:
            trace_id = trace_ids[0]
            if len(trace_ids) > 1:
                notes.append(f"the test-span search returned {len(trace_ids)} traces; using the first ({trace_id})")
            notes.append("the session search found nothing; the trace id came from a test span")
            return trace_id, "ok", notes

        now = datetime.now(UTC)
        age = now - window["start"]
        if age > timedelta(days=self.TEMPO_RETENTION_DAYS):
            return (
                "",
                "missing",
                [
                    *notes,
                    f"the capture is older than Tempo's {self.TEMPO_RETENTION_DAYS}-day "
                    "retention, so no search can find its trace",
                ],
            )
        if age < timedelta(seconds=self.FRESH_CAPTURE_SECONDS):
            return (
                "",
                "not_indexed",
                [
                    *notes,
                    "Tempo search returned no trace in the run window; the capture is recent, so it is not indexed yet",
                ],
            )
        if registry_present:
            return (
                "",
                "missing",
                [
                    *notes,
                    "the registry has a row for the capture, but Tempo has no trace in "
                    "the run window; the trace never arrived or has aged out",
                ],
            )
        return "", "missing", [*notes, "neither Tempo nor the registry has this capture in the run window"]

    def _remember_trace_id(self, capture_id, trace_id, source):
        if self._persist_session_trace_id(capture_id, trace_id):
            return [f"stored the {source} trace id in the capture registry"]
        return []

    def _search(self, query, window):
        params = urllib.parse.urlencode(
            {
                "q": query,
                "start": window["start"],
                "end": window["end"],
                "limit": self.SEARCH_LIMIT,
                "spss": self.SEARCH_SPSS,
            }
        )
        url = f"{self.tempo_url}/api/search?{params}"
        body = _http_get(url, timeout=self.TEMPO_TIMEOUT_SECONDS, max_bytes=self.MAX_SEARCH_BYTES)
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TempoError(f"the search answer is not JSON: {error}") from None
        traces = document.get("traces") if isinstance(document, dict) else None
        if not isinstance(traces, list):
            raise TempoError("the search answer has no traces list")
        trace_ids = []
        for trace in traces:
            if not isinstance(trace, dict):
                continue
            trace_id = trace.get("traceID") or trace.get("traceId")
            if isinstance(trace_id, str) and trace_id and trace_id not in trace_ids:
                trace_ids.append(trace_id)
        return trace_ids

    def _fetch_trace(self, trace_id):
        url = f"{self.tempo_url}/api/traces/{urllib.parse.quote(trace_id, safe='')}"
        body = _http_get(url, timeout=self.TEMPO_TIMEOUT_SECONDS, max_bytes=self.MAX_TRACE_BYTES)
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TempoError(f"the trace answer is not JSON: {error}") from None
        if not isinstance(document, dict):
            raise TempoError("the trace answer is not an object")
        if not document.get("batches") and not document.get("resourceSpans"):
            trace = document.get("trace")
            if not isinstance(trace, dict) or not trace.get("resourceSpans"):
                raise TempoError("the trace answer has no batches")
        return document

    # -- classification ----------------------------------------------------
    def _classify_trace(self, payload):
        """(session, buckets, coverage) for one trace payload.

        Every span is visited exactly once. A span this module cannot read
        (not an object, no timestamps) is not classified, so the counts stop
        adding up and the caller returns unknown instead of a short table.
        """
        spans = []
        spans_total = 0
        for span, resource_attributes in self._iter_spans(payload):
            spans_total += 1
            entry = self._span_entry(span, resource_attributes)
            if entry is not None:
                spans.append(entry)
        self._resolve_workers(spans)

        buckets = {"tests": [], "waits": [], "fixtures": [], "fixture_uses": []}
        test_entries = [entry for entry in spans if entry["class"] == "test"]
        parent_tests = [entry for entry in test_entries if not entry["name"].endswith(PHASE_SUFFIXES)]
        phase_counts = {}
        for entry in test_entries:
            nodeid = entry["attributes"].get("pytest.nodeid") or entry["name"]
            phase_counts[nodeid] = phase_counts.get(nodeid, 0) + 1

        for entry in parent_tests:
            nodeid = entry["attributes"].get("pytest.nodeid") or entry["name"]
            buckets["tests"].append(
                {
                    "name": entry["name"],
                    "nodeid": nodeid,
                    "file": entry["attributes"].get("code.filepath") or "",
                    "worker": entry["worker"],
                    "started_at": entry["started_at"],
                    "duration_s": entry["duration_s"],
                    "status": entry["status"],
                    "phase_spans": phase_counts.get(nodeid, 1),
                }
            )
        for entry in spans:
            if entry["class"] == "wait":
                buckets["waits"].append(self._wait_row(entry))
            elif entry["class"] == "fixture":
                buckets["fixtures"].append(self._fixture_row(entry))
            elif entry["class"] == "fixture_use":
                buckets["fixture_uses"].append(self._fixture_use_row(entry))

        for rows in buckets.values():
            rows.sort(key=lambda row: (row["started_at"] or "", row["name"], row.get("worker") or ""))

        session_span = next(
            (entry for entry in spans if entry["class"] == "session" and entry["name"] == "test run"), None
        )
        if session_span is not None:
            started_at = session_span["started_at"]
            duration = session_span["duration_s"]
            status = session_span["status"]
        else:
            started_at = min((entry["started_at"] for entry in spans), default=None)
            if spans:
                first = min(entry["start_ns"] for entry in spans)
                last = max(entry["end_ns"] for entry in spans)
                duration = round((last - first) / 1_000_000_000, 3)
            else:
                duration = None
            status = None

        workers = sorted({entry["worker"] for entry in spans if entry["worker"]})
        classified = sum(1 for entry in spans if entry["class"] in PYTEST_CLASSES)
        unclassified = sum(1 for entry in spans if entry["class"] == "other")
        session = {
            "trace_id": "",
            "started_at": started_at,
            "duration_seconds": duration,
            "status": status,
            "tests": len(parent_tests),
            "test_attempts": len(test_entries),
            "workers": workers,
            "span_total": spans_total,
        }
        coverage = {
            "spans_total": spans_total,
            "spans_classified": classified,
            "spans_unclassified": unclassified,
            "session_span": 1 if session_span is not None else 0,
            "service_calls": 0,
            "service_calls_complete": False,
        }
        return session, buckets, coverage

    @staticmethod
    def _iter_spans(payload):
        if not isinstance(payload, dict):
            return
        batches = payload.get("batches")
        if not isinstance(batches, list):
            trace = payload.get("trace")
            batches = trace.get("resourceSpans") if isinstance(trace, dict) else payload.get("resourceSpans")
        if not isinstance(batches, list):
            return
        for batch in batches:
            if not isinstance(batch, dict):
                continue
            resource = batch.get("resource")
            resource_attributes = _attributes(resource.get("attributes") if isinstance(resource, dict) else None)
            scopes = batch.get("scopeSpans") or batch.get("instrumentationLibrarySpans") or []
            if not isinstance(scopes, list):
                continue
            for scope in scopes:
                if not isinstance(scope, dict):
                    continue
                spans = scope.get("spans")
                if not isinstance(spans, list):
                    continue
                for span in spans:
                    yield span, resource_attributes

    def _span_entry(self, span, resource_attributes):
        if not isinstance(span, dict):
            return None
        try:
            start_ns = int(span.get("startTimeUnixNano"))
            end_ns = int(span.get("endTimeUnixNano"))
        except (TypeError, ValueError):
            return None
        attributes = dict(resource_attributes)
        attributes.update(_attributes(span.get("attributes")))
        name = span.get("name") if isinstance(span.get("name"), str) else ""
        span_type = attributes.get("pytest.span_type")
        span_class = self._span_class(span_type, attributes)
        return {
            "span_id": span.get("spanId") or "",
            "parent_span_id": span.get("parentSpanId") or "",
            "name": name,
            "class": span_class,
            "attributes": attributes,
            "start_ns": start_ns,
            "end_ns": end_ns,
            "started_at": _iso_from_ns(start_ns),
            "duration_s": round(max(0, end_ns - start_ns) / 1_000_000_000, 3),
            "status": _status_name(span.get("status")),
            "worker_attribute": str(attributes.get("osac.ci.xdist_worker") or ""),
            "worker": "",
        }

    @staticmethod
    def _span_class(span_type, attributes):
        if span_type in ("test", "fixture", "fixture_use", "wait"):
            return span_type
        if span_type == "run":
            return "session"
        # Older captures recorded a wait as a poll span with no span_type.
        if not span_type and "poll.delay" in attributes:
            return "wait"
        return "other"

    @staticmethod
    def _resolve_workers(spans):
        by_id = {entry["span_id"]: entry for entry in spans if entry["span_id"]}
        for entry in spans:
            worker = entry["worker_attribute"]
            if not worker:
                match = WORKER_SPAN_RE.match(entry["name"])
                if match:
                    worker = match.group(1)
            current = entry
            depth = 0
            while not worker and depth < 32:
                parent = by_id.get(current["parent_span_id"])
                if parent is None:
                    break
                worker = parent["worker_attribute"]
                if not worker:
                    match = WORKER_SPAN_RE.match(parent["name"])
                    if match:
                        worker = match.group(1)
                current = parent
                depth += 1
            entry["worker"] = worker

    @staticmethod
    def _wait_row(entry):
        attributes = entry["attributes"]
        return {
            "name": entry["name"],
            "worker": entry["worker"],
            "started_at": entry["started_at"],
            "duration_s": entry["duration_s"],
            "status": entry["status"],
            "attempts": attributes.get("poll.attempts"),
            "condition_met": attributes.get("poll.condition_met"),
            "resource_kind": attributes.get("osac.wait.resource_kind") or "",
            "resource_id": attributes.get("osac.wait.resource_id") or "",
        }

    @staticmethod
    def _fixture_row(entry):
        attributes = entry["attributes"]
        name = entry["name"]
        fixture_name = attributes.get("code.function") or re.sub(r" (setup|teardown)$", "", name)
        if name.endswith(" teardown"):
            phase = "teardown"
        elif name.endswith(" setup"):
            phase = "setup"
        else:
            phase = ""
        return {
            "name": name,
            "fixture_name": fixture_name,
            "scope": attributes.get("pytest.fixture_scope") or "",
            "phase": phase,
            "worker": entry["worker"],
            "started_at": entry["started_at"],
            "duration_s": entry["duration_s"],
            "status": entry["status"],
        }

    @staticmethod
    def _fixture_use_row(entry):
        attributes = entry["attributes"]
        return {
            "name": entry["name"],
            "fixture_name": attributes.get("pytest.fixture_name") or entry["name"],
            "nodeid": attributes.get("pytest.nodeid") or "",
            "worker": entry["worker"],
            "started_at": entry["started_at"],
            "duration_s": entry["duration_s"],
            "status": entry["status"],
        }

    def _cap(self, rows, bucket, notes):
        if len(rows) <= self.MAX_ROWS_PER_BUCKET:
            return rows
        notes.append(f"{bucket} is capped at {self.MAX_ROWS_PER_BUCKET} of {len(rows)} rows")
        return rows[: self.MAX_ROWS_PER_BUCKET]

    # -- JUnit evidence ----------------------------------------------------
    def _cached_failed_tests(self, run_id, run_attempt, notes):
        """Failed tests from an already-populated JUnit evidence cache entry.

        The artifact is fetched by the /api/ci-observe/run endpoint, which the
        Capture page's CI evidence table already calls. This endpoint must
        never trigger that download: a cold artifact fetch costs more than
        the Capture cold budget. A cache miss returns no rows and points the
        reader at the CI evidence panel; it is not an empty test result.
        """
        evidence = getattr(self.exporter, "_run_evidence", None)
        cache = getattr(evidence, "_cache", None)
        if cache is None or run_id is None or run_attempt is None:
            notes.append("failed test detail is in the CI evidence panel")
            return []
        cached = cache.get(("native", run_id, run_attempt))
        if not isinstance(cached, dict):
            notes.append("failed test detail is in the CI evidence panel")
            return []
        status = cached.get("status")
        if status != "available":
            notes.append(f"failed test detail is not available in the CI evidence panel (JUnit artifact is {status!r})")
            return []
        failed = cached.get("failed_tests")
        return failed if isinstance(failed, list) else []

    @staticmethod
    def _param(params, name):
        value = params.get(name, "") if hasattr(params, "get") else ""
        if isinstance(value, list):
            return value[0] if value else ""
        return value or ""
