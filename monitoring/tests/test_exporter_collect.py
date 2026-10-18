"""The polling cycle of workflow-exporter.py: collect(), completed runs, main()."""

from __future__ import annotations

import pytest

from helpers import FakeResponse, make_gh_job, make_job, make_run, make_step


def gauge(metric, **labels):
    return metric.labels(**labels)._value.get()


def counter(metric, **labels):
    return metric.labels(**labels)._value.get()


@pytest.fixture
def R(wfe):
    return lambda path: f"/repos/{wfe.ORG}/{path}"


def rows(exporter, sql, *args):
    with exporter._db() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


class TestProcessCompletedRun:
    def test_skips_ignored_and_current_runs(self, exporter):
        assert not exporter._process_completed_run("osac", make_run(event="dynamic"))
        exporter._upsert_job(make_job(id=100))
        assert not exporter._process_completed_run("osac", make_run())

    def test_skips_failed_jobs_fetch(self, exporter):
        assert not exporter._process_completed_run("osac", make_run())
        assert rows(exporter, "SELECT id FROM jobs") == []

    def test_records_failure_and_metrics(self, exporter, wfe, github, R):
        wf = "E2E metrics test"
        github.add(R("osac/actions/runs/100/jobs?filter=latest&per_page=30"), FakeResponse(payload={"jobs": [
            make_gh_job("e2e", "failure", [make_step("Run E2E tests", "failure")])]}))
        run = make_run(name=wf, conclusion="failure")
        before = counter(wfe.failed_step_total, org=wfe.ORG, workflow=wf, step="Run E2E tests")
        assert exporter._process_completed_run("osac", run)
        assert counter(wfe.completed_runs, org=wfe.ORG, repo="osac", workflow=wf, conclusion="failure") == 1
        assert counter(wfe.failed_step_total, org=wfe.ORG, workflow=wf, step="Run E2E tests") == before + 1
        row = rows(exporter, "SELECT failure_reason, failed_step FROM jobs")[0]
        assert row == {"failure_reason": "test", "failed_step": "e2e → Run E2E tests"}
        # The same attempt again changes nothing.
        assert not exporter._process_completed_run("osac", run)

    def test_upsert_race_returns_false(self, exporter, github, R, monkeypatch):
        github.add(R("osac/actions/runs/100/jobs?filter=latest&per_page=30"), FakeResponse(payload={"jobs": []}))
        monkeypatch.setattr(exporter, "_upsert_job", lambda record: False)
        assert not exporter._process_completed_run("osac", make_run(run_started_at=None))


class TestCollect:
    @pytest.fixture
    def setup(self, exporter, wfe, monkeypatch):
        monkeypatch.setattr(wfe, "REPOS_FILTER", ["osac", "quiet", "broken"])
        monkeypatch.setattr(exporter, "_refresh_pr_map", lambda repos: None)
        return exporter

    def test_full_cycle(self, setup, wfe, github, R, monkeypatch):
        exporter = setup
        org = wfe.ORG
        github.add(R("osac/actions/runs?status=queued&per_page=1"), FakeResponse(payload={"total_count": 2}))
        github.add(R("osac/actions/runs?status=in_progress&per_page=1"), FakeResponse(payload={"total_count": 1}))
        queued = [
            make_run(id=1, name="E2E VMaaS", status="queued", conclusion=None),
            make_run(id=2, name="pre-commit", status="queued", conclusion=None),
        ]
        in_progress = [make_run(id=3, name="E2E CaaS", status="in_progress", conclusion=None)]
        monkeypatch.setattr(exporter, "_fetch_active_runs", lambda repo: (
            [dict(exporter._make_job_record(queued[0], repo), zombie=True),
             exporter._make_job_record(queued[1], repo),
             exporter._make_job_record(in_progress[0], repo)], True))
        github.add(R("osac/actions/runs?status=completed&per_page=50"),
                   FakeResponse(payload={"workflow_runs": [make_run(id=10, name="Unit tests")]}))
        github.add(R("osac/actions/runs/10/jobs?filter=latest&per_page=30"), FakeResponse(payload={"jobs": []}))
        github.add(R("broken/actions/runs?status=queued&per_page=1"), RuntimeError("boom"))

        # Runs that were active last cycle and are gone now.
        exporter.active_runs = [
            {"id": 3, "repo": "osac"},
            {"id": 20, "repo": "osac"},
            {"id": 21, "repo": "osac"},
            {"id": 22, "repo": "osac"},
            {"id": 23, "repo": "osac"},
        ]
        github.add(R("osac/actions/runs/20"), FakeResponse(payload=make_run(id=20, name="Long E2E")))
        github.add(R("osac/actions/runs/20/jobs?filter=latest&per_page=30"), FakeResponse(payload={"jobs": []}))
        github.add(R("osac/actions/runs/21"), FakeResponse(payload=make_run(id=21, status="in_progress")))
        github.add(R("osac/actions/runs/22"), RuntimeError("boom"))

        exporter._upsert_job(make_job(id=50, event="merge_group", branch="gh-readonly-queue/main/pr-5-ab"))
        exporter.collect()

        assert gauge(wfe.queued_runs, org=org, repo="osac") == 1
        assert gauge(wfe.zombie_runs, org=org, repo="osac") == 1
        assert gauge(wfe.in_progress_runs, org=org, repo="osac") == 1
        assert gauge(wfe.queued_runs, org=org, repo="quiet") == 0
        assert gauge(wfe.queued_total, org=org) == 1
        assert gauge(wfe.zombie_total, org=org) == 1
        assert gauge(wfe.queued_by_category, org=org, category="lint") == 1
        assert gauge(wfe.queued_by_category, org=org, category="e2e") == 0
        assert gauge(wfe.in_progress_by_category, org=org, category="e2e") == 1
        stored = {r["id"] for r in rows(exporter, "SELECT id FROM jobs")}
        assert stored == {10, 20, 50}
        assert {r["id"] for r in exporter.active_runs} == {1, 2, 3, 21}
        assert exporter._pr_backfill_done
        assert rows(exporter, "SELECT pr_display FROM jobs WHERE id = 50") == [{"pr_display": "#5"}]

    def test_incomplete_active_fetch_keeps_category_gauges(self, setup, wfe, github, R, monkeypatch):
        exporter = setup
        org = wfe.ORG
        wfe.queued_by_category.labels(org=org, category="release").set(7)
        github.add(R("osac/actions/runs?status=queued&per_page=1"), FakeResponse(payload={"total_count": 3}))
        github.add(R("quiet/actions/runs?status=in_progress&per_page=1"), FakeResponse(payload={"total_count": 1}))
        exporter.active_runs = [{"id": 99, "repo": "osac"}]

        def fetch(repo):
            if repo == "osac":
                return [], False
            raise RuntimeError("boom")

        monkeypatch.setattr(exporter, "_fetch_active_runs", fetch)
        exporter.collect()
        assert gauge(wfe.queued_runs, org=org, repo="osac") == 3
        assert gauge(wfe.zombie_runs, org=org, repo="osac") == 0
        assert gauge(wfe.queued_by_category, org=org, category="release") == 7
        assert R("osac/actions/runs/99") not in github.calls
        assert exporter.active_runs == []


class TestMain:
    class Stop(BaseException):
        pass

    def test_requires_token(self, wfe, monkeypatch):
        monkeypatch.setattr(wfe, "TOKEN", None)
        with pytest.raises(SystemExit):
            wfe.main()

    def test_backfill_mode(self, wfe, monkeypatch):
        calls = []

        class FakeExporter:
            def backfill(self, days):
                calls.append(days)

        monkeypatch.setattr(wfe, "TOKEN", "t")
        monkeypatch.setattr(wfe, "WorkflowExporter", FakeExporter)
        monkeypatch.setenv("BACKFILL_DAYS", "3")
        wfe.main()
        assert calls == [3]

    def test_serve_mode_polls_until_stopped(self, wfe, monkeypatch):
        events = []

        class FakeExporter:
            def initial_load(self):
                events.append("load")

            def collect(self):
                events.append("collect")
                if events.count("collect") == 1:
                    raise RuntimeError("one bad cycle")

        class FakeServer:
            def __init__(self, addr, handler):
                events.append(("server", addr[1], handler))

            def serve_forever(self):
                events.append("serving")

        def sleep(seconds):
            if events.count("collect") == 2:
                raise self.Stop

        monkeypatch.setattr(wfe, "TOKEN", "t")
        monkeypatch.delenv("BACKFILL_DAYS", raising=False)
        monkeypatch.setattr(wfe, "WorkflowExporter", FakeExporter)
        monkeypatch.setattr(wfe, "ThreadingHTTPServer", FakeServer)
        monkeypatch.setattr(wfe.time, "sleep", sleep)
        monkeypatch.setattr(wfe.ExporterHandler, "exporter", None)
        with pytest.raises(self.Stop):
            wfe.main()
        assert ("server", wfe.PORT, wfe.ExporterHandler) in events
        assert events.count("collect") == 2
        assert isinstance(wfe.ExporterHandler.exporter, FakeExporter)
