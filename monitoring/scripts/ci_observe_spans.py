"""Where each test's time went, from the ci-observe traces (span rollup).

A recorded E2E run leaves one trace in Tempo: the pytest session, every test
with its setup, call and teardown phase, every fixture setup and every
poll_until wait (`pytest.span_type="wait"`, with the resource kind it waited
on). Tempo keeps that trace for 30 days and answers it one capture at a
time. This script reads each finished capture's trace once and stores the
per-test breakdown in trends.db, next to the JUnit history, so the
ci-observe pages can answer across runs and over time:

- how long a test spends setting up, running and tearing down;
- how much of that is waiting for the product (a wait span: OSAC
  reconciling a ComputeInstance, a ClusterOrder, a BareMetalInstance ...),
  by resource kind and by what was waited for;
- which shared (session- or module-scoped) fixtures cost the most.

Tables (all keyed by capture id, replaced whole when a capture is read
again): span_runs (one row per capture: whether its trace was complete),
test_spans (one row per test), wait_spans (one row per wait),
fixture_spans (one row per top-level fixture setup).

Attribution: a wait or fixture span belongs to the test phase it nests
under. The exporter loses some parent links (see the osac telemetry
module), so a span whose parent chain is broken is placed by time instead:
the phase span on the same xdist worker that contains it. A span that fits
neither stays unattributed rather than being charged to a guess.

The waits of one test phase are counted as a union of intervals, so nested
or overlapping waits are never counted twice, and a test's own time splits
into four parts that add up to its duration: setup, waiting for the
product, test actions (the rest of the call) and teardown.

Run every hour (ci-observe-spans.timer). A capture is read once its run has
been finished for SETTLE_MINUTES (Tempo needs about 15 minutes before a
flushed block is searchable). A trace without its session span is read
again on later passes for RETRY_HOURS, then kept as it is.

    CACHE_DIR=~/.monitoring-server/data ./ci_observe_spans.py
    CACHE_DIR=~/.monitoring-server/data ./ci_observe_spans.py --capture vmaas-pr12-123-a1 --force
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ci_observe_phases as phases
from ci_observe_capture_view import (
    CIObserveCaptureView,
    TempoBusy,
    TempoError,
    TempoMissing,
    TempoTooLarge,
)

logger = logging.getLogger(__name__)

CACHE_DIR = os.getenv("CACHE_DIR", os.path.expanduser("~/.monitoring-server/data"))
EXPORTER_DB = os.path.join(CACHE_DIR, "workflow-exporter.db")
TRENDS_DB = os.path.join(CACHE_DIR, "trends.db")
KEEP_DAYS = int(os.getenv("CI_OBSERVE_KEEP_DAYS", "90"))
# Tempo keeps 30 days; an older capture cannot be read any more.
TEMPO_RETENTION_DAYS = 30
SETTLE_MINUTES = int(os.getenv("CI_OBSERVE_SPANS_SETTLE_MINUTES", "20"))
RETRY_HOURS = 48
DEFAULT_LIMIT = int(os.getenv("CI_OBSERVE_SPANS_PER_PASS", "120"))
SHARED_SCOPES = ("session", "module", "package")
PHASE_NAMES = ("setup", "call", "teardown")

SCHEMA = """
CREATE TABLE IF NOT EXISTS span_runs (
    capture_id TEXT PRIMARY KEY,
    run_id INTEGER,
    run_attempt INTEGER,
    suite TEXT NOT NULL DEFAULT '',
    day TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT '',
    trace_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    complete INTEGER NOT NULL DEFAULT 0,
    spans_total INTEGER,
    tests INTEGER,
    waits INTEGER,
    session_s REAL,
    unattributed_waits INTEGER,
    rolled_at TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_span_runs_day ON span_runs(day);
CREATE TABLE IF NOT EXISTS test_spans (
    capture_id TEXT NOT NULL,
    run_id INTEGER,
    day TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT '',
    suite TEXT NOT NULL DEFAULT '',
    test_key TEXT NOT NULL,
    nodeid TEXT NOT NULL,
    worker TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    duration_s REAL,
    setup_s REAL,
    call_s REAL,
    teardown_s REAL,
    wait_s REAL,
    wait_setup_s REAL,
    wait_call_s REAL,
    wait_teardown_s REAL,
    shared_setup_s REAL,
    waits INTEGER,
    PRIMARY KEY (capture_id, nodeid)
);
CREATE INDEX IF NOT EXISTS idx_test_spans_key ON test_spans(test_key, day);
CREATE INDEX IF NOT EXISTS idx_test_spans_day ON test_spans(day);
CREATE TABLE IF NOT EXISTS wait_spans (
    capture_id TEXT NOT NULL,
    run_id INTEGER,
    day TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT '',
    suite TEXT NOT NULL DEFAULT '',
    test_key TEXT NOT NULL DEFAULT '',
    nodeid TEXT NOT NULL DEFAULT '',
    phase TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    signature TEXT NOT NULL,
    resource_kind TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    duration_s REAL,
    attempts INTEGER,
    condition_met INTEGER,
    failed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_wait_spans_capture ON wait_spans(capture_id);
CREATE INDEX IF NOT EXISTS idx_wait_spans_day ON wait_spans(day);
CREATE INDEX IF NOT EXISTS idx_wait_spans_key ON wait_spans(test_key, day);
CREATE TABLE IF NOT EXISTS fixture_spans (
    capture_id TEXT NOT NULL,
    run_id INTEGER,
    day TEXT NOT NULL,
    suite TEXT NOT NULL DEFAULT '',
    test_key TEXT NOT NULL DEFAULT '',
    nodeid TEXT NOT NULL DEFAULT '',
    fixture TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    duration_s REAL
);
CREATE INDEX IF NOT EXISTS idx_fixture_spans_capture ON fixture_spans(capture_id);
CREATE INDEX IF NOT EXISTS idx_fixture_spans_day ON fixture_spans(day);
"""

# What a wait description names besides its kind and condition: resource
# names with random suffixes, UUIDs, hex ids and counts. Replaced so the
# same wait in two runs groups together.
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
# A name Kubernetes (or a test copying its generateName) made unique with a
# five-character suffix from its random alphabet, which has no vowels and no
# 0, 1 or 3: "vm-ssvtr", "ci-x8k2p". The suffix can hold no digit at all,
# so the digit rule below does not catch every such name.
GENERATED_NAME_RE = re.compile(r"\b[a-z0-9]+(?:[.-][a-z0-9]+)*-[bcdfghjklmnpqrstvwxz2456789]{5}\b")
HEX_RE = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,}\b", re.IGNORECASE)
NAME_WITH_DIGITS_RE = re.compile(r"\b(?=[A-Za-z0-9._-]*\d)[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)+\b")
NUMBER_RE = re.compile(r"\b\d+\b")
QUOTED_RE = re.compile(r"(['\"])[^'\"]{1,80}\1")


def wait_signature(name, resource_id=""):
    """The wait's description with its instance-specific parts replaced:
    "ComputeInstance <id> to become Ready". Groups the same wait across
    runs and tests. Applying it to a signature returns the signature, so a
    reader can regroup signatures stored before a rule was added."""
    text = " ".join((name or "").split())
    if resource_id and len(resource_id) >= 3:
        text = text.replace(resource_id, "<id>")
    text = UUID_RE.sub("<id>", text)
    text = GENERATED_NAME_RE.sub("<id>", text)
    text = QUOTED_RE.sub(r"\1<v>\1", text)
    text = NAME_WITH_DIGITS_RE.sub("<id>", text)
    text = HEX_RE.sub("<id>", text)
    text = NUMBER_RE.sub("N", text)
    text = re.sub(r"(<id>)(\s*<id>)+", r"\1", text)
    return text[:200] or "(unnamed wait)"


def union_seconds(intervals):
    """Seconds covered by [(start_ns, end_ns), ...], overlaps counted once."""
    total = 0
    current_start = current_end = None
    for start, end in sorted(interval for interval in intervals if interval[1] > interval[0]):
        if current_end is None or start > current_end:
            if current_end is not None:
                total += current_end - current_start
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    if current_end is not None:
        total += current_end - current_start
    return round(total / 1_000_000_000, 3)


def _seconds(entry):
    return round(max(0, entry["end_ns"] - entry["start_ns"]) / 1_000_000_000, 3)


class _ReadOnlyExporter:
    """The slice of the workflow exporter the capture view reads: _db().
    Read-only, so the view's attempt to remember a trace id in the registry
    fails quietly instead of writing the exporter's DB from a batch job."""

    def __init__(self, path):
        self.path = path

    def _db(self):
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn


def breakdown(entries):
    """(tests, waits, fixtures, unattributed) from classified span entries.

    entries: CIObserveCaptureView._span_entry dicts, workers resolved.
    """
    by_id = {entry["span_id"]: entry for entry in entries if entry["span_id"]}
    tests = {}
    phase_of_span = {}
    for entry in entries:
        if entry["class"] != "test":
            continue
        name = entry["name"]
        phase = next((part for part in PHASE_NAMES if name.endswith(f"::{part}")), "")
        nodeid = entry["attributes"].get("pytest.nodeid") or (name[: -len(phase) - 2] if phase else name)
        slot = tests.setdefault(nodeid, {"test": None, "setup": None, "call": None, "teardown": None})
        key = phase or "test"
        current = slot[key]
        if current is None or _seconds(entry) > _seconds(current):
            slot[key] = entry
        if entry["span_id"]:
            phase_of_span[entry["span_id"]] = (nodeid, phase or "call")

    phase_spans = [
        (nodeid, phase, slot[phase])
        for nodeid, slot in tests.items()
        for phase in PHASE_NAMES
        if slot[phase] is not None
    ]

    def attribute(entry):
        current = entry
        for _ in range(64):
            parent = by_id.get(current["parent_span_id"])
            if parent is None:
                break
            if parent["span_id"] in phase_of_span:
                return phase_of_span[parent["span_id"]]
            current = parent
        best = None
        for nodeid, phase, span in phase_spans:
            if entry["worker"] and span["worker"] and entry["worker"] != span["worker"]:
                continue
            if span["start_ns"] <= entry["start_ns"] and entry["end_ns"] <= span["end_ns"]:
                if best is None or _seconds(span) < _seconds(best[2]):
                    best = (nodeid, phase, span)
        return (best[0], best[1]) if best else ("", "")

    def is_nested_fixture(entry):
        parent = by_id.get(entry["parent_span_id"])
        return parent is not None and parent["class"] == "fixture"

    waits = []
    fixtures = []
    unattributed = 0
    for entry in entries:
        if entry["class"] == "wait":
            nodeid, phase = attribute(entry)
            if not nodeid:
                unattributed += 1
            attributes = entry["attributes"]
            met = attributes.get("poll.condition_met")
            waits.append({
                "nodeid": nodeid,
                "phase": phase,
                "name": entry["name"],
                "signature": wait_signature(entry["name"], str(attributes.get("osac.wait.resource_id") or "")),
                "resource_kind": str(attributes.get("osac.wait.resource_kind") or ""),
                "started_at": entry["started_at"] or "",
                "duration_s": _seconds(entry),
                "attempts": attributes.get("poll.attempts") if isinstance(attributes.get("poll.attempts"), int)
                else None,
                "condition_met": None if met is None else (1 if met else 0),
                "failed": 1 if entry["status"] == "error" or met is False else 0,
                "_interval": (entry["start_ns"], entry["end_ns"]),
            })
        elif entry["class"] == "fixture" and entry["name"].endswith(" setup") and not is_nested_fixture(entry):
            nodeid, phase = attribute(entry)
            attributes = entry["attributes"]
            fixtures.append({
                "nodeid": nodeid,
                "fixture": str(attributes.get("code.function") or entry["name"][: -len(" setup")]),
                "scope": str(attributes.get("pytest.fixture_scope") or ""),
                "started_at": entry["started_at"] or "",
                "duration_s": _seconds(entry),
                "_interval": (entry["start_ns"], entry["end_ns"]),
            })

    rows = []
    for nodeid, slot in tests.items():
        test = slot["test"]
        own_phases = {phase: slot[phase] for phase in PHASE_NAMES}
        if test is None and not any(own_phases.values()):
            continue
        phase_seconds = {phase: (_seconds(span) if span else None) for phase, span in own_phases.items()}
        if test is not None:
            duration = _seconds(test)
        else:
            duration = round(sum(value for value in phase_seconds.values() if value), 3)
        test_waits = [wait for wait in waits if wait["nodeid"] == nodeid]
        wait_by_phase = {
            phase: union_seconds(wait["_interval"] for wait in test_waits if wait["phase"] == phase)
            for phase in PHASE_NAMES
        }
        shared = union_seconds(
            fixture["_interval"] for fixture in fixtures
            if fixture["nodeid"] == nodeid and fixture["scope"] in SHARED_SCOPES
        )
        reference = test or next(span for span in own_phases.values() if span)
        rows.append({
            "nodeid": nodeid,
            "test_key": phases.canonical_test_key(nodeid),
            "worker": reference["worker"] or "",
            "status": (test or reference)["status"] or "",
            "started_at": reference["started_at"] or "",
            "duration_s": duration,
            "setup_s": phase_seconds["setup"],
            "call_s": phase_seconds["call"],
            "teardown_s": phase_seconds["teardown"],
            "wait_s": round(sum(wait_by_phase.values()), 3),
            "wait_setup_s": wait_by_phase["setup"],
            "wait_call_s": wait_by_phase["call"],
            "wait_teardown_s": wait_by_phase["teardown"],
            "shared_setup_s": shared,
            "waits": len(test_waits),
        })
    for row in waits + fixtures:
        row.pop("_interval", None)
        row["test_key"] = phases.canonical_test_key(row["nodeid"]) if row["nodeid"] else ""
    return rows, waits, fixtures, unattributed


def open_db(path):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def captures_to_read(exporter_db, trends_conn, now, limit, capture=None, force=False):
    """The captures this pass reads, newest run first.

    A capture is read when its run finished at least SETTLE_MINUTES ago,
    within Tempo's retention, and it has no complete span_runs row yet (or
    --force). An incomplete row is retried until RETRY_HOURS after the run.
    """
    try:
        conn = sqlite3.connect(f"file:{exporter_db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        logger.error("the exporter DB could not be opened: %s", exc)
        return []
    conn.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "captures" not in tables or "jobs" not in tables:
            return []
        args = []
        where = "c.run_id != ''"
        if capture:
            where += " AND c.capture_id = ?"
            args.append(capture)
        rows = conn.execute(
            f"""
            SELECT c.capture_id, c.run_id, c.run_attempt, c.suite, c.capture_started_at, c.first_seen_at,
                   MIN(j.created_at) AS created_at, MAX(j.updated_at) AS updated_at,
                   SUM(CASE WHEN COALESCE(j.conclusion, '') = '' THEN 1 ELSE 0 END) AS running,
                   COUNT(j.id) AS job_rows
              FROM captures c
              LEFT JOIN jobs j ON j.run_id = c.run_id AND j.category = 'e2e'
             WHERE {where}
             GROUP BY c.capture_id
            """,
            args,
        ).fetchall()
    except sqlite3.Error as exc:
        logger.error("the exporter DB could not be read: %s", exc)
        return []
    finally:
        conn.close()
    done = {
        row["capture_id"]: dict(row)
        for row in trends_conn.execute("SELECT capture_id, complete, rolled_at, created_at FROM span_runs")
    }
    settle = timedelta(minutes=SETTLE_MINUTES)
    chosen = []
    for row in rows:
        row = dict(row)
        if not row["job_rows"] or row["running"]:
            continue
        finished = phases.parse_time(row["updated_at"])
        created = phases.parse_time(row["created_at"]) or phases.parse_time(row["capture_started_at"])
        if finished is None or created is None:
            continue
        if now - finished < settle and not force:
            continue
        if now - created > timedelta(days=TEMPO_RETENTION_DAYS):
            continue
        previous = done.get(row["capture_id"])
        if previous and not force:
            if previous["complete"]:
                continue
            if now - finished > timedelta(hours=RETRY_HOURS):
                continue
        chosen.append(row)
    chosen.sort(key=lambda row: (row["created_at"] or "", row["capture_id"]), reverse=True)
    return chosen[:limit]


def read_capture(view, row, now):
    """(span_runs row, tests, waits, fixtures) for one capture."""
    capture_id = row["capture_id"]
    created = row["created_at"] or row["capture_started_at"] or row["first_seen_at"] or ""
    finished = phases.parse_time(row["updated_at"])
    summary = {
        "capture_id": capture_id,
        "run_id": int(row["run_id"]) if str(row["run_id"]).isdigit() else None,
        "run_attempt": int(row["run_attempt"]) if str(row["run_attempt"] or "").isdigit() else None,
        "suite": row["suite"] or "",
        "day": created[:10],
        "created_at": created,
        "trace_id": "",
        "state": "unknown",
        "complete": 0,
        "spans_total": None,
        "tests": None,
        "waits": None,
        "session_s": None,
        "unattributed_waits": None,
        "rolled_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "notes": "",
    }
    given_up = finished is not None and now - finished > timedelta(hours=RETRY_HOURS)
    identity, registry = view._registry(capture_id)
    trace_id = str((registry or {}).get("session_trace_id") or "").strip()
    if not trace_id:
        window = view._run_window(summary["run_id"], summary["run_attempt"], registry)
        if window is None:
            summary.update(state="missing", complete=1, notes="no run window to search Tempo in")
            return summary, [], [], []
        trace_id, state, notes = view._discover_trace(capture_id, window, registry_present=registry is not None)
        if not trace_id:
            summary.update(state=state, complete=1 if given_up else 0, notes="; ".join(notes)[:500])
            return summary, [], [], []
    summary["trace_id"] = trace_id
    try:
        payload = view._fetch_trace(trace_id)
    except (TempoBusy, TempoMissing, TempoTooLarge, TempoError) as error:
        state = "too_large" if isinstance(error, TempoTooLarge) else "unavailable"
        summary.update(state=state, complete=1 if given_up or state == "too_large" else 0, notes=str(error)[:500])
        return summary, [], [], []
    entries = []
    total = 0
    for span, resource in view._iter_spans(payload):
        total += 1
        entry = view._span_entry(span, resource)
        if entry is not None:
            entries.append(entry)
    view._resolve_workers(entries)
    tests, waits, fixtures, unattributed = breakdown(entries)
    session = next((entry for entry in entries if entry["class"] == "session" and entry["name"] == "test run"), None)
    complete = session is not None
    summary.update(
        state="ok" if complete else "no_session",
        complete=1 if complete or given_up else 0,
        spans_total=total,
        tests=len(tests),
        waits=len(waits),
        session_s=_seconds(session) if session else None,
        unattributed_waits=unattributed,
        notes="" if complete else "the trace has no 'test run' session span yet",
    )
    return summary, tests, waits, fixtures


def write_capture(conn, summary, tests, waits, fixtures):
    """Replace one capture's rows in one transaction."""
    common = {
        "capture_id": summary["capture_id"],
        "run_id": summary["run_id"],
        "day": summary["day"],
        "suite": summary["suite"],
    }
    with conn:
        for table in ("test_spans", "wait_spans", "fixture_spans"):
            conn.execute(f"DELETE FROM {table} WHERE capture_id = ?", (summary["capture_id"],))
        conn.executemany(
            "INSERT OR REPLACE INTO test_spans (capture_id, run_id, day, created_at, suite, test_key, nodeid,"
            " worker, status, started_at, duration_s, setup_s, call_s, teardown_s, wait_s, wait_setup_s,"
            " wait_call_s, wait_teardown_s, shared_setup_s, waits) VALUES (:capture_id, :run_id, :day,"
            " :created_at, :suite, :test_key, :nodeid, :worker, :status, :started_at, :duration_s, :setup_s,"
            " :call_s, :teardown_s, :wait_s, :wait_setup_s, :wait_call_s, :wait_teardown_s, :shared_setup_s,"
            " :waits)",
            [{**common, "created_at": summary["created_at"], **row} for row in tests],
        )
        conn.executemany(
            "INSERT INTO wait_spans (capture_id, run_id, day, created_at, suite, test_key, nodeid, phase, name,"
            " signature, resource_kind, started_at, duration_s, attempts, condition_met, failed) VALUES"
            " (:capture_id, :run_id, :day, :created_at, :suite, :test_key, :nodeid, :phase, :name, :signature,"
            " :resource_kind, :started_at, :duration_s, :attempts, :condition_met, :failed)",
            [{**common, "created_at": summary["created_at"], **row} for row in waits],
        )
        conn.executemany(
            "INSERT INTO fixture_spans (capture_id, run_id, day, suite, test_key, nodeid, fixture, scope,"
            " started_at, duration_s) VALUES (:capture_id, :run_id, :day, :suite, :test_key, :nodeid,"
            " :fixture, :scope, :started_at, :duration_s)",
            [{**common, **row} for row in fixtures],
        )
        conn.execute(
            "INSERT OR REPLACE INTO span_runs (capture_id, run_id, run_attempt, suite, day, created_at, trace_id,"
            " state, complete, spans_total, tests, waits, session_s, unattributed_waits, rolled_at, notes)"
            " VALUES (:capture_id, :run_id, :run_attempt, :suite, :day, :created_at, :trace_id, :state,"
            " :complete, :spans_total, :tests, :waits, :session_s, :unattributed_waits, :rolled_at, :notes)",
            summary,
        )


def prune(conn, cutoff_day):
    with conn:
        for table in ("span_runs", "test_spans", "wait_spans", "fixture_spans"):
            conn.execute(f"DELETE FROM {table} WHERE day < ?", (cutoff_day,))


def main(argv=None, now=None):
    parser = argparse.ArgumentParser(description="Per-test time breakdown from the ci-observe traces.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="captures to read this pass")
    parser.add_argument("--capture", default=None, help="read one capture id")
    parser.add_argument("--force", action="store_true", help="read captures again even when complete")
    parser.add_argument("--exporter-db", default=None)
    parser.add_argument("--trends-db", default=None)
    parser.add_argument("--tempo-url", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")

    exporter_db = args.exporter_db or EXPORTER_DB
    trends_db = args.trends_db or TRENDS_DB
    now = now or datetime.now(UTC)
    if not os.path.exists(exporter_db):
        logger.error("the exporter DB %s does not exist", exporter_db)
        return 2
    conn = open_db(trends_db)
    try:
        prune(conn, (now - timedelta(days=KEEP_DAYS)).date().isoformat())
        rows = captures_to_read(exporter_db, conn, now, max(1, args.limit), args.capture, args.force)
        view = CIObserveCaptureView(_ReadOnlyExporter(exporter_db), tempo_url=args.tempo_url)
        states = {}
        for row in rows:
            summary, tests, waits, fixtures = read_capture(view, row, now)
            write_capture(conn, summary, tests, waits, fixtures)
            states[summary["state"]] = states.get(summary["state"], 0) + 1
            logger.info(
                "  %s: %s, %s test(s), %s wait(s)",
                summary["capture_id"], summary["state"], summary["tests"], summary["waits"],
            )
        logger.info("read %d capture(s): %s", len(rows), states or "nothing new")
        phases.record_pass(conn, "spans", now, f"{len(rows)} capture(s) read")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
