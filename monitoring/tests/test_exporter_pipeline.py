"""How workflow-exporter.py counts the pull-request flow's runs as "e2e"."""

from __future__ import annotations

import pytest

from helpers import FakeResponse, make_gh_job, make_job, make_run

SUITE = "caas / e2e-caas-full-install / e2e"
FLOW = "E2E for pull requests"


@pytest.fixture
def W(wfe):
    return wfe.WorkflowExporter


@pytest.fixture
def R(wfe):
    return lambda path: f"/repos/{wfe.ORG}/{path}"


def rows(exporter, sql, *args):
    with exporter._db() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def job(name, conclusion="success", status="completed", started=True):
    return {
        "name": name,
        "conclusion": conclusion,
        "status": status,
        "steps": [{"name": "Run E2E tests", "conclusion": conclusion, "started_at": "t" if started else None}],
    }


def refined(W, jobs, active=False, workflow=FLOW):
    record = {"workflow": workflow, "category": "automation"}
    W._refine_pipeline_category(record, jobs, active)
    return record["category"]


# which workflow a run belongs to -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("run", "name"),
    [
        ({"name": "E2E #12 (suites checked in the comment)", "path": ".github/workflows/e2e-pr.yml"}, FLOW),
        ({"path": ".github/workflows/e2e-queue.yml@refs/heads/main"}, "E2E for the merge queue"),
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


def test_gate_detection_by_name_is_unchanged(W):
    for name in ("decide / Decide", "track / Suite checks", "finish / Record the result", "plan / Plan", "plan"):
        assert not W._is_gate_job(name), name
    assert W._is_gate_job("e2e-caas-gate")
    assert W._is_gate_job("e2e-readiness")


# the category of a run --------------------------------------------------------------------------------------------


def test_decide_only_run_is_automation(W):
    assert refined(W, [job("decide / Decide"), job("caas", "skipped"), job("finish", "skipped")]) == "automation"


def test_a_suite_that_ran_is_e2e(W):
    assert refined(W, [job("decide / Decide"), job(SUITE)]) == "e2e"
    assert refined(W, [job("decide / Decide"), job(SUITE, "failure")]) == "e2e"


def test_a_skipped_suite_is_not_e2e(W):
    assert refined(W, [job("decide / Decide"), job(SUITE, "skipped")]) == "automation"


def test_a_suite_cancelled_before_it_started_is_not_e2e(W):
    assert refined(W, [job(SUITE, "cancelled", started=False)]) == "automation"
    assert refined(W, [job(SUITE, "cancelled", started=True)]) == "e2e"


def test_a_suite_waiting_for_a_runner_counts_only_while_the_run_is_active(W):
    waiting = job(SUITE, None, status="queued")
    assert refined(W, [waiting], active=True) == "e2e"
    assert refined(W, [waiting], active=False) == "automation"


def test_no_jobs_is_automation(W):
    assert refined(W, []) == "automation"
    assert refined(W, None) == "automation"


def test_other_workflows_are_untouched(W):
    record = {"workflow": "E2E CaaS Full Install", "category": "e2e"}
    W._refine_pipeline_category(record, [job("decide / Decide")])
    assert record["category"] == "e2e"


def test_orchestration_failure_is_not_counted_as_an_e2e_failure(W):
    jobs = [job("decide / Decide", "failure"), job(SUITE, "skipped")]
    W._refine_pipeline_category({"workflow": FLOW, "category": "automation"}, jobs)
    assert W._is_gate_only_failure(jobs)
    assert W._extract_failed_steps(W, jobs) == []
    assert W._extract_step_durations(W, jobs) == []


def test_a_failed_suite_next_to_a_failed_orchestration_job_is_still_a_failure(W):
    jobs = [job("finish / Record the result", "failure"), job(SUITE, "failure")]
    W._refine_pipeline_category({"workflow": FLOW, "category": "automation"}, jobs)
    assert not W._is_gate_only_failure(jobs)
    assert len(W._extract_failed_steps(W, jobs)) == 1


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
    assert W._extract_step_durations(W, [timed]) == [{"name": "Run plan", "duration_s": 30}]


# rows stored before the flow had its own handling -------------------------------------------------------------------------


def stored(exporter, *specs):
    for i, (workflow, category, runner, reason) in enumerate(specs, start=1):
        exporter._upsert_job(make_job(
            id=i, workflow=workflow, display_name=f"osac / {workflow}", category=category,
            runner_name=runner, failure_reason=reason,
        ))


def test_run_names_become_the_workflow_and_the_runner_decides_the_category(exporter):
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
    )
    exporter._normalize_pipeline_rows_if_needed()
    got = {r["id"]: (r["workflow"], r["category"], r["failure_reason"])
           for r in rows(exporter, "SELECT * FROM jobs")}
    assert got[1] == (FLOW, "automation", "n/a")
    assert got[2] == (FLOW, "e2e", "")
    assert got[3] == ("E2E review relay", "automation", "n/a")
    assert got[4] == ("E2E CaaS Full Install", "e2e", "")  # a real suite workflow stays
    # Nothing else is touched: other E2E workflows, bot workflows, CI, odd names.
    assert got[5] == (f"E2E CaaS Netris Full Install / PR #1748 @ {'b' * 40}", "e2e", "")
    assert got[6] == ("E2E on CodeRabbit approval", "automation", "")
    assert got[7] == ("CI", "ci", "")
    assert got[8] == ("E2E vmaas:nightly @ not-a-sha", "e2e", "")


def test_a_category_the_ingestion_derived_is_not_overridden(exporter):
    # Already under the stable name: a run cancelled before any step started has a runner but is "automation"
    # by its jobs; a suite that ran has "e2e" and keeps it.
    stored(
        exporter,
        (FLOW, "automation", "osac-1-runner-04", "n/a"),
        (FLOW, "e2e", "osac-1-runner-04", ""),
        ("E2E for the merge queue", "e2e", "", "infra"),  # a legacy row that never used a machine
    )
    exporter._normalize_pipeline_rows_if_needed()
    got = {r["id"]: (r["category"], r["failure_reason"]) for r in rows(exporter, "SELECT * FROM jobs")}
    assert got[1] == ("automation", "n/a")
    assert got[2] == ("e2e", "")
    assert got[3] == ("automation", "n/a")


def test_running_the_migration_twice_changes_nothing_more(exporter):
    stored(exporter, ("E2E #12 (suites checked in the comment)", "e2e", "", ""))
    exporter._normalize_pipeline_rows_if_needed()
    first = rows(exporter, "SELECT * FROM jobs")
    exporter._normalize_pipeline_rows_if_needed()
    assert rows(exporter, "SELECT * FROM jobs") == first


def test_startup_recategorize_leaves_the_flow_alone(exporter):
    stored(exporter, (FLOW, "automation", "", "n/a"))
    exporter._recategorize_jobs_if_needed()
    assert rows(exporter, "SELECT category FROM jobs")[0]["category"] == "automation"


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
    assert cancelled["category"] == "e2e"  # a cancelled run may have started a suite: the jobs decide, not this rule


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
    exporter._upsert_job(make_job(id=3, workflow=FLOW, category="e2e", conclusion="skipped"))
    exporter._recategorize_jobs_if_needed()
    got = {r["id"]: r["category"] for r in rows(exporter, "SELECT id, category FROM jobs")}
    assert got == {1: "automation", 2: "e2e", 3: "e2e"}  # the flow's own rows are decided by their jobs
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


def test_the_jobs_of_an_active_flow_run_decide_its_category(exporter, github, R):
    running = job(SUITE, None, status="in_progress") | {"runner_name": "box-1"}
    active_world(github, R, FakeResponse(payload={"jobs": [running]}))
    runs, complete = exporter._fetch_active_runs("osac")
    assert complete
    assert runs[0]["workflow"] == FLOW and runs[0]["category"] == "e2e"


def test_a_flow_run_is_counted_under_its_stable_workflow_name(exporter, wfe, github, R):
    github.add(R("osac/actions/runs/1/jobs?filter=latest&per_page=30"),
               FakeResponse(payload={"jobs": [make_gh_job("decide / Decide")]}))
    run = flow_run() | {"status": "completed", "conclusion": "success"}
    before = wfe.completed_runs.labels(org=wfe.ORG, repo="osac", workflow=FLOW, conclusion="success")._value.get()
    assert exporter._process_completed_run("osac", run)
    after = wfe.completed_runs.labels(org=wfe.ORG, repo="osac", workflow=FLOW, conclusion="success")._value.get()
    assert after == before + 1
    assert rows(exporter, "SELECT workflow, category FROM jobs") == [{"workflow": FLOW, "category": "automation"}]
