"""The ci-observe history path: one test or one job over a window.

Two stores feed it. A test reads trends.db, where ci_observe_trends.py writes
the daily rollup and the raw per-run rows behind it. A job reads the workflow
exporter's job history. These tests pin the honest parts: a window percentile
is computed from raw runs, a day rolled up before those rows existed says the
value was not measured, a job run without a duration is excluded instead of
counted as zero, retries and suites of one job aggregate, and every missing
store or unknown unit is a note in a 200, never an error.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from helpers import make_job

MONITORING = Path(__file__).resolve().parents[1]
SHA = "9f1c2ab37d54e6f0a1b2c3d4e5f60718293a4b5c"
OTHER_SHA = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"
TEST_ID = "tests/e2e/vmaas/test_vm.py::test_create"
JOB = "osac / E2E VMaaS Full Install"


def recent_iso(days_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def aggregate_row(day, test_id=TEST_ID, runs=4, passes=3, failures=1,
                  p50=10.0, p95=20.0, p99=21.0, mean=12.0):
    return {
        "day": day, "repo": "osac", "workflow": "E2E VMaaS Full Install",
        "suite": "tests/e2e/vmaas", "test_id": test_id, "source_sha": SHA,
        "runs": runs, "passes": passes, "failures": failures,
        "p50_duration_s": p50, "p95_duration_s": p95,
        "p99_duration_s": p99, "mean_duration_s": mean,
    }


def raw_row(day, test_id=TEST_ID, run_id=100, duration=10.0, passed=1,
            created_at=None, sha=SHA):
    return {
        "day": day, "repo": "osac", "workflow": "E2E VMaaS Full Install",
        "suite": "tests/e2e/vmaas", "test_id": test_id, "source_sha": sha,
        "run_id": run_id, "created_at": created_at or recent_iso(),
        "duration_s": duration, "passed": passed,
    }


def coverage(day, listed=4, parsed=4, unavailable=0):
    return {
        "day": day, "runs_listed": listed, "runs_parsed": parsed,
        "runs_unavailable": unavailable, "generated_at": recent_iso(),
    }


@pytest.fixture
def history_db(trends, exporter, tmp_path):
    """A trends.db in the exporter fixture's CACHE_DIR with one day of
    aggregates and the raw rows behind them."""
    path = str(tmp_path / "trends.db")
    day = today()
    conn = trends.open_trends_db(path)
    try:
        trends.write_day(
            conn, day, [aggregate_row(day)], coverage(day),
            [
                raw_row(day, run_id=100, duration=1.0, passed=1),
                raw_row(day, run_id=101, duration=2.0, passed=1),
                raw_row(day, run_id=102, duration=3.0, passed=0, sha=OTHER_SHA),
                raw_row(day, run_id=103, duration=4.0, passed=1),
            ],
        )
    finally:
        conn.close()
    return path


class TestRollupPercentiles:
    def run(self, cases):
        return {
            "run_id": 100, "day": "2026-11-01", "repo": "osac",
            "workflow": "E2E VMaaS Full Install", "source_sha": SHA,
            "created_at": "2026-11-01T10:00:00Z", "cases": cases,
        }

    def test_p99_and_mean_come_from_the_days_raw_durations(self, trends):
        rows = trends.rollup_rows([self.run([
            {"suite": "s", "test_id": "c::t", "passed": True, "duration_s": 1.0},
            {"suite": "s", "test_id": "c::t", "passed": True, "duration_s": 2.0},
            {"suite": "s", "test_id": "c::t", "passed": False, "duration_s": 3.0},
            {"suite": "s", "test_id": "c::t", "passed": True, "duration_s": 4.0},
        ])])

        assert rows[0]["p50_duration_s"] == 2.0
        assert rows[0]["p95_duration_s"] == 4.0
        assert rows[0]["p99_duration_s"] == 4.0
        assert rows[0]["mean_duration_s"] == 2.5

    def test_a_missing_duration_leaves_p99_and_mean_empty_not_zero(self, trends):
        rows = trends.rollup_rows([self.run([
            {"suite": "s", "test_id": "c::t", "passed": True, "duration_s": None},
        ])])

        assert rows[0]["p99_duration_s"] is None
        assert rows[0]["mean_duration_s"] is None

    def test_one_raw_row_per_test_per_run(self, trends):
        rows = trends.test_run_rows([self.run([
            {"suite": "s", "test_id": "c::a", "passed": True, "duration_s": 1.0},
            {"suite": "s", "test_id": "c::b", "passed": False, "duration_s": 2.0},
        ])])

        assert [(row["test_id"], row["run_id"], row["passed"]) for row in rows] == [
            ("c::a", 100, 1), ("c::b", 100, 0),
        ]
        assert rows[0]["created_at"] == "2026-11-01T10:00:00Z"


class TestMigration:
    def test_an_old_rollup_gains_the_columns_and_reads_null(self, trends, tmp_path):
        path = str(tmp_path / "trends.db")
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE test_rollup (
                day TEXT NOT NULL, repo TEXT NOT NULL, workflow TEXT NOT NULL,
                suite TEXT NOT NULL, test_id TEXT NOT NULL, source_sha TEXT NOT NULL,
                runs INTEGER NOT NULL, passes INTEGER NOT NULL, failures INTEGER NOT NULL,
                p50_duration_s REAL, p95_duration_s REAL,
                PRIMARY KEY (day, repo, workflow, suite, test_id, source_sha)
            );
            CREATE TABLE rollup_days (
                day TEXT PRIMARY KEY, runs_listed INTEGER NOT NULL,
                runs_parsed INTEGER NOT NULL, runs_unavailable INTEGER NOT NULL,
                generated_at TEXT NOT NULL
            );
            INSERT INTO test_rollup VALUES
                ('2026-11-01', 'osac', 'w', 's', 'c::t', 'sha', 1, 1, 0, 1.0, 1.0);
        """)
        conn.commit()
        conn.close()

        conn = trends.open_trends_db(path)
        try:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(test_rollup)")}
            row = dict(conn.execute("SELECT * FROM test_rollup").fetchone())
            tables = {
                row["name"] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            conn.close()

        assert {"p99_duration_s", "mean_duration_s"} <= columns
        assert row["p50_duration_s"] == 1.0, "an existing value survives the migration"
        assert row["p99_duration_s"] is None, "a value never measured is NULL, not 0"
        assert row["mean_duration_s"] is None
        assert "test_runs" in tables


class TestTestHistory:
    def get(self, exporter, **params):
        return exporter.get_ci_observe_history_json(
            {key: [value] for key, value in params.items()}
        )

    def test_the_summary_uses_true_window_percentiles(self, exporter, history_db):
        doc = self.get(exporter, kind="test", name=TEST_ID, days="30")

        assert doc["kind"] == "test"
        assert doc["name"] == TEST_ID
        summary = doc["summary"][0]
        assert (summary["runs"], summary["passes"], summary["failures"]) == (4, 3, 1)
        assert summary["pass_rate"] == 0.75
        assert summary["p50_duration_s"] == 2.0
        assert summary["p95_duration_s"] == 4.0
        assert summary["p99_duration_s"] == 4.0
        assert summary["mean_duration_s"] == 2.5
        assert summary["measured_runs"] == 4
        assert "true percentiles" in doc["duration_basis"]

    def test_the_daily_series_comes_from_the_rollup(self, exporter, history_db):
        doc = self.get(exporter, kind="test", name=TEST_ID)

        assert len(doc["daily"]) == 1
        day = doc["daily"][0]
        assert (day["day"], day["runs"], day["passes"], day["failures"]) == (today(), 4, 3, 1)
        assert day["p99_duration_s"] == 21.0

    def test_the_recent_table_has_when_result_duration_and_run_link(self, exporter, history_db):
        doc = self.get(exporter, kind="test", name=TEST_ID)

        assert len(doc["recent"]) == 4
        newest = doc["recent"][0]
        assert newest["run_id"] == "103"
        assert newest["conclusion"] == "success"
        assert newest["duration_s"] == 4.0
        assert newest["commit"] in (SHA, OTHER_SHA)
        assert newest["run_url"].endswith("/actions/runs/103")

    def test_a_legacy_window_says_the_percentile_was_not_measured(self, exporter, trends, tmp_path):
        path = str(tmp_path / "trends.db")
        conn = trends.open_trends_db(path)
        try:
            trends.write_day(conn, today(), [aggregate_row(
                today(), p50=10.0, p95=20.0, p99=None, mean=12.0,
            )], coverage(today()))
        finally:
            conn.close()

        doc = self.get(exporter, kind="test", name=TEST_ID)

        summary = doc["summary"][0]
        assert (summary["runs"], summary["passes"], summary["failures"]) == (4, 3, 1)
        assert summary["p50_duration_s"] == 10.0, "median of the daily p50 values"
        assert summary["p95_duration_s"] == 20.0, "median of the daily p95 values"
        assert summary["p99_duration_s"] is None, "not measured, never a median of daily p99s"
        assert summary["mean_duration_s"] is None, "not measured, never a weighted daily mean"
        assert summary["measured_runs"] == 0
        assert "median of the daily values" in doc["duration_basis"]
        assert "not measured" in doc["duration_basis"]
        assert doc["recent"] == []
        assert any("no raw per-run durations" in note for note in doc["notes"])

    def test_a_partial_raw_window_reports_how_many_runs_it_measured(self, exporter, trends, tmp_path):
        """Some runs have a raw row and some do not. The percentiles are true
        percentiles of the measured runs, and the response says 2 of 4."""
        path = str(tmp_path / "trends.db")
        day = today()
        conn = trends.open_trends_db(path)
        try:
            trends.write_day(
                conn, day, [aggregate_row(day)], coverage(day),
                [
                    raw_row(day, run_id=100, duration=1.0, passed=1),
                    raw_row(day, run_id=101, duration=2.0, passed=1),
                ],
            )
        finally:
            conn.close()

        doc = self.get(exporter, kind="test", name=TEST_ID)

        summary = doc["summary"][0]
        assert summary["runs"] == 4
        assert summary["measured_runs"] == 2
        assert summary["p50_duration_s"] == 1.0
        assert summary["p99_duration_s"] == 2.0
        assert summary["mean_duration_s"] == 1.5
        assert "2 of 4 run(s)" in doc["duration_basis"]

    def test_a_database_without_the_new_columns_still_answers(self, exporter, tmp_path):
        path = str(tmp_path / "trends.db")
        conn = sqlite3.connect(path)
        conn.executescript(f"""
            CREATE TABLE test_rollup (
                day TEXT NOT NULL, repo TEXT NOT NULL, workflow TEXT NOT NULL,
                suite TEXT NOT NULL, test_id TEXT NOT NULL, source_sha TEXT NOT NULL,
                runs INTEGER NOT NULL, passes INTEGER NOT NULL, failures INTEGER NOT NULL,
                p50_duration_s REAL, p95_duration_s REAL,
                PRIMARY KEY (day, repo, workflow, suite, test_id, source_sha)
            );
            INSERT INTO test_rollup VALUES
                ('{today()}', 'osac', 'w', 's', '{TEST_ID}', 'sha', 2, 1, 1, 8.0, 9.0);
        """)
        conn.commit()
        conn.close()

        doc = self.get(exporter, kind="test", name=TEST_ID)

        assert doc["summary"][0]["runs"] == 2
        assert doc["summary"][0]["p99_duration_s"] is None
        assert doc["summary"][0]["mean_duration_s"] is None

    def test_a_substring_with_several_matches_returns_a_note_and_a_list(self, exporter, trends, tmp_path):
        path = str(tmp_path / "trends.db")
        conn = trends.open_trends_db(path)
        try:
            trends.write_day(conn, today(), [
                aggregate_row(today(), test_id="tests/e2e/vmaas/test_vm.py::test_create"),
                aggregate_row(today(), test_id="tests/e2e/vmaas/test_vm.py::test_delete"),
            ], coverage(today()))
        finally:
            conn.close()

        doc = self.get(exporter, kind="test", name="test_vm.py")

        assert doc["summary"] == []
        assert [row["test_id"] for row in doc["matches"]] == [
            "tests/e2e/vmaas/test_vm.py::test_create",
            "tests/e2e/vmaas/test_vm.py::test_delete",
        ]
        assert all(row["history_url"].startswith("/d/ci-observe-test/") for row in doc["matches"])
        assert any("2 tests match" in note for note in doc["notes"])

    def test_a_single_substring_match_is_used_and_named(self, exporter, history_db):
        doc = self.get(exporter, kind="test", name="test_vm.py::test_cre")

        assert doc["name"] == TEST_ID
        assert any("not an exact test id" in note for note in doc["notes"])

    def test_a_missing_rollup_is_a_note_not_an_error(self, exporter):
        doc = self.get(exporter, kind="test", name=TEST_ID)

        assert doc["summary"] == []
        assert any("no per-test rollup" in note for note in doc["notes"])

    def test_an_empty_name_asks_for_one(self, exporter, history_db):
        doc = self.get(exporter, kind="test", name="")

        assert doc["summary"] == []
        assert any("type a test name" in note for note in doc["notes"])

    def test_a_window_with_no_rows_is_a_note(self, exporter, history_db):
        doc = self.get(exporter, kind="test", name="tests/e2e/vmaas/test_vm.py::other")

        assert doc["summary"] == []
        assert any("no test whose id matches" in note for note in doc["notes"])

    def test_an_unknown_kind_falls_back_to_test_with_a_note(self, exporter, history_db):
        doc = self.get(exporter, kind="flavour", name=TEST_ID)

        assert doc["kind"] == "test"
        assert any("neither test nor job" in note for note in doc["notes"])

    def test_the_population_names_the_unit_and_the_window(self, exporter, history_db):
        doc = self.get(exporter, kind="test", name=TEST_ID, days="7")

        assert doc["days"] == 7
        assert doc["window_from"] in doc["population"]
        assert "all jobs, not captured runs" in doc["population"]
        assert "trends.db" in doc["source"]


class TestJobHistory:
    @staticmethod
    def store(exporter, **overrides):
        row = make_job(
            display_name=JOB,
            repo="osac",
            workflow="E2E VMaaS Full Install",
            created_at=recent_iso(),
            duration_s=10,
            conclusion="success",
        )
        row.update(overrides)
        exporter._upsert_job(row)

    def get(self, exporter, **params):
        return exporter.get_ci_observe_history_json(
            {key: [value] for key, value in params.items()}
        )

    def test_a_cancelled_run_counts_in_runs_and_the_pass_rate_denominator(self, exporter):
        """Pass rate is passes divided by all stored rows, so a cancelled run
        lowers it even though it is neither a pass nor a failure."""
        self.store(exporter, id=1, duration_s=10, conclusion="success")
        self.store(exporter, id=2, duration_s=10, conclusion="cancelled")

        doc = self.get(exporter, kind="job", name=JOB)

        summary = doc["summary"][0]
        assert (summary["runs"], summary["passes"], summary["failures"]) == (2, 1, 0)
        assert summary["other"] == 1
        assert summary["pass_rate"] == 0.5
        assert any("neither pass nor fail" in note for note in doc["notes"])

    def test_a_job_opens_its_suite_time_page_in_its_repository(self, history):
        """A job's history is its suite's Where the time goes page: the
        suite's phases and steps per day, in the job's repository."""
        url = history.history_url("job", "osac / E2E VMaaS", days=7, repo="osac")

        assert url.startswith("/d/ci-observe-time/")
        assert "var-suite=vmaas" in url
        assert "var-days=7" in url
        assert "var-repo=osac" in url

    def test_a_job_names_its_suite_by_its_workflow(self, history):
        url = history.history_url("job", "osac / anything", repo="osac", workflow="E2E suite · caas · #12 · abc1234")

        assert "var-suite=caas" in url
        assert "var-days=30" in url

    def test_a_job_outside_the_three_suites_opens_all_of_them(self, history):
        url = history.history_url("job", "osac / E2E Netris Full Install", days=30)

        assert "var-suite=all" in url
        assert "var-repo=all" in url

    def test_a_test_opens_its_test_history_under_its_canonical_key(self, history):
        url = history.history_url("test", "tests/e2e/vmaas/sanity/test_vm.py::TestVm::test_create", days=14)

        assert url.startswith("/d/ci-observe-test/")
        assert "var-test=tests.e2e.vmaas.sanity.test_vm::TestVm::test_create&" in url
        assert "var-suite=vmaas" in url
        assert "var-days=14" in url

    def test_a_window_the_pages_do_not_offer_widens_to_one_they_do(self, history):
        assert "var-days=30" in history.history_url("test", TEST_ID, days=20)
        assert "var-days=90" in history.history_url("test", TEST_ID, days=365)

    def test_the_summary_uses_true_percentiles_from_the_raw_jobs(self, exporter):
        for index, (duration, conclusion) in enumerate(
            [(10, "success"), (20, "failure"), (30, "success"), (40, "success")], start=1
        ):
            self.store(exporter, id=index, duration_s=duration, conclusion=conclusion,
                       created_at=recent_iso())

        doc = self.get(exporter, kind="job", name=JOB)

        summary = doc["summary"][0]
        assert (summary["runs"], summary["passes"], summary["failures"]) == (4, 3, 1)
        assert summary["pass_rate"] == 0.75
        assert summary["p50_duration_s"] == 20
        assert summary["p95_duration_s"] == 40
        assert summary["p99_duration_s"] == 40
        assert summary["mean_duration_s"] == 25
        assert summary["measured_runs"] == 4

    def test_a_run_without_a_duration_is_excluded_not_counted_as_zero(self, exporter):
        self.store(exporter, id=1, duration_s=10, conclusion="success")
        self.store(exporter, id=2, duration_s=0, conclusion="failure")
        self.store(exporter, id=3, duration_s=None, conclusion="success")

        doc = self.get(exporter, kind="job", name=JOB)

        summary = doc["summary"][0]
        assert summary["runs"] == 3
        assert summary["measured_runs"] == 1, "zero and NULL durations are not measurements"
        assert summary["p50_duration_s"] == 10
        assert summary["mean_duration_s"] == 10
        assert any("no stored duration" in note for note in doc["notes"])

    def test_retries_and_suites_of_one_job_aggregate(self, exporter):
        # Two repository rows with the same display name, one of them a
        # pull-request-flow suite row: the unit is the name, not the row id.
        self.store(exporter, id=1, duration_s=10, conclusion="success")
        self.store(exporter, id=2, duration_s=20, conclusion="failure",
                   url="https://github.com/osac-project/osac/actions/runs/2/job/2",
                   run_id="2")

        doc = self.get(exporter, kind="job", name=JOB)

        assert doc["summary"][0]["runs"] == 2
        assert doc["summary"][0]["p50_duration_s"] == 10
        assert doc["summary"][0]["p95_duration_s"] == 20

    def test_the_daily_series_and_recent_table_are_one_row_per_run(self, exporter):
        self.store(exporter, id=1, duration_s=10, conclusion="success",
                   created_at=recent_iso(1), head_sha=SHA,
                   url="https://github.com/osac-project/osac/actions/runs/1")
        self.store(exporter, id=2, duration_s=20, conclusion="failure",
                   created_at=recent_iso(), run_id="2",
                   url="https://github.com/osac-project/osac/actions/runs/2")

        doc = self.get(exporter, kind="job", name=JOB)

        assert len(doc["daily"]) == 2
        assert doc["daily"][-1]["failures"] == 1
        assert doc["recent"][0]["run_id"] == "2"
        assert doc["recent"][0]["conclusion"] == "failure"
        assert doc["recent"][0]["duration_s"] == 20
        assert doc["recent"][0]["run_url"].endswith("/actions/runs/2")
        assert doc["recent"][1]["commit"] == SHA

    def test_a_substring_with_several_matches_lists_them(self, exporter):
        self.store(exporter, id=1, display_name="osac / E2E VMaaS Full Install")
        self.store(exporter, id=2, display_name="osac / E2E VMaaS Snapshot",
                   workflow="E2E VMaaS Snapshot")

        doc = self.get(exporter, kind="job", name="E2E VMaaS")

        assert doc["summary"] == []
        assert {row["display_name"] for row in doc["matches"]} == {
            "osac / E2E VMaaS Full Install", "osac / E2E VMaaS Snapshot"
        }
        assert any("2 jobs match" in note for note in doc["notes"])

    def test_an_unknown_job_is_a_note_not_an_error(self, exporter):
        doc = self.get(exporter, kind="job", name="osac / No Such Job")

        assert doc["summary"] == []
        assert any("no job whose name matches" in note for note in doc["notes"])

    def test_a_missing_job_db_is_a_note(self, exporter, tmp_path, monkeypatch, wfe):
        monkeypatch.setattr(wfe, "DB_FILE", str(tmp_path / "absent.db"))

        doc = self.get(exporter, kind="job", name=JOB)

        assert doc["summary"] == []
        assert any("workflow exporter DB does not exist" in note for note in doc["notes"])

    def test_the_population_names_the_unit_and_the_window(self, exporter):
        self.store(exporter, id=1)

        doc = self.get(exporter, kind="job", name=JOB, days="7")

        assert doc["repo"] == "osac"
        assert doc["window_from"] in doc["population"]
        assert "all jobs, not captured runs" in doc["population"]
        assert "workflow-exporter.db" in doc["source"]


class TestJobsEndpoint:
    def get(self, exporter, **params):
        return exporter.get_ci_observe_jobs_json(
            {key: [value] for key, value in params.items()}
        )

    def test_it_lists_matching_job_names_with_a_history_link(self, exporter):
        for index in (1, 2):
            exporter._upsert_job(make_job(
                id=index, display_name="osac / E2E VMaaS Full Install",
                repo="osac", created_at=recent_iso(),
            ))
        exporter._upsert_job(make_job(
            id=3, display_name="osac / E2E CaaS Full Install",
            repo="osac", created_at=recent_iso(),
        ))

        doc = self.get(exporter, q="VMaaS")

        assert [row["display_name"] for row in doc["jobs"]] == ["osac / E2E VMaaS Full Install"]
        assert doc["jobs"][0]["runs"] == 2
        assert doc["jobs"][0]["history_url"].startswith("/d/ci-observe-time/")
        assert "var-suite=vmaas" in doc["jobs"][0]["history_url"]
        assert "var-repo=osac" in doc["jobs"][0]["history_url"]

    def test_the_cap_is_a_note(self, exporter):
        for index in range(1, 4):
            exporter._upsert_job(make_job(
                id=index, display_name=f"osac / E2E Job {index}", created_at=recent_iso(),
            ))

        doc = self.get(exporter, limit="2")

        assert len(doc["jobs"]) == 2
        assert doc["truncated"] is True
        assert any("job limit" in note for note in doc["notes"])

    def test_a_missing_db_is_a_note(self, exporter, tmp_path, monkeypatch, wfe):
        monkeypatch.setattr(wfe, "DB_FILE", str(tmp_path / "absent.db"))

        doc = self.get(exporter)

        assert doc["jobs"] == []
        assert any("does not exist" in note for note in doc["notes"])


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

    def test_test_history_is_served(self, base_url, history_db):
        status, headers, body = self.get(
            f"{base_url}/api/ci-observe/history?kind=test&name={urllib.parse.quote(TEST_ID)}&days=30"
        )

        assert status == 200
        assert headers.get("Content-Type") == "application/json"
        assert headers.get("Access-Control-Allow-Origin") == "*"
        doc = json.loads(body)
        assert doc["kind"] == "test"
        assert doc["summary"][0]["runs"] == 4

    def test_job_history_is_served(self, base_url, exporter):
        exporter._upsert_job(make_job(
            id=1, display_name=JOB, repo="osac", created_at=recent_iso(), duration_s=10,
        ))

        status, _, body = self.get(
            f"{base_url}/api/ci-observe/history?kind=job&name={urllib.parse.quote(JOB)}"
        )

        assert status == 200
        assert json.loads(body)["summary"][0]["runs"] == 1

    def test_an_unknown_unit_is_still_200_with_a_note(self, base_url, history_db):
        status, _, body = self.get(f"{base_url}/api/ci-observe/history?kind=job&name=nope")

        assert status == 200
        assert any("no job whose name matches" in note for note in json.loads(body)["notes"])

    def test_the_jobs_picker_is_served(self, base_url, exporter):
        exporter._upsert_job(make_job(
            id=1, display_name=JOB, repo="osac", created_at=recent_iso(),
        ))

        status, _, body = self.get(f"{base_url}/api/ci-observe/jobs?days=30&q=VMaaS")

        assert status == 200
        assert [row["display_name"] for row in json.loads(body)["jobs"]] == [JOB]


def test_the_deployment_copies_the_history_module():
    setup = (MONITORING / "scripts" / "monitoring-setup.sh").read_text()
    quadlet = (MONITORING / "quadlet" / "workflow-exporter.container").read_text()

    assert setup.count("ci_observe_history.py") >= 2
    assert "ci_observe_history.py:/app/ci_observe_history.py" in quadlet
