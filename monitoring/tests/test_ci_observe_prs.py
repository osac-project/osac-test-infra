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
from typing import TYPE_CHECKING

import pytest
import requests
from helpers import FakeGitHub, FakeResponse, iso

if TYPE_CHECKING:
    from ci_observe_prs import CIObservePRs

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ci_observe_prs"
DASHBOARDS_DIR = Path(__file__).resolve().parents[1] / "config" / "grafana" / "dashboards"
DASHBOARD_FILE = DASHBOARDS_DIR / "ci-observe-pull-requests.json"

NOW = datetime(2026, 10, 15, tzinfo=UTC)
DAYS = 30
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


@pytest.fixture(autouse=True)
def fixed_clock(prs, monkeypatch):
    """Pin the read path to NOW. `get_json` defaults to the wall clock, and a
    test whose window depends on today would expire."""
    original = prs.CIObservePRs.get_json

    def get_json(self, params, now=None):
        return original(self, params, NOW if now is None else now)

    monkeypatch.setattr(prs.CIObservePRs, "get_json", get_json)


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


def seed_jobs(service, entries, pr=101):
    """A workflow-exporter job history next to prs.db, with the columns the
    runs lookup reads. entries are (id, run_id, workflow, event, head_sha,
    created_at, conclusion) tuples; everything else takes a default."""
    db_file = Path(service.db_file).parent / "workflow-exporter.db"
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS jobs ("
            "id INTEGER PRIMARY KEY, repo TEXT, workflow TEXT, display_name TEXT, category TEXT,"
            "branch TEXT, pr_url TEXT, pr_display TEXT, status TEXT, conclusion TEXT, event TEXT,"
            "trigger TEXT, duration_s INTEGER, duration TEXT, actor TEXT, url TEXT, created_at TEXT,"
            "updated_at TEXT, run_number INTEGER, run_attempt INTEGER, failed_step TEXT, steps_json TEXT,"
            "failure_reason TEXT, runner_name TEXT, head_sha TEXT, run_id TEXT)"
        )
        for job_id, run_id, workflow, event, head_sha, created_at, conclusion in entries:
            conn.execute(
                "INSERT INTO jobs (id, repo, workflow, display_name, category, branch, pr_display, status,"
                " conclusion, event, trigger, duration_s, url, created_at, updated_at, run_number,"
                " run_attempt, failed_step, steps_json, failure_reason, runner_name, head_sha, run_id)"
                " VALUES (?, 'osac', ?, ?, 'e2e', 'branch', ?, 'completed', ?, ?, ?, 600,"
                " ?, ?, ?, 1, 1, '', '[]', '', 'runner-1', ?, ?)",
                (
                    job_id,
                    workflow,
                    f"osac / {workflow}",
                    f"#{pr}",
                    conclusion,
                    event,
                    event,
                    f"https://github.com/osac-project/osac/actions/runs/{run_id}",
                    created_at,
                    created_at,
                    head_sha,
                    str(run_id),
                ),
            )


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
        assert row["queue_seconds"] == 34 * 60
        assert row["merged_seconds"] == 60
        assert row["total_seconds"] == 95 * 60

    def test_measured_stages_sum_to_the_total(self, service: CIObservePRs) -> None:
        """The queue ends at the last attempt and the merge starts there, so
        the five stages account for the whole life without overlap."""
        run_backfill(service)
        row = pr_row(service, 101)
        measured = sum(
            row[key]
            for key in ("authoring_seconds", "review_seconds", "waiting_seconds", "queue_seconds", "merged_seconds")
        )
        assert abs(measured - row["total_seconds"]) <= 60

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

    def test_e2e_counts_read_the_exporter_job_history(self, prs, github, tmp_path):
        """Every E2E run of the pull request counts: the pull_request rows, the
        merge-queue rows and the suite runs the decision starts with
        workflow_dispatch, which name the pull request only in their run
        name. A retest is a run beyond the first of its suite; a version is
        a distinct commit its pull-request runs tested."""
        jobs_db = tmp_path / "workflow-exporter.db"
        with sqlite3.connect(jobs_db) as conn:
            conn.execute(
                "CREATE TABLE jobs (id INTEGER PRIMARY KEY, repo TEXT, pr_display TEXT, category TEXT, event TEXT,"
                " workflow TEXT, conclusion TEXT, head_sha TEXT, failure_reason TEXT)"
            )
            conn.executemany(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (1, REPO, "", "e2e", "workflow_dispatch", "E2E suite · vmaas · #101 · aaaaaaa", "failure",
                     "main1", ""),
                    (2, REPO, "", "e2e", "workflow_dispatch", "E2E suite · vmaas · #101 · bbbbbbb", "success",
                     "main2", ""),
                    (3, REPO, "", "e2e", "workflow_dispatch", "E2E suite · caas · #101 · bbbbbbb", "success",
                     "main2", ""),
                    (4, REPO, "#101", "e2e", "merge_group", "E2E vmaas (merge queue)", "success", "merge", ""),
                    (5, REPO, "", "e2e", "workflow_dispatch", "E2E suite · vmaas · #1010 · ccccccc", "success",
                     "main3", ""),
                    (6, REPO, "#101", "e2e", "pull_request", "E2E caas-gate", "failure", "x", "gate"),
                    (7, REPO, "#102", "e2e", "pull_request", "E2E VMaaS Full Install", "success", "ddd", ""),
                ],
            )
        service = prs.CIObservePRs(db_file=str(tmp_path / "prs.db"), api=FakeAPI(github))
        run_backfill(service)
        first = pr_row(service, 101)
        assert (first["e2e_runs"], first["e2e_failures"], first["e2e_retests"], first["e2e_versions"]) == (4, 1, 2, 2)
        assert first["retest_count"] == 2
        assert (pr_row(service, 102)["e2e_runs"], pr_row(service, 102)["retest_count"]) == (1, 0)

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

    def test_outside_window(self, prs, service, github):
        run_backfill(service, mode="full")
        ended = iso(NOW - timedelta(days=prs.PRS_HISTORY_DAYS, hours=1))
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
        # One failed attempt and one cancelled one: the cancelled entry was
        # re-queued, the pull request was not ejected for it.
        assert first["ejected_attempt_count"] == 1
        assert first["requeued_attempt_count"] == 1
        assert first["age_display"]
        assert first["option_label"].startswith("#103 · merged · ")
        assert "derived, not recorded by GitHub" in doc["derivation"]["ejections"]

    def test_index_filters_by_repository(self, filled):
        with sqlite3.connect(filled.db_file) as conn:
            conn.execute(
                "INSERT INTO pr_events (repo, number, title, final_stage, opened_at, fetched_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("osac-test-infra", 9, "Other repo", "merged", "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z"),
            )

        doc = filled.get_json({"days": [str(DAYS)], "repo": ["osac-test-infra"]})

        assert [pr["number"] for pr in doc["prs"]] == [9]

    def test_every_index_row_has_a_picker_label(self, filled):
        doc = filled.get_json({"days": [str(DAYS)]})

        for pr in doc["prs"]:
            assert pr["option_label"] == f"#{pr['number']} · {pr['final_stage']} · {pr['title']}"

    def test_each_stage_carries_a_display_duration(self, filled, prs):
        doc = filled.get_json({"number": ["103"], "repo": [REPO], "days": [str(DAYS)]})

        for stage in doc["stages"]:
            assert stage["time_display"] == prs.human_duration(stage["seconds"])
            assert stage["time_display"], stage["stage"]

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
        assert funnel["queue"]["median_seconds"] == 2520
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
        assert doc["attempts"][0]["started_display"] == "03 Oct 09:00"
        assert doc["attempts"][0]["why_url"] == doc["attempts"][0]["comment_url"]
        assert doc["attempts"][1]["why_url"] == doc["attempts"][1]["run_url"]
        assert [attempt["queue_outcome"] for attempt in doc["attempts"]] == ["ejected", "re-queued", "passed"]
        assert (doc["pr"]["ejected_attempt_count"], doc["pr"]["requeued_attempt_count"]) == (1, 1)
        assert any("derived" in note for note in doc["notes"])

    def test_the_summary_and_chart_name_measured_stages(self, filled: CIObservePRs) -> None:
        doc = filled.get_json({"number": ["101"], "repo": [REPO], "days": [str(DAYS)]})

        assert doc["summary"] == [
            {"total": "1h 35m", "authoring": "20m", "review": "30m", "waiting": "10m", "queue": "34m",
             "total_s": 5700, "authoring_s": 1200, "review_s": 1800, "waiting_s": 600, "queue_s": 2040}
        ]
        assert doc["chart"] == [
            {
                "category": "PR #101",
                "authoring": 1200,
                "review": 1800,
                "waiting": 600,
                "queue": 2040,
                "merged": 60,
            }
        ]

    def test_the_identity_line_links_and_dates(self, filled: CIObservePRs) -> None:
        pr = filled.get_json({"number": ["101"], "repo": [REPO], "days": [str(DAYS)]})["pr"]

        assert pr["author_url"] == "https://github.com/author1"
        assert pr["repo_url"] == "https://github.com/osac-project/osac"
        assert pr["dates_display"] == "Opened 01 Oct 10:00 · Merged 01 Oct 11:35"

    def test_a_bot_login_goes_into_its_link_unescaped(self, filled: CIObservePRs) -> None:
        """Grafana escapes a data link's "[" and "]" itself; an escaped login
        would arrive as dependabot%255Bbot%255D."""
        with sqlite3.connect(filled.db_file) as conn:
            conn.execute("UPDATE pr_events SET author = ? WHERE number = 101", ("dependabot[bot]",))

        pr = filled.get_json({"number": ["101"], "repo": [REPO], "days": [str(DAYS)]})["pr"]

        assert pr["author_url"] == "https://github.com/dependabot[bot]"

    def test_missing_stages_say_not_measured_and_are_absent_from_the_chart(self, filled: CIObservePRs) -> None:
        doc = filled.get_json({"number": ["102"]})

        assert doc["summary"][0]["review"] == "not measured"
        assert doc["summary"][0]["queue"] == "not measured"
        assert (doc["summary"][0]["review_s"], doc["summary"][0]["queue_s"]) == (None, None)
        assert doc["chart"] == [
            {
                "category": "PR #102",
                "authoring": 1800,
                "review": None,
                "waiting": None,
                "queue": None,
                "merged": None,
            }
        ]

    def test_a_normal_stored_pull_request_has_no_status_message(self, filled: CIObservePRs) -> None:
        seed_jobs(filled, [])
        doc = filled.get_json({"number": ["101"], "repo": [REPO], "days": [str(DAYS)]})

        assert doc["status_message"] == ""

    def test_a_missing_pull_request_status_says_why(self, filled: CIObservePRs) -> None:
        doc = filled.get_json({"number": ["999"]})

        assert doc["pr"] is None
        assert doc["status_message"]
        assert doc["status_message"] == doc["pr_state_message"]

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
        assert filled.get_json({"days": ["999"]})["days"] == 90
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
        assert "does not exist yet" in one["status_message"]
        assert not db_file.exists(), "the read path must not create prs.db"

    def test_no_token_or_header_leaks(self, filled):
        body = json.dumps([filled.get_json({}), filled.get_json({"number": ["101"]})])
        assert "PRIVATE_GITHUB_TOKEN" not in body
        assert "Authorization" not in body


# ---------------------------------------------------------------------------
# The pull request's runs
# ---------------------------------------------------------------------------
class TestRuns:
    def test_every_run_is_listed_and_each_commit_is_a_version(self, service):
        run_backfill(service)
        seed_jobs(
            service,
            [
                (1, 9001, "E2E VMaaS Full Install", "pull_request", "a" * 40, "2026-10-01T10:00:00Z", "success"),
                (2, 9002, "E2E vmaas (pull requests)", "pull_request", "b" * 40, "2026-10-02T10:00:00Z", "failure"),
                (3, 9003, "E2E vmaas (merge queue)", "merge_group", "b" * 40, "2026-10-03T10:00:00Z", "success"),
                (4, 9004, "E2E vmaas-gate", "pull_request", "c" * 40, "2026-10-04T10:00:00Z", "failure"),
            ],
        )
        seed_jobs(
            service,
            [(5, 9005, "E2E VMaaS Full Install", "pull_request", "d" * 40, "2026-10-01T10:00:00Z", "success")],
            pr=102,
        )
        doc = service.get_json({"number": ["101"]})

        assert [run["run_id"] for run in doc["runs"]] == ["9003", "9002", "9001"]
        assert [run["version"] for run in doc["runs"]] == ["", 2, 1]
        assert [run["suite"] for run in doc["runs"]] == ["VMaaS", "VMaaS", "VMaaS"]
        assert [run["event"] for run in doc["runs"]] == ["merge_group", "pull_request", "pull_request"]
        # A merge-queue run tests a temporary merge commit, not the PR head:
        # it is not a code version and has no commit link.
        assert doc["runs"][0]["head"] == ""
        assert doc["runs"][0]["commit_url"] == ""
        # The gate row and another pull request's row are not e2e results.
        assert all(run["run_id"] != "9004" for run in doc["runs"])
        assert doc["runs"][0]["recorded"] == ""
        assert doc["runs"][0]["run_url"] == ""

    def test_a_failed_run_names_its_exact_step_and_area(self, service: CIObservePRs) -> None:
        run_backfill(service)
        seed_jobs(
            service,
            [(2, 9002, "E2E vmaas (pull requests)", "pull_request", "b" * 40, "2026-10-02T10:00:00Z", "failure")],
        )
        with sqlite3.connect(Path(service.db_file).parent / "workflow-exporter.db") as conn:
            conn.execute(
                "UPDATE jobs SET failed_step = ?, failure_reason = ? WHERE id = 2",
                (
                    "vmaas / e2e-vmaas-full-install / e2e → Install OSAC; "
                    "vmaas / e2e-vmaas-full-install / test → Run E2E tests",
                    "test",
                ),
            )
        doc = service.get_json({"number": ["101"]})

        # The stored area is "test", so the pytest step wins over the
        # earlier install step even though install is the first entry.
        assert doc["runs"][0]["failed_step_name"] == "Run E2E tests"
        assert doc["runs"][0]["failure_area"] == "test"
        assert doc["runs"][0]["code_display"] == "v1 · " + "b" * 12
        assert doc["runs"][0]["created_display"] == "02 Oct 10:00"
        assert doc["runs"][0]["status_display"] == "failure"
        assert doc["runs"][0]["run_link"] == "https://github.com/osac-project/osac/actions/runs/9002"

        # A non-test area keeps the first failed step.
        with sqlite3.connect(Path(service.db_file).parent / "workflow-exporter.db") as conn:
            conn.execute("UPDATE jobs SET failure_reason = 'setup' WHERE id = 2")
        doc = service.get_json({"number": ["101"]})
        assert doc["runs"][0]["failed_step_name"] == "Install OSAC"

    def test_a_passed_run_has_no_failed_step(self, service: CIObservePRs) -> None:
        run_backfill(service)
        seed_jobs(
            service,
            [(1, 9001, "E2E VMaaS Full Install", "pull_request", "a" * 40, "2026-10-01T10:00:00Z", "success")],
        )
        doc = service.get_json({"number": ["101"]})

        assert doc["runs"][0]["failed_step_name"] == ""
        assert doc["runs"][0]["failure_area"] == ""

    def test_a_missing_duration_is_empty_not_zero(self, service):
        run_backfill(service)
        seed_jobs(
            service,
            [(1, 9001, "E2E VMaaS Full Install", "pull_request", "a" * 40, "2026-10-01T10:00:00Z", "success")],
        )
        with sqlite3.connect(Path(service.db_file).parent / "workflow-exporter.db") as conn:
            conn.execute("UPDATE jobs SET duration_s = NULL WHERE id = 1")
        doc = service.get_json({"number": ["101"]})
        assert doc["runs"][0]["duration_s"] is None
        assert doc["runs"][0]["duration_display"] == ""

    def test_a_pull_request_without_runs_says_why(self, service):
        run_backfill(service)
        seed_jobs(service, [])
        doc = service.get_json({"number": ["102"]})
        assert doc["runs"] == []
        assert "no E2E run" in doc["notes"][-1]

    def test_an_open_pull_request_is_read_from_github_with_stages_and_queue_runs(self, service, github):
        run_backfill(service)
        fixture = load_fixture("merged_pr")
        open_pr = dict(fixture["pr"])
        open_pr.update({"number": 777, "state": "open", "merged_at": None, "closed_at": None})
        github.add(f"/repos/osac-project/{REPO}/pulls/777", FakeResponse(payload=open_pr))
        route_pr(github, {**fixture, "pr": open_pr}, number=777)
        seed_jobs(
            service,
            [
                (11, 9101, "E2E vmaas (pull requests)", "pull_request", "a" * 40, "2026-10-05T10:00:00Z", "success"),
                (12, 9102, "E2E vmaas (merge queue)", "merge_group", "b" * 40, "2026-10-06T11:00:00Z", "failure"),
                (13, 9103, "E2E caas (merge queue)", "merge_group", "b" * 40, "2026-10-06T11:05:00Z", "success"),
            ],
            pr=777,
        )

        doc = service.get_json({"number": ["777"], "repo": [REPO]})

        assert doc["source"] == "live", (doc.get("pr_state_message"), doc.get("notes"))
        assert doc["pr_state"] == "open"
        assert doc["pr"]["state"] == "open"
        assert doc["pr"]["merged_at"] is None
        # The review history produces the same stages the backfill stores.
        assert [stage["stage"] for stage in doc["stages"]] == [
            "authoring", "review", "waiting", "queue", "merged"
        ]
        assert doc["stages"][1]["state"] == "measured"
        assert doc["pr"]["queued_at"] == "2026-10-06T11:00:00Z"
        assert doc["pr"]["final_stage"] == "open in queue"
        # Queue time runs to the live as-of moment while the pull request is
        # still queued. The stage, the stat and the bar all end there.
        assert doc["pr"]["as_of"] == "2026-10-15T00:00:00Z"
        assert doc["stages"][3]["state"] == "measured"
        assert doc["stages"][3]["end_at"] == "2026-10-15T00:00:00Z"
        assert doc["stages"][3]["seconds"] > 0
        assert doc["summary"][0]["queue"] == doc["stages"][3]["time_display"]
        assert doc["chart"][0]["queue"] == round(doc["stages"][3]["seconds"])
        assert doc["stages"][4]["state"] == "not_reached"
        assert len(doc["runs"]) == 3
        # The queue history comes from the stored merge_group runs. The two
        # workflows of one queue entry share a branch and commit, so they are
        # one attempt; the failed workflow marks it ejected.
        assert len(doc["attempts"]) == 1
        assert doc["attempts"][0]["run_ids"] == ["9102", "9103"]
        assert doc["attempts"][0]["run_count"] == 2
        assert doc["attempts"][0]["run_id"] == 9102
        assert doc["attempts"][0]["ejected"] is True
        assert doc["attempts"][0]["ejection_kind"] == "check_failed"

    def test_an_approved_pull_request_not_in_the_queue_ends_waiting_at_as_of(
        self, service: CIObservePRs, github: FakeGitHub
    ) -> None:
        run_backfill(service)
        fixture = load_fixture("merged_pr")
        open_pr = dict(fixture["pr"])
        open_pr.update({"number": 778, "state": "open", "merged_at": None, "closed_at": None})
        github.add(f"/repos/osac-project/{REPO}/pulls/778", FakeResponse(payload=open_pr))
        route_pr(github, {**fixture, "pr": open_pr}, number=778)
        seed_jobs(
            service,
            [(21, 9201, "E2E vmaas (pull requests)", "pull_request", "a" * 40, "2026-10-05T10:00:00Z", "success")],
            pr=778,
        )

        doc = service.get_json({"number": ["778"], "repo": [REPO]})

        waiting = next(stage for stage in doc["stages"] if stage["stage"] == "waiting")
        queue = next(stage for stage in doc["stages"] if stage["stage"] == "queue")
        assert doc["pr"]["as_of"] == "2026-10-15T00:00:00Z"
        assert waiting["state"] == "measured"
        assert waiting["end_at"] == "2026-10-15T00:00:00Z"
        assert waiting["seconds"] > 0
        assert queue["state"] == "not_reached"
        assert doc["summary"][0]["waiting"] == waiting["time_display"]
        assert doc["chart"][0]["waiting"] == round(waiting["seconds"])

    def test_an_unreadable_review_history_does_not_invent_stages(self, service, github):
        run_backfill(service)
        fixture = load_fixture("merged_pr")
        open_pr = dict(fixture["pr"])
        open_pr.update({"number": 888, "state": "open", "merged_at": None, "closed_at": None})
        # The pull request itself is readable; its timeline is not routed.
        github.add(f"/repos/osac-project/{REPO}/pulls/888", FakeResponse(payload=open_pr))

        doc = service.get_json({"number": ["888"], "repo": [REPO]})

        assert doc["source"] == "live"
        assert doc["pr"]["state"] == "open"
        # An unreadable history is missing, not "never reviewed".
        assert doc["stages"] == []
        assert any("review history could not be read" in note for note in doc["notes"])
        assert "review history could not be read" in doc["status_message"]

    def test_a_live_merged_pull_request_measures_its_merge_segment(
        self, service: CIObservePRs, github: FakeGitHub
    ) -> None:
        run_backfill(service)
        fixture = load_fixture("merged_pr")
        merged_pr = dict(fixture["pr"])
        merged_pr.update({"number": 779, "state": "closed", "created_at": "2026-10-06T10:00:00Z"})
        merged_pr.update({"merged_at": "2026-10-06T12:00:00Z", "closed_at": "2026-10-06T12:00:00Z"})
        github.add(f"/repos/osac-project/{REPO}/pulls/779", FakeResponse(payload=merged_pr))
        route_pr(github, {**fixture, "pr": merged_pr}, number=779)
        seed_jobs(
            service,
            [(31, 9301, "E2E vmaas (merge queue)", "merge_group", "b" * 40, "2026-10-06T11:00:00Z", "success")],
            pr=779,
        )

        doc = service.get_json({"number": ["779"], "repo": [REPO]})

        merged = next(stage for stage in doc["stages"] if stage["stage"] == "merged")
        assert doc["pr"]["state"] == "merged"
        assert doc["pr"]["merged_seconds"] == 3600
        assert merged["state"] == "measured"
        assert merged["end_at"] == "2026-10-06T12:00:00Z"
        assert doc["chart"][0]["merged"] == 3600


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

    def test_a_run_links_to_its_own_suite_capture_or_unknown(self, base_url, service):
        """One pull-request-flow run holds a capture per suite, and a missing
        capture is only a proven absence when the registry can judge it."""
        run_backfill(service)
        seed_jobs(
            service,
            [
                (1, 9201, "E2E VMaaS Full Install", "pull_request", "a" * 40, "2026-10-01T10:00:00Z", "success"),
                (2, 9202, "E2E VMaaS Full Install", "pull_request", "b" * 40, "2026-10-02T10:00:00Z", "failure"),
                (3, 9203, "E2E vmaas (pull requests)", "pull_request", "c" * 40, "2026-10-03T10:00:00Z", "success"),
                (4, 9203, "E2E caas (pull requests)", "pull_request", "c" * 40, "2026-10-03T10:00:00Z", "success"),
            ],
        )

        class FakeRuns:
            def capture_ids_by_run(self):
                return {
                    "9201": ["vmaas-pr101-9201-a1"],
                    "9203": ["caas-pr101-9203-a1", "vmaas-pr101-9203-a1"],
                }

            @staticmethod
            def parse_capture_id(capture_id):
                suite = capture_id.split("-pr")[0].split("-main")[0]
                return {"suite": suite, "run_attempt": capture_id.rsplit("-a", 1)[1]}

            def verdict(self, run, now=None):
                if run["capture_ids"]:
                    return "capture", ""
                return "unknown", "the capture registry has no complete answer yet"

        service._runs = FakeRuns()
        status, _, body = self.get(f"{base_url}/api/ci-observe/prs?number=101&repo=osac")

        assert status == 200
        runs = json.loads(body)["runs"]
        by_run = {run["run_id"]: run for run in runs}
        assert by_run["9201"]["capture_id"] == "vmaas-pr101-9201-a1"
        assert by_run["9201"]["recorded"] == "yes"
        assert "var-capture_id=vmaas-pr101-9201-a1" in by_run["9201"]["run_url"]
        # No capture and a registry that cannot judge: unknown, not "No".
        assert by_run["9202"]["recorded"] == "unknown"
        assert by_run["9202"]["recorded_reason"]
        assert by_run["9202"]["run_url"] == ""
        # Two suites of one run each link to their own capture.
        vmaas_row = next(run for run in runs if run["run_id"] == "9203" and run["suite_key"] == "vmaas")
        caas_row = next(run for run in runs if run["run_id"] == "9203" and run["suite_key"] == "caas")
        assert vmaas_row["capture_id"] == "vmaas-pr101-9203-a1"
        assert caas_row["capture_id"] == "caas-pr101-9203-a1"


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
    seed_jobs(
        service,
        [
            (1, 9001, "E2E VMaaS Full Install", "pull_request", "a" * 40, "2026-10-01T10:00:00Z", "success"),
        ],
    )
    dashboard = json.loads(DASHBOARD_FILE.read_text())
    documents = {
        "index": service.get_json({"days": [str(DAYS)]}),
        "pr": service.get_json({"number": ["101"], "repo": [REPO], "days": [str(DAYS)]}),
    }

    checked = 0
    for panel in iter_panels(dashboard["panels"]):
        for target in panel.get("targets", []):
            doc = documents["pr"] if "number=" in target["url"] else documents["index"]
            selector = target["root_selector"]
            if selector == "$":
                sample = doc
            else:
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
    assert checked >= 9, "the page has an identity row, five tiles, a chart and three tables"


def test_backfill_is_mounted_in_the_exporter_and_scheduled_hourly():
    """The backfill runs inside the exporter container: that is where the
    token, requests and the /data volume live. It runs hourly so a merge
    shows up within the hour; --mode auto keeps one full pass a day."""
    monitoring = Path(__file__).resolve().parents[1]
    quadlet = (monitoring / "quadlet" / "workflow-exporter.container").read_text()
    assert "ci_observe_prs.py:/app/ci_observe_prs.py" in quadlet

    service = (monitoring / "systemd" / "ci-observe-prs.service").read_text()
    assert "podman exec workflow-exporter python3 /app/ci_observe_prs.py --days 90 --mode auto" in service

    timer = (monitoring / "systemd" / "ci-observe-prs.timer").read_text()
    assert "OnCalendar=*-*-* *:05:00" in timer
    assert "RandomizedDelaySec=300" in timer
    assert "Persistent=true" in timer

    setup = (monitoring / "scripts" / "monitoring-setup.sh").read_text()
    assert "systemd/ci-observe-prs.service" in setup
    assert "systemd/ci-observe-prs.timer" in setup
    assert "systemctl --user enable --now ci-observe-prs.timer" in setup
    assert "systemctl --user enable ci-observe-prs.timer" in setup


# ---------------------------------------------------------------------------
# The second record format: rebases, review rounds, readiness, E2E counts,
# and the window's headline numbers
# ---------------------------------------------------------------------------
class TestRecordFormat:
    def test_rebases_reviews_and_readiness_are_stored(self, prs, github, service):
        fixture = load_fixture("merged_pr")
        timeline = [
            *fixture["timeline"],
            {"event": "head_ref_force_pushed", "created_at": "2026-10-01T10:30:00Z"},
            {"event": "head_ref_force_pushed", "created_at": "2026-10-01T10:40:00Z"},
            {"event": "ready_for_review", "created_at": "2026-10-01T10:10:00Z"},
        ]
        reviews = [
            *fixture["reviews"],
            {"state": "CHANGES_REQUESTED", "user": {"login": "reviewer2", "type": "User"},
             "submitted_at": "2026-10-01T10:25:00Z"},
        ]
        github.add(f"/repos/osac-project/{REPO}/issues/101/timeline?per_page=100", FakeResponse(payload=timeline))
        github.add(f"/repos/osac-project/{REPO}/pulls/101/reviews?per_page=100", FakeResponse(payload=reviews))
        run_backfill(service)
        row = pr_row(service, 101)
        assert row["force_push_count"] == 2
        assert row["changes_requested_count"] == 1
        assert row["human_review_count"] == 3
        assert row["ready_at"] == "2026-10-01T10:10:00Z"
        assert row["review_wait_seconds"] == 600, "ready at 10:10, first human review at 10:20"
        assert row["record_version"] == prs.RECORD_VERSION

    def test_a_pull_request_that_was_never_a_draft_is_ready_when_opened(self, service):
        run_backfill(service)
        row = pr_row(service, 101)
        assert row["ready_at"] == row["opened_at"]
        assert row["force_push_count"] == 0

    def test_a_row_of_the_older_format_is_refetched_a_bounded_number_per_pass(self, prs, service, github,
                                                                               monkeypatch):
        # A pass that refetches one pull request asks for the merge-group
        # runs from that pull request's creation.
        for name in ("merged_pr", "ejected_pr", "closed_pr"):
            created = datetime.fromisoformat(load_fixture(name)["pr"]["created_at"].replace("Z", "+00:00"))
            github.add(runs_window_path(created, NOW), FakeResponse(
                payload={"total_count": 7, "workflow_runs": load_fixture("merge_group_runs")["runs"]}))
        run_backfill(service)
        with sqlite3.connect(service.db_file) as conn:
            conn.execute("UPDATE pr_events SET record_version = NULL, force_push_count = NULL")
        monkeypatch.setattr(prs, "LEGACY_REFRESH_PER_PASS", 1)
        summary = run_backfill(service)
        assert summary["prs_stored"] == 1
        assert summary["prs_skipped"] == 2
        summary = run_backfill(service)
        assert summary["prs_stored"] == 1
        upgraded = rows(service, "SELECT number FROM pr_events WHERE record_version = ?", (prs.RECORD_VERSION,))
        assert len(upgraded) == 2


class TestIndexNumbers:
    def test_the_index_carries_kpis_and_a_weekly_trend(self, service):
        run_backfill(service)
        doc = service.get_json({"days": ["30"]})
        kpis = doc["kpis"][0]
        assert kpis["prs"] == 3
        assert kpis["merged"] == 2
        assert kpis["closed"] == 1
        assert kpis["lead_median_s"] is not None
        assert kpis["queued_share"] is not None
        assert kpis["measured_prs"] == 2
        assert doc["weekly"], "the merged pull requests fall in at least one week"
        assert all(week["week"] <= NOW.strftime("%Y-%m-%d") for week in doc["weekly"])
        assert sum(week["merged"] for week in doc["weekly"]) == 2
        assert all(row["detail_url"].startswith("/d/ci-observe-pull-requests/") for row in doc["prs"])

    def test_an_empty_window_measures_nothing(self, prs):
        kpis = prs._kpis([], {})[0]
        assert kpis["merged"] == 0
        assert kpis["lead_median_s"] is None
        assert kpis["rebased_share"] is None
        assert kpis["e2e_retests_mean"] is None
        assert prs._weekly([]) == []

    def test_the_e2e_numbers_describe_the_pull_requests_that_ran_e2e(self, prs):
        """Most pull requests run no suite: a median over all of them is 0."""
        def merged(number, runs, retests, failures):
            return {"repo": "osac", "number": number, "state": "merged", "total_seconds": 3600, "record_version": 2,
                    "e2e_runs": runs, "e2e_retests": retests, "e2e_failures": failures,
                    "merged_at": "2026-10-05T10:00:00Z"}

        rows_ = [merged(1, 0, 0, 0), merged(2, 0, 0, 0), merged(3, 0, 0, 0), merged(4, 3, 0, 0),
                 merged(5, 9, 6, 2), merged(6, None, None, None)]
        kpis = prs._kpis(rows_, {})[0]

        assert kpis["e2e_ran_share"] == 0.4
        assert kpis["e2e_runs_median"] == 6
        assert kpis["e2e_retests_median"] == 3
        assert kpis["e2e_failed_share"] == 0.5

    def test_kpis_use_measured_rows_only_for_the_new_counts(self, prs):
        rows_ = [
            {"repo": "osac", "number": 1, "state": "merged", "total_seconds": 3600, "record_version": 2,
             "force_push_count": 2, "e2e_runs": 3, "e2e_retests": 1, "e2e_failures": 1,
             "changes_requested_count": 0, "merged_at": "2026-10-05T10:00:00Z"},
            {"repo": "osac", "number": 2, "state": "merged", "total_seconds": 7200, "record_version": None,
             "force_push_count": None, "merged_at": "2026-10-06T10:00:00Z"},
        ]
        attempts_ = {("osac", 1): [{"ejected": 1, "conclusion": "failure"}, {"ejected": 1, "conclusion": "cancelled"},
                                   {"ejected": 0, "conclusion": "success"}]}
        kpis = prs._kpis(rows_, attempts_)[0]
        assert kpis["rebased_share"] == 1.0
        assert kpis["force_pushes_mean"] == 2.0
        assert kpis["e2e_retests_mean"] == 1.0
        assert kpis["e2e_failed_share"] == 1.0
        assert kpis["ejected_share"] == 1.0
        assert kpis["ejections"] == 1
        assert kpis["requeues"] == 1
        assert kpis["queued_share"] == 0.5
        assert kpis["measured_prs"] == 1
        weekly = prs._weekly(rows_)
        assert [week["week"] for week in weekly] == ["2026-10-05"]
        assert weekly[0]["merged"] == 2


@pytest.mark.parametrize(
    ("attempt", "outcome"),
    [
        ({"ejected": 0, "conclusion": "success"}, "passed"),
        ({"ejected": 1, "conclusion": "failure", "ejection_kind": "check_failed"}, "ejected"),
        ({"ejected": 1, "conclusion": "timed_out", "ejection_kind": "timed_out"}, "ejected"),
        ({"ejected": 1, "conclusion": "startup_failure"}, "ejected"),
        # Every entry behind a failing one is cancelled and queued again.
        ({"ejected": 1, "conclusion": "cancelled", "ejection_kind": "cancelled",
          "ejection_reason": "merge_group entry (run 5102) concluded cancelled"}, "requeued"),
        # GitHub cancels an entry that ran past the queue's time limit.
        ({"ejected": 1, "conclusion": "cancelled", "ejection_kind": "cancelled",
          "ejection_reason": "removed from the merge queue: the required checks timed out"}, "ejected"),
        # A passed entry that a later attempt replaced.
        ({"ejected": 1, "conclusion": "success", "ejection_kind": "unknown"}, "requeued"),
    ],
)
def test_queue_outcome(prs, attempt, outcome):
    assert prs.queue_outcome(attempt) == outcome


def test_a_pull_request_suite_run_appears_with_the_commit_it_tested(prs, service):
    """The decision starts a pull request's suite with workflow_dispatch from
    the default branch: the row names the pull request and the commit only
    in its run name."""
    run_backfill(service)
    seed_jobs(service, [
        (900, 900, "E2E suite · vmaas · #101 · 0123456789abcdef0123456789abcdef01234567", "workflow_dispatch",
         "mainsha", iso(NOW - timedelta(days=2)), "success"),
    ], pr="")
    doc = service.get_json({"number": ["101"], "repo": [REPO], "days": ["30"]})
    assert [run["run_id"] for run in doc["runs"]] == ["900"]
    assert doc["runs"][0]["head"] == "0123456789ab"
    assert doc["runs"][0]["code_display"].startswith("v1 · 0123456789ab")


class TestE2EHistory:
    """A pull request's E2E counts come from the exporter's 30 days of jobs
    and the 90-day run archive in trends.db, and say "not measured" when
    the stores start after the pull request was opened."""

    def archive(self, service, rows):
        trends_db = Path(service.db_file).parent / "trends.db"
        with sqlite3.connect(trends_db) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS suite_runs (id INTEGER PRIMARY KEY, run_id TEXT, repo TEXT,"
                " workflow TEXT, conclusion TEXT, event TEXT, created_at TEXT, pr_display TEXT, head_sha TEXT,"
                " failure_reason TEXT)"
            )
            conn.executemany(
                "INSERT INTO suite_runs (id, run_id, repo, workflow, conclusion, event, created_at, pr_display,"
                " head_sha, failure_reason) VALUES (?, ?, 'osac', ?, ?, ?, ?, '#101', ?, '')",
                rows,
            )

    def test_runs_older_than_the_exporter_window_come_from_the_archive(self, service):
        opened = iso(NOW - timedelta(days=50))
        seed_jobs(service, [
            (1, 1, "E2E VMaaS Full Install", "workflow_dispatch", "a" * 40, iso(NOW - timedelta(days=20)),
             "success"),
        ])
        self.archive(service, [
            (1, "1", "E2E VMaaS Full Install", "success", "workflow_dispatch", iso(NOW - timedelta(days=20)), "a" * 40),
            (2, "2", "E2E VMaaS Full Install", "failure", "workflow_dispatch", iso(NOW - timedelta(days=45)), "b" * 40),
            (3, "3", "E2E CaaS Full Install", "success", "workflow_dispatch", iso(NOW - timedelta(days=60)), "b" * 40),
        ])
        stats = service._e2e_stats("osac", 101, opened_at=opened)
        assert stats == {"runs": 3, "failures": 1, "retests": 1, "versions": 2}, "a job in both stores counts once"

    def test_a_pull_request_opened_before_the_stores_begin_is_not_measured(self, service):
        seed_jobs(service, [
            (1, 1, "E2E VMaaS Full Install", "workflow_dispatch", "a" * 40, iso(NOW - timedelta(days=20)),
             "success"),
        ])
        stats = service._e2e_stats("osac", 101, opened_at=iso(NOW - timedelta(days=40)))
        assert stats == {"runs": None, "failures": None, "retests": None, "versions": None}

    def test_no_store_is_not_measured(self, service):
        assert service._e2e_stats("osac", 101)["runs"] is None

    def test_only_the_three_suites_count(self, service):
        seed_jobs(service, [
            (1, 1, "E2E VMaaS Full Install", "workflow_dispatch", "a" * 40, iso(NOW - timedelta(days=2)), "success"),
            (2, 2, "E2E VMaaS Netris Full Install", "workflow_dispatch", "a" * 40, iso(NOW - timedelta(days=2)),
             "failure"),
        ])
        stats = service._e2e_stats("osac", 101)
        assert (stats["runs"], stats["failures"]) == (1, 0), "Netris is another scenario"


def test_the_overview_asks_for_both_repositories(service):
    run_backfill(service)
    both = service.get_json({"days": ["30"], "repo": ["all"]})
    assert both["kpis"] == service.get_json({"days": ["30"]})["kpis"]
    assert both["kpis"][0]["prs"] == 3
