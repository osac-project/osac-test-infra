"""Tests for the ci-observe Capture document (ci_observe_capture_view.py).

The script is loaded from its path like the other monitoring tests: its
file name is hyphen-free, but it is still run from scripts/, not imported
as a package.

No live Tempo is used. ``FakeTempo`` serves raw v1 trace payloads and
search answers. The three payloads below reproduce the live fixtures named
in the read-path spec: the VMaaS trace at 1854 spans (37 parent tests,
148 test spans, 338 fixtures, 1154 fixture uses, 209 waits) and the CaaS
trace with 8 tests. The builders are deterministic, so every assertion is
stable. A real captured payload can replace a builder by dropping
``<capture id>.json`` into monitoring/tests/fixtures/ci_observe_capture_view/.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import itertools
import json
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "ci_observe_capture_view"


def _load_capture_view():
    name = "ci_observe_capture_view"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / "ci_observe_capture_view.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


capture_view = _load_capture_view()

CAAS_CAPTURE = "caas-main-41709318840-a1"
VMAAS_CAPTURE = "vmaas-main-41709279176-a1"
VMAAS_41818898681 = "vmaas-main-41818898681-a1"

CAAS_TESTS = [
    ("tests/e2e/caas/sanity/test_cluster_create.py", "test_cluster_create"),
    (
        "tests/e2e/caas/sanity/test_cluster_delete_feedback_light.py",
        "test_cluster_delete_reports_deleting_state_without_provisioning",
    ),
    ("tests/e2e/caas/sanity/test_cluster_scale.py", "test_cluster_scale_up"),
    ("tests/e2e/caas/sanity/test_cluster_scale.py", "test_cluster_scale_down"),
    ("tests/e2e/caas/sanity/test_cluster_upgrade.py", "test_cluster_upgrade"),
    ("tests/e2e/caas/sanity/test_node_pool.py", "test_node_pool_create"),
    ("tests/e2e/caas/sanity/test_node_pool.py", "test_node_pool_delete"),
    ("tests/e2e/caas/sanity/test_cluster_delete.py", "test_cluster_delete"),
]

BASE_TIME = datetime(2026, 11, 3, 20, 57, 34, tzinfo=UTC)


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def trace_id_for(capture_id):
    return hashlib.md5(capture_id.encode()).hexdigest()


def _ns(moment):
    return str(int(moment.timestamp() * 1_000_000_000))


def _attr(key, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _span(span_id, parent_id, name, start, duration_seconds, attributes, status="unset", trace_id=""):
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "parentSpanId": parent_id,
        "name": name,
        "kind": 1,
        "startTimeUnixNano": _ns(start),
        "endTimeUnixNano": _ns(start + timedelta(seconds=duration_seconds)),
        "attributes": [_attr(key, value) for key, value in attributes.items()],
        "status": {"code": 2 if status == "error" else 1 if status == "ok" else 0},
    }


def _test_names(capture_id, count):
    if capture_id.startswith("caas-"):
        names = list(CAAS_TESTS)
        while len(names) < count:
            names.append((f"tests/e2e/caas/sanity/test_extra_{len(names)}.py", f"test_extra_{len(names)}"))
        return names[:count]
    return [(f"tests/e2e/vmaas/sanity/test_case_{index}.py", f"test_case_{index}") for index in range(count)]


def build_trace(
    capture_id,
    *,
    parent_tests,
    workers,
    fixtures,
    fixture_uses,
    waits,
    service_spans,
    session_seconds=1794.0,
    failed_index=3,
    include_session=True,
    malformed=False,
    base_time=None,
):
    """A raw v1 Tempo payload with deterministic span counts."""
    base_time = base_time or BASE_TIME
    trace_id = trace_id_for(capture_id)
    suite = "caas" if capture_id.startswith("caas-") else "vmaas"
    counter = itertools.count(1)

    def span_id():
        return f"{next(counter):016x}"

    pytest_spans = []
    root_id = span_id()
    if include_session:
        pytest_spans.append(
            _span(
                root_id,
                "",
                "test run",
                base_time,
                session_seconds,
                {"pytest.span_type": "run"},
                status="error" if failed_index is not None else "ok",
                trace_id=trace_id,
            )
        )
    worker_ids = []
    for index in range(workers):
        worker_id = span_id()
        worker_ids.append(worker_id)
        pytest_spans.append(
            _span(
                worker_id,
                root_id if include_session else "",
                f"test worker gw{index}",
                base_time + timedelta(seconds=1),
                session_seconds - 2,
                {"pytest.span_type": "run"},
                status="ok",
                trace_id=trace_id,
            )
        )

    names = _test_names(capture_id, parent_tests)
    test_ids = []
    spacing = max(1, int(session_seconds // (parent_tests + 1)))
    for index, (path, function) in enumerate(names):
        test_id = span_id()
        test_ids.append(test_id)
        nodeid = f"{path}::{function}"
        start = base_time + timedelta(seconds=5 + index * spacing)
        duration = 8 + index % 7
        status = "error" if index == failed_index else "ok"
        attributes = {
            "pytest.span_type": "test",
            "pytest.nodeid": nodeid,
            "code.function": function,
            "code.filepath": path,
        }
        if index % 2 == 0:
            attributes["osac.ci.xdist_worker"] = f"gw{index % workers}"
        pytest_spans.append(
            _span(
                test_id,
                worker_ids[index % workers],
                nodeid,
                start,
                duration,
                attributes,
                status=status,
                trace_id=trace_id,
            )
        )
        for phase in ("setup", "call", "teardown"):
            pytest_spans.append(
                _span(
                    span_id(),
                    test_id,
                    f"{nodeid}::{phase}",
                    start,
                    duration / 3,
                    attributes,
                    status="error" if phase == "call" and index == failed_index else status,
                    trace_id=trace_id,
                )
            )

    for index in range(fixtures):
        parent = test_ids[index % len(test_ids)]
        name = f"fixture_{index} setup"
        start = base_time + timedelta(seconds=4 + index)
        pytest_spans.append(
            _span(
                span_id(),
                parent,
                name,
                start,
                0.5 + (index % 5),
                {
                    "pytest.span_type": "fixture",
                    "code.function": f"fixture_{index}",
                    "pytest.fixture_scope": "function",
                },
                status="ok",
                trace_id=trace_id,
            )
        )

    for index in range(fixture_uses):
        parent = test_ids[index % len(test_ids)]
        name = f"fixture_{index % max(1, fixtures)}"
        start = base_time + timedelta(seconds=6 + index)
        path, function = names[index % len(names)]
        pytest_spans.append(
            _span(
                span_id(),
                parent,
                name,
                start,
                0.01,
                {
                    "pytest.span_type": "fixture_use",
                    "pytest.fixture_name": name,
                    "pytest.nodeid": f"{path}::{function}",
                },
                status="ok",
                trace_id=trace_id,
            )
        )

    for index in range(waits):
        parent = test_ids[index % len(test_ids)]
        start = base_time + timedelta(seconds=7 + index)
        pytest_spans.append(
            _span(
                span_id(),
                parent,
                f"wait for resource {index}",
                start,
                1.5 + (index % 20),
                {
                    "pytest.span_type": "wait",
                    "poll.delay": 2,
                    "poll.attempts": 1 + index % 12,
                    "poll.condition_met": True,
                    "osac.wait.resource_kind": "ComputeInstance",
                    "osac.wait.resource_id": f"01a1a33a-{index:04d}",
                },
                status="ok",
                trace_id=trace_id,
            )
        )

    service = []
    for index in range(service_spans):
        parent = test_ids[index % len(test_ids)]
        start = base_time + timedelta(seconds=8 + index)
        service.append(
            _span(
                span_id(),
                parent,
                f"GET /api/v1/resource/{index}",
                start,
                0.05,
                {"http.request.method": "GET"},
                status="ok",
                trace_id=trace_id,
            )
        )

    resource = {
        "osac.ci.capture_id": capture_id,
        "osac.ci.suite": suite,
        "osac.ci.pr": "none",
        "osac.ci.run_attempt": "1",
        "service.name": "tests",
    }
    batches = [
        {
            "resource": {"attributes": [_attr(key, value) for key, value in resource.items()]},
            "scopeSpans": [{"scope": {"name": "pytest-opentelemetry"}, "spans": pytest_spans}],
        }
    ]
    if service:
        service_resource = dict(resource)
        service_resource["service.name"] = "beyla"
        batches.append(
            {
                "resource": {"attributes": [_attr(key, value) for key, value in service_resource.items()]},
                "scopeSpans": [{"scope": {"name": "beyla"}, "spans": service}],
            }
        )
    if malformed:
        batches[0]["scopeSpans"][0]["spans"].append("not a span")
    return {"batches": batches}


def raw_span_count(payload):
    return sum(
        len(scope.get("spans") or [])
        for batch in payload.get("batches") or []
        for scope in batch.get("scopeSpans") or []
    )


def fixture_trace(capture_id):
    """The payload for a named fixture capture, from disk when one was captured."""
    path = FIXTURE_DIR / f"{capture_id}.json"
    if path.exists():
        return json.loads(path.read_text())
    shapes = {
        CAAS_CAPTURE: dict(
            parent_tests=8,
            workers=1,
            fixtures=20,
            fixture_uses=30,
            waits=15,
            service_spans=50,
            session_seconds=2463.0,
            failed_index=None,
        ),
        VMAAS_CAPTURE: dict(
            parent_tests=25,
            workers=4,
            fixtures=120,
            fixture_uses=300,
            waits=60,
            service_spans=100,
            session_seconds=2100.0,
            failed_index=None,
        ),
        VMAAS_41818898681: dict(
            parent_tests=37,
            workers=4,
            fixtures=338,
            fixture_uses=1154,
            waits=209,
            service_spans=0,
            session_seconds=1794.0,
            failed_index=3,
        ),
    }
    return build_trace(capture_id, **shapes[capture_id])


class FakeTempo:
    """A fake Tempo that serves search answers and raw v1 traces."""

    def __init__(
        self, traces=None, search_handler=None, search_error=None, trace_error=None, search_delay=0.0, trace_delay=0.0
    ):
        self.traces = traces or {}
        self.search_handler = search_handler
        self.search_error = search_error
        self.trace_error = trace_error
        self.search_delay = search_delay
        self.trace_delay = trace_delay
        self.calls = []
        self._lock = threading.Lock()

    # -- helpers towards tests --
    def urls(self, fragment):
        with self._lock:
            return [url for url in self.calls if fragment in url]

    def query(self, index=0):
        search_calls = self.urls("/api/search?")
        parsed = urllib.parse.parse_qs(urllib.parse.urlsplit(search_calls[index]).query)
        return parsed

    # -- the injected _http_get --
    def get(self, url, *, timeout, max_bytes):
        with self._lock:
            self.calls.append(url)
        if "/api/search?" in url:
            if self.search_delay:
                time.sleep(self.search_delay)
            if isinstance(self.search_error, BaseException):
                raise self.search_error
            parsed = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            query = parsed.get("q", [""])[0]
            if self.search_handler is not None:
                trace_ids = self.search_handler(query, parsed)
            else:
                trace_ids = self._default_search(query)
            body = json.dumps({"traces": [{"traceID": entry} for entry in trace_ids]}).encode()
            if len(body) > max_bytes:
                raise capture_view.TempoTooLarge("the search answer is too large")
            return body

        if "/api/traces/" in url:
            if self.trace_delay:
                time.sleep(self.trace_delay)
            if isinstance(self.trace_error, BaseException):
                raise self.trace_error
            trace_id = urllib.parse.unquote(url.rsplit("/", 1)[-1])
            payload = self.traces.get(trace_id)
            if payload is None:
                raise capture_view.TempoMissing("HTTP 404: trace not found")
            body = json.dumps(payload).encode()
            if len(body) > max_bytes:
                raise capture_view.TempoTooLarge("the trace answer is too large")
            return body

        raise AssertionError(f"unrouted Tempo URL: {url}")

    def _default_search(self, query):
        match = re.search(r'osac\.ci\.capture_id"="([^"]+)"', query)
        if match is None or 'name="test run"' not in query:
            return []
        trace_id = trace_id_for(match.group(1))
        return [trace_id] if trace_id in self.traces else []


class FakeEvidenceCache:
    """The already-populated part of CIObserveRunEvidence's BoundedCache."""

    def __init__(self, entries=None):
        self.entries = entries or {}
        self.reads = []

    def get(self, key):
        self.reads.append(key)
        return self.entries.get(key)


class FakeEvidence:
    """Cache-only stand-in: any artifact request is a test failure."""

    def __init__(self, *, run_id=None, run_attempt=1, native=None):
        entries = {}
        if native is not None:
            entries[("native", run_id, run_attempt)] = native
        self._cache = FakeEvidenceCache(entries)
        self.artifact_requests = 0

    def get_ci_observe_run_json(self, params):
        self.artifact_requests += 1
        raise AssertionError("the capture endpoint must not download an artifact")


class FakeExporter:
    def __init__(self, db_path, evidence=None):
        self.db_path = db_path
        self._run_evidence = evidence

    def _db(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection


JOB_COLUMNS = [
    ("id", "INTEGER PRIMARY KEY"),
    ("repo", "TEXT"),
    ("workflow", "TEXT"),
    ("display_name", "TEXT"),
    ("category", "TEXT"),
    ("branch", "TEXT"),
    ("status", "TEXT"),
    ("conclusion", "TEXT"),
    ("duration_s", "INTEGER"),
    ("url", "TEXT"),
    ("created_at", "TEXT"),
    ("updated_at", "TEXT"),
    ("run_attempt", "INTEGER"),
]

CAPTURE_COLUMNS = [
    ("capture_id", "TEXT PRIMARY KEY"),
    ("run_id", "TEXT NOT NULL DEFAULT ''"),
    ("run_attempt", "TEXT NOT NULL DEFAULT ''"),
    ("suite", "TEXT NOT NULL DEFAULT ''"),
    ("pr", "TEXT NOT NULL DEFAULT ''"),
    ("workflow", "TEXT NOT NULL DEFAULT ''"),
    ("test_ref", "TEXT NOT NULL DEFAULT ''"),
    ("test_sha", "TEXT NOT NULL DEFAULT ''"),
    ("test_infra_ref", "TEXT NOT NULL DEFAULT ''"),
    ("test_infra_sha", "TEXT NOT NULL DEFAULT ''"),
    ("test_infra_workflow_sha", "TEXT NOT NULL DEFAULT ''"),
    ("test_suite", "TEXT NOT NULL DEFAULT ''"),
    ("test_filter", "TEXT NOT NULL DEFAULT ''"),
    ("test_marker", "TEXT NOT NULL DEFAULT ''"),
    ("session_trace_id", "TEXT NOT NULL DEFAULT ''"),
    ("identity_source", "TEXT NOT NULL DEFAULT 'none'"),
    ("capture_started_at", "TEXT"),
    ("first_seen_at", "TEXT NOT NULL DEFAULT ''"),
    ("last_seen_at", "TEXT NOT NULL DEFAULT ''"),
]


def make_db(tmp_path, *, jobs=(), captures=None, run_id_column=False):
    path = tmp_path / "workflow-exporter.db"
    connection = sqlite3.connect(path)
    columns = list(JOB_COLUMNS)
    if run_id_column:
        columns.append(("run_id", "TEXT"))
    connection.execute("CREATE TABLE jobs (" + ", ".join(f"{name} {kind}" for name, kind in columns) + ")")
    for job in jobs:
        _insert(connection, "jobs", job)
    if captures is not None:
        connection.execute(
            "CREATE TABLE captures (" + ", ".join(f"{name} {kind}" for name, kind in CAPTURE_COLUMNS) + ")"
        )
        for row in captures:
            _insert(connection, "captures", row)
    connection.commit()
    connection.close()
    return path


def _insert(connection, table, record):
    names = ", ".join(record)
    placeholders = ", ".join("?" for _ in record)
    connection.execute(f"INSERT INTO {table} ({names}) VALUES ({placeholders})", tuple(record.values()))


def make_job(run_id, created_at, *, attempt=1, suite_run=False):
    url = f"https://github.com/osac-project/osac/actions/runs/{run_id}"
    job_id = run_id
    if suite_run:
        job_id = run_id + 1000
        url = f"https://github.com/osac-project/osac/actions/runs/{run_id}/job/{job_id}"
    start = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return {
        "id": job_id,
        "repo": "osac",
        "workflow": "E2E VMaaS Full Install",
        "display_name": "osac / E2E VMaaS Full Install",
        "category": "e2e",
        "branch": "main",
        "status": "completed",
        "conclusion": "failure",
        "duration_s": 4447,
        "url": url,
        "created_at": created_at,
        "updated_at": iso(start + timedelta(seconds=4447)),
        "run_attempt": attempt,
    }


def make_capture(capture_id, run_id, *, attempt=1, started_at=None, trace_id="", **overrides):
    row = {name: "" for name, _ in CAPTURE_COLUMNS}
    row.update(
        {
            "capture_id": capture_id,
            "run_id": str(run_id),
            "run_attempt": str(attempt),
            "suite": "caas" if capture_id.startswith("caas-") else "vmaas",
            "pr": "none",
            "workflow": "E2E VMaaS Full Install",
            "test_ref": "main",
            "test_sha": "a" * 40,
            "test_infra_ref": "main",
            "test_infra_sha": "b" * 40,
            "test_infra_workflow_sha": "c" * 40,
            "test_suite": "tests/e2e/",
            "test_filter": "none",
            "test_marker": "none",
            "session_trace_id": trace_id,
            "identity_source": "info",
            "capture_started_at": started_at,
            "first_seen_at": started_at or iso(BASE_TIME),
            "last_seen_at": started_at or iso(BASE_TIME),
        }
    )
    row.update(overrides)
    return row


def build_view(tmp_path, tempo, *, jobs=(), captures=None, run_id_column=False, evidence=None, monkeypatch=None):
    db_path = make_db(tmp_path, jobs=jobs, captures=captures, run_id_column=run_id_column)
    view = capture_view.CIObserveCaptureView(FakeExporter(db_path, evidence=evidence), tempo_url="http://tempo.test")
    if monkeypatch is not None:
        monkeypatch.setattr(capture_view, "_http_get", tempo.get)
    return view, FakeExporter(db_path, evidence=evidence)


def fetch(view, capture_id):
    return view.get_json({"capture_id": [capture_id]})


# ---------------------------------------------------------------------------
# Completeness against the raw trace
# ---------------------------------------------------------------------------


PHASE_SUFFIXES = ("::setup", "::call", "::teardown")


def raw_span_classes(payload):
    """Counts read from the raw payload, independent of the module's parser."""
    counts = {"parent_tests": 0, "test": 0, "fixture": 0, "fixture_use": 0, "wait": 0, "session": 0, "other": 0}
    workers = set()
    session_spans = 0
    for batch in payload.get("batches") or []:
        resource = {
            entry["key"]: next(iter(entry["value"].values()))
            for entry in (batch.get("resource") or {}).get("attributes") or []
        }
        for scope in batch.get("scopeSpans") or []:
            for span in scope.get("spans") or []:
                if not isinstance(span, dict):
                    continue
                attributes = dict(resource)
                attributes.update(
                    {entry["key"]: next(iter(entry["value"].values())) for entry in span.get("attributes") or []}
                )
                span_type = attributes.get("pytest.span_type")
                name = span.get("name") or ""
                if span_type == "test":
                    counts["test"] += 1
                    if not name.endswith(PHASE_SUFFIXES):
                        counts["parent_tests"] += 1
                elif span_type in ("fixture", "fixture_use", "wait"):
                    counts[span_type] += 1
                elif span_type == "run":
                    counts["session"] += 1
                    if name == "test run":
                        session_spans += 1
                    match = re.match(r"^test worker (\S+)$", name)
                    if match:
                        workers.add(match.group(1))
                elif not span_type and "poll.delay" in attributes:
                    counts["wait"] += 1
                else:
                    counts["other"] += 1
                worker = attributes.get("osac.ci.xdist_worker")
                if worker:
                    workers.add(str(worker))
    counts["session_spans"] = session_spans
    counts["workers"] = workers
    return counts


@pytest.mark.parametrize("capture_id", [CAAS_CAPTURE, VMAAS_CAPTURE, VMAAS_41818898681])
def test_live_fixture_trace_is_complete(capture_id, tmp_path, monkeypatch):
    payload = fixture_trace(capture_id)
    total = raw_span_count(payload)
    expected = raw_span_classes(payload)
    run_id = int(re.search(r"-(\d+)-a\d+$", capture_id).group(1))
    tempo = FakeTempo({trace_id_for(capture_id): payload})
    view, _ = build_view(tmp_path, tempo, jobs=[make_job(run_id, "2026-11-03T21:00:00Z")], monkeypatch=monkeypatch)

    document = fetch(view, capture_id)

    assert document["state"] == "ok"
    assert document["coverage"]["spans_total"] == total
    assert document["coverage"]["spans_classified"] + document["coverage"]["spans_unclassified"] == total
    assert document["coverage"]["spans_unclassified"] == expected["other"]
    assert document["coverage"]["session_span"] == expected["session_spans"]
    assert len(document["tests"]) == expected["parent_tests"]
    assert document["session"]["tests"] == expected["parent_tests"]
    assert document["session"]["test_attempts"] == expected["test"]
    assert len(document["fixtures"]) == expected["fixture"]
    assert len(document["fixture_uses"]) == expected["fixture_use"]
    assert len(document["waits"]) == expected["wait"]
    assert document["session"]["span_total"] == total
    assert document["session"]["workers"] == sorted(expected["workers"])
    assert document["session"]["trace_id"] == trace_id_for(capture_id)
    assert all(row["worker"] for row in document["tests"])
    assert len(tempo.urls("/api/search?")) == 1
    assert len(tempo.urls("/api/traces/")) == 1


def test_caas_trace_has_eight_tests(tmp_path, monkeypatch):
    payload = fixture_trace(CAAS_CAPTURE)
    tempo = FakeTempo(
        {trace_id_for(CAAS_CAPTURE): payload},
        search_handler=lambda query, params: [trace_id_for(CAAS_CAPTURE)] if 'name="test run"' in query else [],
    )
    view, _ = build_view(tmp_path, tempo, jobs=[make_job(41709318840, "2026-11-03T21:00:00Z")], monkeypatch=monkeypatch)

    document = fetch(view, CAAS_CAPTURE)

    assert document["state"] == "ok"
    assert document["session"]["tests"] == 8
    assert len(document["tests"]) == 8
    # The spec's failure mode: a search returns one test span, not eight.
    search = tempo.query()
    assert search["limit"] == ["10"]
    assert search["spss"] == ["10"]
    # TraceQL needs the braces; without them Tempo answers HTTP 400.
    assert search["q"] == [
        '{ resource."osac.ci.capture_id"="caas-main-41709318840-a1" && name="test run" }'
    ]


def test_a_capture_without_a_session_span_is_cached_only_briefly(tmp_path, monkeypatch):
    """The session span is exported last, so an in-progress run has partial
    tables. Ten minutes of that is a wrong count on an open page."""
    capture_id = CAAS_CAPTURE
    # No "test run" span, which is exactly an in-progress run.
    payload = build_trace(
        capture_id, parent_tests=8, workers=1, fixtures=2, fixture_uses=3,
        waits=1, service_spans=0, session_seconds=2463.0, include_session=False,
    )
    tempo = FakeTempo({trace_id_for(capture_id): payload})
    view, _ = build_view(tmp_path, tempo, jobs=[make_job(41709318840, "2026-11-03T21:00:00Z")], monkeypatch=monkeypatch)

    document = fetch(view, capture_id)
    assert document["state"] == "ok"
    assert document["coverage"]["session_span"] == 0
    remaining = view._cache[capture_id]["expires_at"] - time.monotonic()
    assert remaining <= view.INCOMPLETE_CACHE_TTL_SECONDS + 1


def test_a_capture_without_a_session_span_that_is_recent_reports_running(tmp_path, monkeypatch):
    """A live run has no session span yet. Saying "no result" makes it look
    like data was lost, so report the state the reader can act on."""
    capture_id = CAAS_CAPTURE
    started = datetime.now(UTC) - timedelta(minutes=1)
    payload = build_trace(
        capture_id, parent_tests=1, workers=1, fixtures=0, fixture_uses=0,
        waits=0, service_spans=0, session_seconds=30.0, include_session=False,
        base_time=started,
    )
    tempo = FakeTempo({trace_id_for(capture_id): payload})
    live_job = make_job(41709318840, iso(started))
    live_job.update({"status": "in_progress", "conclusion": ""})
    view, _ = build_view(
        tmp_path,
        tempo,
        jobs=[live_job],
        captures=[make_capture(capture_id, 41709318840, started_at=iso(started))],
        monkeypatch=monkeypatch,
    )

    document = fetch(view, capture_id)

    assert document["state"] == "ok"
    assert document["coverage"]["session_span"] == 0
    assert document["session"]["status"] == "running"
    assert document["summary"][0]["status"] == "running"
    assert any("still running" in note for note in document["notes"])


def test_the_summary_uses_github_when_a_setup_error_misses_the_session_span(tmp_path, monkeypatch):
    """A setup or teardown failure can fail the job without marking the
    pytest session span as an error. GitHub owns the result."""
    capture_id = CAAS_CAPTURE
    payload = build_trace(
        capture_id, parent_tests=1, workers=1, fixtures=0, fixture_uses=0,
        waits=0, service_spans=0, session_seconds=30.0, failed_index=None,
    )
    tempo = FakeTempo({trace_id_for(capture_id): payload})
    view, _ = build_view(
        tmp_path,
        tempo,
        jobs=[make_job(41709318840, "2026-11-03T21:00:00Z")],
        monkeypatch=monkeypatch,
    )

    document = fetch(view, capture_id)

    assert document["session"]["status"] == "ok", "the trace alone says the session passed"
    assert document["summary"][0]["status"] == "error", "the job failed in setup or teardown"


def test_a_transient_tempo_failure_is_cached_only_briefly(tmp_path, monkeypatch):
    """A 30-second Tempo restart must not pin the Capture page at `unknown`
    for the full ten-minute TTL."""
    capture_id = VMAAS_CAPTURE
    tempo = FakeTempo({}, search_error=capture_view.TempoError("HTTP 503"))
    view, _ = build_view(tmp_path, tempo, jobs=[make_job(41709279176, "2026-11-03T21:00:00Z")], monkeypatch=monkeypatch)

    document = fetch(view, capture_id)

    assert document["state"] == "unknown"
    assert document["transient"] is True
    entry = view._cache[capture_id]
    remaining = entry["expires_at"] - time.monotonic()
    assert remaining <= view.NEGATIVE_CACHE_TTL_SECONDS + 1


def test_a_rerun_searches_from_the_attempts_start(tmp_path, monkeypatch):
    """created_at is fixed across re-runs, so a re-run that starts a day
    later falls outside [created_at - 5m, created_at + 8h]. The window must
    start at this attempt's run_started_at, which is updated_at - duration_s."""
    capture_id = "vmaas-main-41709279176-a2"
    payload = fixture_trace(VMAAS_CAPTURE)
    tempo = FakeTempo({trace_id_for(capture_id): payload})
    job = make_job(41709279176, iso(BASE_TIME - timedelta(days=1)), attempt=2)
    job["updated_at"] = iso(BASE_TIME + timedelta(seconds=60))
    job["duration_s"] = 60
    view, _ = build_view(
        tmp_path,
        tempo,
        jobs=[job],
        captures=[make_capture(capture_id, 41709279176, attempt=2)],
        monkeypatch=monkeypatch,
    )

    document = fetch(view, capture_id)

    assert document["state"] == "ok"
    window = tempo.query()
    assert int(window["start"][0]) == int((BASE_TIME - view.WINDOW_BEFORE).timestamp())


def test_the_first_attempt_searches_from_created_at(tmp_path, monkeypatch):
    capture_id = VMAAS_CAPTURE
    tempo = FakeTempo({trace_id_for(capture_id): fixture_trace(capture_id)})
    job = make_job(41709279176, iso(BASE_TIME), attempt=1)
    job["updated_at"] = iso(BASE_TIME + timedelta(days=1))
    view, _ = build_view(
        tmp_path,
        tempo,
        jobs=[job],
        captures=[make_capture(capture_id, 41709279176, attempt=1)],
        monkeypatch=monkeypatch,
    )

    assert fetch(view, capture_id)["state"] == "ok"
    window = tempo.query()
    assert int(window["start"][0]) == int((BASE_TIME - view.WINDOW_BEFORE).timestamp())


def test_an_unknown_attempt_falls_back_to_created_at(tmp_path, monkeypatch):
    """The job history has attempt 2, the URL asks for 3. Search from the
    run's creation rather than declare a capture missing."""
    capture_id = "vmaas-main-41709279176-a3"
    tempo = FakeTempo({trace_id_for(capture_id): fixture_trace(VMAAS_CAPTURE)})
    job = make_job(41709279176, iso(BASE_TIME), attempt=2)
    job["updated_at"] = iso(BASE_TIME + timedelta(seconds=60))
    job["duration_s"] = 60
    view, _ = build_view(
        tmp_path,
        tempo,
        jobs=[job],
        captures=[make_capture(capture_id, 41709279176, attempt=3)],
        monkeypatch=monkeypatch,
    )

    assert fetch(view, capture_id)["state"] == "ok"
    window = tempo.query()
    assert int(window["start"][0]) == int((BASE_TIME - view.WINDOW_BEFORE).timestamp())


def test_the_search_query_is_a_braced_spanset(tmp_path, monkeypatch):
    """Tempo 2.10 rejects a bare `resource...` expression with HTTP 400, and
    the Capture page then shows unknown for every capture."""
    capture_id = CAAS_CAPTURE
    tempo = FakeTempo({trace_id_for(capture_id): fixture_trace(capture_id)})
    view, _ = build_view(
        tmp_path,
        tempo,
        jobs=[make_job(41709318840, "2026-11-03T21:00:00Z")],
        monkeypatch=monkeypatch,
    )

    assert fetch(view, capture_id)["state"] == "ok"
    query = tempo.query()["q"][0]
    assert query.startswith("{ ") and query.endswith(" }")
    assert "resource." in query and capture_id in query


def test_response_is_deterministic(tmp_path, monkeypatch):
    payload = fixture_trace(VMAAS_41818898681)
    trace_id = trace_id_for(VMAAS_41818898681)
    tempo = FakeTempo({trace_id: payload})
    view, _ = build_view(
        tmp_path,
        tempo,
        jobs=[make_job(41818898681, "2026-11-03T20:50:00Z")],
        captures=[make_capture(VMAAS_41818898681, 41818898681, trace_id=trace_id)],
        monkeypatch=monkeypatch,
    )

    first = json.dumps(fetch(view, VMAAS_41818898681), sort_keys=True)
    view._cache.clear()
    second = json.dumps(fetch(view, VMAAS_41818898681), sort_keys=True)

    assert first == second


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------


def test_synthetic_id_is_missing_without_a_tempo_call(tmp_path, monkeypatch):
    tempo = FakeTempo({})
    view, _ = build_view(tmp_path, tempo, monkeypatch=monkeypatch)

    document = fetch(view, "caas-main-99999999999-a1")

    assert document["state"] == "missing"
    assert tempo.calls == []
    assert document["tests"] == []
    assert document["notes"]


def test_not_indexed_when_registry_has_a_row_and_search_is_empty(tmp_path, monkeypatch):
    capture_id = VMAAS_CAPTURE
    run_id = 41709279176
    tempo = FakeTempo({})
    view, _ = build_view(
        tmp_path,
        tempo,
        jobs=[make_job(run_id, iso(datetime.now(UTC) - timedelta(minutes=1)))],
        captures=[make_capture(capture_id, run_id)],
        monkeypatch=monkeypatch,
    )

    document = fetch(view, capture_id)

    assert document["state"] == "not_indexed"
    assert len(tempo.urls("/api/search?")) == 2
    assert any("not indexed" in note for note in document["notes"])


def test_not_indexed_when_the_search_returns_a_trace_that_is_not_readable(tmp_path, monkeypatch):
    capture_id = VMAAS_CAPTURE
    trace_id = trace_id_for(capture_id)
    tempo = FakeTempo(
        {trace_id: fixture_trace(capture_id)},
        search_handler=lambda query, params: [trace_id],
        trace_error=capture_view.TempoMissing("HTTP 404: not found"),
    )
    view, _ = build_view(tmp_path, tempo, jobs=[make_job(41709279176, "2026-11-03T21:00:00Z")], monkeypatch=monkeypatch)

    document = fetch(view, capture_id)

    assert document["state"] == "not_indexed"
    assert any("cannot read it yet" in note for note in document["notes"])
    # A search can return a trace the ingester has not flushed yet. Storing
    # it would turn the next transient 404 into a cached `missing`.
    connection = view._connection()
    try:
        row = connection.execute(
            "SELECT session_trace_id FROM captures WHERE capture_id = ?", (capture_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        row = None  # no captures table in this fixture: nothing was stored
    finally:
        connection.close()
    assert row is None or row["session_trace_id"] == ""


def test_busy_when_tempo_refuses_the_search(tmp_path, monkeypatch):
    tempo = FakeTempo({}, search_error=capture_view.TempoBusy("HTTP 429"))
    view, _ = build_view(tmp_path, tempo, jobs=[make_job(41709279176, "2026-11-03T21:00:00Z")], monkeypatch=monkeypatch)

    document = fetch(view, VMAAS_CAPTURE)

    assert document["state"] == "busy"


def test_unknown_when_tempo_fails(tmp_path, monkeypatch):
    tempo = FakeTempo({}, search_error=capture_view.TempoError("HTTP 500"))
    view, _ = build_view(tmp_path, tempo, jobs=[make_job(41709279176, "2026-11-03T21:00:00Z")], monkeypatch=monkeypatch)

    document = fetch(view, VMAAS_CAPTURE)

    assert document["state"] == "unknown"


def test_too_large_trace_is_refused(tmp_path, monkeypatch):
    capture_id = CAAS_CAPTURE
    tempo = FakeTempo({trace_id_for(capture_id): fixture_trace(capture_id)})
    view, _ = build_view(tmp_path, tempo, jobs=[make_job(41709318840, "2026-11-03T21:00:00Z")], monkeypatch=monkeypatch)
    view.MAX_TRACE_BYTES = 64

    document = fetch(view, capture_id)

    assert document["state"] == "too_large"
    assert document["tests"] == []


def test_missing_when_a_registered_capture_is_older_than_tempo_retention(tmp_path, monkeypatch):
    capture_id = VMAAS_CAPTURE
    started = iso(datetime.now(UTC) - timedelta(days=40))
    tempo = FakeTempo({})
    view, _ = build_view(
        tmp_path,
        tempo,
        jobs=[make_job(41709279176, started)],
        captures=[make_capture(capture_id, 41709279176, started_at=started)],
        monkeypatch=monkeypatch,
    )

    document = fetch(view, capture_id)

    assert document["state"] == "missing"
    assert any("retention" in note for note in document["notes"])


def test_missing_when_the_run_is_old_and_unregistered(tmp_path, monkeypatch):
    tempo = FakeTempo({})
    view, _ = build_view(
        tmp_path,
        tempo,
        jobs=[make_job(41709279176, iso(datetime.now(UTC) - timedelta(hours=2)))],
        monkeypatch=monkeypatch,
    )

    document = fetch(view, VMAAS_CAPTURE)

    assert document["state"] == "missing"
    assert any("neither Tempo nor the registry" in note for note in document["notes"])
    remaining = view._cache[VMAAS_CAPTURE]["expires_at"] - time.monotonic()
    assert remaining <= view.NEGATIVE_CACHE_TTL_SECONDS + 1, "a capture that registers later must not stay missing"


def test_unreadable_span_returns_unknown_not_a_short_table(tmp_path, monkeypatch):
    capture_id = CAAS_CAPTURE
    payload = build_trace(
        capture_id, parent_tests=8, workers=1, fixtures=4, fixture_uses=4, waits=2, service_spans=2, malformed=True
    )
    tempo = FakeTempo({trace_id_for(capture_id): payload})
    view, _ = build_view(tmp_path, tempo, jobs=[make_job(41709318840, "2026-11-03T21:00:00Z")], monkeypatch=monkeypatch)

    document = fetch(view, capture_id)

    assert document["state"] == "unknown"
    assert document["tests"] == []
    assert any("did not classify completely" in note for note in document["notes"])


def test_coverage_invariant_failure_returns_unknown(tmp_path, monkeypatch):
    capture_id = CAAS_CAPTURE
    tempo = FakeTempo({trace_id_for(capture_id): fixture_trace(capture_id)})
    view, _ = build_view(tmp_path, tempo, jobs=[make_job(41709318840, "2026-11-03T21:00:00Z")], monkeypatch=monkeypatch)

    def broken(payload):
        return (
            {"tests": 0, "test_attempts": 0, "workers": []},
            {"tests": [], "waits": [], "fixtures": [], "fixture_uses": []},
            {
                "spans_total": 10,
                "spans_classified": 8,
                "spans_unclassified": 1,
                "session_span": 0,
                "service_calls": 1,
                "service_calls_complete": False,
            },
        )

    monkeypatch.setattr(view, "_classify_trace", broken)

    document = fetch(view, capture_id)

    assert document["state"] == "unknown"
    assert any("did not classify completely" in note for note in document["notes"])


def test_missing_capture_id_is_unknown(tmp_path, monkeypatch):
    tempo = FakeTempo({})
    view, _ = build_view(tmp_path, tempo, monkeypatch=monkeypatch)

    document = view.get_json({"capture_id": []})

    assert document["state"] == "unknown"
    assert document["notes"]


def test_trace_without_a_session_span_is_incomplete_but_ok(tmp_path, monkeypatch):
    capture_id = CAAS_CAPTURE
    payload = build_trace(
        capture_id,
        parent_tests=8,
        workers=1,
        fixtures=2,
        fixture_uses=2,
        waits=1,
        service_spans=0,
        include_session=False,
    )
    tempo = FakeTempo({trace_id_for(capture_id): payload})
    view, _ = build_view(tmp_path, tempo, jobs=[make_job(41709318840, "2026-11-03T21:00:00Z")], monkeypatch=monkeypatch)

    document = fetch(view, capture_id)

    assert document["state"] == "ok"
    assert document["coverage"]["session_span"] == 0
    assert document["session"]["started_at"] is not None
    assert document["session"]["duration_seconds"] is not None
    assert any("no 'test run' session span" in note for note in document["notes"])


# ---------------------------------------------------------------------------
# Window, search and persistence
# ---------------------------------------------------------------------------


def test_search_window_comes_from_the_job_history(tmp_path, monkeypatch):
    tempo = FakeTempo({trace_id_for(VMAAS_CAPTURE): fixture_trace(VMAAS_CAPTURE)})
    view, _ = build_view(tmp_path, tempo, jobs=[make_job(41709279176, "2026-11-03T21:00:00Z")], monkeypatch=monkeypatch)

    fetch(view, VMAAS_CAPTURE)

    query = tempo.query()
    assert query["start"] == [str(int(datetime(2026, 11, 3, 20, 55, tzinfo=UTC).timestamp()))]
    assert query["end"] == [str(int(datetime(2026, 11, 4, 5, 0, tzinfo=UTC).timestamp()))]


def test_legacy_capture_id_is_accepted(tmp_path, monkeypatch):
    capture_id = "run-41700000000-attempt-2"
    payload = build_trace(capture_id, parent_tests=2, workers=1, fixtures=1, fixture_uses=1, waits=1, service_spans=0)
    tempo = FakeTempo({trace_id_for(capture_id): payload})
    view, _ = build_view(
        tmp_path, tempo, jobs=[make_job(41700000000, "2026-11-03T21:00:00Z", attempt=2)], monkeypatch=monkeypatch
    )

    document = fetch(view, capture_id)

    assert document["state"] == "ok"
    assert document["session"]["tests"] == 2


def test_run_id_column_is_the_primary_join(tmp_path, monkeypatch):
    capture_id = VMAAS_CAPTURE
    run_id = 41709279176
    job = make_job(run_id, "2026-11-03T21:00:00Z", suite_run=True)
    job["run_id"] = str(run_id)
    tempo = FakeTempo({trace_id_for(capture_id): fixture_trace(capture_id)})
    view, _ = build_view(tmp_path, tempo, jobs=[job], run_id_column=True, monkeypatch=monkeypatch)

    document = fetch(view, capture_id)

    assert document["state"] == "ok"
    assert document["session"]["tests"] >= 1
    # The job row's id is the job id, so only the run_id column can find it.
    assert tempo.query()["start"] == [str(int(datetime(2026, 11, 3, 20, 55, tzinfo=UTC).timestamp()))]


def test_registry_trace_id_skips_the_search(tmp_path, monkeypatch):
    capture_id = VMAAS_CAPTURE
    trace_id = trace_id_for(capture_id)
    tempo = FakeTempo({trace_id: fixture_trace(capture_id)})
    view, _ = build_view(
        tmp_path, tempo, captures=[make_capture(capture_id, 41709279176, trace_id=trace_id)], monkeypatch=monkeypatch
    )

    document = fetch(view, capture_id)

    assert document["state"] == "ok"
    assert tempo.urls("/api/search?") == []
    assert len(tempo.urls("/api/traces/")) == 1
    assert document["identity"]["session_trace_id"] == trace_id


def test_discovered_trace_id_is_persisted_in_captures(tmp_path, monkeypatch):
    capture_id = VMAAS_CAPTURE
    trace_id = trace_id_for(capture_id)
    tempo = FakeTempo({trace_id: fixture_trace(capture_id)})
    view, exporter = build_view(
        tmp_path,
        tempo,
        jobs=[make_job(41709279176, "2026-11-03T21:00:00Z")],
        captures=[make_capture(capture_id, 41709279176)],
        monkeypatch=monkeypatch,
    )

    document = fetch(view, capture_id)

    assert document["state"] == "ok"
    connection = exporter._db()
    row = connection.execute("SELECT session_trace_id FROM captures WHERE capture_id = ?", (capture_id,)).fetchone()
    connection.close()
    assert row["session_trace_id"] == trace_id
    assert any("stored the search trace id" in note for note in document["notes"])


def test_search_falls_back_to_a_test_span(tmp_path, monkeypatch):
    capture_id = VMAAS_CAPTURE
    trace_id = trace_id_for(capture_id)

    def handler(query, params):
        if ".pytest.span_type" in query:
            return [trace_id]
        return []

    tempo = FakeTempo({trace_id: fixture_trace(capture_id)}, search_handler=handler)
    view, _ = build_view(tmp_path, tempo, jobs=[make_job(41709279176, "2026-11-03T21:00:00Z")], monkeypatch=monkeypatch)

    document = fetch(view, capture_id)

    assert document["state"] == "ok"
    assert len(tempo.urls("/api/search?")) == 2
    second = tempo.query(1)
    assert '.pytest.span_type = "test"' in second["q"][0]
    assert any("test span" in note for note in document["notes"])


def test_registry_start_time_is_the_window_fallback(tmp_path, monkeypatch):
    capture_id = VMAAS_CAPTURE
    started_at = "2026-11-03T21:00:00Z"
    tempo = FakeTempo({trace_id_for(capture_id): fixture_trace(capture_id)})
    view, _ = build_view(
        tmp_path,
        tempo,
        captures=[make_capture(capture_id, 41709279176, started_at=started_at)],
        monkeypatch=monkeypatch,
    )

    document = fetch(view, capture_id)

    assert document["state"] == "ok"
    assert any("captures.capture_started_at" in note for note in document["notes"])
    assert tempo.query()["start"] == [str(int(datetime(2026, 11, 3, 20, 55, tzinfo=UTC).timestamp()))]


# ---------------------------------------------------------------------------
# Cache and single flight
# ---------------------------------------------------------------------------


def test_positive_answer_is_cached(tmp_path, monkeypatch):
    capture_id = CAAS_CAPTURE
    tempo = FakeTempo({trace_id_for(capture_id): fixture_trace(capture_id)})
    view, _ = build_view(tmp_path, tempo, jobs=[make_job(41709318840, "2026-11-03T21:00:00Z")], monkeypatch=monkeypatch)

    first = fetch(view, capture_id)
    second = fetch(view, capture_id)

    assert first == second
    assert len(tempo.urls("/api/search?")) == 1
    assert len(tempo.urls("/api/traces/")) == 1


def test_negative_answer_is_cached_for_ten_seconds(tmp_path, monkeypatch):
    tempo = FakeTempo({})
    view, _ = build_view(
        tmp_path,
        tempo,
        jobs=[make_job(41709279176, iso(datetime.now(UTC)))],
        captures=[make_capture(VMAAS_CAPTURE, 41709279176)],
        monkeypatch=monkeypatch,
    )

    fetch(view, VMAAS_CAPTURE)
    assert len(tempo.urls("/api/search?")) == 2
    fetch(view, VMAAS_CAPTURE)
    assert len(tempo.urls("/api/search?")) == 2

    view._cache.clear()
    view.NEGATIVE_CACHE_TTL_SECONDS = 0
    fetch(view, VMAAS_CAPTURE)
    assert len(tempo.urls("/api/search?")) == 4


def test_single_flight_shares_one_tempo_lookup(tmp_path, monkeypatch):
    capture_id = CAAS_CAPTURE
    tempo = FakeTempo({trace_id_for(capture_id): fixture_trace(capture_id)}, search_delay=0.05, trace_delay=0.05)
    view, _ = build_view(tmp_path, tempo, jobs=[make_job(41709318840, "2026-11-03T21:00:00Z")], monkeypatch=monkeypatch)
    barrier = threading.Barrier(8)
    results = []
    errors = []

    def reader():
        try:
            barrier.wait(timeout=5)
            results.append(json.dumps(fetch(view, capture_id), sort_keys=True))
        except BaseException as error:  # noqa: BLE001 - reported to the test
            errors.append(error)

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert len(results) == 8
    assert len(set(results)) == 1
    assert len(tempo.urls("/api/search?")) == 1
    assert len(tempo.urls("/api/traces/")) == 1


def test_cache_evicts_the_oldest_entry(tmp_path, monkeypatch):
    caas_payload = fixture_trace(CAAS_CAPTURE)
    vmaas_payload = fixture_trace(VMAAS_CAPTURE)
    tempo = FakeTempo({trace_id_for(CAAS_CAPTURE): caas_payload, trace_id_for(VMAAS_CAPTURE): vmaas_payload})
    view, _ = build_view(tmp_path, tempo, monkeypatch=monkeypatch)
    view.CACHE_MAX_BYTES = 1

    fetch(view, CAAS_CAPTURE)
    fetch(view, VMAAS_CAPTURE)

    assert CAAS_CAPTURE not in view._cache
    assert VMAAS_CAPTURE in view._cache


# ---------------------------------------------------------------------------
# Buckets, identity and failed tests
# ---------------------------------------------------------------------------


def test_buckets_are_capped_with_a_note(tmp_path, monkeypatch):
    capture_id = CAAS_CAPTURE
    tempo = FakeTempo({trace_id_for(capture_id): fixture_trace(capture_id)})
    view, _ = build_view(tmp_path, tempo, jobs=[make_job(41709318840, "2026-11-03T21:00:00Z")], monkeypatch=monkeypatch)
    view.MAX_ROWS_PER_BUCKET = 5

    document = fetch(view, capture_id)

    assert document["state"] == "ok"
    assert len(document["tests"]) == 5
    assert len(document["fixtures"]) == 5
    assert len(document["fixture_uses"]) == 5
    assert len(document["waits"]) == 5
    assert sum("capped" in note for note in document["notes"]) == 4


def test_identity_is_the_registry_row(tmp_path, monkeypatch):
    capture_id = VMAAS_CAPTURE
    trace_id = trace_id_for(capture_id)
    tempo = FakeTempo({trace_id: fixture_trace(capture_id)})
    view, _ = build_view(
        tmp_path, tempo, captures=[make_capture(capture_id, 41709279176, trace_id=trace_id)], monkeypatch=monkeypatch
    )

    document = fetch(view, capture_id)

    identity = document["identity"]
    assert identity["run_id"] == "41709279176"
    assert identity["suite"] == "vmaas"
    assert identity["test_sha"] == "a" * 40
    assert identity["identity_source"] == "info"
    assert identity["session_trace_id"] == trace_id


def test_identity_carries_github_links(tmp_path, monkeypatch):
    capture_id = VMAAS_CAPTURE
    trace_id = trace_id_for(capture_id)
    tempo = FakeTempo({trace_id: fixture_trace(capture_id)})
    view, _ = build_view(
        tmp_path,
        tempo,
        jobs=[make_job(41709279176, "2026-11-03T21:00:00Z")],
        captures=[make_capture(capture_id, 41709279176, trace_id=trace_id, pr="1620")],
        monkeypatch=monkeypatch,
    )

    identity = fetch(view, capture_id)["identity"]

    assert identity["repo"] == "osac"
    assert identity["run_url"] == "https://github.com/osac-project/osac/actions/runs/41709279176"
    assert identity["pr_url"] == "https://github.com/osac-project/osac/pull/1620"
    assert identity["commit_url"] == f"https://github.com/osac-project/osac/commit/{'a' * 40}"


def test_identity_is_empty_when_the_registry_has_no_row(tmp_path, monkeypatch):
    capture_id = CAAS_CAPTURE
    tempo = FakeTempo({trace_id_for(capture_id): fixture_trace(capture_id)})
    view, _ = build_view(tmp_path, tempo, jobs=[make_job(41709318840, "2026-11-03T21:00:00Z")], monkeypatch=monkeypatch)

    document = fetch(view, capture_id)

    assert document["identity"]["run_id"] == ""
    assert any("registry has no captures row" in note for note in document["notes"])


def test_failed_tests_come_from_a_warm_evidence_cache(tmp_path, monkeypatch):
    capture_id = VMAAS_41818898681
    evidence = FakeEvidence(
        run_id=41818898681,
        run_attempt=1,
        native={
            "status": "available",
            "failed_tests": [
                {
                    "classname": "tests.e2e.vmaas.sanity.test_from_junit",
                    "name": "test_from_junit",
                    "type": "failure",
                    "message": "boom",
                    "time_s": 8.0,
                    "artifact_url": "https://example.test/artifact",
                }
            ],
        },
    )
    tempo = FakeTempo({trace_id_for(capture_id): fixture_trace(capture_id)})
    view, _ = build_view(
        tmp_path,
        tempo,
        jobs=[make_job(41818898681, "2026-11-03T21:00:00Z")],
        evidence=evidence,
        monkeypatch=monkeypatch,
    )

    document = fetch(view, capture_id)

    assert document["state"] == "ok"
    assert document["failed_tests"][0]["name"] == "test_from_junit"
    assert evidence.artifact_requests == 0
    assert evidence._cache.reads == [("native", 41818898681, 1)]
    # The trace's failed test span is not the source of failed_tests.
    failed_span_rows = [row for row in document["tests"] if row["status"] == "error"]
    assert len(failed_span_rows) == 1
    assert document["failed_tests"][0]["name"] != failed_span_rows[0]["nodeid"].rsplit("::", 1)[-1]


def test_failed_tests_cold_cache_returns_empty_without_an_artifact_request(tmp_path, monkeypatch):
    capture_id = CAAS_CAPTURE
    evidence = FakeEvidence(run_id=41709318840, run_attempt=1)
    tempo = FakeTempo({trace_id_for(capture_id): fixture_trace(capture_id)})
    view, _ = build_view(
        tmp_path,
        tempo,
        jobs=[make_job(41709318840, "2026-11-03T21:00:00Z")],
        evidence=evidence,
        monkeypatch=monkeypatch,
    )

    document = fetch(view, capture_id)

    assert document["failed_tests"] == []
    assert evidence.artifact_requests == 0
    assert "failed test detail is in the CI evidence panel" in document["notes"]


def test_failed_tests_empty_when_the_cached_junit_is_unavailable(tmp_path, monkeypatch):
    capture_id = CAAS_CAPTURE
    evidence = FakeEvidence(run_id=41709318840, run_attempt=1, native={"status": "expired", "failed_tests": []})
    tempo = FakeTempo({trace_id_for(capture_id): fixture_trace(capture_id)})
    view, _ = build_view(
        tmp_path,
        tempo,
        jobs=[make_job(41709318840, "2026-11-03T21:00:00Z")],
        evidence=evidence,
        monkeypatch=monkeypatch,
    )

    document = fetch(view, capture_id)

    assert document["failed_tests"] == []
    assert evidence.artifact_requests == 0
    assert any("JUnit artifact is 'expired'" in note for note in document["notes"])


def test_rows_carry_wait_and_fixture_attributes(tmp_path, monkeypatch):
    capture_id = CAAS_CAPTURE
    tempo = FakeTempo({trace_id_for(capture_id): fixture_trace(capture_id)})
    view, _ = build_view(tmp_path, tempo, jobs=[make_job(41709318840, "2026-11-03T21:00:00Z")], monkeypatch=monkeypatch)

    document = fetch(view, capture_id)

    wait = document["waits"][0]
    assert wait["attempts"] >= 1
    assert wait["condition_met"] is True
    assert wait["resource_kind"] == "ComputeInstance"
    assert wait["resource_id"].startswith("01a1a33a-")
    fixture = document["fixtures"][0]
    assert fixture["fixture_name"].startswith("fixture_")
    assert fixture["scope"] == "function"
    use = document["fixture_uses"][0]
    assert use["fixture_name"].startswith("fixture_")
    assert use["worker"].startswith("gw")


def test_response_has_the_documented_shape(tmp_path, monkeypatch):
    capture_id = CAAS_CAPTURE
    tempo = FakeTempo({trace_id_for(capture_id): fixture_trace(capture_id)})
    view, _ = build_view(tmp_path, tempo, jobs=[make_job(41709318840, "2026-11-03T21:00:00Z")], monkeypatch=monkeypatch)

    document = fetch(view, capture_id)

    assert set(document) == {
        "capture_id",
        "state",
        "session",
        "coverage",
        "identity",
        "summary",
        "tests",
        "waits",
        "fixtures",
        "fixture_uses",
        "failed_tests",
        "transient",
        "notes",
    }
    summary = document["summary"][0]
    assert set(summary) == {"status", "tests", "duration_seconds"}
    assert summary["tests"] == document["session"]["tests"]
    assert summary["duration_seconds"] == document["session"]["duration_seconds"]
    assert set(document["session"]) == {
        "trace_id",
        "started_at",
        "duration_seconds",
        "status",
        "tests",
        "test_attempts",
        "workers",
        "span_total",
    }
    assert set(document["coverage"]) == {
        "spans_total",
        "spans_classified",
        "spans_unclassified",
        "session_span",
        "service_calls",
        "service_calls_complete",
    }
    # The per-row keys are the dashboard's contract. The Capture fixture in
    # test_ci_observe_dashboard.py copies them; a rename here must fail both.
    assert set(document["tests"][0]) == {
        "name", "nodeid", "file", "worker", "started_at", "duration_s",
        "status", "phase_spans",
        # Where the test's time went, from the span rollup's breakdown.
        "setup_s", "wait_s", "actions_s", "teardown_s", "waits",
    }
    assert set(document["waits"][0]) == {
        "name", "worker", "started_at", "duration_s", "status", "attempts",
        "condition_met", "resource_kind", "resource_id",
    }
    assert set(document["fixtures"][0]) == {
        "name", "fixture_name", "scope", "phase", "worker", "started_at",
        "duration_s", "status",
    }
    assert set(document["fixture_uses"][0]) == {
        "name", "fixture_name", "nodeid", "worker", "started_at", "duration_s",
        "status",
    }
    assert set(document["identity"]) == {
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
        "repo",
        "run_url",
        "pr_url",
        "commit_url",
    }
