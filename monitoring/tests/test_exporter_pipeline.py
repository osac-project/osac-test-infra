"""How workflow-exporter.py records the pull-request flow's runs: the run is "automation", and each suite that produced
a result (vmaas, caas, bmaas, netris ...) is an "e2e" row of its own."""

from __future__ import annotations

import itertools

import pytest

from helpers import FakeResponse, make_gh_job, make_job, make_run

SUITE = "caas / e2e-caas-full-install / e2e"
FLOW = "E2E for pull requests"
QUEUE = "E2E for the merge queue"
IDS = itertools.count(1000)


@pytest.fixture
def W(wfe):
    return wfe.WorkflowExporter


@pytest.fixture
def R(wfe):
    return lambda path: f"/repos/{wfe.ORG}/{path}"


def rows(exporter, sql, *args):
    with exporter._db() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def job(name, conclusion="success", status="completed", started=True, runner="box-runner-01"):
    return {
        "id": next(IDS),
        "name": name,
        "conclusion": conclusion,
        "status": status,
        "runner_name": runner,
        "html_url": f"https://github.com/osac-project/osac/actions/runs/1/job/{next(IDS)}",
        "started_at": "2026-10-19T10:00:00Z" if started else None,
        "completed_at": "2026-10-19T10:30:00Z" if status == "completed" and started else None,
        "steps": [
            {
                "name": "Run E2E tests", "conclusion": conclusion, "status": status,
                "started_at": "2026-10-19T10:00:00Z" if started else None,
                "completed_at": "2026-10-19T10:30:00Z" if status == "completed" and started else None,
            }
        ],
    }


def suite_rows(W, exporter, jobs, workflow=FLOW, active=False):
    """The run's row (refined as the ingestion does) and the suite rows built from its jobs."""
    record = {"id": 1, "repo": "osac", "workflow": workflow, "category": "automation", "url": "run-url",
              "run_attempt": 1, "run_number": 5}
    W._refine_pipeline_category(record, jobs, active)
    return record, exporter._suite_records(record, jobs, active)


def by_workflow(rows_):
    return {r["workflow"]: r for r in rows_}


# which workflow a run belongs to -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("run", "name"),
    [
        ({"name": "E2E #12 (suites checked in the comment)", "path": ".github/workflows/e2e-pr.yml"}, FLOW),
        ({"path": ".github/workflows/e2e-queue.yml@refs/heads/main"}, QUEUE),
        ({"path": ".github/workflows/ci.yml"}, ""),
        ({}, ""),
    ],
)
def test_the_workflow_file_names_the_run(W, run, name):
    assert W._pipeline_workflow_name(run) == name


def test_suite_job_names(W):
    for name in (SUITE, "vmaas / e2e-vmaas-full-install / e2e", "e2e-bmaas-netris-full-install"):
        assert W._is_suite_job({"name": name}), name
    for name in ("decide / Decide", "track / Suite checks", "finish / Record the result", "caas", "plan"):
        assert not W._is_suite_job({"name": name}), name


def test_the_suite_is_the_caller_job_of_its_jobs(W):
    assert W._suite_of({"name": "vmaas / e2e-vmaas-full-install / e2e"}) == "vmaas"
    assert W._suite_of({"name": "netris / e2e-bmaas-netris-full-install / provision"}) == "netris"
    assert W._suite_of({"name": "decide / Decide"}) == ""
    assert W._suite_of({"name": "e2e-bmaas-netris-full-install"}) == ""  # no caller job: not a flow job


def test_gate_detection_by_name_is_unchanged(W):
    for name in ("decide / Decide", "track / Suite checks", "finish / Record the result", "plan / Plan", "plan"):
        assert not W._is_gate_job(name), name
    assert W._is_gate_job("e2e-caas-gate")
    assert W._is_gate_job("e2e-readiness")


# the run itself is never e2e ------------------------------------------------------------------------------------------------


def test_the_run_stays_automation_even_when_suites_ran(W, exporter):
    record, _ = suite_rows(W, exporter, [job("decide / Decide"), job(SUITE)])
    assert record["category"] == "automation"


def test_other_workflows_are_untouched(W, exporter):
    jobs = [job("decide / Decide"), job(SUITE)]
    record = {"id": 1, "repo": "osac", "workflow": "E2E CaaS Full Install", "category": "e2e"}
    W._refine_pipeline_category(record, jobs)
    assert record["category"] == "e2e"
    assert exporter._suite_records(record, jobs) == []
    assert not any(j.get("pipeline_suite") or j.get("pipeline_plumbing") for j in jobs)


def test_the_jobs_of_the_run_hold_no_steps_of_the_run(W, exporter):
    jobs = [job("decide / Decide", "failure"), job(SUITE, "failure")]
    suite_rows(W, exporter, jobs)
    assert W._extract_failed_steps(W, jobs) == []
    assert W._extract_step_durations(W, jobs) == []


def test_other_workflows_keep_every_job_even_when_it_is_named_like_ours(W):
    # A job called "plan / x" or "track / x" in any other workflow is a real job there.
    jobs = [job("plan / Plan", "failure"), job("track / Build", "failure")]
    W._refine_pipeline_category({"workflow": "CI", "category": "ci"}, jobs)
    assert not W._is_gate_only_failure(jobs)
    assert len(W._extract_failed_steps(W, jobs)) == 2
    timed = job("plan / Plan")
    timed["steps"] = [
        {"name": "Run plan", "status": "completed", "started_at": "2026-10-18T00:00:00Z",
         "completed_at": "2026-10-18T00:00:30Z"}
    ]
    timed["conclusion"] = "success"
    assert W._extract_step_durations(W, [timed]) == [{"name": "Run plan", "duration_s": 30}]


# one e2e row per suite that produced a result -----------------------------------------------------------------------


def test_each_suite_that_ran_is_one_e2e_row(W, exporter):
    jobs = [
        job("decide / Decide"),
        job("vmaas / e2e-vmaas-full-install / e2e"),
        job("caas / e2e-caas-full-install / e2e", "failure"),
        job("bmaas / e2e-bmaas-full-install / e2e", "skipped", started=False),
        job("finish / Record the result"),
    ]
    _, found = suite_rows(W, exporter, jobs)
    got = by_workflow(found)
    assert sorted(got) == ["E2E caas (pull requests)", "E2E vmaas (pull requests)"]
    assert {r["category"] for r in found} == {"e2e"}
    assert got["E2E vmaas (pull requests)"]["conclusion"] == "success"
    assert got["E2E caas (pull requests)"]["conclusion"] == "failure"
    assert got["E2E vmaas (pull requests)"]["id"] == jobs[1]["id"]
    assert got["E2E vmaas (pull requests)"]["display_name"] == "osac / E2E vmaas (pull requests)"
    assert got["E2E vmaas (pull requests)"]["duration_s"] == 1800
    assert got["E2E vmaas (pull requests)"]["runner_name"] == "box-runner-01"
    assert all(r["suite"] and r["run_id"] == 1 for r in found)


def test_a_suite_of_the_merge_queue_says_so(W, exporter):
    _, found = suite_rows(W, exporter, [job("vmaas / e2e-vmaas-full-install / e2e")], workflow=QUEUE)
    assert [r["workflow"] for r in found] == ["E2E vmaas (merge queue)"]


def test_a_cancelled_suite_is_no_e2e_result(W, exporter):
    # Replaced by a newer push or dropped from the queue: shown in the run's row, in no e2e row or count.
    for started in (True, False):
        jobs = [job("decide / Decide"), job("vmaas / e2e-vmaas-full-install / e2e", "cancelled", started=started)]
        record, found = suite_rows(W, exporter, jobs)
        assert found == [] and record["category"] == "automation"


def test_a_skipped_or_missing_suite_gives_no_row(W, exporter):
    assert suite_rows(W, exporter, [job("decide / Decide"), job("caas", "skipped")])[1] == []
    assert suite_rows(W, exporter, [])[1] == []
    assert suite_rows(W, exporter, None)[1] == []


def test_a_failed_suite_carries_its_failed_step_and_reason(W, exporter):
    failing = job("vmaas / e2e-vmaas-full-install / e2e", "failure")
    failing["steps"] = [{"name": "Run E2E tests", "conclusion": "failure", "status": "completed",
                         "started_at": "2026-10-19T10:00:00Z", "completed_at": "2026-10-19T10:30:00Z"}]
    _, found = suite_rows(W, exporter, [job("decide / Decide", "failure"), failing])
    (row,) = found
    assert row["failed_step"] == "vmaas / e2e-vmaas-full-install / e2e → Run E2E tests"
    assert row["failure_reason"] == "test"
    assert [f["step"] for f in row["_failed"]] == ["Run E2E tests"]


def test_a_suite_with_several_jobs_is_one_row_decided_by_them(W, exporter):
    name = "netris / e2e-bmaas-netris-full-install / "
    ok = [job(name + n) for n in ("authorize", "provision", "push-secrets", "e2e", "teardown")]
    assert [r["conclusion"] for r in suite_rows(W, exporter, ok)[1]] == ["success"]
    # provision failed, the test job never ran, teardown still ran: a failure of the suite.
    broken = [job(name + "authorize"), job(name + "provision", "failure"), job(name + "push-secrets", "skipped"),
              job(name + "e2e", "skipped"), job(name + "teardown")]
    (row,) = suite_rows(W, exporter, broken)[1]
    assert row["conclusion"] == "failure" and row["workflow"] == "E2E netris (pull requests)"
    # only the authorization passed: no test ran.
    unauthorized = [job(name + "authorize"), job(name + "provision", "skipped"), job(name + "e2e", "skipped")]
    assert suite_rows(W, exporter, unauthorized)[1] == []


def test_a_suite_still_running_or_waiting_is_a_row_only_while_the_run_is_active(W, exporter):
    running = job("vmaas / e2e-vmaas-full-install / e2e", None, status="in_progress")
    waiting = job("caas / e2e-caas-full-install / e2e", None, status="queued", started=False)
    _, found = suite_rows(W, exporter, [running, waiting], active=True)
    got = by_workflow(found)
    assert got["E2E vmaas (pull requests)"]["status"] == "in_progress"
    assert got["E2E caas (pull requests)"]["status"] == "queued"
    assert got["E2E vmaas (pull requests)"]["conclusion"] == ""
    assert suite_rows(W, exporter, [running, waiting], active=False)[1] == []


# rows stored before the flow had its own handling -------------------------------------------------------------------------


def stored(exporter, *specs):
    for i, (workflow, category, runner, reason) in enumerate(specs, start=1):
        exporter._upsert_job(make_job(
            id=i, workflow=workflow, display_name=f"osac / {workflow}", category=category,
            runner_name=runner, failure_reason=reason,
        ))


def test_stored_flow_runs_get_the_stable_name_and_are_automation(exporter):
    sha = "a" * 40
    stored(
        exporter,
        (f"E2E fork/osac:fix/x @ {sha}", "e2e", "", "infra"),
        ("E2E #12 (suites checked in the comment)", "e2e", "osac-1-runner-04", ""),
        ("E2E review relay", "e2e", "", ""),
        ("E2E CaaS Full Install", "e2e", "", ""),
        (f"E2E CaaS Netris Full Install / PR #1748 @ {'b' * 40}", "e2e", "", ""),
        ("E2E on CodeRabbit approval", "automation", "", ""),
        ("CI", "ci", "", ""),
        ("E2E vmaas:nightly @ not-a-sha", "e2e", "", ""),
        (FLOW, "e2e", "osac-1-runner-04", ""),
        ("E2E vmaas (pull requests)", "e2e", "osac-1-runner-04", ""),
    )
    exporter._normalize_pipeline_rows_if_needed()
    got = {r["id"]: (r["workflow"], r["category"], r["failure_reason"])
           for r in rows(exporter, "SELECT * FROM jobs")}
    assert got[1] == (FLOW, "automation", "n/a")
    assert got[2] == (FLOW, "automation", "n/a")
    assert got[3] == ("E2E review relay", "automation", "n/a")
    assert got[4] == ("E2E CaaS Full Install", "e2e", "")  # a real suite workflow stays
    # Nothing else is touched: other E2E workflows, bot workflows, CI, odd names.
    assert got[5] == (f"E2E CaaS Netris Full Install / PR #1748 @ {'b' * 40}", "e2e", "")
    assert got[6] == ("E2E on CodeRabbit approval", "automation", "")
    assert got[7] == ("CI", "ci", "")
    assert got[8] == ("E2E vmaas:nightly @ not-a-sha", "e2e", "")
    assert got[9] == (FLOW, "automation", "n/a")  # a run stored as e2e under its stable name
    assert got[10] == ("E2E vmaas (pull requests)", "e2e", "")  # a suite row is no flow run


def test_running_the_migration_twice_changes_nothing_more(exporter):
    stored(exporter, ("E2E #12 (suites checked in the comment)", "e2e", "", ""))
    exporter._normalize_pipeline_rows_if_needed()
    first = rows(exporter, "SELECT * FROM jobs")
    exporter._normalize_pipeline_rows_if_needed()
    assert rows(exporter, "SELECT * FROM jobs") == first


def test_startup_recategorize_leaves_the_flow_alone_and_the_suites_e2e(exporter):
    stored(exporter, (FLOW, "automation", "", "n/a"), ("E2E vmaas (pull requests)", "e2e", "box", ""))
    exporter._recategorize_jobs_if_needed()
    assert [r["category"] for r in rows(exporter, "SELECT category FROM jobs ORDER BY id")] == ["automation", "e2e"]


# a run GitHub skipped as a whole ------------------------------------------------------------------------------------------


def test_a_skipped_run_of_an_e2e_workflow_is_not_e2e(exporter):
    # A Netris caller on pull_request_target whose `if` was false: no job ran, whatever its name says.
    skipped = exporter._make_job_record(
        make_run(name="E2E BMaaS Netris Full Install", event="pull_request_target", conclusion="skipped"),
        "osac-test-infra",
    )
    ran = exporter._make_job_record(make_run(name="E2E BMaaS Netris Full Install"), "osac-test-infra")
    cancelled = exporter._make_job_record(
        make_run(name="E2E BMaaS Netris Full Install", conclusion="cancelled"), "osac-test-infra"
    )
    assert skipped["category"] == "automation"
    assert ran["category"] == "e2e"
    assert cancelled["category"] == "e2e"  # a suite workflow of its own: only the flow's runs are split by suite


def test_the_comment_reservation_workflow_is_never_e2e(W):
    # It posts a placeholder comment on a GitHub-hosted runner; its runs are skipped or succeed, without a test.
    assert W._category_of_run("Reserve E2E comment slot on start", "success") == "automation"
    assert W._category_of_run("Reserve E2E comment slot on start", "skipped") == "automation"
    assert W._category_of_run("E2E VMaaS Full Install", "success") == "e2e"


def test_a_skipped_run_of_any_other_category_keeps_it(exporter):
    record = exporter._make_job_record(make_run(name="CI", conclusion="skipped"), "osac")
    assert record["category"] == "ci"


def test_startup_recategorize_demotes_stored_skipped_e2e_runs(exporter):
    exporter._upsert_job(make_job(id=1, workflow="E2E BMaaS Netris Full Install", category="e2e", conclusion="skipped"))
    exporter._upsert_job(make_job(id=2, workflow="E2E BMaaS Netris Full Install", category="e2e", conclusion="success"))
    exporter._upsert_job(make_job(id=3, workflow=FLOW, category="automation", conclusion="skipped"))
    exporter._recategorize_jobs_if_needed()
    got = {r["id"]: r["category"] for r in rows(exporter, "SELECT id, category FROM jobs")}
    assert got == {1: "automation", 2: "e2e", 3: "automation"}
    exporter._recategorize_jobs_if_needed()
    assert {r["id"]: r["category"] for r in rows(exporter, "SELECT id, category FROM jobs")} == got


# active and completed runs ----------------------------------------------------------------------------------------------


def flow_run(**overrides):
    return make_run(
        id=1, name="E2E #12 (suites asked for in a comment)", event="issue_comment",
        path=".github/workflows/e2e-pr.yml", status="in_progress", conclusion=None, **overrides,
    )


def active_world(github, R, jobs_response):
    github.add(R("osac/actions/runs?status=queued&per_page=100"),
               FakeResponse(payload={"workflow_runs": [], "total_count": 0}))
    github.add(R("osac/actions/runs?status=in_progress&per_page=100"),
               FakeResponse(payload={"workflow_runs": [flow_run()], "total_count": 1}))
    github.add(R("osac/actions/runs/1/jobs?filter=latest&per_page=30"), jobs_response)


def test_a_flow_run_whose_jobs_cannot_be_read_makes_the_fetch_incomplete(exporter, github, R):
    active_world(github, R, FakeResponse(502))
    runs, complete = exporter._fetch_active_runs("osac")
    assert not complete
    assert [r["category"] for r in runs] == ["automation"]


def test_the_suites_of_an_active_flow_run_are_active_e2e_rows(exporter, github, R):
    running = job("vmaas / e2e-vmaas-full-install / e2e", None, status="in_progress")
    active_world(github, R, FakeResponse(payload={"jobs": [job("decide / Decide"), running]}))
    runs, complete = exporter._fetch_active_runs("osac")
    assert complete
    assert [(r["workflow"], r["category"], r.get("suite", False)) for r in runs] == [
        (FLOW, "automation", False),
        ("E2E vmaas (pull requests)", "e2e", True),
    ]
    assert runs[0]["id"] == 1 and runs[1]["id"] == running["id"]


def test_a_completed_flow_run_is_stored_with_a_row_and_counts_per_suite(exporter, wfe, github, R):
    jobs = [
        job("decide / Decide"),
        job("vmaas / e2e-vmaas-full-install / e2e"),
        job("caas / e2e-caas-full-install / e2e", "failure"),
        job("bmaas / e2e-bmaas-full-install / e2e", "cancelled"),
    ]
    github.add(R("osac/actions/runs/1/jobs?filter=latest&per_page=30"), FakeResponse(payload={"jobs": jobs}))
    run = flow_run() | {"status": "completed", "conclusion": "failure"}

    def count(workflow, conclusion):
        return wfe.completed_runs.labels(
            org=wfe.ORG, repo="osac", workflow=workflow, conclusion=conclusion
        )._value.get()

    def failed(workflow):
        return wfe.failed_step_total.labels(org=wfe.ORG, workflow=workflow, step="Run E2E tests")._value.get()

    before = (count(FLOW, "failure"), count("E2E vmaas (pull requests)", "success"),
              count("E2E caas (pull requests)", "failure"), failed("E2E caas (pull requests)"), failed(FLOW))
    assert exporter._process_completed_run("osac", run)
    after = (count(FLOW, "failure"), count("E2E vmaas (pull requests)", "success"),
             count("E2E caas (pull requests)", "failure"), failed("E2E caas (pull requests)"), failed(FLOW))
    assert [a - b for a, b in zip(after, before)] == [1, 1, 1, 1, 0]
    got = {r["workflow"]: (r["category"], r["conclusion"])
           for r in rows(exporter, "SELECT workflow, category, conclusion FROM jobs")}
    assert got == {
        FLOW: ("automation", "failure"),
        "E2E vmaas (pull requests)": ("e2e", "success"),
        "E2E caas (pull requests)": ("e2e", "failure"),
    }  # the cancelled suite has no e2e row, the run's row shows it
    assert not exporter._process_completed_run("osac", run)  # the same attempt again: nothing stored twice


def test_a_backfilled_flow_run_stores_its_suite_rows_too(exporter, wfe, github, R):
    # The same rows whichever way the run is found: the backfill of a time range, a page of runs, the first load.
    jobs = [job("decide / Decide"), job("vmaas / e2e-vmaas-full-install / e2e")]
    run = flow_run() | {"status": "completed", "conclusion": "success"}
    github.add(R("osac/actions/runs/1/jobs?filter=latest&per_page=30"), FakeResponse(payload={"jobs": jobs}))
    github.add(R("osac/actions/runs?page=1"), FakeResponse(payload={"workflow_runs": [run]}))
    exporter._backfill_page("osac", f"{wfe.API_URL}/repos/{wfe.ORG}/osac/actions/runs?page=1")
    assert sorted(r["workflow"] for r in rows(exporter, "SELECT workflow FROM jobs")) == [
        FLOW, "E2E vmaas (pull requests)"]


def test_a_run_with_no_suite_result_is_one_automation_row(exporter, github, R):
    github.add(R("osac/actions/runs/1/jobs?filter=latest&per_page=30"),
               FakeResponse(payload={"jobs": [make_gh_job("decide / Decide")]}))
    run = flow_run() | {"status": "completed", "conclusion": "success"}
    assert exporter._process_completed_run("osac", run)
    assert rows(exporter, "SELECT workflow, category FROM jobs") == [{"workflow": FLOW, "category": "automation"}]


def test_the_active_run_tracking_follows_runs_not_suite_rows(exporter, github, R, monkeypatch):
    # A suite row is keyed by its job id: a run that is still active must not be looked up under it once it drops out.
    running = job("vmaas / e2e-vmaas-full-install / e2e", None, status="in_progress")
    active_world(github, R, FakeResponse(payload={"jobs": [running]}))
    github.add(R("osac/actions/runs?status=completed&per_page=50"),
               FakeResponse(payload={"workflow_runs": []}))
    exporter.active_runs = [{"id": 1, "repo": "osac"}, {"id": running["id"], "repo": "osac", "suite": True}]
    monkeypatch.setattr(exporter, "_run_count", lambda repo, status: 1)
    monkeypatch.setattr(exporter, "get_active_repos", lambda: ["osac"])
    monkeypatch.setattr(exporter, "_refresh_pr_map", lambda repos: None)
    monkeypatch.setattr(exporter, "_backfill_missing_pr_data", lambda: None)
    monkeypatch.setattr(exporter, "_backfill_queue_data_if_needed", lambda: None)
    exporter.collect()
    assert not any(f"/actions/runs/{running['id']}" in call for call in github.calls)
    assert [r["id"] for r in exporter.active_runs] == [1, running["id"]]
