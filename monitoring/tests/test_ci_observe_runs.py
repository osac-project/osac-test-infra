"""The ci-observe runs read path: /api/ci-observe/runs and the capture registry.

The workflow exporter knows every E2E run and, through the 60-second capture
registry, every capture that arrived. Both live in workflow-exporter.db, so
this endpoint is one SQL page joined on the GitHub run id and never calls
Tempo.

These tests pin the join, the keyset paging, the 30-day window, the three
registry endpoints, and above all the rule that keeps a data gap from
becoming a false verdict: a running run, a run that just ended, a registry
with no answer, a stale answer, an answer older than the run, a run older
than coverage, and a missing finish time are all `unknown`, never `not
collected`.

The live shapes are faked: Prometheus and Loki answer with the same
{"status": "success", "data": [...]} documents they return, and an info
answer is the vector `last_over_time(osac_ci_capture_info[15d])` returns.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from helpers import FakeResponse, make_job

MONITORING = Path(__file__).resolve().parents[1]
DASHBOARD = MONITORING / "config" / "grafana" / "dashboards" / "ci-observe-runs-home.json"
RUN_ID = 41535773459
SECOND_RUN_ID = 41535606958
CAPTURE = f"vmaas-main-{RUN_ID}-a1"

# The 11 live capture ids whose Prometheus label exists while Tempo, the old
# discovery source, did not list them (2026-11-04). Each run must resolve to
# its capture from the label value alone.
PROMETHEUS_ONLY_IDS = [
    41535773459, 41535606958, 41535448226, 41535784743, 41535773459,
    41535740828, 41535817688, 41535829377, 41535839995, 41535870771,
    41535890452,
]

IDENTITY = {
    "ci_run_id": str(RUN_ID),
    "ci_run_attempt": "1",
    "ci_suite": "vmaas",
    "ci_pr": "main",
    "ci_workflow": "E2E VMaaS Full Install",
    "ci_test_ref": "main",
    "ci_test_sha": "a" * 40,
    "ci_test_infra_ref": "main",
    "ci_test_infra_sha": "b" * 40,
    "ci_test_infra_workflow_sha": "c" * 40,
    "ci_test_suite": "tests/e2e",
    "ci_test_filter": "none",
    "ci_test_marker": "none",
}


def iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_job(run_id=RUN_ID, created_minutes=60, finished_minutes=50, **overrides) -> dict:
    """A run-level E2E row: its id is the GitHub run id and its run_id is its
    own id, which is what the run index lists."""
    row = make_job(
        id=run_id,
        workflow="E2E VMaaS Full Install",
        display_name="osac / E2E VMaaS Full Install",
        category="e2e",
        trigger="PR #123",
        conclusion="success",
        created_at=iso(created_minutes),
        updated_at=iso(finished_minutes),
        url=f"https://github.com/osac-project/osac/actions/runs/{run_id}",
    )
    row.update(overrides)
    return row


class FakeSources:
    """Stands in for Prometheus, Loki and the metrics query.

    `prometheus`, `loki` and `info` are the values/series each source
    returns; `failures` maps a source name to the HTTP status that source
    should fail with. Every request is recorded so a test can prove the
    request path never made one.
    """

    def __init__(self):
        self.prometheus = []
        self.loki = []
        self.info = []
        self.failures = {}
        self.calls = []

    def get(self, url, timeout=None, headers=None):
        self.calls.append(url)
        if "/loki/api/v1/label/" in url:
            source = "loki"
        elif "/api/v1/label/" in url:
            source = "prometheus"
        elif "/api/v1/query" in url:
            source = "info"
        else:
            return FakeResponse(404, text=f"unknown url {url}")
        if source in self.failures:
            return FakeResponse(self.failures[source], text=f"{source} is down")
        if source == "info":
            data = {
                "resultType": "vector",
                "result": [
                    {"metric": labels, "value": [datetime.now(timezone.utc).timestamp(), "1"]}
                    for labels in self.info
                ],
            }
        else:
            data = list(getattr(self, source))
        return FakeResponse(200, payload={"status": "success", "data": data})

    def count(self, source):
        needles = {
            "prometheus": "/api/v1/label/",
            "loki": "/loki/api/v1/label/",
            "info": "/api/v1/query",
        }
        return sum(1 for url in self.calls if needles[source] in url)


@pytest.fixture
def sources(exporter, wfe, monkeypatch):
    """The fake sources. Depends on the exporter fixture so its patch of
    requests.get is applied after the fake GitHub's."""
    fake = FakeSources()
    monkeypatch.setattr(wfe.requests, "get", fake.get)
    return fake


@pytest.fixture
def registry(exporter, sources):
    return exporter._captures


@pytest.fixture
def client(runs, exporter):
    return runs.CIObserveRuns(exporter)


@pytest.fixture
def jobs(exporter):
    for row in (run_job(RUN_ID), run_job(SECOND_RUN_ID, conclusion="failure")):
        exporter._upsert_job(row)
    return exporter


@pytest.fixture
def fresh(registry, sources):
    """A registry with a complete answer from all three sources."""
    sources.prometheus = [CAPTURE, f"caas-main-{SECOND_RUN_ID}-a1"]
    sources.loki = [CAPTURE, f"caas-main-{SECOND_RUN_ID}-a1"]
    sources.info = [
        IDENTITY | {"osac_ci_capture_id": CAPTURE},
        IDENTITY | {
            "osac_ci_capture_id": f"caas-main-{SECOND_RUN_ID}-a1",
            "ci_run_id": str(SECOND_RUN_ID),
            "ci_suite": "caas",
        },
    ]
    registry.refresh()
    return registry


def clear_captures(exporter):
    """Start a test from an empty captures table (the fresh fixture seeds two
    rows on purpose, for the common case)."""
    with exporter._db() as conn:
        conn.execute("DELETE FROM captures")


def state_of(doc, run_id):
    return next(run for run in doc["runs"] if run["id"] == str(run_id))


# ---------------------------------------------------------------------------
# The join: already-known live captures
# ---------------------------------------------------------------------------
def test_the_live_prometheus_only_captures_resolve(client, exporter, fresh, sources):
    """The 11 live ids the old Tempo lookup missed. Each is a Prometheus label
    value; the registry must list every one and join it to its run."""
    clear_captures(exporter)
    sources.prometheus = [f"vmaas-main-{run_id}-a1" for run_id in PROMETHEUS_ONLY_IDS]
    sources.loki = []
    sources.info = []
    for run_id in PROMETHEUS_ONLY_IDS:
        exporter._upsert_job(run_job(run_id, created_minutes=30, finished_minutes=20))
    fresh.refresh()

    doc = client.get_json({"days": ["30"], "limit": ["2000"]})

    got = {run["id"]: run for run in doc["runs"] if run["id"] in {str(i) for i in PROMETHEUS_ONLY_IDS}}
    assert len(got) == len(set(PROMETHEUS_ONLY_IDS))
    for run_id in set(PROMETHEUS_ONLY_IDS):
        assert got[str(run_id)]["capture_state"] == "capture"
        assert got[str(run_id)]["capture_ids"] == [f"vmaas-main-{run_id}-a1"]
    assert doc["source"]["captures"] == len(set(PROMETHEUS_ONLY_IDS))
    assert doc["source"]["rejected"]["count"] == 0


def test_a_capture_the_info_metric_alone_carries_is_still_a_capture(client, exporter, fresh, sources):
    """The info metric is the identity source, and it also counts as a
    discovery source: a capture whose label values have expired is still
    known, so its run is never `not collected`."""
    clear_captures(exporter)
    sources.prometheus = []
    sources.loki = []
    sources.info = [IDENTITY | {"osac_ci_capture_id": CAPTURE}]
    exporter._upsert_job(run_job(RUN_ID))
    fresh.refresh()

    doc = client.get_json({"days": ["7"]})

    run = state_of(doc, RUN_ID)
    assert run["capture_state"] == "capture"
    assert run["capture_ids"] == [CAPTURE]
    assert run["identity_source"] == "info"


def test_a_run_with_a_capture_is_marked_capture(client, jobs, fresh):
    doc = client.get_json({"days": ["7"]})

    states = {run["id"]: run["capture_state"] for run in doc["runs"]}
    assert states == {str(RUN_ID): "capture", str(SECOND_RUN_ID): "capture"}
    run = state_of(doc, RUN_ID)
    assert run["capture_ids"] == [CAPTURE]
    assert run["capture_reason"] == ""
    # Grafana data links read epoch milliseconds. The run's start and finish
    # are supplied in that form because its URL parser ignores ISO strings.
    assert isinstance(run["started_at_ms"], int) and run["started_at_ms"] > 1_600_000_000_000
    assert isinstance(run["done_at_ms"], int) and run["done_at_ms"] >= run["started_at_ms"]
    assert doc["source"]["state"] == "ok"
    assert doc["source"]["captures"] == 2


def test_job_rows_are_not_listed_as_runs(client, exporter, fresh):
    """A pull-request-flow run's suite rows are jobs. They group into their
    parent run, so the suite's job id never appears as a run id and the run is
    listed once, not once per suite."""
    exporter._upsert_job(run_job(RUN_ID))
    exporter._upsert_job(make_job(
        id=41310072001,
        workflow="E2E vmaas (pull requests)",
        category="e2e",
        created_at=iso(30),
        updated_at=iso(20),
        run_id=RUN_ID,
        url=f"https://github.com/osac-project/osac/actions/runs/{RUN_ID}/job/41310072001",
    ))

    doc = client.get_json({"days": ["7"]})

    assert [run["id"] for run in doc["runs"]] == [str(RUN_ID)]
    suite = exporter.get_ci_observe_run_page(days=7, limit=10)["rows"]
    assert [row["id"] for row in suite] == [str(RUN_ID)]
    assert "E2E vmaas (pull requests)" in suite[0]["workflow"]


def test_the_exact_lookup_rejects_a_non_numeric_run_id(client, exporter, fresh):
    """A cast would turn 123abc into 123 and answer for a different run."""
    exporter._upsert_job(run_job(RUN_ID))

    doc = client.get_json({"days": ["7"], "run": [f"{RUN_ID}abc"]})

    assert doc["runs"] == []
    assert any(f"no run with id {RUN_ID}abc" in note for note in doc["notes"])


def test_unusual_conclusions_keep_their_name(client, exporter, fresh):
    """Only failure, timed_out and startup_failure mean a failed run; the
    rest must not be rewritten to success."""
    cases = {"timed_out": "failure", "startup_failure": "failure", "action_required": "action_required"}
    for offset, (stored, _) in enumerate(cases.items()):
        row = run_job(RUN_ID + offset)
        row["conclusion"] = stored
        exporter._upsert_job(row)

    doc = client.get_json({"days": ["7"]})
    by_id = {run["id"]: run["conclusion"] for run in doc["runs"]}

    for offset, (stored, expected) in enumerate(cases.items()):
        assert by_id[str(RUN_ID + offset)] == expected, stored


def test_run_id_is_backfilled_from_the_run_url(wfe, tmp_path, monkeypatch, github):
    """Rows stored before the column existed get their run id from the URL:
    the run id for a run row and the parent run id for a .../job/ row."""
    import sqlite3

    db = tmp_path / "workflow-exporter.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, repo TEXT, workflow TEXT, category TEXT, "
        "event TEXT, conclusion TEXT, failed_step TEXT, steps_json TEXT, created_at TEXT, "
        "url TEXT, run_attempt INTEGER)"
    )
    conn.executemany(
        "INSERT INTO jobs (id, repo, workflow, category, conclusion, created_at, url, run_attempt) "
        "VALUES (?, 'osac', 'E2E VMaaS', 'e2e', 'success', '2026-11-01T10:00:00Z', ?, 1)",
        [
            (500, "https://github.com/osac-project/osac/actions/runs/500"),
            (501, "https://github.com/osac-project/osac/actions/runs/500/job/501"),
            (502, "https://github.com/osac-project/osac/actions/runs/502"),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(wfe, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(wfe, "DB_FILE", str(db))
    monkeypatch.setattr(wfe, "LEGACY_CACHE_FILE", str(tmp_path / "none.json"))
    monkeypatch.setattr(wfe, "TOKEN", None)
    exporter = wfe.WorkflowExporter()

    with exporter._db() as conn:
        got = {
            row["id"]: row["run_id"]
            for row in conn.execute("SELECT id, run_id FROM jobs")
        }
    assert got == {500: "500", 501: "500", 502: "502"}


# ---------------------------------------------------------------------------
# Never a fake verdict
# ---------------------------------------------------------------------------
def test_a_running_run_is_unknown(client, exporter, fresh):
    clear_captures(exporter)
    exporter._upsert_job(run_job(RUN_ID, conclusion="", status="in_progress", updated_at=iso(1)))

    doc = client.get_json({"days": ["7"]})

    run = state_of(doc, RUN_ID)
    assert run["capture_state"] == "unknown"
    assert "has not finished" in run["capture_reason"]
    assert run["finished"] is False


def test_a_just_ended_run_is_unknown(client, exporter, fresh):
    clear_captures(exporter)
    exporter._upsert_job(run_job(RUN_ID, created_minutes=20, finished_minutes=5))

    doc = client.get_json({"days": ["7"]})

    run = state_of(doc, RUN_ID)
    assert run["capture_state"] == "unknown"
    assert "just ended" in run["capture_reason"]


def test_a_run_without_a_finish_time_is_unknown(client, exporter, fresh):
    clear_captures(exporter)
    exporter._upsert_job(run_job(RUN_ID, updated_at=""))

    doc = client.get_json({"days": ["7"]})

    assert "no finish time" in state_of(doc, RUN_ID)["capture_reason"]


def test_a_registry_with_no_answer_is_unknown(client, jobs, registry):
    doc = client.get_json({"days": ["7"]})

    assert doc["source"]["state"] == "unknown"
    assert {run["capture_state"] for run in doc["runs"]} == {"unknown"}
    assert all("no complete answer" in run["capture_reason"] for run in doc["runs"])
    assert "never as not collected" in doc["notes"][-1]


def test_no_complete_discovery_since_the_run_finished_is_unknown(client, jobs, registry):
    """A run that finished after the registry's last complete answer may have
    sent its capture since. Its absence proves nothing."""
    with registry.exporter._db() as conn:
        conn.execute(
            "INSERT INTO capture_source (source, coverage_from, last_ok_at, last_ok_count) "
            "VALUES ('prometheus', ?, ?, 1)",
            (iso(14 * 24 * 60), iso(60)),
        )

    doc = client.get_json({"days": ["7"]})

    assert doc["source"]["state"] == "partial"
    assert "no complete discovery since" in state_of(doc, RUN_ID)["capture_reason"]


def test_a_stale_registry_is_unknown(client, exporter, registry):
    exporter._upsert_job(run_job(RUN_ID, created_minutes=240, finished_minutes=200))
    with registry.exporter._db() as conn:
        for source in ("prometheus", "loki", "info"):
            conn.execute(
                "INSERT INTO capture_source (source, coverage_from, last_ok_at, last_ok_count) "
                "VALUES (?, ?, ?, 1)",
                (source, iso(14 * 24 * 60), iso(120)),
            )

    doc = client.get_json({"days": ["7"]})

    assert doc["source"]["state"] == "partial"
    assert "stale" in state_of(doc, RUN_ID)["capture_reason"]


def test_a_run_older_than_coverage_is_unknown(client, exporter, fresh):
    clear_captures(exporter)
    """Prometheus keeps 30 days and the registry's coverage starts a day
    inside that. An older run's capture may have expired, so its absence is
    not evidence."""
    exporter._upsert_job(run_job(
        RUN_ID, created_minutes=30 * 24 * 60, finished_minutes=30 * 24 * 60 - 10
    ))

    doc = client.get_json({"days": ["30"]})

    run = state_of(doc, RUN_ID)
    assert run["capture_state"] == "unknown"
    assert "predates capture coverage" in run["capture_reason"]


def test_a_run_without_created_at_is_unknown(client, exporter, fresh):
    clear_captures(exporter)
    exporter._upsert_job(run_job(RUN_ID, created_at=""))

    doc = client.get_json({"days": ["7"], "run": [str(RUN_ID)]})

    assert "no created_at" in state_of(doc, RUN_ID)["capture_reason"]


def test_one_source_never_answering_is_unknown(client, jobs, registry, sources):
    """Prometheus and Loki answered; the info source never has. The identity
    may be missing from every capture, but more importantly the union is not
    complete, so a missing capture is unknown."""
    sources.failures = {"info": 503}
    registry.refresh()

    doc = client.get_json({"days": ["7"]})

    assert doc["source"]["state"] == "partial"
    assert {run["capture_state"] for run in doc["runs"]} == {"unknown"}
    assert "info: " in doc["notes"][-1]


def test_every_source_failing_is_unknown(client, jobs, registry, sources):
    sources.failures = {"prometheus": 500, "loki": 500, "info": 500}
    registry.refresh()

    doc = client.get_json({"days": ["7"]})

    assert doc["source"]["state"] == "unknown"
    assert {run["capture_state"] for run in doc["runs"]} == {"unknown"}
    assert doc["source"]["rejected"]["count"] == 0


def test_a_junk_capture_id_is_rejected_and_named(client, jobs, registry, sources):
    sources.prometheus = ["not-a-capture", CAPTURE]
    registry.refresh()

    doc = client.get_json({"days": ["7"]})

    assert doc["source"]["rejected"]["count"] == 1
    assert doc["source"]["rejected"]["ids"] == ["not-a-capture"]
    assert state_of(doc, RUN_ID)["capture_state"] == "capture"


# ---------------------------------------------------------------------------
# Paging and the 30-day window
# ---------------------------------------------------------------------------
def test_the_run_page_is_one_sql_statement(client, exporter, wfe, monkeypatch):
    exporter._upsert_job(run_job(RUN_ID))
    statements = []
    real_db = wfe.WorkflowExporter._db

    def traced(self):
        conn = real_db(self)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(wfe.WorkflowExporter, "_db", traced)
    exporter.get_ci_observe_run_page(days=7, limit=10)

    queries = [sql for sql in statements if "FROM jobs" in sql]
    assert len(queries) == 1
    assert "LEFT JOIN captures" in queries[0]
    assert "COUNT(*) OVER ()" in queries[0]


def test_the_run_page_pages_by_run_id_without_gaps(client, exporter, fresh):
    run_ids = [RUN_ID + index for index in range(25)]
    for index, run_id in enumerate(run_ids):
        exporter._upsert_job(run_job(
            run_id, created_minutes=200 - index, finished_minutes=190 - index
        ))

    first = client.get_json({"days": ["7"], "limit": ["10"]})
    second = client.get_json({"days": ["7"], "limit": ["10"], "before": [first["next_before"]]})
    third = client.get_json({"days": ["7"], "limit": ["10"], "before": [second["next_before"]]})

    assert first["truncated"] is True
    assert first["counts"]["returned"] == 10
    assert first["counts"]["matched"] == 25
    assert first["counts"]["capture"] + first["counts"]["not collected"] + first["counts"]["unknown"] == 25
    assert second["truncated"] is True
    assert third["truncated"] is False
    assert third["next_before"] is None
    seen = [run["id"] for doc in (first, second, third) for run in doc["runs"]]
    assert len(seen) == len(set(seen)) == 25
    assert seen == [str(run_id) for run_id in sorted(run_ids, reverse=True)]


def test_an_invalid_before_is_ignored_with_a_note(client, jobs, fresh):
    doc = client.get_json({"days": ["7"], "before": ["not-a-run-id"]})

    assert doc["counts"]["returned"] == 2
    assert "is not a run id" in doc["notes"][0]


def test_a_thirty_day_window_keeps_every_day(client, exporter, fresh, wfe):
    """The count cap must not cut a full-retention page. One run per day for
    30 days must all be listed."""
    assert wfe.JOBS_HISTORY_MAX_COUNT >= 300_000
    exporter._upsert_job(run_job(1, created_minutes=29 * 24 * 60, finished_minutes=29 * 24 * 60 - 10))
    for day in range(1, 30):
        exporter._upsert_job(run_job(
            1 + day,
            created_minutes=day * 24 * 60,
            finished_minutes=day * 24 * 60 - 10,
        ))

    exporter._prune_jobs()
    doc = client.get_json({"days": ["30"], "limit": ["2000"]})

    assert len(doc["runs"]) == 30
    assert doc["counts"]["matched"] == 30
    assert doc["truncated"] is False


def test_a_pr_suite_run_reports_its_pull_request(client, exporter, fresh):
    exporter._upsert_job(run_job(
        workflow="E2E suite · caas · #1620 · " + "a" * 40,
        trigger="manual",
    ))

    run = client.get_json({"days": ["30"]})["runs"][0]

    assert run["workflow"] == "E2E CaaS Full Install"
    assert run["pr"] == "#1620"
    assert run["trigger"] == "PR"
    assert run["pr_url"] == "https://github.com/osac-project/osac/pull/1620"


def test_run_is_an_exact_lookup_outside_the_window(client, exporter, fresh):
    exporter._upsert_job(run_job(RUN_ID))
    exporter._upsert_job(run_job(
        SECOND_RUN_ID, created_minutes=45 * 24 * 60, finished_minutes=45 * 24 * 60 - 10
    ))

    doc = client.get_json({"days": ["7"], "run": [str(SECOND_RUN_ID)]})

    assert [run["id"] for run in doc["runs"]] == [str(SECOND_RUN_ID)]
    assert doc["counts"]["returned"] == 1
    assert doc["counts"]["matched"] == 1
    assert doc["counts"]["capture"] + doc["counts"]["not collected"] + doc["counts"]["unknown"] == 1


def test_an_unknown_run_id_is_an_empty_page_with_a_note(client, jobs, fresh):
    doc = client.get_json({"days": ["7"], "run": ["999"]})

    assert doc["runs"] == []
    assert "no run with id 999" in doc["notes"][-1]


@pytest.mark.parametrize(
    ("params", "expected_days", "expected_limit"),
    [
        ({}, 30, 200),
        ({"days": ["0"]}, 1, 200),
        ({"days": ["9999"]}, 30, 200),
        ({"days": ["nonsense"]}, 30, 200),
        ({"limit": ["0"]}, 30, 1),
        ({"limit": ["999999"]}, 30, 2000),
        ({"limit": ["nonsense"]}, 30, 200),
    ],
)
def test_days_and_limit_are_clamped(client, jobs, fresh, params, expected_days, expected_limit):
    doc = client.get_json(params)

    assert doc["days"] == expected_days
    assert 0 < len(doc["runs"]) <= expected_limit


def test_an_empty_job_history_still_answers(client, sources):
    doc = client.get_json({"days": ["7"]})

    assert doc["runs"] == []
    assert sources.calls == [], "no runs means no reason to query any source"
    assert "no E2E runs" in doc["notes"][0]


# ---------------------------------------------------------------------------
# No Tempo, and the Pull requests read side uses the registry
# ---------------------------------------------------------------------------
def test_the_request_path_never_calls_any_http_source(client, jobs, fresh, sources):
    sources.calls.clear()

    doc = client.get_json({"days": ["60"]})

    assert doc["runs"]
    assert sources.calls == [], "/runs reads SQLite only"


def test_the_runs_module_has_no_tempo_lookup():
    import ci_observe_runs

    assert not hasattr(ci_observe_runs.CIObserveRuns, "TEMPO_TIMEOUT_SECONDS")
    assert not hasattr(ci_observe_runs.CIObserveRuns, "_tempo_capture_ids")
    assert not hasattr(ci_observe_runs, "CAPTURE_TAG")


def test_capture_ids_by_run_reads_the_registry_without_tempo(client, exporter, fresh, sources):
    sources.calls.clear()

    captures = client.capture_ids_by_run(days=7)

    assert captures[str(RUN_ID)] == [CAPTURE]
    assert sources.calls == []


# ---------------------------------------------------------------------------
# /api/ci-observe/captures and /api/ci-observe/cohorts
# ---------------------------------------------------------------------------
def test_captures_are_filterable_and_counted(registry, fresh):
    doc = registry.captures_json({"days": ["7"]})
    assert doc["counts"] == {"returned": 2, "matched": 2}
    assert {row["capture_id"] for row in doc["captures"]} == {
        CAPTURE, f"caas-main-{SECOND_RUN_ID}-a1",
    }
    for field in (
        "capture_id", "run_id", "suite", "pr", "workflow", "test_ref",
        "test_sha", "test_infra_ref", "test_infra_sha",
        "test_infra_workflow_sha", "test_suite", "test_filter", "test_marker",
        "session_trace_id", "identity_source", "capture_started_at",
        "first_seen_at", "last_seen_at", "capture_window_from",
        "capture_window_to", "capture_window_from_ms", "capture_window_to_ms",
    ):
        assert field in doc["captures"][0], f"capture row is missing {field}"
    assert isinstance(doc["captures"][0]["capture_window_from_ms"], int)
    assert isinstance(doc["captures"][0]["capture_window_to_ms"], int)

    one = registry.captures_json({"days": ["7"], "run_id": [str(RUN_ID)]})
    assert [row["capture_id"] for row in one["captures"]] == [CAPTURE]

    by_suite = registry.captures_json({"days": ["7"], "suite": ["caas"]})
    assert [row["capture_id"] for row in by_suite["captures"]] == [f"caas-main-{SECOND_RUN_ID}-a1"]

    limited = registry.captures_json({"days": ["7"], "limit": ["1"]})
    assert limited["truncated"] is True
    assert limited["counts"] == {"returned": 1, "matched": 2}


def test_a_pull_request_flow_run_is_listed_once_with_its_capture(client, exporter, sources, registry, fresh):
    """e2e-pr.yml stores one e2e row per suite: its id is the suite job id and
    its run_id is the parent run. The run index must list that run with its
    capture, not skip it (the old id-equality filter) and not list it twice."""
    parent = RUN_ID + 100
    for offset, suite in enumerate(("vmaas", "caas")):
        job = run_job(
            parent,
            id=parent * 10 + offset,
            workflow=f"E2E {suite} (pr)",
            url=f"https://github.com/osac-project/osac/actions/runs/{parent}/job/{parent * 10 + offset}",
            created_minutes=30,
            finished_minutes=20,
        )
        job["run_id"] = str(parent)
        exporter._upsert_job(job)
    capture = f"vmaas-pr9-{parent}-a1"
    sources.prometheus = [capture]
    sources.loki = []
    sources.info = []
    registry.refresh()

    doc = client.get_json({"days": ["7"]})

    rows = [run for run in doc["runs"] if run["id"] == str(parent)]
    assert len(rows) == 1, doc["runs"]
    assert rows[0]["capture_state"] == "capture"
    assert rows[0]["capture_id"] == capture
    assert rows[0]["workflow"] == "E2E caas (pr),E2E vmaas (pr)"
    assert rows[0]["url"] == f"https://github.com/osac-project/osac/actions/runs/{parent}"
    assert rows[0]["conclusion"] == "success"

    exact = client.get_json({"days": ["7"], "run": [str(parent)]})
    assert [run["id"] for run in exact["runs"]] == [str(parent)]

    # Paging by run id still neither repeats nor skips a run when one of them
    # is stored as several suite rows.
    exporter._upsert_job(run_job(parent + 1, created_minutes=40, finished_minutes=35))
    exporter._upsert_job(run_job(parent - 1, created_minutes=10, finished_minutes=5))
    seen = []
    before = ""
    for _ in range(4):
        page = client.get_json({"days": ["7"], "limit": ["1"], "before": [before]})
        if not page["runs"]:
            break
        seen.append(page["runs"][0]["id"])
        before = page["next_before"] or ""
        if not before:
            break
    assert seen == [str(parent + 1), str(parent), str(parent - 1)]
    assert len(set(seen)) == len(seen)


def test_an_empty_run_lookup_returns_no_rows(client, exporter, fresh):
    """The Find box with nothing typed sends run=. That must answer "no
    rows", not fall back to the whole window."""
    exporter._upsert_job(run_job(RUN_ID))

    doc = client.get_json({"days": ["7"], "run": [""]})

    assert doc["runs"] == []
    assert doc["total"] == 0
    assert doc["counts"]["matched"] == 0
    assert any("Find box is empty" in note for note in doc["notes"])


def test_an_empty_capture_lookup_returns_no_rows(registry, exporter, sources, fresh):
    sources.prometheus = [CAPTURE]
    sources.loki = []
    sources.info = [IDENTITY | {"osac_ci_capture_id": CAPTURE}]
    registry.refresh()

    doc = registry.captures_json({"days": ["7"], "capture_id": [""]})

    assert doc["captures"] == []
    assert doc["counts"] == {"returned": 0, "matched": 0}
    assert any("lookup is empty" in note for note in doc["notes"])


def test_the_verdict_counts_cover_the_whole_window(client, exporter, fresh):
    """The cards must not be a page size: ten runs with limit=2 still report
    the whole-window totals."""
    for index in range(10):
        exporter._upsert_job(run_job(RUN_ID + index, created_minutes=60 - index, finished_minutes=50 - index))

    doc = client.get_json({"days": ["7"], "limit": ["2"]})

    assert doc["shown"] == 2
    assert doc["total"] == 10
    assert doc["counts"]["matched"] == 10
    assert doc["counts"]["capture"] + doc["counts"]["not collected"] + doc["counts"]["unknown"] == 10


def test_cohorts_group_on_the_six_identity_fields(registry, exporter, sources, fresh):
    clear_captures(exporter)
    base = IDENTITY | {"osac_ci_capture_id": CAPTURE, "ci_run_id": str(RUN_ID)}
    same = base | {"osac_ci_capture_id": f"caas-main-{SECOND_RUN_ID}-a1", "ci_run_id": str(SECOND_RUN_ID)}
    other_sha = base | {"osac_ci_capture_id": "vmaas-pr7-77-a1", "ci_run_id": "77", "ci_test_sha": "d" * 40}
    incomplete = base | {"osac_ci_capture_id": "vmaas-main-78-a1", "ci_run_id": "78", "ci_test_infra_sha": ""}
    unknown = base | {"osac_ci_capture_id": "vmaas-main-79-a1", "ci_run_id": "79", "ci_test_sha": "unknown"}
    unknown_suite = base | {"osac_ci_capture_id": "vmaas-main-81-a1", "ci_run_id": "81", "ci_test_suite": "unknown"}
    empty_filter = base | {"osac_ci_capture_id": "vmaas-main-82-a1", "ci_run_id": "82", "ci_test_filter": ""}
    sources.prometheus = [
        CAPTURE, f"caas-main-{SECOND_RUN_ID}-a1", "vmaas-pr7-77-a1",
        "vmaas-main-78-a1", "vmaas-main-79-a1", "vmaas-main-80-a1",
        "vmaas-main-81-a1", "vmaas-main-82-a1",
    ]
    sources.loki = []
    sources.info = [base, same, other_sha, incomplete, unknown, unknown_suite, empty_filter]
    registry.refresh()

    doc = registry.cohorts_json({"days": ["7"]})

    assert doc["group_by"] == [
        "test_sha", "test_infra_sha", "test_infra_workflow_sha",
        "test_suite", "test_filter", "test_marker",
    ]
    assert len(doc["cohorts"]) == 2
    first = next(row for row in doc["cohorts"] if row["test_sha"] == "a" * 40)
    assert first["captures"] == 2
    # The requested ref is metadata, not a group key: one commit, one cohort.
    assert first["test_ref"] == "main"
    assert first["capture_ids"] == sorted([CAPTURE, f"caas-main-{SECOND_RUN_ID}-a1"])
    assert all(row["test_sha"] for row in doc["cohorts"])
    # No cohort value is ever "unknown".
    for row in doc["cohorts"]:
        assert "unknown" not in row.values()
    ungrouped = {row["capture_id"]: row for row in doc["ungrouped"]}
    assert set(ungrouped) == {
        "vmaas-main-78-a1", "vmaas-main-79-a1", "vmaas-main-80-a1",
        "vmaas-main-81-a1", "vmaas-main-82-a1",
    }
    assert {row["identity_source"] for row in doc["ungrouped"]} == {"none", "info"}
    assert ungrouped["vmaas-main-78-a1"]["reason"] == "missing identity: test_infra_sha"
    assert ungrouped["vmaas-main-79-a1"]["reason"] == "unknown identity: test_sha"
    assert ungrouped["vmaas-main-80-a1"]["reason"] == "no identity from the info metric"
    # An empty filter is a real value; `unknown` is not.
    assert ungrouped["vmaas-main-81-a1"]["reason"] == "unknown identity: test_suite"
    # An empty label means the info metric never carried it: the Compare span
    # queries reject it, so it must not key a cohort.
    assert ungrouped["vmaas-main-82-a1"]["reason"] == "missing identity: test_filter"
    for row in doc["ungrouped"]:
        assert {
            "capture_started_at", "last_seen_at", "capture_window_from",
            "capture_window_to", "capture_window_from_ms", "capture_window_to_ms",
        } <= set(row)
        assert row["capture_window_from"]
        assert row["capture_window_to"]
        # Grafana links need epoch ms; the ISO strings are for display only.
        assert isinstance(row["capture_window_from_ms"], int)
        assert isinstance(row["capture_window_to_ms"], int)
    assert "source" in doc and "notes" in doc


# ---------------------------------------------------------------------------
# Wiring: the exporter methods and the HTTP routes
# ---------------------------------------------------------------------------
def test_the_exporter_exposes_the_endpoints(exporter, jobs, fresh):
    runs_doc = exporter.get_ci_observe_runs_json({"days": ["7"]})
    captures_doc = exporter.get_ci_observe_captures_json({"days": ["7"]})
    cohorts_doc = exporter.get_ci_observe_cohorts_json({"days": ["7"]})

    assert {run["id"] for run in runs_doc["runs"]} == {str(RUN_ID), str(SECOND_RUN_ID)}
    assert captures_doc["counts"]["matched"] == 2
    assert cohorts_doc["cohorts"]


class TestHTTP:
    @pytest.fixture
    def base_url(self, wfe, exporter, monkeypatch):
        monkeypatch.setattr(wfe.ExporterHandler, "exporter", exporter)
        server = ThreadingHTTPServer(("127.0.0.1", 0), wfe.ExporterHandler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_address[1]}"
        server.shutdown()
        server.server_close()

    @staticmethod
    def get(url):
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.headers, response.read()

    def test_runs_json_shape_and_cors(self, exporter, jobs, fresh, base_url):
        status, headers, body = self.get(f"{base_url}/api/ci-observe/runs?days=7&limit=10")

        assert status == 200
        assert headers.get("Content-Type") == "application/json"
        assert headers.get("Access-Control-Allow-Origin") == "*"
        doc = json.loads(body)
        assert doc["days"] == 7
        assert set(doc) == {
            "days", "window_from", "window_to", "source", "runs", "truncated",
            "shown", "total", "counts", "next_before", "notes",
        }
        assert doc["shown"] == 2
        assert doc["total"] == 2
        assert doc["truncated"] is False
        assert set(doc["source"]) == {
            "state", "last_ok_at", "coverage_from", "captures", "rejected", "sources",
        }
        first = next(run for run in doc["runs"] if run["id"] == str(RUN_ID))
        for field in (
            "id", "workflow", "trigger", "branch", "conclusion", "created_at",
            "started_at", "finished_at", "url", "capture_state", "capture_ids",
            "capture_id", "capture_reason", "identity_source",
        ):
            assert field in first, f"run row is missing {field}"
        assert first["capture_id"] == CAPTURE
        assert first["started_at"] == first["created_at"]
        assert first["finished_at"]
        assert "matched" not in first, "the window count is not part of a run row"
        assert {run["id"]: run["capture_state"] for run in doc["runs"]} == {
            str(RUN_ID): "capture",
            str(SECOND_RUN_ID): "capture",
        }

    def test_an_empty_registry_is_still_200(self, exporter, jobs, base_url):
        status, _, body = self.get(f"{base_url}/api/ci-observe/runs")

        assert status == 200
        assert {run["capture_state"] for run in json.loads(body)["runs"]} == {"unknown"}

    def test_captures_and_cohorts_routes(self, exporter, fresh, base_url):
        status, _, body = self.get(f"{base_url}/api/ci-observe/captures?days=7")
        assert status == 200
        assert len(json.loads(body)["captures"]) == 2

        status, _, body = self.get(f"{base_url}/api/ci-observe/cohorts?days=7")
        assert status == 200
        cohorts = json.loads(body)["cohorts"]
        assert len(cohorts) == 1
        assert cohorts[0]["captures"] == 2


# ---------------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------------
def test_the_runs_module_is_mounted_and_copied():
    quadlet = (MONITORING / "quadlet" / "workflow-exporter.container").read_text()
    setup = (MONITORING / "scripts" / "monitoring-setup.sh").read_text()

    assert "ci_observe_runs.py:/app/ci_observe_runs.py" in quadlet
    assert "scripts/ci_observe_runs.py" in setup
    # A first install (--central) must copy every module the quadlet mounts,
    # not only the ones --update-central refreshes.
    first_install = setup.split("Phase 3: Deploy Quadlet units")[0]
    for module in (
        "workflow-exporter.py",
        "ci_observe_run_evidence.py",
        "ci_observe_runs.py",
        "ci_observe_capture_view.py",
    ):
        assert f"scripts/{module}" in first_install, f"a first install does not copy {module}"


def test_the_quadlet_reaches_the_registry_sources_on_the_host():
    """The registry polls the ci-observe Prometheus and Loki on the host's
    loopback. Both are loopback-only, which is why the container keeps host
    networking and pins its own API back to loopback."""
    quadlet = (MONITORING / "quadlet" / "workflow-exporter.container").read_text()

    assert "Network=host" in quadlet
    assert "Environment=BIND_ADDRESS=127.0.0.1" in quadlet
    assert "Environment=CI_OBSERVE_METRICS_URL=http://127.0.0.1:9092" in quadlet
    assert "Environment=CI_OBSERVE_LOKI_URL=http://127.0.0.1:3100" in quadlet
    assert "Environment=CAPTURE_POLL_INTERVAL=60" in quadlet
    assert "Environment=JOBS_HISTORY_MAX_COUNT=400000" in quadlet
    assert "\nPublishPort=" not in quadlet, "host networking rejects published ports"


def test_the_registry_defaults_are_documented_constants(wfe):
    registry = wfe.CaptureRegistry
    assert registry.DEFAULT_METRICS_URL == "http://127.0.0.1:9092"
    assert registry.DEFAULT_LOKI_URL == "http://127.0.0.1:3100"
    assert registry.POLL_SECONDS == 60
    assert registry.CAPTURE_LABEL == "osac_ci_capture_id"
    assert registry.PROMETHEUS_RETENTION_DAYS == 30
    assert registry.LOKI_RETENTION_DAYS == 30


# ---------------------------------------------------------------------------
# Contract: the dashboard reads what the endpoint returns
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not DASHBOARD.exists(), reason="the entry page ships with the dashboard")
def test_the_dashboard_selectors_resolve_against_the_endpoint(exporter, jobs, fresh):
    dashboard = json.loads(DASHBOARD.read_text())
    panel = next(p for p in dashboard["panels"] if p["id"] == 12)
    target = panel["targets"][0]

    doc = exporter.get_ci_observe_runs_json({"days": ["7"]})
    node = doc[target["root_selector"].lstrip("$.")]

    assert node, "the contract document has no rows"
    for column in target["columns"]:
        assert column["selector"] in node[0], f"column {column['selector']} is not in the response"
