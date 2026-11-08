"""The ci-observe Trends data path: the daily per-test JUnit rollup
(monitoring/scripts/ci_observe_trends.py) and the exporter's
/api/ci-observe/trends endpoint.

The rollup turns the JUnit XML inside a run's GitHub artifact -- the only
per-test record, deleted after 30 days -- into one row per day, repo,
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
            {"suite": "tests/e2e/vmaas", "test_id": "tests/e2e/vmaas::test_x", "passed": False, "duration_s": 1.0,
             "suite_key": "", "message": "boom red"}
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

    @pytest.mark.parametrize("reversed_rows", [False, True], ids=["run-row-first", "run-row-last"])
    def test_an_empty_sha_never_overwrites_the_run_rows_sha(self, trends, reversed_rows):
        """The exporter stores the run's own row and, for a flow run, a row per
        suite. A suite row written before head_sha existed keeps an empty one,
        and a suite row whose job payload carried no html_url stores the run's
        URL. Such a row used to overwrite the run row and blank the SHA of a
        whole day's rollup (source_sha ''), losing exactly the two revisions a
        comparison needs."""
        rows = [
            pipeline_row(),  # the run's own row: id == run id, head_sha set
            suite_row(101, url=f"https://github.com/osac-project/osac/actions/runs/{RUN_ID}", head_sha=""),
        ]
        if reversed_rows:
            rows.reverse()

        runs = trends.candidate_runs(rows)

        assert len(runs) == 1
        assert runs[0]["source_sha"] == SHA, "an empty head_sha must not win over the run row's"
        assert runs[0]["workflow"] == "E2E for pull requests", "the run row names the run, not a suite"

    def test_a_run_row_keeps_its_sha_when_only_a_suite_row_has_none(self, trends):
        runs = trends.candidate_runs([run_row(head_sha=""), suite_row(101, head_sha="")])

        assert runs[0]["source_sha"] == ""

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

    def test_an_hourly_pass_never_reads_a_run_twice(self, wired, exporter, github, wfe):
        """The pass runs every hour: a run already read keeps its stored rows,
        and its artifact is not downloaded again."""
        exporter._upsert_job(run_row())

        assert wired.main(["--days", "7"]) == 0
        first = self.rows(wired)
        assert github.count(LIST_PATH) == 1
        github.add(DOWNLOAD_PATH, FakeResponse(content=zip_bytes({"junit.xml": junit(passing("a"))})))
        assert wired.main(["--days", "7"]) == 0

        assert github.count(LIST_PATH) == 1, "the second pass reuses the stored rows"
        assert self.rows(wired) == first

    def test_force_rereads_a_day_and_replaces_its_rows(self, wired, exporter, github, wfe):
        exporter._upsert_job(run_row())

        assert wired.main(["--days", "7"]) == 0
        first = self.rows(wired)
        # The forced pass sees a changed artifact; the day's rows must be
        # replaced, not appended.
        github.add(DOWNLOAD_PATH, FakeResponse(content=zip_bytes({"junit.xml": junit(passing("a"))})))
        assert wired.main(["--day", today(), "--force"]) == 0
        second = self.rows(wired)

        assert len(first) == 3
        assert len(second) == 1
        assert (second[0]["runs"], second[0]["passes"], second[0]["failures"]) == (1, 1, 0)

    def test_a_thinner_pass_keeps_the_stored_day(self, wired, exporter, github):
        """Artifacts expire after 30 days. A later pass for the same day reads
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

    def test_force_replaces_a_day_the_guard_would_keep(self, wired, exporter, github):
        """--force is how a stored day is repaired without waiting a day: it
        re-parses the requested days and replaces their rows even when this
        pass reads fewer runs."""
        exporter._upsert_job(run_row())
        assert wired.main(["--days", "7"]) == 0
        assert len(self.rows(wired)) == 3

        github.routes[LIST_PATH] = [FakeResponse(payload={"total_count": 0, "artifacts": []})]
        assert wired.main(["--days", "7"]) == 0
        assert len(self.rows(wired)) == 3, "the default still keeps the stored day"

        assert wired.main(["--day", today(), "--force"]) == 0

        assert self.rows(wired) == []
        coverage = self.coverage(wired)
        assert (coverage["runs_listed"], coverage["runs_parsed"], coverage["runs_unavailable"]) == (1, 0, 1)

    def test_force_rewrites_a_stored_day_with_the_shas_the_run_rows_carry(self, wired, exporter):
        """The repair the two cohort days need: rows rolled up with
        source_sha '' come back keyed by the exporter's head_sha."""
        exporter._upsert_job(run_row())
        assert wired.main(["--days", "7"]) == 0
        conn = sqlite3.connect(wired.TRENDS_DB)
        try:
            conn.execute("UPDATE test_rollup SET source_sha = ''")
            conn.commit()
        finally:
            conn.close()
        assert {row["source_sha"] for row in self.rows(wired)} == {""}

        assert wired.main(["--day", today(), "--force"]) == 0

        assert {row["source_sha"] for row in self.rows(wired)} == {SHA}

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


def rollup_row(day, sha, test, runs, failures, p50):
    return {
        "day": day, "repo": "osac", "workflow": "E2E VMaaS Full Install",
        "suite": "tests/e2e/vmaas", "test_id": f"tests/e2e/vmaas/test_vm::{test}",
        "source_sha": sha, "runs": runs, "passes": runs - failures, "failures": failures,
        "p50_duration_s": p50, "p95_duration_s": p50,
    }


@pytest.fixture
def two_revisions(trends, exporter, tmp_path):
    """A trends.db in the exporter fixture's CACHE_DIR: two source SHAs, the
    base revision on two days and the head one on the newest day, plus a test
    that exists on one side only."""
    path = str(tmp_path / "trends.db")
    day = today()
    earlier = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    conn = trends.open_trends_db(path)
    try:
        trends.write_day(conn, earlier, [
            rollup_row(earlier, SHA, "test_restart", 2, 2, 20.0),
            rollup_row(earlier, SHA, "test_create", 2, 0, 5.0),
            rollup_row(earlier, SHA, "test_removed", 2, 1, 1.0),
        ], {"day": earlier, "runs_listed": 2, "runs_parsed": 2, "runs_unavailable": 0,
            "generated_at": recent_iso()})
        trends.write_day(conn, day, [
            rollup_row(day, SHA, "test_restart", 2, 0, 10.0),
            rollup_row(day, OTHER_SHA, "test_restart", 3, 0, 8.0),
            rollup_row(day, OTHER_SHA, "test_create", 3, 0, 6.0),
            rollup_row(day, OTHER_SHA, "test_added", 3, 0, 2.0),
        ], {"day": day, "runs_listed": 3, "runs_parsed": 3, "runs_unavailable": 0,
            "generated_at": recent_iso()})
    finally:
        conn.close()
    return path


class TestCompare:
    @staticmethod
    def by_test(rows):
        return {row["test_id"].rsplit("::", 1)[-1]: row for row in rows}

    def test_the_join_pairs_the_same_test_across_two_shas(self, trends, two_revisions):
        doc = trends.compare_document(two_revisions, SHA, OTHER_SHA)
        rows = self.by_test(doc["rows"])

        assert set(rows) == {"test_restart", "test_create"}
        restart = rows["test_restart"]
        assert (restart["base_runs"], restart["base_failures"]) == (4, 2), "days are summed"
        assert restart["base_p50_duration_s"] == 10.0, "median of the two days' p50 values"
        assert (restart["head_runs"], restart["head_failures"]) == (3, 0)
        assert restart["delta_failures"] == -2, "head fails less"
        assert restart["delta_p50_s"] == -2.0, "head is faster"
        assert restart["workflow"] == "E2E VMaaS Full Install"
        assert restart["suite"] == "tests/e2e/vmaas"

        create = rows["test_create"]
        assert create["delta_failures"] == 0
        assert create["delta_p50_s"] == 1.0, "head is slower"

    def test_a_test_on_one_side_only_is_listed_there(self, trends, two_revisions):
        doc = trends.compare_document(two_revisions, SHA, OTHER_SHA)

        assert [row["test_id"].rsplit("::", 1)[-1] for row in doc["only_base"]] == ["test_removed"]
        assert [row["test_id"].rsplit("::", 1)[-1] for row in doc["only_head"]] == ["test_added"]
        assert set(doc["only_base"][0]) == {
            "repo", "workflow", "suite", "test_id", "runs", "failures", "p50_duration_s",
        }
        assert doc["only_head"][0]["runs"] == 3
        assert doc["only_head"][0]["failures"] == 0

    def test_the_document_states_its_population_and_window(self, trends, two_revisions):
        doc = trends.compare_document(two_revisions, SHA, OTHER_SHA, days=7)

        assert (doc["base_sha"], doc["head_sha"], doc["days"]) == (SHA, OTHER_SHA, 7)
        assert doc["window_from"] in doc["population"]
        assert "all CI jobs" in doc["population"]
        assert "not a per-capture comparison" in doc["population"]
        assert doc["window_to"] == today()

    def test_a_missing_rollup_is_a_note_not_an_error(self, trends, tmp_path):
        doc = trends.compare_document(str(tmp_path / "absent.db"), SHA, OTHER_SHA)

        assert (doc["rows"], doc["only_base"], doc["only_head"]) == ([], [], [])
        assert any("no per-test rollup" in note for note in doc["notes"])

    def test_an_unreadable_rollup_is_a_note_not_an_error(self, trends, tmp_path):
        (tmp_path / "trends.db").write_bytes(b"this is not a sqlite database")

        doc = trends.compare_document(str(tmp_path / "trends.db"), SHA, OTHER_SHA)

        assert doc["rows"] == []
        assert any("could not be" in note for note in doc["notes"])

    def test_a_missing_sha_is_a_note_not_an_error(self, trends, two_revisions):
        doc = trends.compare_document(two_revisions, "", OTHER_SHA)

        assert doc["rows"] == []
        assert any("base and head are required" in note for note in doc["notes"])

    def test_an_unknown_sha_is_a_note_and_keeps_the_other_side(self, trends, two_revisions):
        unknown = "0123456789abcdef0123456789abcdef01234567"

        doc = trends.compare_document(two_revisions, SHA, unknown)

        assert doc["rows"] == []
        assert self.by_test(doc["only_base"]).keys() == {"test_restart", "test_create", "test_removed"}
        assert any("no rows for head SHA" in note for note in doc["notes"])

    def test_the_rollup_file_is_never_written(self, trends, two_revisions):
        before = Path(two_revisions).read_bytes()

        trends.compare_document(two_revisions, SHA, OTHER_SHA)

        assert Path(two_revisions).read_bytes() == before

    def test_a_capped_table_is_a_note_not_a_silent_truncation(self, trends, two_revisions, monkeypatch):
        monkeypatch.setattr(trends, "COMPARE_MAX_ROWS", 1)

        doc = trends.compare_document(two_revisions, SHA, OTHER_SHA)

        assert len(doc["rows"]) == 1
        assert any("capped at 1" in note for note in doc["notes"])


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
            "p99_duration_s", "mean_duration_s",
        }
        assert doc["coverage"][0]["runs_unavailable"] == 1

    def test_the_test_filter_is_a_substring(self, exporter, rollup):
        doc = exporter.get_ci_observe_trends_json({"days": ["30"], "test": ["restart"]})

        assert [row["test_id"].rsplit("::", 1)[-1] for row in doc["rows"]] == ["test_restart"]

    def test_defaults_and_garbage_days(self, exporter, rollup):
        assert exporter.get_ci_observe_trends_json({})["days"] == 30
        assert exporter.get_ci_observe_trends_json({"days": ["nonsense"]})["days"] == 30
        assert exporter.get_ci_observe_trends_json({"days": ["0"]})["days"] == 1

    def test_a_missing_rollup_is_a_note_not_an_error(self, exporter):
        doc = exporter.get_ci_observe_trends_json({"days": ["60"]})

        assert doc["rows"] == []
        assert any("no per-test rollup" in note for note in doc["notes"])

    def test_an_unmigrated_rollup_still_serves_the_old_charts(self, exporter, tmp_path):
        """The read path must not select a column the file does not have yet.
        A deploy can install the new code before the rollup migrates
        trends.db; until then the page keeps its p50 and p95 charts and the
        new columns read as not measured."""
        conn = sqlite3.connect(str(tmp_path / "trends.db"))
        conn.executescript(f"""
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
                ('{today()}', 'osac', 'E2E VMaaS Full Install', 'tests/e2e/vmaas',
                 'tests/e2e/vmaas/test_vm::test_restart', '{SHA}', 2, 1, 1, 12.0, 30.0);
            INSERT INTO rollup_days VALUES ('{today()}', 2, 2, 0, '2026-11-06T00:00:00Z');
        """)
        conn.commit()
        conn.close()

        doc = exporter.get_ci_observe_trends_json({"days": ["30"]})

        assert len(doc["rows"]) == 1
        assert doc["rows"][0]["p50_duration_s"] == 12.0
        assert doc["rows"][0]["p95_duration_s"] == 30.0
        assert doc["rows"][0]["p99_duration_s"] is None
        assert doc["rows"][0]["mean_duration_s"] is None
        assert doc["coverage"][0]["runs_parsed"] == 2
        assert not any("could not be read" in note for note in doc["notes"])

        summary = exporter.get_ci_observe_trends_json({"days": ["30"], "summary": ["test"]})
        assert summary["tests"][0]["test_id"] == "tests/e2e/vmaas/test_vm::test_restart"

    def test_an_unreadable_rollup_is_a_note_not_an_error(self, exporter, tmp_path):
        (tmp_path / "trends.db").write_bytes(b"this is not a sqlite database")

        doc = exporter.get_ci_observe_trends_json({"days": ["60"]})

        assert doc["rows"] == []
        assert any("could not be" in note for note in doc["notes"])

    def test_the_rollup_file_is_never_written(self, exporter, rollup):
        before = Path(rollup).read_bytes()

        exporter.get_ci_observe_trends_json({"days": ["60"]})

        assert Path(rollup).read_bytes() == before

    def test_the_test_summary_is_one_row_per_test(self, exporter, rollup):
        """The entry page's lookup table: one row per test id, suite and
        repository, with runs and failures summed over the window."""
        doc = exporter.get_ci_observe_trends_json({"days": ["30"], "summary": ["test"]})

        assert doc["summary"] == "test"
        assert doc["rows"] == [], "the summary replaces the raw rows, it does not add to them"
        assert [row["test_id"].rsplit("::", 1)[-1] for row in doc["tests"]] == [
            "test_restart", "test_create"
        ], "worst first: failures, then runs"
        assert set(doc["tests"][0]) == {
            "test_id", "suite", "repo", "runs", "failures", "history_url",
        }
        # A test opens its Test history page, under its canonical key.
        assert doc["tests"][0]["history_url"].startswith("/d/ci-observe-test/")
        assert "var-test=tests.e2e.vmaas.test_vm::test_restart&" in doc["tests"][0]["history_url"]
        assert doc["tests"][0]["runs"] == 4
        assert doc["tests"][0]["failures"] == 1
        assert doc["tests"][0]["suite"] == "tests/e2e/vmaas"

    def test_the_test_summary_sums_the_whole_window_before_any_cap(
        self, exporter, rollup, trends, monkeypatch
    ):
        """The response row cap keeps the newest days only. If the page summed
        the returned rows, a busy window would silently total a fraction of
        itself, so the sum runs in SQL over every row of the window."""
        conn = trends.open_trends_db(rollup)
        try:
            yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
            trends.write_day(conn, yesterday, [{
                "day": yesterday, "repo": "osac", "workflow": "E2E VMaaS Full Install",
                "suite": "tests/e2e/vmaas", "test_id": "tests/e2e/vmaas/test_vm::test_restart",
                "source_sha": OTHER_SHA, "runs": 6, "passes": 3, "failures": 3,
                "p50_duration_s": 12.0, "p95_duration_s": 30.0,
            }], {
                "day": yesterday, "runs_listed": 6, "runs_parsed": 6,
                "runs_unavailable": 0, "generated_at": "2026-11-02T00:00:00Z",
            })
        finally:
            conn.close()
        monkeypatch.setattr(exporter, "CIOBS_TRENDS_MAX_ROWS", 1)

        raw = exporter.get_ci_observe_trends_json({"days": ["30"]})
        summary = exporter.get_ci_observe_trends_json({"days": ["30"], "summary": ["test"]})

        assert len(raw["rows"]) == 1, "the raw row cap really does drop a row here"
        restart = next(row for row in summary["tests"] if row["test_id"].endswith("test_restart"))
        assert (restart["runs"], restart["failures"]) == (10, 4)

    def test_the_test_summary_honours_the_test_filter(self, exporter, rollup):
        doc = exporter.get_ci_observe_trends_json(
            {"days": ["30"], "summary": ["test"], "test": ["restart"]}
        )

        assert [row["test_id"].rsplit("::", 1)[-1] for row in doc["tests"]] == ["test_restart"]

    def test_an_unknown_summary_returns_the_rows(self, exporter, rollup):
        doc = exporter.get_ci_observe_trends_json({"days": ["30"], "summary": ["nope"]})

        assert doc["tests"] == []
        assert len(doc["rows"]) == 2

    def test_the_test_summary_of_a_missing_rollup_is_a_note(self, exporter):
        doc = exporter.get_ci_observe_trends_json({"days": ["30"], "summary": ["test"]})

        assert doc["tests"] == []
        assert any("no per-test rollup" in note for note in doc["notes"])

    def test_compare_returns_the_two_revisions_side_by_side(self, exporter, two_revisions):
        doc = exporter.get_ci_observe_trends_compare_json(
            {"base": [SHA], "head": [OTHER_SHA], "days": ["30"]}
        )

        assert doc["days"] == 30
        assert len(doc["rows"]) == 2
        assert doc["base_sha"] == SHA
        assert doc["head_sha"] == OTHER_SHA

    def test_compare_defaults_and_garbage_days(self, exporter, two_revisions):
        assert exporter.get_ci_observe_trends_compare_json({})["days"] == 30
        assert exporter.get_ci_observe_trends_compare_json({"days": ["nonsense"]})["days"] == 30
        assert exporter.get_ci_observe_trends_compare_json({"days": ["0"]})["days"] == 1
        assert (
            exporter.get_ci_observe_trends_compare_json({"days": ["100000"]})["days"]
            == exporter.CIOBS_COMPARE_MAX_DAYS
        )

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

    def test_the_test_summary_is_served_for_the_entry_page(self, rollup, base_url):
        status, _, body = self.get(f"{base_url}/api/ci-observe/trends?days=7&summary=test")

        assert status == 200
        doc = json.loads(body)
        assert doc["summary"] == "test"
        assert [row["test_id"].rsplit("::", 1)[-1] for row in doc["tests"]] == [
            "test_restart", "test_create"
        ]
        assert doc["rows"] == []

    def test_compare_json_shape_and_cors(self, two_revisions, base_url):
        status, headers, body = self.get(
            f"{base_url}/api/ci-observe/trends/compare?base={SHA}&head={OTHER_SHA}&days=30"
        )

        assert status == 200
        assert headers.get("Content-Type") == "application/json"
        assert headers.get("Access-Control-Allow-Origin") == "*"
        doc = json.loads(body)
        assert doc["base_sha"] == SHA
        assert len(doc["rows"]) == 2

    def test_compare_without_shas_is_still_200(self, rollup, base_url):
        status, _, body = self.get(f"{base_url}/api/ci-observe/trends/compare")

        assert status == 200
        doc = json.loads(body)
        assert doc["rows"] == []
        assert any("base and head are required" in note for note in doc["notes"])

def test_deployment_copies_the_rollup_script():
    """The rollup runs from the deployed scripts directory and imports its
    sibling module, so monitoring-setup.sh must copy both."""
    setup = (MONITORING / "scripts" / "monitoring-setup.sh").read_text()

    assert "scripts/ci_observe_trends.py" in setup
    assert "ci_observe_run_evidence.py" in setup


def test_update_central_queues_the_rollup_without_waiting_for_it():
    """--update-central queues the rollup once after enabling the timer so a
    schema migration and the read of new runs start during the deploy, not
    at the next hourly run. It must not wait for them: a first backfill takes
    many minutes and the deploy would stall."""
    setup = (MONITORING / "scripts" / "monitoring-setup.sh").read_text()
    block = setup.split('if [[ "${MODE}" == "update-central" ]]', 1)[1]
    block = block.split('if [[ "${MODE}" == "update-agent" ]]', 1)[0]

    enable = block.index("systemctl --user enable --now ci-observe-trends.timer")
    start = block.index(
        "systemctl --user start --no-block ci-observe-trends.service"
    )
    assert enable < start, "the timer is installed before the one-shot starts"
    assert "systemctl --user start ci-observe-trends.service" not in block, (
        "a blocking start makes the deploy wait for the whole 30-day backfill"
    )
    assert "if ! systemctl --user start --no-block ci-observe-trends.service" in block, (
        "a start systemd cannot queue must warn, not fail the deploy"
    )
    assert "warn " in block
    assert "next hourly timer run" in block, "the warning says when the next run happens"
    # With --no-block the deploy never sees the backfill result, so the
    # warning must not claim the run finished or failed.
    warning = block.split("warn \"", 1)[1].split("\"", 1)[0]
    assert "did not finish" not in warning
    assert "could not queue" in warning


def test_rollup_is_mounted_in_the_exporter_and_scheduled_hourly():
    """The rollup runs inside the exporter container: that is where the token,
    requests and the /data volume live. A timer runs it every hour: a pass
    reads only the runs it has not read, so the pages are at most an hour
    behind and the archive never falls behind the exporter's 30 days."""
    quadlet = (MONITORING / "quadlet" / "workflow-exporter.container").read_text()
    assert "ci_observe_trends.py:/app/ci_observe_trends.py" in quadlet

    service = (MONITORING / "systemd" / "ci-observe-trends.service").read_text()
    assert "podman exec workflow-exporter python3 /app/ci_observe_trends.py --days 30" in service

    timer = (MONITORING / "systemd" / "ci-observe-trends.timer").read_text()
    assert "OnCalendar=*-*-* *:25:00" in timer
    assert "Persistent=true" in timer, "a missed pass runs when the machine comes back"

    setup = (MONITORING / "scripts" / "monitoring-setup.sh").read_text()
    assert "systemd/ci-observe-trends.service" in setup
    assert "systemd/ci-observe-trends.timer" in setup
    assert "systemctl --user enable --now ci-observe-trends.timer" in setup
    assert "systemctl --user enable ci-observe-trends.timer" in setup


def test_span_rollup_is_mounted_in_the_exporter_and_scheduled_hourly():
    """The span rollup reads Tempo from inside the exporter container and
    writes trends.db on its /data volume. It runs hourly, away from the
    trends rollup, and every central install and update wires it."""
    quadlet = (MONITORING / "quadlet" / "workflow-exporter.container").read_text()
    for module in ("ci_observe_spans.py", "ci_observe_phases.py", "ci_observe_insights.py"):
        assert f"{module}:/app/{module}" in quadlet, module

    service = (MONITORING / "systemd" / "ci-observe-spans.service").read_text()
    assert "podman exec workflow-exporter python3 /app/ci_observe_spans.py" in service
    timer = (MONITORING / "systemd" / "ci-observe-spans.timer").read_text()
    assert "OnCalendar=*-*-* *:45:00" in timer, "away from the trends rollup at 25 past"
    assert "Persistent=true" in timer

    setup = (MONITORING / "scripts" / "monitoring-setup.sh").read_text()
    update = setup.split('if [[ "${MODE}" == "update-central" ]]', 1)[1]
    update = update.split('if [[ "${MODE}" == "update-agent" ]]', 1)[0]
    install = setup.split('if [[ "${MODE}" == "update-agent" ]]', 1)[1]
    for module in ("ci_observe_spans.py", "ci_observe_phases.py", "ci_observe_insights.py"):
        assert f"scripts/{module}" in update, f"--update-central copies {module}"
        assert f"scripts/{module}" in install, f"a first install copies {module}"
    for block in (update, install):
        assert "systemd/ci-observe-spans.service" in block
        assert "systemd/ci-observe-spans.timer" in block
    assert "systemctl --user enable --now ci-observe-spans.timer" in update
    assert "systemctl --user enable ci-observe-spans.timer" in install
    assert '"ci-observe-spans.timer"' in install, "a first install starts the timer"


def test_the_archive_keeps_ninety_days():
    """Every ci-observe archive keeps the same 90 days, set once on the
    exporter container, so a page never compares windows of two lengths."""
    quadlet = (MONITORING / "quadlet" / "workflow-exporter.container").read_text()
    assert "Environment=CI_OBSERVE_KEEP_DAYS=90" in quadlet
    service = (MONITORING / "systemd" / "ci-observe-prs.service").read_text()
    assert "/app/ci_observe_prs.py --days 90" in service
