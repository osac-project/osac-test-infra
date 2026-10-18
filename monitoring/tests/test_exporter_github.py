"""GitHub API access of workflow-exporter.py: repos, PRs, runs, jobs, backfill."""

from __future__ import annotations

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
    github.add(f"/orgs/{wfe.ORG}/repos?per_page=100&type=all", FakeResponse(500, text="boom"))
    assert exporter.get_repos() == []


def test_get_active_repos(exporter, wfe, github, R):
    exporter._repos_cache = ["a", "b", "c"]
    exporter._repos_cache_ts = 1e18
    github.add(R("a/actions/runs?per_page=1"), FakeResponse(payload={"total_count": 3}))
    github.add(R("b/actions/runs?per_page=1"), FakeResponse(409))
    github.add(R("c/actions/runs?per_page=1"), FakeResponse(payload={"total_count": 0}))
    assert exporter.get_active_repos() == ["a"]
    assert exporter.get_active_repos() == ["a"]
    assert github.count(R("a/actions/runs?per_page=1")) == 1


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
    assert exporter._run_count("osac", "in_progress") == 0


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
