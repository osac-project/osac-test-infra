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
  repository, and no merge_group query in an hour with nothing new;
- the hourly pass: auto mode is a full pass once a day and incremental
  otherwise, a pull request merged between two runs appears on the next one,
  the listing carries `since`, and stored rows are skipped;
- freshness: never, fresh and stale, plus the pass history in
  pr_backfill_runs;
- the four miss states (open, not_yet_fetched, missing, outside_window),
  including the read-only live lookup and its cache;
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
from helpers import FakeGitHub, FakeResponse, iso

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ci_observe_prs"
DASHBOARDS_DIR = Path(__file__).resolve().parents[1] / "config" / "grafana" / "dashboards"
DASHBOARD_FILE = DASHBOARDS_DIR / "ci-observe-pull-requests.json"

NOW = datetime(2026, 11, 1, tzinfo=UTC)
DAYS = 60
REPO = "osac"
RETENTION_START = NOW - timedelta(days=DAYS)
REPOS_PATH = "/orgs/osac-project/repos?per_page=100&type=all"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
def load_fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def pulls_path(since):
    """A closed-PR listing bounded by updated_at, the `since` the backfill
    sends on every pass."""
    return f"/repos/osac-project/{REPO}/pulls?state=closed&sort=updated&direction=desc&per_page=100&since={iso(since)}"


PULLS_PATH = pulls_path(RETENTION_START)


def oldest_pending():
    """The earliest created_at among the three fixture pull requests. The
    merge_group query starts there: a queue entry cannot predate its PR."""
    return min(
        datetime.fromisoformat(load_fixture(name)["pr"]["created_at"].replace("Z", "+00:00"))
        for name in ("merged_pr", "ejected_pr", "closed_pr")
    )


def merge_runs_path(now=NOW):
    return runs_window_path(oldest_pending(), now)


def runs_window_path(since, until):
    return f"/repos/osac-project/{REPO}/actions/runs?event=merge_group&created={iso(since)}..{iso(until)}&per_page=100"


class FakeAPI:
    """The exporter-shaped API surface the backfill uses: _get and headers."""

    def __init__(self, github):
        self.github = github
        self.headers = {}

    def _get(self, url):
        return self.github.get(url)


def route_pr(github, fixture, number=None):
    number = number or fixture["pr"]["number"]
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
    # The live lookup cache is module-level so the exporter's per-request
    # CIObservePRs shares it. Start every test from a cold cache.
    prs._LIVE_PR_CACHE.clear()
    return prs.CIObservePRs(db_file=str(tmp_path / "prs.db"), api=FakeAPI(github))


def run_backfill(service, **kwargs):
    # The parser tests describe one full pass; the hourly behaviour has its
    # own section and asks for mode explicitly.
    kwargs.setdefault("days", DAYS)
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("mode", "full")
    return service.backfill(**kwargs)


def pull(number, **overrides):
    """A closed-PR listing entry based on the merged_pr fixture."""
    pr = dict(load_fixture("merged_pr")["pr"])
    pr.update({"number": number, **overrides})
    return pr


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
        # Org repos plus the one closed-PR listing: no per-PR fetch and no
        # merge_group query, because nothing new is going to be stored.
        assert len(github.calls) - calls_before == 2

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
        # The next run retries only the missing pull request. Its queue window
        # starts at that pull request's creation, not at the oldest of three.
        github.add(broken, FakeResponse(payload=load_fixture("closed_pr")["reviews"]))
        github.add(
            runs_window_path(datetime(2026, 10, 2, 9, tzinfo=UTC), NOW),
            FakeResponse(payload={"total_count": 7, "workflow_runs": load_fixture("merge_group_runs")["runs"]}),
        )
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
        GitHub's list cap are not silently dropped. The window starts at the
        oldest pending pull request, not at the retention cutoff: a queue
        entry cannot predate the pull request that entered it."""
        start = datetime.fromisoformat(load_fixture("merged_pr")["pr"]["created_at"].replace("Z", "+00:00"))
        middle = start + (NOW - start) / 2
        top = runs_window_path(start, NOW)
        left = runs_window_path(start, middle)
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
# Hourly incremental pass
# ---------------------------------------------------------------------------
class TestHourly:
    """The timer runs every hour. A full pass is daily; the other hours list
    only what changed, minus a two-hour overlap, and spend no call at all on
    the merge queue when there is nothing to store."""

    def test_auto_is_full_then_incremental(self, service, github):
        first = run_backfill(service, mode="auto")
        assert first["mode"] == "full"
        assert first["since"] == iso(RETENTION_START)

        github.add(pulls_path(NOW - timedelta(hours=2)), FakeResponse(payload=[]))
        second = run_backfill(service, mode="auto", now=NOW + timedelta(hours=1))

        assert second["mode"] == "incremental"
        assert second["since"] == iso(NOW - timedelta(hours=2))
        assert github.count(pulls_path(NOW - timedelta(hours=2))) == 1

    def test_a_full_pass_runs_again_after_a_day(self, service, github):
        run_backfill(service, mode="auto")
        for hour in range(1, 26):
            # coverage before run `hour` is NOW + (hour - 1); the listing
            # floor is that minus the two-hour overlap.
            github.add(pulls_path(NOW + timedelta(hours=hour - 3)), FakeResponse(payload=[]))
            summary = run_backfill(service, mode="auto", now=NOW + timedelta(hours=hour))

        assert summary["mode"] == "full", "24 hours after the last full pass, the daily pass is due again"

    def test_pr_merged_between_runs_appears_next_hour(self, service, github):
        """The regression this section exists for: a pull request merged at
        10:10 was absent until the next day. With the hourly pass it is
        stored on the 11:05 run."""
        run_backfill(service, mode="auto")
        run_at = NOW + timedelta(hours=1)
        merged_at = iso(NOW + timedelta(minutes=10))
        new_pr = pull(
            104,
            title="Merged between two runs",
            created_at=iso(NOW - timedelta(hours=2)),
            updated_at=merged_at,
            merged_at=merged_at,
            closed_at=merged_at,
        )
        github.add(pulls_path(NOW - timedelta(hours=2)), FakeResponse(payload=[new_pr]))
        route_pr(github, load_fixture("merged_pr"), number=104)
        github.add(
            runs_window_path(NOW - timedelta(hours=2), run_at),
            FakeResponse(payload={"total_count": 0, "workflow_runs": []}),
        )

        summary = run_backfill(service, mode="incremental", now=run_at)

        assert summary["prs_stored"] == 1
        row = pr_row(service, 104)
        assert row["merged_at"] == merged_at
        document = service.get_json({"number": ["104"], "repo": [REPO]}, now=run_at)
        assert document["pr_state"] == "stored"
        assert document["freshness"]["last_fetched_at"] == iso(run_at)

    def test_an_idle_hour_stays_under_thirty_api_calls(self, service, github):
        run_backfill(service, mode="auto")
        github.add(pulls_path(NOW - timedelta(hours=2)), FakeResponse(payload=[]))
        calls_before = len(github.calls)

        summary = run_backfill(service, mode="incremental", now=NOW + timedelta(hours=1))

        assert summary["api_calls"] == len(github.calls) - calls_before
        assert summary["api_calls"] <= 30
        # One org listing and one PR listing; no merge_group window.
        assert summary["api_calls"] == 2
        assert sum(1 for call in github.calls if "event=merge_group" in call) == 1

    def test_force_still_lists_the_full_window(self, service, github):
        """--force refetches stored rows, which needs the whole retention
        window; an incremental listing would only see the last hours."""
        run_backfill(service, mode="auto")
        run_at = NOW + timedelta(hours=1)
        github.add(
            runs_window_path(oldest_pending(), run_at),
            FakeResponse(payload={"total_count": 7, "workflow_runs": load_fixture("merge_group_runs")["runs"]}),
        )

        summary = run_backfill(service, mode="auto", force=True, now=run_at)

        assert summary["mode"] == "full"
        assert summary["since"] == iso(run_at - timedelta(days=DAYS))

    def test_stored_pull_requests_in_the_overlap_are_skipped(self, service, github):
        run_backfill(service, mode="auto")
        stored = [load_fixture(name)["pr"] for name in ("merged_pr", "ejected_pr", "closed_pr")]
        github.add(pulls_path(NOW - timedelta(hours=2)), FakeResponse(payload=stored))
        calls_before = len(github.calls)

        summary = run_backfill(service, mode="incremental", now=NOW + timedelta(hours=1))

        assert summary["prs_stored"] == 0
        assert summary["prs_skipped"] == 3
        # No per-PR pages and no merge_group window for already stored rows.
        assert len(github.calls) - calls_before == 2

    def test_a_failed_listing_does_not_advance_coverage(self, service, github):
        run_backfill(service, mode="auto")
        github.add(pulls_path(NOW - timedelta(hours=2)), FakeResponse(500, text="boom"))

        summary = run_backfill(service, mode="incremental", now=NOW + timedelta(hours=1))

        assert summary["ok"] is False
        assert summary["listing_complete"] is False
        assert service._coverage_until() == iso(NOW)

    def test_a_failed_repository_listing_does_not_advance_coverage(self, service, github):
        """An org listing that fails is not an org with no repositories."""
        github.add(REPOS_PATH, FakeResponse(500, text="boom"))

        summary = run_backfill(service, mode="auto")

        assert summary["listing_complete"] is False
        assert summary["ok"] is False
        assert "repository listing failed" in summary["stopped_early"]
        assert service.get_json({}, now=NOW)["coverage_until"] is None


# ---------------------------------------------------------------------------
# pr_backfill_runs: what each pass did
# ---------------------------------------------------------------------------
class TestBackfillRuns:
    def test_each_pass_is_recorded(self, service):
        run_backfill(service, mode="full")

        [run] = rows(service, "SELECT * FROM pr_backfill_runs")
        assert run["ran_at"] == iso(NOW)
        assert run["mode"] == "full"
        assert run["window_from"] == iso(RETENTION_START)
        assert run["window_to"] == iso(NOW)
        assert (run["stored"], run["skipped"], run["failed"]) == (3, 0, 0)
        assert run["api_calls"] == 12
        assert run["rate_limit_remaining"] is None
        assert run["stopped_early"] == ""
        assert run["listing_complete"] == 1
        assert run["ok"] == 1

    def test_a_stopped_pass_does_not_advance_coverage(self, service, github):
        """A pass that stops before it read every repository did not cover
        its window. Coverage stays where it was, so the pull requests it
        missed read "not fetched yet" instead of "missing", and a full pass
        that stopped here is retried on the next hour."""
        limit = {"remaining": "50"}
        original_get = github.get

        def limited(url, **kwargs):
            response = original_get(url, **kwargs)
            response.headers["X-RateLimit-Remaining"] = limit["remaining"]
            return response

        github.get = limited
        summary = run_backfill(service, mode="full")

        [run] = rows(service, "SELECT * FROM pr_backfill_runs")
        assert run["listing_complete"] == 0
        assert run["ok"] == 0
        assert "rate limit" in run["stopped_early"]
        assert summary["ok"] is False
        assert service._coverage_until() is None
        assert service._full_pass_due(NOW + timedelta(hours=1)) is True

    def test_a_repository_filtered_pass_does_not_move_org_coverage(self, service, github, prs):
        """--repo reads one repository. It must not advance org-wide
        coverage or count as the day's full pass."""
        run_backfill(service, mode="full")  # the real org-wide pass
        coverage = service._coverage_until()

        filtered = prs.CIObservePRs(db_file=service.db_file, api=FakeAPI(github), repos=[REPO])
        run_at = NOW + timedelta(hours=6)
        # Route the URL this pass actually asks for, so it reads the listing
        # successfully and the only reason coverage stays put is the filter.
        github.add(pulls_path(run_at - timedelta(days=DAYS)), FakeResponse(payload=[]))
        run_backfill(filtered, mode="full", now=run_at)

        [latest] = rows(service, "SELECT * FROM pr_backfill_runs ORDER BY id DESC LIMIT 1")
        assert latest["listing_complete"] == 0
        assert latest["ok"] == 0
        assert "org-wide coverage did not advance" in latest["stopped_early"]
        assert "the pull request listing failed" not in latest["stopped_early"]
        assert service._coverage_until() == coverage
        assert service._full_pass_due(run_at + timedelta(hours=19)) is True

    def test_an_incremental_pass_is_recorded_with_its_since(self, service, github):
        run_backfill(service, mode="auto")
        github.add(pulls_path(NOW - timedelta(hours=2)), FakeResponse(payload=[]))
        run_backfill(service, mode="incremental", now=NOW + timedelta(hours=1))

        latest = rows(service, "SELECT * FROM pr_backfill_runs ORDER BY id DESC LIMIT 1")[0]
        assert latest["mode"] == "incremental"
        assert latest["window_from"] == iso(NOW - timedelta(hours=2))
        assert latest["window_to"] == iso(NOW + timedelta(hours=1))
        assert latest["ok"] == 1


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------
class TestFreshness:
    def test_never(self, service):
        doc = service.get_json({"days": ["30"]}, now=NOW)

        assert doc["freshness"]["state"] == "never"
        assert doc["last_fetched_at"] is None
        assert doc["coverage_until"] is None
        assert doc["next_run_at"] == iso(NOW.replace(minute=5))
        assert "has not run yet" in doc["freshness"]["message"]

    def test_fresh_and_stale(self, service):
        run_backfill(service, mode="full")

        fresh = service.get_json({"days": ["30"]}, now=NOW + timedelta(minutes=20))
        assert fresh["freshness"]["state"] == "fresh"
        assert fresh["freshness"]["age_seconds"] == 20 * 60
        assert fresh["freshness"]["coverage_until"] == iso(NOW)
        assert fresh["last_fetched_at"] == iso(NOW)
        assert fresh["next_run_at"] == iso(NOW + timedelta(hours=1, minutes=5))
        assert iso(NOW) in fresh["freshness"]["message"]
        assert "20 min ago" in fresh["freshness"]["message"]

        stale = service.get_json({"days": ["30"]}, now=NOW + timedelta(hours=3))
        assert stale["freshness"]["state"] == "stale"
        assert any("stale" in note for note in stale["notes"])

    def test_the_message_binds_to_last_fetched_at(self, service, github):
        """The dashboard text is built from the same field the panel shows,
        so the two cannot disagree about when the backfill last ran."""
        run_backfill(service, mode="full")
        early = service.get_json({}, now=NOW + timedelta(minutes=5))
        assert early["freshness"]["last_fetched_at"] == iso(NOW)
        assert f"last ran at {iso(NOW)}" in early["freshness"]["message"]

        github.add(pulls_path(NOW - timedelta(hours=2)), FakeResponse(payload=[]))
        run_backfill(service, mode="incremental", now=NOW + timedelta(hours=1))
        late = service.get_json({}, now=NOW + timedelta(minutes=65))

        assert late["freshness"]["last_fetched_at"] == iso(NOW + timedelta(hours=1))
        assert late["freshness"]["last_fetched_at"] != early["freshness"]["last_fetched_at"]
        assert f"last ran at {iso(NOW + timedelta(hours=1))}" in late["freshness"]["message"]

    def test_freshness_follows_the_coverage_age_not_the_last_run(self, service):
        """An hourly pass whose listing keeps failing still writes a row, so
        the last-run time stays current while the data does not. The page
        must call that stale, or it reports days-old data as fresh."""
        service._init_db()
        with sqlite3.connect(service.db_file) as conn:
            conn.execute(
                "INSERT INTO pr_backfill_runs (ran_at, mode, window_from, window_to, stored, skipped,"
                " failed, api_calls, rate_limit_remaining, stopped_early, listing_complete, ok)"
                " VALUES (?, 'full', ?, ?, 0, 0, 0, 0, NULL, '', 0, 0)",
                (iso(NOW), iso(NOW - timedelta(days=DAYS)), iso(NOW)),
            )
            conn.execute(
                "INSERT INTO pr_backfill_runs (ran_at, mode, window_from, window_to, stored, skipped,"
                " failed, api_calls, rate_limit_remaining, stopped_early, listing_complete, ok)"
                " VALUES (?, 'full', ?, ?, 3, 0, 0, 12, NULL, '', 1, 1)",
                (iso(NOW - timedelta(hours=6)), iso(NOW - timedelta(days=DAYS)), iso(NOW - timedelta(hours=6))),
            )

        doc = service.get_json({"days": ["30"]}, now=NOW + timedelta(minutes=10))

        assert doc["freshness"]["last_fetched_at"] == iso(NOW)
        assert doc["freshness"]["coverage_until"] == iso(NOW - timedelta(hours=6))
        assert doc["freshness"]["coverage_age_seconds"] >= 6 * 3600
        assert doc["freshness"]["state"] == "stale"

    def test_the_next_run_is_the_next_five_past_the_hour(self, service):
        run_backfill(service, mode="full")

        doc = service.get_json({}, now=NOW + timedelta(hours=2, minutes=7))
        assert doc["next_run_at"] == iso(NOW + timedelta(hours=3, minutes=5))

    def test_a_database_without_the_pass_table_reads_as_never(self, prs, tmp_path):
        """A prs.db written before this change has no pr_backfill_runs. The
        read path says never and must not fail until the next pass."""
        db_file = tmp_path / "prs.db"
        prs.CIObservePRs(db_file=str(db_file))._init_db()
        with sqlite3.connect(db_file) as conn:
            conn.execute("DROP TABLE pr_backfill_runs")

        service = prs.CIObservePRs(db_file=str(db_file), api=None)
        index = service.get_json({"days": ["30"]}, now=NOW)
        one = service.get_json({"number": ["101"]}, now=NOW)

        assert index["freshness"]["state"] == "never"
        assert index["prs"] == []
        assert one["pr_state"] == "missing"
        assert "no GitHub access" in one["pr_state_message"] or "no answer" in one["pr_state_message"]


# ---------------------------------------------------------------------------
# The four miss states
# ---------------------------------------------------------------------------
def route_live_pr(github, number, **overrides):
    payload = {
        "number": number,
        "state": "closed",
        "title": f"Live pull request {number}",
        "user": {"login": "author1"},
        "html_url": f"https://github.com/osac-project/{REPO}/pull/{number}",
        "labels": [{"name": "ok-to-test"}],
        "created_at": "2026-10-01T10:00:00Z",
        "updated_at": "2026-10-20T00:00:00Z",
        "merged_at": None,
        "closed_at": None,
    }
    payload.update(overrides)
    github.add(f"/repos/osac-project/{REPO}/pulls/{number}", FakeResponse(payload=payload))


class TestStates:
    def detail(self, service, number, now, repo=REPO):
        return service.get_json({"number": [str(number)], "repo": [repo]}, now=now)

    def test_open(self, service, github):
        run_backfill(service, mode="full")
        route_live_pr(github, 200, state="open")

        doc = self.detail(service, 200, NOW + timedelta(minutes=20))

        assert doc["pr_state"] == "open"
        assert doc["source"] == "live"
        assert doc["pr"]["state"] == "open"
        assert doc["pr"]["title"] == "Live pull request 200"
        assert "is open" in doc["pr_state_message"]

    def test_not_yet_fetched(self, service, github):
        run_backfill(service, mode="full")
        merged_at = iso(NOW + timedelta(minutes=10))
        route_live_pr(github, 201, state="closed", merged_at=merged_at, closed_at=merged_at)

        doc = self.detail(service, 201, NOW + timedelta(minutes=20))

        assert doc["pr_state"] == "not_yet_fetched"
        assert doc["source"] == "live"
        assert merged_at in doc["pr_state_message"]
        assert doc["next_run_at"] in doc["pr_state_message"]

    def test_a_stopped_pass_makes_its_pull_requests_not_yet_fetched(self, service, github):
        """A stopped pass did not cover its window, so an absent pull request
        inside that window is a delay with a next run, not a gap."""
        limit = {"remaining": "50"}
        original_get = github.get

        def limited(url, **kwargs):
            response = original_get(url, **kwargs)
            response.headers["X-RateLimit-Remaining"] = limit["remaining"]
            return response

        github.get = limited
        run_backfill(service, mode="full")
        ended = "2026-10-02T12:00:00Z"  # inside the window the pass listed
        route_live_pr(github, 202, state="closed", merged_at=ended, closed_at=ended)

        doc = self.detail(service, 202, NOW)

        assert doc["pr_state"] == "not_yet_fetched"
        assert "next hourly run" in doc["pr_state_message"]

    def test_a_failed_live_lookup_is_cached_for_a_few_seconds(self, service, github):
        """Every detail panel refreshes. While GitHub returns 403, a refresh
        must not spend the shared budget again on each panel."""
        run_backfill(service, mode="full")
        path = f"/repos/osac-project/{REPO}/pulls/206"
        github.add(path, FakeResponse(403, text="rate limited"))
        before = github.count(path)

        first = self.detail(service, 206, NOW)
        second = self.detail(service, 206, NOW)

        assert first["pr_state"] == second["pr_state"] == "missing"
        assert "HTTP 403" in first["pr_state_message"]
        assert github.count(path) == before + 1

    def test_missing_without_a_live_answer(self, service):
        run_backfill(service, mode="full")

        doc = self.detail(service, 203, NOW)

        assert doc["pr_state"] == "missing"
        assert doc["source"] == "none"
        assert "no pull request #203" in doc["pr_state_message"]

    def test_a_repository_that_is_not_a_name_is_never_requested(self, service, github):
        """repo comes from the query string. A path segment there would move
        the live lookup, made with the shared token, to another repository."""
        run_backfill(service, mode="full")
        before = len(github.calls)

        doc = self.detail(service, 205, NOW, repo="../../repos/other-org/private-repo")

        assert doc["pr_state"] == "missing"
        assert "not a repository name" in doc["pr_state_message"]
        assert len(github.calls) == before

    def test_outside_window(self, service, github):
        run_backfill(service, mode="full")
        ended = iso(NOW - timedelta(days=DAYS, hours=1))
        route_live_pr(github, 204, state="closed", merged_at=ended, closed_at=ended)

        doc = self.detail(service, 204, NOW)

        assert doc["pr_state"] == "outside_window"
        assert "retention" in doc["pr_state_message"]

    def test_every_document_lists_the_four_states(self, service, github):
        run_backfill(service, mode="full")
        route_live_pr(github, 205, state="open")

        document = self.detail(service, 205, NOW)

        assert [state["state"] for state in document["states"]] == [
            "open",
            "not_yet_fetched",
            "missing",
            "outside_window",
        ]
        assert all(state["message"] for state in document["states"])

    def test_a_stored_pull_request_is_not_a_miss(self, service):
        run_backfill(service, mode="full")

        doc = self.detail(service, 101, NOW)

        assert doc["pr_state"] == "stored"
        assert doc["source"] == "store"
        assert "Stored from the backfill" in doc["pr_state_message"]

    def test_the_live_lookup_is_read_only_and_cached(self, service, github):
        run_backfill(service, mode="full")
        route_live_pr(github, 206, state="open")
        path = f"/repos/osac-project/{REPO}/pulls/206"

        self.detail(service, 206, NOW)
        self.detail(service, 206, NOW)

        assert github.count(path) == 1, "the second read must come from the cache"
        assert rows(service, "SELECT number FROM pr_events WHERE number = 206") == []


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

    def test_the_index_also_answers_the_detail_panels(self, filled):
        """The detail panels query the same endpoint with an empty number.
        The index document must carry their roots and the four states, so
        they render an explanation instead of a missing-root error."""
        doc = filled.get_json({"days": [str(DAYS)]})

        assert doc["pr"] is None
        assert doc["stages"] == []
        assert doc["attempts"] == []
        assert [state["state"] for state in doc["states"]] == ["open", "not_yet_fetched", "missing", "outside_window"]

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
        assert "no stored row in the backfill" in doc["notes"][0]

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
        assert index["freshness"]["state"] == "never"
        assert index["last_fetched_at"] is None
        assert index["next_run_at"]
        assert one["pr"] is None
        assert one["pr_state"] == "not_yet_fetched"
        assert "does not exist yet" in one["notes"][-1]
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
            if selector == "$":
                continue
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


def test_backfill_is_mounted_in_the_exporter_and_scheduled_hourly():
    """The backfill runs inside the exporter container: that is where the
    token, requests and the /data volume live. It runs hourly so a merge
    shows up within the hour; --mode auto keeps one full pass a day."""
    monitoring = Path(__file__).resolve().parents[1]
    quadlet = (monitoring / "quadlet" / "workflow-exporter.container").read_text()
    assert "ci_observe_prs.py:/app/ci_observe_prs.py" in quadlet

    service = (monitoring / "systemd" / "ci-observe-prs.service").read_text()
    assert "podman exec workflow-exporter python3 /app/ci_observe_prs.py --days 60 --mode auto" in service

    timer = (monitoring / "systemd" / "ci-observe-prs.timer").read_text()
    assert "OnCalendar=*-*-* *:05:00" in timer
    assert "RandomizedDelaySec=300" in timer
    assert "Persistent=true" in timer

    setup = (monitoring / "scripts" / "monitoring-setup.sh").read_text()
    assert "systemd/ci-observe-prs.service" in setup
    assert "systemd/ci-observe-prs.timer" in setup
    assert "systemctl --user enable --now ci-observe-prs.timer" in setup
    assert "systemctl --user enable ci-observe-prs.timer" in setup
