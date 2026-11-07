"""The span rollup (ci_observe_spans.py): one finished capture's trace read
once, broken down per test into setup, waiting for the product, test actions
and teardown, with every wait and shared fixture kept for the cross-run
pages."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from helpers import make_job

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
CAPTURE = "vmaas-pr12-500-a1"
TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
T0 = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
NOW = T0 + timedelta(hours=3)
NODE = "tests/e2e/vmaas/sanity/test_vm.py::test_create"


def load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def spans():
    return load("ci_observe_spans")


def ns(seconds):
    return str(int((T0 + timedelta(seconds=seconds)).timestamp() * 1_000_000_000))


def attr(key, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": value}}


def span(span_id, parent, name, start, end, status=0, **attributes):
    return {
        "traceId": TRACE_ID,
        "spanId": span_id,
        "parentSpanId": parent,
        "name": name,
        "startTimeUnixNano": ns(start),
        "endTimeUnixNano": ns(end),
        "attributes": [attr(key.replace("__", "."), value) for key, value in attributes.items()],
        "status": {"code": status},
    }


def trace(include_session=True):
    worker = {"osac__ci__xdist_worker": "gw0"}
    test = {"pytest__span_type": "test", "pytest__nodeid": NODE, **worker}
    spans_ = [
        span("w0", "s1", "test worker gw0", 0, 200, pytest__span_type="worker", **worker),
        span("t1", "w0", NODE, 0, 100, **test),
        span("t1s", "t1", f"{NODE}::setup", 0, 30, **test),
        span("t1c", "t1", f"{NODE}::call", 30, 90, **test),
        span("t1t", "t1", f"{NODE}::teardown", 90, 100, **test),
        span("f1", "t1s", "tenant setup", 0, 20, pytest__span_type="fixture", pytest__fixture_scope="session",
             code__function="tenant", **worker),
        span("f2", "t1s", "subnet setup", 20, 28, pytest__span_type="fixture", pytest__fixture_scope="function",
             code__function="subnet", **worker),
        # A fixture that another fixture requests from its own body nests
        # under it; only top-level fixtures are fixture rows.
        span("f3", "f1", "keycloak setup", 2, 5, pytest__span_type="fixture", pytest__fixture_scope="session",
             **worker),
        span("wa", "t1c", "ComputeInstance ci-e2e-4f9a to become Ready", 40, 80, pytest__span_type="wait",
             osac__wait__resource_kind="ComputeInstance", osac__wait__resource_id="ci-e2e-4f9a",
             poll__attempts=8, poll__condition_met=True, **worker),
        # Nested in a fixture: the phase is the fixture's.
        span("wb", "f2", "Subnet sub-1 to become Ready", 21, 27, pytest__span_type="wait",
             osac__wait__resource_kind="Subnet", osac__wait__resource_id="sub-1", poll__attempts=3,
             poll__condition_met=True, **worker),
        # Parent lost: placed by time on the same worker.
        span("wc", "lost", "ComputeInstance ci-e2e-4f9a to be deleted", 92, 98, status=2,
             pytest__span_type="wait", osac__wait__resource_kind="ComputeInstance",
             osac__wait__resource_id="ci-e2e-4f9a", poll__attempts=60, poll__condition_met=False, **worker),
        # Parent lost on a worker with no test: stays unattributed.
        span("wd", "lost", "Tenant t-9 to become Ready", 50, 60, pytest__span_type="wait",
             osac__wait__resource_kind="Tenant", osac__ci__xdist_worker="gw1"),
        span("x1", "", "GET /api/fulfillment/v1/compute_instances", 41, 42),
    ]
    if include_session:
        spans_.insert(0, span("s1", "", "test run", 0, 200, pytest__span_type="run"))
    return {"batches": [{"resource": {"attributes": [attr("osac.ci.capture_id", CAPTURE)]},
                         "scopeSpans": [{"spans": spans_}]}]}


def entries(view_module, payload):
    view = view_module.CIObserveCaptureView(None)
    rows = [view._span_entry(span_, resource) for span_, resource in view._iter_spans(payload)]
    rows = [row for row in rows if row is not None]
    view._resolve_workers(rows)
    return rows


class TestBreakdown:
    def test_a_test_splits_into_setup_waits_actions_and_teardown(self, spans):
        view_module = load("ci_observe_capture_view")
        tests, waits, fixtures, unattributed = spans.breakdown(entries(view_module, trace()))
        assert len(tests) == 1
        test = tests[0]
        assert test["test_key"] == "tests.e2e.vmaas.sanity.test_vm::test_create"
        assert (test["duration_s"], test["setup_s"], test["call_s"], test["teardown_s"]) == (100, 30, 60, 10)
        assert (test["wait_setup_s"], test["wait_call_s"], test["wait_teardown_s"]) == (6, 40, 6)
        assert test["wait_s"] == 52
        assert test["shared_setup_s"] == 20, "the session-scoped tenant, not the function-scoped subnet"
        assert test["waits"] == 3
        assert test["worker"] == "gw0"
        assert unattributed == 1

        by_name = {wait["name"]: wait for wait in waits}
        assert by_name["Subnet sub-1 to become Ready"]["phase"] == "setup"
        assert by_name["ComputeInstance ci-e2e-4f9a to be deleted"]["phase"] == "teardown"
        assert by_name["ComputeInstance ci-e2e-4f9a to be deleted"]["failed"] == 1
        assert by_name["ComputeInstance ci-e2e-4f9a to become Ready"]["signature"] == (
            "ComputeInstance <id> to become Ready")
        assert by_name["ComputeInstance ci-e2e-4f9a to become Ready"]["attempts"] == 8
        assert by_name["ComputeInstance ci-e2e-4f9a to become Ready"]["condition_met"] == 1
        assert by_name["Tenant t-9 to become Ready"]["nodeid"] == ""

        assert sorted((fixture["fixture"], fixture["scope"]) for fixture in fixtures) == [
            ("subnet", "function"), ("tenant", "session")]
        assert all(fixture["nodeid"] == NODE for fixture in fixtures)

    def test_overlapping_waits_are_counted_once(self, spans):
        assert spans.union_seconds([]) == 0
        assert spans.union_seconds([(0, 2e9), (1e9, 3e9), (5e9, 6e9), (6e9, 6e9)]) == 4.0

    @pytest.mark.parametrize(
        ("name", "resource", "signature"),
        [
            ("ComputeInstance ci-e2e-4f9a to become Ready", "ci-e2e-4f9a", "ComputeInstance <id> to become Ready"),
            ("ClusterOrder 3f2b1c9e-1111-2222-3333-444455556666 Ready", "", "ClusterOrder <id> Ready"),
            ("Subnet subnet-7 in VirtualNetwork vnet-ci-12 Ready", "", "Subnet <id> in VirtualNetwork <id> Ready"),
            ("Keycloak realm 'osac' reachable", "", "Keycloak realm '<v>' reachable"),
            ("", "", "(unnamed wait)"),
        ],
    )
    def test_wait_signature(self, spans, name, resource, signature):
        assert spans.wait_signature(name, resource) == signature


@pytest.fixture
def stores(spans, exporter, wfe, tmp_path, monkeypatch):
    """A registry with one finished capture, a trends.db, and a Tempo that
    answers with the trace above."""
    exporter._upsert_job(make_job(id=500, run_id="500", workflow="E2E suite · vmaas · #12 · 0123abc",
                                  created_at="2026-11-06T09:59:00Z", updated_at="2026-11-06T10:05:00Z"))
    with exporter._db() as conn:
        conn.execute(
            "INSERT INTO captures (capture_id, run_id, run_attempt, suite, first_seen_at, last_seen_at)"
            " VALUES (?, '500', '1', 'vmaas', '2026-11-06T10:00:00Z', '2026-11-06T10:05:00Z')",
            (CAPTURE,),
        )
    view_module = load("ci_observe_capture_view")
    state = {"payload": trace(), "calls": []}

    def http_get(url, *, timeout, max_bytes):
        state["calls"].append(url)
        if "/api/search" in url:
            return json.dumps({"traces": [{"traceID": TRACE_ID}]}).encode()
        return json.dumps(state["payload"]).encode()

    monkeypatch.setattr(view_module, "_http_get", http_get)
    return {"exporter_db": wfe.DB_FILE, "trends_db": str(tmp_path / "trends.db"), "state": state}


def query(path, sql, *args):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, args)]
    finally:
        conn.close()


class TestPass:
    def run(self, spans, stores, *extra, now=NOW):
        return spans.main(["--exporter-db", stores["exporter_db"], "--trends-db", stores["trends_db"], *extra],
                          now=now)

    def test_a_finished_capture_is_read_once(self, spans, stores):
        assert self.run(spans, stores) == 0
        summary = query(stores["trends_db"], "SELECT * FROM span_runs")[0]
        assert (summary["state"], summary["complete"], summary["tests"], summary["waits"]) == ("ok", 1, 1, 4)
        assert summary["unattributed_waits"] == 1
        assert summary["day"] == "2026-11-06"
        assert summary["suite"] == "vmaas"
        rows = query(stores["trends_db"], "SELECT * FROM test_spans")
        assert rows[0]["wait_s"] == 52
        assert rows[0]["run_id"] == 500
        assert len(query(stores["trends_db"], "SELECT * FROM wait_spans")) == 4
        assert len(query(stores["trends_db"], "SELECT * FROM fixture_spans")) == 2
        calls = len(stores["state"]["calls"])
        assert self.run(spans, stores) == 0
        assert len(stores["state"]["calls"]) == calls, "a complete capture is not read again"

    def test_force_reads_it_again_and_replaces_its_rows(self, spans, stores):
        assert self.run(spans, stores) == 0
        assert self.run(spans, stores, "--force", "--capture", CAPTURE) == 0
        assert len(query(stores["trends_db"], "SELECT * FROM wait_spans")) == 4
        assert len(query(stores["trends_db"], "SELECT * FROM test_spans")) == 1

    def test_a_trace_without_its_session_span_is_retried(self, spans, stores):
        stores["state"]["payload"] = trace(include_session=False)
        assert self.run(spans, stores) == 0
        summary = query(stores["trends_db"], "SELECT * FROM span_runs")[0]
        assert (summary["state"], summary["complete"]) == ("no_session", 0)
        stores["state"]["payload"] = trace()
        assert self.run(spans, stores) == 0
        summary = query(stores["trends_db"], "SELECT * FROM span_runs")[0]
        assert (summary["state"], summary["complete"]) == ("ok", 1)

    def test_an_incomplete_capture_is_kept_after_the_retry_window(self, spans, stores):
        stores["state"]["payload"] = trace(include_session=False)
        assert self.run(spans, stores, now=NOW + timedelta(hours=72)) == 0
        summary = query(stores["trends_db"], "SELECT * FROM span_runs")[0]
        assert (summary["state"], summary["complete"]) == ("no_session", 1)

    def test_a_capture_is_not_read_before_tempo_settles(self, spans, stores):
        assert self.run(spans, stores, now=datetime(2026, 11, 6, 10, 10, tzinfo=UTC)) == 0
        assert query(stores["trends_db"], "SELECT * FROM span_runs") == []

    def test_a_running_run_is_not_read(self, spans, stores, exporter):
        with exporter._db() as conn:
            conn.execute("UPDATE jobs SET conclusion = '' WHERE id = 500")
        assert self.run(spans, stores) == 0
        assert query(stores["trends_db"], "SELECT * FROM span_runs") == []

    def test_a_capture_older_than_tempo_retention_is_not_read(self, spans, stores):
        assert self.run(spans, stores, now=NOW + timedelta(days=40)) == 0
        assert query(stores["trends_db"], "SELECT * FROM span_runs") == []

    def test_tempo_errors_are_retried(self, spans, stores, monkeypatch):
        view_module = load("ci_observe_capture_view")

        def broken(url, *, timeout, max_bytes):
            if "/api/search" in url:
                return json.dumps({"traces": [{"traceID": TRACE_ID}]}).encode()
            raise view_module.TempoError("HTTP 500: boom")

        monkeypatch.setattr(view_module, "_http_get", broken)
        assert self.run(spans, stores) == 0
        summary = query(stores["trends_db"], "SELECT * FROM span_runs")[0]
        assert (summary["state"], summary["complete"]) == ("unavailable", 0)

    def test_a_missing_trace_is_retried_then_given_up(self, spans, stores, monkeypatch):
        view_module = load("ci_observe_capture_view")
        monkeypatch.setattr(view_module, "_http_get",
                            lambda url, *, timeout, max_bytes: json.dumps({"traces": []}).encode())
        assert self.run(spans, stores) == 0
        assert query(stores["trends_db"], "SELECT complete FROM span_runs")[0]["complete"] == 0

    def test_old_rows_are_pruned(self, spans, stores):
        assert self.run(spans, stores) == 0
        assert self.run(spans, stores, now=NOW + timedelta(days=120)) == 0
        assert query(stores["trends_db"], "SELECT * FROM test_spans") == []

    def test_a_missing_exporter_db_is_an_error(self, spans, tmp_path):
        assert spans.main(["--exporter-db", str(tmp_path / "nope.db"), "--trends-db",
                           str(tmp_path / "t.db")]) == 2

    def test_a_registry_without_tables_reads_nothing(self, spans, tmp_path):
        empty = tmp_path / "empty.db"
        sqlite3.connect(empty).close()
        conn = spans.open_db(str(tmp_path / "t.db"))
        try:
            assert spans.captures_to_read(str(empty), conn, NOW, 10) == []
        finally:
            conn.close()
