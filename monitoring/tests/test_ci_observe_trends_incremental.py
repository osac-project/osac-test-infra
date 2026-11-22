"""The hourly, incremental rollup pass and the history archive
(ci_observe_trends.py): what is read again and what is not, the new per-run
fields (suite, trigger, pull request, failure message, canonical test key),
the suite_runs archive and the 90-day retention."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from helpers import FakeResponse, make_job
from conftest import _load

trends_module = _load("ci_observe_trends", "ci_observe_trends.py")
from test_ci_observe_trends import (
    DOWNLOAD_PATH,
    LIST_PATH,
    PRIVATE_NAME_PATH,
    RUN_ID,
    SECOND_DOWNLOAD_PATH,
    SECOND_LIST_PATH,
    SECOND_RUN_ID,
    SHA,
    TOKEN,
    VALID_JUNIT,
    artifact_payload,
    junit,
    passing,
    recent_iso,
    run_row,
    today,
    zip_bytes,
)

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def no_artifacts_anywhere(github):
    """Both places a run's artifact can be answer with none (see
    find_artifacts: both are checked)."""
    github.routes[LIST_PATH] = [FakeResponse(payload={"total_count": 0, "artifacts": []})]
    for name in (f"osac-logs-full-install-{RUN_ID}", f"osac-logs-caas-full-install-{RUN_ID}",
                 f"osac-logs-bmaas-full-install-{RUN_ID}"):
        github.routes[PRIVATE_NAME_PATH.format(name=name)] = [
            FakeResponse(payload={"total_count": 0, "artifacts": []})]


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def wired(trends, exporter, wfe, github, monkeypatch, tmp_path):
    monkeypatch.setattr(trends, "TOKEN", TOKEN)
    monkeypatch.setattr(trends, "EXPORTER_DB", wfe.DB_FILE)
    monkeypatch.setattr(trends, "TRENDS_DB", str(tmp_path / "trends.db"))
    github.add(LIST_PATH, artifact_payload(wfe.API_URL))
    github.add(DOWNLOAD_PATH, FakeResponse(content=zip_bytes({"junit.xml": VALID_JUNIT})))
    return trends


def query(trends_module, sql, *args):
    conn = sqlite3.connect(trends_module.TRENDS_DB)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, args)]
    finally:
        conn.close()


class TestNewFields:
    def test_a_failed_case_keeps_the_first_line_of_its_message(self, trends):
        document = junit(
            '<testcase classname="tests.e2e.vmaas.test_vm" name="test_x" time="1.0">'
            '<failure message="StuckState: ComputeInstance ci-1 to become Ready not reached after 600s&#10;'
            'second line">trace</failure></testcase>'
        )
        cases, _ = trends.parse_junit_outcomes([("junit.xml", document, "vmaas")])
        assert cases[0]["message"] == "StuckState: ComputeInstance ci-1 to become Ready not reached after 600s"
        assert cases[0]["suite_key"] == "vmaas"

    def test_a_failure_without_a_message_names_its_type(self, trends):
        document = junit('<testcase classname="c" name="t" time="1.0"><error type="TimeoutError"/></testcase>')
        cases, _ = trends.parse_junit_outcomes([("junit.xml", document)])
        assert cases[0]["message"] == "TimeoutError"

    def test_the_same_test_in_two_suites_of_one_run_is_two_rows(self, trends):
        cases, _ = trends.parse_junit_outcomes([
            ("junit.xml", junit(passing("test_ref")), "vmaas"),
            ("junit.xml", junit(passing("test_ref")), "bmaas"),
        ])
        assert sorted(case["suite_key"] for case in cases) == ["bmaas", "vmaas"]

    @pytest.mark.parametrize(
        ("name", "suite"),
        [
            (f"osac-logs-full-install-{RUN_ID}", "vmaas"),
            (f"osac-logs-bmaas-full-install-{RUN_ID}", "bmaas"),
            (f"osac-logs-caas-full-install-{RUN_ID}", "caas"),
            ("something-else", ""),
        ],
    )
    def test_the_artifact_names_the_suite(self, trends, name, suite):
        assert trends.artifact_suite(name) == suite

    def test_test_run_rows_carry_suite_trigger_pr_and_key(self, trends):
        run = {
            "run_id": 1, "day": "2026-11-01", "repo": "osac", "created_at": "2026-11-01T10:00:00Z",
            "workflow": "E2E for the merge queue", "source_sha": SHA, "trigger": "merge_queue", "pr": "42",
            "cases": [
                {"suite": "s", "test_id": "tests.e2e.caas.sanity.test_c::test_a", "passed": False,
                 "duration_s": 3.0, "message": "boom"},
                {"suite": "s", "test_id": "tests.e2e.bmaas.test_b::test_b", "passed": True, "duration_s": 1.0,
                 "suite_key": "bmaas"},
            ],
        }
        rows = trends.test_run_rows([run])
        assert rows[0]["suite_key"] == "caas", "a merge-queue run names no suite; the test path does"
        assert rows[0]["test_key"] == "tests.e2e.caas.sanity.test_c::test_a"
        assert (rows[0]["trigger"], rows[0]["pr"], rows[0]["message"]) == ("merge_queue", "42", "boom")
        assert rows[1]["suite_key"] == "bmaas"
        assert rows[1]["message"] == ""

    def test_a_run_knows_its_trigger_pr_and_whether_it_finished(self, trends):
        runs = trends.candidate_runs([
            run_row(workflow="E2E suite · caas · #77 · 0123abc", event="workflow_dispatch",
                    conclusion="", status="in_progress"),
        ])
        assert runs[0]["trigger"] == "pr"
        assert runs[0]["pr"] == "77"
        assert runs[0]["finished"] is False
        flow = trends.candidate_runs([
            make_job(id=RUN_ID, workflow="E2E for the merge queue", category="automation", event="merge_group",
                     pr_display="#9", url=f"https://github.com/osac-project/osac/actions/runs/{RUN_ID}"),
            make_job(id=5, workflow="E2E vmaas (merge queue)", category="e2e", event="merge_group",
                     url=f"https://github.com/osac-project/osac/actions/runs/{RUN_ID}/job/5",
                     updated_at="2026-11-01T11:00:00Z"),
        ])
        assert flow[0]["trigger"] == "merge_queue"
        assert flow[0]["pr"] == "9"
        assert flow[0]["finished"] is True
        assert flow[0]["finished_at"] == "2026-11-01T11:00:00Z"


class TestReadDecision:
    run = {"finished": True, "created_at": iso(NOW - timedelta(days=1)), "finished_at": iso(NOW - timedelta(hours=5))}

    def fetch(self, state, checked=NOW - timedelta(hours=2), version=None, finished=None):
        return {"state": state, "checked_at": iso(checked),
                "version": trends_module.PARSER_VERSION if version is None else version,
                "finished_at": iso(finished) if finished else self.run["finished_at"]}

    def test_new_and_unfinished_runs(self, trends):
        assert trends.read_decision(self.run, None, False, NOW) == "fetch"
        assert trends.read_decision({**self.run, "finished": False}, None, False, NOW) == "skip"
        assert trends.read_decision(self.run, self.fetch("parsed"), True, NOW, force=True) == "fetch"

    def test_a_parsed_run_is_reused(self, trends):
        assert trends.read_decision(self.run, self.fetch("parsed"), True, NOW) == "stored"
        assert trends.read_decision(self.run, self.fetch("parsed"), False, NOW) == "fetch", "rows went missing"

    def test_an_older_parser_rereads_while_the_artifact_exists(self, trends):
        assert trends.read_decision(self.run, self.fetch("parsed", version=1), True, NOW) == "fetch"
        old = {**self.run, "created_at": iso(NOW - timedelta(days=40))}
        assert trends.read_decision(old, self.fetch("parsed", version=1), True, NOW) == "stored"

    def test_a_lookup_only_change_does_not_reread_parsed_runs(self, trends):
        """Version 3 changed where an artifact is looked for, not how a found
        one is parsed: a run version 2 parsed is current. Re-reading every
        parsed run on 2026-11-22 cost some 4,000 runs' worth of the budget
        everything shares, for identical rows."""
        assert trends.PARSED_CURRENT_FROM < trends.PARSER_VERSION
        parsed_v2 = self.fetch("parsed", version=trends.PARSED_CURRENT_FROM)
        assert trends.read_decision(self.run, parsed_v2, True, NOW) == "stored"
        no_data_v2 = self.fetch("no_data", version=trends.NO_DATA_CURRENT_FROM - 1,
                                checked=NOW - timedelta(hours=2), finished=NOW - timedelta(hours=6))
        assert trends.read_decision(self.run, no_data_v2, False, NOW) == "fetch", "a lookup change can find it"

    def test_a_reader_asks_a_run_whose_artifact_is_gone_once(self, trends):
        """A re-read for new fields that found the artifact gone keeps the old
        rows; the reader that looked does not look again every hour."""
        looked = {**self.fetch("parsed", version=1), "checked_version": trends.PARSER_VERSION}
        assert trends.read_decision(self.run, looked, True, NOW) == "stored"
        older_reader = {**self.fetch("parsed", version=1), "checked_version": trends.PARSER_VERSION - 1}
        assert trends.read_decision(self.run, older_reader, True, NOW) == "fetch"

    def test_the_most_urgent_runs_are_read_first(self, trends):
        """No data at all before new fields; an artifact about to expire
        before a newer run; then the newest."""
        def run(run_id, days_ago):
            return {"run_id": run_id, "created_at": iso(NOW - timedelta(days=days_ago))}
        fresh, older, expiring = run(1, 1), run(2, 5), run(3, 29)
        reread = run(4, 0.5)
        order = sorted([(older, False), (reread, True), (fresh, False), (expiring, False)],
                       key=lambda pair: trends.read_priority(pair[0], pair[1], NOW))
        assert [pair[0]["run_id"] for pair in order] == [3, 1, 2, 4]

    def test_no_data_is_checked_once_more_only_soon_after_the_run(self, trends):
        finished = NOW - timedelta(hours=5)
        early = self.fetch("no_data", checked=finished + timedelta(minutes=5), finished=finished)
        late = self.fetch("no_data", checked=finished + timedelta(hours=4), finished=finished)
        assert trends.read_decision(self.run, early, False, NOW) == "fetch"
        assert trends.read_decision(self.run, late, False, NOW) == "skip"

    def test_an_older_parser_rereads_a_failed_lookup_while_the_artifact_exists(self, trends):
        """A no_data row written by an older parser is re-read: a newer
        parser can resolve what an older one could not (the
        private-publication switch of 2026-11-14 darkened every
        pull-request run until the reader learned the second place). Past
        artifact retention it stays skipped: re-reading cannot change the
        answer."""
        older = self.fetch("no_data", version=trends.PARSER_VERSION - 1, checked=NOW - timedelta(hours=2),
                           finished=NOW - timedelta(hours=6))
        assert trends.read_decision(self.run, older, False, NOW) == "fetch"
        old_run = {**self.run, "created_at": iso(NOW - timedelta(days=40))}
        assert trends.read_decision(old_run, older, False, NOW) == "skip"

    def test_an_error_is_retried_after_a_while(self, trends):
        assert trends.read_decision(self.run, self.fetch("error", checked=NOW - timedelta(minutes=5)), False,
                                    NOW) == "skip"
        assert trends.read_decision(self.run, self.fetch("error", checked=NOW - timedelta(hours=2)), False,
                                    NOW) == "fetch"


class TestHourlyPass:
    def test_a_run_without_an_artifact_is_not_listed_again(self, wired, exporter, github):
        exporter._upsert_job(run_row(updated_at=recent_iso(days_ago=1)))
        no_artifacts_anywhere(github)
        assert wired.main(["--days", "7"]) == 0
        assert wired.main(["--days", "7"]) == 0
        assert github.count(LIST_PATH) == 1
        fetches = query(wired, "SELECT state, version FROM run_fetches")
        assert fetches == [{"state": "no_data", "version": wired.PARSER_VERSION}]

    def test_every_pass_is_recorded(self, wired, exporter, github):
        assert wired.main(["--days", "7"]) == 0
        assert [row["job"] for row in query(wired, "SELECT job FROM rollup_passes")] == ["trends"], (
            "a pass with nothing to roll up is still a pass")
        exporter._upsert_job(run_row(updated_at=recent_iso(days_ago=1)))
        no_artifacts_anywhere(github)
        assert wired.main(["--days", "7"]) == 0
        assert query(wired, "SELECT job, summary FROM rollup_passes") == [
            {"job": "trends", "summary": "1 run(s) considered"}]

    def test_a_github_error_stops_the_pass_and_is_not_a_pass(self, wired, exporter, github, caplog):
        exporter._upsert_job(run_row())
        github.routes[LIST_PATH] = [FakeResponse(status=502)]

        assert wired.main(["--days", "7"]) == 1

        assert "the test history pass stopped" in caplog.text
        assert f"run {RUN_ID} of osac-project/osac answered HTTP 502" in caplog.text
        assert query(wired, "SELECT * FROM run_fetches") == []
        tables = {row["name"] for row in query(wired, "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "rollup_passes" not in tables, "a stopped pass is not a pass"
        # GitHub answers again: the next pass reads the run.
        github.routes[LIST_PATH] = [artifact_payload("https://api.github.com")]
        assert wired.main(["--days", "7"]) == 0
        assert query(wired, "SELECT state FROM run_fetches") == [{"state": "parsed"}]
        assert len(query(wired, "SELECT * FROM test_runs")) == 3

    def test_a_refusal_keeps_what_was_read_before_it(self, wired, exporter, github):
        """Until 2026-11-22 a stopped pass stored nothing: with more runs to
        read than one pass could, every hourly pass re-read the same runs,
        spent the token's budget, stored nothing and starved the exporter
        and the run pages. What was read before the refusal is stored now,
        and the next pass reads only what is left."""
        exporter._upsert_job(run_row(created_at=recent_iso(days_ago=0)))
        exporter._upsert_job(run_row(run_id=SECOND_RUN_ID, created_at=recent_iso(days_ago=1)))
        github.add(SECOND_LIST_PATH, FakeResponse(status=502))

        assert wired.main(["--days", "7"]) == 1

        assert query(wired, "SELECT run_id, state FROM run_fetches") == [{"run_id": RUN_ID, "state": "parsed"}]
        assert len(query(wired, "SELECT * FROM test_runs WHERE run_id = ?", RUN_ID)) == 3
        assert "rollup_passes" not in {row["name"] for row in query(
            wired, "SELECT name FROM sqlite_master WHERE type = 'table'")}, "a refused pass is not a pass"
        github.add(SECOND_LIST_PATH, artifact_payload("https://api.github.com", run_id=SECOND_RUN_ID, art_id=43))
        github.add(SECOND_DOWNLOAD_PATH, FakeResponse(content=zip_bytes({"junit.xml": VALID_JUNIT})))
        assert wired.main(["--days", "7"]) == 0
        assert github.count(LIST_PATH) == 1, "the run read before the refusal is not read again"
        assert {row["run_id"] for row in query(wired, "SELECT run_id FROM run_fetches")} == {RUN_ID, SECOND_RUN_ID}

    def test_a_spent_budget_leaves_the_rest_to_the_next_pass(self, wired, exporter, github):
        exporter._upsert_job(run_row(created_at=recent_iso(days_ago=0)))
        exporter._upsert_job(run_row(run_id=SECOND_RUN_ID, created_at=recent_iso(days_ago=1)))
        github.add(SECOND_LIST_PATH, FakeResponse(
            status=403, text='{"message": "API rate limit exceeded for user ID 1."}',
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1795000000"}))

        assert wired.main(["--days", "7"]) == 0, "it read what the budget allowed: a pass"

        [recorded] = query(wired, "SELECT summary, pending FROM rollup_passes WHERE job = 'trends'")
        assert recorded["pending"] == 1
        assert "1 read, 1 left for the next passes" in recorded["summary"]
        assert "rate limit is spent; it resets at" in recorded["summary"]
        assert [row["run_id"] for row in query(wired, "SELECT run_id FROM run_fetches")] == [RUN_ID]

    def test_a_pass_that_reads_nothing_it_has_to_is_not_a_pass(self, wired, exporter, github, caplog):
        exporter._upsert_job(run_row())
        github.routes[LIST_PATH] = [FakeResponse(status=403, text="API rate limit exceeded",
                                                 headers={"X-RateLimit-Remaining": "0"})]
        assert wired.main(["--days", "7"]) == 1
        assert "read none of the 1 run(s) it has to read" in caplog.text
        assert "rollup_passes" not in {row["name"] for row in query(
            wired, "SELECT name FROM sqlite_master WHERE type = 'table'")}

    def test_the_reads_stop_at_the_reserve_left_for_the_exporter(self, wired, exporter, github, monkeypatch):
        monkeypatch.setattr(wired, "GITHUB_RESERVE", 2500)
        exporter._upsert_job(run_row(created_at=recent_iso(days_ago=0)))
        exporter._upsert_job(run_row(run_id=SECOND_RUN_ID, created_at=recent_iso(days_ago=1)))
        github.add(LIST_PATH, FakeResponse(payload=artifact_payload("https://api.github.com").json(),
                                           headers={"X-RateLimit-Remaining": "2499"}))
        assert wired.main(["--days", "7"]) == 0
        assert github.count(SECOND_LIST_PATH) == 0, "below the reserve, nothing more is asked"
        [recorded] = query(wired, "SELECT summary, pending FROM rollup_passes WHERE job = 'trends'")
        assert recorded["pending"] == 1
        assert "2499 of the token's GitHub API calls are left this hour, below the 2500" in recorded["summary"]

    def test_a_pass_reads_at_most_its_bound(self, wired, exporter, github, monkeypatch):
        monkeypatch.setattr(wired, "MAX_READS_PER_PASS", 1)
        exporter._upsert_job(run_row(created_at=recent_iso(days_ago=0)))
        exporter._upsert_job(run_row(run_id=SECOND_RUN_ID, created_at=recent_iso(days_ago=1)))
        assert wired.main(["--days", "7"]) == 0
        assert github.count(SECOND_LIST_PATH) == 0
        assert query(wired, "SELECT pending FROM rollup_passes WHERE job = 'trends'") == [{"pending": 1}]

    def test_a_reread_left_for_later_keeps_its_rows(self, wired, exporter, github, monkeypatch):
        """A run waiting for its re-read keeps what is stored for it: a pass
        stopped early never thins a day."""
        exporter._upsert_job(run_row())
        assert wired.main(["--days", "7"]) == 0
        conn = sqlite3.connect(wired.TRENDS_DB)
        conn.execute("UPDATE run_fetches SET version = 1, checked_version = NULL")
        conn.commit()
        conn.close()
        monkeypatch.setattr(wired, "MAX_READS_PER_PASS", 0)
        assert wired.main(["--days", "7"]) == 1, "it read none of what it had to"
        assert len(query(wired, "SELECT * FROM test_runs")) == 3
        assert query(wired, "SELECT runs_parsed FROM rollup_days") == [{"runs_parsed": 1}]

    def test_a_new_run_is_stored_beside_a_reread_left_for_later(self, wired, exporter, github, monkeypatch):
        """A day holding a run waiting for its re-read and a run read now is
        rebuilt with both: the waiting run counts with its stored rows, so
        the new run's rows are written now instead of waiting for it."""
        exporter._upsert_job(run_row(created_at=recent_iso(days_ago=0)))
        assert wired.main(["--days", "7"]) == 0
        conn = sqlite3.connect(wired.TRENDS_DB)
        conn.execute("UPDATE run_fetches SET version = 1, checked_version = NULL")
        conn.commit()
        conn.close()
        exporter._upsert_job(run_row(run_id=SECOND_RUN_ID, created_at=recent_iso(days_ago=0)))
        github.add(SECOND_LIST_PATH, artifact_payload("https://api.github.com", run_id=SECOND_RUN_ID, art_id=43))
        github.add(SECOND_DOWNLOAD_PATH, FakeResponse(content=zip_bytes({"junit.xml": VALID_JUNIT})))
        monkeypatch.setattr(wired, "MAX_READS_PER_PASS", 1)
        assert wired.main(["--days", "7"]) == 0
        assert github.count(SECOND_LIST_PATH) == 1 and github.count(LIST_PATH) == 1, "the run with no data first"
        stored = {row["run_id"] for row in query(wired, "SELECT DISTINCT run_id FROM test_runs")}
        assert stored == {RUN_ID, SECOND_RUN_ID}
        assert query(wired, "SELECT runs_parsed FROM rollup_days") == [{"runs_parsed": 2}]

    def test_runs_stored_after_a_private_404_are_read_again(self, wired, exporter, github):
        """Before 2026-11-15 a 404 from osac-ci-artifacts (a token that
        cannot see it) was stored as "no artifact" and never asked again."""
        exporter._upsert_job(run_row())
        conn = wired.open_trends_db(wired.TRENDS_DB)
        with conn:
            conn.execute(
                "INSERT INTO run_fetches (run_id, day, state, reason, version, checked_at, finished_at)"
                " VALUES (?, '2026-11-14', 'no_data', ?, 2, '2026-11-14T06:00:00Z', '2026-11-14T05:26:50Z')",
                (RUN_ID, "no osac-logs-[<suite>-]full-install artifact "
                         "(private artifact lookup in osac-ci-artifacts failed with HTTP 404)"),
            )
        conn.close()

        assert wired.main(["--days", "7"]) == 0

        assert query(wired, "SELECT state FROM run_fetches") == [{"state": "parsed"}]
        assert len(query(wired, "SELECT * FROM test_runs")) == 3

    def test_a_running_run_waits_until_it_finished(self, wired, exporter, github):
        exporter._upsert_job(run_row(conclusion="", status="in_progress"))
        assert wired.main(["--days", "7"]) == 0
        assert github.count(LIST_PATH) == 0
        coverage = query(wired, "SELECT * FROM rollup_days")[0]
        assert (coverage["runs_listed"], coverage["runs_parsed"]) == (1, 0)
        exporter._upsert_job(run_row(conclusion="failure", status="failure", run_attempt=2))
        assert wired.main(["--days", "7"]) == 0
        assert github.count(LIST_PATH) == 1
        coverage = query(wired, "SELECT * FROM rollup_days")[0]
        assert (coverage["runs_listed"], coverage["runs_parsed"]) == (1, 1)

    def test_the_rows_carry_the_new_fields(self, wired, exporter):
        exporter._upsert_job(run_row(workflow="E2E suite · vmaas · #12 · 0123abc", event="workflow_dispatch"))
        assert wired.main(["--days", "7"]) == 0
        rows = {row["test_id"].rsplit("::", 1)[-1]: row for row in query(wired, "SELECT * FROM test_runs")}
        assert rows["test_fail"]["message"] == "assert 1 == 2"
        assert rows["test_error"]["message"] == "timed out after 30s"
        assert rows["test_pass"]["message"] == ""
        assert {row["suite_key"] for row in rows.values()} == {"vmaas"}
        assert {(row["trigger"], row["pr"]) for row in rows.values()} == {("pr", "12")}
        assert rows["test_pass"]["test_key"] == "tests.e2e.networking.vmaas.test_vm::test_pass"

    def test_a_legacy_run_keeps_its_rows_when_its_artifact_is_gone(self, wired, exporter, github):
        """A run an older pass read (no run_fetches row) is read again for the
        new fields; when the artifact is gone, its stored rows stay."""
        exporter._upsert_job(run_row())
        assert wired.main(["--days", "7"]) == 0
        conn = sqlite3.connect(wired.TRENDS_DB)
        conn.execute("DELETE FROM run_fetches")
        conn.commit()
        conn.close()
        no_artifacts_anywhere(github)
        assert wired.main(["--days", "7"]) == 0
        assert len(query(wired, "SELECT * FROM test_runs")) == 3
        assert query(wired, "SELECT state FROM run_fetches") == [{"state": "parsed"}]
        assert wired.main(["--days", "7"]) == 0
        assert github.count(LIST_PATH) == 2, "the legacy run is not asked about again"

    def test_stored_rows_get_their_keys_on_open(self, trends, tmp_path):
        path = str(tmp_path / "trends.db")
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE test_runs (day TEXT NOT NULL, repo TEXT NOT NULL, workflow TEXT NOT NULL,
                suite TEXT NOT NULL, test_id TEXT NOT NULL, source_sha TEXT NOT NULL, run_id INTEGER NOT NULL,
                created_at TEXT NOT NULL, duration_s REAL, passed INTEGER NOT NULL);
            INSERT INTO test_runs VALUES ('2026-11-01', 'osac', 'E2E for the merge queue', 's',
                'tests.e2e.bmaas.sanity.test_b::test_x', '', 1, '2026-11-01T00:00:00Z', 1.0, 1);
        """)
        conn.commit()
        conn.close()
        trends.open_trends_db(path).close()
        conn = sqlite3.connect(path)
        row = conn.execute("SELECT test_key, suite_key, message FROM test_runs").fetchone()
        conn.close()
        assert row == ("tests.e2e.bmaas.sanity.test_b::test_x", "bmaas", None)


class TestArchive:
    def test_every_e2e_row_is_copied_and_kept_after_the_exporter_prunes_it(self, wired, exporter, wfe):
        exporter._upsert_job(run_row(timeline_json='[{"name": "e2e"}]'))
        exporter._upsert_job(make_job(id=77, category="ci", workflow="CI"))
        assert wired.main(["--days", "7"]) == 0
        archived = query(wired, "SELECT id, workflow, timeline_json FROM suite_runs")
        assert archived == [{"id": RUN_ID, "workflow": "E2E VMaaS Full Install",
                             "timeline_json": '[{"name": "e2e"}]'}]
        with exporter._db() as conn:
            conn.execute("DELETE FROM jobs")
        assert wired.main(["--days", "7"]) == 0
        assert [row["id"] for row in query(wired, "SELECT id FROM suite_runs")] == [RUN_ID]

    def test_an_exporter_db_from_before_timelines_is_copied_too(self, trends, tmp_path):
        exporter_db = tmp_path / "old.db"
        conn = sqlite3.connect(exporter_db)
        conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, repo TEXT, workflow TEXT, category TEXT,"
                     " created_at TEXT, conclusion TEXT)")
        conn.execute("INSERT INTO jobs VALUES (1, 'osac', 'E2E CaaS Full Install', 'e2e', '2026-11-01T00:00:00Z',"
                     " 'success')")
        conn.commit()
        conn.close()
        trends_conn = trends.open_trends_db(str(tmp_path / "trends.db"))
        try:
            assert trends.archive_suite_runs(trends_conn, str(exporter_db)) == 1
            row = dict(trends_conn.execute("SELECT * FROM suite_runs").fetchone())
        finally:
            trends_conn.close()
        assert (row["workflow"], row["timeline_json"], row["steps_json"]) == ("E2E CaaS Full Install", "", "[]")

    def test_an_unreadable_exporter_db_archives_nothing(self, trends, tmp_path):
        conn = trends.open_trends_db(str(tmp_path / "trends.db"))
        try:
            # An unreadable exporter DB is a refused archive, not "zero
            # rows copied": the caller must not record a fresh pass.
            assert trends.archive_suite_runs(conn, str(tmp_path / "missing" / "nope.db")) is None
        finally:
            conn.close()

    def test_retention_is_the_keep_window(self, trends, tmp_path):
        conn = trends.open_trends_db(str(tmp_path / "trends.db"))
        try:
            conn.execute("INSERT INTO suite_runs (id, created_at) VALUES (1, '2026-01-01T00:00:00Z')")
            conn.execute("INSERT INTO suite_runs (id, created_at) VALUES (2, ?)", (recent_iso(),))
            conn.execute("INSERT INTO run_fetches (run_id, day, state, version, checked_at)"
                         " VALUES (1, '2026-01-01', 'parsed', 2, 'x')")
            conn.commit()
            trends.prune_old_days(conn, (datetime.now(timezone.utc) - timedelta(days=90)).date().isoformat())
            assert [row[0] for row in conn.execute("SELECT id FROM suite_runs")] == [2]
            assert conn.execute("SELECT COUNT(*) FROM run_fetches").fetchone()[0] == 0
        finally:
            conn.close()
        assert trends.KEEP_DAYS == 90


def test_the_day_coverage_names_its_runs(wired, exporter):
    exporter._upsert_job(run_row())
    assert wired.main(["--days", "7"]) == 0
    coverage = query(wired, "SELECT * FROM rollup_days WHERE day = ?", today())[0]
    assert json.loads(coverage["parsed_run_ids"]) == [RUN_ID]
