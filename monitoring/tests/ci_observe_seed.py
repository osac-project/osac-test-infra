"""A deterministic, realistic ci-observe dataset for tests and previews.

`seed(directory, now)` writes the three stores the ci-observe pages read,
shaped exactly like production: the workflow exporter's workflow-exporter.db
(E2E job rows with job and step timelines, and the capture registry),
trends.db (the JUnit rows of every run, the run archive and the span
rollup) and prs.db (pull requests and their queue attempts).

The numbers follow the real workflows: a VMaaS full install is about 100
minutes on a runner, CaaS and BMaaS about two and a half hours, tests are
the biggest phase, a few tests are flaky, one gets slower over the window,
and failures spread over tests, install, build, cluster and CI setup.
Nothing here reaches the network.
"""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

# (step name, phase-typical minutes, jitter fraction)
STEPS = {
    "vmaas": [
        ("Set up job", 0.2), ("Checkout repository", 0.3), ("Overlay tests from osac mono-repo", 0.4),
        ("Validate and bootstrap", 0.5), ("Resolve and validate components input", 0.2),
        ("Authorize fork PR", 0.1), ("Fetch and write secrets", 0.3), ("Ensure helm and yq are available", 0.3),
        ("Prepare cluster environment", 4.0), ("Boot cluster clone", 11.0),
        ("Resolve chart version to install", 0.5), ("Build and load component images", 12.0),
        ("Build test image", 3.0), ("Install grpcurl", 0.2), ("Install infrastructure operators", 9.0),
        ("Install OSAC", 16.0), ("Create test adapter route for metering tests", 0.3),
        ("Resolve effective test suite (fallback if tier dir is empty)", 0.1),
        ("Run E2E tests (ci-observe capture)", 34.0), ("Gather cluster logs", 2.5), ("Teardown", 1.5),
        ("Scan and upload artifacts", 1.2), ("Complete job", 0.1),
    ],
    "caas": [
        ("Set up job", 0.2), ("Checkout repository", 0.3), ("Overlay tests from osac mono-repo", 0.4),
        ("Validate and bootstrap", 0.5), ("Authorize fork PR", 0.1), ("Fetch and write secrets", 0.3),
        ("Fetch CaaS secrets", 0.2), ("Prepare cluster environment", 4.0), ("Boot cluster clone", 12.0),
        ("Build and load component images", 10.0), ("Build test image", 3.0),
        ("Prepare cluster prerequisites", 7.5), ("Install grpcurl", 0.2),
        ("Install infrastructure operators", 11.0), ("Setup virtual BareMetalHosts", 5.0), ("Install OSAC", 18.0),
        ("Setup CaaS agent", 3.0), ("Resolve effective test suite (fail if requested tier is empty)", 0.1),
        ("Run E2E tests (ci-observe capture)", 62.0), ("Gather cluster logs", 3.0), ("Teardown", 2.0),
        ("Scan and upload artifacts", 1.5), ("Complete job", 0.1),
    ],
    "bmaas": [
        ("Set up job", 0.2), ("Checkout repository", 0.3), ("Overlay tests from osac mono-repo", 0.4),
        ("Validate and bootstrap", 0.5), ("Authorize fork PR", 0.1), ("Fetch and write secrets", 0.3),
        ("Prepare cluster environment", 4.0), ("Boot cluster clone", 11.0),
        ("Setup virtual BareMetalHosts", 6.0), ("Build and load component images", 12.0),
        ("Build test image", 3.0), ("Install grpcurl", 0.2), ("Install infrastructure operators", 10.0),
        ("Install OSAC", 17.0), ("Resolve effective test selection", 0.1),
        ("Run E2E tests (ci-observe capture)", 70.0), ("Gather cluster logs", 3.0), ("Teardown", 2.0),
        ("Scan and upload artifacts", 1.5), ("Complete job", 0.1),
    ],
}

# Where a failed run stopped, and how often.
FAILURE_STEPS = [
    ("Run E2E tests (ci-observe capture)", 46),
    ("Install OSAC", 14),
    ("Install infrastructure operators", 6),
    ("Build and load component images", 9),
    ("Boot cluster clone", 9),
    ("Prepare cluster environment", 3),
    ("Fetch and write secrets", 3),
    ("Authorize fork PR", 3),
    ("", 7),  # no failed step: a timeout or a lost runner
]

# (module path under tests/e2e, test names, typical seconds)
TESTS = {
    "vmaas": [
        ("vmaas.sanity.test_compute_instance_creation", ["test_compute_instance_lifecycle",
                                                         "test_compute_instance_with_catalog_item"], 420),
        ("vmaas.sanity.test_compute_instance_restart", ["test_restart", "test_restart_updates_status"], 360),
        ("vmaas.sanity.test_virtual_network", ["test_virtual_network_lifecycle", "test_subnet_lifecycle"], 140),
        ("vmaas.regression.test_compute_instance_delete_during_provision", ["test_delete_during_provision"], 300),
        ("vmaas.sanity.test_security_group", ["test_security_group_rules", "test_security_group_attach"], 160),
        ("vmaas.sanity.test_external_ip", ["test_external_ip_attach", "test_external_ip_pool_exhausted"], 210),
        ("vmaas.test_self_subject_access_review", ["test_tenant_user_can_list"], 20),
        ("references.test_compute_references", ["test_catalog_item_references"], 45),
    ],
    "caas": [
        ("caas.sanity.test_cluster_create", ["test_cluster_create"], 1900),
        ("caas.sanity.test_cluster_scale", ["test_cluster_scale_up", "test_cluster_scale_down"], 900),
        ("caas.sanity.test_node_pool", ["test_node_pool_create", "test_node_pool_delete"], 700),
        ("caas.sanity.test_cluster_delete", ["test_cluster_delete"], 600),
    ],
    "bmaas": [
        ("bmaas.sanity.test_bare_metal_instance", ["test_bare_metal_instance_lifecycle",
                                                   "test_bare_metal_instance_power_cycle"], 1500),
        ("bmaas.sanity.test_host_inventory", ["test_host_inventory_lists_hosts"], 90),
        ("bmaas.sanity.test_bare_metal_network", ["test_bare_metal_attach_subnet"], 800),
        ("references.test_cluster_baremetal_references", ["test_baremetal_references"], 60),
    ],
}

# What each suite's tests wait on: (resource kind, description, typical seconds)
WAITS = {
    "vmaas": [("ComputeInstance", "ComputeInstance {id} to become Ready", 170),
              ("VirtualMachineInstance", "VirtualMachineInstance {id} to report Running", 45),
              ("Subnet", "Subnet {id} to become Ready", 25),
              ("ComputeInstance", "ComputeInstance {id} to be deleted", 60)],
    "caas": [("ClusterOrder", "ClusterOrder {id} to become Ready", 1300),
             ("NodePool", "NodePool {id} to reach its size", 420),
             ("ClusterOrder", "ClusterOrder {id} to be deleted", 300)],
    "bmaas": [("BareMetalInstance", "BareMetalInstance {id} to become Provisioned", 950),
              ("BareMetalHost", "BareMetalHost {id} to become available", 180),
              ("BareMetalInstance", "BareMetalInstance {id} to be deleted", 240)],
}

RUNS_PER_DAY = {"vmaas": 10, "caas": 5, "bmaas": 5}
TRIGGERS = [("pr", 55), ("merge_queue", 25), ("scheduled", 15), ("manual", 5)]
FLAKY = {"tests.e2e.vmaas.sanity.test_external_ip::test_external_ip_pool_exhausted": 0.25,
         "tests.e2e.caas.sanity.test_cluster_scale::test_cluster_scale_up": 0.15,
         "tests.e2e.bmaas.sanity.test_bare_metal_instance::test_bare_metal_instance_power_cycle": 0.2}
GETTING_SLOWER = "tests.e2e.vmaas.sanity.test_compute_instance_creation::test_compute_instance_lifecycle"
MESSAGES = {
    "test": (
        "StuckState: ComputeInstance ci-e2e-{id} to become Ready not reached after 600s; "
        "last value: 'Provisioning'"
    ),
    "flaky": "AssertionError: external IP pool ip-pool-{id} still has 1 free address",
}


def _load(name, filename):
    import importlib.util

    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(*parts):
    """A stable 40-hex commit id for the parts (hash() is salted per process)."""
    return hashlib.sha1(repr(parts).encode()).hexdigest()


def _pick(rng, weighted):
    total = sum(weight for _, weight in weighted)
    point = rng.uniform(0, total)
    for value, weight in weighted:
        point -= weight
        if point <= 0:
            return value
    return weighted[-1][0]


JOBS_SCHEMA = """
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY, repo TEXT, workflow TEXT, display_name TEXT, category TEXT, branch TEXT,
    pr_url TEXT, pr_display TEXT, status TEXT, conclusion TEXT, event TEXT, trigger TEXT,
    duration_s INTEGER, duration TEXT, actor TEXT, url TEXT, created_at TEXT, updated_at TEXT,
    run_number INTEGER, run_attempt INTEGER, failed_step TEXT, steps_json TEXT, failure_reason TEXT,
    runner_name TEXT, head_sha TEXT, run_id TEXT, timeline_json TEXT
);
CREATE INDEX idx_jobs_created_at ON jobs(created_at);
CREATE INDEX idx_jobs_run ON jobs(run_id);
CREATE TABLE captures (
    capture_id TEXT PRIMARY KEY, run_id TEXT NOT NULL DEFAULT '', run_attempt TEXT NOT NULL DEFAULT '',
    suite TEXT NOT NULL DEFAULT '', pr TEXT NOT NULL DEFAULT '', workflow TEXT NOT NULL DEFAULT '',
    test_ref TEXT NOT NULL DEFAULT '', test_sha TEXT NOT NULL DEFAULT '', test_infra_ref TEXT NOT NULL DEFAULT '',
    test_infra_sha TEXT NOT NULL DEFAULT '', test_infra_workflow_sha TEXT NOT NULL DEFAULT '',
    test_suite TEXT NOT NULL DEFAULT '', test_filter TEXT NOT NULL DEFAULT '', test_marker TEXT NOT NULL DEFAULT '',
    session_trace_id TEXT NOT NULL DEFAULT '', identity_source TEXT NOT NULL DEFAULT 'none',
    capture_started_at TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
"""


def _workflow(suite, trigger, pr, sha):
    label = {"vmaas": "VMaaS", "caas": "CaaS", "bmaas": "BMaaS"}[suite]
    if trigger == "pr":
        return f"E2E suite · {suite} · #{pr} · {sha}", "workflow_dispatch"
    if trigger == "merge_queue":
        return f"E2E {suite} (merge queue)", "merge_group"
    if trigger == "scheduled":
        return f"E2E {label} Full Install", "schedule"
    return f"E2E {label} Full Install", "workflow_dispatch"


def _timeline(rng, suite, created, failed_step, cancel_at, queue_s, slow_factor):
    """(jobs, steps, completed): the suite job and its steps."""
    started = created + timedelta(seconds=queue_s)
    moment = started
    steps = []
    failed = False
    for number, (name, minutes) in enumerate(STEPS[suite], 1):
        seconds = max(3.0, rng.gauss(minutes * 60, minutes * 60 * 0.12))
        if name.startswith("Run E2E tests"):
            seconds *= slow_factor
        conclusion = "success"
        collect = name in ("Gather cluster logs", "Teardown", "Scan and upload artifacts", "Complete job")
        if failed and not collect:
            steps.append({"number": number, "name": name, "status": "completed", "conclusion": "skipped",
                          "started_at": "", "completed_at": ""})
            continue
        if failed_step and name == failed_step:
            seconds *= rng.uniform(0.3, 1.0)
            conclusion = "failure"
            failed = True
        if cancel_at and name == cancel_at:
            steps.append({"number": number, "name": name, "status": "completed", "conclusion": "cancelled",
                          "started_at": _iso(moment), "completed_at": _iso(moment + timedelta(hours=3))})
            moment += timedelta(hours=3)
            failed = True
            continue
        steps.append({"number": number, "name": name, "status": "completed", "conclusion": conclusion,
                      "started_at": _iso(moment), "completed_at": _iso(moment + timedelta(seconds=seconds))})
        moment += timedelta(seconds=seconds)
    jobs = [{
        "name": f"{suite} / e2e-{suite}-full-install / e2e",
        "status": "completed",
        "conclusion": "failure" if failed else "success",
        "created_at": _iso(created),
        "started_at": _iso(started),
        "completed_at": _iso(moment),
        "runner_name": f"osac-{rng.randint(2, 8)}-runner-0{rng.randint(1, 4)}",
        "steps": steps,
    }]
    return jobs, moment


def seed(directory, now=None, days=40, rng_seed=7):
    """Write workflow-exporter.db, trends.db and prs.db into directory and
    return their paths. Deterministic for a given now and seed."""
    trends = _load("ci_observe_trends", "ci_observe_trends.py")
    spans = _load("ci_observe_spans", "ci_observe_spans.py")
    phases = _load("ci_observe_phases", "ci_observe_phases.py")
    now = (now or datetime.now(UTC)).replace(microsecond=0)
    rng = random.Random(rng_seed)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    jobs_db = directory / "workflow-exporter.db"
    trends_db = directory / "trends.db"
    prs_db = directory / "prs.db"
    for path in (jobs_db, trends_db, prs_db):
        if path.exists():
            path.unlink()

    jobs = sqlite3.connect(jobs_db)
    jobs.executescript(JOBS_SCHEMA)
    run_id = 40_000_000_000
    pr_numbers = list(range(1500, 1500 + days * 3))
    runs_for_rollup = []
    span_rows = []
    pr_runs = {}
    versions = {}
    for day_index in range(days, -1, -1):
        day = (now - timedelta(days=day_index)).replace(hour=0, minute=0, second=0)
        progress = (days - day_index) / days
        for suite, per_day in RUNS_PER_DAY.items():
            for _ in range(rng.randint(per_day - 2, per_day + 2)):
                created = day + timedelta(minutes=rng.randint(0, 23 * 60))
                if created > now - timedelta(hours=3):
                    continue
                run_id += rng.randint(3, 40)
                trigger = _pick(rng, TRIGGERS)
                pr = rng.choice(pr_numbers[: max(3, int(len(pr_numbers) * progress) + 3)])
                # A pull request's runs share its commit until the next push;
                # scheduled runs of one day share main's commit; a merge-queue
                # entry tests a merge commit of its own.
                if trigger == "pr":
                    if rng.random() < 0.3:
                        versions[pr] = versions.get(pr, 1) + 1
                    sha = _sha("pr", pr, versions.get(pr, 1))
                elif trigger in ("scheduled", "manual"):
                    sha = _sha("main", day_index)
                else:
                    sha = f"{rng.getrandbits(160):040x}"
                workflow, event = _workflow(suite, trigger, pr, sha[:40])
                repo = "osac" if rng.random() < 0.8 else "osac-test-infra"
                outcome = rng.random()
                cancelled = outcome < 0.05
                failed = not cancelled and outcome < (0.26 if suite == "vmaas" else 0.33)
                failed_step = _pick(rng, FAILURE_STEPS) if failed else ""
                if failed_step and failed_step not in [name for name, _ in STEPS[suite]]:
                    failed_step = "Run E2E tests (ci-observe capture)"
                cancel_at = "Install OSAC" if failed and not failed_step and rng.random() < 0.5 else ""
                queue_s = rng.expovariate(1 / (420 if trigger != "scheduled" else 120))
                slow = 1.0 + (0.25 * progress if suite == "vmaas" else 0)
                timeline, completed = _timeline(rng, suite, created, failed_step, cancel_at, queue_s, slow)
                if cancelled:
                    completed = created + timedelta(minutes=rng.randint(5, 60))
                conclusion = "cancelled" if cancelled else ("failure" if failed else "success")
                job = f"{suite} / e2e-{suite}-full-install / e2e"
                steps = [
                    {"name": step["name"], "duration_s": round(
                        phases.seconds_between(step["started_at"], step["completed_at"]) or 0)}
                    for step in timeline[0]["steps"] if step["started_at"]
                ]
                row_id = run_id if trigger != "merge_queue" else run_id * 10 + 1
                jobs.execute(
                    "INSERT INTO jobs (id, repo, workflow, display_name, category, branch, pr_url, pr_display,"
                    " status, conclusion, event, trigger, duration_s, url, created_at, updated_at, run_number,"
                    " run_attempt, failed_step, steps_json, failure_reason, runner_name, head_sha, run_id,"
                    " timeline_json) VALUES (?, ?, ?, ?, 'e2e', ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?,"
                    " ?, ?, ?, ?)",
                    (
                        row_id, repo, workflow,
                        f"osac / {workflow}", "main" if trigger in ("scheduled", "manual") else f"pr-{pr}",
                        f"#{pr}" if trigger == "merge_queue" else "",
                        conclusion, conclusion, event, trigger,
                        round((completed - created).total_seconds()),
                        f"https://github.com/osac-project/{repo}/actions/runs/{run_id}",
                        _iso(created), _iso(completed), 2 if rng.random() < 0.06 else 1,
                        f"{job} → {failed_step}" if failed_step else "", json.dumps(steps),
                        "" if not failed else ("test" if "tests" in failed_step else "infra"),
                        timeline[0]["runner_name"], "mainsha" if trigger == "pr" else sha, str(run_id),
                        json.dumps(timeline),
                    ),
                )
                if trigger == "pr":
                    pr_runs.setdefault(pr, []).append((suite, conclusion, sha))
                capture = f"{suite}-{'pr' + str(pr) if trigger == 'pr' else 'main'}-{run_id}-a1"
                tests_ran = not cancelled and (not failed or "tests" in failed_step)
                if tests_ran:
                    jobs.execute(
                        "INSERT INTO captures (capture_id, run_id, run_attempt, suite, pr, workflow,"
                        " first_seen_at, last_seen_at) VALUES (?, ?, '1', ?, ?, ?, ?, ?)",
                        (capture, str(run_id), suite, str(pr) if trigger == "pr" else "", workflow,
                         _iso(created), _iso(completed)),
                    )
                    cases, run_spans = _tests_for(rng, suite, failed, progress, capture, sha)
                    runs_for_rollup.append({
                        "run_id": run_id, "day": _iso(created)[:10], "repo": repo, "workflow": workflow,
                        "created_at": _iso(created), "source_sha": sha[:40], "trigger": trigger,
                        "pr": str(pr) if trigger == "pr" else "", "cases": cases,
                    })
                    span_rows.append((capture, run_id, suite, _iso(created), run_spans))
    jobs.commit()
    jobs.close()

    conn = trends.open_trends_db(str(trends_db))
    try:
        by_day = {}
        for run in runs_for_rollup:
            by_day.setdefault(run["day"], []).append(run)
        for day, day_runs in by_day.items():
            trends.write_day(conn, day, trends.rollup_rows(day_runs), {
                "day": day, "runs_listed": len(day_runs), "runs_parsed": len(day_runs), "runs_unavailable": 0,
                "generated_at": _iso(now), "parsed_run_ids": [run["run_id"] for run in day_runs],
            }, trends.test_run_rows(day_runs))
        trends.archive_suite_runs(conn, str(jobs_db))
    finally:
        conn.close()

    conn = spans.open_db(str(trends_db))
    try:
        for capture, rid, suite, created, (tests, waits, fixtures) in span_rows:
            spans.write_capture(conn, {
                "capture_id": capture, "run_id": rid, "run_attempt": 1, "suite": suite, "day": created[:10],
                "created_at": created, "trace_id": f"{rid:032x}", "state": "ok", "complete": 1,
                "spans_total": 400, "tests": len(tests), "waits": len(waits), "session_s": 2400.0,
                "unattributed_waits": 0, "rolled_at": _iso(now), "notes": "",
            }, tests, waits, fixtures)
    finally:
        conn.close()

    _seed_prs(prs_db, rng, now, days, pr_runs)
    return {"jobs_db": str(jobs_db), "trends_db": str(trends_db), "prs_db": str(prs_db)}


def _tests_for(rng, suite, failed, progress, capture, sha):
    """The JUnit cases and the span rows of one run's tests."""
    phases = sys.modules["ci_observe_phases"]
    cases = []
    tests = []
    waits = []
    fixtures = []
    failing_index = rng.randrange(sum(len(names) for _, names, _ in TESTS[suite])) if failed else -1
    index = 0
    for module, names, typical in TESTS[suite]:
        for name in names:
            classname = f"tests.e2e.{module}"
            key = phases.canonical_test_key(f"{classname}::{name}")
            seconds = max(5.0, rng.gauss(typical, typical * 0.15))
            if key == GETTING_SLOWER:
                seconds *= 1 + 0.6 * progress
            passed = index != failing_index
            message = ""
            if not passed:
                message = MESSAGES["test"].format(id=f"{rng.getrandbits(16):04x}")
            elif key in FLAKY and rng.random() < FLAKY[key]:
                passed = False
                message = MESSAGES["flaky"].format(id=rng.randint(1, 9))
            cases.append({"suite": "pytest", "test_id": f"{classname}::{name}", "passed": passed,
                          "duration_s": round(seconds, 3), "suite_key": suite, "message": message})
            nodeid = f"tests/e2e/{module.replace('.', '/')}.py::{name}"
            setup = seconds * rng.uniform(0.1, 0.25)
            teardown = seconds * rng.uniform(0.05, 0.15)
            call = seconds - setup - teardown
            kinds = WAITS[suite]
            wait_call = 0.0
            for kind, description, wait_typical in kinds[:2]:
                duration = min(call * 0.8, max(5.0, rng.gauss(wait_typical, wait_typical * 0.25)))
                if key == GETTING_SLOWER and kind == "ComputeInstance":
                    duration *= 1 + 0.6 * progress
                wait_call += duration
                waits.append({"nodeid": nodeid, "test_key": key, "phase": "call",
                              "name": description.format(id=f"e2e-{rng.getrandbits(16):04x}"),
                              "signature": description.format(id="<id>"), "resource_kind": kind,
                              "started_at": "", "duration_s": round(duration, 3), "attempts": int(duration // 5),
                              "condition_met": 1 if passed else 0, "failed": 0 if passed else 1})
            delete = kinds[-1]
            wait_teardown = min(teardown * 0.9, delete[2] * rng.uniform(0.5, 1.2))
            waits.append({"nodeid": nodeid, "test_key": key, "phase": "teardown",
                          "name": delete[1].format(id="x"), "signature": delete[1].format(id="<id>"),
                          "resource_kind": delete[0], "started_at": "", "duration_s": round(wait_teardown, 3),
                          "attempts": 6, "condition_met": 1, "failed": 0})
            shared = setup * 0.6 if index == 0 else 0.0
            tests.append({"nodeid": nodeid, "test_key": key, "worker": f"gw{index % 4}",
                          "status": "ok" if passed else "error", "started_at": "", "duration_s": round(seconds, 3),
                          "setup_s": round(setup, 3), "call_s": round(call, 3), "teardown_s": round(teardown, 3),
                          "wait_s": round(wait_call + wait_teardown, 3), "wait_setup_s": 0.0,
                          "wait_call_s": round(wait_call, 3), "wait_teardown_s": round(wait_teardown, 3),
                          "shared_setup_s": round(shared, 3), "waits": 3})
            if index == 0:
                for fixture, scope, fixture_seconds in (("tenant", "session", 95), ("jwt_grpc_tenant1", "session", 12),
                                                        ("default_subnet", "module", 40)):
                    fixtures.append({"nodeid": nodeid, "test_key": key, "fixture": fixture, "scope": scope,
                                     "started_at": "", "duration_s": round(rng.gauss(fixture_seconds, 5), 3)})
            index += 1
    return cases, (tests, waits, fixtures)


def _seed_prs(path, rng, now, days, pr_runs):
    prs = _load("ci_observe_prs", "ci_observe_prs.py")
    service = prs.CIObservePRs(db_file=str(path))
    service._init_db()
    with service._db() as conn:
        for number in sorted(pr_runs):
            opened = now - timedelta(days=rng.uniform(1, days), hours=rng.uniform(0, 20))
            first_review = opened + timedelta(hours=rng.expovariate(1 / 9))
            approval = first_review + timedelta(hours=rng.expovariate(1 / 14))
            queued = approval + timedelta(hours=rng.expovariate(1 / 3))
            attempts = 1 + (1 if rng.random() < 0.2 else 0)
            last_attempt = queued + timedelta(hours=1.8 * attempts + rng.uniform(0, 1))
            merged = last_attempt + timedelta(minutes=rng.uniform(1, 10))
            if merged > now:
                continue
            runs = pr_runs[number]
            suites = {suite for suite, _, _ in runs}
            record = {
                "repo": "osac", "number": number, "title": f"OSAC-{number}: change {number}", "author": "dev",
                "url": f"https://github.com/osac-project/osac/pull/{number}", "state": "merged", "labels": "",
                "opened_at": _iso(opened), "first_review_at": _iso(first_review), "first_approval_at": _iso(approval),
                "queued_at": _iso(queued), "merged_at": _iso(merged), "closed_at": _iso(merged),
                "last_attempt_at": _iso(last_attempt),
                "authoring_seconds": round((first_review - opened).total_seconds()),
                "review_seconds": round((approval - first_review).total_seconds()),
                "waiting_seconds": round((queued - approval).total_seconds()),
                "queue_seconds": round((last_attempt - queued).total_seconds()),
                "merged_seconds": round((merged - last_attempt).total_seconds()),
                "total_seconds": round((merged - opened).total_seconds()),
                "commit_count": rng.randint(1, 8), "review_count": rng.randint(1, 5),
                "review_request_count": 1, "retest_count": max(0, len(runs) - len(suites)),
                "final_stage": "merged", "fetched_at": _iso(now), "ready_at": _iso(opened),
                "review_wait_seconds": round((first_review - opened).total_seconds()),
                "force_push_count": rng.choice([0, 0, 1, 1, 2, 3]), "changes_requested_count": rng.choice([0, 0, 1]),
                "human_review_count": rng.randint(1, 4), "e2e_runs": len(runs),
                "e2e_failures": sum(1 for _, conclusion, _ in runs if conclusion == "failure"),
                "e2e_retests": max(0, len(runs) - len(suites)),
                "e2e_versions": len({sha for _, _, sha in runs}), "record_version": prs.RECORD_VERSION,
            }
            columns = list(record)
            conn.execute(
                f"INSERT INTO pr_events ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                [record[column] for column in columns],
            )
            for attempt in range(attempts):
                start = queued + timedelta(hours=1.8 * attempt)
                ejected = attempt < attempts - 1
                conn.execute(
                    "INSERT INTO pr_queue_attempts (repo, number, run_id, run_number, run_attempt, branch, head_sha,"
                    " run_ids, conclusion, started_at, completed_at, duration_seconds, url, ejected,"
                    " ejection_reason, ejection_kind, reason_source, fetched_at) VALUES ('osac', ?, ?, 1, 1, ?, ?,"
                    " ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (number, 50_000_000 + number * 10 + attempt, f"gh-readonly-queue/main/pr-{number}-abc", "abc",
                     json.dumps([50_000_000 + number * 10 + attempt]), "failure" if ejected else "success",
                     _iso(start), _iso(start + timedelta(hours=1.7)), round(1.7 * 3600),
                     "https://github.com/osac-project/osac/actions/runs/1", 1 if ejected else 0,
                     "E2E VMaaS failed in the merge queue" if ejected else "", "failed_check" if ejected else "",
                     "run" if ejected else "", _iso(now)),
                )
        conn.execute(
            "INSERT INTO pr_backfill_runs (ran_at, mode, window_from, window_to, stored, listing_complete, ok)"
            " VALUES (?, 'full', ?, ?, 1, 1, 1)",
            (_iso(now - timedelta(minutes=20)), _iso(now - timedelta(days=days)), _iso(now - timedelta(minutes=20))),
        )


if __name__ == "__main__":
    print(json.dumps(seed(sys.argv[1] if len(sys.argv) > 1 else "/tmp/ci-observe-seed"), indent=2))
