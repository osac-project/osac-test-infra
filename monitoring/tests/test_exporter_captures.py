"""The capture registry and the jobs.run_id migration in workflow-exporter.py.

The registry is the union of three answers (Prometheus label values, Loki
label values, the osac_ci_capture_info metric). These tests pin the poller's
promises: an upsert never empties a known field, coverage_from is written once
and never moves, last_ok_at only advances on a complete answer, junk ids are
counted and named, and a capture is not deleted before its 90-day prune.

They also pin the job-history cap: the old 100k rows cut a 60-day page to
about 20 days, so the default cap and the prune must keep a 60-day window.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from helpers import FakeResponse, make_job


def iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def days_ago(days):
    return datetime.now(timezone.utc) - timedelta(days=days)


class FakeSources:
    """The three registry sources, answering the shapes the live ones do."""

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
            return FakeResponse(404, text="unknown url")
        if source in self.failures:
            return FakeResponse(self.failures[source], text=f"{source} is down")
        if source == "info":
            data = {
                "resultType": "vector",
                "result": [{"metric": labels, "value": [0, "1"]} for labels in self.info],
            }
        else:
            data = list(getattr(self, source))
        return FakeResponse(200, payload={"status": "success", "data": data})


@pytest.fixture
def sources(exporter, wfe, monkeypatch):
    fake = FakeSources()
    monkeypatch.setattr(wfe.requests, "get", fake.get)
    return fake


@pytest.fixture
def registry(exporter, sources):
    return exporter._captures


def rows(exporter, sql, *args):
    with exporter._db() as conn:
        return [dict(row) for row in conn.execute(sql, args)]


def capture_row(exporter, capture_id):
    found = rows(exporter, "SELECT * FROM captures WHERE capture_id = ?", capture_id)
    return found[0] if found else None


def source_row(exporter, source):
    return rows(exporter, "SELECT * FROM capture_source WHERE source = ?", source)[0]


CAPTURE = "vmaas-main-41709318840-a1"
IDENTITY = {
    "osac_ci_capture_id": CAPTURE,
    "ci_run_id": "41709318840",
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
}


# ---------------------------------------------------------------------------
# Schema and jobs.run_id
# ---------------------------------------------------------------------------
def test_the_schema_has_the_registry_and_the_run_index(exporter):
    job_cols = {row["name"] for row in rows(exporter, "PRAGMA table_info(jobs)")}
    assert "run_id" in job_cols
    indexes = {row["name"] for row in rows(exporter, "PRAGMA index_list(jobs)")}
    assert "idx_jobs_run" in indexes
    capture_cols = {row["name"] for row in rows(exporter, "PRAGMA table_info(captures)")}
    assert {
        "capture_id", "run_id", "run_attempt", "suite", "pr", "workflow",
        "test_ref", "test_sha", "test_infra_ref", "test_infra_sha",
        "test_infra_workflow_sha", "test_suite", "test_filter", "test_marker",
        "session_trace_id", "identity_source", "capture_started_at",
        "first_seen_at", "last_seen_at",
    } <= capture_cols
    source_cols = {row["name"] for row in rows(exporter, "PRAGMA table_info(capture_source)")}
    assert source_cols == {
        "source", "coverage_from", "last_ok_at", "last_ok_count",
        "last_error", "last_error_at",
    }


def test_run_id_is_stored_on_a_run_row_and_a_suite_row(exporter):
    exporter._upsert_job(make_job(id=700, url="https://x/actions/runs/700"))
    exporter._upsert_job(make_job(
        id=701, url="https://x/actions/runs/700/job/701", run_id=700,
    ))

    got = {row["id"]: row["run_id"] for row in rows(exporter, "SELECT id, run_id FROM jobs")}
    assert got == {700: "700", 701: "700"}


def test_the_job_history_cap_does_not_bind_before_sixty_days(exporter, wfe):
    """~5k jobs/day is ~300k rows in 60 days. The old 100k cap silently cut
    the page to about 20 days."""
    assert wfe.JOBS_HISTORY_MAX_COUNT >= 300_000
    for day in range(60):
        exporter._upsert_job(make_job(
            id=1000 + day, created_at=iso(days_ago(day)),
        ))
    exporter._prune_jobs()

    assert len(rows(exporter, "SELECT id FROM jobs")) == 60


# ---------------------------------------------------------------------------
# Discovery and identity
# ---------------------------------------------------------------------------
def test_refresh_unions_the_three_sources(registry, sources):
    sources.prometheus = [CAPTURE, "caas-main-77-a1"]
    sources.loki = ["caas-main-77-a1", "vmaas-pr7-78-a1"]
    sources.info = [IDENTITY]

    summary = registry.refresh()

    assert {row["capture_id"] for row in rows(registry.exporter, "SELECT capture_id FROM captures")} == {
        CAPTURE, "caas-main-77-a1", "vmaas-pr7-78-a1",
    }
    assert summary["captures"] == 3
    assert summary["rejected"] == {"count": 0, "ids": []}
    assert [row["last_ok_count"] for row in summary["sources"]] == [2, 2, 1]
    assert summary["state"] == "ok"
    assert summary["last_ok_at"]
    assert summary["coverage_from"]


def test_an_id_only_answer_never_empties_a_known_identity(registry, sources):
    """A later poll whose sources carry only the label must not erase the
    identity the info metric already wrote."""
    sources.prometheus = [CAPTURE]
    sources.info = [IDENTITY]
    registry.refresh(days_ago(1))
    before = capture_row(registry.exporter, CAPTURE)

    sources.info = []
    registry.refresh()
    after = capture_row(registry.exporter, CAPTURE)

    assert after["identity_source"] == "info"
    for column in registry.IDENTITY_COLUMNS:
        assert after[column] == before[column]
    assert after["first_seen_at"] == before["first_seen_at"]
    assert after["last_seen_at"] > before["last_seen_at"]


def test_a_later_info_answer_fills_a_field_an_id_only_answer_left_empty(registry, sources):
    sources.prometheus = [CAPTURE]
    registry.refresh()
    assert capture_row(registry.exporter, CAPTURE)["identity_source"] == "none"

    sources.info = [IDENTITY]
    registry.refresh()

    row = capture_row(registry.exporter, CAPTURE)
    assert row["identity_source"] == "info"
    assert row["test_sha"] == "a" * 40


def test_a_poll_never_clobbers_the_session_trace_id_or_start_time(registry, sources, exporter):
    """The capture view writes session_trace_id and capture_started_at; the
    registry must leave them alone on every later poll."""
    sources.prometheus = [CAPTURE]
    registry.refresh()
    with exporter._db() as conn:
        conn.execute(
            "UPDATE captures SET session_trace_id = 'trace-1', capture_started_at = ? "
            "WHERE capture_id = ?",
            ("2026-11-01T10:00:00Z", CAPTURE),
        )

    registry.refresh()

    row = capture_row(exporter, CAPTURE)
    assert row["session_trace_id"] == "trace-1"
    assert row["capture_started_at"] == "2026-11-01T10:00:00Z"


def test_parse_capture_id_formats(wfe):
    parse = wfe.CaptureRegistry.parse_capture_id
    assert parse(CAPTURE) == {"run_id": "41709318840", "run_attempt": "1", "suite": "vmaas"}
    assert parse("caas-pr103-41709318840-a2") == {
        "run_id": "41709318840", "run_attempt": "2", "suite": "caas",
    }
    assert parse("run-41709318840-attempt-1") == {
        "run_id": "41709318840", "run_attempt": "1", "suite": "",
    }
    assert parse("41709318840") is None
    assert parse("not-a-capture") is None
    assert parse("") is None


def test_junk_ids_are_counted_and_named_not_stored(registry, sources):
    sources.prometheus = ["not-a-capture", "41709318840"]
    sources.loki = ["also-junk"]

    summary = registry.refresh()

    assert summary["rejected"]["count"] == 3
    assert summary["rejected"]["ids"] == ["41709318840", "also-junk", "not-a-capture"]
    assert rows(registry.exporter, "SELECT capture_id FROM captures") == []


# ---------------------------------------------------------------------------
# Coverage and failures
# ---------------------------------------------------------------------------
def test_coverage_from_is_written_once_and_never_moves(registry, sources):
    first = days_ago(10)
    registry.refresh(first)
    row = source_row(registry.exporter, "prometheus")
    assert row["coverage_from"] == iso(first - timedelta(days=14))

    registry.refresh()
    later = source_row(registry.exporter, "prometheus")
    assert later["coverage_from"] == row["coverage_from"]
    assert later["last_ok_at"] > row["last_ok_at"]


def test_a_failed_poll_keeps_the_last_good_answer(registry, sources):
    registry.refresh(days_ago(2))
    before = source_row(registry.exporter, "loki")

    sources.failures = {"loki": 503}
    summary = registry.refresh()

    after = source_row(registry.exporter, "loki")
    assert after["last_ok_at"] == before["last_ok_at"]
    assert after["last_ok_count"] == before["last_ok_count"]
    assert after["coverage_from"] == before["coverage_from"]
    assert "HTTP 503" in after["last_error"]
    assert after["last_error_at"]
    assert summary["state"] == "partial"
    assert next(row for row in summary["sources"] if row["source"] == "loki")["fresh"] is False


def test_a_successful_poll_clears_the_error(registry, sources):
    sources.failures = {"prometheus": 500}
    registry.refresh()
    assert source_row(registry.exporter, "prometheus")["last_error"]

    sources.failures = {}
    registry.refresh()

    row = source_row(registry.exporter, "prometheus")
    assert row["last_error"] == ""
    assert row["last_ok_at"]


def test_refresh_never_raises_when_every_source_fails(registry, sources):
    sources.failures = {"prometheus": 500, "loki": 500, "info": 500}

    summary = registry.refresh()

    assert summary["state"] == "unknown"
    assert summary["last_ok_at"] == ""
    assert summary["coverage_from"] == ""
    for row in summary["sources"]:
        assert row["last_error"]
        assert row["last_ok_at"] == ""


def test_a_bad_document_is_a_source_failure_not_an_empty_capture_set(registry, sources, wfe, monkeypatch):
    registry.refresh(days_ago(1))
    before = source_row(registry.exporter, "prometheus")

    def broken(url, timeout=None, headers=None):
        return FakeResponse(200, payload={"status": "success", "data": {"not": "a list"}})

    monkeypatch.setattr(wfe.requests, "get", broken)
    registry.refresh()

    after = source_row(registry.exporter, "prometheus")
    assert after["last_ok_at"] == before["last_ok_at"]
    assert "data list" in after["last_error"]


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------
def test_a_capture_is_kept_until_ninety_days(registry, sources):
    sources.prometheus = [CAPTURE]
    registry.refresh(days_ago(89))
    sources.prometheus = ["vmaas-main-1-a1"]
    registry.refresh(days_ago(1))

    assert capture_row(registry.exporter, CAPTURE)
    assert capture_row(registry.exporter, "vmaas-main-1-a1")


def test_a_capture_older_than_ninety_days_is_pruned(registry, sources):
    sources.prometheus = [CAPTURE]
    registry.refresh(days_ago(91))
    sources.prometheus = ["vmaas-main-1-a1"]
    registry.refresh()

    assert capture_row(registry.exporter, CAPTURE) is None
    assert capture_row(registry.exporter, "vmaas-main-1-a1")


def test_capture_ids_by_run_is_windowed(registry, sources):
    sources.prometheus = [CAPTURE, "vmaas-main-2-a1"]
    registry.refresh(days_ago(20))

    assert registry.capture_ids_by_run(days=7) == {}
    assert set(registry.capture_ids_by_run(days=30)) == {"41709318840", "2"}
