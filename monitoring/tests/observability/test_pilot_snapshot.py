"""Tests for the read-only CI observability pilot snapshot.

The pilot is driven through the ``gh`` CLI boundary: every test installs
a fake ``gh`` executable first on PATH (the standard mock-environment
boundary, no production fields are patched) that answers fixed GitHub API
payloads and records the argv it was called with. Sanitized fixtures
only; no live GitHub access, no raw API JSON committed beyond the
minimal shapes these cases need, and the parent handles any live gh
execution.

Run with (``-o addopts=`` clears pytest.ini's coverage flags for a
focused run):

    cd monitoring/tests && pytest -o addopts= observability/test_pilot_snapshot.py
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _write_fake_gh(*, directory: Path, responses: list[object], record: Path | None = None) -> None:
    """A fake gh that answers fixed payload files in call order.

    A counter file gives each invocation the next scripted response
    (every call is a fresh process, so the counter is the only state).
    Each response is gh's JSON stdout. Beyond the scripted sequence the
    fake exits nonzero so an unexpected call fails loudly.
    """
    counter = directory / "gh-call-counter"
    counter.write_text("0\n", encoding="utf-8")
    body = ["#!/bin/sh", "set -eu", f"count=$(/bin/cat {counter})", "case $count in"]
    for index, response in enumerate(responses):
        fixture = directory / f"gh-response-{index}.json"
        fixture.write_text(json.dumps(response) + "\n", encoding="utf-8")
        body.append(f"  {index}) /bin/cat {fixture} ;;")
    body.append("  *) exit 5 ;;")
    body.append("esac")
    body.append(f"echo $((count + 1)) > {counter}")
    if record is not None:
        body.append(f'printf "%s\\n" "$*" >> {record}')
    body.append("exit 0")
    (directory / "gh").write_text("\n".join(body) + "\n", encoding="utf-8")
    (directory / "gh").chmod(0o700)


@pytest.fixture
def gh_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A PATH whose gh is the fake, plus a fresh output directory."""
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def _run_module(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "monitoring.ci_observability.pilot", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


RUN_PAYLOAD = {
    "id": 1234,
    "name": "tests",
    "event": "pull_request",
    "status": "completed",
    "conclusion": "success",
    "head_sha": "a" * 40,
    "run_attempt": 2,
    "created_at": "2026-10-20T04:00:00Z",
    "path": ".github/workflows/tests.yml",
}

ATTEMPT_PAYLOAD = {
    "id": 1234,
    "run_number": 12,
    "run_attempt": 2,
    "status": "completed",
    "conclusion": "success",
    "head_sha": "b" * 40,
    "run_started_at": "2026-10-20T04:00:01Z",
    "updated_at": "2026-10-20T05:00:00Z",
    "created_at": "2026-10-20T04:00:00Z",
}

JOBS_PAYLOAD = {
    "jobs": [
        {
            "id": 500,
            "name": "e2e-caas",
            "status": "completed",
            "conclusion": "success",
            "started_at": "2026-10-20T04:00:02Z",
            "completed_at": "2026-10-20T04:10:00Z",
            "run_attempt": 2,
            "steps": [
                {
                    "number": 1,
                    "name": "Run tests",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": "2026-10-20T04:00:03Z",
                    "completed_at": "2026-10-20T04:09:00Z",
                }
            ],
        },
        {
            "id": 501,
            "name": "e2e-vmaas",
            "status": "completed",
            "conclusion": "failure",
            "started_at": None,
            "completed_at": None,
            "run_attempt": 1,
            "steps": [],
        },
    ]
}

ARTIFACTS_PAYLOAD = {
    "artifacts": [
        {
            "id": 900,
            "name": "junit.xml",
            "size_in_bytes": 4096,
            "expired": False,
            "url": "https://evil.example.com/artifact",
        }
    ]
}


def test_snapshot_projects_attempt_jobs_and_built_urls(gh_env: Path) -> None:
    _write_fake_gh(directory=gh_env, responses=[RUN_PAYLOAD, ATTEMPT_PAYLOAD, JOBS_PAYLOAD, ARTIFACTS_PAYLOAD])
    output = gh_env / "out" / "snapshot.json"
    result = _run_module(
        "--repository", "osac-project/osac", "--run-id", "1234", "--attempt", "2", "--output", str(output)
    )
    assert result.returncode == 0, result.stderr
    snapshot = json.loads(output.read_text(encoding="utf-8"))

    assert snapshot["schema"] == "osac.ci-observability.pilot"
    assert snapshot["version"] == 1
    assert snapshot["repository"] == "osac-project/osac"
    assert snapshot["run"]["id"] == 1234
    assert snapshot["run"]["attempt"] == 2
    assert snapshot["run"]["url"] == "https://github.com/osac-project/osac/actions/runs/1234"
    # The attempt's head SHA, projected as-is and explicitly not the tested SHA.
    assert snapshot["run"]["head_sha"] == "b" * 40
    assert "run-head-sha-is-not-tested-sha" in snapshot["coverage"]["notes"]

    # Only the attempt-2 job is in the list; the attempt-1 row is a
    # separate execution, named in the notes, never counted.
    assert [job["id"] for job in snapshot["jobs"]] == [500]
    job = snapshot["jobs"][0]
    assert job["url"] == "https://github.com/osac-project/osac/actions/runs/1234/job/500"
    assert job["steps"][0]["name"] == "Run tests"
    assert any("jobs-of-other-attempt-excluded" in note for note in snapshot["coverage"]["notes"])
    assert snapshot["coverage"]["jobs_complete"] is False

    # The artifact URL is built from validated ids, never the payload's.
    artifact = snapshot["artifacts"][0]
    assert artifact["url"] == "https://github.com/osac-project/osac/actions/runs/1234/artifacts/900"
    assert "artifacts-are-run-wide-not-attempt-bound" in snapshot["coverage"]["notes"]
    assert snapshot["coverage"]["test_results"] == "not-collected"
    assert snapshot["coverage"]["tested_revision"] == "unverified"


def test_null_and_unknown_values_stay_unknown(gh_env: Path) -> None:
    attempt = dict(ATTEMPT_PAYLOAD, conclusion=None, run_started_at=None, updated_at=None)
    jobs = {
        "jobs": [
            {
                "id": 502,
                "name": None,
                "status": None,
                "conclusion": None,
                "started_at": None,
                "completed_at": None,
                "run_attempt": 2,
                "steps": [],
            }
        ]
    }
    _write_fake_gh(directory=gh_env, responses=[RUN_PAYLOAD, attempt, jobs, {"artifacts": []}])
    output = gh_env / "snapshot.json"
    result = _run_module("--repository", "osac-project/osac", "--run-id", "1234", "--output", str(output))
    assert result.returncode == 0, result.stderr
    snapshot = json.loads(output.read_text(encoding="utf-8"))
    assert snapshot["run"]["conclusion"] is None
    assert snapshot["run"]["started_at"] is None
    assert snapshot["run"]["updated_at"] is None
    job = snapshot["jobs"][0]
    assert job["name"] == "unknown"
    assert job["status"] == "unknown"
    assert job["conclusion"] is None
    assert job["started_at"] is None


def test_pagination_completes_and_caps_mark_coverage(gh_env: Path) -> None:
    full_page = {
        "jobs": [
            {"id": 1000 + i, "name": f"j{i}", "status": "completed", "run_attempt": 2, "steps": []} for i in range(100)
        ]
    }
    pages = [RUN_PAYLOAD, ATTEMPT_PAYLOAD]
    pages.extend([full_page] * 12)
    _write_fake_gh(directory=gh_env, responses=pages)
    output = gh_env / "snapshot.json"
    result = _run_module(
        "--repository", "osac-project/osac", "--run-id", "1234", "--attempt", "2", "--output", str(output)
    )
    # The cap (10 pages) is reached with more pages scripted: the CLI's
    # fixed exit 5 after the scripted responses surfaces as an error
    # only after the cap, so the snapshot either fails closed or marks
    # itself incomplete — it never claims a complete count.
    if result.returncode == 0:
        snapshot = json.loads(output.read_text(encoding="utf-8"))
        assert len(snapshot["jobs"]) <= 1000
        assert snapshot["coverage"]["jobs_complete"] is False
        assert "jobs-pagination-incomplete" in snapshot["coverage"]["notes"]
    else:
        assert "pilot:" in result.stderr


def test_attempt_mismatch_fails_fixed_label(gh_env: Path) -> None:
    wrong_attempt = dict(ATTEMPT_PAYLOAD, run_attempt=3)
    _write_fake_gh(directory=gh_env, responses=[RUN_PAYLOAD, wrong_attempt])
    output = gh_env / "snapshot.json"
    result = _run_module(
        "--repository", "osac-project/osac", "--run-id", "1234", "--attempt", "2", "--output", str(output)
    )
    assert result.returncode == 1
    assert result.stderr.strip() == "pilot: attempt-mismatch"
    assert not output.exists()


def test_missing_fields_fail_not_fabricated(gh_env: Path) -> None:
    bad_jobs = {"jobs": [{"name": "no-id", "run_attempt": 2, "steps": []}]}
    _write_fake_gh(directory=gh_env, responses=[RUN_PAYLOAD, ATTEMPT_PAYLOAD, bad_jobs, {"artifacts": []}])
    output = gh_env / "snapshot.json"
    result = _run_module(
        "--repository", "osac-project/osac", "--run-id", "1234", "--attempt", "2", "--output", str(output)
    )
    assert result.returncode == 1
    assert result.stderr.strip() == "pilot: job-id-invalid"


def test_url_forgery_and_control_characters_neutralized(gh_env: Path) -> None:
    jobs = {
        "jobs": [
            {
                "id": 505,
                "name": "evil\x00name\nhttp://evil.example",
                "status": "completed",
                "run_attempt": 2,
                "steps": [],
            }
        ]
    }
    artifacts = {"artifacts": [{"id": 901, "name": "a\x1fb", "size_in_bytes": 1, "expired": False}]}
    _write_fake_gh(directory=gh_env, responses=[RUN_PAYLOAD, ATTEMPT_PAYLOAD, jobs, artifacts])
    output = gh_env / "snapshot.json"
    result = _run_module(
        "--repository", "osac-project/osac", "--run-id", "1234", "--attempt", "2", "--output", str(output)
    )
    assert result.returncode == 0, result.stderr
    snapshot = json.loads(output.read_text(encoding="utf-8"))
    job_name = snapshot["jobs"][0]["name"]
    assert "\x00" not in job_name and "\n" not in job_name
    assert "evil.example" not in snapshot["jobs"][0]["url"]
    assert snapshot["artifacts"][0]["url"].endswith("/artifacts/901")


def test_private_exclusive_output(gh_env: Path) -> None:
    _write_fake_gh(directory=gh_env, responses=[RUN_PAYLOAD, ATTEMPT_PAYLOAD, JOBS_PAYLOAD, ARTIFACTS_PAYLOAD])
    output = gh_env / "private" / "snapshot.json"
    result = _run_module(
        "--repository", "osac-project/osac", "--run-id", "1234", "--attempt", "2", "--output", str(output)
    )
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700

    # A refresh to the same path refuses to overwrite; a new name works.
    (gh_env / "gh-call-counter").write_text("0\n", encoding="utf-8")
    result_again = _run_module(
        "--repository", "osac-project/osac", "--run-id", "1234", "--attempt", "2", "--output", str(output)
    )
    assert result_again.returncode == 1
    assert "pilot:" in result_again.stderr
    new_output = gh_env / "private" / "snapshot-2.json"
    (gh_env / "gh-call-counter").write_text("0\n", encoding="utf-8")
    result_new = _run_module(
        "--repository", "osac-project/osac", "--run-id", "1234", "--attempt", "2", "--output", str(new_output)
    )
    assert result_new.returncode == 0, result_new.stderr
    assert new_output.exists()


def test_gh_invocation_uses_fixed_api_paths_only(gh_env: Path) -> None:
    record = gh_env / "argv.txt"
    _write_fake_gh(
        directory=gh_env, responses=[RUN_PAYLOAD, ATTEMPT_PAYLOAD, JOBS_PAYLOAD, ARTIFACTS_PAYLOAD], record=record
    )
    output = gh_env / "snapshot.json"
    result = _run_module(
        "--repository", "osac-project/osac", "--run-id", "1234", "--attempt", "2", "--output", str(output)
    )
    assert result.returncode == 0, result.stderr
    calls = record.read_text(encoding="utf-8").splitlines()
    base = "api --hostname github.com --method GET https://api.github.com/repos/osac-project/osac/actions/runs/1234"
    assert calls == [
        base,
        f"{base}/attempts/2",
        f"{base}/attempts/2/jobs?per_page=100&page=1",
        f"{base}/artifacts?per_page=100&page=1",
    ]
    # No token ever appears on the command line.
    assert all("token" not in call.lower() for call in calls)


def test_invalid_repository_and_run_id_rejected(gh_env: Path) -> None:
    _write_fake_gh(directory=gh_env, responses=[])
    output = gh_env / "snapshot.json"
    result = _run_module("--repository", "not a repo", "--run-id", "1234", "--output", str(output))
    assert result.returncode == 1
    assert result.stderr.strip() == "pilot: repository-invalid"

    result = _run_module("--repository", "osac-project/osac", "--run-id", "0", "--output", str(output))
    assert result.returncode == 2  # argparse rejects non-positive run ids


def test_gh_failure_is_fixed_label_not_stderr(gh_env: Path) -> None:
    _write_fake_gh(directory=gh_env, responses=[])
    output = gh_env / "snapshot.json"
    result = _run_module("--repository", "osac-project/osac", "--run-id", "1234", "--output", str(output))
    assert result.returncode == 1
    assert result.stderr.strip() == "pilot: github-api-error"
    assert not output.exists()


def test_artifacts_list_key_is_read_and_missing_key_fails(gh_env: Path) -> None:
    """Artifacts come from the artifacts key; a missing expected key fails.

    Before the fix, pagination read items-or-jobs and never artifacts,
    so a real artifact page looked like an empty complete list. Now a
    page without the expected key is a constant-label failure, and a
    real artifacts page projects its rows.
    """
    _write_fake_gh(directory=gh_env, responses=[RUN_PAYLOAD, ATTEMPT_PAYLOAD, JOBS_PAYLOAD, ARTIFACTS_PAYLOAD])
    output = gh_env / "snapshot.json"
    result = _run_module(
        "--repository", "osac-project/osac", "--run-id", "1234", "--attempt", "2", "--output", str(output)
    )
    assert result.returncode == 0, result.stderr
    snapshot = json.loads(output.read_text(encoding="utf-8"))
    assert len(snapshot["artifacts"]) == 1
    assert snapshot["artifacts"][0]["id"] == 900
    assert snapshot["artifacts"][0]["name"] == "junit.xml"
    assert snapshot["artifacts"][0]["size_bytes"] == 4096
    assert snapshot["coverage"]["artifacts_complete"] is True

    # A jobs-shaped page served to the artifacts endpoint (the old bug's
    # silent shape) now fails because the expected key is absent.
    (gh_env / "gh-call-counter").write_text("0\n", encoding="utf-8")
    jobs_shaped = {"jobs": JOBS_PAYLOAD["jobs"], "total_count": 1}
    _write_fake_gh(directory=gh_env, responses=[RUN_PAYLOAD, ATTEMPT_PAYLOAD, JOBS_PAYLOAD, jobs_shaped])
    output2 = gh_env / "snapshot-2.json"
    result2 = _run_module(
        "--repository", "osac-project/osac", "--run-id", "1234", "--attempt", "2", "--output", str(output2)
    )
    assert result2.returncode == 1
    assert result2.stderr.strip() == "pilot: page-missing-artifacts"


def test_total_count_drives_completeness(gh_env: Path) -> None:
    """Rows short of total_count mark the list incomplete, never complete."""
    partial = {"artifacts": [], "total_count": 1}
    _write_fake_gh(directory=gh_env, responses=[RUN_PAYLOAD, ATTEMPT_PAYLOAD, JOBS_PAYLOAD, partial])
    output = gh_env / "snapshot.json"
    result = _run_module(
        "--repository", "osac-project/osac", "--run-id", "1234", "--attempt", "2", "--output", str(output)
    )
    assert result.returncode == 0, result.stderr
    snapshot = json.loads(output.read_text(encoding="utf-8"))
    assert snapshot["coverage"]["artifacts_complete"] is False
    assert "artifacts-pagination-incomplete" in snapshot["coverage"]["notes"]


def test_existing_permissive_parent_refused_unchanged(gh_env: Path) -> None:
    """An existing 0755 parent is refused, not chmod'ed silently."""
    _write_fake_gh(directory=gh_env, responses=[RUN_PAYLOAD, ATTEMPT_PAYLOAD, JOBS_PAYLOAD, ARTIFACTS_PAYLOAD])
    shared = gh_env / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    output = shared / "snapshot.json"
    result = _run_module(
        "--repository", "osac-project/osac", "--run-id", "1234", "--attempt", "2", "--output", str(output)
    )
    assert result.returncode == 1
    assert result.stderr.strip() == "pilot: output-parent-not-private"
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755, "an existing parent was chmod'ed"
    assert not output.exists()


def test_measured_empty_lists_are_complete(gh_env: Path) -> None:
    _write_fake_gh(
        directory=gh_env,
        responses=[RUN_PAYLOAD, ATTEMPT_PAYLOAD, {"jobs": [], "total_count": 0}, {"artifacts": [], "total_count": 0}],
    )
    output = gh_env / "snapshot.json"
    result = _run_module("--repository", "osac-project/osac", "--run-id", "1234", "--output", str(output))
    assert result.returncode == 0, result.stderr
    snapshot = json.loads(output.read_text())
    assert snapshot["jobs"] == []
    assert snapshot["artifacts"] == []
    assert snapshot["coverage"]["jobs_complete"] is True
    assert snapshot["coverage"]["artifacts_complete"] is True


def test_measured_zero_artifact_size_is_preserved(gh_env: Path) -> None:
    artifacts = {"artifacts": [{"id": 900, "name": "empty", "size_in_bytes": 0, "expired": False}], "total_count": 1}
    _write_fake_gh(directory=gh_env, responses=[RUN_PAYLOAD, ATTEMPT_PAYLOAD, JOBS_PAYLOAD, artifacts])
    output = gh_env / "snapshot.json"
    result = _run_module("--repository", "osac-project/osac", "--run-id", "1234", "--output", str(output))
    assert result.returncode == 0, result.stderr
    snapshot = json.loads(output.read_text())
    assert snapshot["artifacts"][0]["size_bytes"] == 0
    assert snapshot["artifacts"][0]["expired"] is False


@pytest.mark.parametrize(
    ("field", "error"), [("size_in_bytes", "artifact-size-invalid"), ("expired", "artifact-expired-invalid")]
)
def test_missing_artifact_facts_are_not_invented(gh_env: Path, field: str, error: str) -> None:
    artifact = dict(ARTIFACTS_PAYLOAD["artifacts"][0])
    del artifact[field]
    _write_fake_gh(directory=gh_env, responses=[RUN_PAYLOAD, ATTEMPT_PAYLOAD, JOBS_PAYLOAD, {"artifacts": [artifact]}])
    output = gh_env / "snapshot.json"
    result = _run_module("--repository", "osac-project/osac", "--run-id", "1234", "--output", str(output))
    assert result.returncode == 1
    assert result.stderr.strip() == f"pilot: {error}"
    assert not output.exists()


def test_attempt_record_must_identify_the_requested_run(gh_env: Path) -> None:
    _write_fake_gh(directory=gh_env, responses=[RUN_PAYLOAD, dict(ATTEMPT_PAYLOAD, id=999)])
    output = gh_env / "snapshot.json"
    result = _run_module("--repository", "osac-project/osac", "--run-id", "1234", "--output", str(output))
    assert result.returncode == 1
    assert result.stderr.strip() == "pilot: attempt-run-id-mismatch"
    assert not output.exists()


def test_malformed_steps_do_not_become_an_empty_list(gh_env: Path) -> None:
    jobs = {"jobs": [dict(JOBS_PAYLOAD["jobs"][0], steps="unavailable")]}
    _write_fake_gh(directory=gh_env, responses=[RUN_PAYLOAD, ATTEMPT_PAYLOAD, jobs, ARTIFACTS_PAYLOAD])
    output = gh_env / "snapshot.json"
    result = _run_module("--repository", "osac-project/osac", "--run-id", "1234", "--output", str(output))
    assert result.returncode == 1
    assert result.stderr.strip() == "pilot: job-steps-unavailable"
    assert not output.exists()
