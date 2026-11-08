"""The shared vocabulary of the ci-observe pages: suites, triggers, phases and
failure causes (ci_observe_phases.py)."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "monitoring" / "scripts"
WORKFLOWS = ROOT / ".github" / "workflows"

# The workflows whose steps an in-scope suite run executes.
IN_SCOPE_WORKFLOWS = (
    "e2e-vmaas-full-install.yml",
    "e2e-caas-full-install.yml",
    "e2e-bmaas-full-install.yml",
    "e2e-vmaas.yml",
)


@pytest.fixture(scope="module")
def phases():
    if "ci_observe_phases" in sys.modules:
        return sys.modules["ci_observe_phases"]
    spec = importlib.util.spec_from_file_location("ci_observe_phases", SCRIPTS / "ci_observe_phases.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["ci_observe_phases"] = module
    spec.loader.exec_module(module)
    return module


def workflow_steps():
    for filename in IN_SCOPE_WORKFLOWS:
        document = yaml.safe_load((WORKFLOWS / filename).read_text())
        for job_name, job in (document.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if step.get("name"):
                    yield filename, job_name, step["name"]


def test_every_step_of_the_in_scope_workflows_has_a_named_phase(phases):
    unnamed = [
        f"{filename}: {step}"
        for filename, job, step in workflow_steps()
        if phases.step_phase(step, job) == "other"
    ]
    assert not unnamed, "add these steps to ci_observe_phases._STEP_PHASE: " + ", ".join(unnamed)


@pytest.mark.parametrize(
    ("step", "phase"),
    [
        ("Checkout repository", "prepare"),
        ("Authorize fork PR", "prepare"),
        ("Boot cluster clone", "cluster"),
        ("Setup virtual BareMetalHosts", "cluster"),
        ("Build and load component images", "build"),
        ("Install OSAC", "install"),
        ("Run E2E tests (ci-observe capture)", "test"),
        ("Scan and upload artifacts", "collect"),
        ("Set up job", "prepare"),
        ("Complete job", "collect"),
        ("Post Checkout repository", "collect"),
        ("Gather e2e test logs", "collect"),
        ("Run pytest suite", "test"),
        ("Mirror images", "build"),
        ("Something nobody named", "other"),
        # Steps of real runs whose workflows live outside this repository
        # (osac-ci) or in earlier workflow versions.
        ("Check e2e readiness", "prepare"),
        ("Complete stale in_progress gate checks", "prepare"),
        ("Mark e2e-vmaas-gate pending", "prepare"),
        ("Mark e2e-bmaas-gate pending", "prepare"),
        ("Cancel superseded runs for this queue entry", "prepare"),
        ("Run osac-project/osac-ci/.github/actions/suite-inputs@0d5f2b7c9e1a4b3c8d6e5f4a3b2c1d0e9f8a7b6c", "prepare"),
        ("Read the ci-observe label", "prepare"),
        ("Read the pull request", "prepare"),
        ("Collect agent watcher results", "collect"),
    ],
)
def test_step_phase(phases, step, phase):
    assert phases.step_phase(step) == phase


def test_the_verify_job_of_a_pr_suite_is_ci_setup(phases):
    assert phases.step_phase("Check the inputs", "verify") == "prepare"


@pytest.mark.parametrize(
    ("workflow", "suite", "trigger"),
    [
        ("E2E suite · vmaas · #1620 · 0123abc", "vmaas", "pr"),
        ("E2E VMaaS PR suite", "vmaas", "pr"),
        ("E2E CaaS Full Install", "caas", "manual"),
        ("E2E bmaas (merge queue)", "bmaas", "merge_queue"),
        ("E2E VMaaS", "vmaas", "manual"),
        ("E2E VMaaS Netris Full Install", "", "manual"),
        ("E2E VMaaS Production Install", "", "manual"),
        ("E2E CaaS Agentless-Net Full Install", "", "manual"),
        ("E2E Full Regression", "", "manual"),
    ],
)
def test_suite_and_trigger(phases, workflow, suite, trigger):
    assert phases.suite_key(workflow) == suite
    assert phases.trigger_key("workflow_dispatch", workflow) == trigger


def test_event_triggers(phases):
    assert phases.trigger_key("schedule", "E2E VMaaS Full Install") == "scheduled"
    assert phases.trigger_key("merge_group", "E2E for the merge queue") == "merge_queue"
    assert phases.trigger_key("pull_request", "E2E VMaaS Full Install") == "pr"
    assert phases.trigger_key("push", "E2E VMaaS Full Install") == "push"
    assert phases.trigger_key("", "") == "other"


def test_pr_number_and_tested_sha(phases):
    name = "E2E suite · caas · #77 · 0123456789abcdef0123456789abcdef01234567"
    assert phases.pr_number("", name) == "77"
    assert phases.pr_number("#12", "E2E CaaS Full Install") == "12"
    assert phases.pr_number("", "E2E CaaS Full Install") == ""
    assert phases.tested_sha(name, "mainsha") == "0123456789abcdef0123456789abcdef01234567"
    assert phases.tested_sha("E2E CaaS Full Install", "headsha") == "headsha"


def test_result_key(phases):
    assert phases.result_key("success") == "passed"
    assert phases.result_key("failure") == "failed"
    assert phases.result_key("timed_out") == "failed"
    assert phases.result_key("cancelled") == "cancelled"
    assert phases.result_key("") == "other"


def test_the_earliest_failed_phase_is_the_cause(phases):
    text = "e2e → Scan and upload artifacts; e2e → Run E2E tests"
    cause = phases.failure_cause("failure", text)
    assert cause["cause"] == "test"
    assert cause["area"] == "Tests"
    assert cause["step"] == "Run E2E tests"


def test_failure_causes(phases):
    assert phases.failure_cause("failure", "e2e → Install OSAC")["area"] == "OSAC install"
    assert phases.failure_cause("failure", "e2e → Build and load component images")["area"] == "Build"
    assert phases.failure_cause("failure", "e2e → Boot cluster clone")["area"] == "CI infrastructure"
    assert phases.failure_cause("failure", "e2e → Authorize fork PR")["cause"] == "blocked"
    assert phases.failure_cause("timed_out", "e2e → Install OSAC")["cause"] == "timeout"
    assert phases.failure_cause("failure", "e2e → A step nobody named")["cause"] == "other"
    assert phases.failure_cause("success", "")["cause"] == ""
    assert phases.failure_cause("cancelled", "")["cause"] == ""


def test_a_failure_without_a_failed_step_is_a_timeout_or_a_lost_runner(phases):
    timeline = [{"name": "e2e", "conclusion": "failure",
                 "steps": [{"name": "Run E2E tests", "conclusion": "cancelled"}]}]
    timed_out = phases.failure_cause("failure", "", timeline)
    assert timed_out["cause"] == "timeout"
    assert timed_out["step"] == "Run E2E tests"
    assert timed_out["phase"] == "test"
    assert phases.failure_cause("failure", "", [])["cause"] == "runner"
    assert phases.failure_cause("timed_out", "", None)["cause"] == "timeout"


def test_run_phases_from_a_timeline(phases):
    timeline = [
        {"name": "verify", "created_at": "2026-11-01T10:00:00Z", "started_at": "2026-11-01T10:00:05Z",
         "completed_at": "2026-11-01T10:00:35Z",
         "steps": [{"name": "Set up job", "started_at": "2026-11-01T10:00:05Z",
                    "completed_at": "2026-11-01T10:00:35Z"}]},
        {"name": "vmaas / e2e-vmaas-full-install / e2e", "created_at": "2026-11-01T10:00:40Z",
         "started_at": "2026-11-01T10:10:40Z", "completed_at": "2026-11-01T11:10:40Z",
         "steps": [
             {"name": "Boot cluster clone", "started_at": "2026-11-01T10:10:40Z",
              "completed_at": "2026-11-01T10:30:40Z"},
             {"name": "Run E2E tests", "started_at": "2026-11-01T10:30:40Z",
              "completed_at": "2026-11-01T11:10:40Z"},
             {"name": "Teardown", "started_at": "", "completed_at": ""},
         ]},
    ]
    result = phases.run_phases(timeline)
    assert result["source"] == "timeline"
    assert result["queue_s"] == 600
    assert result["phases"]["queue"] == 600
    assert result["phases"]["prepare"] == 30
    assert result["phases"]["cluster"] == 1200
    assert result["phases"]["test"] == 2400
    assert result["total_s"] == 4240
    assert phases.main_job(timeline)["name"].endswith("/ e2e")


def test_run_phases_from_stored_step_durations(phases):
    result = phases.run_phases([], [{"name": "Install OSAC", "duration_s": 900},
                                    {"name": "Run E2E tests", "duration_s": 1800},
                                    {"name": "Broken", "duration_s": None}])
    assert result["source"] == "steps"
    assert result["queue_s"] is None
    assert result["total_s"] is None
    assert result["phases"]["install"] == 900
    assert result["phases"]["test"] == 1800


def test_load_timeline_tolerates_junk(phases):
    assert phases.load_timeline("") == []
    assert phases.load_timeline("not json") == []
    assert phases.load_timeline('{"a": 1}') == []
    assert phases.load_timeline('[{"name": "e2e"}]') == [{"name": "e2e"}]
    assert phases.load_timeline([{"name": "e2e"}]) == [{"name": "e2e"}]
    assert phases.main_job([]) is None
    assert phases.seconds_between("bad", "2026-11-01T10:00:00Z") is None


# -- pages and colors ---------------------------------------------------------


@pytest.mark.parametrize(("days", "window"), [(1, 7), (7, 7), (8, 14), (30, 30), (31, 90), (400, 90), ("x", 30)])
def test_a_link_opens_the_smallest_window_that_covers_it(phases, days, window):
    assert phases.window_days(days) == window


def test_a_test_link_opens_its_history_under_its_canonical_key(phases):
    url = phases.link_to_test("tests/e2e/caas/test_cluster.py::TestCluster::test_create[a b]", days=10)

    # Unescaped: Grafana's data links run the URL through encodeURI, which
    # encodes "[", "]" and the space itself and would turn a "%3A" into "%253A".
    assert url == (
        phases.TEST_PAGE
        + "?var-test=tests.e2e.caas.test_cluster::TestCluster::test_create[a b]"
        + "&var-days=14&var-suite=caas"
    )
    assert phases.link_to_test("test_x", suite="bmaas").endswith("&var-suite=bmaas")
    assert phases.link_to_test("test_x").endswith("&var-suite=all"), "a test outside the suites opens all"


@pytest.mark.parametrize("key", ["tests.e2e.vmaas.test_vm::test_create[a&b]", "tests.e2e.x::t[50%]",
                                 "tests.e2e.x::t[a=b]", "tests.e2e.x::t[#1]", "tests.e2e.x::t[a+b]"])
def test_a_key_a_link_cannot_carry_goes_as_its_hash(phases, key):
    value = phases.test_link_value(key)

    assert value.startswith("~") and re.fullmatch(r"~[0-9a-f]{16}", value)
    assert phases.test_for_link_value(value, {key, "tests.e2e.other::t"}) == key
    assert f"var-test={value}&" in phases.link_to_test(key)


def test_every_url_a_data_link_carries_is_free_of_percent_escapes(phases):
    """Grafana encodes a data link's "%" again; no link may contain one."""
    for url in (
        phases.link_to_test("tests/e2e/caas/test_cluster.py::TestCluster::test_create[a b]"),
        phases.link_to_suite(phases.TIME_PAGE, "caas", 30, "osac-test-infra", "merge_queue"),
    ):
        assert "%" not in url, url


def test_a_suite_link_keeps_the_readers_filters(phases):
    url = phases.link_to_suite(phases.FAILURES_PAGE, "vmaas", days=30, repo="osac", trigger="pr")

    assert url == phases.FAILURES_PAGE + "?var-suite=vmaas&var-days=30&var-repo=osac&var-trigger=pr"
    assert "var-suite=all" in phases.link_to_suite(phases.TIME_PAGE, "netris")
    assert "var-repo=all&var-trigger=all" in phases.link_to_suite(phases.TIME_PAGE, "caas")


def test_every_phase_and_area_has_its_own_palette_color(phases):
    assert len(phases.PALETTE) == 8 and len(set(phases.PALETTE)) == 8
    assert [color for _, _, color in phases.PHASES] == list(phases.PALETTE)
    assert [phases.AREA_COLORS[area] for area in phases.AREAS] == list(phases.PALETTE[:5])
    assert set(phases.AREA_KEYS) == set(phases.AREAS)
    for key, _, area, color in phases.CAUSES:
        assert area in phases.AREAS, key
        assert color == phases.AREA_COLORS[area], "a cause wears its area's color"
