"""Pure helpers of workflow-exporter.py: classification, parsing, formatting."""

from __future__ import annotations

import pytest

from helpers import make_gh_job, make_step


@pytest.fixture
def W(wfe):
    return wfe.WorkflowExporter


@pytest.mark.parametrize(
    ("name", "category"),
    [
        ("Bump chart version", "automation"),
        ("E2E on CodeRabbit approval", "automation"),
        ("Scan E2E logs", "automation"),
        ("AI diagnostic (E2E fork PR failures)", "ai"),
        ("E2E VMaaS Full Install", "e2e"),
        ("pre-commit", "lint"),
        ("Publish Helm chart", "release"),
        ("Build container image", "release"),
        ("Unit tests", "ci"),
        ("Something unknown", "ci"),
    ],
)
def test_categorize_workflow(W, name, category):
    assert W._categorize_workflow(name) == category


def test_gate_names(W):
    assert W._is_gate_name("label-gate")
    assert W._is_gate_name("LABEL-GATE")
    assert not W._is_gate_name("e2e")
    assert not W._is_gate_name(None)
    assert W._is_gate_job("e2e-caas-gate")
    assert W._is_gate_job("changes")
    assert W._is_gate_job("E2E-Readiness")
    assert not W._is_gate_job("e2e-caas-full-install")
    assert not W._is_gate_job(None)


def test_gate_only_failure(W):
    assert not W._is_gate_only_failure(None)
    assert not W._is_gate_only_failure([{"name": "e2e", "conclusion": "success"}])
    assert W._is_gate_only_failure([
        {"name": "changes", "conclusion": "success"},
        {"name": "e2e-readiness", "conclusion": "failure"},
        {"name": "e2e-vmaas-gate", "conclusion": "failure"},
        {"name": "e2e-vmaas-full-install", "conclusion": "skipped"},
    ])
    assert not W._is_gate_only_failure([
        {"name": "e2e-vmaas-gate", "conclusion": "failure"},
        {"name": "e2e-vmaas-full-install", "conclusion": "failure"},
    ])


def test_classify_failure_reason_e2e(W):
    assert W._classify_failure_reason("lint", [{"step": "x"}]) == "n/a"
    assert W._classify_failure_reason("e2e", []) == "infra"
    assert W._classify_failure_reason("e2e", [{"step": "Checkout"}]) == "infra"
    assert W._classify_failure_reason("e2e", [{"step": "Checkout"}, {"step": "Run E2E tests"}]) == "test"
    gate_jobs = [{"name": "e2e-readiness", "conclusion": "failure"}]
    assert W._classify_failure_reason("e2e", [], gate_jobs) == "gate"


def test_classify_failure_reason_ai(W):
    assert W._classify_failure_reason("ai", None, None) == "n/a"
    assert W._classify_failure_reason("ai", None, []) == "n/a"
    ran = [{"steps": [{"name": W.AI_DIAGNOSIS_STEP, "conclusion": "success"}]}]
    skipped = [{"steps": [{"name": W.AI_DIAGNOSIS_STEP, "conclusion": "skipped"}]}, {"steps": None}]
    assert W._classify_failure_reason("ai", None, ran) == "ran"
    assert W._classify_failure_reason("ai", None, skipped) == "skipped"


def test_apply_ai_status(W):
    record = {"category": "ai"}
    W._apply_ai_status(record, [])
    assert record["failure_reason"] == "n/a"
    other = {"category": "e2e"}
    W._apply_ai_status(other, [])
    assert "failure_reason" not in other


def test_extract_merge_queue_pr(W):
    assert W._extract_merge_queue_pr("gh-readonly-queue/main/pr-42-abc123") == 42
    assert W._extract_merge_queue_pr("gh-readonly-queue/release/4.20/pr-7-deadbeef") == 7
    assert W._extract_merge_queue_pr("feature") is None
    assert W._extract_merge_queue_pr(None) is None


@pytest.mark.parametrize(
    ("run", "trigger"),
    [
        ({"event": "pull_request", "pull_requests": [{"number": 9}]}, "PR #9"),
        ({"event": "pull_request", "pull_requests": [], "head_branch": "fix"}, "PR (fix)"),
        ({"event": "merge_group", "head_branch": "gh-readonly-queue/main/pr-3-ab"}, "merge queue (PR #3)"),
        ({"event": "merge_group", "head_branch": "odd"}, "merge queue"),
        ({"event": "push", "head_branch": "main"}, "push (main)"),
        ({"event": "schedule"}, "scheduled"),
        ({"event": "workflow_dispatch"}, "manual"),
        ({"event": "issue_comment"}, "issue_comment"),
        ({}, "unknown"),
    ],
)
def test_extract_trigger(W, run, trigger):
    assert W._extract_trigger(run) == trigger


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "-"), (-5, "-"), (42, "42s"), (125, "2m 5s"), (3725, "1h 2m 5s")],
)
def test_fmt_duration(W, seconds, text):
    assert W._fmt_duration(seconds) == text


def test_seconds_between(W):
    assert W._seconds_between("2026-01-01T00:00:00Z", "2026-01-01T00:01:30Z") == 90
    assert W._seconds_between("2026-01-01T00:01:00Z", "2026-01-01T00:00:00Z") == 0
    assert W._seconds_between(None, "2026-01-01T00:00:00Z") is None
    assert W._seconds_between("2026-01-01T00:00:00Z", "") is None
    assert W._seconds_between("garbage", "2026-01-01T00:00:00Z") is None


def test_percentile(W):
    assert W._percentile([], 50) is None
    assert W._percentile([10], 99) == 10
    assert W._percentile([10, 20, 30, 40], 50) == 25
    assert W._percentile([10, 20, 30, 40], 100) == 40


@pytest.mark.parametrize(
    ("count", "bucket"),
    [(1, "1"), (5, "5"), (6, "6-10"), (10, "6-10"), (11, "11-20"), (21, "21-50"), (51, "51+")],
)
def test_jobs_per_pr_bucket(W, count, bucket):
    assert W._jobs_per_pr_bucket(count) == bucket


def test_parse_limit(W, wfe):
    assert W._parse_limit({}) == 200
    assert W._parse_limit({"limit": ["50"]}) == 50
    assert W._parse_limit({"limit": ["nope"]}) == 200
    assert W._parse_limit({"limit": ["-3"]}) == 1
    assert W._parse_limit({"limit": [str(wfe.JOBS_HISTORY_MAX_COUNT + 1)]}) == wfe.JOBS_HISTORY_MAX_COUNT


def test_normalize_iso(W):
    assert W._normalize_iso("2026-10-10T12:00:00.123+02:00") == "2026-10-10T10:00:00Z"
    assert W._normalize_iso("2026-10-10T10:00:00Z") == "2026-10-10T10:00:00Z"


def test_parse_grafana_param(exporter):
    p = exporter._parse_grafana_param
    assert p({}, "repo") is None
    assert p({"repo": [""]}, "repo") is None
    assert p({"repo": ["All"]}, "repo") is None
    assert p({"repo": ["${repo}"]}, "repo") is None
    assert p({"repo": [" : "]}, "repo") is None
    assert p({"repo": ["osac:"]}, "repo") == "osac"


def test_extract_runner_names(W):
    jobs = [
        {"runner_name": "b-runner-01"},
        {"runner_name": "a-runner-01"},
        {"runner_name": "a-runner-01"},
        {"runner_name": "GitHub Actions 12"},
        {"runner_name": None},
        {},
    ]
    assert W._extract_runner_names(jobs) == "a-runner-01, b-runner-01"


def test_extract_failed_steps(exporter):
    jobs = [
        make_gh_job("e2e-gate", "failure", [make_step("relay", "failure")]),
        make_gh_job("lint", "success", [make_step("ruff", "success")]),
        make_gh_job("e2e", "failure", [make_step("Checkout"), make_step("Run E2E tests", "failure")]),
    ]
    assert exporter._extract_failed_steps(jobs) == [
        {"display": "e2e → Run E2E tests", "step": "Run E2E tests"},
    ]


def test_extract_step_durations(exporter):
    jobs = [
        make_gh_job("e2e-gate", "success", [make_step("relay")]),
        make_gh_job("skipped-job", "skipped", [make_step("x")]),
        make_gh_job("e2e", "failure", [
            make_step("Checkout", start="2026-10-10T10:00:00Z", end="2026-10-10T10:00:30Z"),
            {"name": "Running", "status": "in_progress"},
            {"name": "No times", "status": "completed", "started_at": "", "completed_at": ""},
            {"name": "Bad times", "status": "completed", "started_at": "x", "completed_at": "y"},
            make_step("Backwards", start="2026-10-10T10:05:00Z", end="2026-10-10T10:00:00Z"),
        ]),
    ]
    assert exporter._extract_step_durations(jobs) == [
        {"name": "Checkout", "duration_s": 30},
        {"name": "Backwards", "duration_s": 0},
    ]
