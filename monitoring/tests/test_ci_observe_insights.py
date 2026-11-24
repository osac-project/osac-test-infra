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
import yaml

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
        self.incidents_file = str(directory / "incidents.yml")
        # No recorded incident until a test writes one: the documents' notes
        # stay about the stores the test built.
        self.write_incidents([])
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
        workflow = workflow or {
            "vmaas": "E2E VMaaS Full Install", "caas": "E2E CaaS Full Install", "bmaas": "E2E BMaaS Full Install",
            "it-fulfillment": "IT fulfillment (Tests)", "it-operator": "IT operator (Tests)",
            "it-bmf": "IT BMF (Tests)", "it-caas": "IT CaaS (Tests)",
        }[suite]
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
        return insights.CIObserveInsights(self.jobs_db, self.trends_db, clock=lambda: now,
                                          incidents_file=self.incidents_file)

    def write_incidents(self, records):
        """Replace the incidents file the api() reads."""
        Path(self.incidents_file).write_text(yaml.safe_dump({"incidents": records}), encoding="utf-8")


@pytest.fixture
def stores(tmp_path):
    return Stores(tmp_path)


def get(api, kind, **params):
    return api.get_json(kind, {name: [str(value)] for name, value in params.items()})


def span_test(key, setup, call, teardown, wait_setup=0.0, wait_call=0.0, wait_teardown=0.0):
    def total(*values):
        return None if any(value is None for value in values) else sum(values)

    return {
        "test_key": key, "nodeid": key, "worker": "gw0", "status": "passed", "started_at": iso(NOW),
        "duration_s": total(setup, call, teardown),
        "setup_s": setup, "call_s": call, "teardown_s": teardown,
        "wait_s": total(wait_setup, wait_call, wait_teardown),
        "wait_setup_s": wait_setup, "wait_call_s": wait_call,
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


def test_a_failure_hidden_by_a_re_run_shows_in_the_first_try_pass_rate(stores):
    """GitHub keeps one row per run and a re-run replaces its outcome: a run
    that failed and passed on its second attempt reads as passed."""
    stores.run(NOW - timedelta(hours=3), "success")
    stores.run(NOW - timedelta(hours=6), "success", attempt=2)
    stores.run(NOW - timedelta(hours=9), "failure", failed="Run E2E tests")
    stores.run(NOW - timedelta(hours=12), "cancelled")

    document = get(stores.api(), "health")
    tile, all_suites = document["tiles"][0], document["suites"][-1]

    assert (tile["pass_rate"], tile["first_try_pass_rate"]) == (round(2 / 3, 4), round(1 / 3, 4))
    assert (tile["reruns"], tile["rerun_share"]) == (1, 0.25)
    assert (all_suites["first_try_pass_rate"], all_suites["reruns"]) == (round(1 / 3, 4), 1)
    assert document["daily"][0]["first_try_pass_rate"] == round(1 / 3, 4)


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
    stored_since(stores)
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

    assert set(rows) == {*phases.SUITES, "all"}
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


def test_an_end_moves_the_window_and_the_cache_key():
    filters = insights.Filters({"days": ["7"], "end": ["2026-11-01"]}, NOW)
    assert filters.window_to == datetime(2026, 11, 1, tzinfo=UTC)
    assert filters.window_from == datetime(2026, 10, 25, tzinfo=UTC)
    assert filters.key() == (7, "all", "", "all", "2026-11-01T00:00:00Z")
    assert filters.window()["end"] == "2026-11-01T00:00:00Z"
    # Without end the window ends now, as the dashboards' fixed windows do.
    assert insights.Filters({"days": ["7"]}, NOW).key() == (7, "all", "", "all", "")


@pytest.mark.parametrize(("end", "note_wants"), [
    ("2026-11-01T12:00:00Z", None),  # a full UTC time is accepted too
    ("2026-12-01", "in the future"),
    ("yesterday", "neither a date"),
])
def test_an_end_that_is_not_a_past_moment_is_a_note_and_ends_now(end, note_wants):
    filters = insights.Filters({"days": ["7"], "end": [end]}, NOW)
    if note_wants:
        assert filters.window_to == NOW
        assert len(filters.notes) == 1 and note_wants in filters.notes[0]
    else:
        assert filters.window_to == datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
        assert filters.notes == []


def test_an_end_window_counts_only_the_runs_before_it(stores):
    stores.run(NOW - timedelta(days=1), suite="vmaas")
    stores.run(NOW - timedelta(days=4), suite="caas")
    stores.run(NOW - timedelta(days=9), suite="bmaas")
    api = stores.api()

    # The default 7-day window holds the runs of Nov 6 and Nov 3, not Oct 29.
    assert get(api, "health")["tiles"][0]["runs"] == 2
    # The window [2026-11-02, 2026-11-04]: only the run of Nov 3 is inside.
    ended = get(api, "health", days="2", end="2026-11-04")
    assert ended["tiles"][0]["runs"] == 1
    assert ended["window"][0]["end"] == "2026-11-04T00:00:00Z"
    assert ended["about"][0]["window"] == "2026-11-02 00:00 to 2026-11-04 00:00 UTC"
    # Two different ends are two different documents, not one cache entry.
    assert get(api, "health", days="2", end="2026-11-05")["tiles"][0]["runs"] == 1
    assert get(api, "health", days="2", end="2026-11-04")["tiles"][0]["runs"] == 1


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


def test_flake_campaign_runs_are_counted_only_when_asked_for_by_name(stores):
    """A campaign re-runs one fixed commit a hundred-plus times; under Any
    trigger its rows would swamp every pass rate, so "all" leaves them out
    and "flake" is the only selection that shows them -- their pass rate is
    the flake rate."""
    stores.run(NOW - timedelta(hours=2), suite="it-fulfillment", event="schedule")
    stores.run(NOW - timedelta(hours=3), suite="it-fulfillment", event="schedule",
               workflow="IT fulfillment (Flake detection)")
    stores.run(NOW - timedelta(hours=4), "failure", suite="it-caas", event="schedule",
               workflow="IT CaaS (Flake detection)")
    api = stores.api()

    assert get(api, "health")["tiles"][0]["runs"] == 1, "only the plain Tests run counts by default"
    assert get(api, "health", trigger="scheduled")["tiles"][0]["runs"] == 1
    flake = get(api, "health", trigger="flake")
    assert flake["tiles"][0]["runs"] == 2
    assert flake["tiles"][0]["failed"] == 1
    by_suite = {row["suite"]: row for row in flake["suites"]}
    assert by_suite["it-fulfillment"]["runs"] == 1
    assert by_suite["it-caas"]["runs"] == 1
    assert "only when asked for by name" in get(api, "health")["population"]


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


def stored_since(stores, days_ago=61):
    """A run older than any window compared here: the stores reach back to
    it, so 'vs before' has a fully stored span to compare with."""
    stores.run(NOW - timedelta(days=days_ago))


def seed_tests(stores):
    stored_since(stores)
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


def test_every_test_link_and_option_reaches_its_test(stores):
    """Grafana runs a data link's URL through encodeURI: a percent-escaped
    key would arrive escaped twice and match no test. Links and the Test
    drop-down carry the key unescaped, or its hash when a URL cannot."""
    run_id = stores.run(NOW - timedelta(days=1))
    stores.test(run_id, "tests/e2e/vmaas/sanity/test_vm.py::TestVm::test_create", True, 100.0, NOW - timedelta(days=1))
    stores.test(run_id, "tests/e2e/vmaas/sanity/test_p.py::test_p[a&b]", True, 10.0, NOW - timedelta(days=1))
    api = stores.api()

    document = get(api, "tests")

    for row in document["tests"]:
        assert "%" not in row["test_url"], row["test_url"]
    for option in document["options"]:
        assert get(api, "test", test=option["value"])["summary"], option


def test_tests_of_out_of_scope_runs_are_not_counted(stores):
    """Staging Install calls the VMaaS full-install workflow, so its JUnit
    artifact is filed as VMaaS. The run pages leave the run out; so must the
    test pages. A merge-queue suite row carries its parent run's id, which is
    the id its tests are stored under."""
    created = NOW - timedelta(days=1)
    in_scope = stores.run(created)
    staging = stores.run(created, workflow="E2E VMaaS Staging Install")
    with sqlite3.connect(stores.jobs_db) as conn:
        conn.execute(
            "INSERT INTO jobs (id, run_id, repo, workflow, category, conclusion, status, event, created_at)"
            " VALUES (77, '7000', 'osac', 'E2E vmaas (merge queue)', 'e2e', 'success', 'completed', 'merge_group', ?)",
            (iso(created),),
        )
    for run_id in (in_scope, staging, "7000"):
        stores.test(run_id, "tests/e2e/vmaas/sanity/test_vm.py::TestVm::test_create", run_id != staging, 100.0,
                    created)

    tests = get(stores.api(), "tests")["tests"]
    health = get(stores.api(), "health")["tiles"][0]

    assert [(row["test"], row["runs"]) for row in tests] == [("test_vm::TestVm::test_create", 2)]
    assert health["failing_tests"] == 0, "the Staging failure is not a VMaaS failure"


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


def test_one_wait_on_differently_named_vms_is_one_row(stores):
    """Each run names its VM with a random suffix ("vm-ssvtr", "vm-9btxs").
    A signature stored before the suffix rule existed still groups with the
    signatures stored after it."""
    run_a, _ = seed_tests(stores)
    key = "tests.e2e.vmaas.sanity.test_vm::TestVm::test_create"
    old = span_wait(key, "vm-ssvtr Running", "ComputeInstance", 30.0)
    old["signature"] = "vm-ssvtr Running"
    stores.spans("cap", run_a, NOW - timedelta(days=1), [span_test(key, 0.0, 100.0, 0.0, wait_call=90.0)],
                 [old, span_wait(key, "vm-9btxs Running", "ComputeInstance", 20.0),
                  span_wait(key, "vm-ltcml Running", "ComputeInstance", 40.0)])

    waits = get(stores.api(), "waits")["waits"]
    test_waits = get(stores.api(), "test", test=key)["waits"]

    assert [(row["wait"], row["waits"]) for row in waits] == [("<id> Running", 3)]
    assert [(row["wait"], row["count"]) for row in test_waits] == [("<id> Running", 3)]


def test_an_incomplete_trace_adds_no_waits(stores):
    run_id = stores.run(NOW - timedelta(days=1))
    stores.spans("cap", run_id, NOW - timedelta(days=1), [span_test("a", 0.0, 10.0, 0.0, wait_call=10.0)],
                 [span_wait("a", "VM vm-1 to become Ready", "VirtualMachine", 10.0)], state="incomplete")

    document = get(stores.api(), "waits")

    assert document["kinds"] == [] and document["coverage"][0]["incomplete_runs"] == 1
    assert any("complete trace" in note for note in document["notes"])


# -- missing stores, the cache, the about row ------------------------------------------------------


def test_an_unreadable_rollup_pass_table_is_unknown_freshness_not_a_crash(stores):
    """A read failure of the rollup pass table is 'freshness unknown', not
    a crash and not 'no pass recorded yet'. The handler that says unknown
    called a logger the module never defined, so the honest path raised
    NameError and took the document down instead."""
    stores.run(NOW - timedelta(hours=2))
    record_passes(stores, trends=0.5, spans=1)
    # A file that opens but is not a database: _open returns a connection,
    # the first execute raises sqlite3.Error inside rollup_passes.
    Path(stores.trends_db).write_bytes(b"SQLite format 3\x00" + b"\x00" * 4080)

    assert stores.api().stores.rollup_passes() is None
    text, _notes = stores.api()._freshness(NOW)
    assert "could not be read, so their freshness is unknown" in text
    assert "no pass recorded yet" not in text, "unknown freshness is not 'never ran'"


def test_missing_stores_are_notes_never_errors(tmp_path):
    # No incidents file either: every kind, the incidents document included,
    # must answer with a note rather than an error.
    api = insights.CIObserveInsights(str(tmp_path / "none.db"), str(tmp_path / "none-trends.db"),
                                     clock=lambda: NOW, incidents_file=str(tmp_path / "no-incidents.yml"))

    for kind in insights.CIObserveInsights.KINDS:
        document = get(api, kind, test="anything")
        assert document["notes"], kind
        assert document["about"][0]["missing"] != "Nothing is missing in this window.", kind
    assert get(api, "health")["tiles"][0]["flaky_tests"] is None, "not measured, never zero"
    assert get(api, "nothing")["notes"] == ["unknown document 'nothing'"]


def test_every_document_says_what_it_counts_and_when(stores):
    stores.run(NOW - timedelta(hours=5))

    for kind in ("health", "time", "failures", "tests", "waits", "incidents"):
        about = get(stores.api(), kind, days=14)["about"][0]
        assert about["population"], kind
        assert about["window"] == "2026-10-24 12:00 to 2026-11-07 12:00 UTC", kind
        assert about["generated_at"] == "2026-11-07T12:00:00Z"


def record_passes(stores, **ages):
    with sqlite3.connect(stores.trends_db) as conn:
        for job, hours in ages.items():
            phases.record_pass(conn, job, NOW - timedelta(hours=hours))


def test_the_about_row_says_how_old_each_source_is(stores):
    stored_since(stores)
    stores.run(NOW - timedelta(hours=2))
    record_passes(stores, trends=0.5, spans=1, logs=1.5)

    about = get(stores.api(), "health")["about"][0]

    assert about["sources"] == (
        "Runs: up to 2026-11-07 10:00 UTC · Test results: updated 2026-11-07 11:30 UTC"
        " · Inside the tests: updated 2026-11-07 11:00 UTC · Error signatures: updated 2026-11-07 10:30 UTC"
        " · Not checked against GitHub yet (ci-observe-audit.timer)"
    )
    assert about["missing"] == "Nothing is missing in this window."


def test_a_log_rollup_that_stopped_says_so(stores):
    stored_since(stores)
    stores.run(NOW - timedelta(hours=2))
    record_passes(stores, trends=0.5, spans=1, logs=30)
    about = get(stores.api(), "failures")["about"][0]
    assert "error signatures were last updated 30 h ago (ci-observe-logs.timer)" in about["missing"]


def write_audit(stores, hours_ago, **e2e):
    block = {"listed_from": "2026-10-10T12:00:00Z", "runs_listed": 5309, "runs_missing": 0, "runs_other_result": 0,
             "lists_incomplete": 0, "sample": 30, "sample_differ": 0, "recount_ok": True, **e2e}
    summary = {"checked_at": (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ"), "days": 30,
               "e2e": block, "prs": None, "ok": True}
    folder = Path(stores.jobs_db).parent
    (folder / phases.AUDIT_FILE_NAME).write_text(json.dumps({"summary": summary, "e2e": {"sample": []}}))


def test_the_about_row_says_what_the_check_against_github_found(stores):
    stored_since(stores)
    stores.run(NOW - timedelta(hours=2))
    record_passes(stores, trends=0.5, spans=1)
    write_audit(stores, 3)
    about = get(stores.api(), "health")["about"][0]
    assert about["sources"].endswith(
        "Checked against GitHub 2026-11-07 09:00 UTC: 5,309 completed suite runs since 2026-10-10, 0 not stored;"
        " 30 sampled runs, 0 differ"
    )
    assert about["missing"] == "Nothing is missing in this window."

    write_audit(stores, 3, runs_missing=61, runs_other_result=2, sample_differ=1, recount_ok=False,
                lists_incomplete=1)
    missing = get(stores.api(), "health")["about"][0]["missing"]
    assert "found 61 of 5,309 completed suite runs since 2026-10-10 not stored" in missing
    assert "found 2 stored runs with an outdated result or attempt" in missing
    assert "1 of 30 sampled runs differ from GitHub (ci-observe-audit.json lists them)" in missing
    assert "the Overview's counts differ from a recount of the stored rows" in missing
    assert "GitHub could not list every run of 1 workflows" in missing


def test_a_late_check_against_github_says_so(stores):
    stores.run(NOW - timedelta(hours=2))
    write_audit(stores, 40)
    missing = get(stores.api(), "failures")["about"][0]["missing"]
    assert "the last check against GitHub ran 40 h ago (2026-11-05 20:00 UTC); the daily check is late" in missing


def test_an_unreadable_check_is_a_broken_check_not_no_check(stores):
    # "Not checked yet" would hide that the guard itself is down.
    stores.run(NOW - timedelta(hours=2))
    (Path(stores.jobs_db).parent / phases.AUDIT_FILE_NAME).write_text("{not json")
    about = get(stores.api(), "health")["about"][0]
    assert "The check against GitHub cannot be read (ci-observe-audit.json could not be read" in about["sources"]
    assert "the result of the daily check against GitHub cannot be read" in about["missing"]


def test_without_stored_runs_before_the_window_nothing_is_compared(stores):
    """The stores hold three weeks; a 30-day window has no earlier window.
    Comparing with nothing would make every count read as all new."""
    stores.run(NOW - timedelta(days=21), "failure", failed="Run E2E tests")
    stores.run(NOW - timedelta(days=10), "success")
    stores.run(NOW - timedelta(days=2), "failure", failed="Run E2E tests")
    stores.run(NOW - timedelta(days=1), "success")

    month = get(stores.api(), "health", days="30")
    causes = get(stores.api(), "failures", days="30")["causes"]
    week = get(stores.api(), "health", days="7")

    assert month["tiles"][0]["pass_rate_delta"] is None
    assert all(row["delta"] is None for row in causes)
    assert "'vs before' is empty: the stored runs start on 2026-10-17" in month["about"][0]["missing"]
    assert week["tiles"][0]["pass_rate_delta"] is not None, "a week ago is stored"
    assert "vs before" not in week["about"][0]["missing"]


def test_a_window_longer_than_the_stored_test_results_says_so(stores):
    """Runs reach back further than the JUnit reports read from them; the
    test pages must not look like the tests started running last week."""
    stored_since(stores)
    run_id = stores.run(NOW - timedelta(days=3))
    stores.test(run_id, "tests/e2e/vmaas/sanity/test_vm.py::TestVm::test_create", True, 100.0, NOW - timedelta(days=3))

    month = get(stores.api(), "tests", days="30")["about"][0]["missing"]
    week = get(stores.api(), "tests", days="7")["about"][0]["missing"]
    time = get(stores.api(), "time", days="30")["about"][0]["missing"]

    assert "test results are stored from 2026-11-04: the test numbers do not cover 2026-10-08 to 2026-11-04" in month
    assert "test results are stored from 2026-11-04" in week
    assert "test results are stored from 2026-11-04" in time, "its test table counts tests"


def test_a_rollup_that_stopped_says_what_is_missing(stores):
    """A timer that stops leaves its last pass behind: the page must not
    claim nothing is missing while its test numbers end a day ago."""
    stores.run(NOW - timedelta(hours=2))
    record_passes(stores, trends=26, spans=1)

    for kind in ("health", "tests", "test", "failures", "time", "waits"):
        missing = get(stores.api(), kind, test="anything")["about"][0]["missing"]
        assert "test results were last updated 26 h ago (ci-observe-trends.timer)" in missing, kind
        assert "2026-11-06 10:00 UTC" in missing, kind


def test_a_source_never_written_is_named_without_a_note(stores):
    stores.run(NOW - timedelta(days=2))

    about = get(stores.api(), "health")["about"][0]

    assert "Test results: no pass recorded yet" in about["sources"]
    assert "no workflow run has been stored for 48 h" in about["missing"]


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
    parts = insights.time_parts(span_test("k", 10.0, 70.0, 20.0, wait_call=50.0))
    assert sum(parts) == 100.0
    # A phase the trace did not measure is None, never 0: "not measured"
    # must not read as "took no time".
    parts = insights.time_parts(span_test("k", None, 70.0, 20.0))
    assert parts[0] is None and parts[1] == 0.0 and sum(
        value for value in parts if value is not None) == 90.0


def test_the_waits_page_answers_when_a_phase_was_not_measured(stores):
    """A test_spans row with a NULL phase (the span rollup writes one
    whenever a trace lacks a phase span) must not crash the waits page:
    the part is excluded from its column and from the total."""
    run = stores.run(NOW - timedelta(days=1))
    stores.spans("cap-a", run, NOW - timedelta(days=1),
                 [span_test("a", None, 70.0, 20.0, wait_call=None)])

    document = get(stores.api(), "waits")
    row = next(row for row in document["test_time"] if row["tests"])
    assert row["setup"] is None, "the unmeasured phase is unknown, not 0 hours"
    assert row["actions"] == round(70.0 / 3600, 2)
    row = {"setup_s": 10.0, "call_s": 50.0, "teardown_s": 5.0, "wait_s": 35.0, "wait_setup_s": 5.0,
           "wait_call_s": 30.0, "wait_teardown_s": 0.0}

    assert insights.time_parts(row) == (5.0, 35.0, 20.0, 5.0)
    assert sum(insights.time_parts(row)) == 65.0


# -- the seeded dataset -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    directory = tmp_path_factory.mktemp("seed")
    paths = ci_observe_seed.seed(directory, now=NOW)
    incidents_file = directory / "incidents.yml"
    incidents_file.write_text("incidents: []\n", encoding="utf-8")
    return insights.CIObserveInsights(paths["jobs_db"], paths["trends_db"], clock=lambda: NOW,
                                      incidents_file=str(incidents_file))


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
    assert {row["category"] for row in time_document["phases"]} == {
        "VMaaS", "CaaS", "BMaaS", "IT fulfillment", "IT operator", "IT BMF", "IT CaaS", "All suites"}


def test_the_seeded_test_that_gets_slower_says_so(seeded):
    rows = {row["test_key"]: row for row in get(seeded, "tests", days=14)["tests"]}

    assert rows[ci_observe_seed.GETTING_SLOWER]["delta"] > 0.03


# -- the error signatures of the artifact-log rollup ----------------------------------------


def index_bundles(stores, runs, signatures, attempt="1", state="indexed"):
    """The rollup's tables: each (run id, outcome) indexed, with the given
    signatures [(signature, phase)] for it."""
    with sqlite3.connect(stores.trends_db) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS artifact_logs (run_id TEXT NOT NULL, suite TEXT NOT NULL,"
                     " repo TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',"
                     " files INTEGER NOT NULL DEFAULT 0, lines INTEGER NOT NULL DEFAULT 0,"
                     " error_lines INTEGER NOT NULL DEFAULT 0, skipped_files INTEGER NOT NULL DEFAULT 0,"
                     " skipped_lines INTEGER NOT NULL DEFAULT 0, ingested_at TEXT NOT NULL,"
                     " attempt TEXT NOT NULL DEFAULT '', PRIMARY KEY (run_id, suite))")
        conn.execute("CREATE TABLE IF NOT EXISTS artifact_log_signatures (run_id TEXT NOT NULL, suite TEXT NOT NULL,"
                     " kind TEXT NOT NULL, signature TEXT NOT NULL, outcome TEXT NOT NULL, phase TEXT NOT NULL,"
                     " lines INTEGER NOT NULL, PRIMARY KEY (run_id, suite, kind, signature))")
        for run_id, outcome in runs:
            conn.execute("INSERT INTO artifact_logs (run_id, suite, repo, state, ingested_at, attempt)"
                         " VALUES (?, 'vmaas', 'osac', ?, ?, ?)", (run_id, state, iso(NOW), attempt))
            for signature, phase in signatures(run_id, outcome):
                conn.execute("INSERT INTO artifact_log_signatures VALUES (?, 'vmaas', 'pod', ?, ?, ?, 3)",
                             (run_id, signature, outcome, phase))


def test_the_cause_outranks_the_noise_every_run_logs_when_most_runs_fail(stores):
    """The review's case: 80 failed and 20 passing runs. Every run logs the
    same noise; 60 failed runs and no passing one log the cause. By counts
    the noise led (80 - 20 = 60 against 60 - 0); by shares it is 100% of
    both, a difference of nothing, and the cause is 75% against 0%."""
    runs = []
    for i in range(100):
        failed = i < 80
        run_id = stores.run(NOW - timedelta(hours=i + 1), conclusion="failure" if failed else "success",
                            failed="Install OSAC" if failed else None)
        runs.append((run_id, "failure" if failed else "success", i))
    index_bundles(stores, [(run_id, outcome) for run_id, outcome, _ in runs],
                  lambda run_id, outcome: [("informer sync timeout after <n>s", "install" if outcome == "failure"
                                            else "")] + ([("cert-manager cannot get lease", "install")]
                                                         if outcome == "failure" and int(run_id) % 100 < 61 else []))
    rows = get(stores.api(), "failures", days="7", suite="vmaas")["signatures"]
    assert [row["signature"] for row in rows] == ["cert-manager cannot get lease", "informer sync timeout after <n>s"]
    cause, noise = rows
    assert (noise["failed_runs"], noise["passing_runs"], noise["score"]) == (80, 20, 0.0)
    assert cause["passing_runs"] == 0 and cause["score"] == cause["failed_share"] > 0.7


def test_one_signature_is_one_row_whatever_phase_its_runs_stopped_in(stores):
    # Failed runs stop in different phases and passing runs in none; a row
    # per phase gave every failed phase "0 passing runs".
    failed_install = stores.run(NOW - timedelta(hours=1), conclusion="failure", failed="Install OSAC")
    failed_test = stores.run(NOW - timedelta(hours=2), conclusion="failure", failed="Run E2E tests")
    passing = stores.run(NOW - timedelta(hours=3))
    index_bundles(stores, [(failed_install, "failure"), (failed_test, "failure"), (passing, "success")],
                  lambda run_id, outcome: [("the same noise", {failed_install: "install", failed_test: "test"}
                                            .get(run_id, ""))])
    rows = get(stores.api(), "failures", days="7")["signatures"]
    assert len(rows) == 1
    assert (rows[0]["failed_runs"], rows[0]["passing_runs"]) == (2, 1)


def test_the_signatures_say_how_much_of_the_window_they_cover(stores):
    indexed = stores.run(NOW - timedelta(hours=1), conclusion="failure", failed="Install OSAC")
    stores.run(NOW - timedelta(hours=2), conclusion="failure", failed="Install OSAC")  # no bundle indexed
    index_bundles(stores, [(indexed, "failure")], lambda run_id, outcome: [("an error", "install")])
    notes = get(stores.api(), "failures", days="7")["notes"]
    assert any("cover 1 of 2 failed and 0 of 0 passing suite runs" in note for note in notes)
    assert any("cannot tell a cause from the noise" in note for note in notes)


def test_an_unreadable_signature_store_is_unknown_not_empty(stores, monkeypatch):
    stores.run(NOW - timedelta(hours=1), conclusion="failure", failed="Install OSAC")
    api = stores.api()
    # A store that exists but cannot be opened (a permission, a lock) is a
    # read failure, not "nothing indexed yet".
    real_connect = insights.sqlite3.connect

    def connect(database, *args, **kwargs):
        if str(stores.trends_db) in str(database):
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(insights.sqlite3, "connect", connect)
    notes = get(api, "failures", days="7")["notes"]
    assert any("could not be read" in note for note in notes)
    assert not any("has indexed no bundle" in note for note in notes)


def test_each_signature_links_its_runs_lines_by_metadata_over_their_time(stores):
    run_id = stores.run(NOW - timedelta(hours=5), conclusion="failure", failed="Install OSAC")
    index_bundles(stores, [(run_id, "failure")], lambda run_id, outcome: [("an error", "install")])
    [row] = get(stores.api(), "failures", days="7")["signatures"]
    assert row["logs_query"] == f'{{source="artifact"}} | run_id=~"{run_id}"'
    assert row["logs_from"] == iso(NOW - timedelta(hours=7))
    assert row["logs_to"] > row["logs_from"]


def test_a_reruns_older_attempt_does_not_count_under_its_newer_result(stores):
    """A run that failed, was indexed, and then passed on a re-run: until
    the rollup reads the new bundle, the run has no indexed bundle at its
    latest attempt -- its old failure signatures must not count in the
    passing pool, and the coverage note must say it is not covered."""
    with sqlite3.connect(stores.jobs_db) as conn:
        for attempt, conclusion, failed in ((1, "failure", "Install OSAC"), (2, "success", "")):
            conn.execute(
                "INSERT INTO jobs (run_id, repo, workflow, category, status, conclusion, event, created_at,"
                " updated_at, run_attempt) VALUES ('777', 'osac', 'E2E VMaaS Full Install', 'e2e', 'completed',"
                " ?, 'schedule', ?, ?, ?)",
                (conclusion, iso(NOW - timedelta(hours=4)), iso(NOW - timedelta(hours=2) if attempt == 2
                                                                else NOW - timedelta(hours=3)), attempt))
    index_bundles(stores, [("777", "failure")], lambda run_id, outcome: [("cert-manager cannot get lease", "install")],
                  attempt="1")
    rows = get(stores.api(), "failures", days="7")["signatures"]
    assert rows == [], "the older attempt's signatures are not this result's"
    notes = get(stores.api(), "failures", days="7")["notes"]
    assert any("cover 0 of 0 failed and 0 of 1 passing" in note for note in notes)


def test_a_bundle_loki_rejected_lines_for_still_counts_its_signatures(stores):
    """A rejected bundle was read: its signatures are bundle facts, and
    the ranking counts them even though the lines are not in Loki."""
    failed = stores.run(NOW - timedelta(hours=1), conclusion="failure", failed="Install OSAC")
    index_bundles(stores, [(failed, "failure")], lambda run_id, outcome: [("an error", "install")],
                  state="rejected")
    rows = get(stores.api(), "failures", days="7")["signatures"]
    assert [row["signature"] for row in rows] == ["an error"]


# -- windows the stores do not wholly hold --------------------------------------------------


def test_a_window_that_starts_before_the_stored_runs_says_which_part_it_covers(stores):
    for days_ago in (20, 15, 12):
        stores.run(NOW - timedelta(days=days_ago))
    doc = get(stores.api(), "health", days="30", end=(NOW - timedelta(days=10)).strftime("%Y-%m-%d"))
    assert doc["tiles"][0]["runs"] == 3
    assert any("after this window's start" in note and "cover 2026-10-18" in note for note in doc["notes"])


def test_vs_before_of_a_past_window_names_no_date_it_will_never_reach(stores):
    stores.run(NOW - timedelta(days=20))
    doc = get(stores.api(), "health", days="30", end=(NOW - timedelta(days=10)).strftime("%Y-%m-%d"))
    note = next(note for note in doc["notes"] if note.startswith("'vs before' is empty"))
    assert "after the start of the 30 days before the window" in note
    assert "are stored from" not in note


def test_a_signature_table_from_before_the_attempt_column_is_read(stores):
    """The rollup adds artifact_logs.attempt when it next opens the store;
    until then the rows are the first attempt's. Selecting the column before
    that made the whole table unreadable after the 2026-11-22 deploy."""
    failed = stores.run(NOW - timedelta(hours=1), conclusion="failure", failed="Install OSAC")
    with sqlite3.connect(stores.trends_db) as conn:
        conn.execute("CREATE TABLE artifact_logs (run_id TEXT NOT NULL, suite TEXT NOT NULL, repo TEXT NOT NULL,"
                     " state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', ingested_at TEXT NOT NULL,"
                     " PRIMARY KEY (run_id, suite))")
        conn.execute("CREATE TABLE artifact_log_signatures (run_id TEXT NOT NULL, suite TEXT NOT NULL,"
                     " kind TEXT NOT NULL, signature TEXT NOT NULL, outcome TEXT NOT NULL, phase TEXT NOT NULL,"
                     " lines INTEGER NOT NULL, PRIMARY KEY (run_id, suite, kind, signature))")
        conn.execute("INSERT INTO artifact_logs VALUES (?, 'vmaas', 'osac', 'indexed', '', ?)", (failed, iso(NOW)))
        conn.execute("INSERT INTO artifact_log_signatures VALUES (?, 'vmaas', 'pod', 'an error', 'failure',"
                     " 'install', 3)", (failed,))
    document = get(stores.api(), "failures", days="7")
    assert [row["signature"] for row in document["signatures"]] == ["an error"]
    assert not any("could not be read" in note for note in document["notes"])


def test_an_unreadable_signature_table_names_sqlites_reason(stores):
    stores.run(NOW - timedelta(hours=1), conclusion="failure", failed="Install OSAC")
    with sqlite3.connect(stores.trends_db) as conn:
        conn.execute("CREATE TABLE artifact_logs (run_id TEXT)")
        conn.execute("CREATE TABLE artifact_log_signatures (run_id TEXT)")
    notes = get(stores.api(), "failures", days="7")["notes"]
    note = next(note for note in notes if "signature table could not be read" in note)
    assert "no such column" in note, "the page says why, not only that"


def test_runs_a_pass_left_for_the_next_ones_are_on_the_page(stores):
    """A pass the shared GitHub budget stopped is a pass; the runs it left
    are said next to its time, and in Missing or partial data."""
    stores.run(NOW - timedelta(hours=1))
    with sqlite3.connect(stores.trends_db) as conn:
        phases.record_pass(conn, "trends", NOW - timedelta(minutes=20), "summary", pending=1148)
    document = get(stores.api(), "health", days="7")
    assert "Test results: updated 2026-11-07 11:40 UTC, 1,148 runs not read yet" in document["about"][0]["sources"]
    assert any(note.startswith("1,148 runs are not in the test results yet") for note in document["notes"])


def test_an_old_pass_table_gains_its_pending_count(tmp_path):
    path = tmp_path / "trends.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE rollup_passes (job TEXT PRIMARY KEY, finished_at TEXT NOT NULL,"
                     " summary TEXT NOT NULL DEFAULT '')")
        conn.execute("INSERT INTO rollup_passes VALUES ('spans', '2026-11-07T10:00:00Z', '')")
        phases.record_pass(conn, "trends", NOW, "s", pending=3)
        assert sorted(conn.execute("SELECT job, pending FROM rollup_passes")) == [("spans", 0), ("trends", 3)]


def test_concurrent_documents_of_one_window_parse_its_runs_once(stores, monkeypatch):
    """An agent's fan-out (a kind per suite and trigger) asks one window
    many times at once: the runs are parsed once and shared, and every
    document still counts only its own population."""
    for hours in range(1, 7):
        stores.run(NOW - timedelta(hours=hours), suite="vmaas")
        stores.run(NOW - timedelta(hours=hours, minutes=5), suite="caas", conclusion="failure", failed="Install OSAC")
    api = stores.api()
    reads = []
    real = api.stores.suite_run_rows

    def counted(since, until):
        reads.append((since, until))
        time.sleep(0.05)  # long enough for every asker to arrive while the first parses
        return real(since, until)

    monkeypatch.setattr(api.stores, "suite_run_rows", counted)
    answers = {}

    def ask(kind, suite):
        answers[(kind, suite)] = get(api, kind, days="7", suite=suite)

    threads = [threading.Thread(target=ask, args=(kind, suite))
               for kind in ("health", "failures", "time") for suite in ("vmaas", "caas")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(reads) == 1, "one parse for the window, whatever the population"
    health = {suite: answers[("health", suite)]["suites"][0] for suite in ("vmaas", "caas")}
    assert (health["vmaas"]["runs"], health["vmaas"]["failed"]) == (6, 0)
    assert (health["caas"]["runs"], health["caas"]["failed"]) == (6, 6)


def test_the_shared_run_facts_keep_at_most_a_few_windows(stores, monkeypatch):
    """A window's facts are some 6 KB a run: three windows asked in a row
    keep the newest FACTS_CACHE_ENTRIES, not all of them."""
    stores.run(NOW - timedelta(hours=1))
    api = stores.api()
    for days in ("1", "7", "30"):
        get(api, "health", days=days)
    assert len(api._facts_cache) == insights.FACTS_CACHE_ENTRIES


# -- the incidents reverse gate ---------------------------------------------------------------------


def ongoing_incident(**overrides):
    """One identified incident with an open window, over vmaas only."""
    record = {
        "id": "INC-1",
        "title": "Installs fail on a broken snapshot",
        "status": "identified",
        "diagnosis_confidence": "confirmed",
        "window": {"from": "2026-10-01T00:00:00Z"},
        "areas": ["install"],
        "suites": ["vmaas"],
        "repos": ["osac"],
        "summary": "Install failures doubled from Nov 5. The snapshots were broken.",
        "contributing_factors": ["The freshness check failed silently, so broken snapshots rolled out."],
        "evidence": [{"label": "the post-mortem", "url": "https://example.com/pm"}],
        "follow_ups": [{"text": "rebuild the snapshot", "state": "open"}],
    }
    record.update(overrides)
    return record


def test_an_ongoing_incident_whose_suites_recovered_is_asked_to_close(stores):
    """The reverse gate: vmaas failed two of three runs in the week before
    the window and passes three of three in it, so the diagnosis outlived
    the incident -- the incidents document says so instead of serving the
    ongoing claim unchecked."""
    for hours in (9 * 24, 10 * 24):  # the week before the window: two failures
        stores.run(NOW - timedelta(hours=hours), "failure", failed="Install OSAC")
    stores.run(NOW - timedelta(hours=11 * 24), "success")
    for hours in (24, 48, 72):  # the window: three passes
        stores.run(NOW - timedelta(hours=hours), "success")
    stores.write_incidents([ongoing_incident()])

    document = get(stores.api(), "incidents", days=7)

    assert [row["id"] for row in document["incidents"]] == ["INC-1"]
    assert document["notes"] == [
        "record INC-1 claims ongoing, but its suites have shown no degradation since 2026-10-31 —"
        " close it or re-verify"
    ]
    assert document["about"][0]["missing"] == document["notes"][0], "a note, never an error"


def test_an_ongoing_incident_whose_suites_still_degrade_is_served_silently(stores):
    """The archive agrees with the record: no note, and the numbers stay
    the story the incident tells."""
    for hours in (9 * 24, 10 * 24, 11 * 24):  # the week before the window: three passes
        stores.run(NOW - timedelta(hours=hours), "success")
    for hours in (24, 48):  # the window: two failures
        stores.run(NOW - timedelta(hours=hours), "failure", failed="Install OSAC")
    stores.write_incidents([ongoing_incident()])

    document = get(stores.api(), "incidents", days=7)

    assert document["notes"] == []


def test_an_ongoing_incident_the_archive_cannot_rate_is_missing_data(stores):
    """No run of the record's suite is stored: the gate says the archive
    cannot answer -- missing data is named, never read as a healthy zero,
    so the close-it note cannot fire over an empty archive."""
    stores.run(NOW - timedelta(hours=48), "success")  # vmaas, not the record's suite
    stores.write_incidents([ongoing_incident(suites=["caas"])])

    document = get(stores.api(), "incidents", days=7)

    assert document["notes"] == [
        "incident INC-1 claims ongoing, but the archive cannot say whether its suites still degrade:"
        " no pass rate for caas to compare (the last 7 days against the 7 before them)"
    ]


def test_an_ongoing_incident_without_any_stored_runs_is_missing_data(tmp_path):
    """Neither store exists: the same rule over a deployment gap."""
    incidents_file = tmp_path / "incidents.yml"
    incidents_file.write_text(yaml.safe_dump({"incidents": [ongoing_incident()]}), encoding="utf-8")
    api = insights.CIObserveInsights(str(tmp_path / "none.db"), str(tmp_path / "none-trends.db"),
                                     clock=lambda: NOW, incidents_file=str(incidents_file))

    document = get(api, "incidents", days=7)

    assert [note for note in document["notes"] if "claims ongoing" in note] == [
        "incident INC-1 claims ongoing, but the archive cannot say whether its suites still degrade:"
        " no pass rate for vmaas to compare (the last 7 days against the 7 before them)"
    ]


def test_the_gate_needs_every_suite_of_the_record(stores):
    """One suite still degrading keeps the record honest; one suite the
    archive cannot rate is missing data: either way the close-it note does
    not fire."""
    for hours in (9 * 24, 10 * 24):
        stores.run(NOW - timedelta(hours=hours), "success")
        stores.run(NOW - timedelta(hours=hours), "success", suite="caas")
    stores.run(NOW - timedelta(hours=24), "success")
    stores.run(NOW - timedelta(hours=24), "failure", suite="caas", failed="Install OSAC")

    stores.write_incidents([ongoing_incident(suites=["vmaas", "caas"])])
    document = get(stores.api(), "incidents", days=7)
    assert document["notes"] == [], "caas is worse than the week before: the claim stands"

    stores.write_incidents([ongoing_incident(suites=["vmaas", "bmaas"])])
    document = get(stores.api(), "incidents", days=7)
    assert document["notes"] == [
        "incident INC-1 claims ongoing, but the archive cannot say whether its suites still degrade:"
        " no pass rate for bmaas to compare (the last 7 days against the 7 before them)"
    ]


def test_a_terminated_incident_is_not_cross_checked(stores):
    """The gate is for records that claim to be ongoing: a record whose
    window closed, or an ongoing one that names no suites to check, is
    served without the archive's opinion."""
    ended = {"from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"}
    for hours in (24, 48, 72):
        stores.run(NOW - timedelta(hours=hours), "success")
    stores.write_incidents([ongoing_incident(id="INC-over", status="fixed", window=ended,
                                             follow_ups=[{"text": "rebuild the snapshot",
                                                          "state": "done"}]),
                            ongoing_incident(id="INC-undiagnosed", status="ongoing",
                                             diagnosis_confidence="suspected", suites=[])])

    document = get(stores.api(), "incidents", days=7)

    assert [row["id"] for row in document["incidents"]] == ["INC-over", "INC-undiagnosed"]
    assert document["notes"] == []
