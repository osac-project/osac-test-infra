"""How workflow-exporter.py records the integration legs of the osac
mono-repo's three Tests-workflow callers.

The reusable tests.yml runs one job per unit- or integration-test leg, and
its callers (ci_observe_phases.IT_WORKFLOW, CI_WORKFLOW, FLAKE_WORKFLOW)
name their runs after themselves: "Integration Tests" runs every
integration leg on a schedule (2x/day), by hand and from the nightly
build; "CI" runs on every pull request and merge queue entry, its
paths-filter plan picking the legs the change touches -- zero or more per
run, next to its unit, contract, helm and ui checks, and the bulk of all
integration volume; "Flake detection" runs campaigns (~2x/week) whose
repetition matrix re-runs every leg a hundred-plus times on one fixed
commit.

The four in-scope legs (it-fulfillment-service, it-osac-operator,
it-bare-metal-fulfillment-operator, it-caas) become e2e rows of their own,
one per leg job a run actually contains and each with the leg's own
conclusion, under their suite's workflow name, so every ci-observe reader
maps them like any other suite run -- the same shape the pull-request
flow's suites follow (_suite_records). A campaign's rows wear the suite's
flake-detection name, which is what the Started by filter reads "flake"
off and what keeps them out of the default population. The run's own row
keeps its workflow's name and category and holds none of the legs' steps;
every other leg (unit tests, contract checks, the installer, preflight and
AAP suites) stays a job of it. A leg that was skipped, never started or
was cancelled produced no result and gets no row: the run's own row still
shows it.

A called leg carries its caller's job name as a prefix ("Integration test
(fulfillment-service) / it-fulfillment-service", and one part deeper in a
campaign: "Integration repetition 4 / Integration test (CaaS) /
it-caas"), so the last " / " part names the kind. A row stored before this
refinement ran keeps no leg rows: the leg catch-up
(_backfill_integration_legs_if_needed) examines each stored run once and
marks it, so _needs_upsert leaves stored rows alone instead of re-fetching
them on every pass.
"""

from __future__ import annotations

import json

import pytest

from helpers import FakeResponse, make_run, make_step

TESTS = "Integration Tests"
CI = "CI"
FLAKE = "Flake detection"

# The leg kinds ci_observe_phases counts, and their suites' stored workflow
# names.
IN_SCOPE = {
    "it-fulfillment-service": "IT fulfillment (Tests)",
    "it-osac-operator": "IT operator (Tests)",
    "it-bare-metal-fulfillment-operator": "IT BMF (Tests)",
    "it-caas": "IT CaaS (Tests)",
}

OUT_OF_SCOPE = [
    "go-unit",
    "metering-contract",
    "ui-unit",
    "caas-unit",
    "helm-unit",
    "it-osac-aap",
    "it-osac-installer",
    "it-osac-installer-preflight-helm3",
    "fulfillment-service",
]


@pytest.fixture
def W(wfe):
    return wfe.WorkflowExporter


@pytest.fixture
def R(wfe):
    return lambda path: f"/repos/{wfe.ORG}/{path}"


def rows(exporter, sql, *args):
    with exporter._db() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def a_run(workflow, **overrides):
    overrides.setdefault("event", "pull_request")
    return make_run(id=1, name=workflow, **overrides)


def leg_job(name, conclusion="success", status="completed", job_id=1000, steps=None):
    """A called Tests job: the leg is the last " / " part of its name, and
    its steps are the ones tests.yml runs for its kind."""
    return {
        "id": job_id,
        "name": name,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "started_at": "2026-11-24T12:14:00Z" if status != "queued" else None,
        "completed_at": "2026-11-24T12:40:00Z" if status == "completed" else None,
        "runner_name": "GitHub Actions 7",
        "html_url": f"https://github.com/osac-project/osac/actions/runs/1/job/{job_id}",
        "run_attempt": 1,
        "steps": steps if steps is not None else [
            make_step("Install infrastructure on Kind"),
            make_step("Run integration tests", conclusion=conclusion),
        ],
    }


def leg_record(workflow):
    return {"id": 1, "repo": "osac", "workflow": workflow, "category": "ci",
            "display_name": f"osac / {workflow}", "url": "run-url", "run_attempt": 1,
            "run_number": 5}


def suite_rows(W, exporter, workflow, jobs, active=False):
    """The run's row (refined as the ingestion does) and the leg rows built
    from its jobs."""
    record = leg_record(workflow)
    W._refine_run_record(record, jobs, active)
    return record, exporter._suite_records(record, jobs, active)


def by_workflow(rows_):
    return {r["workflow"]: r for r in rows_}


# one row per in-scope leg ----------------------------------------------------------------------


@pytest.mark.parametrize(("kind", "workflow"), sorted(IN_SCOPE.items()))
def test_an_in_scope_leg_becomes_a_suite_row_of_its_own(W, exporter, kind, workflow):
    record, [row] = suite_rows(W, exporter, TESTS, [leg_job(kind, job_id=77)])
    assert row["workflow"] == workflow
    assert row["display_name"] == f"osac / {workflow}"
    assert row["category"] == "e2e"
    assert row["conclusion"] == "success"
    assert row["id"] == 77, "the row's id is the leg job's, not the run's"
    assert row["suite"] is True
    assert row["run_id"] == record["id"]
    assert [s["name"] for s in row["steps"]] == ["Install infrastructure on Kind", "Run integration tests"]


def test_a_called_leg_names_its_kind_after_the_callers_prefix(W, exporter):
    _, [row] = suite_rows(W, exporter, CI, [leg_job("Integration test (CaaS) / it-caas")])
    assert row["workflow"] == "IT CaaS (Tests)"


def test_the_run_row_keeps_the_workflows_name_category_and_own_steps(W, exporter):
    installer_steps = [make_step("Lint charts"), make_step("Deploy charts")]
    jobs = [leg_job("Integration test (osac-installer) / it-osac-installer", job_id=2, steps=installer_steps),
            leg_job("Integration test (fulfillment-service) / it-fulfillment-service", job_id=3)]
    record, leg_rows = suite_rows(W, exporter, CI, jobs)
    assert record == {"id": 1, "repo": "osac", "workflow": CI, "category": "ci",
                      "display_name": f"osac / {CI}", "url": "run-url", "run_attempt": 1,
                      "run_number": 5}
    assert len(leg_rows) == 1, "the out-of-scope installer leg is no suite row"
    assert [s["name"] for s in exporter._extract_step_durations(jobs)] == ["Lint charts", "Deploy charts"], (
        "the run row holds the installer leg's steps but none of a leg stored as its own row"
    )


@pytest.mark.parametrize("kind", OUT_OF_SCOPE)
def test_every_other_leg_stays_a_job_of_the_runs_own_row(W, exporter, kind):
    jobs = [leg_job(f"Integration test (other) / {kind}")]
    record, leg_rows = suite_rows(W, exporter, CI, jobs)
    assert leg_rows == []
    assert not any(j.get("pipeline_suite") for j in jobs)
    assert record["workflow"] == CI


def test_a_leg_that_produced_no_result_gets_no_row(W, exporter):
    jobs = [leg_job("Integration test (CaaS) / it-caas", conclusion="cancelled"),
            leg_job("Integration test (osac-operator) / it-osac-operator", status="queued")]
    _, leg_rows = suite_rows(W, exporter, CI, jobs)
    assert leg_rows == []


def test_a_running_leg_of_an_active_ci_run_lists_as_in_progress(W, exporter):
    jobs = [leg_job("Integration test (CaaS) / it-caas", status="in_progress", job_id=9)]
    _, [row] = suite_rows(W, exporter, CI, jobs, active=True)
    assert row["status"] == "in_progress"
    assert row["conclusion"] == ""


# a multi-leg CI run, live-shaped (osac run 45391420667 and its kin) ------------------------------


def ci_run_jobs():
    """A pull request's CI run: the plan, a unit leg, the out-of-scope
    installer leg, four in-scope legs and the one required-check relay job.
    A recent run also contained it-osac-installer's preflight legs; those
    are out of scope like the installer itself."""
    return [
        leg_job("Plan", job_id=1, steps=[]),
        leg_job("Unit tests (CaaS integration) / caas-unit", job_id=2,
                steps=[make_step("Check and test CaaS integration module")]),
        leg_job("Integration test (osac-installer) / it-osac-installer", job_id=3,
                steps=[make_step("Lint charts"), make_step("Deploy charts")]),
        leg_job("Integration test (bare-metal-fulfillment-operator) / it-bare-metal-fulfillment-operator",
                job_id=4),
        leg_job("Integration test (fulfillment-service) / it-fulfillment-service", job_id=5),
        leg_job("Integration test (CaaS) / it-caas", job_id=6),
        leg_job("Integration test (osac-operator) / it-osac-operator", job_id=7),
        leg_job("CI", job_id=8, steps=[make_step("Every check that applied passed")]),
    ]


def test_a_multi_leg_ci_run_stores_one_row_per_in_scope_leg(W, exporter):
    _, leg_rows = suite_rows(W, exporter, CI, ci_run_jobs())
    assert sorted(row["workflow"] for row in leg_rows) == sorted(IN_SCOPE.values())
    assert sorted(row["id"] for row in leg_rows) == [4, 5, 6, 7], "each row is its own leg job"


def test_a_failed_leg_of_a_ci_run_classifies_its_test_step(W, exporter):
    jobs = ci_run_jobs()
    jobs[4] = leg_job("Integration test (fulfillment-service) / it-fulfillment-service",
                      conclusion="failure", job_id=5)
    _, leg_rows = suite_rows(W, exporter, CI, jobs)
    row = by_workflow(leg_rows)[IN_SCOPE["it-fulfillment-service"]]
    assert row["conclusion"] == "failure"
    assert row["failure_reason"] == "test", "the integration steps are the product's own"
    assert row["failed_step"] == ("Integration test (fulfillment-service) / it-fulfillment-service"
                                  " → Run integration tests")


def test_a_completed_ci_run_is_stored_with_its_legs(W, exporter, github, R):
    github.add(
        R("osac/actions/runs/1/attempts/1/jobs?per_page=30"),
        FakeResponse(payload={"jobs": ci_run_jobs()}),
    )
    assert exporter._process_completed_run("osac", a_run(CI))
    stored_rows = rows(exporter, "SELECT id, workflow, category, conclusion FROM jobs ORDER BY id")
    assert stored_rows == [
        {"id": 1, "workflow": CI, "category": "ci", "conclusion": "success"},
        *[{"id": job_id, "workflow": workflow, "category": "e2e", "conclusion": "success"}
          for job_id, workflow in sorted(((4, "IT BMF (Tests)"), (5, "IT fulfillment (Tests)"),
                                          (6, "IT CaaS (Tests)"), (7, "IT operator (Tests)")))],
    ]
    # The same run again: every row is current, nothing is stored twice.
    assert not exporter._process_completed_run("osac", a_run(CI))
    assert len(rows(exporter, "SELECT id FROM jobs")) == 5


def test_the_runs_own_row_holds_no_in_scope_leg_steps(W, exporter, github, R):
    github.add(
        R("osac/actions/runs/1/attempts/1/jobs?per_page=30"),
        FakeResponse(payload={"jobs": ci_run_jobs()}),
    )
    assert exporter._process_completed_run("osac", a_run(CI))
    [run_row] = rows(exporter, "SELECT steps_json FROM jobs WHERE id = 1")
    steps = [s["name"] for s in json.loads(run_row["steps_json"])]
    assert "Run integration tests" not in steps, "the in-scope legs' steps live on the leg rows"
    assert "Deploy charts" in steps, "the out-of-scope installer leg's steps stay on the run row"


def test_a_cancelled_ci_run_stores_no_leg_rows(W, exporter, github, R):
    jobs = [leg_job(name, conclusion="cancelled") for name in (
        "Plan",
        "Integration test (fulfillment-service) / it-fulfillment-service",
        "Integration test (osac-installer) / it-osac-installer",
    )]
    github.add(
        R("osac/actions/runs/1/attempts/1/jobs?per_page=30"),
        FakeResponse(payload={"jobs": jobs}),
    )
    assert exporter._process_completed_run("osac", a_run(CI, conclusion="cancelled"))
    [row] = rows(exporter, "SELECT workflow, category, conclusion FROM jobs")
    assert row == {"workflow": CI, "category": "ci", "conclusion": "cancelled"}


def test_a_ci_run_with_only_out_of_scope_legs_keeps_its_own_row(W, exporter, github, R):
    github.add(
        R("osac/actions/runs/1/attempts/1/jobs?per_page=30"),
        FakeResponse(payload={"jobs": [leg_job("Plan"),
                                       leg_job("Integration test (osac-installer) / it-osac-installer"),
                                       leg_job("Unit tests (osac-ui) / ui-unit")]}),
    )
    assert exporter._process_completed_run("osac", a_run(CI))
    [row] = rows(exporter, "SELECT workflow, category, conclusion FROM jobs")
    assert row == {"workflow": CI, "category": "ci", "conclusion": "success"}


# the scheduled Integration Tests runs, every leg at once ----------------------------------------


def scheduled_jobs():
    """A scheduled Integration Tests run (osac run 45370986467): every
    integration leg at once, one of them failing, the installer and a
    preflight leg out of scope."""
    return [
        leg_job("Plan", job_id=1, steps=[]),
        leg_job("Integration test (CaaS) / it-caas", job_id=2),
        leg_job("Integration test (osac-installer preflight, Helm 3.18.0) / it-osac-installer-preflight-helm3",
                job_id=3),
        leg_job("Integration test (bare-metal-fulfillment-operator) / it-bare-metal-fulfillment-operator",
                job_id=4),
        leg_job("Integration test (osac-installer) / it-osac-installer", job_id=5,
                steps=[make_step("Lint charts"), make_step("Deploy charts")]),
        leg_job("Integration test (fulfillment-service) / it-fulfillment-service",
                conclusion="failure", job_id=6),
        leg_job("Integration test (osac-operator) / it-osac-operator", job_id=7),
    ]


def test_a_scheduled_run_counts_every_leg_with_its_own_conclusion(W, exporter):
    record, leg_rows = suite_rows(W, exporter, TESTS, scheduled_jobs())
    by_name = by_workflow(leg_rows)
    assert sorted(by_name) == ["IT BMF (Tests)", "IT CaaS (Tests)", "IT fulfillment (Tests)",
                               "IT operator (Tests)"]
    assert by_name["IT CaaS (Tests)"]["conclusion"] == "success"
    assert by_name["IT fulfillment (Tests)"]["conclusion"] == "failure"
    assert by_name["IT fulfillment (Tests)"]["failure_reason"] == "test"
    assert record["workflow"] == TESTS, "the run's own row keeps the workflow's name"
    assert record["category"] == "ci"


def test_a_scheduled_run_is_stored_with_every_leg(W, exporter, github, R):
    github.add(
        R("osac/actions/runs/1/attempts/1/jobs?per_page=30"),
        FakeResponse(payload={"jobs": scheduled_jobs()}),
    )
    assert exporter._process_completed_run(
        "osac", a_run(TESTS, event="schedule", conclusion="failure"))
    stored_rows = rows(exporter, "SELECT workflow, category, conclusion FROM jobs ORDER BY workflow")
    assert stored_rows == [
        {"workflow": "IT BMF (Tests)", "category": "e2e", "conclusion": "success"},
        {"workflow": "IT CaaS (Tests)", "category": "e2e", "conclusion": "success"},
        {"workflow": "IT fulfillment (Tests)", "category": "e2e", "conclusion": "failure"},
        {"workflow": "IT operator (Tests)", "category": "e2e", "conclusion": "success"},
        {"workflow": TESTS, "category": "ci", "conclusion": "failure"},
    ]
    # The same run again: every row is current, nothing is stored twice.
    assert not exporter._process_completed_run("osac", a_run(TESTS, event="schedule",
                                                             conclusion="failure"))


def test_a_stored_run_is_current_and_needs_no_refetch(W, exporter, github, R):
    github.add(
        R("osac/actions/runs/1/attempts/1/jobs?per_page=30"),
        FakeResponse(payload={"jobs": [leg_job("Integration test (CaaS) / it-caas")]}),
    )
    assert exporter._process_completed_run("osac", a_run(TESTS))
    # The run's own row never changes shape once its legs are stored, so
    # the listing guard leaves it alone: the leg catch-up below owns the
    # rows stored before this refinement ran.
    assert not exporter._needs_upsert(a_run(TESTS))
    assert not exporter._needs_upsert(a_run(CI))


# the leg catch-up for rows stored before the refinement -----------------------------------------


def an_old_row(exporter, run_id=1, created="2026-11-01T00:00:00Z", workflow=CI, category="ci",
               legs=()):
    """A run-level row as the old code stored it: no leg rows (unless given),
    and for the old single-suite refine's output a workflow renamed to a
    suite's name with the e2e category that refine set."""
    with exporter._db() as conn:
        conn.execute(
            "INSERT INTO jobs (id, repo, workflow, display_name, category, status, conclusion,"
            " run_attempt, run_id, url, created_at)"
            " VALUES (?, 'osac', ?, ?, ?, 'completed', 'success', 1, ?, 'u', ?)",
            (run_id, workflow, f"osac / {workflow}", category, str(run_id), created),
        )
        for job_id, name in legs:
            conn.execute(
                "INSERT INTO jobs (id, repo, workflow, display_name, category, status, conclusion,"
                " run_attempt, run_id, url, created_at)"
                " VALUES (?, 'osac', 'IT CaaS (Tests)', 'osac / IT CaaS (Tests)', 'e2e', 'completed',"
                " 'success', 1, ?, 'u', ?)",
                (job_id, str(run_id), created),
            )


def test_the_leg_catch_up_stores_the_legs_of_a_row_stored_before_it(W, exporter, github, R, wfe,
                                                                    monkeypatch):
    monkeypatch.setattr(wfe, "TOKEN", "token")
    an_old_row(exporter)
    github.add(
        R("osac/actions/runs/1/attempts/1/jobs?per_page=30"),
        FakeResponse(payload={"jobs": ci_run_jobs()}),
    )
    assert exporter._backfill_integration_legs_if_needed(limit=10) == 1
    leg_rows = rows(exporter, "SELECT id, workflow, category FROM jobs WHERE id != 1 ORDER BY id")
    assert [(r["id"], r["workflow"]) for r in leg_rows] == [
        (4, "IT BMF (Tests)"), (5, "IT fulfillment (Tests)"), (6, "IT CaaS (Tests)"),
        (7, "IT operator (Tests)"),
    ]
    # The examined mark leaves the row alone on the next pass.
    assert exporter._backfill_integration_legs_if_needed(limit=10) == 0
    assert github.count(R("osac/actions/runs/1/attempts/1/jobs?per_page=30")) == 1


def test_a_failed_fetch_leaves_the_row_for_a_later_cycle(W, exporter, github, R, wfe, monkeypatch):
    monkeypatch.setattr(wfe, "TOKEN", "token")
    an_old_row(exporter)
    github.add(
        R("osac/actions/runs/1/attempts/1/jobs?per_page=30"),
        FakeResponse(status=502, text="boom"),
    )
    assert exporter._backfill_integration_legs_if_needed(limit=10) == 0
    with exporter._db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM integration_leg_checks").fetchone()[0] == 0


def test_the_catch_up_restores_a_row_the_old_refine_renamed(W, exporter, github, R, wfe,
                                                            monkeypatch):
    """The old single-suite refine renamed a whole Integration Tests run to
    its first in-scope leg's suite; the catch-up gives the run its own name
    back and every leg a row of its own."""
    monkeypatch.setattr(wfe, "TOKEN", "token")
    an_old_row(exporter, workflow="IT CaaS (Tests)", category="e2e")
    github.add(
        R("osac/actions/runs/1/attempts/1/jobs?per_page=30"),
        FakeResponse(payload={"jobs": scheduled_jobs()}),
    )
    assert exporter._backfill_integration_legs_if_needed(limit=10) == 1
    stored_rows = rows(exporter, "SELECT id, workflow, category FROM jobs ORDER BY id")
    assert stored_rows == [
        {"id": 1, "workflow": TESTS, "category": "ci"},
        *[{"id": job_id, "workflow": workflow, "category": "e2e"}
          for job_id, workflow in sorted(((2, "IT CaaS (Tests)"), (4, "IT BMF (Tests)"),
                                          (6, "IT fulfillment (Tests)"), (7, "IT operator (Tests)")))],
    ]


def test_the_catch_up_skips_rows_created_after_the_first_examination(W, exporter, github, R, wfe,
                                                                     monkeypatch):
    monkeypatch.setattr(wfe, "TOKEN", "token")
    an_old_row(exporter, run_id=1, created="2026-11-01T00:00:00Z")
    github.add(
        R("osac/actions/runs/1/attempts/1/jobs?per_page=30"),
        FakeResponse(payload={"jobs": []}),
    )
    assert exporter._backfill_integration_legs_if_needed(limit=10) == 1
    # A row ingested since the first examination already carries its legs
    # (or proved to have none): not fetched again.
    an_old_row(exporter, run_id=2, created="2099-01-01T00:00:00Z")
    assert exporter._backfill_integration_legs_if_needed(limit=10) == 0
    assert github.count(R("osac/actions/runs/2/attempts/1/jobs?per_page=30")) == 0


def test_the_catch_up_skips_a_run_that_already_has_leg_rows(W, exporter, github, wfe, monkeypatch):
    monkeypatch.setattr(wfe, "TOKEN", "token")
    an_old_row(exporter, run_id=1, created="2026-11-01T00:00:00Z",
               legs=((2, "Integration test (CaaS) / it-caas"),))
    assert exporter._backfill_integration_legs_if_needed(limit=10) == 0
    assert github.calls == []


def test_the_catch_up_rebuilds_the_parents_own_details(W, exporter, github, R, wfe, monkeypatch):
    """The parent run's stored steps and failed step were written by an
    ingestion that counted the legs as its own jobs; the catch-up rebuilds
    both with the builders a fresh ingestion uses -- out-of-scope jobs
    kept, leg steps and a leg-pointing failed step gone -- and writes them
    back explicitly, because _upsert_job will not rewrite an unchanged
    run."""
    monkeypatch.setattr(wfe, "TOKEN", "token")
    with exporter._db() as conn:
        conn.execute(
            "INSERT INTO jobs (id, repo, workflow, display_name, category, status, conclusion, run_attempt,"
            " run_id, url, created_at, steps_json, failed_step, failure_reason)"
            " VALUES (1, 'osac', ?, 'osac / CI', 'ci', 'completed', 'failure', 1, '1', 'u',"
            " '2026-11-01T00:00:00Z', ?, ?, 'test')",
            (CI, json.dumps([{"name": "Run integration tests", "duration_s": 60},
                             {"name": "Lint charts", "duration_s": 30}]),
             "Integration test (CaaS) / it-caas → Run integration tests"),
        )
    jobs = ci_run_jobs()
    jobs[5] = leg_job("Integration test (CaaS) / it-caas", conclusion="failure", job_id=6)
    github.add(
        R("osac/actions/runs/1/attempts/1/jobs?per_page=30"),
        FakeResponse(payload={"jobs": jobs}),
    )
    assert exporter._backfill_integration_legs_if_needed(limit=10) == 1
    [parent] = rows(exporter, "SELECT steps_json, failed_step, failure_reason FROM jobs WHERE id = 1")
    steps = [s["name"] for s in json.loads(parent["steps_json"])]
    assert "Run integration tests" not in steps, "the leg's steps live on the leg row"
    assert "Lint charts" in steps and "Deploy charts" in steps, "the out-of-scope installer leg stays"
    assert parent["failed_step"] == "", "the run row no longer points at a leg's step"
    assert parent["failure_reason"] == "n/a", "a ci row's classification"


# the flake-detection campaigns, every leg a hundred-plus times ----------------------------------


def campaign_jobs(repetitions=3):
    """A flake-detection campaign run (osac run 44909254863 and its kin,
    live-confirmed): the plan, a unit repetition, and N integration
    repetitions, each a called Integration Tests whose legs are called
    Tests jobs -- three " / " parts deep, the last still the leg kind."""
    jobs = [leg_job("plan", job_id=1, steps=[]),
            leg_job("Unit repetition 1 / Plan", job_id=2, steps=[])]
    job_id = 10
    for repetition in range(1, repetitions + 1):
        jobs.append(leg_job(f"Integration repetition {repetition} / Plan", job_id=job_id, steps=[]))
        job_id += 1
        jobs.append(leg_job(
            f"Integration repetition {repetition} / Integration test (CaaS) / it-caas",
            conclusion="failure" if repetition == 2 else "success", job_id=job_id))
        job_id += 1
        jobs.append(leg_job(
            f"Integration repetition {repetition} / Integration test (fulfillment-service) / it-fulfillment-service",
            job_id=job_id))
        job_id += 1
        jobs.append(leg_job(
            f"Integration repetition {repetition} / Integration test (osac-installer) / it-osac-installer",
            job_id=job_id, steps=[make_step("Lint charts")]))
        job_id += 1
    return jobs


def test_a_campaign_stores_one_row_per_repetition(W, exporter):
    record, leg_rows = suite_rows(W, exporter, FLAKE, campaign_jobs())
    caas = [row for row in leg_rows if row["workflow"] == "IT CaaS (Flake detection)"]
    fulfillment = [row for row in leg_rows if row["workflow"] == "IT fulfillment (Flake detection)"]
    assert len(caas) == 3, "one row per repetition, not one per suite"
    assert len(fulfillment) == 3
    assert sorted(row["id"] for row in caas) == [11, 15, 19], "each row is its own repetition job"
    assert sorted(row["conclusion"] for row in caas) == ["failure", "success", "success"], (
        "a repetition's own conclusion, the flake among them"
    )
    assert record["workflow"] == FLAKE and record["category"] == "ci"


def test_a_campaigns_rows_read_as_the_flake_trigger(wfe, exporter):
    _, leg_rows = suite_rows(wfe.WorkflowExporter, exporter, FLAKE, campaign_jobs())
    for row in leg_rows:
        assert wfe.ci_observe_phases.trigger_key("schedule", row["workflow"]) == "flake"
        assert wfe.ci_observe_phases.suite_key(row["workflow"]) in ("it-caas", "it-fulfillment")
    assert wfe.ci_observe_phases.trigger_selected("all", "flake") is False
    assert wfe.ci_observe_phases.trigger_selected("flake", "flake") is True


def test_a_completed_campaign_is_stored_with_its_repetitions(W, exporter, github, R):
    github.add(
        R("osac/actions/runs/1/attempts/1/jobs?per_page=30"),
        FakeResponse(payload={"jobs": campaign_jobs()}),
    )
    assert exporter._process_completed_run(
        "osac", a_run(FLAKE, event="schedule", conclusion="failure"))
    stored_rows = rows(exporter, "SELECT id, workflow, category, conclusion FROM jobs ORDER BY id")
    assert stored_rows == [
        {"id": 1, "workflow": FLAKE, "category": "ci", "conclusion": "failure"},
        *[{"id": job_id, "workflow": workflow, "category": "e2e", "conclusion": conclusion}
          for job_id, workflow, conclusion in (
              (11, "IT CaaS (Flake detection)", "success"),
              (12, "IT fulfillment (Flake detection)", "success"),
              (15, "IT CaaS (Flake detection)", "failure"),
              (16, "IT fulfillment (Flake detection)", "success"),
              (19, "IT CaaS (Flake detection)", "success"),
              (20, "IT fulfillment (Flake detection)", "success"),
          )],
    ]
    # The same run again: every row is current, nothing is stored twice.
    assert not exporter._process_completed_run(
        "osac", a_run(FLAKE, event="schedule", conclusion="failure"))


def test_the_leg_catch_up_stores_a_campaign_stored_before_it(W, exporter, github, R, wfe, monkeypatch):
    monkeypatch.setattr(wfe, "TOKEN", "token")
    an_old_row(exporter, workflow=FLAKE)
    github.add(
        R("osac/actions/runs/1/attempts/1/jobs?per_page=30"),
        FakeResponse(payload={"jobs": campaign_jobs()}),
    )
    assert exporter._backfill_integration_legs_if_needed(limit=10) == 1
    leg_rows = rows(exporter, "SELECT COUNT(*) AS n FROM jobs WHERE workflow LIKE '%(Flake detection)'"
                    " AND id != 1")
    assert leg_rows[0]["n"] == 6
    assert exporter._backfill_integration_legs_if_needed(limit=10) == 0


# the readers' vocabulary ------------------------------------------------------------------------


def test_the_run_index_filters_the_integration_suites(W):
    """core" is the ci-observe population: the three E2E suites and the four
    integration ones, and nothing else. The flake campaigns' names are in
    each suite's list but not in core: they are counted only when the
    Started by filter asks for them by name."""
    expected = {
        "it-fulfillment": ("it fulfillment (tests)", "it fulfillment (flake detection)"),
        "it-operator": ("it operator (tests)", "it operator (flake detection)"),
        "it-bmf": ("it bmf (tests)", "it bmf (flake detection)"),
        "it-caas": ("it caas (tests)", "it caas (flake detection)"),
    }
    for suite, names in expected.items():
        assert W.CI_OBSERVE_SUITE_WORKFLOWS[suite] == names
        assert names[0] in W.CI_OBSERVE_SUITE_WORKFLOWS["core"]
        assert names[1] not in W.CI_OBSERVE_SUITE_WORKFLOWS["core"]


def test_every_suite_workflows_name_categorizes_as_e2e(wfe):
    """A suite workflow's stored name must categorize as e2e whatever it
    contains. The integration suites' leg-row names ("IT fulfillment
    (Tests)", ...) carry "tests", which the ci patterns would otherwise
    catch -- and the startup recategorization would then flip rows the
    exporter's _integration_leg_records already stores as e2e back to ci.
    The e2e patterns are derived from ci_observe_phases.SUITE_WORKFLOWS so
    a future suite whose name lacks "e2e" cannot hit the same bug."""
    W = wfe.WorkflowExporter
    for names in wfe.ci_observe_phases.SUITE_WORKFLOWS.values():
        for name in names:
            assert W._categorize_workflow(name) == "e2e", name
    # The callers and legs that stay out of scope keep the ci category.
    assert W._categorize_workflow(TESTS) == "ci"
    assert W._categorize_workflow(CI) == "ci"
    assert W._categorize_workflow("Unit Tests") == "ci"
