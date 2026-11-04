"""The ci-observe pull-request backfill (ci_observe_prs.py) and its endpoint.

Covered here:

- the parser: a merged pull request's stage boundaries, a closed pull
  request with no queue history, and a merged pull request whose first
  queue attempt was ejected (comment reason) and whose second was
  cancelled (run-conclusion fallback);
- the merge_group link: the gh-readonly-queue branch names one pull
  request, and a queue comment that links a run adds the other pull
  requests of the group;
- idempotency: a second run stores nothing, a --force run rewrites in
  place, and a failed GitHub page leaves the pull request unstored so the
  next run retries it;
- the API cost: one pass per pull request, merge_group runs listed once per
  repository;
- the endpoint: index + funnel, one pull request, missing DB, clamping,
  invalid input;
- the dashboard contract: the real response resolves every selector the
  dashboard queries.

Fixtures live in fixtures/ci_observe_prs/. The window is pinned with
backfill(now=...) so the tests never race the clock.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import requests
from helpers import FakeGitHub, FakeResponse

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ci_observe_prs"
DASHBOARDS_DIR = Path(__file__).resolve().parents[1] / "config" / "grafana" / "dashboards"
DASHBOARD_FILE = DASHBOARDS_DIR / "ci-observe-pull-requests.json"

NOW = datetime(2026, 11, 1, tzinfo=UTC)
DAYS = 60
REPO = "osac"
PULLS_PATH = f"/repos/osac-project/{REPO}/pulls?state=closed&sort=updated&direction=desc&per_page=100"
REPOS_PATH = "/orgs/osac-project/repos?per_page=100&type=all"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
def load_fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def merge_runs_path(now=NOW, days=DAYS):
    return runs_window_path(now - timedelta(days=days), now)


def runs_window_path(since, until):
    return (
        f"/repos/osac-project/{REPO}/actions/runs?event=merge_group"
        f"&created={since.strftime('%Y-%m-%dT%H:%M:%SZ')}..{until.strftime('%Y-%m-%dT%H:%M:%SZ')}&per_page=100"
    )


class FakeAPI:
    """The exporter-shaped API surface the backfill uses: _get and headers."""

    def __init__(self, github):
        self.github = github
        self.headers = {}

    def _get(self, url):
        return self.github.get(url)


def route_pr(github, fixture):
    number = fixture["pr"]["number"]
    github.add(
        f"/repos/osac-project/{REPO}/issues/{number}/timeline?per_page=100", FakeResponse(payload=fixture["timeline"])
    )
    github.add(
        f"/repos/osac-project/{REPO}/pulls/{number}/reviews?per_page=100", FakeResponse(payload=fixture["reviews"])
    )
    github.add(
        f"/repos/osac-project/{REPO}/pulls/{number}/commits?per_page=100", FakeResponse(payload=fixture["commits"])
    )


@pytest.fixture
def fixtures():
    return [load_fixture("merged_pr"), load_fixture("ejected_pr"), load_fixture("closed_pr")]


@pytest.fixture
def github(wfe):
    fake = FakeGitHub(wfe.API_URL)
    for fixture in [load_fixture("merged_pr"), load_fixture("ejected_pr"), load_fixture("closed_pr")]:
        route_pr(fake, fixture)
    fake.add(REPOS_PATH, FakeResponse(payload=[{"name": REPO}]))
    fake.add(
        PULLS_PATH,
        FakeResponse(payload=[load_fixture(name)["pr"] for name in ("merged_pr", "ejected_pr", "closed_pr")]),
    )
    fake.add(
        merge_runs_path(),
        FakeResponse(payload={"total_count": 7, "workflow_runs": load_fixture("merge_group_runs")["runs"]}),
    )
    return fake


@pytest.fixture
def service(prs, github, tmp_path):
    return prs.CIObservePRs(db_file=str(tmp_path / "prs.db"), api=FakeAPI(github))


def run_backfill(service, **kwargs):
    kwargs.setdefault("days", DAYS)
    kwargs.setdefault("now", NOW)
    return service.backfill(**kwargs)


def rows(service, sql, params=()):
    with sqlite3.connect(service.db_file) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params)]


def pr_row(service, number):
    return rows(service, "SELECT * FROM pr_events WHERE number = ?", (number,))[0]


def attempts(service, number):
    return rows(service, "SELECT * FROM pr_queue_attempts WHERE number = ? ORDER BY started_at", (number,))


# ---------------------------------------------------------------------------
# Parser: the three fixtures
# ---------------------------------------------------------------------------
class TestParser:
    def test_merged_pull_request_stage_durations(self, service):
        summary = run_backfill(service)
        assert summary["prs_stored"] == 3
        assert summary["attempts_stored"] == 5

        row = pr_row(service, 101)
        assert row["state"] == "merged"
        assert row["final_stage"] == "merged"
        assert row["author"] == "author1"
        assert row["labels"] == "ok-to-test"
        assert row["commit_count"] == 3
        assert row["review_count"] == 3
        assert row["review_request_count"] == 1
        # Bot reviews do not start the authoring stage and cannot approve.
        assert row["first_review_at"] == "2026-10-01T10:20:00Z"
        assert row["first_approval_at"] == "2026-10-01T10:50:00Z"
        # Enqueued is the start of the first merge_group run.
        assert row["queued_at"] == "2026-10-01T11:00:00Z"
        assert row["authoring_seconds"] == 20 * 60
        assert row["review_seconds"] == 30 * 60
        assert row["waiting_seconds"] == 10 * 60
        assert row["queue_seconds"] == 35 * 60
        assert row["merged_seconds"] == 60
        assert row["total_seconds"] == 95 * 60

    def test_merged_pull_request_keeps_both_attempts(self, service):
        run_backfill(service)
        first, second = attempts(service, 101)
        assert first["run_id"] == 5001
        assert first["ejected"] == 1
        assert first["ejection_kind"] == "check_failed"
        assert first["reason_source"] == "comment"
        assert "e2e / vmaas" in first["ejection_reason"]
        assert first["comment_author"] == "github-actions[bot]"
        assert first["comment_at"] == "2026-10-01T11:07:00Z"
        assert second["run_id"] == 5002
        assert second["ejected"] == 0
        assert second["ejection_reason"] == ""
        assert second["reason_source"] == ""

    def test_two_workflows_on_one_queue_entry_are_one_attempt(self, service):
        """One queue entry starts several workflows at one head_sha. Grouping
        them keeps a clean pass from looking like an ejection."""
        run_backfill(service)
        first, second = attempts(service, 101)

        assert first["run_id"] == 5001
        assert json.loads(first["run_ids"]) == [5001, 5003]
        assert first["conclusion"] == "failure"
        assert first["started_at"] == "2026-10-01T11:00:00Z"
        assert first["completed_at"] == "2026-10-01T11:05:00Z"

        assert second["run_id"] == 5002
        assert json.loads(second["run_ids"]) == [5002]

    def test_closed_pull_request_has_no_queue_history(self, service):
        run_backfill(service)
        row = pr_row(service, 102)
        assert row["state"] == "closed"
        assert row["final_stage"] == "closed in review"
        assert row["merged_at"] is None
        assert row["first_approval_at"] is None
        assert row["queued_at"] is None
        assert row["queue_seconds"] is None
        assert row["merged_seconds"] is None
        assert row["authoring_seconds"] == 30 * 60
        assert row["review_seconds"] is None  # no approval: unknown, not zero
        assert row["total_seconds"] == 3 * 60 * 60
        assert attempts(service, 102) == []

    def test_ejected_attempts_and_run_conclusion_fallback(self, service):
        run_backfill(service)
        ejected = attempts(service, 103)
        assert [attempt["run_id"] for attempt in ejected] == [5101, 5102, 5103]
        assert ejected[0]["ejection_kind"] == "check_failed"
        assert ejected[0]["reason_source"] == "comment"
        assert "removed from the merge queue" in ejected[0]["ejection_reason"]
        # The cancelled attempt has no comment: the reason is the run itself.
        assert ejected[1]["conclusion"] == "cancelled"
        assert ejected[1]["ejection_kind"] == "cancelled"
        assert ejected[1]["reason_source"] == "run"
        assert ejected[1]["ejection_reason"] == "merge_group entry (run 5102) concluded cancelled"
        assert ejected[2]["ejected"] == 0

    def test_unrelated_merge_group_run_is_not_attached(self, service):
        run_backfill(service)
        attached = {attempt["run_id"] for number in (101, 102, 103) for attempt in attempts(service, number)}
        assert attached == {5001, 5002, 5101, 5102, 5103}
        assert 5999 not in attached

    def test_comment_can_link_a_run_named_for_another_pull_request(self, prs, service, fixtures):
        """One merge group carries up to four pull requests; its branch names
        one. A queue comment that links the run adds the others."""
        runs = load_fixture("merge_group_runs")["runs"]
        timeline = [
            {
                "event": "commented",
                "created_at": "2026-10-03T09:16:00Z",
                "user": {"login": "github-actions[bot]", "type": "Bot"},
                "body": (
                    "Removed from the merge queue after https://github.com/osac-project/osac/actions/runs/5999 failed."
                ),
            }
        ]
        built = service._build_attempts(REPO, 101, runs, timeline)
        assert 5999 in [attempt["run_id"] for attempt in built]
        assert [attempt["run_id"] for attempt in built] == [5001, 5002, 5999]

    def test_retest_count_reads_the_exporter_job_history(self, prs, github, tmp_path):
        jobs_db = tmp_path / "workflow-exporter.db"
        with sqlite3.connect(jobs_db) as conn:
            conn.execute(
                "CREATE TABLE jobs (id INTEGER PRIMARY KEY, repo TEXT, pr_display TEXT, category TEXT, event TEXT)"
            )
            conn.executemany(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, ?)",
                [
                    (1, REPO, "#101", "e2e", "pull_request"),
                    (2, REPO, "#101", "e2e", "pull_request"),
                    (3, REPO, "#101", "e2e", "pull_request"),
                    (4, REPO, "#101", "e2e", "schedule"),
                    (5, REPO, "#102", "e2e", "pull_request"),
                ],
            )
        service = prs.CIObservePRs(db_file=str(tmp_path / "prs.db"), api=FakeAPI(github))
        run_backfill(service)
        assert pr_row(service, 101)["retest_count"] == 2
        assert pr_row(service, 102)["retest_count"] == 0

    def test_retest_count_is_unknown_without_a_jobs_db(self, service):
        run_backfill(service)
        assert pr_row(service, 101)["retest_count"] is None


# ---------------------------------------------------------------------------
# Idempotency and API cost
# ---------------------------------------------------------------------------
class TestBackfill:
    def test_api_cost_is_one_pass_per_pull_request(self, service, github):
        summary = run_backfill(service)
        # 1 org repos + 1 closed-PR page + 1 merge_group runs page, then
        # timeline + reviews + commits for each of the three pull requests.
        assert summary["api_calls"] == 12
        assert github.count(merge_runs_path()) == 1

    def test_second_run_stores_nothing(self, service, github):
        run_backfill(service)
        before = rows(service, "SELECT * FROM pr_events ORDER BY number")
        attempts_before = rows(service, "SELECT * FROM pr_queue_attempts ORDER BY run_id")
        calls_before = len(github.calls)

        summary = run_backfill(service)

        assert summary["prs_stored"] == 0
        assert summary["prs_skipped"] == 3
        assert rows(service, "SELECT * FROM pr_events ORDER BY number") == before
        assert rows(service, "SELECT * FROM pr_queue_attempts ORDER BY run_id") == attempts_before
        # Only the three shared listing calls: no per-PR fetch at all.
        assert len(github.calls) - calls_before == 3

    def test_force_rewrites_in_place(self, service):
        run_backfill(service)
        run_backfill(service, force=True)
        assert len(rows(service, "SELECT * FROM pr_events")) == 3
        assert len(rows(service, "SELECT * FROM pr_queue_attempts")) == 5

    def test_failed_page_leaves_the_pull_request_unstored(self, service, github):
        broken = f"/repos/osac-project/{REPO}/pulls/102/reviews?per_page=100"
        github.add(broken, FakeResponse(500, text="boom"))

        summary = run_backfill(service)

        assert summary["prs_stored"] == 2
        assert summary["prs_failed"] == 1
        assert rows(service, "SELECT number FROM pr_events ORDER BY number") == [{"number": 101}, {"number": 103}]
        # The next run retries only the missing pull request.
        github.add(broken, FakeResponse(payload=load_fixture("closed_pr")["reviews"]))
        second = run_backfill(service)
        assert second["prs_stored"] == 1
        assert pr_row(service, 102)["state"] == "closed"

    def test_network_timeout_is_a_gap_not_a_crash(self, service, github):
        """One timeout must not end the daily run before the other pull
        requests are stored; the affected one is retried next time."""
        broken = f"/repos/osac-project/{REPO}/issues/102/timeline?per_page=100"
        github.add(broken, requests.Timeout("timed out"))

        summary = run_backfill(service)

        assert summary["prs_stored"] == 2
        assert summary["prs_failed"] == 1
        assert [row["number"] for row in rows(service, "SELECT number FROM pr_events ORDER BY number")] == [101, 103]

    def test_failed_merge_group_query_leaves_pull_requests_unstored(self, service, github):
        """A transient failure on the runs query must not be written down as
        'no queue history'; the next run retries it."""
        healthy = FakeResponse(payload={"total_count": 7, "workflow_runs": load_fixture("merge_group_runs")["runs"]})
        github.add(merge_runs_path(), FakeResponse(500, text="boom"))

        summary = run_backfill(service)

        assert summary["prs_stored"] == 0
        assert summary["prs_failed"] == 3
        assert rows(service, "SELECT number FROM pr_events") == []

        github.add(merge_runs_path(), healthy)
        retry = run_backfill(service)
        assert retry["prs_stored"] == 3
        assert retry["attempts_stored"] == 5

    def test_merge_group_window_is_split_at_the_result_cap(self, service, github):
        """A busy window is split until each query fits, so runs past
        GitHub's list cap are not silently dropped."""
        cutoff = NOW - timedelta(days=DAYS)
        middle = cutoff + (NOW - cutoff) / 2
        top = runs_window_path(cutoff, NOW)
        left = runs_window_path(cutoff, middle)
        right = runs_window_path(middle, NOW)
        github.add(top, FakeResponse(payload={"total_count": 950, "workflow_runs": []}))
        github.add(
            left, FakeResponse(payload={"total_count": 7, "workflow_runs": load_fixture("merge_group_runs")["runs"]})
        )
        github.add(right, FakeResponse(payload={"total_count": 0, "workflow_runs": []}))

        summary = run_backfill(service)

        assert github.count(top) == 1
        assert github.count(left) == 1
        assert github.count(right) == 1
        assert summary["attempts_stored"] == 5

    def test_an_existing_attempt_table_gains_run_ids(self, prs, github, tmp_path):
        """A database written before grouping keeps its rows and gains the
        new column in place."""
        db_file = tmp_path / "prs.db"
        with sqlite3.connect(db_file) as conn:
            conn.execute("CREATE TABLE pr_queue_attempts (repo TEXT, number INTEGER, run_id INTEGER)")

        service = prs.CIObservePRs(db_file=str(db_file), api=FakeAPI(github))
        service._init_db()

        with sqlite3.connect(db_file) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(pr_queue_attempts)")}
        assert "run_ids" in columns

    def test_rate_limit_budget_stops_the_run(self, service, github):
        limit = {"remaining": "50"}
        original_get = github.get

        def limited(url, **kwargs):
            response = original_get(url, **kwargs)
            response.headers["X-RateLimit-Remaining"] = limit["remaining"]
            return response

        github.get = limited
        summary = run_backfill(service)

        assert summary["rate_limit_remaining"] == 50
        assert "rate limit" in summary["stopped_early"]
        assert summary["prs_stored"] == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class TestHelpers:
    def test_merge_queue_branch_parsing(self, prs):
        assert prs.merge_queue_pr_number("gh-readonly-queue/main/pr-42-abc123") == 42
        assert prs.merge_queue_pr_number("gh-readonly-queue/release/4.20/pr-7-deadbeef") == 7
        assert prs.merge_queue_pr_number("gh-readonly-queue/main/pr-42") is None
        assert prs.merge_queue_pr_number("main") is None

    def test_comment_body_is_one_clean_line(self, prs):
        body = "**The merge queue failed** for this pull request.\n\nSee [the run](https://x)."
        assert prs.clean_comment_body(body) == "The merge queue failed for this pull request."

    def test_ejection_kind_prefers_the_failed_check(self, prs):
        assert prs.ejection_kind("failure", "removed from the merge queue because a check failed") == "check_failed"
        assert prs.ejection_kind("cancelled", "") == "cancelled"
        assert prs.ejection_kind("failure", "the run timed out after 60 minutes") == "timed_out"
        assert prs.ejection_kind("success", "removed from the merge queue") == "removed"
        assert prs.ejection_kind("", "") == "unknown"

    def test_entry_conclusion_names_the_real_failure(self, prs):
        assert prs._entry_conclusion([{"conclusion": "cancelled"}, {"conclusion": "failure"}]) == "failure"
        assert prs._entry_conclusion([{"conclusion": "success"}, {"conclusion": "skipped"}]) == "success"
        assert prs._entry_conclusion([{"conclusion": "success"}, {"conclusion": None}]) == ""

    def test_missing_boundary_is_never_zero(self, prs):
        assert prs.seconds_between(None, "2026-10-01T10:00:00Z") is None
        assert prs.seconds_between("2026-10-01T10:00:00Z", None) is None
        assert prs.seconds_between("2026-10-01T10:00:00Z", "2026-10-01T10:01:00Z") == 60

    def test_percentile_matches_linear_interpolation(self, prs):
        assert prs.percentile([], 50) is None
        assert prs.percentile([600, 1200, 1800], 50) == 1200
        assert prs.percentile([600, 1200, 1800], 95) == 1740
        assert prs.percentile([100], 95) == 100

    def test_final_stage_names_where_a_closed_pr_stopped(self, prs):
        assert prs.final_stage("merged", "a", "b", "c") == "merged"
        assert prs.final_stage("closed", "a", "b", "c") == "closed in queue"
        assert prs.final_stage("closed", "a", "b", None) == "closed after approval"
        assert prs.final_stage("closed", "a", None, None) == "closed in review"
        assert prs.final_stage("closed", None, None, None) == "closed in authoring"


# ---------------------------------------------------------------------------
# Endpoint document
# ---------------------------------------------------------------------------
class TestEndpointDocument:
    @pytest.fixture
    def filled(self, service):
        run_backfill(service)
        return service

    def test_index_lists_recent_pull_requests(self, filled):
        doc = filled.get_json({"days": [str(DAYS)]})

        assert doc["mode"] == "index"
        assert doc["days"] == DAYS
        assert [pr["number"] for pr in doc["prs"]] == [103, 102, 101]
        first = doc["prs"][0]
        assert first["repo"] == REPO
        assert first["final_stage"] == "merged"
        assert first["author"] == "author3"
        assert first["url"] == "https://github.com/osac-project/osac/pull/103"
        assert first["queue_attempt_count"] == 3
        assert first["ejected_attempt_count"] == 2
        assert first["age_display"]
        assert "derived, not recorded by GitHub" in doc["derivation"]["ejections"]

    def test_index_age_is_the_lifetime_not_the_time_since_opened(self, filled):
        """The column says "opened to merge or close", so a pull request
        merged 95 minutes after it opened is 95 minutes old, however long
        ago that was."""
        by_number = {pr["number"]: pr for pr in filled.get_json({"days": [str(DAYS)]})["prs"]}

        assert by_number[101]["age_seconds"] == by_number[101]["total_seconds"] == 95 * 60
        assert by_number[101]["age_display"] == "1h 35m"

    def test_index_funnel_median_and_p95(self, filled):
        funnel = {row["stage"]: row for row in filled.get_json({"days": [str(DAYS)]})["funnel"]}

        assert set(funnel) == {"authoring", "review", "waiting", "queue", "merged"}
        assert funnel["authoring"]["samples"] == 3
        assert funnel["authoring"]["median_seconds"] == 1200
        assert funnel["authoring"]["p95_seconds"] == 1740
        # The closed PR was never approved, so it contributes to neither
        # review, waiting, queue nor merged.
        assert funnel["review"]["samples"] == 2
        assert funnel["review"]["median_seconds"] == 1500
        assert funnel["queue"]["samples"] == 2
        assert funnel["queue"]["median_seconds"] == 2850
        assert funnel["merged"]["median_seconds"] == 330

    def test_one_pull_request_document(self, filled):
        doc = filled.get_json({"number": ["103"], "repo": [REPO], "days": [str(DAYS)]})

        assert doc["mode"] == "pr"
        assert doc["pr"]["number"] == 103
        assert doc["pr"]["final_stage"] == "merged"
        assert doc["pr"]["queue_history"] == "present"
        assert doc["pr"]["queue_attempt_count"] == 3
        assert doc["pr"]["age_seconds"] == doc["pr"]["total_seconds"] == 2 * 60 * 60
        states = {stage["stage"]: stage["state"] for stage in doc["stages"]}
        assert states == {stage: "measured" for stage in ("authoring", "review", "waiting", "queue", "merged")}
        assert [attempt["run_id"] for attempt in doc["attempts"]] == [5101, 5102, 5103]
        assert doc["attempts"][0]["ejected"] is True
        assert doc["attempts"][0]["capture_url"] == ""
        assert doc["attempts"][0]["ejection"] == "ejected"
        assert any("derived" in note for note in doc["notes"])

    def test_closed_pull_request_says_no_queue_history(self, filled):
        doc = filled.get_json({"number": ["102"]})
        assert doc["pr"]["queue_history"] == "none"
        assert doc["attempts"] == []
        states = {stage["stage"]: stage["state"] for stage in doc["stages"]}
        assert states["merged"] == "not_reached"
        assert states["queue"] == "not_reached"
        assert states["waiting"] == "not_reached"
        assert any("no queue history" in note for note in doc["notes"])

    def test_unknown_number_is_an_empty_document_not_an_error(self, filled):
        doc = filled.get_json({"number": ["999"]})
        assert doc["mode"] == "pr"
        assert doc["pr"] is None
        assert doc["attempts"] == []
        assert "not in the backfill" in doc["notes"][0]

    def test_invalid_number_is_explained(self, filled):
        doc = filled.get_json({"number": ["abc"]})
        assert doc["pr"] is None
        assert "not a pull request number" in doc["notes"][0]

    def test_wrong_repository_returns_empty(self, filled):
        doc = filled.get_json({"number": ["101"], "repo": ["other"]})
        assert doc["pr"] is None

    def test_days_are_clamped_to_the_retention(self, filled):
        assert filled.get_json({"days": ["999"]})["days"] == 60
        assert filled.get_json({"days": ["0"]})["days"] == 1
        assert filled.get_json({"days": ["nonsense"]})["days"] == 30

    def test_missing_db_is_an_empty_dataset_and_read_only(self, prs, github, tmp_path):
        db_file = tmp_path / "missing" / "prs.db"
        service = prs.CIObservePRs(db_file=str(db_file), api=FakeAPI(github))

        index = service.get_json({"days": ["30"]})
        one = service.get_json({"number": ["101"]})

        assert index["prs"] == []
        assert {row["stage"] for row in index["funnel"]} == {"authoring", "review", "waiting", "queue", "merged"}
        assert all(row["samples"] == 0 for row in index["funnel"])
        assert "has not been backfilled" in index["notes"][0]
        assert one["pr"] is None
        assert one["notes"] == []
        assert not db_file.exists(), "the read path must not create prs.db"

    def test_no_token_or_header_leaks(self, filled):
        body = json.dumps([filled.get_json({}), filled.get_json({"number": ["101"]})])
        assert "PRIVATE_GITHUB_TOKEN" not in body
        assert "Authorization" not in body


# ---------------------------------------------------------------------------
# HTTP route
# ---------------------------------------------------------------------------
class TestHTTP:
    @pytest.fixture
    def base_url(self, wfe, service, monkeypatch, tmp_path):
        # The route reads prs.db next to the exporter's DB_FILE, which the
        # shared fixture points at tmp_path. Give it the backfilled file.
        monkeypatch.setattr(wfe, "DB_FILE", str(tmp_path / "workflow-exporter.db"))
        monkeypatch.setattr(wfe.ExporterHandler, "exporter", service)
        server = ThreadingHTTPServer(("127.0.0.1", 0), wfe.ExporterHandler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_address[1]}"
        server.shutdown()
        server.server_close()

    def get(self, url):
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.headers, resp.read()

    def test_index_route(self, base_url, service):
        run_backfill(service)
        status, headers, body = self.get(f"{base_url}/api/ci-observe/prs?days=60")

        assert status == 200
        assert headers.get("Content-Type") == "application/json"
        assert headers.get("Access-Control-Allow-Origin") == "*"
        doc = json.loads(body)
        assert doc["mode"] == "index"
        assert len(doc["prs"]) == 3

    def test_one_pull_request_route(self, base_url, service):
        run_backfill(service)
        status, _, body = self.get(f"{base_url}/api/ci-observe/prs?number=101&repo=osac")

        assert status == 200
        doc = json.loads(body)
        assert doc["mode"] == "pr"
        assert doc["pr"]["number"] == 101
        assert len(doc["attempts"]) == 2

    def test_queue_attempt_links_to_its_capture(self, base_url, service):
        """The store has no capture id; the exporter joins the run id to the
        Tempo cache and fills the Capture link. A missing cache stays empty."""
        run_backfill(service)

        class FakeRuns:
            def capture_ids_by_run(self):
                return {"5101": ["caas-pr103-5101-a1"]}

        service._runs = FakeRuns()
        status, _, body = self.get(f"{base_url}/api/ci-observe/prs?number=103&repo=osac")

        assert status == 200
        attempt = json.loads(body)["attempts"][0]
        assert attempt["capture_ids"] == ["caas-pr103-5101-a1"]
        assert "var-capture_id=caas-pr103-5101-a1" in attempt["capture_url"]
        assert attempt["capture_url"].startswith("/d/ci-observe-runs/")


# ---------------------------------------------------------------------------
# Dashboard contract: the real response resolves what the dashboard queries
# ---------------------------------------------------------------------------
requires_dashboard = pytest.mark.skipif(
    not DASHBOARD_FILE.exists(), reason="ci-observe-pull-requests.json ships in the dashboard change"
)


def iter_panels(panels):
    for panel in panels:
        yield panel
        yield from iter_panels(panel.get("panels", []))


@requires_dashboard
def test_dashboard_selectors_resolve_against_the_real_response(service):
    """Every root_selector and column the dashboard asks for must exist in
    the real endpoint response, so the two cannot drift apart."""
    run_backfill(service)
    dashboard = json.loads(DASHBOARD_FILE.read_text())
    documents = {
        "index": service.get_json({"days": [str(DAYS)]}),
        "pr": service.get_json({"number": ["101"], "repo": [REPO], "days": [str(DAYS)]}),
    }

    checked = 0
    for panel in iter_panels(dashboard["panels"]):
        for target in panel.get("targets", []):
            doc = documents["index"] if "days=" in target["url"] else documents["pr"]
            selector = target["root_selector"]
            if selector.startswith("$."):
                selector = selector[2:]
            node = doc
            for part in selector.split("."):
                node = node[part]
            assert node, f"{panel['title']}: {target['root_selector']} is empty"
            sample = node[0] if isinstance(node, list) else node
            for column in target["columns"]:
                assert column["selector"] in sample, (
                    f"{panel['title']}: column {column['selector']} is not in the endpoint response"
                )
            checked += 1
    assert checked >= 5


def test_backfill_is_mounted_in_the_exporter_and_scheduled_daily():
    """The backfill runs inside the exporter container: that is where the
    token, requests and the /data volume live. One pass a day is enough; a
    stored pull request is never refetched without --force."""
    monitoring = Path(__file__).resolve().parents[1]
    quadlet = (monitoring / "quadlet" / "workflow-exporter.container").read_text()
    assert "ci_observe_prs.py:/app/ci_observe_prs.py" in quadlet

    service = (monitoring / "systemd" / "ci-observe-prs.service").read_text()
    assert "podman exec workflow-exporter python3 /app/ci_observe_prs.py --days 60" in service

    timer = (monitoring / "systemd" / "ci-observe-prs.timer").read_text()
    assert "OnCalendar=*-*-* 04:45:00" in timer
    assert "Persistent=true" in timer

    setup = (monitoring / "scripts" / "monitoring-setup.sh").read_text()
    assert "systemd/ci-observe-prs.service" in setup
    assert "systemd/ci-observe-prs.timer" in setup
    assert "systemctl --user enable --now ci-observe-prs.timer" in setup
    assert "systemctl --user enable ci-observe-prs.timer" in setup
