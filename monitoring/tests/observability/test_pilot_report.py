"""Tests for the shared pilot report module and its query CLI.

The loader, validation, identity and timing rules are the ones the
accepted base dashboard used, so the human view and the agent CLI derive
identical facts. Fixtures are small local snapshots; the CLI is exercised
as a real subprocess and never performs network access (no gh invocation,
no HTTP). Real snapshot files are queried privately by the parent.

Run with (``-o addopts=`` clears pytest.ini's coverage flags for a
focused run):

    cd monitoring/tests && pytest -o addopts= observability/test_pilot_report.py
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from monitoring.ci_observability.pilot_report import SnapshotError, build_report, load_snapshot

REPO_ROOT = Path(__file__).resolve().parents[3]

# A compact snapshot shaped exactly like the collector's output: one job
# with known bounds, a failed step, a cancelled step, a running job with
# open bounds, and one artifact.
BASE_SNAPSHOT: dict[str, object] = {
    "schema": "osac.ci-observability.pilot",
    "version": 1,
    "fetched_at": "2026-10-28T04:30:00Z",
    "repository": "osac-project/osac",
    "run": {
        "id": 40548825479,
        "attempt": 1,
        "name": "e2e-vmaas-full-install",
        "event": "pull_request",
        "status": "completed",
        "conclusion": "failure",
        "head_sha": "572d8f877721120f5f9f533000fe5d27cb70eff3",
        "created_at": "2026-10-27T17:15:19Z",
        "updated_at": "2026-10-27T19:13:46Z",
        "url": "https://github.com/osac-project/osac/actions/runs/40548825479",
        "workflow_path": ".github/workflows/e2e-vmaas-full-install.yml",
    },
    "jobs": [
        {
            "id": 122346680034,
            "name": "e2e-vmaas-full-install / e2e",
            "status": "completed",
            "conclusion": "failure",
            "started_at": "2026-10-27T17:15:21Z",
            "completed_at": "2026-10-27T19:13:45Z",
            "url": "https://github.com/osac-project/osac/actions/runs/40548825479/job/122346680034",
            "steps": [
                {
                    "number": 1,
                    "name": "Set up job",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": "2026-10-27T17:15:22Z",
                    "completed_at": "2026-10-27T17:15:32Z",
                },
                {
                    "number": 18,
                    "name": "Run E2E tests",
                    "status": "completed",
                    "conclusion": "failure",
                    "started_at": "2026-10-27T17:20:00Z",
                    "completed_at": "2026-10-27T18:17:57Z",
                },
                {
                    "number": 19,
                    "name": "Tear down cluster",
                    "status": "completed",
                    "conclusion": "cancelled",
                    "started_at": "2026-10-27T18:18:00Z",
                    "completed_at": "2026-10-27T18:19:00Z",
                },
                {
                    "number": 20,
                    "name": "Post actions",
                    "status": "completed",
                    "conclusion": "skipped",
                    "started_at": None,
                    "completed_at": None,
                },
            ],
        },
        {
            "id": 122346680035,
            "name": "still-running-job",
            "status": "in_progress",
            "conclusion": None,
            "started_at": "2026-10-27T17:15:25Z",
            "completed_at": None,
            "url": "https://github.com/osac-project/osac/actions/runs/40548825479/job/122346680035",
            "steps": [],
        },
    ],
    "artifacts": [
        {
            "id": 12762398922,
            "name": "osac-logs-full-install-40548825479",
            "size_bytes": 4683673,
            "expired": False,
            "url": "https://github.com/osac-project/osac/actions/runs/40548825479/artifacts/12762398922",
        }
    ],
    "coverage": {
        "jobs_complete": False,
        "artifacts_complete": True,
        "test_results": "not-collected",
        "tested_revision": "unverified",
        "notes": ["run-head-sha-is-not-tested-sha", "artifacts-are-run-wide-not-attempt-bound"],
    },
}


def _snapshot_with_complete_jobs() -> dict[str, object]:
    """The base snapshot with both jobs finished and coverage complete."""
    snapshot = copy.deepcopy(BASE_SNAPSHOT)
    running = snapshot["jobs"][1]
    assert isinstance(running, dict)
    running["status"] = "completed"
    running["conclusion"] = "success"
    running["completed_at"] = "2026-10-27T19:00:00Z"
    coverage = snapshot["coverage"]
    assert isinstance(coverage, dict)
    coverage["jobs_complete"] = True
    return snapshot


@pytest.fixture
def snapshot_path(*, tmp_path: Path) -> Path:
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(BASE_SNAPSHOT), encoding="utf-8")
    return path


def _run_cli(*, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "monitoring.ci_observability.pilot_report", *arguments],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_load_snapshot_roundtrip_no_mutation(*, snapshot_path: Path) -> None:
    before = snapshot_path.read_bytes()
    snapshot = load_snapshot(path=snapshot_path)
    assert snapshot["schema"] == "osac.ci-observability.pilot"
    assert snapshot_path.read_bytes() == before


def test_build_report_source_identity_and_coverage(*, snapshot_path: Path) -> None:
    snapshot = load_snapshot(path=snapshot_path)
    report = build_report(snapshot=snapshot)
    assert report["schema"] == "osac.ci-observability.pilot-report"
    assert report["version"] == 1
    source = report["source"]
    assert source["kind"] == "snapshot"
    assert source["repository"] == "osac-project/osac"
    assert source["run_id"] == 40548825479
    assert source["run_attempt"] == 1
    assert source["github_url"] == "https://github.com/osac-project/osac/actions/runs/40548825479/attempts/1"
    assert source["head_sha"] == BASE_SNAPSHOT["run"]["head_sha"]
    assert source["tested_revision"] == "unverified"
    assert source["fetched_at"] == snapshot["fetched_at"]
    # Coverage is carried through unchanged.
    assert report["coverage"] == snapshot["coverage"]


def test_job_span_only_when_complete_and_bounded(*, snapshot_path: Path) -> None:
    snapshot = load_snapshot(path=snapshot_path)
    report = build_report(snapshot=snapshot)
    # One job is open and coverage says jobs_complete is false: no span.
    assert report["summary"]["job_span_seconds"] is None
    assert report["summary"]["job_span_minutes"] is None

    complete = _snapshot_with_complete_jobs()
    report2 = build_report(snapshot=complete)
    # Earliest start 17:15:21 to latest end 19:13:45 = 7104 seconds.
    assert report2["summary"]["job_span_seconds"] == 7104.0
    assert report2["summary"]["job_span_minutes"] == pytest.approx(118.4)


def test_job_span_is_not_the_sum_of_parallel_jobs(*, snapshot_path: Path) -> None:
    """Two overlapping jobs: the span is the wall-clock envelope."""
    snapshot = _snapshot_with_complete_jobs()
    jobs = snapshot["jobs"]
    assert isinstance(jobs, list)
    # Make the two jobs run in parallel windows inside the envelope:
    # 17:30 to 19:00 (5400 s) and 17:15 to 18:00 (2700 s). The sum of
    # the durations is 8100 seconds; the wall-clock envelope is earliest
    # 17:15 to latest 19:00 = 6300 seconds, which is what a span is.
    jobs[0]["started_at"] = "2026-10-27T17:30:00Z"
    jobs[0]["completed_at"] = "2026-10-27T19:00:00Z"
    jobs[1]["started_at"] = "2026-10-27T17:15:00Z"
    jobs[1]["completed_at"] = "2026-10-27T18:00:00Z"
    report = build_report(snapshot=snapshot)
    assert report["summary"]["job_span_seconds"] == 6300.0
    job_durations = [job["duration_seconds"] for job in report["jobs"]]
    assert sum(duration for duration in job_durations if duration is not None) == 8100.0


def test_partial_jobs_keep_nulls_and_measured_zero(*, snapshot_path: Path) -> None:
    snapshot = load_snapshot(path=snapshot_path)
    report = build_report(snapshot=snapshot)
    running = report["jobs"][1]
    assert running["duration_seconds"] is None
    assert running["duration_minutes"] is None
    assert running["completed_at"] is None
    # The completed job keeps its measured duration in both units.
    finished = report["jobs"][0]
    assert finished["duration_seconds"] == 7104.0
    assert finished["duration_minutes"] == pytest.approx(118.4)

    # A measured zero-duration step stays exactly zero, not null.
    zero_snapshot = copy.deepcopy(BASE_SNAPSHOT)
    steps = zero_snapshot["jobs"][0]["steps"]
    assert isinstance(steps, list)
    first = steps[0]
    assert isinstance(first, dict)
    first["completed_at"] = first["started_at"]
    report_zero = build_report(snapshot=zero_snapshot)
    zero_step = report_zero["steps"][0]
    assert zero_step["duration_seconds"] == 0.0


def test_failed_and_attention_lists_separate_kinds(*, snapshot_path: Path) -> None:
    snapshot = load_snapshot(path=snapshot_path)
    report = build_report(snapshot=snapshot)
    assert report["summary"]["failed_job_count"] == 1
    assert report["summary"]["failed_step_count"] == 1
    failures = report["failures"]
    assert [job["conclusion"] for job in failures["jobs"]] == ["failure"]
    conclusions = [step["conclusion"] for step in failures["steps"]]
    assert "failure" in conclusions
    assert "cancelled" in conclusions
    assert "skipped" not in conclusions
    # Attention preserves the kind; failed vs cancelled stay distinct.
    kinds = {step["conclusion"] for step in failures["steps"]}
    assert kinds == {"failure", "cancelled"}


def test_slowest_steps_top_eight_positive_measured(*, snapshot_path: Path) -> None:
    snapshot = _snapshot_with_complete_jobs()
    jobs = snapshot["jobs"]
    assert isinstance(jobs, list)
    steps = jobs[0]["steps"]
    assert isinstance(steps, list)
    # Grow to ten measured positive steps plus the existing ones.
    base = len(steps)
    for index in range(10):
        steps.append(
            {
                "number": base + index,
                "name": f"generated-{index}",
                "status": "completed",
                "conclusion": "success",
                "started_at": "2026-10-27T18:00:00Z",
                "completed_at": f"2026-10-27T18:{index + 1:02d}:00Z",
            }
        )
    report = build_report(snapshot=snapshot)
    slowest = report["slowest_steps"]
    assert len(slowest) == 8
    durations = [row["duration_seconds"] for row in slowest]
    assert durations == sorted(durations, reverse=True)
    assert all(isinstance(duration, (int, float)) and duration > 0 for duration in durations)
    # All records are retained in the base steps list; top 8 is a
    # presentation subset, not a dropped fact.
    assert len(report["steps"]) == base + 10
    # The denominator counts every positive measured step duration.
    expected_total = sum(
        1
        for row in report["steps"]
        if isinstance(row["duration_seconds"], (int, float)) and row["duration_seconds"] > 0
    )
    assert report["slowest_steps_total"] == expected_total
    # Skipped steps (unmeasured) never appear in the top list.
    assert all(row["conclusion"] != "skipped" for row in slowest)


def test_artifact_rows_typed(*, snapshot_path: Path) -> None:
    snapshot = load_snapshot(path=snapshot_path)
    report = build_report(snapshot=snapshot)
    artifact = report["artifacts"][0]
    assert artifact["id"] == 12762398922
    assert artifact["name"] == "osac-logs-full-install-40548825479"
    assert artifact["size_bytes"] == 4683673
    assert artifact["expired"] is False
    assert artifact["url"] == "https://github.com/osac-project/osac/actions/runs/40548825479/artifacts/12762398922"


def test_unknown_artifact_size_fails_constant(*, snapshot_path: Path) -> None:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["artifacts"][0]["size_bytes"] = "unknown"
    with pytest.raises(SnapshotError, match="artifact size_bytes must be a non-negative integer"):
        build_report(snapshot=snapshot)


def test_nonfinite_duration_fails_constant(*, snapshot_path: Path) -> None:
    """A NaN literal in the raw snapshot is rejected by the loader."""
    text = snapshot_path.read_text(encoding="utf-8")
    poisoned = text.replace('"version": 1,', '"version": 1, "poison": NaN,', 1)
    poisoned_path = snapshot_path.parent / "poisoned.json"
    poisoned_path.write_text(poisoned, encoding="utf-8")
    with pytest.raises(SnapshotError, match="snapshot is not valid JSON"):
        load_snapshot(path=poisoned_path)


def test_load_snapshot_rejects_symlink(*, tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text(json.dumps(BASE_SNAPSHOT), encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(OSError):
        load_snapshot(path=link)


def test_cli_sections_json_only(*, snapshot_path: Path) -> None:
    for section in ("summary", "jobs", "steps", "failures", "artifacts", "slowest_steps"):
        result = _run_cli(arguments=["--snapshot", str(snapshot_path), "--section", section])
        assert result.returncode == 0, result.stderr
        document = json.loads(result.stdout)
        assert document["schema"] == "osac.ci-observability.pilot-report"
        assert document["version"] == 1
        assert document["section"] == section
        assert document["source"]["run_id"] == 40548825479
        assert document["source"]["fetched_at"] == "2026-10-28T04:30:00Z"
        assert document["coverage"] == BASE_SNAPSHOT["coverage"]
        assert isinstance(document["data"], (list, dict))

    result = _run_cli(arguments=["--snapshot", str(snapshot_path), "--section", "all"])
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["summary"]["job_count"] == 2

    # Default section is summary.
    result = _run_cli(arguments=["--snapshot", str(snapshot_path)])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["section"] == "summary"


def test_cli_error_constant_stderr_no_path_leak(*, tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.json"
    result = _run_cli(arguments=["--snapshot", str(missing)])
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("ERROR:")
    assert str(tmp_path) not in result.stderr

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    result = _run_cli(arguments=["--snapshot", str(bad)])
    assert result.returncode == 1
    assert result.stderr.strip() == "ERROR: snapshot is not valid JSON"


def test_cli_data_matches_public_api(*, snapshot_path: Path) -> None:
    """The CLI's section data is the same object the public API builds."""
    report = build_report(snapshot=load_snapshot(path=snapshot_path))
    result = _run_cli(arguments=["--snapshot", str(snapshot_path), "--section", "jobs"])
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["data"] == report["jobs"]


def test_parallel_span_matches_report_module(*, tmp_path: Path) -> None:
    """The CLI and build_report agree on the parallel-jobs envelope."""
    path = tmp_path / "parallel.json"
    path.write_text(json.dumps(_snapshot_with_complete_jobs()), encoding="utf-8")
    result = _run_cli(arguments=["--snapshot", str(path), "--section", "summary"])
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)["data"]
    report = build_report(snapshot=load_snapshot(path=path))
    assert summary == report["summary"]
    assert summary["job_span_seconds"] == 7104.0


def test_report_never_imports_dashboard() -> None:
    """No circular import: the report module must not import the view."""
    module = sys.modules["monitoring.ci_observability.pilot_report"]
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "pilot_dashboard" not in source


def test_slowest_steps_section_carries_selection(*, tmp_path: Path) -> None:
    """The slowest_steps section states its subset and denominator."""
    snapshot = _snapshot_with_complete_jobs()
    jobs = snapshot["jobs"]
    assert isinstance(jobs, list)
    steps = jobs[0]["steps"]
    assert isinstance(steps, list)
    base = len(steps)
    for index in range(10):
        steps.append(
            {
                "number": base + index,
                "name": f"generated-{index}",
                "status": "completed",
                "conclusion": "success",
                "started_at": "2026-10-27T18:00:00Z",
                "completed_at": f"2026-10-27T18:{index + 1:02d}:00Z",
            }
        )
    snap_path = tmp_path / "many.json"
    snap_path.write_text(json.dumps(snapshot), encoding="utf-8")
    report = build_report(snapshot=snapshot)
    result = _run_cli(arguments=["--snapshot", str(snap_path), "--section", "slowest_steps"])
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    selection = document["selection"]
    assert selection["limit"] == 8
    assert selection["returned"] == 8
    assert selection["total_measured_positive"] == report["slowest_steps_total"]
    assert selection["total_measured_positive"] > 8
    # No records dropped: the all section still has every step.
    result_all = _run_cli(arguments=["--snapshot", str(snap_path), "--section", "all"])
    assert result_all.returncode == 0, result_all.stderr
    full = json.loads(result_all.stdout)
    assert len(full["steps"]) == base + 10


def test_minutes_are_unrounded_machine_precision(*, tmp_path: Path) -> None:
    """One second is 1/60 minute in machine JSON, not a rounded 0.02."""
    snapshot = _snapshot_with_complete_jobs()
    jobs = snapshot["jobs"]
    assert isinstance(jobs, list)
    steps = jobs[0]["steps"]
    assert isinstance(steps, list)
    first = steps[0]
    assert isinstance(first, dict)
    # A one-second measured step.
    first["started_at"] = "2026-10-27T17:15:22Z"
    first["completed_at"] = "2026-10-27T17:15:23Z"
    snap_path = tmp_path / "one-second.json"
    snap_path.write_text(json.dumps(snapshot), encoding="utf-8")

    report = build_report(snapshot=snapshot)
    step = report["steps"][0]
    assert step["duration_seconds"] == 1.0
    assert step["duration_minutes"] == pytest.approx(1 / 60)
    assert step["duration_minutes"] != 0.02

    # The public CLI agrees with the public API on the same value.
    result = _run_cli(arguments=["--snapshot", str(snap_path), "--section", "steps"])
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["data"][0]["duration_minutes"] == pytest.approx(1 / 60)


def test_bad_conclusion_type_fails_constant(*, snapshot_path: Path) -> None:
    """A non-string conclusion is malformed input, not a quiet unknown."""
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["jobs"][0]["conclusion"] = 7
    with pytest.raises(SnapshotError, match="snapshot conclusion must be a string or null"):
        build_report(snapshot=snapshot)

    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["jobs"][0]["steps"][0]["conclusion"] = ["failure"]
    with pytest.raises(SnapshotError, match="snapshot conclusion must be a string or null"):
        build_report(snapshot=snapshot)

    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["run"]["conclusion"] = True
    with pytest.raises(SnapshotError, match="snapshot conclusion must be a string or null"):
        build_report(snapshot=snapshot)


def test_fractional_seconds_preserved_unrounded(*, tmp_path: Path) -> None:
    """Fractional timestamps keep raw seconds and unrounded minutes."""
    snapshot = _snapshot_with_complete_jobs()
    steps = snapshot["jobs"][0]["steps"]
    steps[0]["started_at"] = "2026-10-27T17:15:22.000000Z"
    steps[0]["completed_at"] = "2026-10-27T17:15:22.123456Z"
    report = build_report(snapshot=snapshot)
    step = report["steps"][0]
    assert step["duration_seconds"] == pytest.approx(0.123456)
    assert step["duration_minutes"] == pytest.approx(0.123456 / 60)

    snap_path = tmp_path / "fractional.json"
    snap_path.write_text(json.dumps(snapshot), encoding="utf-8")
    result = _run_cli(arguments=["--snapshot", str(snap_path), "--section", "steps"])
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["data"][0]["duration_seconds"] == pytest.approx(0.123456)


def test_empty_coverage_returns_null_span_and_revision(*, snapshot_path: Path) -> None:
    """Coverage fields are optional: empty coverage means unknown, not KeyError."""
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["coverage"] = {}
    report = build_report(snapshot=snapshot)
    assert report["summary"]["job_span_seconds"] is None
    assert report["summary"]["job_span_minutes"] is None
    assert report["source"]["tested_revision"] is None
    assert report["coverage"] == {}


def test_mixed_precision_timestamps_parse_before_comparison() -> None:
    """ISO strings with different precision sort as datetimes, not strings."""
    snapshot = _snapshot_with_complete_jobs()
    jobs = snapshot["jobs"]
    assert isinstance(jobs, list)
    jobs[0]["started_at"] = "2026-10-28T02:00:00.500Z"
    jobs[0]["completed_at"] = "2026-10-28T02:00:01.000Z"
    jobs[1]["started_at"] = "2026-10-28T02:00:00Z"
    jobs[1]["completed_at"] = "2026-10-28T02:00:01Z"
    report = build_report(snapshot=snapshot)
    assert report["summary"]["job_span_seconds"] == 1.0


def test_finite_exponent_overflow_cli_constant_error(*, snapshot_path: Path) -> None:
    """1e999 parses to inf; the CLI fails with a constant error, empty stdout."""
    text = snapshot_path.read_text(encoding="utf-8")
    poisoned = text.replace(
        '"notes": ["run-head-sha-is-not-tested-sha", "artifacts-are-run-wide-not-attempt-bound"]', '"notes": [1e999]'
    )
    assert poisoned != text, "fixture notes not found for the overflow probe"
    poisoned_path = snapshot_path.parent / "overflow.json"
    poisoned_path.write_text(poisoned, encoding="utf-8")
    result = _run_cli(arguments=["--snapshot", str(poisoned_path), "--section", "summary"])
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.strip() == "ERROR: snapshot cannot be processed"
    assert "Traceback" not in result.stderr


def test_missing_optional_step_timestamps_stay_unknown(*, snapshot_path: Path) -> None:
    snapshot = load_snapshot(path=snapshot_path)
    step = snapshot["jobs"][0]["steps"][0]
    del step["started_at"]
    del step["completed_at"]
    report = build_report(snapshot=snapshot)
    measured = report["steps"][0]
    assert measured["started_at"] is None
    assert measured["completed_at"] is None
    assert measured["duration_seconds"] is None
    assert measured["duration_minutes"] is None
