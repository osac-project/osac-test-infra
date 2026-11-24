"""How workflow-exporter.py records the Tests workflow's runs.

The osac mono-repo's reusable "Tests" workflow (.github/workflows/tests.yml)
runs one unit- or integration-test leg per run, and the leg is the run's
single job, named after its kind. The four integration legs ci-observe covers
(it-fulfillment-service, it-osac-operator, it-bare-metal-fulfillment-operator,
it-caas) are stored as e2e rows under their suite's workflow name, so every
ci-observe reader maps them like any other suite run; every other leg (unit
tests, contract checks, the installer, preflight and AAP suites) keeps the
workflow's own name and the ci category.

A called run may carry its caller's job name as a prefix
("Integration test (fulfillment-service) / it-fulfillment-service"), and a
row stored before this refinement ran keeps the workflow's own name, so
_needs_upsert re-examines it until it carries a suite's name.
"""

from __future__ import annotations

import pytest

from helpers import FakeResponse, make_gh_job, make_run, make_step

TESTS = "Tests"


@pytest.fixture
def W(wfe):
    return wfe.WorkflowExporter


@pytest.fixture
def R(wfe):
    return lambda path: f"/repos/{wfe.ORG}/{path}"


def rows(exporter, sql, *args):
    with exporter._db() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def a_tests_run(**overrides):
    return make_run(id=1, name=TESTS, event="pull_request", **overrides)


def leg_job(kind, conclusion="success"):
    return make_gh_job(
        name=kind,
        conclusion=conclusion,
        steps=[make_step("Install infrastructure on Kind"), make_step("Run integration tests", conclusion=conclusion)],
    )


@pytest.mark.parametrize(
    ("kind", "workflow"),
    [
        ("it-fulfillment-service", "IT fulfillment (Tests)"),
        ("it-osac-operator", "IT operator (Tests)"),
        ("it-bare-metal-fulfillment-operator", "IT BMF (Tests)"),
        ("it-caas", "IT CaaS (Tests)"),
    ],
)
def test_an_integration_leg_becomes_a_suite_row_of_its_own(W, kind, workflow):
    record = {"id": 1, "repo": "osac", "workflow": TESTS, "category": "ci", "display_name": "osac / Tests"}
    W._refine_integration_suite(record, [leg_job(kind)])
    assert record["workflow"] == workflow
    assert record["display_name"] == f"osac / {workflow}"
    assert record["category"] == "e2e"


def test_a_called_leg_names_its_kind_after_the_callers_prefix(W):
    record = {"id": 1, "repo": "osac", "workflow": TESTS, "category": "ci", "display_name": "osac / Tests"}
    W._refine_integration_suite(record, [leg_job("Integration test (CaaS) / it-caas")])
    assert record["workflow"] == "IT CaaS (Tests)"
    assert record["category"] == "e2e"


@pytest.mark.parametrize(
    "kind",
    [
        "go-unit",
        "metering-contract",
        "ui-unit",
        "caas-unit",
        "helm-unit",
        "it-osac-aap",
        "it-osac-installer",
        "it-osac-installer-preflight-helm3",
        "fulfillment-service",
    ],
)
def test_every_other_leg_stays_the_workflow_and_the_ci_category(W, kind):
    record = {"id": 1, "repo": "osac", "workflow": TESTS, "category": "ci", "display_name": "osac / Tests"}
    W._refine_integration_suite(record, [leg_job(kind)])
    assert record == {"id": 1, "repo": "osac", "workflow": TESTS, "category": "ci", "display_name": "osac / Tests"}


def test_any_other_workflow_is_untouched(W):
    record = {
        "id": 1,
        "repo": "osac",
        "workflow": "E2E VMaaS Full Install",
        "category": "e2e",
        "display_name": "osac / E2E VMaaS Full Install",
    }
    W._refine_integration_suite(record, [leg_job("it-fulfillment-service")])
    assert record["workflow"] == "E2E VMaaS Full Install"


def test_a_completed_integration_run_is_stored_as_its_suite(W, exporter, github, R):
    github.add(
        R("osac/actions/runs/1/attempts/1/jobs?per_page=30"),
        FakeResponse(payload={"jobs": [leg_job("it-fulfillment-service")]}),
    )
    assert exporter._process_completed_run("osac", a_tests_run())
    [row] = rows(exporter, "SELECT workflow, category, conclusion FROM jobs")
    assert row == {"workflow": "IT fulfillment (Tests)", "category": "e2e", "conclusion": "success"}
    # The same run again: the row is current, nothing is stored twice.
    assert not exporter._process_completed_run("osac", a_tests_run())


def test_a_failed_integration_run_classifies_its_test_step(W, exporter, github, R):
    github.add(
        R("osac/actions/runs/1/attempts/1/jobs?per_page=30"),
        FakeResponse(payload={"jobs": [leg_job("it-bare-metal-fulfillment-operator", conclusion="failure")]}),
    )
    assert exporter._process_completed_run("osac", a_tests_run(conclusion="failure"))
    [row] = rows(exporter, "SELECT workflow, failure_reason, failed_step FROM jobs")
    assert row["workflow"] == "IT BMF (Tests)"
    assert row["failure_reason"] == "test", "the integration steps are the product's own"
    assert "Run integration tests" in row["failed_step"]


def test_an_out_of_scope_leg_keeps_the_workflow_name_and_is_reexamined(W, exporter, github, R):
    github.add(
        R("osac/actions/runs/1/attempts/1/jobs?per_page=30"), FakeResponse(payload={"jobs": [leg_job("it-osac-aap")]})
    )
    assert exporter._process_completed_run("osac", a_tests_run())
    [row] = rows(exporter, "SELECT workflow, category FROM jobs")
    assert row == {"workflow": TESTS, "category": "ci"}
    # The row keeps the workflow's own name, so the guard asks for its jobs
    # again on every pass: the price of re-examining rows stored before the
    # refinement ran, bounded by the reconcile and sweep budget gates.
    assert exporter._needs_upsert(a_tests_run())


def test_a_stored_tests_row_is_reexamined_until_it_names_its_suite(W, exporter, github, R):
    # Stored before the refinement ran: the row keeps the workflow's own
    # name, so the guard asks for its jobs again.
    github.add(
        R("osac/actions/runs/1/attempts/1/jobs?per_page=30"),
        FakeResponse(payload={"jobs": [leg_job("it-fulfillment-service")]}),
    )
    with exporter._db() as conn:
        conn.execute(
            "INSERT INTO jobs (id, repo, workflow, category, status, conclusion, run_attempt, run_id, url,"
            " created_at) VALUES (1, 'osac', ?, 'ci', 'completed', 'success', 1, '1', 'u', '2026-11-01T00:00:00Z')",
            (TESTS,),
        )
    assert exporter._needs_upsert(a_tests_run())
    assert exporter._process_completed_run("osac", a_tests_run())
    [row] = rows(exporter, "SELECT workflow, category FROM jobs")
    assert row == {"workflow": "IT fulfillment (Tests)", "category": "e2e"}
    # Once it carries the suite's name, the guard leaves it alone.
    assert not exporter._needs_upsert(a_tests_run())


def test_the_run_index_filters_the_integration_suites(W):
    """core" is the ci-observe population: the three E2E suites and the four
    integration ones, and nothing else."""
    expected = {
        "it-fulfillment": ("it fulfillment (tests)",),
        "it-operator": ("it operator (tests)",),
        "it-bmf": ("it bmf (tests)",),
        "it-caas": ("it caas (tests)",),
    }
    for suite, names in expected.items():
        assert W.CI_OBSERVE_SUITE_WORKFLOWS[suite] == names
        assert names[0] in W.CI_OBSERVE_SUITE_WORKFLOWS["core"]
