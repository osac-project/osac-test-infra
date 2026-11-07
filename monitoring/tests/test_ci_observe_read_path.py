"""Acceptance tests for the ci-observe read path.

Two layers, on purpose.

The first layer tests the live harness in
monitoring/scripts/ci_observe_read_path_test.py: its budget matrix, its
percentiles, its truncated-count and superset rules, and its four-reader
runner against fake servers. Those run everywhere, including CI, and they
keep the harness from drifting away from the spec.

The second layer is the acceptance gate for the registry-backed endpoints.
It talks to the real exporter handler over loopback and checks the rules a
live host cannot prove: no outbound call on the registry indexes, one Tempo
search for four concurrent Capture views, a cache that outlives the
dashboard refresh, truncated counts, and the verdict matrix that must never
turn an exception into `not collected`.

The registry endpoints do not exist on this branch yet, so the second layer
skips until they do. The skip is keyed on the HTTP routes, not on a module
name: once workflow-exporter.py routes `/api/ci-observe/captures`, every
test here runs and a missing behaviour fails.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import re
import sqlite3
import sys
import threading
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import pytest
from helpers import make_job

MONITORING = Path(__file__).resolve().parents[1]
SCRIPTS = MONITORING / "scripts"
EXPORTER = SCRIPTS / "workflow-exporter.py"
DASHBOARDS = MONITORING / "config" / "grafana" / "dashboards"
HOME_DASHBOARD = DASHBOARDS / "ci-observe-runs-home.json"

_spec = importlib.util.spec_from_file_location(
    "ci_observe_read_path_test", SCRIPTS / "ci_observe_read_path_test.py"
)
harness = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
# The script uses dataclasses with postponed annotations, which look the
# module up in sys.modules while the class is built.
sys.modules[_spec.name] = harness
_spec.loader.exec_module(harness)

REGISTRY_ROUTES = (
    '"/api/ci-observe/captures"',
    '"/api/ci-observe/cohorts"',
    '"/api/ci-observe/capture"',
)
RUN_ID = 41310072671
CAPTURE = f"vmaas-main-{RUN_ID}-a1"
SYNTHETIC_CAPTURE = "vmaas-main-99999999999-a1"


def registry_is_routed() -> bool:
    """True when workflow-exporter.py serves the registry read endpoints."""
    text = EXPORTER.read_text()
    return all(route in text for route in REGISTRY_ROUTES)


requires_registry = pytest.mark.skipif(
    not registry_is_routed(),
    reason="the registry read path is not routed in workflow-exporter.py on this branch",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def iso(minutes_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_job(run_id: int = RUN_ID, **overrides) -> dict:
    """A run-level E2E row: its url has no /job/ segment, which is what the
    run index lists."""
    row = make_job(
        id=run_id,
        workflow="E2E VMaaS Full Install",
        display_name="osac / E2E VMaaS Full Install",
        category="e2e",
        conclusion="success",
        created_at=iso(180),
        updated_at=iso(120),
        url=f"https://github.com/osac-project/osac/actions/runs/{run_id}",
    )
    row.update(overrides)
    return row


def dashboard_refresh_seconds() -> int:
    """The Start here refresh interval, which the capture cache must outlive.

    A cache shorter than the refresh makes every dashboard reload pay the
    cold path again, which is how the old Tempo lookup turned a 5-minute
    refresh into a 20-second wait.
    """
    text = json.loads(HOME_DASHBOARD.read_text())["refresh"]
    match = re.fullmatch(r"(\d+)([smh])", text)
    assert match, f"the dashboard refresh {text!r} is not a duration this test understands"
    return int(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)]


def dashboard_days() -> set[int]:
    """Every window length the ci-observe dashboards offer."""
    days = set()
    for path in DASHBOARDS.glob("ci-observe-*.json"):
        document = json.loads(path.read_text())
        for variable in document.get("templating", {}).get("list", []):
            if variable.get("name") != "days":
                continue
            for option in variable.get("options", []):
                days.add(int(option["value"]))
    return days


def get_json(base_url: str, path: str, timeout: float = 10.0) -> tuple[int, dict | None]:
    """A loopback GET with http.client, not urllib: the no-outbound tests
    replace urllib.request.urlopen and must still be able to reach the
    handler under test."""
    parsed = urlparse(base_url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        return response.status, json.loads(body) if body else None
    finally:
        connection.close()


class FakeServer:
    """A loopback server whose answer the test chooses."""

    def __init__(self, answer):
        self.answer = answer
        self.paths: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.paths.append(self.path)
                status, document = outer.answer
                body = json.dumps(document).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeTempo:
    """Serves the two Tempo calls the Capture view may make: a tight-window
    search for the session trace id, and a trace fetch."""

    def __init__(self):
        self.searches: list[str] = []
        self.traces: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/api/search" or parsed.path.startswith("/api/search/"):
                    query = dict(parse_qsl(parsed.query))
                    outer.searches.append(query.get("q", ""))
                    status, document = 200, {"traces": []}
                elif parsed.path.startswith("/api/traces/"):
                    outer.traces.append(parsed.path.rsplit("/", 1)[-1])
                    status, document = 200, {"batches": []}
                else:
                    status, document = 404, {"error": f"unknown path {parsed.path}"}
                body = json.dumps(document).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def searches_for(self, capture_id: str) -> int:
        return sum(1 for query in self.searches if capture_id in query)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def api(wfe, exporter, monkeypatch):
    """The real handler on an empty exporter, on a loopback port."""
    monkeypatch.setattr(wfe.ExporterHandler, "exporter", exporter)
    server = ThreadingHTTPServer(("127.0.0.1", 0), wfe.ExporterHandler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def jobs(exporter):
    for offset in range(12):
        exporter._upsert_job(run_job(RUN_ID + offset))
    return exporter


def seed_source(exporter, wfe, rows: list[dict]) -> None:
    """Write capture_source rows straight into the exporter's DB.

    The endpoint reads the table; seeding it is the only way to test the
    verdict rule for a source that answered completely, partially or never.
    """
    connection = sqlite3.connect(wfe.DB_FILE)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS capture_source ("
            "source TEXT PRIMARY KEY, coverage_from TEXT NOT NULL, last_ok_at TEXT,"
            "last_ok_count INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',"
            "last_error_at TEXT)"
        )
        for row in rows:
            connection.execute(
                "INSERT OR REPLACE INTO capture_source"
                " (source, coverage_from, last_ok_at, last_ok_count, last_error, last_error_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row["source"],
                    row.get("coverage_from", iso(60 * 24 * 20)),
                    row.get("last_ok_at"),
                    row.get("last_ok_count", 0),
                    row.get("last_error", ""),
                    row.get("last_error_at"),
                ),
            )
        connection.commit()
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# The budget matrix
# ---------------------------------------------------------------------------
def test_budgets_match_the_spec():
    """/runs 400/200ms, Capture 1500/400ms, pages 1.0/0.4s and 2.0/0.8s,
    Compare and PR detail 500ms, no request over twice its budget."""
    assert harness.BUDGETS["runs"] == (0.400, 0.200)
    assert harness.BUDGETS["capture"] == (1.500, 0.400)
    assert harness.BUDGETS["compare"] == (0.500, 0.500)
    assert harness.BUDGETS["prs"] == (0.500, 0.500)
    assert harness.PAGE_BUDGETS["start_here"][0] == (1.000, 0.400)
    assert harness.PAGE_BUDGETS["capture_page"][0] == (2.000, 0.800)
    assert harness.MAX_BUDGET_FACTOR == 2.0
    assert harness.CONCURRENCY_P95_FACTOR == 2.0
    assert harness.PROXY_TIMEOUT_SECONDS == 30.0


def test_percentile_is_nearest_rank():
    assert harness.percentile([], 0.95) is None
    assert harness.percentile([0.3], 0.95) == 0.3
    assert harness.percentile([0.4, 0.1, 0.3, 0.2], 0.50) == 0.2
    assert harness.percentile([0.4, 0.1, 0.3, 0.2], 0.95) == 0.4


def test_a_slow_sample_fails_the_budget_and_a_fast_one_passes():
    slow = harness.check_budget("runs", 0.900, [0.300, 0.300])
    assert [check.ok for check in slow] == [False, False, False]

    fast = harness.check_budget("runs", 0.100, [0.050, 0.060])
    assert all(check.ok is True for check in fast)


def test_a_dead_endpoint_is_a_skip_on_the_budget_not_a_fast_pass():
    """No successful answer means no latency claim: the status check already
    failed, and a 0.000s warm p95 must not appear as a PASS."""
    checks = harness.check_budget("runs", None, [])

    assert [check.ok for check in checks] == [None, None]


def test_a_dead_endpoint_has_no_concurrency_claim():
    check = next(
        check for check in harness.check_concurrency("runs", harness.Load([]), harness.Load([]))
        if check.name == "runs/4-readers p95"
    )
    assert check.ok is None


def test_one_request_over_twice_the_budget_fails_even_when_p95_passes():
    checks = harness.check_budget("runs", 0.100, [0.050, 0.900])

    worst = next(check for check in checks if check.name == "runs/max")
    assert worst.ok is False
    assert "0.900s" in worst.detail


def test_the_harness_windows_do_not_touch_a_dashboard_window():
    """A harness cache key must not evict a reader's entry, or the warm
    sample would measure the harness's own cache and not the reader's."""
    dashboards = dashboard_days()

    assert dashboards == set(harness.DASHBOARD_DAYS)
    assert harness.PERF_DAYS not in dashboards
    assert harness.WARM_DAYS not in dashboards
    assert harness.PERF_DAYS <= 30 and harness.WARM_DAYS <= 30


def test_every_measured_url_uses_the_private_window():
    plan = harness.targets(
        "http://127.0.0.1:9103", harness.PERF_DAYS, CAPTURE, "base-sha", "head-sha"
    )

    measured = {name: url for name, url in plan}
    assert set(measured) == {
        "runs", "captures", "cohorts", "capture", "trends", "compare", "prs",
        "health", "time", "failures", "tests", "waits",
    }
    for name, url in measured.items():
        if name == "capture":
            continue
        assert f"days={harness.PERF_DAYS}" in url, name
        for public in harness.DASHBOARD_DAYS:
            assert f"days={public}" not in url, f"{name} uses a dashboard window"


def test_the_harness_never_writes():
    source = (SCRIPTS / "ci_observe_read_path_test.py").read_text()

    assert "POST" not in source
    assert "PUT" not in source
    assert "DELETE" not in source
    assert "sqlite3" not in source


# ---------------------------------------------------------------------------
# Four readers, against fakes
# ---------------------------------------------------------------------------
def test_four_readers_get_no_429_from_a_cooperating_server():
    server = FakeServer((200, {"runs": [], "days": 13}))
    try:
        four = harness.load(f"{server.url}/api/ci-observe/runs?days=13", readers=4, per_reader=2, timeout=5)
    finally:
        server.close()

    assert len(four.results) == 8
    assert all(check.ok is True for check in harness.check_clean("runs", four))
    assert all(check.ok is True for check in harness.check_concurrency("runs", four, four))


def test_the_harness_names_a_429_and_a_500():
    limited = FakeServer((429, {"error": "too many"}))
    broken = FakeServer((500, {"error": "boom"}))
    try:
        rate_limited = harness.load(f"{limited.url}/x", readers=4, per_reader=2, timeout=5)
        failed = harness.load(f"{broken.url}/x", readers=4, per_reader=2, timeout=5)
    finally:
        limited.close()
        broken.close()

    rates = {check.name: check for check in harness.check_clean("runs", rate_limited)}
    assert rates["runs/429"].ok is False
    assert "8" in rates["runs/429"].detail
    errors = {check.name: check for check in harness.check_clean("runs", failed)}
    assert errors["runs/status"].ok is False
    assert "500" in errors["runs/status"].detail


def test_four_readers_failing_the_p95_headroom_are_named():
    single = harness.Load([harness.Result("u", 200, {}, 0.600)])
    concurrent = harness.Load([harness.Result("u", 200, {}, 1.500)])

    check = next(
        check for check in harness.check_concurrency("runs", single, concurrent)
        if check.name == "runs/4-readers p95"
    )
    assert check.ok is False
    assert "1.500s" in check.detail


def test_sub_millisecond_jitter_is_not_a_headroom_failure():
    """A fast endpoint's two samples can differ by the clock's resolution;
    that is not four readers falling over."""
    single = harness.Load([harness.Result("u", 200, {}, 0.0006)])
    concurrent = harness.Load([harness.Result("u", 200, {}, 0.0012)])

    check = next(
        check for check in harness.check_concurrency("runs", single, concurrent)
        if check.name == "runs/4-readers p95"
    )
    assert check.ok is True
    # Four Python threads share one interpreter: the ratio only has to catch
    # real serialization, and the absolute ceiling is one second.
    assert harness.CONCURRENCY_MIN_SECONDS == 1.0


# ---------------------------------------------------------------------------
# Completeness and the counts
# ---------------------------------------------------------------------------
def test_a_registry_gap_names_the_missing_capture():
    registry = {"a", "b"}
    store = {"a", "b", "c"}

    gap = harness.check_superset(registry, store, "loki")
    assert gap.ok is False
    assert "c" in gap.detail
    assert "1 missing" in gap.detail

    assert harness.check_superset(registry, {"a"}, "prometheus").ok is True


def test_a_registry_poll_lag_is_retried_once():
    """Prometheus scrapes before the registry polls. A capture that arrived
    seconds ago is not a lost capture."""
    checks = harness.check_store_supersets({"a"}, {"loki": {"a", "b"}}, lambda: {"a", "b"})
    assert all(check.ok for check in checks)


def test_a_real_gap_survives_the_retry():
    checks = harness.check_store_supersets({"a"}, {"loki": {"a", "b"}}, lambda: {"a"})
    assert [check.ok for check in checks] == [False]


def test_no_retry_when_every_store_is_covered():
    calls = []
    checks = harness.check_store_supersets({"a", "b"}, {"loki": {"a", "b"}}, lambda: calls.append(1))
    assert all(check.ok for check in checks)
    assert calls == []


def test_registry_ids_are_read_from_either_row_key():
    document = {"captures": [{"capture_id": "a"}, {"id": "b"}, {"suite": "caas"}]}

    assert harness.capture_ids_from_registry(document) == {"a", "b"}
    assert harness.capture_ids_from_registry(None) == set()


def test_truncated_must_agree_with_both_counts():
    document = {
        "days": 13,
        "shown_runs": 5,
        "total_runs": 12,
        "truncated": True,
        "runs": [{"id": str(value)} for value in range(5)],
    }

    checks = {check.name: check for check in harness.check_index("runs", document, 5)}
    assert checks["runs/limit"].ok is True
    assert checks["runs/truncated"].ok is True

    document["truncated"] = False
    checks = {check.name: check for check in harness.check_index("runs", document, 5)}
    assert checks["runs/truncated"].ok is False


def test_a_document_without_counts_fails_the_truncated_check():
    document = {"days": 13, "truncated": True, "runs": [{"id": "1"}]}

    checks = {check.name: check for check in harness.check_index("runs", document, 5)}
    assert checks["runs/truncated"].ok is False
    assert "no integer total count" in checks["runs/truncated"].detail


def test_the_window_metadata_is_never_mistaken_for_a_total():
    total, key = harness.total_and_shown({"days": 60, "limit": 200, "runs": [{}] * 5}, 5)

    assert total is None, "days and limit are window metadata, not row counts"
    assert key is None


def test_a_capped_completeness_answer_cannot_prove_a_superset():
    rows = [{"capture_id": str(value)} for value in range(10)]

    checks = {check.name: check for check in harness.check_coverage_window({"captures": rows}, 10)}
    assert checks["completeness/answer"].ok is False

    checks = {check.name: check for check in harness.check_coverage_window({"captures": rows[:9]}, 10)}
    assert checks["completeness/answer"].ok is True


# ---------------------------------------------------------------------------
# The verdict rules, checked against a document
# ---------------------------------------------------------------------------
def verdict_document(state: str = "ok", last_ok_at: str | None = None, coverage_from: str | None = None,
                     run_state: str = "unknown", created_at: str | None = None) -> dict:
    return {
        "source": {
            "loki": {
                "state": state,
                "last_ok_at": last_ok_at,
                "coverage_from": coverage_from or iso(60 * 24 * 20),
                "captures": 212,
                "rejected": 0,
            }
        },
        "runs": [{"created_at": created_at or iso(60), "capture_state": run_state}],
    }


def test_the_dead_source_rule_forbids_not_collected():
    now = datetime.now(UTC)
    dead = verdict_document(state="unknown", last_ok_at=None, run_state=harness.NOT_COLLECTED)

    checks = {check.name: check for check in harness.check_verdicts(dead, now)}
    assert checks["verdicts/dead-source"].ok is False

    healthy = verdict_document(state="ok", last_ok_at=iso(1), run_state="unknown")
    checks = {check.name: check for check in harness.check_verdicts(healthy, now)}
    assert checks["verdicts/dead-source"].ok is None, "the rule cannot be exercised when every source is healthy"


def test_a_run_younger_than_the_grace_period_is_never_not_collected():
    now = datetime.now(UTC)
    document = verdict_document(
        state="ok", last_ok_at=iso(1), run_state=harness.NOT_COLLECTED, created_at=iso(5)
    )

    checks = {check.name: check for check in harness.check_verdicts(document, now)}
    assert checks["verdicts/grace"].ok is False


def test_a_verdict_backed_by_an_old_discovery_fails():
    now = datetime.now(UTC)
    document = verdict_document(
        state="ok", last_ok_at=iso(180), run_state=harness.NOT_COLLECTED, created_at=iso(240)
    )
    document["runs"][0]["updated_at"] = iso(120)

    checks = {check.name: check for check in harness.check_verdicts(document, now)}
    assert checks["verdicts/freshness"].ok is False

    document["source"]["loki"]["last_ok_at"] = iso(5)
    checks = {check.name: check for check in harness.check_verdicts(document, now)}
    assert checks["verdicts/freshness"].ok is True


def test_a_run_before_coverage_is_never_not_collected():
    now = datetime.now(UTC)
    document = verdict_document(
        state="ok",
        last_ok_at=iso(1),
        coverage_from=iso(60 * 24 * 10),
        run_state=harness.NOT_COLLECTED,
        created_at=iso(60 * 24 * 20),
    )

    checks = {check.name: check for check in harness.check_verdicts(document, now)}
    assert checks["verdicts/coverage"].ok is False


def test_a_flat_source_block_is_read_as_one_registry_row():
    """The spec names the fields, not the container: one aggregate row and a
    row per source must both be understood."""
    flat = {
        "source": {
            "state": "unknown",
            "last_ok_at": None,
            "coverage_from": iso(60 * 24 * 20),
            "captures": 217,
            "rejected": 0,
        },
        "runs": [],
    }

    assert set(harness.normalize_sources(flat)) == {"registry"}


def test_an_answer_without_a_source_block_fails():
    now = datetime.now(UTC)

    checks = {check.name: check for check in harness.check_verdicts({"runs": []}, now)}
    assert checks["verdicts/dead-source"].ok is False


def test_the_runs_shape_requires_the_registry_block_and_counts():
    """The pre-registry document carries `tempo` and no counts; that must be
    a named shape failure, not a silently empty panel."""
    legacy = {"days": 7, "window_from": "", "window_to": "", "tempo": {}, "runs": [], "notes": []}

    checks = {check.name: check for check in harness.check_shape("runs", legacy)}
    assert checks["runs/keys"].ok is False
    assert "source" in checks["runs/keys"].detail
    assert "truncated" in checks["runs/keys"].detail


def test_a_source_row_without_counts_fails_the_shape_check():
    """Junk ids are counted, not dropped silently; the counts are part of the
    contract the dashboard reads."""
    document = {
        "days": 13,
        "runs": [],
        "truncated": False,
        "total_runs": 0,
        "source": {"loki": {"state": "ok", "last_ok_at": iso(1), "coverage_from": iso(60 * 24 * 20)}},
    }

    checks = {check.name: check for check in harness.check_shape("runs", document)}
    assert checks["runs/source shape"].ok is False
    assert "loki" in checks["runs/source shape"].detail


def test_an_unknown_cohort_value_fails_the_shape_check():
    unknown = {"cohorts": [{"suite": "unknown"}], "ungrouped": []}
    checks = {check.name: check for check in harness.check_shape("cohorts", unknown)}
    assert checks["cohorts/no-unknown"].ok is False

    named = {"cohorts": [{"suite": "caas", "unknown_runs": 0}], "ungrouped": []}
    checks = {check.name: check for check in harness.check_shape("cohorts", named)}
    assert checks["cohorts/no-unknown"].ok is True


def test_the_capture_view_has_no_not_collected_state():
    assert harness.NOT_COLLECTED not in harness.CAPTURE_VIEW_STATES
    document = {
        "capture_id": CAPTURE,
        "state": harness.NOT_COLLECTED,
        "session": {},
        "coverage": {},
        "identity": {},
        "tests": [],
        "waits": [],
        "fixtures": [],
        "fixture_uses": [],
        "failed_tests": [],
        "notes": [],
    }

    checks = {check.name: check for check in harness.check_shape("capture", document)}
    assert checks["capture/state"].ok is False


# ---------------------------------------------------------------------------
# Acceptance gate: the registry-backed endpoints
# ---------------------------------------------------------------------------
@requires_registry
def test_the_registry_indexes_make_no_outbound_call(api, monkeypatch, wfe):
    """The whole point of the registry: /runs, /captures and /cohorts read
    SQLite. Any outbound call there is the old Tempo path coming back."""
    def forbidden(*args, **kwargs):
        raise AssertionError("the read path made an outbound HTTP call")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(wfe.requests, "get", forbidden)

    for path in ("/api/ci-observe/runs?days=7", "/api/ci-observe/captures?days=7",
                 "/api/ci-observe/cohorts?days=7", "/api/ci-observe/prs?days=7"):
        status, document = get_json(api, path)
        assert status == 200, path
        assert isinstance(document, dict), path


@requires_registry
def test_the_capture_view_searches_tempo_at_most_once_for_four_readers(api, exporter, jobs, monkeypatch):
    """Single-flight: four readers that arrive together share one search.
    Zero searches is also correct when the registry already holds the
    session trace id; more than one is the thundering herd the cache exists
    to prevent."""
    tempo = FakeTempo()
    # The view reads its URL at construction, so the env var set after the
    # exporter exists would not reach it. Point the live object at the fake.
    exporter._capture_view.tempo_url = tempo.url
    try:
        job = run_job(RUN_ID)
        job["run_id"] = str(RUN_ID)
        exporter._upsert_job(job)
        results = harness.load(
            f"{api}/api/ci-observe/capture?capture_id={CAPTURE}", readers=4, per_reader=1, timeout=15
        )
    finally:
        tempo.close()

    assert [result.status for result in results.results] == [200, 200, 200, 200]
    states = {result.document.get("state") for result in results.results if result.document}
    assert len(states) == 1, f"four readers saw different states: {states}"
    assert states <= harness.CAPTURE_VIEW_STATES
    # The fake returns no traces, so a cold load searches twice: the session
    # span, then the test-span fallback. Four readers share those two
    # searches; eight means the single flight is not working.
    assert tempo.searches_for(CAPTURE) == 2


@requires_registry
def test_the_capture_cache_outlives_the_dashboard_refresh():
    """The spec says a 10-minute cache; the dashboard reloads every 5
    minutes. A shorter cache makes every reload pay the cold path."""
    refresh = dashboard_refresh_seconds()
    pattern = r"^(\s*)([A-Z][A-Z0-9_]*)\s*=\s*([0-9*\s+]+)$"
    ttls = []
    for path in [EXPORTER, *sorted(SCRIPTS.glob("*.py"))]:
        text = path.read_text()
        in_capture_view = "not_indexed" in text
        for match in re.finditer(pattern, text, re.M):
            name = match.group(2)
            if "CACHE_TTL" not in name:
                continue
            if not in_capture_view and "CAPTURE" not in name:
                continue
            seconds = 1
            for factor in match.group(3).split("*"):
                seconds *= int(factor.strip())
            ttls.append((path.name, name, seconds))

    assert ttls, "no capture view cache TTL found in the ci-observe scripts"
    for filename, name, seconds in ttls:
        if "NEGATIVE" in name or "INCOMPLETE" in name:
            # These hold a document that is expected to change: a missing,
            # busy, transient or in-progress capture. They are not the final
            # document cache under test, and must stay short.
            assert seconds <= 60, f"{filename}:{name}={seconds}s is not a short TTL"
            continue
        assert seconds >= refresh, f"{filename}:{name}={seconds}s is shorter than the {refresh}s refresh"
    assert max(seconds for _, _, seconds in ttls) >= 600, (
        f"no cache TTL reaches the spec's 10 minutes: {ttls}"
    )


@requires_registry
def test_the_run_index_reports_both_counts(jobs, api):
    status, document = get_json(api, "/api/ci-observe/runs?days=7&limit=5")

    assert status == 200
    checks = {check.name: check for check in harness.check_index("runs", document, 5)}
    assert checks["runs/limit"].ok is True
    assert checks["runs/truncated"].ok is True
    assert len(document["runs"]) == 5

    status, whole = get_json(api, "/api/ci-observe/runs?days=7&limit=2000")
    assert status == 200
    assert whole["truncated"] is False
    assert len(whole["runs"]) == 12


@requires_registry
def test_the_runs_document_carries_the_registry_block(jobs, api):
    status, document = get_json(api, "/api/ci-observe/runs?days=7")

    assert status == 200
    checks = harness.check_shape("runs", document)
    assert all(check.ok is True for check in checks), [check.render() for check in checks]
    assert harness.normalize_sources(document), "the source block has no per-source rows"


@requires_registry
@pytest.mark.parametrize(
    ("source_rows", "run_overrides", "expected"),
    [
        # No source has ever answered.
        ([], {}, "unknown"),
        # A source row exists but has no complete answer yet.
        ([{"source": "loki", "last_ok_at": None}], {}, "unknown"),
        # The run finished after the last complete discovery.
        (
            [{"source": "loki", "last_ok_at": iso(60), "last_ok_count": 212}],
            {"updated_at": iso(30)},
            "unknown",
        ),
        # The run just ended; telemetry may still arrive.
        (
            [{"source": "loki", "last_ok_at": iso(1), "last_ok_count": 212}],
            {"updated_at": iso(5)},
            "unknown",
        ),
        # The run is not finished, so nothing can be missing.
        (
            [{"source": "loki", "last_ok_at": iso(1), "last_ok_count": 212}],
            {"status": "in_progress", "conclusion": "", "updated_at": iso(120)},
            "unknown",
        ),
        # The run predates capture coverage.
        (
            [{"source": "loki", "last_ok_at": iso(5), "last_ok_count": 212,
              "coverage_from": iso(60 * 24)}],
            {"created_at": iso(60 * 24 * 3), "updated_at": iso(60 * 24 * 3 - 60)},
            "unknown",
        ),
        # Every rule passes: the capture is really absent. The registry
        # requires every source that covers the window, not just one.
        (
            [
                {"source": "prometheus", "last_ok_at": iso(5), "last_ok_count": 217,
                 "coverage_from": iso(60 * 24 * 20)},
                {"source": "loki", "last_ok_at": iso(5), "last_ok_count": 212,
                 "coverage_from": iso(60 * 24 * 20)},
                {"source": "info", "last_ok_at": iso(5), "last_ok_count": 217,
                 "coverage_from": iso(60 * 24 * 20)},
            ],
            {"updated_at": iso(120)},
            "not collected",
        ),
    ],
)
def test_the_verdict_matrix_never_turns_an_incomplete_source_into_not_collected(
    exporter, wfe, api, source_rows, run_overrides, expected
):
    exporter._upsert_job(run_job(**run_overrides))
    seed_source(exporter, wfe, source_rows)

    status, document = get_json(api, "/api/ci-observe/runs?days=7")

    assert status == 200
    rows = [row for row in document["runs"] if row["id"] == str(RUN_ID)]
    assert len(rows) == 1, document.get("notes")
    assert rows[0]["capture_state"] == expected


@requires_registry
def test_an_exception_path_never_produces_not_collected(api, exporter, wfe):
    """A source that recorded an error and never a complete answer leaves
    every run unknown. This is the failure the old endpoint produced as
    `not collected` for 9-11 runs."""
    exporter._upsert_job(run_job())
    seed_source(
        exporter,
        wfe,
        [{"source": "loki", "last_ok_at": None, "last_error": "connection refused",
          "last_error_at": iso(1)}],
    )

    status, document = get_json(api, "/api/ci-observe/runs?days=7")

    assert status == 200
    assert all(row["capture_state"] != harness.NOT_COLLECTED for row in document["runs"])
    assert harness.normalize_sources(document)


@requires_registry
def test_a_synthetic_capture_is_never_not_collected_and_answers_fast(api, exporter):
    exporter._capture_view.tempo_url = "http://127.0.0.1:1"

    started = time.monotonic()
    status, document = get_json(api, f"/api/ci-observe/capture?capture_id={SYNTHETIC_CAPTURE}")
    elapsed = time.monotonic() - started

    assert status == 200
    assert document.get("state") in harness.CAPTURE_VIEW_STATES
    assert document.get("state") != harness.NOT_COLLECTED
    assert document.get("notes")
    assert elapsed <= harness.BUDGETS["capture"][0], (
        f"{elapsed:.3f}s exceeds the cold Capture budget "
        f"{harness.BUDGETS['capture'][0]:.3f}s; a dead source must answer, not hang"
    )


@requires_registry
def test_four_readers_of_the_registry_indexes_get_no_429(api):
    for path in ("/api/ci-observe/runs?days=7&limit=200",
                 "/api/ci-observe/captures?days=7&limit=200"):
        samples = harness.load(f"{api}{path}", readers=4, per_reader=2, timeout=10)
        assert all(check.ok is True for check in harness.check_clean(path, samples)), (
            [check.render() for check in harness.check_clean(path, samples)]
        )
