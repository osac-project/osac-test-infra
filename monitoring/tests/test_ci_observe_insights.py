"""The at-a-glance documents behind the Overview, Where the time goes, Why
runs fail and Test history pages (ci_observe_insights.py).

Small hand-built stores pin each definition with exact numbers: pass rate,
time to result, the runner wait, failure causes, flakiness, the previous
window, the filters, the per-test time split and the cache. The seeded
dataset (ci_observe_seed) checks that every document builds and adds up.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import ci_observe_seed

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def _load(name, filename):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


insights = _load("ci_observe_insights", "ci_observe_insights.py")
phases = _load("ci_observe_phases", "ci_observe_phases.py")
trends = _load("ci_observe_trends", "ci_observe_trends.py")
spans = _load("ci_observe_spans", "ci_observe_spans.py")


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


STEPS = (
    ("Checkout repository", 60),
    ("Boot cluster clone", 600),
    ("Build and load component images", 300),
    ("Install OSAC", 900),
    ("Run E2E tests (ci-observe capture)", 1800),
    ("Gather cluster logs", 120),
)


def timeline(created, queue_s, steps=STEPS, failed=None, conclusion="success"):
    """One suite job: its wait for a runner, then its steps back to back."""
    started = created + timedelta(seconds=queue_s)
    moment = started
    rows = []
    for number, (name, seconds) in enumerate(steps, start=1):
        end = moment + timedelta(seconds=seconds)
        rows.append({
            "number": number, "name": name, "status": "completed",
            "conclusion": "failure" if name == failed else "success",
            "started_at": iso(moment), "completed_at": iso(end),
        })
        moment = end
        if name == failed:
            break
    return [{
        "name": "vmaas / e2e", "status": "completed", "conclusion": conclusion, "created_at": iso(created),
        "started_at": iso(started), "completed_at": iso(moment), "runner_name": "box-1", "steps": rows,
    }]


class Stores:
    """A workflow-exporter.db and a trends.db with only what a test adds."""

    def __init__(self, directory):
        self.jobs_db = str(directory / "workflow-exporter.db")
        self.trends_db = str(directory / "trends.db")
        with sqlite3.connect(self.jobs_db) as conn:
            conn.execute(
                "CREATE TABLE jobs (id INTEGER PRIMARY KEY, run_id TEXT, repo TEXT, workflow TEXT, category TEXT,"
                " pr_display TEXT, conclusion TEXT, status TEXT, event TEXT, duration_s INTEGER, created_at TEXT,"
                " updated_at TEXT, run_attempt INTEGER, failed_step TEXT, steps_json TEXT, failure_reason TEXT,"
                " head_sha TEXT, url TEXT, runner_name TEXT, timeline_json TEXT)"
            )
            conn.execute(
                "CREATE TABLE captures (capture_id TEXT PRIMARY KEY, run_id TEXT, run_attempt TEXT, suite TEXT)"
            )
        trends.open_trends_db(self.trends_db).close()
        spans.open_db(self.trends_db).close()
        self.next_id = 1000

    def run(self, created, conclusion="success", suite="vmaas", queue_s=60, failed=None, repo="osac",
            event="schedule", workflow=None, sha="a" * 40, with_timeline=True, duration_s=None, attempt=1):
        self.next_id += 1
        label = {"vmaas": "VMaaS", "caas": "CaaS", "bmaas": "BMaaS"}[suite]
        workflow = workflow or f"E2E {label} Full Install"
        jobs = timeline(created, queue_s, failed=failed, conclusion=conclusion) if with_timeline else []
        ended = phases.parse_time(jobs[0]["completed_at"]) if jobs else created + timedelta(seconds=duration_s or 0)
        steps = [{"name": name, "duration_s": seconds} for name, seconds in STEPS]
        with sqlite3.connect(self.jobs_db) as conn:
            conn.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, 'e2e', '', ?, 'completed', ?, ?, ?, ?, ?, ?, ?, '', ?, '',"
                " 'box-1', ?)",
                (
                    self.next_id, str(self.next_id), repo, workflow, conclusion, event,
                    duration_s if duration_s is not None else int((ended - created).total_seconds()),
                    iso(created), iso(ended), attempt,
                    f"{suite} / e2e → {failed}" if failed else "", json.dumps(steps), sha,
                    json.dumps(jobs) if with_timeline else "",
                ),
            )
        return str(self.next_id)

    def test(self, run_id, test_id, passed, duration_s, created, suite="vmaas", sha="a" * 40, message="",
             trigger="scheduled", repo="osac"):
        with sqlite3.connect(self.trends_db) as conn:
            conn.execute(
                "INSERT INTO test_runs (day, repo, workflow, suite, test_id, source_sha, run_id, created_at,"
                " duration_s, passed, suite_key, test_key, trigger, pr, message)"
                " VALUES (?, ?, 'E2E', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)",
                (
                    iso(created)[:10], repo, f"tests/e2e/{suite}", test_id, sha, int(run_id), iso(created),
                    duration_s, 1 if passed else 0, suite, phases.canonical_test_key(test_id), trigger, message,
                ),
            )

    def capture(self, capture_id, run_id, suite="vmaas"):
        with sqlite3.connect(self.jobs_db) as conn:
            conn.execute("INSERT INTO captures VALUES (?, ?, '1', ?)", (capture_id, run_id, suite))

    def spans(self, capture_id, run_id, created, tests, waits=(), fixtures=(), suite="vmaas", state="ok"):
        conn = spans.open_db(self.trends_db)
        try:
            spans.write_capture(conn, {
                "capture_id": capture_id, "run_id": int(run_id), "run_attempt": 1, "suite": suite,
                "day": iso(created)[:10], "created_at": iso(created), "trace_id": "t" * 32, "state": state,
                "complete": 1, "spans_total": 10, "tests": len(tests), "waits": len(waits), "session_s": 100.0,
                "unattributed_waits": 0, "rolled_at": iso(NOW), "notes": "",
            }, list(tests), list(waits), list(fixtures))
        finally:
            conn.close()

    def api(self, now=NOW):
        return insights.CIObserveInsights(self.jobs_db, self.trends_db, clock=lambda: now)


@pytest.fixture
def stores(tmp_path):
    return Stores(tmp_path)


def get(api, kind, **params):
    return api.get_json(kind, {name: [str(value)] for name, value in params.items()})


def span_test(key, setup, call, teardown, wait_setup=0.0, wait_call=0.0, wait_teardown=0.0):
    return {
        "test_key": key, "nodeid": key, "worker": "gw0", "status": "passed", "started_at": iso(NOW),
        "duration_s": setup + call + teardown, "setup_s": setup, "call_s": call, "teardown_s": teardown,
        "wait_s": wait_setup + wait_call + wait_teardown, "wait_setup_s": wait_setup, "wait_call_s": wait_call,
        "wait_teardown_s": wait_teardown, "shared_setup_s": 0.0, "waits": 1,
    }


def span_wait(key, name, kind, seconds, phase="call", failed=0):
    return {
        "test_key": key, "nodeid": key, "phase": phase, "name": name, "signature": spans.wait_signature(name, ""),
        "resource_kind": kind, "started_at": iso(NOW), "duration_s": seconds, "attempts": 3,
        "condition_met": 0 if failed else 1, "failed": failed,
    }


# -- health ---------------------------------------------------------------------------------------


def test_pass_rate_leaves_cancelled_runs_out(stores):
    for hours, conclusion in enumerate(["success", "success", "success", "failure", "cancelled", "cancelled"], 1):
        failed = "Install OSAC" if conclusion == "failure" else None
        stores.run(NOW - timedelta(hours=hours * 3), conclusion, failed=failed)

    tile = get(stores.api(), "health")["tiles"][0]

    assert (tile["runs"], tile["passed"], tile["failed"], tile["cancelled"]) == (6, 3, 1, 2)
    assert tile["pass_rate"] == 0.75


def test_time_to_result_counts_the_wait_for_a_runner(stores):
    """From the first job's creation to the last job's end: the 300 seconds
    in GitHub's queue are part of what the developer waits for."""
    stores.run(NOW - timedelta(hours=5), queue_s=300)

    tile = get(stores.api(), "health")["tiles"][0]

    assert tile["median_result_s"] == 300 + sum(seconds for _, seconds in STEPS)
    assert tile["median_queue_s"] == 300


def test_a_run_stored_before_timelines_has_its_duration_but_no_runner_wait(stores):
    stores.run(NOW - timedelta(hours=5), with_timeline=False, duration_s=4000)

    document = get(stores.api(), "health")

    assert document["tiles"][0]["median_result_s"] == 4000
    assert document["tiles"][0]["median_queue_s"] is None, "not measured, never zero"
    assert any("before job timelines existed" in note for note in document["notes"])


def test_a_failure_is_charged_to_its_first_failed_phase(stores):
    """Install failed first, so the run failed in the OSAC install even though
    the log collection after it failed too. A cluster failure is CI
    infrastructure."""
    created = NOW - timedelta(hours=5)
    run_id = stores.run(created, "failure", failed="Install OSAC")
    with sqlite3.connect(stores.jobs_db) as conn:
        conn.execute(
            "UPDATE jobs SET failed_step = ? WHERE run_id = ?",
            ("vmaas / e2e → Gather cluster logs; vmaas / e2e → Install OSAC", run_id),
        )
    stores.run(NOW - timedelta(hours=7), "failure", failed="Boot cluster clone")

    document = get(stores.api(), "health")
    failures = {row["failed_step"]: row["cause_label"] for row in document["recent_failures"]}

    assert failures == {"Install OSAC": "OSAC install failed", "Boot cluster clone": "Cluster or host setup failed"}
    assert document["tiles"][0]["infra_share"] == 0.5
    causes = next(row for row in document["causes"] if row["suite"] == "all")
    assert (causes["install"], causes["cluster"], causes["area_install"], causes["area_infra"]) == (1, 1, 1, 1)


def test_a_flaky_test_passed_and_failed_on_the_same_commit(stores):
    run_a = stores.run(NOW - timedelta(hours=5))
    run_b = stores.run(NOW - timedelta(hours=4))
    created = NOW - timedelta(hours=4)
    stores.test(run_a, "tests/e2e/vmaas/test_vm.py::test_create", True, 10.0, created, sha="1" * 40)
    stores.test(run_b, "tests/e2e/vmaas/test_vm.py::test_create", False, 12.0, created, sha="1" * 40)
    stores.test(run_a, "tests/e2e/vmaas/test_vm.py::test_delete", False, 5.0, created, sha="1" * 40)
    stores.test(run_b, "tests/e2e/vmaas/test_vm.py::test_delete", True, 5.0, created, sha="2" * 40)

    tile = get(stores.api(), "health")["tiles"][0]
    failures = get(stores.api(), "failures")

    assert (tile["flaky_tests"], tile["failing_tests"]) == (1, 2)
    assert [row["test"] for row in failures["flaky"]] == ["test_vm::test_create"]
    assert failures["flaky"][0]["flaky_shas"] == 1


def test_the_window_is_compared_with_the_one_before(stores):
    for days_ago, conclusion in ((1, "success"), (2, "failure"), (8, "failure"), (9, "failure"), (10, "success")):
        failed = "Install OSAC" if conclusion == "failure" else None
        stores.run(NOW - timedelta(days=days_ago), conclusion, failed=failed)

    tile = get(stores.api(), "health", days=7)["tiles"][0]

    assert (tile["pass_rate"], tile["pass_rate_prev"]) == (0.5, round(1 / 3, 4))
    assert tile["pass_rate_delta"] == round(0.5 - round(1 / 3, 4), 4)


def test_every_number_says_not_measured_when_nothing_ran(stores):
    tile = get(stores.api(), "health")["tiles"][0]

    assert tile["runs"] == 0
    for field in ("pass_rate", "median_result_s", "median_queue_s", "infra_share"):
        assert tile[field] is None, field


def test_the_suite_rows_link_to_their_pages_with_the_filters(stores):
    stores.run(NOW - timedelta(hours=5), suite="caas")

    rows = {row["suite"]: row for row in get(stores.api(), "health", days=14, repo="osac")["suites"]}

    assert set(rows) == {"vmaas", "caas", "bmaas", "all"}
    assert rows["caas"]["time_url"].startswith(phases.TIME_PAGE)
    assert "var-suite=caas" in rows["caas"]["time_url"]
    assert "var-days=14" in rows["caas"]["failures_url"]
    assert "var-repo=osac" in rows["caas"]["failures_url"]


# -- filters ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(("params", "expected"), [
    ({}, (7, "all", "", "all")),
    ({"days": "3"}, (3, "all", "", "all")),
    ({"days": "400"}, (insights.KEEP_DAYS, "all", "", "all")),
    ({"days": "x"}, (7, "all", "", "all")),
    ({"suite": "core"}, (7, "all", "", "all")),
    ({"suite": "CaaS"}, (7, "caas", "", "all")),
    ({"repo": "all", "trigger": "merge_queue"}, (7, "all", "", "merge_queue")),
    ({"repo": "osac-test-infra"}, (7, "all", "osac-test-infra", "all")),
])
def test_the_filters_are_validated(params, expected):
    filters = insights.Filters({key: [value] for key, value in params.items()}, NOW)

    assert (filters.days, filters.suite, filters.repo, filters.trigger) == expected
    assert filters.notes == []


@pytest.mark.parametrize("params", [{"suite": "netris"}, {"trigger": "nightly"}, {"repo": "../etc"}])
def test_an_unknown_filter_is_a_note_and_is_ignored(params):
    filters = insights.Filters({key: [value] for key, value in params.items()}, NOW)

    assert (filters.suite, filters.repo, filters.trigger) == ("all", "", "all")
    assert len(filters.notes) == 1


def test_the_previous_window_exists_only_inside_the_archive():
    assert insights.Filters({"days": ["30"]}, NOW).has_previous is True
    assert insights.Filters({"days": ["90"]}, NOW).has_previous is False


def test_the_suite_trigger_and_repository_filters_narrow_the_runs(stores):
    stores.run(NOW - timedelta(hours=2), suite="vmaas", event="schedule")
    stores.run(NOW - timedelta(hours=3), suite="caas", event="merge_group", workflow="E2E caas (merge queue)")
    stores.run(NOW - timedelta(hours=4), suite="bmaas", event="workflow_dispatch",
               workflow="E2E suite · bmaas · #12 · abcdef1", repo="osac-test-infra")
    # Netris and an agentless CaaS run are other scenarios: never counted.
    stores.run(NOW - timedelta(hours=5), workflow="E2E Netris Full Install")
    stores.run(NOW - timedelta(hours=6), workflow="E2E CaaS Agentless Net Full Install")
    api = stores.api()

    assert get(api, "health")["tiles"][0]["runs"] == 3
    assert get(api, "health", suite="caas")["tiles"][0]["runs"] == 1
    assert get(api, "health", trigger="pr")["tiles"][0]["runs"] == 1
    assert get(api, "health", trigger="merge_queue")["tiles"][0]["runs"] == 1
    assert get(api, "health", repo="osac-test-infra")["tiles"][0]["runs"] == 1


# -- time -------------------------------------------------------------------------------------------


def test_the_phases_of_a_passing_run_add_up_to_its_time(stores):
    stores.run(NOW - timedelta(hours=5), queue_s=120)
    stores.run(NOW - timedelta(hours=6), "failure", failed="Install OSAC")

    document = get(stores.api(), "time")
    table = {row["phase"]: row for row in document["phase_table"]}
    total = document["totals"][0]

    assert total["passing_runs"] == 1, "a failed run stops early, so phases use passing runs"
    assert table["queue"]["mean_s"] == 120
    assert (table["cluster"]["mean_s"], table["install"]["mean_s"], table["test"]["mean_s"]) == (600, 900, 1800)
    assert sum(row["mean_s"] for row in document["phase_table"]) == total["mean_result_s"]
    assert round(sum(row["share"] for row in document["phase_table"]), 3) == 1.0
    steps = {row["step"]: row for row in document["steps"]}
    assert steps["Install OSAC"]["phase_label"] == "Install OSAC"
    assert steps["Install OSAC"]["median_s"] == 900


def test_runner_hours_count_every_run_and_name_the_failed_share(stores):
    stores.run(NOW - timedelta(hours=5), queue_s=0)
    stores.run(NOW - timedelta(hours=6), "failure", queue_s=0, failed="Run E2E tests (ci-observe capture)")

    total = get(stores.api(), "time")["totals"][0]

    passing = sum(seconds for _, seconds in STEPS)
    failing = sum(seconds for _, seconds in STEPS[:5])
    assert total["runner_hours"] == round((passing + failing) / 3600, 1)
    assert total["failed_share"] == round(total["failed_hours"] / total["runner_hours"], 4)


# -- tests and one test --------------------------------------------------------------------------------


def seed_tests(stores):
    run_a = stores.run(NOW - timedelta(days=2))
    run_b = stores.run(NOW - timedelta(days=1))
    old = stores.run(NOW - timedelta(days=9))
    created_a, created_b, created_old = NOW - timedelta(days=2), NOW - timedelta(days=1), NOW - timedelta(days=9)
    for run_id, created in ((run_a, created_a), (run_b, created_b)):
        stores.test(run_id, "tests/e2e/vmaas/sanity/test_vm.py::TestVm::test_create", True, 100.0, created)
        stores.test(run_id, "tests.e2e.vmaas.sanity.test_vm.TestVm::test_delete", run_id == run_a, 20.0, created,
                    message="StuckState: VM vm-1234 not Ready after 600s")
    stores.test(old, "tests/e2e/vmaas/sanity/test_vm.py::TestVm::test_create", True, 80.0, created_old)
    return run_a, run_b


def test_the_tests_document_ranks_by_time_failures_or_name(stores):
    seed_tests(stores)
    api = stores.api()

    by_time = get(api, "tests")["tests"]
    by_failures = get(api, "tests", sort="failures")["tests"]

    assert [row["test"] for row in by_time] == ["test_vm::TestVm::test_create", "test_vm::TestVm::test_delete"]
    assert [row["test"] for row in by_failures][0] == "test_vm::TestVm::test_delete"
    create = by_time[0]
    assert (create["runs"], create["median_s"], create["median_prev_s"]) == (2, 100.0, 80.0)
    assert create["delta"] == 0.25, "the median is 25% slower than the window before"
    assert create["group"] == "vmaas/sanity"
    assert create["test_url"].startswith(phases.TEST_PAGE)
    assert [row["test"] for row in get(api, "tests", q="delete")["tests"]] == ["test_vm::TestVm::test_delete"]
    assert len(get(api, "tests", limit=1)["tests"]) == 1
    assert get(api, "tests")["options"][0] == {
        "text": "VMaaS · vmaas/sanity · test_vm::TestVm::test_create",
        "value": "tests.e2e.vmaas.sanity.test_vm::TestVm::test_create",
    }


def test_one_test_is_found_by_any_of_its_names(stores):
    seed_tests(stores)
    api = stores.api()

    for name in (
        "tests.e2e.vmaas.sanity.test_vm::TestVm::test_delete",
        "tests/e2e/vmaas/sanity/test_vm.py::TestVm::test_delete",
        "test_delete",
    ):
        document = get(api, "test", test=name)
        assert document["test_key"] == "tests.e2e.vmaas.sanity.test_vm::TestVm::test_delete", name
        assert document["summary"][0]["failed"] == 1

    ambiguous = get(api, "test", test="test_vm")
    assert any("2 tests contain" in note for note in ambiguous["notes"])
    missing = get(api, "test", test="test_nothing")
    assert missing["summary"] == [] and any("no test in this window" in note for note in missing["notes"])
    assert any("choose a test" in note for note in get(api, "test")["notes"])


def test_one_test_lists_every_run_and_groups_its_errors(stores):
    run_a, run_b = seed_tests(stores)
    stores.capture("vmaas-main-a1", run_a)

    document = get(stores.api(), "test", test="test_delete")
    runs = {row["run"]: row for row in document["runs"]}

    assert runs[run_a]["result"] == "passed" and runs[run_a]["passed_s"] == 20.0 and runs[run_a]["failed_s"] is None
    assert runs[run_b]["result"] == "failed" and runs[run_b]["failed_s"] == 20.0
    assert runs[run_a]["details_url"].startswith(phases.RUN_PAGE), "a recorded run opens its run page"
    assert runs[run_b]["details_url"].startswith("https://github.com/"), "an unrecorded one opens GitHub"
    assert document["messages"] == [{
        "message": "StuckState: VM <id> not Ready after N", "count": 1, "last_seen": runs[run_b]["time"],
        "example": "StuckState: VM vm-1234 not Ready after 600s", "run": run_b,
    }]
    assert [row["failures"] for row in document["daily"]] == [0, 1]


def test_one_test_splits_its_time_from_the_span_rollup(stores):
    run_a, run_b = seed_tests(stores)
    key = "tests.e2e.vmaas.sanity.test_vm::TestVm::test_create"
    stores.spans("cap-a", run_a, NOW - timedelta(days=2), [span_test(key, 30.0, 60.0, 10.0, wait_call=40.0)],
                 [span_wait(key, "ComputeInstance ci-1 to become Ready", "ComputeInstance", 40.0)],
                 [{"test_key": key, "nodeid": key, "fixture": "cluster", "scope": "session", "started_at": iso(NOW),
                   "duration_s": 25.0}])
    stores.spans("cap-b", run_b, NOW - timedelta(days=1), [span_test(key, 10.0, 80.0, 10.0, wait_setup=4.0,
                                                                     wait_call=60.0)],
                 [span_wait(key, "ComputeInstance ci-2 to become Ready", "ComputeInstance", 64.0, failed=1)])

    document = get(stores.api(), "test", test=key)
    split = document["breakdown"][0]

    assert document["summary"][0]["recorded_runs"] == 2
    assert (split["setup"], split["waits"], split["actions"], split["teardown"]) == (18.0, 52.0, 20.0, 10.0)
    assert sum(split[part] for part in ("setup", "waits", "actions", "teardown")) == 100.0
    assert len(document["waits"]) == 1, "two waits on two instances are one wait signature"
    wait = document["waits"][0]
    assert (wait["count"], wait["per_run"], wait["timeouts"], wait["resource_kind"]) == (2, 1.0, 1, "ComputeInstance")
    assert document["fixtures"][0]["fixture"] == "cluster"


# -- waits ------------------------------------------------------------------------------------------


def test_the_tests_time_split_adds_up_and_names_the_product_share(stores):
    run_a = stores.run(NOW - timedelta(days=1))
    run_b = stores.run(NOW - timedelta(days=1, hours=1), suite="caas")
    stores.spans("cap-a", run_a, NOW - timedelta(days=1), [span_test("a", 10.0, 70.0, 20.0, wait_call=50.0)],
                 [span_wait("a", "VM vm-1 to become Ready", "VirtualMachine", 50.0)])
    stores.spans("cap-b", run_b, NOW - timedelta(days=1), [span_test("b", 0.0, 100.0, 0.0, wait_call=100.0)],
                 [span_wait("b", "ClusterOrder co-1 to become Ready", "ClusterOrder", 100.0)], suite="caas")

    document = get(stores.api(), "waits")
    rows = {row["suite"]: row for row in document["test_time"]}

    assert rows["vmaas"]["wait_share"] == 0.5 and rows["caas"]["wait_share"] == 1.0
    assert rows["all"]["waits"] == round(150 / 3600, 2)
    for row in document["test_time"]:
        if row["tests"]:
            shares = sum(row[f"{part}_share"] for part in ("setup", "waits", "actions", "teardown"))
            assert round(shares, 3) == 1.0, row["suite"]
    kinds = {row["resource_kind"]: row for row in document["kinds"]}
    assert kinds["ClusterOrder"]["share"] == 0.5, "of all recorded test time"
    assert document["chart"][0] == {"category": "ClusterOrder", "hours": round(100 / 3600, 2)}
    assert document["coverage"][0]["recorded_runs"] == 2


def test_the_repository_filter_reaches_the_span_rollup_through_the_run(stores):
    ours = stores.run(NOW - timedelta(days=1), repo="osac")
    theirs = stores.run(NOW - timedelta(days=1, hours=2), repo="osac-test-infra")
    stores.spans("cap-ours", ours, NOW - timedelta(days=1), [span_test("a", 0.0, 10.0, 0.0, wait_call=10.0)],
                 [span_wait("a", "VM vm-1 to become Ready", "VirtualMachine", 10.0)])
    stores.spans("cap-theirs", theirs, NOW - timedelta(days=1), [span_test("b", 0.0, 99.0, 0.0, wait_call=99.0)],
                 [span_wait("b", "ClusterOrder co-1 to become Ready", "ClusterOrder", 99.0)])

    document = get(stores.api(), "waits", repo="osac")

    assert [row["resource_kind"] for row in document["kinds"]] == ["VirtualMachine"]
    assert document["coverage"][0]["recorded_runs"] == 1


def test_an_incomplete_trace_adds_no_waits(stores):
    run_id = stores.run(NOW - timedelta(days=1))
    stores.spans("cap", run_id, NOW - timedelta(days=1), [span_test("a", 0.0, 10.0, 0.0, wait_call=10.0)],
                 [span_wait("a", "VM vm-1 to become Ready", "VirtualMachine", 10.0)], state="incomplete")

    document = get(stores.api(), "waits")

    assert document["kinds"] == [] and document["coverage"][0]["incomplete_runs"] == 1
    assert any("complete trace" in note for note in document["notes"])


# -- missing stores, the cache, the about row ------------------------------------------------------


def test_missing_stores_are_notes_never_errors(tmp_path):
    api = insights.CIObserveInsights(str(tmp_path / "none.db"), str(tmp_path / "none-trends.db"), clock=lambda: NOW)

    for kind in insights.CIObserveInsights.KINDS:
        document = get(api, kind, test="anything")
        assert document["notes"], kind
        assert document["about"][0]["missing"] != "Nothing is missing in this window.", kind
    assert get(api, "health")["tiles"][0]["flaky_tests"] is None, "not measured, never zero"
    assert get(api, "nothing")["notes"] == ["unknown document 'nothing'"]


def test_every_document_says_what_it_counts_and_when(stores):
    stores.run(NOW - timedelta(hours=5))

    for kind in ("health", "time", "failures", "tests", "waits"):
        about = get(stores.api(), kind, days=14)["about"][0]
        assert about["population"], kind
        assert about["window"] == "2026-10-24 12:00 to 2026-11-07 12:00 UTC", kind
        assert about["generated_at"] == "2026-11-07T12:00:00Z"


def test_a_document_is_computed_once_per_window_until_it_expires(stores, monkeypatch):
    api = stores.api()
    calls = []
    original = api._health
    monkeypatch.setattr(api, "_health", lambda filters, params: calls.append(1) or original(filters, params))
    clock = [1000.0]
    monkeypatch.setattr(insights.time, "monotonic", lambda: clock[0])

    first = get(api, "health")
    assert get(api, "health") is first
    get(api, "health", days=14)
    assert len(calls) == 2, "another window is another document"
    clock[0] += insights.CACHE_SECONDS + 1
    get(api, "health")
    assert len(calls) == 3, "an expired document is computed again"


def test_concurrent_requests_share_one_computation(stores, monkeypatch):
    api = stores.api()
    calls = []
    release = threading.Event()

    def slow(filters, params):
        calls.append(1)
        release.wait(5)
        return {"tiles": []}

    monkeypatch.setattr(api, "_health", slow)
    results = []
    threads = [threading.Thread(target=lambda: results.append(get(api, "health"))) for _ in range(4)]
    for thread in threads:
        thread.start()
    time.sleep(0.2)
    release.set()
    for thread in threads:
        thread.join(5)

    assert len(calls) == 1
    assert len(results) == 4 and all(result is results[0] for result in results)


# -- helpers ------------------------------------------------------------------------------------


def test_percentiles_are_nearest_rank():
    assert insights.percentile([], 0.5) is None
    assert insights.percentile([5, None, 1, 3], 0.5) == 3
    assert insights.percentile([1, 2, 3, 4], 0.9) == 4
    assert insights.change(12, 10) == 0.2
    assert insights.change(12, 0) is None


def test_an_error_message_groups_without_its_ids_and_numbers():
    signature = insights.message_signature(
        "Timeout: ClusterOrder co-7f3a2 not Ready after 1800s (uuid 0f8fad5b-d9cb-469f-a165-70867728950e)"
    )

    assert signature == "Timeout: ClusterOrder <id> not Ready after N (uuid <id>)"


def test_a_tests_time_parts_add_up_to_its_phases():
    row = {"setup_s": 10.0, "call_s": 50.0, "teardown_s": 5.0, "wait_s": 35.0, "wait_setup_s": 5.0,
           "wait_call_s": 30.0, "wait_teardown_s": 0.0}

    assert insights.time_parts(row) == (5.0, 35.0, 20.0, 5.0)
    assert sum(insights.time_parts(row)) == 65.0


# -- the seeded dataset -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    directory = tmp_path_factory.mktemp("seed")
    paths = ci_observe_seed.seed(directory, now=NOW)
    return insights.CIObserveInsights(paths["jobs_db"], paths["trends_db"], clock=lambda: NOW)


@pytest.mark.parametrize("days", [7, 30])
def test_every_seeded_document_adds_up(seeded, days):
    health = get(seeded, "health", days=days)
    every = next(row for row in health["suites"] if row["suite"] == "all")
    assert every["runs"] == sum(row["runs"] for row in health["suites"] if row["suite"] != "all")
    assert every["runs"] == health["tiles"][0]["runs"]
    failures = get(seeded, "failures", days=days)
    tile = failures["tiles"][0]
    assert tile["failed"] == sum(row["failures"] for row in failures["causes"])
    assert tile["failed"] == sum(tile[area] for area in ("tests", "build", "install", "infra", "blocked"))
    for day in failures["daily"]:
        assert day["failed"] == sum(day[key] for key in phases.AREA_KEYS.values())
    assert failures["runs"] and failures["tests"] and failures["messages"]
    tests = get(seeded, "tests", days=days)["tests"]
    assert round(sum(row["share"] for row in tests), 2) == 1.0
    waits = get(seeded, "waits", days=days)
    assert waits["kinds"] and waits["fixtures"]
    one = get(seeded, "test", days=days, test=tests[0]["test_key"])
    assert one["summary"][0]["runs"] == len(one["runs"])
    assert one["breakdown"] and one["waits"]
    time_document = get(seeded, "time", days=days)
    assert {row["category"] for row in time_document["phases"]} == {"VMaaS", "CaaS", "BMaaS", "All suites"}


def test_the_seeded_test_that_gets_slower_says_so(seeded):
    rows = {row["test_key"]: row for row in get(seeded, "tests", days=14)["tests"]}

    assert rows[ci_observe_seed.GETTING_SLOWER]["delta"] > 0.03
