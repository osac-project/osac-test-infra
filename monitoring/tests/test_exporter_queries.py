"""The JSON query endpoints of workflow-exporter.py and their HTTP routes."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from helpers import make_job


def ids(jobs):
    return sorted(j["id"] for j in jobs)


@pytest.fixture
def history(exporter):
    """A small job history covering the filterable fields."""
    jobs = [
        make_job(id=1, workflow="E2E VMaaS", display_name="osac / E2E VMaaS", conclusion="success",
                 event="schedule", created_at="2026-10-10T01:00:00Z", runner_name="osac-42-runner-01",
                 steps=[{"name": "Checkout", "duration_s": 10}, {"name": "Run E2E tests", "duration_s": 600}],
                 duration_s=900),
        make_job(id=2, workflow="E2E VMaaS", display_name="osac / E2E VMaaS", conclusion="failure",
                 event="pull_request", pr_display="#7", created_at="2026-10-10T02:00:00Z",
                 failed_step="e2e → Install OSAC", failure_reason="test",
                 runner_name="osac-421-runner-01, other-runner-02", duration_s=1200,
                 steps=[{"name": "Checkout", "duration_s": 20}, {"name": "Tiny", "duration_s": 1},
                        {"name": "", "duration_s": 5}]),
        make_job(id=3, workflow="E2E VMaaS", display_name="osac / E2E VMaaS", conclusion="success",
                 event="pull_request", pr_display="#7", created_at="2026-10-10T05:00:00Z", run_attempt=2),
        make_job(id=4, workflow="E2E CaaS", display_name="osac-test-infra / E2E CaaS", repo="osac-test-infra",
                 conclusion="failure", event="pull_request", pr_display="#8", created_at="2026-10-11T00:00:00Z",
                 failed_step="e2e → Setup cluster; e2e → Teardown", failure_reason="infra"),
        make_job(id=5, workflow="E2E CaaS", display_name="osac-test-infra / E2E CaaS", repo="osac-test-infra",
                 conclusion="cancelled", event="workflow_dispatch", created_at="2026-10-11T01:00:00Z",
                 duration_s=0),
        make_job(id=6, workflow="pre-commit", display_name="osac / pre-commit", category="lint",
                 conclusion="failure", event="merge_group",
                 created_at="2026-10-11T02:00:00Z", failed_step="lint → ruff", failure_reason="n/a"),
        make_job(id=7, workflow="label-gate", category="ci", conclusion="failure",
                 created_at="2026-10-11T03:00:00Z"),
        make_job(id=8, workflow="E2E VMaaS", conclusion="failure", failure_reason="gate",
                 created_at="2026-10-11T04:00:00Z"),
        make_job(id=9, workflow="AI diagnostic", display_name="osac / AI diagnostic", category="ai",
                 conclusion="success", failure_reason="ran", event="workflow_run",
                 actor="bot-user", created_at="2026-10-11T05:00:00Z"),
    ]
    for job in jobs:
        exporter._upsert_job(job)
    exporter.active_runs = [
        {"id": 100, "repo": "osac", "workflow": "E2E VMaaS", "category": "e2e", "status": "queued",
         "conclusion": "", "event": "pull_request", "created_at": "2026-10-12T00:00:00Z",
         "runner_name": "", "zombie": True},
        {"id": 101, "repo": "osac", "workflow": "E2E VMaaS", "category": "e2e", "status": "in_progress",
         "conclusion": "", "event": "schedule", "created_at": "2026-10-12T01:00:00Z",
         "runner_name": "osac-42-runner-02", "actor": "dev"},
        {"id": 102, "repo": "osac", "workflow": "label-gate", "category": "ci", "status": "queued",
         "conclusion": "", "event": "pull_request", "created_at": "2026-10-12T00:00:00Z"},
        {"id": 103, "repo": "osac", "workflow": "pre-commit", "category": "lint", "status": "queued",
         "conclusion": "", "event": "pull_request", "created_at": "bad-date"},
    ]
    return exporter


def q(**params):
    return {k: [v] for k, v in params.items()}


class TestGetJobs:
    def test_excludes_gates(self, history):
        assert ids(history.get_jobs_json({})) == [1, 2, 3, 4, 5, 6, 9]

    @pytest.mark.parametrize(
        ("params", "expected"),
        [
            ({"status": "failure"}, [2, 4, 6]),
            ({"failure_reason": "infra", "status": "success"}, [4]),
            ({"failure_reason": "ran"}, [9]),
            ({"repo": "osac-test-infra"}, [4, 5]),
            ({"category": "LINT"}, [6]),
            ({"runner": "osac-42"}, [1]),
            ({"runner": "other"}, [2]),
            ({"workflow": "caas, pre-commit"}, [4, 5, 6]),
            ({"workflow_name": "vmaas"}, [1, 2, 3]),
            ({"job_type": "presubmit"}, [2, 3, 4]),
            ({"job_type": "merge_queue"}, [6]),
            ({"since": "2026-10-10T02:00:00.000Z", "until": "2026-10-11T00:00:00+00:00"}, [2, 3]),
            ({"since": "garbage", "until": "garbage"}, [1, 2, 3, 4, 5, 6, 9]),
            ({"search": "BOT-user"}, [9]),
            ({"search": "#8"}, [4]),
            ({"limit": "2"}, [6, 9]),
        ],
    )
    def test_filters(self, history, params, expected):
        assert ids(history.get_jobs_json(q(**params))) == expected

    def test_steps_are_decoded(self, history):
        job = next(j for j in history.get_jobs_json({}) if j["id"] == 1)
        assert job["steps"][1] == {"name": "Run E2E tests", "duration_s": 600}
        assert "steps_json" not in job

    @pytest.mark.parametrize(
        ("params", "expected_active"),
        [
            ({}, [100, 101, 103]),
            ({"failure_reason": "infra"}, []),
            ({"status": "failure"}, []),
            ({"repo": "other"}, []),
            ({"category": "lint"}, [103]),
            ({"workflow": "vmaas"}, [100, 101]),
            ({"workflow_name": "pre"}, [103]),
            ({"runner": "osac-42"}, [101]),
            ({"job_type": "periodic"}, [101]),
            ({"search": "dev"}, [101]),
            ({"since": "2026-10-12T00:30:00Z"}, [101, 103]),
            ({"until": "2026-10-12T00:30:00Z"}, [100, 103]),
        ],
    )
    def test_active_runs(self, history, params, expected_active):
        jobs = history.get_jobs_json(q(active="true", **params))
        assert sorted(j["id"] for j in jobs if j["id"] >= 100) == expected_active


def test_counts_json(history):
    counts = history.get_counts_json(q(active="true", limit="1000"))
    assert counts["success"] == 3
    assert counts["failure"] == 3
    assert counts["cancelled"] == 1
    assert counts["zombie"] == 1
    assert counts["queued"] == 1
    assert counts["in_progress"] == 1
    assert counts["failure_rate"] == 0.5
    assert counts["success_rate"] == 0.5
    assert counts["cache_oldest_at"] == "2026-10-10T01:00:00Z"
    empty = history.get_counts_json(q(repo="none"))
    assert (empty["failure_rate"], empty["success_rate"], empty["total"]) == (0, 0, 0)


def test_counts_by_workflow(history):
    rows = history.get_counts_by_workflow_json({})
    by = {r["workflow"]: r for r in rows}
    assert by["osac / E2E VMaaS"]["success_rate"] == round(2 / 3, 4)
    assert by["osac-test-infra / E2E CaaS"]["cancelled"] == 1
    merged = history.get_counts_by_workflow_json(q(merge_similar="Yes"))
    assert {r["workflow"] for r in merged} >= {"E2E VMaaS", "E2E CaaS"}
    assert next(r for r in merged if r["workflow"] == "AI diagnostic")["success_rate"] == 1


def test_jobs_per_pr(history):
    data = history.get_jobs_per_pr_json(q(active="true"))
    assert data["total_jobs"] == 3
    assert data["distinct_prs"] == 2
    assert data["median_jobs_per_pr"] == 1.5
    assert data["max_jobs_per_pr"] == 2
    assert data["top_prs"][0] == {"repo": "osac", "pr": "#7", "jobs": 2}
    bucket2 = next(b for b in data["histogram"] if b["bucket"] == "2")
    assert bucket2 == {"bucket": "2", "prs": 1, "success": 1, "cancelled": 0, "failure": 1}
    assert data["outcomes"][2] == {"outcome": "Failure", "count": 2}
    single = history.get_jobs_per_pr_json(q(repo="osac-test-infra"))
    assert single["median_jobs_per_pr"] == 1
    empty = history.get_jobs_per_pr_json(q(repo="none"))
    assert empty["distinct_prs"] == 0
    assert len(empty["histogram"]) == len(history.JOBS_PER_PR_BUCKETS)


def test_flake_rate(history):
    rows = history.get_flake_rate_json(q(merge_similar="true"))
    assert rows[0] == {"workflow": "E2E VMaaS", "flaky_passes": 1, "total_successes": 2, "flake_rate": 0.5}
    assert history.get_flake_rate_json(q(repo="none")) == []


def test_mttr(exporter):
    for i, (concl, ts) in enumerate([
        ("failure", "2026-10-10T00:00:00Z"),
        ("failure", "2026-10-10T00:30:00Z"),
        ("cancelled", "2026-10-10T00:45:00Z"),
        ("success", "2026-10-10T01:00:00Z"),
        ("success", "2026-10-10T02:00:00Z"),
        ("failure", "2026-10-10T03:00:00Z"),
        ("success", "2026-10-10T06:00:00Z"),
    ]):
        exporter._upsert_job(make_job(id=i + 1, conclusion=concl, created_at=ts))
    exporter._upsert_job(make_job(id=50, workflow="Other", display_name="osac / Other", conclusion="success"))
    data = exporter.get_mttr_json({})
    assert data["by_workflow"] == [{"workflow": "osac / E2E VMaaS", "mttr_seconds": 7200,
                                    "mttr_display": "2h 0m 0s", "num_recoveries": 2}]
    assert data["overall"]["num_recoveries"] == 2
    assert exporter.get_mttr_json(q(repo="none")) == {"by_workflow": [], "overall": None}


def test_workflows(history):
    names = [w["workflow"] for w in history.get_workflows_json({})]
    assert names == ["AI diagnostic", "E2E CaaS", "E2E VMaaS", "pre-commit"]


def test_failed_steps(history, exporter):
    for i in range(10):
        exporter._upsert_job(make_job(id=200 + i, conclusion="failure", failed_step="e2e → Install OSAC"))
    steps = history.get_failed_steps_json({})
    assert steps[0] == {"step": "e2e → Install OSAC", "count": 11}
    assert steps[-1] == {"step": "Other", "count": 3}


def test_avg_duration_by_type(history):
    rows = history.get_avg_duration_by_type_json({})
    # The manual run (job 5) has no duration and is left out.
    assert [(r["job_type"], r["avg_duration_s"], r["count"]) for r in rows] == [
        ("Merge Queue", 600, 1), ("Periodic", 900, 1), ("Presubmit", 800, 3), ("workflow_run", 600, 1),
    ]
    assert rows[2]["avg_duration"] == "13m 20s"


def test_avg_step_duration(history):
    rows = history.get_avg_step_duration_json({})
    assert rows == [
        {"step": "Checkout", "avg_duration_s": 15, "avg_duration": "15s", "count": 2},
        {"step": "Run E2E tests", "avg_duration_s": 600, "avg_duration": "10m 0s", "count": 1},
    ]


def test_presubmit_infra_failures(history):
    data = history.get_presubmit_infra_failures_json({})
    assert data == {
        "infra_by_step": [{"step": "Setup cluster", "count": 1}, {"step": "Teardown", "count": 1}],
        "infra_total": 1,
        "test_total": 1,
        "unattributed_total": 1,
        "total_failures": 3,
    }


@pytest.fixture
def merges(exporter):
    rows = [
        # id, repo, number, merge_s, approval_s, retests, queue_wait, approval_to_queue, via_queue, merged_at
        (1, "osac", 1, 3600, 600, 1, 300, 200, 1, "2026-10-10T01:00:00Z"),
        (2, "osac", 2, 7200, 1200, 0, 900, None, 1, "2026-10-10T02:00:00Z"),
        (3, "osac", 3, 100, 900000, None, None, None, 0, "2026-10-10T03:00:00Z"),
        (4, "osac-test-infra", 4, 50, None, 2, 60, 30, 1, "2026-10-11T00:00:00Z"),
    ]
    with exporter._db() as conn:
        for r in rows:
            conn.execute(
                "INSERT INTO pr_merges (id, repo, number, title, author, merge_seconds, "
                "approval_to_merge_seconds, retest_count, queue_wait_seconds, approval_to_queue_seconds, "
                "via_merge_queue, merged_at) VALUES (?, ?, ?, 't', 'dev', ?, ?, ?, ?, ?, ?, ?)", r)
    return exporter


def test_pr_merge_time(merges):
    data = merges.get_pr_merge_time_json({})
    assert data["count"] == 4
    assert data["approved_count"] == 3
    assert data["median_approval_to_merge_seconds"] == 1200
    assert data["via_merge_queue_count"] == 3
    assert data["avg_retest_count"] == 0.8
    assert [o["number"] for o in data["approval_outliers"]] == [3]
    infra = next(r for r in data["by_repo"] if r["repo"] == "osac-test-infra")
    assert infra["avg_approval_to_merge_display"] == "n/a"
    assert infra["median_approval_to_queue_seconds"] == 30

    window = merges.get_pr_merge_time_json(q(repo="osac", since="2026-10-10T01:30:00Z",
                                             until="2026-10-10T02:30:00Z"))
    assert window["count"] == 1
    bad = merges.get_pr_merge_time_json(q(since="x", until="y"))
    assert bad["count"] == 4
    none = merges.get_pr_merge_time_json(q(repo="none"))
    assert (none["count"], none["avg_open_to_merge_seconds"], none["avg_retest_count"]) == (0, 0, 0)
    assert none["approval_outliers"] == []


def test_merge_queue_metrics(merges):
    data = merges.get_merge_queue_metrics_json({})
    assert data["total_queued_prs"] == 3
    assert data["p50_queue_wait_seconds"] == 300
    assert data["p99_approval_to_queue_seconds"] == 198
    assert [p["number"] for p in data["slowest_prs"]] == [2, 1, 4]
    filtered = merges.get_merge_queue_metrics_json(q(repo="osac-test-infra", since="2026-10-10T12:00:00Z",
                                                     until="2026-10-12T00:00:00Z"))
    assert filtered["total_queued_prs"] == 1
    bad = merges.get_merge_queue_metrics_json(q(since="x", until="y"))
    assert bad["total_queued_prs"] == 3
    none = merges.get_merge_queue_metrics_json(q(repo="none"))
    assert none["p50_queue_wait_display"] == "n/a"
    assert none["slowest_prs"] == []


class TestHTTP:
    @pytest.fixture
    def base_url(self, wfe, history, monkeypatch):
        monkeypatch.setattr(wfe.ExporterHandler, "exporter", history)
        server = ThreadingHTTPServer(("127.0.0.1", 0), wfe.ExporterHandler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_address[1]}"
        server.shutdown()
        server.server_close()

    def get(self, url):
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.headers.get("Content-Type"), resp.read()

    def test_metrics(self, base_url):
        status, ctype, body = self.get(f"{base_url}/metrics")
        assert status == 200
        assert ctype.startswith("text/plain")
        assert b"github_actions_queued_runs" in body

    @pytest.mark.parametrize(
        ("path", "check"),
        [
            ("/api/jobs?repo=osac-test-infra", lambda d: sorted(j["id"] for j in d) == [4, 5]),
            ("/api/counts", lambda d: d["in_progress"] == 1),
            ("/api/presubmit-infra-failures", lambda d: d["total_failures"] == 3),
            ("/api/pr-merge-time", lambda d: d["count"] == 0),
            ("/api/merge-queue-metrics", lambda d: d["total_queued_prs"] == 0),
            ("/api/workflows", lambda d: len(d) == 4),
            ("/api/failed-steps", lambda d: d[0]["count"] >= 1),
            ("/api/counts-by-workflow", lambda d: len(d) == 4),
            ("/api/jobs-per-pr", lambda d: d["distinct_prs"] == 2),
            ("/api/flake-rate", lambda d: d[0]["flaky_passes"] == 1),
            ("/api/mttr", lambda d: "overall" in d),
            ("/api/avg-duration-by-type", lambda d: len(d) >= 1),
            ("/api/avg-step-duration", lambda d: d[0]["step"] == "Checkout"),
            ("/api/active", lambda d: len(d) == 4),
            ("/api/categories", lambda d: "e2e" in d),
        ],
    )
    def test_json_endpoints(self, base_url, path, check):
        status, ctype, body = self.get(f"{base_url}{path}")
        assert (status, ctype) == (200, "application/json")
        assert check(json.loads(body))

    def test_unknown_path(self, base_url):
        with pytest.raises(urllib.error.HTTPError) as err:
            self.get(f"{base_url}/nope")
        assert err.value.code == 404
