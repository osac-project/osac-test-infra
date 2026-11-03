"""The ci-observe Trends data path: the daily per-test JUnit rollup
(monitoring/scripts/ci_observe_trends.py) and the exporter's
/api/ci-observe/trends endpoint.

The rollup turns the JUnit XML inside a run's GitHub artifact -- the only
per-test record, deleted after 7 days -- into one row per day, repo,
workflow, suite, test id and source SHA. These tests pin the parser (which
testcase counts as a pass, a failure and neither), the run grouping that
survives a pull-request-flow run's several stored rows, day replacement
(rerunning a day corrects it, never doubles it), the artifact fetch, and
both halves of the endpoint/dashboard contract.
"""

from __future__ import annotations

import io
import json
import sqlite3
import threading
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from helpers import FakeResponse, make_job

MONITORING = Path(__file__).resolve().parents[1]
DASHBOARD = MONITORING / "config" / "grafana" / "dashboards" / "ci-observe-trends.json"
RUN_ID = 41310072671
SECOND_RUN_ID = 41310072672
LIST_PATH = f"/repos/osac-project/osac/actions/runs/{RUN_ID}/artifacts?per_page=100"
SECOND_LIST_PATH = f"/repos/osac-project/osac/actions/runs/{SECOND_RUN_ID}/artifacts?per_page=100"
DOWNLOAD_PATH = "/repos/osac-project/osac/actions/artifacts/42/zip"
SECOND_DOWNLOAD_PATH = "/repos/osac-project/osac/actions/artifacts/43/zip"
SHA = "9f1c2ab37d54e6f0a1b2c3d4e5f60718293a4b5c"
OTHER_SHA = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"
TOKEN = "secret-token-do-not-leak"

VALID_JUNIT = b"""<?xml version="1.0" encoding="utf-8"?>
<testsuites name="e2e" tests="4" failures="1" errors="1" skipped="1" time="12.5">
  <testsuite name="tests/e2e/networking/vmaas" tests="4" failures="1" errors="1" skipped="1" time="12.5">
    <testcase classname="tests/e2e/networking/vmaas/test_vm" name="test_pass" time="1.5"/>
    <testcase classname="tests/e2e/networking/vmaas/test_vm" name="test_fail" time="2.5">
      <failure type="AssertionError" message="assert 1 == 2">Traceback line 1</failure>
    </testcase>
    <testcase classname="tests/e2e/networking/vmaas/test_vm" name="test_error" time="0.5">
      <error type="TimeoutError" message="timed out after 30s"/>
    </testcase>
    <testcase classname="tests/e2e/networking/vmaas/test_vm" name="test_skip" time="0.01">
      <skipped type="pytest.skip" message="no cluster"/>
    </testcase>
  </testsuite>
</testsuites>
"""


def junit(*cases: str, suite: str = "tests/e2e/vmaas") -> bytes:
    """A JUnit document from raw <testcase> elements, for the parser tests."""
    return (
        f'<testsuites><testsuite name="{suite}">' + "".join(cases) + "</testsuite></testsuites>"
    ).encode()


def passing(name: str, time: str = "1.0", classname: str = "tests/e2e/vmaas") -> str:
    return f'<testcase classname="{classname}" name="{name}" time="{time}"/>'


def failing(name: str, time: str = "2.0", classname: str = "tests/e2e/vmaas") -> str:
    return (
        f'<testcase classname="{classname}" name="{name}" time="{time}">'
        f'<failure type="AssertionError" message="boom"/></testcase>'
    )


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def artifact_payload(api_url: str, *, run_id=RUN_ID, art_id=42, expired=False, size=1024) -> FakeResponse:
    artifact = {
        "id": art_id,
        "name": f"osac-logs-full-install-{run_id}",
        "size_in_bytes": size,
        "expired": expired,
        "expires_at": "2026-11-08T10:00:00Z",
        "archive_download_url": f"{api_url}/repos/osac-project/osac/actions/artifacts/{art_id}/zip",
    }
    return FakeResponse(payload={"total_count": 1, "artifacts": [artifact]})


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def recent_iso(days_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_row(run_id=RUN_ID, **overrides) -> dict:
    row = make_job(
        id=run_id,
        workflow="E2E VMaaS Full Install",
        display_name="osac / E2E VMaaS Full Install",
        category="e2e",
        conclusion="failure",
        status="failure",
        url=f"https://github.com/osac-project/osac/actions/runs/{run_id}",
        created_at=recent_iso(),
        head_sha=SHA,
    )
    row.update(overrides)
    return row


def suite_row(job_id, run_id=RUN_ID, **overrides) -> dict:
    row = make_job(
        id=job_id,
        workflow="E2E vmaas (pull requests)",
        display_name="osac / E2E vmaas (pull requests)",
        category="e2e",
        url=f"https://github.com/osac-project/osac/actions/runs/{run_id}/job/{job_id}",
        created_at=recent_iso(),
        head_sha=SHA,
    )
    row.update(overrides)
    return row


def pipeline_row(run_id=RUN_ID, **overrides) -> dict:
    """The pull-request flow's own row: the run, not a suite."""
    row = make_job(
        id=run_id,
        workflow="E2E for pull requests",
        display_name="osac / E2E for pull requests",
        category="automation",
        url=f"https://github.com/osac-project/osac/actions/runs/{run_id}",
        created_at=recent_iso(),
        head_sha=SHA,
    )
    row.update(overrides)
    return row


class TestParseJunitOutcomes:
    def test_every_outcome_is_read_once(self, trends):
        cases, reason = trends.parse_junit_outcomes([("junit.xml", VALID_JUNIT)])

        assert reason is None
        assert [(c["test_id"].rsplit("::", 1)[-1], c["passed"]) for c in cases] == [
            ("test_pass", True),
            ("test_fail", False),
            ("test_error", False),
        ]
        assert {c["suite"] for c in cases} == {"tests/e2e/networking/vmaas"}
        assert cases[0]["test_id"] == "tests/e2e/networking/vmaas/test_vm::test_pass"
        assert cases[0]["duration_s"] == 1.5

    def test_a_skipped_test_is_neither_pass_nor_failure(self, trends):
        cases, _ = trends.parse_junit_outcomes(
            [("junit.xml", junit(passing("a"), f'<testcase name="b" time="0.1"><skipped/></testcase>'))]
        )

        assert [c["test_id"] for c in cases] == ["tests/e2e/vmaas::a"]

    def test_control_characters_inside_a_message_do_not_lose_the_file(self, trends):
        document = junit(
            '<testcase classname="tests/e2e/vmaas" name="test_x" time="1.0">'
            '<failure message="boom \x1b[31mred\x1b[0m"/></testcase>'
        )
        cases, reason = trends.parse_junit_outcomes([("junit.xml", document)])

        assert reason is None
        assert cases == [
            {"suite": "tests/e2e/vmaas", "test_id": "tests/e2e/vmaas::test_x", "passed": False, "duration_s": 1.0}
        ]

    def test_unreadable_time_is_missing_not_zero(self, trends):
        cases, _ = trends.parse_junit_outcomes(
            [("junit.xml", junit(passing("a", time="1e400"), passing("b"))) ]
        )

        assert cases[0]["duration_s"] is None
        assert cases[1]["duration_s"] == 1.0

    def test_a_repeated_test_counts_once_and_the_failure_wins(self, trends):
        cases, _ = trends.parse_junit_outcomes([
            ("junit-a.xml", junit(passing("test_x"))),
            ("junit-b.xml", junit(failing("test_x"))),
        ])

        assert len(cases) == 1
        assert cases[0]["passed"] is False

    def test_a_broken_file_is_a_reason(self, trends):
        cases, reason = trends.parse_junit_outcomes([("junit.xml", b"<testsuites><testsuite>")])

        assert cases is None
        assert "not valid XML" in reason

    def test_a_file_with_no_testcase_parses_to_nothing(self, trends):
        cases, reason = trends.parse_junit_outcomes([("junit.xml", b'<testsuites><testsuite name="x"/></testsuites>')])

        assert cases == []
        assert reason is None


class TestPercentile:
    @pytest.mark.parametrize(
        ("values", "fraction", "expected"),
        [
            ([], 0.50, None),
            ([5.0], 0.50, 5.0),
            ([1.0, 2.0, 3.0, 4.0], 0.50, 2.0),
            ([1.0, 2.0, 3.0, 4.0], 0.95, 4.0),
            ([4.0, 1.0, 3.0, 2.0], 0.50, 2.0),
            ([1.0, 2.0], 0.95, 2.0),
        ],
    )
    def test_nearest_rank(self, trends, values, fraction, expected):
        assert trends.percentile(values, fraction) == expected


class TestRollupRows:
    def run(self, day="2026-11-01", sha=SHA, cases=None, run_id=RUN_ID, workflow="E2E VMaaS Full Install"):
        return {
            "run_id": run_id,
            "day": day,
            "repo": "osac",
            "workflow": workflow,
            "source_sha": sha,
            "cases": cases
            or [
                {"suite": "s", "test_id": "c::test_a", "passed": True, "duration_s": 2.0},
                {"suite": "s", "test_id": "c::test_b", "passed": False, "duration_s": 4.0},
            ],
        }

    def test_counts_runs_passes_failures_and_durations(self, trends):
        rows = trends.rollup_rows([self.run(run_id=RUN_ID), self.run(run_id=SECOND_RUN_ID, cases=[
            {"suite": "s", "test_id": "c::test_a", "passed": False, "duration_s": 6.0},
            {"suite": "s", "test_id": "c::test_b", "passed": True, "duration_s": 8.0},
        ])])

        by_test = {row["test_id"]: row for row in rows}
        assert by_test["c::test_a"]["runs"] == 2
        assert by_test["c::test_a"]["passes"] == 1
        assert by_test["c::test_a"]["failures"] == 1
        assert by_test["c::test_a"]["p50_duration_s"] == 2.0
        assert by_test["c::test_a"]["p95_duration_s"] == 6.0
        assert by_test["c::test_a"]["source_sha"] == SHA

    def test_a_second_sha_is_its_own_row(self, trends):
        rows = trends.rollup_rows([self.run(), self.run(sha=OTHER_SHA, run_id=SECOND_RUN_ID)])

        assert len(rows) == 4
        assert {row["source_sha"] for row in rows} == {SHA, OTHER_SHA}

    def test_a_missing_duration_leaves_the_percentile_empty_not_zero(self, trends):
        rows = trends.rollup_rows([self.run(cases=[
            {"suite": "s", "test_id": "c::test_a", "passed": True, "duration_s": None},
        ])])

        assert rows[0]["p50_duration_s"] is None
        assert rows[0]["p95_duration_s"] is None


class TestCandidateRuns:
    def test_suite_rows_of_one_flow_run_become_one_run(self, trends):
        runs = trends.candidate_runs([
            pipeline_row(),
            suite_row(101),
            suite_row(102, workflow="E2E caas (pull requests)"),
        ])

        assert len(runs) == 1
        assert runs[0]["run_id"] == RUN_ID
        assert runs[0]["workflow"] == "E2E for pull requests"
        assert runs[0]["source_sha"] == SHA
        assert runs[0]["repo"] == "osac"

    def test_a_direct_run_keeps_its_own_workflow(self, trends):
        runs = trends.candidate_runs([run_row()])

        assert [run["workflow"] for run in runs] == ["E2E VMaaS Full Install"]
        assert runs[0]["day"] == recent_iso()[:10]

    def test_rows_without_an_e2e_signal_or_run_id_are_skipped(self, trends):
        runs = trends.candidate_runs([
            make_job(id=7, category="automation", url="https://github.com/osac-project/osac/actions/runs/9"),
            make_job(id=8, category="e2e", url="https://github.com/osac-project/osac/actions/runs/9/job/8"),
        ])

        # The suite row (id 8, e2e) is the only e2e signal of run 9.
        assert [run["run_id"] for run in runs] == [9]
        # No run-level row was stored, so the label falls back to the suite.
        assert runs[0]["workflow"] == "E2E VMaaS"

    def test_the_window_comes_from_the_stored_rows(self, trends, exporter, wfe):
        exporter._upsert_job(run_row(created_at="2026-10-01T10:00:00Z"))
        exporter._upsert_job(run_row(SECOND_RUN_ID, created_at=recent_iso()))

        rows = trends.stored_rows(wfe.DB_FILE, recent_iso(days_ago=7)[:10])

        assert [row["id"] for row in rows] == [SECOND_RUN_ID]


class TestWriteDay:
    def test_rerunning_a_day_replaces_it_and_leaves_other_days(self, trends, tmp_path):
        conn = trends.open_trends_db(str(tmp_path / "trends.db"))
        try:
            trends.write_day(
                conn, "2026-11-01", [self.row("2026-11-01", failures=1)], self.coverage("2026-11-01", 3, 3, 0)
            )
            trends.write_day(
                conn, "2026-11-02", [self.row("2026-11-02", failures=2)], self.coverage("2026-11-02", 1, 1, 0)
            )
            # The same day again, with fewer runs and an unreadable artifact.
            trends.write_day(
                conn, "2026-11-01", [self.row("2026-11-01", failures=7)], self.coverage("2026-11-01", 2, 1, 1)
            )

            rows = conn.execute("SELECT day, failures FROM test_rollup ORDER BY day").fetchall()
            coverage = conn.execute("SELECT day, runs_listed, runs_parsed FROM rollup_days ORDER BY day").fetchall()
        finally:
            conn.close()

        assert [tuple(row) for row in rows] == [("2026-11-01", 7), ("2026-11-02", 2)]
        assert [tuple(row) for row in coverage] == [("2026-11-01", 2, 1), ("2026-11-02", 1, 1)]

    @staticmethod
    def row(day, failures):
        return {
            "day": day, "repo": "osac", "workflow": "w", "suite": "s", "test_id": "c::t", "source_sha": SHA,
            "runs": 2, "passes": 1, "failures": failures, "p50_duration_s": 1.0, "p95_duration_s": 2.0,
        }

    @staticmethod
    def coverage(day, listed, parsed, unavailable):
        return {
            "day": day, "runs_listed": listed, "runs_parsed": parsed,
            "runs_unavailable": unavailable, "generated_at": "2026-11-02T00:00:00Z",
        }


class TestArtifactFetch:
    def test_reads_the_run_artifact(self, trends, github, wfe):
        github.add(LIST_PATH, artifact_payload(wfe.API_URL))
        github.add(DOWNLOAD_PATH, FakeResponse(content=zip_bytes({"junit.xml": VALID_JUNIT})))

        cases, reason = trends.ArtifactFetcher(token=TOKEN).fetch("osac", RUN_ID)

        assert reason is None
        assert len(cases) == 3

    def test_an_expired_artifact_is_no_test_data_not_a_pass(self, trends, github, wfe):
        github.add(LIST_PATH, artifact_payload(wfe.API_URL, expired=True))

        cases, reason = trends.ArtifactFetcher(token=TOKEN).fetch("osac", RUN_ID)

        assert cases is None
        assert "expired" in reason
        assert github.count(DOWNLOAD_PATH) == 0

    def test_a_missing_artifact_is_a_reason(self, trends, github):
        github.add(LIST_PATH, FakeResponse(payload={"total_count": 0, "artifacts": []}))

        cases, reason = trends.ArtifactFetcher(token=TOKEN).fetch("osac", RUN_ID)

        assert cases is None
        assert "no osac-logs" in reason

    def test_the_token_never_reaches_the_outcomes(self, trends, github, wfe):
        github.add(LIST_PATH, artifact_payload(wfe.API_URL))
        github.add(DOWNLOAD_PATH, FakeResponse(content=zip_bytes({"junit.xml": VALID_JUNIT})))

        cases, _ = trends.ArtifactFetcher(token=TOKEN).fetch("osac", RUN_ID)

        assert TOKEN not in json.dumps(cases)


class TestMain:
    @pytest.fixture
    def wired(self, trends, exporter, wfe, github, monkeypatch, tmp_path):
        """The script pointed at the exporter fixture's DB and a rollup DB
        in tmp_path, with the token set and one run routed on GitHub."""
        monkeypatch.setattr(trends, "TOKEN", TOKEN)
        monkeypatch.setattr(trends, "EXPORTER_DB", wfe.DB_FILE)
        monkeypatch.setattr(trends, "TRENDS_DB", str(tmp_path / "trends.db"))
        github.add(LIST_PATH, artifact_payload(wfe.API_URL))
        github.add(DOWNLOAD_PATH, FakeResponse(content=zip_bytes({"junit.xml": VALID_JUNIT})))
        return trends

    def test_one_rollup_writes_the_day(self, wired, exporter):
        exporter._upsert_job(run_row())

        assert wired.main([]) == 0

        rows = self.rows(wired)
        assert [(row["test_id"].rsplit("::", 1)[-1], row["passes"], row["failures"]) for row in rows] == [
            ("test_error", 0, 1),
            ("test_fail", 0, 1),
            ("test_pass", 1, 0),
        ]
        coverage = self.coverage(wired)
        assert (coverage["runs_listed"], coverage["runs_parsed"]) == (1, 1)

    def test_rerunning_a_day_replaces_its_rows(self, wired, exporter, github, wfe):
        exporter._upsert_job(run_row())

        assert wired.main(["--days", "7"]) == 0
        first = self.rows(wired)
        # The second rollup sees a changed artifact; the day's rows must be
        # replaced, not appended.
        github.add(DOWNLOAD_PATH, FakeResponse(content=zip_bytes({"junit.xml": junit(passing("a"))})))
        assert wired.main(["--days", "7"]) == 0
        second = self.rows(wired)

        assert len(first) == 3
        assert len(second) == 1
        assert (second[0]["runs"], second[0]["passes"], second[0]["failures"]) == (1, 1, 0)

    def test_a_thinner_pass_keeps_the_stored_day(self, wired, exporter, github):
        """Artifacts expire after 7 days. A later pass for the same day reads
        fewer runs; replacing the day would delete the only copy."""
        exporter._upsert_job(run_row())
        assert wired.main(["--days", "7"]) == 0
        first = self.rows(wired)
        assert len(first) == 3

        github.routes[LIST_PATH] = [FakeResponse(payload={"total_count": 0, "artifacts": []})]
        assert wired.main(["--days", "7"]) == 0

        assert self.rows(wired) == first, "the stored day must survive a thinner pass"
        coverage = self.coverage(wired)
        assert coverage["runs_parsed"] == 1, "coverage still describes the stored rows"

    def test_a_run_without_an_artifact_is_counted_unavailable(self, wired, exporter, github):
        exporter._upsert_job(run_row())
        github.routes[LIST_PATH] = [FakeResponse(payload={"total_count": 0, "artifacts": []})]

        assert wired.main(["--days", "7"]) == 0

        coverage = self.coverage(wired)
        assert (coverage["runs_listed"], coverage["runs_parsed"], coverage["runs_unavailable"]) == (1, 0, 1)
        assert self.rows(wired) == []

    def test_no_token_is_an_error(self, wired, monkeypatch):
        monkeypatch.setattr(wired, "TOKEN", None)

        assert wired.main(["--days", "7"]) == 2

    def test_a_bad_day_is_an_error(self, wired):
        assert wired.main(["--day", "yesterday"]) == 2

    @staticmethod
    def rows(trends_module):
        conn = sqlite3.connect(trends_module.TRENDS_DB)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM test_rollup")]
        finally:
            conn.close()

    @staticmethod
    def coverage(trends_module):
        conn = sqlite3.connect(trends_module.TRENDS_DB)
        conn.row_factory = sqlite3.Row
        try:
            return dict(conn.execute("SELECT * FROM rollup_days").fetchone())
        finally:
            conn.close()


@pytest.fixture
def rollup(trends, exporter, tmp_path):
    """A trends.db in the exporter fixture's CACHE_DIR, with two tests on
    one day so a filter test means something."""
    path = str(tmp_path / "trends.db")
    day = today()
    conn = trends.open_trends_db(path)
    try:
        trends.write_day(conn, day, [
            {
                "day": day, "repo": "osac", "workflow": "E2E VMaaS Full Install",
                "suite": "tests/e2e/vmaas", "test_id": "tests/e2e/vmaas/test_vm::test_restart",
                "source_sha": SHA, "runs": 4, "passes": 3, "failures": 1,
                "p50_duration_s": 12.0, "p95_duration_s": 30.0,
            },
            {
                "day": day, "repo": "osac", "workflow": "E2E VMaaS Full Install",
                "suite": "tests/e2e/vmaas", "test_id": "tests/e2e/vmaas/test_vm::test_create",
                "source_sha": SHA, "runs": 4, "passes": 4, "failures": 0,
                "p50_duration_s": 5.0, "p95_duration_s": 9.0,
            },
        ], {
            "day": day, "runs_listed": 4, "runs_parsed": 3,
            "runs_unavailable": 1, "generated_at": "2026-11-02T00:00:00Z",
        })
    finally:
        conn.close()
    return path


def test_a_day_may_only_be_replaced_when_every_stored_run_is_still_readable(trends):
    """Counts are not enough. If A parsed and B did not, then A expires and B
    becomes readable, the count stays 1 while A's only copy would be lost."""
    assert trends.day_is_superset([], [1]) is True
    assert trends.day_is_superset([1], [1, 2]) is True
    assert trends.day_is_superset([1], [2]) is False
    assert trends.day_is_superset([1, 2], [1]) is False


class TestEndpoint:
    def test_the_row_cap_keeps_the_newest_days(self, exporter, rollup, trends, monkeypatch):
        """The cap is rows, not days. It must drop the oldest rows, or the
        chart silently ends days before today."""
        conn = trends.open_trends_db(rollup)
        try:
            for offset in (1, 2):
                day = (datetime.now(timezone.utc).date() - timedelta(days=offset)).isoformat()
                trends.write_day(
                    conn,
                    day,
                    [{
                        "day": day, "repo": "osac", "workflow": "E2E VMaaS Full Install",
                        "suite": "tests/e2e/vmaas", "test_id": f"tests/e2e/vmaas/test_vm::test_{offset}",
                        "source_sha": SHA, "runs": 1, "passes": 1, "failures": 0,
                        "p50_duration_s": 1.0, "p95_duration_s": 1.0,
                    }],
                    {"day": day, "runs_listed": 1, "runs_parsed": 1, "runs_unavailable": 0,
                     "generated_at": "2026-11-02T00:00:00Z"},
                )
        finally:
            conn.close()

        monkeypatch.setattr(exporter, "CIOBS_TRENDS_MAX_ROWS", 3)
        doc = exporter.get_ci_observe_trends_json({"days": ["30"]})

        days = [row["day"] for row in doc["rows"]]
        assert len(days) == 3
        assert days == sorted(days), "rows stay in chronological order for the chart"
        assert days[-1] == today(), "the newest day must survive the cap"

    def test_rows_and_coverage(self, exporter, rollup):
        doc = exporter.get_ci_observe_trends_json({"days": [str(100000)]})

        assert doc["days"] == exporter.CIOBS_TRENDS_MAX_DAYS
        assert len(doc["rows"]) == 2
        assert set(doc["rows"][0]) == {
            "day", "repo", "workflow", "suite", "test_id", "source_sha",
            "runs", "passes", "failures", "p50_duration_s", "p95_duration_s",
        }
        assert doc["coverage"][0]["runs_unavailable"] == 1

    def test_the_test_filter_is_a_substring(self, exporter, rollup):
        doc = exporter.get_ci_observe_trends_json({"days": ["30"], "test": ["restart"]})

        assert [row["test_id"].rsplit("::", 1)[-1] for row in doc["rows"]] == ["test_restart"]

    def test_defaults_and_garbage_days(self, exporter, rollup):
        assert exporter.get_ci_observe_trends_json({})["days"] == 60
        assert exporter.get_ci_observe_trends_json({"days": ["nonsense"]})["days"] == 60
        assert exporter.get_ci_observe_trends_json({"days": ["0"]})["days"] == 1

    def test_a_missing_rollup_is_a_note_not_an_error(self, exporter):
        doc = exporter.get_ci_observe_trends_json({"days": ["60"]})

        assert doc["rows"] == []
        assert any("no per-test rollup" in note for note in doc["notes"])

    def test_an_unreadable_rollup_is_a_note_not_an_error(self, exporter, tmp_path):
        (tmp_path / "trends.db").write_bytes(b"this is not a sqlite database")

        doc = exporter.get_ci_observe_trends_json({"days": ["60"]})

        assert doc["rows"] == []
        assert any("could not be" in note for note in doc["notes"])

    def test_the_rollup_file_is_never_written(self, exporter, rollup):
        before = Path(rollup).read_bytes()

        exporter.get_ci_observe_trends_json({"days": ["60"]})

        assert Path(rollup).read_bytes() == before


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

    def test_json_shape_and_cors(self, rollup, base_url):
        status, headers, body = self.get(f"{base_url}/api/ci-observe/trends?days=30&test=restart")

        assert status == 200
        assert headers.get("Content-Type") == "application/json"
        assert headers.get("Access-Control-Allow-Origin") == "*"
        doc = json.loads(body)
        assert doc["test"] == "restart"
        assert len(doc["rows"]) == 1

    def test_empty_rollup_is_still_200(self, base_url):
        status, _, body = self.get(f"{base_url}/api/ci-observe/trends")

        assert status == 200
        assert json.loads(body)["rows"] == []


@pytest.mark.skipif(not DASHBOARD.exists(), reason="ci-observe-trends.json ships with the dashboard")
def test_dashboard_queries_resolve_against_the_endpoint(exporter, trends, tmp_path):
    """Every root_selector and column the Trends dashboard reads from
    /api/ci-observe/trends must exist in the real response, so the two
    cannot drift apart."""
    conn = trends.open_trends_db(str(tmp_path / "trends.db"))
    try:
        trends.write_day(conn, today(), [{
            "day": today(), "repo": "osac", "workflow": "E2E VMaaS Full Install",
            "suite": "tests/e2e/vmaas", "test_id": "tests/e2e/vmaas/test_vm::test_restart",
            "source_sha": SHA, "runs": 2, "passes": 1, "failures": 1,
            "p50_duration_s": 12.0, "p95_duration_s": 30.0,
        }], {
            "day": today(), "runs_listed": 2, "runs_parsed": 2,
            "runs_unavailable": 0, "generated_at": recent_iso(),
        })
    finally:
        conn.close()

    doc = exporter.get_ci_observe_trends_json({"days": ["60"]})
    dashboard = json.loads(DASHBOARD.read_text())

    checked = 0
    for panel in dashboard["panels"]:
        if panel["type"] in ("row", "text"):
            continue
        for target in panel.get("targets") or []:
            if "/api/ci-observe/trends" not in target.get("url", ""):
                continue
            checked += 1
            selector = target["root_selector"]
            node = doc if selector in ("$", "") else doc[selector.split(".", 1)[-1]]
            rows = node if isinstance(node, list) else [node]
            assert rows, f"{panel['title']}: {selector} is empty"
            for column in target["columns"]:
                assert column["selector"] in rows[0], (
                    f"{panel['title']}: column {column['selector']} is not in the response"
                )
    assert checked >= 4, "the dashboard must query the rollup endpoint"


def test_deployment_copies_the_rollup_script():
    """The rollup runs from the deployed scripts directory and imports its
    sibling module, so monitoring-setup.sh must copy both."""
    setup = (MONITORING / "scripts" / "monitoring-setup.sh").read_text()

    assert "scripts/ci_observe_trends.py" in setup
    assert "ci_observe_run_evidence.py" in setup


def test_rollup_is_mounted_in_the_exporter_and_scheduled_daily():
    """The rollup runs inside the exporter container: that is where the token,
    requests and the /data volume live. A timer runs it daily while the 7-day
    artifact window still has the previous day."""
    quadlet = (MONITORING / "quadlet" / "workflow-exporter.container").read_text()
    assert "ci_observe_trends.py:/app/ci_observe_trends.py" in quadlet

    service = (MONITORING / "systemd" / "ci-observe-trends.service").read_text()
    assert "podman exec workflow-exporter python3 /app/ci_observe_trends.py --days 7" in service

    timer = (MONITORING / "systemd" / "ci-observe-trends.timer").read_text()
    assert "OnCalendar=*-*-* 04:15:00" in timer
    assert "Persistent=true" in timer, "a missed day is a permanent hole in per-test history"

    setup = (MONITORING / "scripts" / "monitoring-setup.sh").read_text()
    assert "systemd/ci-observe-trends.service" in setup
    assert "systemd/ci-observe-trends.timer" in setup
    assert "systemctl --user enable --now ci-observe-trends.timer" in setup
    assert "systemctl --user enable ci-observe-trends.timer" in setup
