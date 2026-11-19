"""The check against GitHub (ci_observe_audit.py), run against stores the real
exporter and the real backfill filled from the test fixtures: every check
agrees with a correct store, and a planted error is caught.

The audit must not import the code it checks; these tests may, to fill the
stores the way production does."""

import importlib.util
import io
import json
import sqlite3
import sys
import urllib.error
import zipfile
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


def junit_zip(failed=(), passed=()):
    """A logs artifact as the full-install jobs upload it: junit.xml and logs."""
    cases = "".join(f'<testcase classname="tests.e2e" name="{name}" time="1.0">'
                    f'<failure message="boom">trace</failure></testcase>' for name in failed)
    cases += "".join(f'<testcase classname="tests.e2e" name="{name}" time="1.0"/>' for name in passed)
    xml = f'<testsuites><testsuite name="pytest" tests="{len(failed) + len(passed)}">{cases}</testsuite></testsuites>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("junit.xml", xml)
        archive.writestr("e2e/failures/gw0/01-test/log.txt", "a log")
    return buffer.getvalue()


def e2e_artifacts():
    """The world's artifacts, where their jobs put them: the scheduled runs
    (501, 504) publish to osac-ci-artifacts, the merge-queue run (503) keeps
    one per suite on the run. CaaS failed in the merge queue; 502 failed at
    the install and has none."""
    return {
        501: ("private", [("osac-logs-full-install-501", junit_zip(passed=("test_vm_create", "test_vm_delete")))]),
        503: ("run", [("osac-logs-full-install-503", junit_zip(passed=("test_vm_create",))),
                      ("osac-logs-caas-full-install-503", junit_zip(failed=("test_cluster_create",),
                                                                    passed=("test_cluster_delete",)))]),
        504: ("private", [("osac-logs-full-install-504", junit_zip(passed=("test_vm_create",)))]),
    }


SUITE_NAMES = ("osac-logs-full-install-{run_id}", "osac-logs-caas-full-install-{run_id}",
               "osac-logs-bmaas-full-install-{run_id}")


def artifact_entries(api_url, run_id, artifacts, place="run"):
    repo = "osac-ci-artifacts" if place == "private" else "osac"
    return [{"id": run_id * 10 + index, "name": name, "size_in_bytes": len(content), "expired": False,
             "created_at": "2026-10-12T00:00:00Z", "workflow_run": {"id": 9000 + run_id},
             "archive_download_url": f"{api_url}/repos/osac-project/{repo}/actions/artifacts/{run_id * 10 + index}/zip"}
            for index, (name, content) in enumerate(artifacts)]


def e2e_github(world, artifacts=None, private_status=200):
    runs = {run["id"]: (run, jobs) for run, jobs in world}
    artifacts = artifacts or {}
    workflows = {}
    for run, _ in world:
        workflows[run["workflow_id"]] = {"id": run["workflow_id"], "name": run["name"], "path": run["path"]}
    downloads, on_runs, private = {}, {}, {}
    for run_id, (place, published) in artifacts.items():
        entries = artifact_entries(audit.API, run_id, published, place)
        for entry, (_, content) in zip(entries, published, strict=True):
            downloads[entry["archive_download_url"]] = content
            if place == "private":
                private[entry["name"]] = entry
            else:
                on_runs.setdefault(run_id, []).append(entry)

    def fetch(url, body=None):
        parsed = urlparse(url)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        parts = parsed.path.split("/")[1:]  # repos, org, repo, actions, ...
        repo, rest = parts[2], parts[4:]
        if repo == "osac-ci-artifacts":
            if private_status != 200:
                raise urllib.error.HTTPError(url, private_status, "Not Found", {}, None)
            found = [private[query["name"]]] if query.get("name") in private else []
            return {"total_count": len(found), "artifacts": found}, ""
        if repo != "osac":
            return ({"workflows": []} if rest == ["workflows"] else {}), ""
        if rest[0] == "runs" and rest[2:] == ["artifacts"]:
            found = on_runs.get(int(rest[1]), [])
            return {"total_count": len(found), "artifacts": found}, ""
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

    def download(url, limit):
        return io.BytesIO(downloads[url])

    return audit.GitHub("t", fetch=fetch, download=download)


def serve_artifacts(fake, api_url, artifacts, world, private_visible=True):
    """The same artifacts on the GitHub the exporter and the test history
    read: a run's own listing for every run (empty when its artifacts are
    elsewhere), and the private repository's listing by name, which answers
    an empty listing for a name it does not hold, or 404 to a token that
    cannot see it."""
    from helpers import FakeResponse

    for run, _ in world:
        place, published = artifacts.get(run["id"], ("run", []))
        entries = artifact_entries(api_url, run["id"], published, place)
        on_run = entries if place == "run" else []
        fake.add(f"/repos/osac-project/osac/actions/runs/{run['id']}/artifacts?per_page=100",
                 FakeResponse(payload={"total_count": len(on_run), "artifacts": on_run}))
        if private_visible:
            for pattern in SUITE_NAMES:
                name = pattern.format(run_id=run["id"])
                held = [entry for entry in entries if entry["name"] == name and place == "private"]
                fake.add(f"/repos/osac-project/osac-ci-artifacts/actions/artifacts?name={name}&per_page=100",
                         FakeResponse(payload={"total_count": len(held), "artifacts": held}))
        else:
            for pattern in SUITE_NAMES:
                fake.add(f"/repos/osac-project/osac-ci-artifacts/actions/artifacts"
                         f"?name={pattern.format(run_id=run['id'])}&per_page=100",
                         FakeResponse(404, text="Not Found"))
        for entry, (_, content) in zip(entries, published, strict=True):
            fake.add(entry["archive_download_url"].removeprefix(api_url), FakeResponse(content=content))


def fill_test_history(trends, tmp_path, monkeypatch):
    """trends.db as the hourly pass fills it, at the tests' clock."""
    monkeypatch.setattr(trends, "TOKEN", "t")
    conn = trends.open_trends_db(str(tmp_path / "trends.db"))
    try:
        runs = trends.candidate_runs(trends.stored_rows(str(tmp_path / "workflow-exporter.db"), "2026-10-01"))
        trends._roll_up(conn, runs, False, NOW)
    finally:
        conn.close()


def register_captures(exporter, captures):
    with exporter._db() as conn:
        for capture_id, run_id in captures:
            conn.execute("INSERT INTO captures (capture_id, run_id, run_attempt, suite, first_seen_at, last_seen_at)"
                         " VALUES (?, ?, '1', ?, ?, ?)",
                         (capture_id, str(run_id), capture_id.split("-")[0], audit.iso(NOW), audit.iso(NOW)))


@pytest.fixture
def artifact_github(wfe, monkeypatch):
    """The GitHub the exporter and the test history read the world's
    artifacts from. (This module's `github` fixture is the pull requests'.)"""
    from helpers import FakeGitHub

    fake = FakeGitHub(wfe.API_URL)
    monkeypatch.setattr(wfe.requests, "get", fake.get)
    monkeypatch.setattr(wfe.requests, "post", fake.post)
    return fake


@pytest.fixture
def stored_runs(evidence, wfe, trends, artifact_github, tmp_path, monkeypatch):
    """The stores after the real code read the world, as production fills
    them: the exporter's job history and capture registry, and the test
    history from the runs' artifacts."""
    world = e2e_world()
    jobs_of = {run["id"]: jobs for run, jobs in world}
    monkeypatch.setattr(evidence, "_fetch_run_jobs", lambda repo, run_id, run_attempt=None: jobs_of[run_id])
    for run, _ in world:
        assert evidence._process_completed_run("osac", run)
    serve_artifacts(artifact_github, wfe.API_URL, e2e_artifacts(), world)
    fill_test_history(trends, tmp_path, monkeypatch)
    register_captures(evidence, [("vmaas-main-501-a1", 501), ("vmaas-main-503-a1", 503), ("caas-main-503-a1", 503)])
    insights = wfe.CIObserveInsights(str(tmp_path / "workflow-exporter.db"), str(tmp_path / "trends.db"),
                                     clock=lambda: NOW)
    STORED_EXPORTERS[str(tmp_path)] = evidence
    return world, tmp_path, insights


# The exporter behind a stored_runs world, by its directory: its run pages.
STORED_EXPORTERS = {}


def e2e_main(tmp_path, insights, world, *extra, artifacts=None, private_status=200):
    exporter = STORED_EXPORTERS.get(str(tmp_path))

    def api_get(url):
        """The exporter's API as the audit reads it over HTTP: a failed
        failed-tests read is a 502 whose status line is the reason."""
        parsed = urlparse(url)
        if parsed.path == "/api/ci-observe/failed-tests" and exporter is not None:
            evidence_module = sys.modules["ci_observe_run_evidence"]
            try:
                return exporter._run_evidence.get_ci_observe_failed_tests_json(parse_qs(parsed.query))
            except evidence_module.EvidenceError as error:
                raise urllib.error.HTTPError(url, 502, str(error), {}, None) from None
        assert parsed.path == "/api/ci-observe/health", url
        return insights.get_json("health", parse_qs(parsed.query))

    world_github = e2e_github(world, e2e_artifacts() if artifacts is None else artifacts, private_status)
    return audit.main(["--part", "e2e", "--days", "14", "--jobs-db", str(tmp_path / "workflow-exporter.db"),
                       "--trends-db", str(tmp_path / "trends.db"), *extra],
                      github=world_github, now=NOW, api_get=api_get)


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


# -- E2E tests: the run pages' Failed tests and the test history -----------------------------------------------------


def run_tests_audit(tmp_path, insights, world, capsys, **options):
    status = e2e_main(tmp_path, insights, world, "--json", **options)
    return status, json.loads(capsys.readouterr().out)["e2e"]["tests"]


def test_the_tests_audit_agrees_with_correct_stores(stored_runs, capsys):
    world, tmp_path, insights = stored_runs
    status, tests = run_tests_audit(tmp_path, insights, world, capsys)

    assert tests["private"]["readable"] is True
    coverage = tests["coverage"]
    # 501 (VMaaS) and the two suites of the merge-queue run 503 ran their
    # tests; 502 failed at the install, 504 is not a suite run.
    assert (coverage["runs"], coverage["with_results"], coverage["missing"]) == (3, 3, 0)
    [audited] = tests["failed_runs"]
    assert audited["capture_id"] == "caas-main-503-a1"
    assert audited["page"] == audited["junit"] == audited["history"] == ["test_cluster_create"]
    assert audited["junit_where"] == "run 503 of osac-project/osac", "a merge-queue run keeps it on the run"
    assert (audited["page_error"], audited["problems"]) == ("", [])
    assert status == 0


def test_the_tests_audit_catches_a_page_that_says_no_failures(stored_runs, monkeypatch, capsys):
    """The page as it was before 2026-11-15: an empty list for a run whose
    test step failed, which Grafana shows as "No failures."."""
    world, tmp_path, insights = stored_runs
    evidence_class = sys.modules["ci_observe_run_evidence"].CIObserveRunEvidence
    monkeypatch.setattr(evidence_class, "get_ci_observe_failed_tests_json", lambda self, params: {"failed_tests": []})
    out = tmp_path / "ci-observe-audit.json"

    assert e2e_main(tmp_path, insights, world, "--out", str(out), "--quiet") == 0
    result = json.loads(out.read_text())
    [audited] = result["e2e"]["tests"]["failed_runs"]
    assert audited["problems"][0].startswith("the page lists no failed test for a run whose test step failed")
    summary = result["summary"]
    assert summary["tests"]["pages_claim_none"] == 1
    assert summary["ok"] is False
    phases = _load("ci_observe_phases", "ci_observe_phases.py")
    _, notes = phases.audit_lines(summary, NOW, "e2e")
    assert "1 of 1 runs that failed in their tests show no failed test on their run page" in " ".join(notes)


def test_the_tests_audit_catches_a_wrong_test_history_and_missing_results(stored_runs, capsys):
    world, tmp_path, insights = stored_runs
    with sqlite3.connect(tmp_path / "trends.db") as conn:
        conn.execute("UPDATE test_runs SET passed = 1 WHERE run_id = 503")
        conn.execute("DELETE FROM test_runs WHERE run_id = 501")

    status, tests = run_tests_audit(tmp_path, insights, world, capsys)

    assert tests["coverage"]["missing"] == 1
    assert tests["coverage"]["examples"][0].startswith("https://github.com/osac-project/osac/actions/runs/501 (vmaas)")
    [audited] = tests["failed_runs"]
    assert audited["problems"] == ["the test history has [], the JUnit file ['test_cluster_create']"]
    assert status == 1


def incident_world():
    """e2e_world plus the incident: a scheduled production install (505)
    whose test step failed, and whose artifact went to osac-ci-artifacts."""
    world = e2e_world()
    run = gh_run(505, "E2E VMaaS Production Install", ".github/workflows/e2e-vmaas-prod-install-caller.yml",
                 "failure", "2026-10-12T03:00:00Z", 14)
    world.append((run, [gh_job(505, "e2e-vmaas-prod-install / e2e", "failure", "2026-10-12T03:00:00Z", 40,
                               failed_step="Run E2E tests (ci-observe capture)")]))
    return world


@pytest.fixture
def private_refused(evidence, wfe, trends, artifact_github, tmp_path, monkeypatch):
    """Production since 2026-11-13, when the token cannot see osac-ci-artifacts:
    the merge-queue run's artifacts are on the run, and every request to the
    private repository is a 404 (an unrouted path of the fake GitHub)."""
    world = incident_world()
    jobs_of = {run["id"]: jobs for run, jobs in world}
    monkeypatch.setattr(evidence, "_fetch_run_jobs", lambda repo, run_id, run_attempt=None: jobs_of[run_id])
    for run, _ in world:
        assert evidence._process_completed_run("osac", run)
    artifacts = {503: e2e_artifacts()[503]}
    serve_artifacts(artifact_github, wfe.API_URL, artifacts, world, private_visible=False)
    # The test history pass stops at the first private run, and stores nothing.
    with pytest.raises(trends.GitHubRefused, match="osac-project/osac-ci-artifacts answered HTTP 404"):
        fill_test_history(trends, tmp_path, monkeypatch)
    register_captures(evidence, [("vmaas-main-501-a1", 501), ("caas-main-503-a1", 503), ("vmaas-main-505-a1", 505)])
    insights = wfe.CIObserveInsights(str(tmp_path / "workflow-exporter.db"), str(tmp_path / "trends.db"),
                                     clock=lambda: NOW)
    STORED_EXPORTERS[str(tmp_path)] = evidence
    return world, tmp_path, insights, artifacts


def test_the_tests_audit_names_a_private_repository_the_token_cannot_read(private_refused):
    world, tmp_path, insights, artifacts = private_refused
    out = tmp_path / "ci-observe-audit.json"

    assert e2e_main(tmp_path, insights, world, "--out", str(out), "--quiet", artifacts=artifacts,
                    private_status=404) == 0

    result = json.loads(out.read_text())
    tests = result["e2e"]["tests"]
    assert tests["private"]["readable"] is False
    # No pass finished, so no run has test results.
    assert tests["coverage"]["missing"] == 3
    by_capture = {audit_row["capture_id"]: audit_row for audit_row in tests["failed_runs"]}
    assert set(by_capture) == {"caas-main-503-a1", "vmaas-main-505-a1"}
    # The private repository is looked in first for every run, so the merge-queue
    # run's page fails the same way: a token that cannot read it is the one
    # thing to fix, and every run says so.
    for capture_id in ("caas-main-503-a1", "vmaas-main-505-a1"):
        row = by_capture[capture_id]
        assert row["page_error"].startswith("HTTP 502: osac-project/osac-ci-artifacts answered HTTP 404"), capture_id
        assert "PRIVATE_GITHUB_TOKEN needs read access" in row["page_error"]
        assert row["junit"] is None and row["junit_where"] == "osac-project/osac-ci-artifacts"
        assert row["problems"] == [f"the page cannot list the failed tests ({row['page_error']})"]
    summary = result["summary"]["tests"]
    assert (summary["private_readable"], summary["runs_missing"], summary["pages_error"]) == (False, 3, 2)
    phases = _load("ci_observe_phases", "ci_observe_phases.py")
    source, notes = phases.audit_lines(result["summary"], NOW, "e2e")
    assert "test results stored for 0 of 3 suite runs that ran their tests" in source
    text = " ".join(notes)
    assert ("cannot read osac-project/osac-ci-artifacts, where E2E runs publish their "
            "JUnit files") in text
    assert "2 of 2 runs that failed in their tests cannot show their failed tests on their run page: HTTP 502" in text
    report = audit.report(result)
    assert ("osac-project/osac-ci-artifacts, where E2E runs publish their logs artifact: "
            "NOT READABLE") in report


def test_a_network_failure_in_the_tests_checks_is_a_finding_not_a_crash(stored_runs, monkeypatch, capsys):
    world, tmp_path, insights = stored_runs
    monkeypatch.setattr(audit.GitHub, "download", lambda self, url, limit: (_ for _ in ()).throw(
        urllib.error.URLError("connection reset")))

    status, tests = run_tests_audit(tmp_path, insights, world, capsys)

    [audited] = tests["failed_runs"]
    assert audited["junit"] is None
    assert "download failed" in audited["junit_note"]
    assert audited["problems"] == []
    assert status == 0


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


def test_a_rerun_of_failed_jobs_keeps_each_suite_on_its_own_attempt(exporter, wfe, trends, artifact_github, tmp_path,
                                                                    monkeypatch, capsys):
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
    # The re-run replaced the CaaS artifact; both suites' tests are stored.
    artifacts = {503: ("run", [("osac-logs-full-install-503", junit_zip(passed=("test_vm_create",))),
                               ("osac-logs-caas-full-install-503", junit_zip(passed=("test_cluster_create",)))])}
    serve_artifacts(artifact_github, wfe.API_URL, artifacts, world)
    fill_test_history(trends, tmp_path, monkeypatch)
    insights = wfe.CIObserveInsights(str(tmp_path / "workflow-exporter.db"), str(tmp_path / "trends.db"),
                                     clock=lambda: NOW)
    status = e2e_main(tmp_path, insights, world, "--json", artifacts=artifacts)
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
