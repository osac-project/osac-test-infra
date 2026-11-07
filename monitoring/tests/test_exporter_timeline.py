"""The job and step timeline the workflow exporter stores for E2E rows
(timeline_json), and the catch-up that fills it for rows stored before it."""

from __future__ import annotations

import json

from helpers import FakeResponse, make_job, make_run


def gh_job(name="e2e", conclusion="success", created="2026-10-10T10:00:00Z", started="2026-10-10T10:05:00Z",
           completed="2026-10-10T10:30:00Z", steps=None, runner="box-runner-01"):
    return {
        "id": 1,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "created_at": created,
        "started_at": started,
        "completed_at": completed,
        "runner_name": runner,
        "html_url": "https://github.com/osac-project/osac/actions/runs/100/job/1",
        "steps": steps if steps is not None else [
            {"number": 1, "name": "Boot cluster clone", "status": "completed", "conclusion": "success",
             "started_at": "2026-10-10T10:05:00Z", "completed_at": "2026-10-10T10:15:00Z"},
            {"number": 2, "name": "Run E2E tests", "status": "completed", "conclusion": conclusion,
             "started_at": "2026-10-10T10:15:00Z", "completed_at": "2026-10-10T10:30:00Z"},
        ],
    }


def stored(exporter, run_id):
    with exporter._db() as conn:
        return dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (run_id,)).fetchone())


def test_a_completed_e2e_run_stores_its_timeline(wfe, exporter, github):
    github.add(f"/repos/{wfe.ORG}/osac/actions/runs/100/jobs?filter=latest&per_page=30",
               FakeResponse(payload={"jobs": [gh_job(), gh_job(name="changes-gate", steps=[])]}))
    assert exporter._process_completed_run("osac", make_run(id=100, name="E2E VMaaS Full Install"))
    timeline = json.loads(stored(exporter, 100)["timeline_json"])
    assert [job["name"] for job in timeline] == ["e2e"], "gate jobs are plumbing, not timeline"
    job = timeline[0]
    assert job["created_at"] == "2026-10-10T10:00:00Z"
    assert job["started_at"] == "2026-10-10T10:05:00Z"
    assert [step["name"] for step in job["steps"]] == ["Boot cluster clone", "Run E2E tests"]
    assert job["steps"][1]["conclusion"] == "success"
    assert job["runner_name"] == "box-runner-01"


def test_a_non_e2e_run_stores_no_timeline(wfe, exporter, github):
    github.add(f"/repos/{wfe.ORG}/osac/actions/runs/101/jobs?filter=latest&per_page=30",
               FakeResponse(payload={"jobs": [gh_job()]}))
    assert exporter._process_completed_run("osac", make_run(id=101, name="Pre-commit", event="push"))
    assert stored(exporter, 101)["timeline_json"] == ""


def test_the_timeline_never_reaches_the_jobs_api(wfe, exporter):
    exporter._upsert_job(make_job(id=5, timeline_json='[{"name": "e2e"}]'))
    job = exporter.get_jobs_json({"limit": ["10"]})[0]
    assert "timeline_json" not in job
    assert stored(exporter, 5)["timeline_json"] == '[{"name": "e2e"}]'


def test_a_cancelled_step_is_kept(wfe, exporter):
    timeline = exporter._extract_timeline([gh_job(conclusion="failure", steps=[
        {"number": 1, "name": "Install OSAC", "status": "completed", "conclusion": "cancelled",
         "started_at": "2026-10-10T10:05:00Z", "completed_at": "2026-10-10T16:05:00Z"},
    ])])
    assert timeline[0]["steps"][0]["conclusion"] == "cancelled"


def test_each_merge_queue_suite_row_times_only_its_own_jobs(wfe, exporter):
    jobs = [
        {**gh_job(name="vmaas / e2e-vmaas-full-install / e2e"), "id": 11},
        {**gh_job(name="caas / e2e-caas-full-install / e2e", started="2026-10-10T11:00:00Z"), "id": 12},
        {**gh_job(name="plan / Plan the queue entry", steps=[]), "id": 13},
    ]
    record = {"id": 1, "repo": "osac", "workflow": "E2E for the merge queue", "category": "automation",
              "url": "run-url", "run_attempt": 1, "run_number": 5}
    wfe.WorkflowExporter._refine_pipeline_category(record, jobs)
    rows = {row["workflow"]: row for row in exporter._suite_records(record, jobs)}
    vmaas = json.loads(rows["E2E vmaas (merge queue)"]["timeline_json"])
    caas = json.loads(rows["E2E caas (merge queue)"]["timeline_json"])
    assert [job["name"] for job in vmaas] == ["vmaas / e2e-vmaas-full-install / e2e"]
    assert [job["name"] for job in caas] == ["caas / e2e-caas-full-install / e2e"]


def test_the_catch_up_fills_old_rows_newest_first(wfe, exporter, github, monkeypatch):
    monkeypatch.setattr(wfe, "TOKEN", "token")
    exporter._upsert_job(make_job(id=200, run_id="200", created_at="2026-10-10T10:00:00Z",
                                  workflow="E2E VMaaS Full Install"))
    exporter._upsert_job(make_job(id=201, run_id="201", created_at="2026-10-11T10:00:00Z",
                                  workflow="E2E VMaaS Full Install"))
    exporter._upsert_job(make_job(id=202, run_id="202", category="ci", workflow="CI"))
    exporter._upsert_job(make_job(id=203, run_id="203", conclusion="", status="in_progress"))
    for run_id in (200, 201):
        github.add(f"/repos/{wfe.ORG}/osac/actions/runs/{run_id}/attempts/1/jobs?per_page=30",
                   FakeResponse(payload={"jobs": [gh_job()]}))
    assert exporter._backfill_timelines_if_needed(limit=1) == 1
    assert stored(exporter, 201)["timeline_json"].startswith("[")
    assert stored(exporter, 200)["timeline_json"] in ("", None)
    assert exporter._backfill_timelines_if_needed(limit=5) == 1
    assert json.loads(stored(exporter, 200)["timeline_json"])[0]["name"] == "e2e"
    assert stored(exporter, 202)["timeline_json"] in ("", None), "only E2E rows get a timeline"
    assert stored(exporter, 203)["timeline_json"] in ("", None), "a running run is not final yet"


def test_the_catch_up_skips_rows_no_page_reads(wfe, exporter, github, monkeypatch):
    """The dry-run, cancel-stale and netris rows outnumber the suite runs;
    fetching their jobs would spend the budget on timelines nobody reads."""
    monkeypatch.setattr(wfe, "TOKEN", "token")
    exporter._upsert_job(make_job(id=600, run_id="600", created_at="2026-10-10T10:00:00Z",
                                  workflow="E2E VMaaS Full Install"))
    for number, name in enumerate(("E2E start (dry run)", "Cancel stale e2e runs on push",
                                   "E2E VMaaS Netris Full Install", "E2E VMaaS Staging Install"), start=601):
        exporter._upsert_job(make_job(id=number, run_id=str(number), created_at="2026-10-11T10:00:00Z",
                                      workflow=name))
    github.add(f"/repos/{wfe.ORG}/osac/actions/runs/600/attempts/1/jobs?per_page=30",
               FakeResponse(payload={"jobs": [gh_job()]}))
    assert exporter._backfill_timelines_if_needed(limit=1) == 1
    assert stored(exporter, 600)["timeline_json"].startswith("[")
    assert github.calls == [f"/repos/{wfe.ORG}/osac/actions/runs/600/attempts/1/jobs?per_page=30"]
    assert exporter._backfill_timelines_if_needed(limit=5) == 0


def test_the_catch_up_keeps_a_suite_rows_own_jobs(wfe, exporter, github, monkeypatch):
    monkeypatch.setattr(wfe, "TOKEN", "token")
    exporter._upsert_job(make_job(id=9001, run_id="300", workflow="E2E caas (merge queue)",
                                  event="merge_group"))
    github.add(f"/repos/{wfe.ORG}/osac/actions/runs/300/attempts/1/jobs?per_page=30",
               FakeResponse(payload={"jobs": [
                   {**gh_job(name="vmaas / e2e-vmaas-full-install / e2e"), "id": 1},
                   {**gh_job(name="caas / e2e-caas-full-install / e2e"), "id": 2},
               ]}))
    assert exporter._backfill_timelines_if_needed(limit=5) == 1
    timeline = json.loads(stored(exporter, 9001)["timeline_json"])
    assert [job["name"] for job in timeline] == ["caas / e2e-caas-full-install / e2e"]


def test_the_catch_up_waits_for_budget_and_retries_failed_fetches(wfe, exporter, github, monkeypatch):
    monkeypatch.setattr(wfe, "TOKEN", "token")
    exporter._upsert_job(make_job(id=400, run_id="400"))
    exporter._rate_limit_remaining = 10
    assert exporter._backfill_timelines_if_needed(limit=5) == 0
    assert github.calls == [], "no call while the shared budget is low"
    exporter._rate_limit_remaining = None
    github.add(f"/repos/{wfe.ORG}/osac/actions/runs/400/attempts/1/jobs?per_page=30",
               FakeResponse(status=502), FakeResponse(payload={"jobs": []}))
    assert exporter._backfill_timelines_if_needed(limit=5) == 0
    assert stored(exporter, 400)["timeline_json"] in ("", None), "a failed fetch is retried later"
    assert exporter._backfill_timelines_if_needed(limit=5) == 1
    assert stored(exporter, 400)["timeline_json"] == "[]", "a run without jobs is not fetched again"


def test_the_catch_up_needs_a_token(wfe, exporter, github):
    exporter._upsert_job(make_job(id=500, run_id="500"))
    assert exporter._backfill_timelines_if_needed(limit=5) == 0
    assert github.calls == []


def test_the_rate_limit_header_is_remembered(wfe, exporter):
    exporter._update_rate_limit(FakeResponse(headers={"X-RateLimit-Remaining": "4321"}))
    assert exporter._rate_limit_remaining == 4321
