"""GitHub API access of workflow-exporter.py: repos, PRs, runs, jobs, backfill."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
import requests

from helpers import FakeResponse, iso, make_gh_job, make_job, make_run, make_step


@pytest.fixture
def R(wfe):
    """Builds a route under /repos/<org>/."""
    return lambda path: f"/repos/{wfe.ORG}/{path}"


def rows(exporter, sql, *args):
    with exporter._db() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def runs_page(*runs, total=None, **kw):
    return FakeResponse(payload={"workflow_runs": list(runs), "total_count": total or len(runs)}, **kw)


def test_get_updates_rate_limit(exporter, wfe, github):
    github.add("/x", FakeResponse(headers={"X-RateLimit-Remaining": "42"}))
    exporter._get(f"{wfe.API_URL}/x")
    assert wfe.api_remaining.labels(org=wfe.ORG)._value.get() == 42


def test_get_repos_paginates_and_caches(exporter, wfe, github):
    first = f"/orgs/{wfe.ORG}/repos?per_page=100&type=all"
    github.add(first, FakeResponse(payload=[{"name": "a"}, {"name": "old", "archived": True}],
                                   next_url=f"{wfe.API_URL}/page2"))
    github.add("/page2", FakeResponse(payload=[{"name": "b"}]))
    assert exporter.get_repos() == ["a", "b"]
    assert exporter.get_repos() == ["a", "b"]
    assert github.count(first) == 1


def test_get_repos_failure_keeps_cache(exporter, wfe, github):
    route = f"/orgs/{wfe.ORG}/repos?per_page=100&type=all"
    # With a previous answer cached, a failed listing keeps it.
    exporter._repos_cache = ["osac"]
    exporter._repos_cache_ts = 1e18
    github.add(route, FakeResponse(500, text="boom"))
    assert exporter.get_repos() == ["osac"]
    # With nothing cached, a failed listing refuses: an empty answer here
    # would be cached as "no repos" for 30 minutes of fabricated calm.
    exporter._repos_cache = []
    with pytest.raises(RuntimeError, match="refuses to publish empty repos"):
        exporter.get_repos()


def test_get_active_repos(exporter, wfe, github, R):
    exporter._repos_cache = ["a", "b", "c"]
    exporter._repos_cache_ts = 1e18
    github.add(R("a/actions/runs?per_page=1"), FakeResponse(payload={"total_count": 3}))
    github.add(R("b/actions/runs?per_page=1"), FakeResponse(409))
    github.add(R("c/actions/runs?per_page=1"), FakeResponse(payload={"total_count": 0}))
    assert exporter.get_active_repos() == ["a"]
    assert exporter.get_active_repos() == ["a"]
    assert github.count(R("a/actions/runs?per_page=1")) == 1


def test_a_failed_activity_probe_keeps_previous_membership(exporter, wfe, github, R):
    """A repo whose probe fails is not proven inactive: dropping it would
    blind every gauge -- and the gap check -- for 30 minutes."""
    exporter._repos_cache = ["a", "b"]
    exporter._repos_cache_ts = 1e18
    exporter._active_repos = ["a", "b"]
    exporter._active_repos_ts = 0
    github.add(R("a/actions/runs?per_page=1"), FakeResponse(payload={"total_count": 3}))
    github.add(R("b/actions/runs?per_page=1"), FakeResponse(500, text="boom"))
    assert exporter.get_active_repos() == ["a", "b"]


def test_a_malformed_jobs_answer_is_a_failed_fetch(exporter, wfe, github, R):
    """A 200 without a jobs list is not a jobless run: [] would be stored
    forever as a complete-looking run with no steps and an infra cause."""
    github.add(R("osac/actions/runs/100/jobs?filter=latest&per_page=30"),
               FakeResponse(payload={"message": "not a listing"}))
    assert exporter._fetch_run_jobs("osac", 100) is None


def test_refresh_pr_map(exporter, wfe, github, R, monkeypatch):
    upserted = []
    monkeypatch.setattr(exporter, "_upsert_pr_merge", lambda repo, pr: upserted.append((repo, pr["number"])))
    github.add(R("osac/pulls?state=open&per_page=30&sort=updated&direction=desc"),
               FakeResponse(payload=[{"number": 1, "head": {"ref": "feat"}}, {"number": 2, "head": {}}]))
    github.add(R("osac/pulls?state=closed&per_page=30&sort=updated&direction=desc"),
               FakeResponse(payload=[{"number": 3, "head": {"ref": "done"}, "merged_at": "2026-10-10T00:00:00Z"},
                                     {"number": 4, "head": {"ref": "closed"}}]))
    github.add(R("broken/pulls?state=open&per_page=30&sort=updated&direction=desc"),
               requests.exceptions.ConnectionError())
    exporter._refresh_pr_map(["osac", "broken"])
    assert exporter._lookup_pr("osac", "feat") == (1, f"https://github.com/{wfe.ORG}/osac/pull/1")
    assert exporter._lookup_pr("osac", "done")[0] == 3
    assert exporter._lookup_pr("osac", "nope") == (None, "")
    assert exporter._lookup_pr("unknown", "feat") == (None, "")
    assert upserted == [("osac", 3)]
    calls = len(github.calls)
    exporter._refresh_pr_map(["osac"])  # cached
    assert len(github.calls) == calls


def test_fetch_first_approval(exporter, github, R):
    route = R("osac/pulls/5/reviews?per_page=100")
    github.add(route, requests.exceptions.Timeout())
    assert exporter._fetch_first_approval("osac", 5) is None
    github.add(route, FakeResponse(403))
    assert exporter._fetch_first_approval("osac", 5) is None
    github.add(route, FakeResponse(payload=[
        {"state": "APPROVED", "user": {"type": "Bot"}, "submitted_at": "2026-01-01T00:00:00Z"},
        {"state": "COMMENTED", "user": {"type": "User"}, "submitted_at": "2026-01-01T00:00:01Z"},
    ]))
    assert exporter._fetch_first_approval("osac", 5) == ""
    github.add(route, FakeResponse(payload=[
        {"state": "APPROVED", "user": {"type": "User"}, "submitted_at": "2026-01-03T00:00:00Z"},
        {"state": "APPROVED", "user": {"type": "User"}, "submitted_at": "2026-01-02T00:00:00Z"},
    ]))
    assert exporter._fetch_first_approval("osac", 5) == "2026-01-02T00:00:00Z"


def test_upsert_pr_merge(exporter, github, R):
    pr = {"id": 77, "number": 5, "title": "Fix", "user": {"login": "dev"},
          "created_at": "2026-10-10T00:00:00Z", "merged_at": "2026-10-10T10:00:00Z"}
    exporter._upsert_pr_merge("osac", {"id": 1, "created_at": "bad", "merged_at": "x"})
    exporter._upsert_pr_merge("osac", {"id": 2})
    assert rows(exporter, "SELECT id FROM pr_merges") == []

    exporter._upsert_job(make_job(id=1, event="merge_group", pr_display="#5", created_at="2026-10-10T08:00:00Z"))
    exporter._upsert_job(make_job(id=2, event="merge_group", pr_display="#5", created_at="2026-10-10T09:00:00Z"))
    github.add(R("osac/pulls/5/reviews?per_page=100"), FakeResponse(payload=[
        {"state": "APPROVED", "user": {"type": "User"}, "submitted_at": "2026-10-10T06:00:00Z"}]))
    exporter._upsert_pr_merge("osac", pr)
    row = rows(exporter, "SELECT * FROM pr_merges")[0]
    assert row["merge_seconds"] == 36000
    assert row["approval_to_merge_seconds"] == 4 * 3600
    assert row["queued_at"] == "2026-10-10T09:00:00Z"
    assert row["queue_wait_seconds"] == 3600
    assert row["approval_to_queue_seconds"] == 3 * 3600
    assert row["via_merge_queue"] == 1

    # A stored approval is reused, not fetched again.
    calls = len(github.calls)
    exporter._upsert_pr_merge("osac", pr)
    assert len(github.calls) == calls


def test_upsert_pr_merge_with_bad_approval_timestamp(exporter, github, R):
    github.add(R("osac/pulls/6/reviews?per_page=100"), FakeResponse(payload=[
        {"state": "APPROVED", "user": {"type": "User"}, "submitted_at": "not-a-date"}]))
    exporter._upsert_pr_merge("osac", {"id": 8, "number": 6, "created_at": "2026-10-10T00:00:00Z",
                                       "merged_at": "2026-10-10T01:00:00Z"})
    row = rows(exporter, "SELECT * FROM pr_merges")[0]
    assert row["approval_to_merge_seconds"] is None
    assert row["via_merge_queue"] == 0


def test_backfill_queue_data(exporter):
    exporter._backfill_queue_data_if_needed()  # nothing to do
    with exporter._db() as conn:
        conn.execute("INSERT INTO pr_merges (id, repo, number, merged_at, first_approval_at) "
                     "VALUES (1, 'osac', 5, '2026-10-10T10:00:00Z', '2026-10-10T07:00:00Z')")
        conn.execute("INSERT INTO pr_merges (id, repo, number, merged_at) VALUES (2, 'osac', 6, 'x')")
    exporter._upsert_job(make_job(id=1, event="merge_group", pr_display="#5", created_at="2026-10-10T09:00:00Z"))
    exporter._backfill_queue_data_if_needed()
    got = {r["id"]: r for r in rows(exporter, "SELECT * FROM pr_merges")}
    assert (got[1]["via_merge_queue"], got[1]["queue_wait_seconds"], got[1]["approval_to_queue_seconds"]) == (
        1, 3600, 7200)
    assert got[2]["via_merge_queue"] == 0


def test_backfill_missing_pr_data(exporter, wfe):
    exporter._pr_map = {"osac": {"feat": (3, "https://pr/3")}}
    exporter._upsert_job(make_job(id=1, event="pull_request", branch="feat"))
    exporter._upsert_job(make_job(id=2, event="pull_request", branch="gone"))
    exporter._upsert_job(make_job(id=3, event="merge_group", branch="gh-readonly-queue/main/pr-9-abc"))
    exporter._backfill_missing_pr_data()
    got = {r["id"]: (r["pr_url"], r["pr_display"]) for r in rows(exporter, "SELECT * FROM jobs")}
    assert got == {
        1: ("https://pr/3", "#3"),
        2: ("", ""),
        3: (f"https://github.com/{wfe.ORG}/osac/pull/9", "#9"),
    }


def test_make_job_record(exporter, wfe):
    rec = exporter._make_job_record(make_run(), "osac")
    assert rec["pr_display"] == "#7"
    assert rec["pr_url"] == f"https://github.com/{wfe.ORG}/osac/pull/7"
    assert (rec["duration_s"], rec["duration"], rec["status"]) == (1800, "30m 0s", "success")
    # The source SHA comes from the run payload the poller already fetched.
    assert rec["head_sha"] == "9f1c2ab37d54e6f0a1b2c3d4e5f60718293a4b5c"

    exporter._pr_map = {"osac": {"feature": (8, "https://pr/8")}}
    rec = exporter._make_job_record(make_run(pull_requests=[], conclusion=None, status="queued"), "osac")
    assert (rec["pr_display"], rec["pr_url"], rec["status"]) == ("#8", "https://pr/8", "queued")

    rec = exporter._make_job_record(make_run(pull_requests=[], head_branch="other"), "osac")
    assert rec["pr_display"] == ""

    rec = exporter._make_job_record(
        make_run(event="merge_group", head_branch="gh-readonly-queue/main/pr-4-ab"), "osac")
    assert rec["pr_display"] == "#4"

    rec = exporter._make_job_record(make_run(event="merge_group", head_branch="odd"), "osac")
    assert rec["pr_display"] == ""

    rec = exporter._make_job_record(make_run(event="push", run_started_at="bad"), "osac")
    assert rec["duration_s"] == 0


def test_run_count(exporter, github, R):
    github.add(R("osac/actions/runs?status=queued&per_page=1"), FakeResponse(payload={"total_count": 4}))
    assert exporter._run_count("osac", "queued") == 4
    # A failed count is unknown (None), not a backlog of zero: a repo
    # GitHub did not answer about must not publish calm.
    assert exporter._run_count("osac", "in_progress") is None


def test_fetch_run_jobs(exporter, github, R):
    github.add(R("osac/actions/runs/1/jobs?filter=latest&per_page=30"), FakeResponse(payload={"jobs": [1]}))
    github.add(R("osac/actions/runs/1/attempts/2/jobs?per_page=30"), FakeResponse(payload={"jobs": [2]}))
    assert exporter._fetch_run_jobs("osac", 1) == [1]
    assert exporter._fetch_run_jobs("osac", 1, 2) == [2]
    assert exporter._fetch_run_jobs("osac", 9) is None


def test_recent_completed_and_history(exporter, github, R):
    github.add(R("osac/actions/runs?status=completed&per_page=50"), runs_page(make_run()))
    github.add(R("osac/actions/runs?status=completed&per_page=100"), runs_page(make_run(), make_run(id=2)))
    assert len(exporter._recent_completed("osac")) == 1
    assert len(exporter._fetch_recent_history("osac")) == 2
    assert exporter._recent_completed("other") == []
    assert exporter._fetch_recent_history("other") == []


class TestZombie:
    @pytest.fixture
    def clock(self, wfe, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(wfe.time, "monotonic", lambda: now[0])
        return now

    @pytest.fixture
    def old(self):
        return iso(datetime.now(timezone.utc) - timedelta(hours=1))

    def test_young_or_undated_runs_are_not_zombies(self, exporter):
        young = iso(datetime.now(timezone.utc))
        assert not exporter._is_zombie_run("osac", {"id": 1, "run_started_at": young})
        assert not exporter._is_zombie_run("osac", {"id": 1})
        assert not exporter._is_zombie_run("osac", {"id": 1, "created_at": "garbage"})

    def test_run_with_jobs_is_cached(self, exporter, github, R, old):
        route = R("osac/actions/runs/1/attempts/1/jobs?per_page=30")
        github.add(route, FakeResponse(payload={"jobs": [{"name": "e2e"}]}))
        run = {"id": 1, "run_attempt": 1, "run_started_at": old}
        assert not exporter._is_zombie_run("osac", run)
        assert not exporter._is_zombie_run("osac", run)
        assert github.count(route) == 1

    def test_jobless_run_is_rechecked_after_interval(self, exporter, wfe, github, R, old, clock):
        route = R("osac/actions/runs/1/attempts/2/jobs?per_page=30")
        github.add(route, FakeResponse(payload={"jobs": []}))
        run = {"id": 1, "run_attempt": 2, "run_started_at": old}
        assert exporter._is_zombie_run("osac", run)
        assert exporter._is_zombie_run("osac", run)
        assert github.count(route) == 1
        clock[0] += wfe.ZOMBIE_RECHECK_MINUTES * 60 + 1
        github.add(route, FakeResponse(500))
        assert exporter._is_zombie_run("osac", run)
        assert github.count(route) == 2
        clock[0] += 90
        assert exporter._is_zombie_run("osac", run)
        assert github.count(route) == 2
        clock[0] += wfe.ZOMBIE_RECHECK_MINUTES * 60 + 1
        github.add(route, FakeResponse(payload={"jobs": [{"name": "late"}]}))
        assert not exporter._is_zombie_run("osac", run)

    def test_failed_fetch_without_answer_is_not_zombie(self, exporter, old):
        assert not exporter._is_zombie_run("osac", {"id": 5, "run_attempt": 1, "run_started_at": old})

    def test_caches_are_bounded(self, exporter, github, R, old):
        exporter._runs_with_jobs = {(i, 0) for i in range(5001)}
        exporter._zombie_recheck_at = {(i, 0): 0 for i in range(5001)}
        github.add(R("osac/actions/runs/1/attempts/1/jobs?per_page=30"), FakeResponse(payload={"jobs": [1]}))
        github.add(R("osac/actions/runs/2/attempts/1/jobs?per_page=30"), FakeResponse(payload={"jobs": []}))
        exporter._is_zombie_run("osac", {"id": 1, "run_attempt": 1, "run_started_at": old})
        assert exporter._runs_with_jobs == {(1, 1)}
        exporter._zombie_recheck_at = {(i, 9): 0 for i in range(5001)}
        exporter._is_zombie_run("osac", {"id": 2, "run_attempt": 1, "run_started_at": old})
        assert list(exporter._zombie_recheck_at) == [(2, 1)]


def test_fetch_active_runs(exporter, wfe, github, R):
    old = iso(datetime.now(timezone.utc) - timedelta(hours=1))
    github.add(R("osac/actions/runs?status=queued&per_page=100"),
               runs_page(make_run(id=1, status="queued", conclusion=None, run_started_at=old),
                         make_run(id=2, event="dynamic"),
                         next_url=f"{wfe.API_URL}/queued-page2"))
    github.add("/queued-page2", runs_page(make_run(id=3, status="queued", conclusion=None)))
    github.add(R("osac/actions/runs/1/attempts/1/jobs?per_page=30"), FakeResponse(payload={"jobs": []}))
    github.add(R("osac/actions/runs?status=in_progress&per_page=100"),
               runs_page(make_run(id=4, status="in_progress", conclusion=None)))
    github.add(R("osac/actions/runs/4/jobs?filter=latest&per_page=30"),
               FakeResponse(payload={"jobs": [make_gh_job(runner="box-runner-07")]}))
    runs, complete = exporter._fetch_active_runs("osac")
    assert complete
    by_id = {r["id"]: r for r in runs}
    assert set(by_id) == {1, 3, 4}
    assert by_id[1]["zombie"] is True
    assert "zombie" not in by_id[3]
    assert by_id[4]["runner_name"] == "box-runner-07"


def test_fetch_active_runs_incomplete(exporter, github, R):
    github.add(R("osac/actions/runs?status=queued&per_page=100"), FakeResponse(502))
    github.add(R("osac/actions/runs?status=in_progress&per_page=100"), runs_page())
    assert exporter._fetch_active_runs("osac") == ([], False)


class TestBackfill:
    def failed_jobs(self):
        return {"jobs": [make_gh_job("e2e", "failure", [make_step("Run E2E tests", "failure")])]}

    def test_backfill_page(self, exporter, wfe, github, R, monkeypatch):
        slept = []
        monkeypatch.setattr(wfe.time, "sleep", slept.append)
        exporter._upsert_job(make_job(id=3, run_attempt=1))
        github.add("/p1",
                   FakeResponse(403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"}),
                   runs_page(make_run(id=1, conclusion="failure"), make_run(id=2, event="dynamic"),
                             make_run(id=3), make_run(id=4), make_run(id=5, name="AI diagnostic"),
                             next_url=f"{wfe.API_URL}/p2"))
        github.add(R("osac/actions/runs/1/jobs?filter=latest&per_page=30"), FakeResponse(payload=self.failed_jobs()))
        github.add(R("osac/actions/runs/5/jobs?filter=latest&per_page=30"), FakeResponse(payload={"jobs": []}))
        github.add("/p2", FakeResponse(500, text="later page broke"))
        seen, new = exporter._backfill_page("osac", f"{wfe.API_URL}/p1")
        assert (seen, new) == (4, 2)
        assert len(slept) == 1
        got = {r["id"]: r for r in rows(exporter, "SELECT * FROM jobs")}
        assert got[1]["failure_reason"] == "test"
        assert got[1]["failed_step"] == "e2e → Run E2E tests"
        assert got[1]["runner_name"] == "box-runner-01"
        assert got[5]["failure_reason"] == "n/a"
        assert 4 not in got  # jobs fetch failed, retried on a later pass

    def test_backfill_range_bisects(self, exporter, wfe, github, monkeypatch):
        since = datetime(2026, 10, 1, tzinfo=timezone.utc)
        until = datetime(2026, 10, 1, 4, tzinfo=timezone.utc)
        mid = datetime(2026, 10, 1, 2, tzinfo=timezone.utc)

        def route(a, b):
            return (f"/repos/{wfe.ORG}/osac/actions/runs?status=completed&per_page=100"
                    f"&created={iso(a)}..{iso(b)}")

        slept = []
        monkeypatch.setattr(wfe.time, "sleep", slept.append)
        github.add(route(since, until), runs_page(total=5000))
        github.add(route(since, mid),
                   FakeResponse(403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"}),
                   runs_page(make_run(id=1, conclusion="failure"), make_run(id=2, event="dynamic"),
                             next_url=f"{wfe.API_URL}/more"))
        github.add("/more", runs_page(make_run(id=3)))
        github.add(route(mid, until), runs_page(make_run(id=4), make_run(id=1, conclusion="failure")))
        github.add(f"/repos/{wfe.ORG}/osac/actions/runs/1/jobs?filter=latest&per_page=30",
                   FakeResponse(payload={"jobs": []}))
        github.add(f"/repos/{wfe.ORG}/osac/actions/runs/3/jobs?filter=latest&per_page=30",
                   FakeResponse(payload=self.failed_jobs()))
        seen, new = exporter._backfill_range("osac", since, until)
        assert (seen, new) == (4, 2)
        assert len(slept) == 1
        assert rows(exporter, "SELECT failure_reason FROM jobs WHERE id = 1") == [{"failure_reason": "infra"}]

    def test_backfill_range_probe_failure(self, exporter):
        since = datetime(2026, 10, 1, tzinfo=timezone.utc)
        assert exporter._backfill_range("osac", since, since + timedelta(hours=1)) == (0, 0)

    def test_backfill(self, exporter, wfe, monkeypatch):
        monkeypatch.setattr(wfe, "REPOS_FILTER", ["a", "b"])
        monkeypatch.setattr(exporter, "_refresh_pr_map", lambda repos: None)
        monkeypatch.setattr(exporter, "_backfill_range", lambda repo, s, u: (3, 1))
        assert exporter.backfill(2) == (6, 2)


class TestReconcile:
    """The runs the polling cannot see: bursts, restarts, failed polls, and a
    result that changed under the same attempt."""

    def calls(self, exporter, monkeypatch, result=(10, 2)):
        seen = []

        def fake(repo, since, until, wait=True):
            seen.append((repo, until - since, wait))
            return result

        monkeypatch.setattr(exporter, "_backfill_range", fake)
        return seen

    def test_it_runs_on_the_first_cycle_then_every_interval(self, exporter, wfe, monkeypatch):
        seen = self.calls(exporter, monkeypatch)
        clock = [1000.0]
        monkeypatch.setattr(wfe.time, "monotonic", lambda: clock[0])
        assert exporter._reconcile_if_due(["osac", "osac-test-infra"]) == 4
        assert seen == [("osac", timedelta(hours=wfe.RECONCILE_HOURS), False),
                        ("osac-test-infra", timedelta(hours=wfe.RECONCILE_HOURS), False)]
        clock[0] += wfe.RECONCILE_INTERVAL - 1
        assert exporter._reconcile_if_due(["osac"]) is None
        clock[0] += 1
        assert exporter._reconcile_if_due(["osac"]) == 2
        assert len(seen) == 3

    def test_it_waits_while_the_budget_is_low(self, exporter, wfe, monkeypatch):
        seen = self.calls(exporter, monkeypatch)
        exporter._rate_limit_remaining = wfe.RECONCILE_MIN_RATE_LIMIT - 1
        assert exporter._reconcile_if_due(["osac"]) is None
        assert seen == []
        exporter._rate_limit_remaining = wfe.RECONCILE_MIN_RATE_LIMIT
        assert exporter._reconcile_if_due(["osac"]) == 2

    def test_it_counts_what_it_stored(self, exporter, wfe, monkeypatch):
        self.calls(exporter, monkeypatch, result=(10, 3))
        before = wfe.reconciled_runs.labels(org=wfe.ORG, repo="osac")._value.get()
        exporter._reconcile_if_due(["osac"])
        assert wfe.reconciled_runs.labels(org=wfe.ORG, repo="osac")._value.get() == before + 3

    def test_a_missed_run_and_an_outdated_result_are_stored(self, exporter, wfe, github, monkeypatch):
        since = datetime(2026, 10, 1, tzinfo=timezone.utc)
        until = datetime(2026, 10, 1, 4, tzinfo=timezone.utc)
        exporter._upsert_job(make_job(id=1, conclusion="success"))
        exporter._upsert_job(make_job(id=3, conclusion="action_required"))
        route = (f"/repos/{wfe.ORG}/osac/actions/runs?status=completed&per_page=100"
                 f"&created={iso(since)}..{iso(until)}")
        github.add(route, runs_page(make_run(id=1), make_run(id=2), make_run(id=3, conclusion="failure")))
        for run_id in (2, 3):
            github.add(f"/repos/{wfe.ORG}/osac/actions/runs/{run_id}/jobs?filter=latest&per_page=30",
                       FakeResponse(payload={"jobs": []}))
        assert exporter._backfill_range("osac", since, until, wait=False) == (3, 2)
        got = {r["id"]: r["conclusion"] for r in rows(exporter, "SELECT id, conclusion FROM jobs")}
        assert got == {1: "success", 2: "success", 3: "failure"}

    def test_an_exhausted_budget_ends_the_pass_without_sleeping(self, exporter, wfe, github, monkeypatch):
        slept = []
        monkeypatch.setattr(wfe.time, "sleep", slept.append)
        since = datetime(2026, 10, 1, tzinfo=timezone.utc)
        until = datetime(2026, 10, 1, 4, tzinfo=timezone.utc)
        limited = FakeResponse(403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"})
        route = (f"/repos/{wfe.ORG}/osac/actions/runs?status=completed&per_page=100"
                 f"&created={iso(since)}..{iso(until)}")
        github.add(route, limited)
        assert exporter._backfill_range("osac", since, until, wait=False) == (0, 0)
        github.add("/more", limited)
        assert exporter._backfill_page("osac", f"{wfe.API_URL}/more", wait=False) == (0, 0)
        assert slept == []

    def test_collect_reconciles_after_the_poll(self, exporter, wfe, monkeypatch):
        monkeypatch.setattr(wfe, "REPOS_FILTER", ["osac"])
        monkeypatch.setattr(exporter, "_refresh_pr_map", lambda repos: None)
        monkeypatch.setattr(exporter, "_backfill_missing_pr_data", lambda: None)
        monkeypatch.setattr(exporter, "_backfill_queue_data_if_needed", lambda: None)
        monkeypatch.setattr(exporter, "_run_count", lambda repo, status: 0)
        monkeypatch.setattr(exporter, "_recent_completed", lambda repo: [])
        monkeypatch.setattr(exporter, "_backfill_timelines_if_needed", lambda: None)
        asked = []
        monkeypatch.setattr(exporter, "_reconcile_if_due", lambda repos: asked.append(list(repos)))
        exporter.collect()
        assert asked == [["osac"]]

    def test_a_failed_reconciliation_does_not_stop_the_cycle(self, exporter, wfe, monkeypatch):
        monkeypatch.setattr(wfe, "REPOS_FILTER", ["osac"])
        monkeypatch.setattr(exporter, "_refresh_pr_map", lambda repos: None)
        monkeypatch.setattr(exporter, "_backfill_missing_pr_data", lambda: None)
        monkeypatch.setattr(exporter, "_backfill_queue_data_if_needed", lambda: None)
        monkeypatch.setattr(exporter, "_run_count", lambda repo, status: 0)
        monkeypatch.setattr(exporter, "_recent_completed", lambda repo: [])
        timelines = []
        monkeypatch.setattr(exporter, "_backfill_timelines_if_needed", lambda: timelines.append(True))

        def boom(repos):
            raise RuntimeError("GitHub is down")

        monkeypatch.setattr(exporter, "_reconcile_if_due", boom)
        exporter.collect()
        assert timelines == [True]


class TestInitialLoad:
    def test_skips_non_empty_db(self, exporter, github):
        exporter._upsert_job(make_job())
        exporter.initial_load()
        assert github.calls == []

    def test_loads_history(self, exporter, wfe, github, R, monkeypatch):
        monkeypatch.setattr(exporter, "get_active_repos", lambda: ["osac", "broken"])
        monkeypatch.setattr(exporter, "_refresh_pr_map", lambda repos: None)
        github.add(R("osac/actions/runs?status=completed&per_page=100"),
                   runs_page(make_run(id=1, conclusion="failure"), make_run(id=2, event="dynamic"),
                             make_run(id=3)))
        github.add(R("osac/actions/runs/1/jobs?filter=latest&per_page=30"), FakeResponse(payload={"jobs": [
            make_gh_job("e2e", "failure", [make_step("Checkout", "failure")])]}))
        github.add(R("broken/actions/runs?status=completed&per_page=100"), RuntimeError("boom"))
        exporter.initial_load()
        got = rows(exporter, "SELECT id, failure_reason, failed_step FROM jobs")
        assert got == [{"id": 1, "failure_reason": "infra", "failed_step": "e2e → Checkout"}]


class TestGapCheck:
    """The hourly proof that the whole retention window is complete: one
    count per repo per day, a run-by-run verification only where the counts
    differ, and an error recorded for a day GitHub would not answer."""

    NOW = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)

    def days(self, wfe):
        return wfe.WorkflowExporter._gap_check_days(self.NOW)

    def probe_route(self, wfe, repo, day):
        next_day = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).date().isoformat()
        return (f"/repos/{wfe.ORG}/{repo}/actions/runs?status=completed&per_page=1"
                f"&created={day}T00:00:00Z..{next_day}T00:00:00Z")

    def list_route(self, wfe, repo, day):
        next_day = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).date().isoformat()
        return (f"/repos/{wfe.ORG}/{repo}/actions/runs?status=completed&per_page=100"
                f"&created={day}T00:00:00Z..{next_day}T00:00:00Z")

    def probe_every_day(self, wfe, github, repo, totals):
        """One probe answer per day of the window, None meaning total 0."""
        for day in self.days(wfe):
            github.add(self.probe_route(wfe, repo, day),
                       FakeResponse(payload={"workflow_runs": [], "total_count": totals.get(day, 0)}))

    def day_rows(self, exporter):
        return rows(exporter, "SELECT day, state, expected, adjustment, stored, missing, error"
                              " FROM day_checks ORDER BY day")

    def test_matching_days_are_ok_at_one_call_each(self, exporter, wfe, github):
        exporter._upsert_job(make_job(id=1, created_at="2026-11-20T10:00:00Z"))
        self.probe_every_day(wfe, github, "osac", {"2026-11-20": 1})
        assert exporter._gap_check_if_due(["osac"], now=self.NOW) == 0
        days = self.days(wfe)
        assert [row["state"] for row in self.day_rows(exporter)] == ["ok"] * len(days)
        assert len(github.calls) == len(days)
        assert wfe.gap_check_days.labels(org=wfe.ORG, repo="osac", state="ok")._value.get() == len(days)
        assert wfe.gap_check_missing.labels(org=wfe.ORG, repo="osac")._value.get() == 0

    def test_a_day_with_missing_runs_is_verified_stored_and_ok(self, exporter, wfe, github):
        exporter._upsert_job(make_job(id=1, created_at="2026-11-20T10:00:00Z"))
        self.probe_every_day(wfe, github, "osac", {"2026-11-20": 2})
        github.add(self.list_route(wfe, "osac", "2026-11-20"),
                   runs_page(make_run(id=1, created_at="2026-11-20T10:00:00Z"),
                             make_run(id=2, created_at="2026-11-20T11:00:00Z")))
        github.add(f"/repos/{wfe.ORG}/osac/actions/runs/2/jobs?filter=latest&per_page=30",
                   FakeResponse(payload={"jobs": []}))
        assert exporter._gap_check_if_due(["osac"], now=self.NOW) == 0
        matching = [row for row in self.day_rows(exporter) if row["day"] == "2026-11-20"]
        assert matching == [{"day": "2026-11-20", "state": "ok", "expected": 2, "adjustment": 0,
                             "stored": 2, "missing": 0, "error": ""}]
        assert [row["id"] for row in rows(exporter, "SELECT id FROM jobs ORDER BY id")] == [1, 2]

    def test_dynamic_and_edge_runs_become_the_adjustment(self, exporter, wfe, github, monkeypatch):
        clock = [0.0]
        monkeypatch.setattr(wfe.time, "monotonic", lambda: clock[0])
        exporter._upsert_job(make_job(id=1, created_at="2026-11-20T10:00:00Z"))
        self.probe_every_day(wfe, github, "osac", {"2026-11-20": 3})
        # A dynamic run GitHub counts but the exporter never stores, and a
        # run created at the next day's midnight, which GitHub's created
        # filter counts on both days that touch it.
        github.add(self.list_route(wfe, "osac", "2026-11-20"),
                   runs_page(make_run(id=1, created_at="2026-11-20T10:00:00Z"),
                             make_run(id=2, event="dynamic"),
                             make_run(id=3, created_at="2026-11-21T00:00:00Z")))
        github.add(f"/repos/{wfe.ORG}/osac/actions/runs/3/jobs?filter=latest&per_page=30",
                   FakeResponse(payload={"jobs": []}))
        assert exporter._gap_check_if_due(["osac"], now=self.NOW) == 0
        matching = [row for row in self.day_rows(exporter) if row["day"] == "2026-11-20"][0]
        assert (matching["state"], matching["adjustment"], matching["stored"]) == ("ok", 2, 1)
        # The next pass trusts the adjustment: no listing, only the probe.
        listing_calls = github.count(self.list_route(wfe, "osac", "2026-11-20"))
        clock[0] += wfe.GAP_CHECK_INTERVAL
        assert exporter._gap_check_if_due(["osac"], now=self.NOW) == 0
        assert github.count(self.list_route(wfe, "osac", "2026-11-20")) == listing_calls

    def test_a_failed_probe_is_an_error_never_an_ok(self, exporter, wfe, github, caplog):
        for day in self.days(wfe):
            status = 500 if day == "2026-11-20" else 200
            payload = {"workflow_runs": [], "total_count": 0} if status == 200 else {"message": "boom"}
            github.add(self.probe_route(wfe, "osac", day), FakeResponse(status, payload=payload, text="boom"))
        before = wfe.gap_check_last_success.labels(org=wfe.ORG)._value.get()
        with caplog.at_level("ERROR"):
            assert exporter._gap_check_if_due(["osac"], now=self.NOW) == 0
        matching = [row for row in self.day_rows(exporter) if row["day"] == "2026-11-20"][0]
        assert matching["state"] == "error"
        assert "HTTP 500" in matching["error"]
        assert any("could not read GitHub" in record.getMessage() for record in caplog.records)
        assert wfe.gap_check_last_success.labels(org=wfe.ORG)._value.get() == before
        assert wfe.gap_check_days.labels(org=wfe.ORG, repo="osac", state="error")._value.get() == 1

    def test_verification_is_bounded_per_pass(self, exporter, wfe, github, monkeypatch):
        monkeypatch.setattr(wfe, "GAP_CHECK_VERIFIES_PER_PASS", 1)
        exporter._upsert_job(make_job(id=1, created_at="2026-11-19T10:00:00Z"))
        exporter._upsert_job(make_job(id=2, created_at="2026-11-20T10:00:00Z"))
        self.probe_every_day(wfe, github, "osac", {"2026-11-19": 2, "2026-11-20": 2})
        github.add(self.list_route(wfe, "osac", "2026-11-20"),
                   runs_page(make_run(id=2, created_at="2026-11-20T10:00:00Z")))
        github.add(self.list_route(wfe, "osac", "2026-11-19"),
                   runs_page(make_run(id=1, created_at="2026-11-19T10:00:00Z"),
                             make_run(id=5, created_at="2026-11-19T11:00:00Z")))
        github.add(f"/repos/{wfe.ORG}/osac/actions/runs/5/jobs?filter=latest&per_page=30",
                   FakeResponse(payload={"jobs": []}))
        clock = [0.0]
        monkeypatch.setattr(wfe.time, "monotonic", lambda: clock[0])
        # One verification per pass, and the newest mismatching day gets it.
        assert exporter._gap_check_if_due(["osac"], now=self.NOW) == 1
        states = {row["day"]: row["state"] for row in self.day_rows(exporter)}
        assert states["2026-11-19"] == "pending"
        assert states["2026-11-20"] == "ok"
        clock[0] += wfe.GAP_CHECK_INTERVAL
        assert exporter._gap_check_if_due(["osac"], now=self.NOW) == 0
        states = {row["day"]: row["state"] for row in self.day_rows(exporter)}
        assert states["2026-11-19"] == "ok"

    def test_the_rate_limit_gate_skips_the_pass(self, exporter, wfe, github):
        exporter._rate_limit_remaining = wfe.GAP_CHECK_MIN_RATE_LIMIT - 1
        before = wfe.gap_check_skipped.labels(org=wfe.ORG)._value.get()
        assert exporter._gap_check_if_due(["osac"], now=self.NOW) is None
        assert github.calls == []
        assert wfe.gap_check_skipped.labels(org=wfe.ORG)._value.get() == before + 1

    def test_a_run_the_backfill_cannot_store_stays_gapped(self, exporter, wfe, github):
        exporter._upsert_job(make_job(id=1, created_at="2026-11-20T10:00:00Z"))
        self.probe_every_day(wfe, github, "osac", {"2026-11-20": 2})
        github.add(self.list_route(wfe, "osac", "2026-11-20"),
                   runs_page(make_run(id=1, created_at="2026-11-20T10:00:00Z"),
                             make_run(id=2, created_at="2026-11-20T11:00:00Z")))
        github.add(f"/repos/{wfe.ORG}/osac/actions/runs/2/jobs?filter=latest&per_page=30",
                   FakeResponse(500, text="jobs listing broke"))
        assert exporter._gap_check_if_due(["osac"], now=self.NOW) == 0
        matching = [row for row in self.day_rows(exporter) if row["day"] == "2026-11-20"][0]
        assert matching["state"] == "gapped"
        assert matching["missing"] == 1
        assert "2" in matching["error"]
        assert wfe.gap_check_missing.labels(org=wfe.ORG, repo="osac")._value.get() == 1
        assert wfe.gap_check_days.labels(org=wfe.ORG, repo="osac", state="gapped")._value.get() == 1

    def test_windows_over_the_list_cap_are_bisected(self, exporter, wfe, github):
        since = datetime(2026, 11, 20, tzinfo=timezone.utc)
        until = since + timedelta(days=1)
        mid = since + timedelta(hours=12)

        def route(a, b):
            return (f"/repos/{wfe.ORG}/osac/actions/runs?status=completed&per_page=100"
                    f"&created={iso(a)}..{iso(b)}")

        github.add(route(since, until), runs_page(total=5000))
        github.add(route(since, mid), runs_page(make_run(id=1)))
        github.add(route(mid, until), runs_page(make_run(id=2)))
        ids, dynamic = exporter._list_window_runs("osac", since, until)
        assert (ids, dynamic) == ([1, 2], 0)
        assert github.count(route(since, mid)) == 1
        assert github.count(route(mid, until)) == 1

    def test_suite_rows_do_not_inflate_the_stored_count(self, exporter):
        exporter._upsert_job(make_job(id=1, created_at="2026-11-20T10:00:00Z"))
        exporter._upsert_job(make_job(id=2, run_id="1", created_at="2026-11-20T10:05:00Z"))
        exporter._upsert_job(make_job(id=3, run_id="1", created_at="2026-11-20T10:06:00Z"))
        assert exporter._stored_runs_on_day("osac", "2026-11-20") == 1


class TestStoreFreshness:
    """The last success of every ci-observe store, so a stalled pass is an
    alert, not a quiet gap."""

    def build_stores(self, wfe, tmp_path):
        trends_db = sqlite3.connect(tmp_path / "trends.db")
        trends_db.execute("CREATE TABLE rollup_passes (job TEXT, finished_at TEXT, summary TEXT)")
        trends_db.executemany(
            "INSERT INTO rollup_passes VALUES (?, ?, '')",
            [("trends", "2026-11-21T15:26:02Z", ), ("spans", "2026-11-21T15:50:00Z")])
        trends_db.commit()
        trends_db.close()
        prs_db = sqlite3.connect(tmp_path / "prs.db")
        prs_db.execute("CREATE TABLE pr_backfill_runs (id INTEGER, ran_at TEXT, window_to TEXT,"
                       " listing_complete INTEGER)")
        prs_db.execute("INSERT INTO pr_backfill_runs VALUES (1, '2026-11-21T15:05:49Z',"
                       " '2026-11-21T15:05:49Z', 1)")
        prs_db.execute("INSERT INTO pr_backfill_runs VALUES (2, '2026-11-21T16:05:49Z',"
                       " '2026-11-16T12:09:03Z', 0)")
        prs_db.commit()
        prs_db.close()
        (tmp_path / "ci-observe-audit.json").write_text(
            '{"summary": {"checked_at": "2026-11-20T03:37:10Z"}}', encoding="utf-8")

    def test_every_store_pins_its_last_success(self, exporter, wfe, tmp_path):
        self.build_stores(wfe, tmp_path)
        exporter._update_store_freshness()

        def value(store):
            return wfe.store_last_success.labels(org=wfe.ORG, store=store)._value.get()

        assert value("jobs") > 0
        assert value("trends") == datetime(2026, 11, 21, 15, 26, 2, tzinfo=timezone.utc).timestamp()
        assert value("spans") == datetime(2026, 11, 21, 15, 50, tzinfo=timezone.utc).timestamp()
        # The pass that stopped early does not count: its window was not covered.
        assert value("prs") == datetime(2026, 11, 21, 15, 5, 49, tzinfo=timezone.utc).timestamp()
        assert value("audit") == datetime(2026, 11, 20, 3, 37, 10, tzinfo=timezone.utc).timestamp()

    def test_an_unreadable_store_is_logged_and_left_to_age(self, exporter, wfe, tmp_path, caplog):
        self.build_stores(wfe, tmp_path)
        (tmp_path / "ci-observe-audit.json").unlink()
        before = wfe.store_last_success.labels(org=wfe.ORG, store="audit")._value.get()
        with caplog.at_level("ERROR"):
            exporter._update_store_freshness()
        assert any("audit" in record.getMessage() and "could not be read" in record.getMessage()
                   for record in caplog.records)
        assert wfe.store_last_success.labels(org=wfe.ORG, store="audit")._value.get() == before
