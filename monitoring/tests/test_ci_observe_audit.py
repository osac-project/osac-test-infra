"""The check against GitHub (ci_observe_audit.py), run against stores the real
exporter and the real backfill filled from the test fixtures: every check
agrees with a correct store, and a planted error is caught.

The audit must not import the code it checks; these tests may, to fill the
stores the way production does."""

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from test_ci_observe_prs import (  # noqa: F401  (github, lifecycle_github and service are fixtures)
    NOW, REPO, FakeAPI, github, lifecycle_github, load_fixture, run_backfill, service,
)

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name, filename):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


audit = _load("ci_observe_audit", "ci_observe_audit.py")

FIXTURES = ("merged_pr", "ejected_pr", "closed_pr", "lifecycle_pr")


def fake_github():
    """GitHub as the audit reads it, from the same fixtures the backfill read."""
    by_number = {}
    for name in FIXTURES:
        fixture = load_fixture(name)
        pull = dict(fixture["pr"])
        pull["changed_files"] = fixture["graph"]["changedFiles"]
        by_number[pull["number"]] = (pull, fixture)

    def fetch(url, body=None):
        if body is not None:
            number = body["variables"]["number"]
            graph = by_number[number][1]["graph"]
            nodes = [n for n in graph["timelineItems"]["nodes"]
                     if n["__typename"] in ("AddedToMergeQueueEvent", "RemovedFromMergeQueueEvent")]
            return {"data": {"repository": {"pullRequest": {"timelineItems": {"nodes": nodes}}}}}, ""
        path = url.split("api.github.com", 1)[1].split("per_page")[0].rstrip("?&")
        if path.startswith("/search/issues"):
            merged = sum(1 for pull, _ in by_number.values() if pull.get("merged_at"))
            return {"total_count": merged}, ""
        parts = path.split("/")
        number = int(parts[5])
        pull, fixture = by_number[number]
        tail = "/".join(parts[6:])
        return {
            "": pull, "reviews": fixture["reviews"], "comments": [], "timeline": fixture["timeline"],
        }.get(tail, pull if not tail else []), ""

    return audit.GitHub("t", fetch=fetch)


@pytest.fixture
def filled(prs, service, github, lifecycle_github):  # noqa: F811
    """prs.db with the fixture pull requests, as the backfill stores them."""
    from test_ci_observe_prs import PULLS_PATH, FakeResponse, route_pr, runs_window_path

    fixture = load_fixture("lifecycle_pr")
    route_pr(github, fixture)
    github.add(PULLS_PATH, FakeResponse(payload=[load_fixture(n)["pr"] for n in FIXTURES]))
    created = datetime.fromisoformat(fixture["pr"]["created_at"].replace("Z", "+00:00"))
    github.add(runs_window_path(created, NOW), FakeResponse(payload={"total_count": 0, "workflow_runs": []}))
    run_backfill(service)
    return service


def test_the_audit_agrees_with_a_correct_store(filled, capsys):
    status = audit.main(["--part", "prs", "--days", "30", "--sample", "10", "--db", filled.db_file, "--json"],
                        github=fake_github(), now=NOW,
                        api_get=lambda url: filled.get_json(dict(
                            (k, [v]) for k, v in (p.split("=") for p in url.split("?")[1].split("&"))), now=NOW))
    result = json.loads(capsys.readouterr().out)
    problems = {a["number"]: a["problems"] for a in result["pull_requests"]}
    window = {c["check"]: c for c in result["window"]}
    assert all(not p for p in problems.values()), problems
    assert all(c["ok"] for c in window.values()), window
    assert status == 0
    definitions = result["review_definitions"]
    assert definitions["merged in the sample"] == 3


def test_the_audit_catches_a_wrong_value(filled, capsys):
    with sqlite3.connect(filled.db_file) as conn:
        conn.execute("UPDATE pr_events SET first_approval_at = '2026-10-05T12:00:00Z', review_seconds = 0 "
                     "WHERE number = 104")
    status = audit.main(["--part", "prs", "--days", "30", "--sample", "10", "--db", filled.db_file],
                        github=fake_github(), now=NOW,
                        api_get=lambda url: filled.get_json(dict(
                            (k, [v]) for k, v in (p.split("=") for p in url.split("?")[1].split("&"))), now=NOW))
    out = capsys.readouterr().out
    assert status == 1
    assert "first_approval_at: stored 2026-10-05T12:00:00Z  github 2026-10-05T11:00:00Z" in out
    # First review 10:00 to the final approval 13:00, the last before the queue.
    assert "review_seconds: stored 0  github 10800" in out


def prs_api(filled):
    return lambda url: filled.get_json(dict(
        (k, [v]) for k, v in (p.split("=") for p in url.split("?")[1].split("&"))), now=NOW)


def test_the_report_lists_every_review_to_check_by_hand(filled, capsys):
    audit.main(["--part", "prs", "--days", "30", "--sample", "10", "--db", filled.db_file],
               github=fake_github(), now=NOW, api_get=prs_api(filled))
    out = capsys.readouterr().out
    assert "everything agrees" in out
    assert "How review happens, and the review time under each definition" in out
    assert "the page: first review -> final approval" in out
    assert "#104 by author4 into None" in out
    assert "rev-vm               review COMMENTED" in out


def test_a_pull_request_by_hand(filled, capsys):
    status = audit.main(["--part", "prs", "--days", "30", "--sample", "0", "--pr", "104", "--db", filled.db_file,
                         "--json"], github=fake_github(), now=NOW, api_get=prs_api(filled))
    result = json.loads(capsys.readouterr().out)
    assert [a["number"] for a in result["pull_requests"]] == [104]
    assert result["pull_requests"][0]["github"]["final_approval_at"] == "2026-10-05T13:00:00Z"
    assert status == 0


def test_the_result_file_is_what_the_pages_read(filled, tmp_path, capsys):
    with sqlite3.connect(filled.db_file) as conn:
        conn.execute("UPDATE pr_events SET files_changed = 99 WHERE number = 104")
    out = tmp_path / "data" / "ci-observe-audit.json"
    status = audit.main(["--part", "prs", "--days", "30", "--sample", "10", "--db", filled.db_file,
                         "--out", str(out), "--quiet"], github=fake_github(), now=NOW, api_get=prs_api(filled))
    assert capsys.readouterr().out == ""
    assert status == 0, "with --out a difference is data for the pages, not a failed unit"
    summary = json.loads(out.read_text())["summary"]
    assert summary["checked_at"] == "2026-10-15T00:00:00Z"
    assert summary["e2e"] is None
    assert summary["prs"] == {"repo": REPO, "sample": 3, "sample_differ": 1, "merged_ok": True, "api_ok": True,
                              "order_violations": 0, "order_examples": []}
    assert summary["ok"] is False
    assert not (tmp_path / "data" / "ci-observe-audit.json.partial").exists()


def test_the_machine_accounts_are_the_backfills(prs):
    assert audit.DEFAULT_MACHINE_ACCOUNTS == prs.DEFAULT_MACHINE_ACCOUNTS
    assert not audit.is_person({"login": "osac-ci-bot", "type": "User"}, "author1")
    assert not audit.is_person({"login": "openshift-ci-robot", "type": "User"}, "author1")
    assert not audit.is_person({"login": "author1", "type": "User"}, "author1")
    assert audit.is_person({"login": "reviewer1", "type": "User"}, "author1")


# -- E2E runs --------------------------------------------------------------------------------------------------------

JOB_IDS = iter(range(7000, 8000))


def gh_job(run_id, name, conclusion, created, minutes, attempt=1, failed_step=None):
    """A GitHub job: created, picked up 5 minutes later, two steps."""
    start = datetime.fromisoformat(created.replace("Z", "+00:00"))
    at = [audit.iso(start.replace(minute=start.minute + m)) for m in (0, 5, 5 + minutes // 2, 5 + minutes)]
    steps = [
        {"number": 1, "name": "Set up job", "status": "completed", "conclusion": "success",
         "started_at": at[1], "completed_at": at[2]},
        {"number": 2, "name": failed_step or "Run E2E tests", "status": "completed",
         "conclusion": "failure" if failed_step else "success", "started_at": at[2], "completed_at": at[3]},
    ]
    return {"id": next(JOB_IDS), "run_id": run_id, "run_attempt": attempt, "name": name, "status": "completed",
            "conclusion": conclusion, "created_at": at[0], "started_at": at[1], "completed_at": at[3],
            "runner_name": "box-runner-01", "html_url": f"https://github.com/osac-project/osac/actions/runs/{run_id}",
            "steps": steps}


def gh_run(run_id, name, path, conclusion, created, workflow_id, event="schedule"):
    return {"id": run_id, "name": name, "path": path, "workflow_id": workflow_id, "event": event,
            "status": "completed", "conclusion": conclusion, "head_branch": "main", "head_sha": f"{run_id:040x}",
            "pull_requests": [], "actor": {"login": "dev"}, "run_number": run_id, "run_attempt": 1,
            "html_url": f"https://github.com/osac-project/osac/actions/runs/{run_id}",
            "created_at": created, "run_started_at": created, "updated_at": created[:11] + "23:00:00Z"}


def e2e_world():
    """Four runs: two full installs, a merge-queue run holding two suites, a production install."""
    world = []
    run = gh_run(501, "E2E VMaaS Full Install", ".github/workflows/e2e-vmaas-full-install-caller.yml",
                 "success", "2026-10-08T10:00:00Z", 11)
    world.append((run, [gh_job(501, "e2e", "success", "2026-10-08T10:00:00Z", 40)]))
    run = gh_run(502, "E2E CaaS Full Install", ".github/workflows/e2e-caas-full-install-caller.yml",
                 "failure", "2026-10-09T10:00:00Z", 12)
    world.append((run, [gh_job(502, "e2e", "failure", "2026-10-09T10:00:00Z", 30, failed_step="Install OSAC")]))
    run = gh_run(503, "E2E for the merge queue", ".github/workflows/e2e-queue.yml", "failure",
                 "2026-10-10T10:00:00Z", 13, event="merge_group")
    world.append((run, [
        gh_job(503, "vmaas / e2e-vmaas-full-install / e2e", "success", "2026-10-10T10:01:00Z", 50),
        gh_job(503, "caas / e2e-caas-full-install / e2e", "failure", "2026-10-10T10:02:00Z", 20,
               failed_step="Run E2E tests"),
    ]))
    run = gh_run(504, "E2E VMaaS Production Install", ".github/workflows/e2e-vmaas-prod-install-caller.yml",
                 "success", "2026-10-11T10:00:00Z", 14)
    world.append((run, [gh_job(504, "e2e", "success", "2026-10-11T10:00:00Z", 30)]))
    return world


def e2e_github(world):
    runs = {run["id"]: (run, jobs) for run, jobs in world}
    workflows = {}
    for run, _ in world:
        workflows[run["workflow_id"]] = {"id": run["workflow_id"], "name": run["name"], "path": run["path"]}

    def fetch(url, body=None):
        parsed = urlparse(url)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        parts = parsed.path.split("/")[1:]  # repos, org, repo, actions, ...
        repo, rest = parts[2], parts[4:]
        if repo != "osac":
            return ({"workflows": []} if rest == ["workflows"] else {}), ""
        if rest == ["workflows"]:
            return {"total_count": len(workflows), "workflows": list(workflows.values())}, ""
        if rest[0] == "workflows":
            low, high = (audit.ts(x) for x in query["created"].split(".."))
            listed = [r for r, _ in runs.values() if r["workflow_id"] == int(rest[1])
                      and low <= audit.ts(r["created_at"]) <= high]
            return {"total_count": len(listed), "workflow_runs": listed if query.get("page") == "1" else []}, ""
        run, jobs = runs[int(rest[1])]
        if len(rest) == 2:
            return run, ""
        if rest[2] == "jobs":
            return {"total_count": len(jobs), "jobs": jobs if query.get("page") == "1" else []}, ""
        if rest[2] == "attempts":
            return run, ""
        raise AssertionError(url)

    return audit.GitHub("t", fetch=fetch)


@pytest.fixture
def stored_runs(exporter, wfe, tmp_path, monkeypatch):
    """The exporter's job history after it read the world, as production stores it."""
    world = e2e_world()
    jobs_of = {run["id"]: jobs for run, jobs in world}
    monkeypatch.setattr(exporter, "_fetch_run_jobs", lambda repo, run_id, run_attempt=None: jobs_of[run_id])
    for run, _ in world:
        assert exporter._process_completed_run("osac", run)
    insights = wfe.CIObserveInsights(str(tmp_path / "workflow-exporter.db"), str(tmp_path / "trends.db"),
                                     clock=lambda: NOW)
    return world, tmp_path, insights


def e2e_main(tmp_path, insights, world, *extra):
    def api_get(url):
        parsed = urlparse(url)
        assert parsed.path == "/api/ci-observe/health", url
        return insights.get_json("health", parse_qs(parsed.query))

    return audit.main(["--part", "e2e", "--days", "14", "--jobs-db", str(tmp_path / "workflow-exporter.db"),
                       "--trends-db", str(tmp_path / "trends.db"), *extra],
                      github=e2e_github(world), now=NOW, api_get=api_get)


def test_the_e2e_audit_agrees_with_what_the_exporter_stored(stored_runs, capsys):
    world, tmp_path, insights = stored_runs
    status = e2e_main(tmp_path, insights, world, "--json")
    result = json.loads(capsys.readouterr().out)["e2e"]
    assert [a["problems"] for a in result["sample"]] == [[], [], [], []]
    assert {a["workflow"] for a in result["sample"]} == {
        "E2E VMaaS Full Install", "E2E CaaS Full Install", "E2E vmaas (merge queue)", "E2E caas (merge queue)"}
    assert all(c["ok"] for c in result["recount"]), result["recount"]
    assert [c["recount"] for c in result["recount"]] == ["2/2/0/0", "2/0/2/0", "0/0/0/0", "4/2/2/0"]
    assert all(c["ok"] for c in result["completeness"]), result["completeness"]
    assert [(c["github"], c["stored"]) for c in result["completeness"]] == [(1, 1), (1, 1), (1, 1), (1, 1)]
    suites = {f["workflow"]: f["suite"] for f in result["workflows"]}
    assert suites["E2E VMaaS Production Install"] == ""
    assert suites["E2E vmaas (merge queue)"] == "vmaas"
    assert status == 0


def test_the_e2e_audit_catches_a_wrong_or_missing_run(stored_runs, capsys):
    world, tmp_path, insights = stored_runs
    with sqlite3.connect(tmp_path / "workflow-exporter.db") as conn:
        conn.execute("DELETE FROM jobs WHERE id = 501")
        conn.execute("UPDATE jobs SET conclusion = 'success', status = 'success', failed_step = '' "
                     "WHERE workflow = 'E2E caas (merge queue)'")
        timeline = json.loads(conn.execute("SELECT timeline_json FROM jobs WHERE id = 502").fetchone()[0])
        timeline[0]["started_at"] = "2026-10-09T10:01:00Z"
        conn.execute("UPDATE jobs SET timeline_json = ? WHERE id = 502", (json.dumps(timeline),))
    status = e2e_main(tmp_path, insights, world)
    out = capsys.readouterr().out
    assert status == 1
    assert "not stored: https://github.com/osac-project/osac/actions/runs/501" in out
    assert "conclusion: stored success  github failure" in out
    assert "failed_step: stored []  github ['caas / e2e-caas-full-install / e2e → Run E2E tests']" in out
    assert "job 'e2e' started_at: stored 2026-10-09T10:01:00Z  github 2026-10-09T10:05:00Z" in out


def test_the_e2e_report_names_what_it_compared(stored_runs, capsys):
    world, tmp_path, insights = stored_runs
    e2e_main(tmp_path, insights, world)
    out = capsys.readouterr().out
    assert "Overview counts against the stored rows, counted the way ci-observe/model.md says" in out
    assert "not counted  osac / E2E VMaaS Production Install: 1 rows (success 1)" in out
    assert "The sample: 4 stored suite runs (failures first), 0 that differ from GitHub" in out


def test_the_e2e_summary_counts_the_suite_workflows(stored_runs, capsys):
    world, tmp_path, insights = stored_runs
    with sqlite3.connect(tmp_path / "workflow-exporter.db") as conn:
        conn.execute("DELETE FROM jobs WHERE id IN (501, 504)")
    out = tmp_path / "ci-observe-audit.json"
    assert e2e_main(tmp_path, insights, world, "--out", str(out), "--quiet") == 0
    summary = json.loads(out.read_text())["summary"]["e2e"]
    # The production install is not a suite: its missing run is not on the pages.
    assert (summary["runs_listed"], summary["runs_missing"]) == (3, 1)
    assert summary["recount_ok"] is True


def test_a_gate_job_is_not_a_failed_test_step():
    assert audit.is_gate_job("e2e-caas-gate")
    assert audit.is_gate_job("vmaas / e2e-readiness")
    assert audit.is_gate_job("changes")
    assert not audit.is_gate_job("e2e-caas-full-install / e2e")


def test_a_long_list_is_split_until_github_lists_it_whole():
    asked = []
    since = datetime.fromisoformat("2026-10-01T00:00:00+00:00")
    until = datetime.fromisoformat("2026-10-03T00:00:00+00:00")

    def fetch(url, body=None):
        query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        low, high = (audit.ts(x) for x in query["created"].split(".."))
        asked.append((audit.iso(low), audit.iso(high)))
        if high - low > audit.timedelta(days=1):
            return {"total_count": 1500, "workflow_runs": []}, ""
        run = {"id": int(low.timestamp()), "created_at": audit.iso(low)}
        return {"total_count": 1, "workflow_runs": [run]}, ""

    runs, capped = audit.listed_runs(audit.GitHub("t", fetch=fetch), "osac", 11, since, until)
    assert not capped
    assert sorted(run["created_at"] for run in runs) == ["2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z"]
    assert asked[0] == ("2026-10-01T00:00:00Z", "2026-10-03T00:00:00Z")


def test_suite_workflows_are_the_counted_ones():
    assert audit.counted_workflow("E2E VMaaS Full Install")
    assert audit.counted_workflow("E2E BMaaS PR suite")
    assert audit.counted_workflow("E2E for the merge queue")
    assert not audit.counted_workflow("E2E VMaaS Netris Full Install")
    assert not audit.counted_workflow("E2E review relay")


def test_a_rerun_of_failed_jobs_keeps_each_suite_on_its_own_attempt(exporter, wfe, tmp_path, monkeypatch, capsys):
    """Attempt 2 re-runs the failed caas suite; GitHub's latest jobs keep vmaas's attempt-1 job (the API's
    filter=latest returns each job's most recent execution). vmaas ran once: it stays a first-try pass."""
    vmaas = gh_job(503, "vmaas / e2e-vmaas-full-install / e2e", "success", "2026-10-10T10:01:00Z", 50)
    caas1 = gh_job(503, "caas / e2e-caas-full-install / e2e", "failure", "2026-10-10T10:02:00Z", 20,
                   failed_step="Run E2E tests")
    caas2 = gh_job(503, "caas / e2e-caas-full-install / e2e", "success", "2026-10-10T12:00:00Z", 20, attempt=2)
    run1 = gh_run(503, "E2E for the merge queue", ".github/workflows/e2e-queue.yml", "failure",
                  "2026-10-10T10:00:00Z", 13, event="merge_group")
    run2 = run1 | {"run_attempt": 2, "conclusion": "success"}
    latest = {1: [vmaas, caas1], 2: [vmaas, caas2]}
    monkeypatch.setattr(exporter, "_fetch_run_jobs",
                        lambda repo, run_id, run_attempt=None: latest[current["attempt"]])
    current = {"attempt": 1}
    assert exporter._process_completed_run("osac", run1)
    current["attempt"] = 2
    assert exporter._process_completed_run("osac", run2)
    with exporter._db() as conn:
        stored = {r["id"]: dict(r) for r in conn.execute(
            "SELECT id, workflow, conclusion, run_attempt FROM jobs WHERE category = 'e2e'")}
    assert {(r["workflow"], r["conclusion"], r["run_attempt"]) for r in stored.values()} == {
        ("E2E vmaas (merge queue)", "success", 1),
        ("E2E caas (merge queue)", "failure", 1),
        ("E2E caas (merge queue)", "success", 2),
    }
    world = [(run2, [vmaas, caas1, caas2])]
    insights = wfe.CIObserveInsights(str(tmp_path / "workflow-exporter.db"), str(tmp_path / "trends.db"),
                                     clock=lambda: NOW)
    status = e2e_main(tmp_path, insights, world, "--json")
    result = json.loads(capsys.readouterr().out)["e2e"]
    assert [a["problems"] for a in result["sample"]] == [[], [], []]
    assert status == 0


def test_the_check_is_mounted_in_the_exporter_and_runs_daily():
    """It runs where the token, the stores and the API are, once a day, and
    writes where the pages read; every central install and update wires it."""
    monitoring = Path(__file__).resolve().parents[1]
    quadlet = (monitoring / "quadlet" / "workflow-exporter.container").read_text()
    assert "ci_observe_audit.py:/app/ci_observe_audit.py:ro,Z" in quadlet
    unit = (monitoring / "systemd" / "ci-observe-audit.service").read_text()
    assert ("podman exec workflow-exporter python3 /app/ci_observe_audit.py --days 30 --sample 60 --e2e-sample 30 "
            "--out /data/ci-observe-audit.json --quiet") in unit
    phases = _load("ci_observe_phases", "ci_observe_phases.py")
    assert unit.count(phases.AUDIT_FILE_NAME) == 2
    timer = (monitoring / "systemd" / "ci-observe-audit.timer").read_text()
    assert "OnCalendar=*-*-* 03:35:00 UTC" in timer
    assert "Persistent=true" in timer
    setup = (monitoring / "scripts" / "monitoring-setup.sh").read_text()
    assert setup.count('cp "${MONITORING_REPO_DIR}/scripts/ci_observe_audit.py"') == 2
    assert setup.count("systemd/ci-observe-audit.service") == 2
    assert setup.count("systemd/ci-observe-audit.timer") == 2
    assert "systemctl --user enable --now ci-observe-audit.timer" in setup
    assert "systemctl --user enable ci-observe-audit.timer" in setup


def test_the_check_imports_nothing_it_checks():
    source = (SCRIPTS / "ci_observe_audit.py").read_text()
    for module in ("workflow_exporter", "ci_observe_prs", "ci_observe_insights", "ci_observe_phases",
                   "ci_observe_trends", "ci_observe_runs"):
        assert f"import {module}" not in source and f"from {module}" not in source, module
