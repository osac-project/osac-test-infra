"""Read-only GitHub run snapshot MVP for CI observability.

One explicit workflow run and attempt, fetched through the authenticated
``gh`` CLI (its stored credentials only), projected into a small fixed
JSON shape for the parallel view author. This is a manual snapshot: no
poller, no producer or ledger writes, no artifact downloads, no raw logs,
and no claim of the full collection spec. The run head SHA is the head of
the run attempt, not the tested SHA, and attempt-scoped facts come only
from the attempts endpoints — never inferred from run-wide job lists.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

SCHEMA = "osac.ci-observability.pilot"
VERSION = 1

REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

MAX_ITEMS = 1000
MAX_PAGES = 10
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
GH_TIMEOUT_SECONDS = 60
TEXT_LIMIT = 512


class PilotError(ValueError):
    """A pilot failure. Messages are fixed labels, never payload text."""


def _gh(*, path: str) -> object:
    """One read-only gh api GET on api.github.com, bounded.

    The path is one fully-validated string built by this module; payload
    data never becomes a URL. Only gh's stored authenticated context is
    used: no token is passed on the command line or read from the
    environment here.
    """
    joined = path.strip("/")
    command = ["gh", "api", "--hostname", "github.com", "--method", "GET", f"https://api.github.com/{joined}"]
    result = subprocess.run(command, capture_output=True, timeout=GH_TIMEOUT_SECONDS, check=False)
    if result.returncode != 0:
        raise PilotError("github-api-error")
    body = result.stdout
    if len(body) > MAX_RESPONSE_BYTES:
        raise PilotError("response-oversize")
    try:
        return json.loads(body)
    except ValueError:
        raise PilotError("response-not-json") from None


def _require_mapping(*, value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PilotError(f"{label}-not-an-object")
    return value


def _positive_int(*, value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PilotError(f"{label}-invalid")
    return value


def _nonnegative_int(*, value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PilotError(f"{label}-invalid")
    return value


def _optional_int(*, value: object, label: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value=value, label=label)


def _clean_text(*, value: object) -> str:
    """Bound one metadata label and remove terminal controls."""
    if value is None:
        return ""
    text = CONTROL_CHARS.sub(" ", str(value)).strip()
    return text[:TEXT_LIMIT]


def _optional_utc(*, value: object) -> str | None:
    """A timestamp from GitHub, or None when GitHub has none.

    GitHub timestamps are already UTC ISO-8601; only unknown ones stay
    null, never guessed or zero.
    """
    text = _clean_text(value=value)
    return text or None


def _build_url(*, repository: str, parts: Sequence[str | int]) -> str:
    joined = "/".join(str(part) for part in parts)
    return f"https://github.com/{repository}/actions/runs/{joined}"


def _fetch_run(*, repository: str, run_id: int) -> Mapping[str, object]:
    run = _require_mapping(value=_gh(path=f"repos/{repository}/actions/runs/{run_id}"), label="run")
    if _positive_int(value=run.get("id"), label="run-id") != run_id:
        raise PilotError("run-id-mismatch")
    return run


def _resolve_attempt(*, repository: str, run_id: int, run: Mapping[str, object], attempt: int | None) -> int:
    """The explicit attempt to snapshot: given, or the run's latest.

    GitHub's run attempt number on the run object is the latest attempt;
    an explicit --attempt always wins so the snapshot is reproducible.
    """
    if attempt is not None:
        return attempt
    run_attempt = _optional_int(value=run.get("run_attempt"), label="run-attempt")
    if run_attempt is None:
        raise PilotError("run-attempt-unknown")
    return run_attempt


def _fetch_attempt(*, repository: str, run_id: int, attempt: int) -> Mapping[str, object]:
    """The exact attempt record, so attempt-scoped facts are never inferred."""
    data = _require_mapping(
        value=_gh(path=f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}"), label="attempt"
    )
    if _positive_int(value=data.get("id"), label="attempt-run-id") != run_id:
        raise PilotError("attempt-run-id-mismatch")
    actual = _optional_int(value=data.get("run_attempt"), label="attempt-run-attempt")
    if actual != attempt:
        raise PilotError("attempt-mismatch")
    return data


def _paginate(*, path: str, list_key: str) -> tuple[list[Mapping[str, object]], bool]:
    """Fully paginate one list endpoint within the caps.

    The list key names the endpoint's own rows (``jobs`` for the jobs
    endpoint, ``artifacts`` for the artifacts endpoint); a page without
    that key, or with a non-list value under it, fails with a constant
    label instead of quietly reading an empty list and claiming a
    complete snapshot. Completeness also respects the endpoint's
    ``total_count`` when it is present: rows short of it, or caps hit
    with more known to remain, mark the list incomplete. Exactly 1000
    items with no larger total_count is complete; more known beyond the
    cap is not.
    """
    rows: list[Mapping[str, object]] = []
    base = path.strip("/")
    for page in range(1, MAX_PAGES + 1):
        payload = _require_mapping(value=_gh(path=f"{base}?per_page=100&page={page}"), label="page")
        if list_key not in payload:
            raise PilotError(f"page-missing-{list_key}")
        batch = payload[list_key]
        if not isinstance(batch, list):
            raise PilotError(f"page-{list_key}-not-a-list")
        for item in batch:
            rows.append(_require_mapping(value=item, label="item"))
        total = payload.get("total_count")
        if total is not None:
            total = _nonnegative_int(value=total, label="page-total-count")
        if total is not None and len(rows) >= total:
            return rows, True
        if len(batch) < 100:
            return rows, total is None or len(rows) >= total
        if len(rows) >= MAX_ITEMS:
            return rows, False
    return rows, False


def _project_job(*, raw: Mapping[str, object], repository: str, run_id: int) -> dict[str, object]:
    job_id = _positive_int(value=raw.get("id"), label="job-id")
    steps = []
    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list):
        raise PilotError("job-steps-unavailable")
    for raw_step in raw_steps:
        step = _require_mapping(value=raw_step, label="step")
        number = _positive_int(value=step.get("number"), label="step-number")
        steps.append(
            {
                "number": number,
                "name": _clean_text(value=step.get("name")),
                "status": _clean_text(value=step.get("status")) or "unknown",
                "conclusion": _optional_text(value=step.get("conclusion")),
                "started_at": _optional_utc(value=step.get("started_at")),
                "completed_at": _optional_utc(value=step.get("completed_at")),
            }
        )
    return {
        "id": job_id,
        "name": _clean_text(value=raw.get("name")) or "unknown",
        "status": _clean_text(value=raw.get("status")) or "unknown",
        "conclusion": _optional_text(value=raw.get("conclusion")),
        "started_at": _optional_utc(value=raw.get("started_at")),
        "completed_at": _optional_utc(value=raw.get("completed_at")),
        "url": _build_url(repository=repository, parts=(run_id, "job", job_id)),
        "steps": steps,
    }


def _optional_text(*, value: object) -> str | None:
    text = _clean_text(value=value)
    return text or None


def _project_artifact(*, raw: Mapping[str, object], repository: str, run_id: int) -> dict[str, object]:
    artifact_id = _positive_int(value=raw.get("id"), label="artifact-id")
    size_bytes = _nonnegative_int(value=raw.get("size_in_bytes"), label="artifact-size")
    expired = raw.get("expired")
    if not isinstance(expired, bool):
        raise PilotError("artifact-expired-invalid")
    return {
        "id": artifact_id,
        "name": _clean_text(value=raw.get("name")) or "unknown",
        "size_bytes": size_bytes,
        "expired": expired,
        # Built from validated ids, never from the payload's own URL.
        "url": _build_url(repository=repository, parts=(run_id, "artifacts", artifact_id)),
    }


def _split_jobs(
    *, jobs: Sequence[Mapping[str, object]], attempt: int
) -> tuple[list[Mapping[str, object]], list[str], bool]:
    """Attempt jobs, with run-wide or other-attempt rows kept separate.

    A job whose run_attempt differs from the selected attempt is another
    execution of the run (an earlier attempt, or a re-run): it is
    excluded from the attempt's job list and named in the coverage notes
    instead of being silently counted or dropped.
    """
    projected = []
    other_attempts: set[int] = set()
    no_attempt = 0
    for raw in jobs:
        job_attempt = _optional_int(value=raw.get("run_attempt"), label="job-run-attempt")
        if job_attempt == attempt:
            projected.append(raw)
        elif job_attempt is None:
            no_attempt += 1
        else:
            other_attempts.add(job_attempt)
    notes = [f"jobs-of-other-attempt-excluded: {sorted(other_attempts)}"] if other_attempts else []
    if no_attempt:
        notes.append(f"jobs-without-attempt-excluded: {no_attempt}")
    inherited = bool(other_attempts) or bool(no_attempt)
    return projected, notes, inherited


def build_snapshot(*, repository: str, run_id: int, attempt: int | None) -> dict[str, object]:
    """Fetch and project one run attempt into the pilot JSON contract."""
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise PilotError("repository-invalid")
    for part in repository.split("/"):
        if part in (".", ".."):
            raise PilotError("repository-invalid")
    run = _fetch_run(repository=repository, run_id=run_id)
    selected_attempt = _resolve_attempt(repository=repository, run_id=run_id, run=run, attempt=attempt)
    attempt_data = _fetch_attempt(repository=repository, run_id=run_id, attempt=selected_attempt)

    raw_jobs, jobs_complete = _paginate(
        path=f"repos/{repository}/actions/runs/{run_id}/attempts/{selected_attempt}/jobs", list_key="jobs"
    )
    raw_artifacts, artifacts_complete = _paginate(
        path=f"repos/{repository}/actions/runs/{run_id}/artifacts", list_key="artifacts"
    )

    attempt_jobs, attempt_notes, inherited = _split_jobs(jobs=raw_jobs, attempt=selected_attempt)
    projected_jobs = [_project_job(raw=raw, repository=repository, run_id=run_id) for raw in attempt_jobs]
    projected_artifacts = [_project_artifact(raw=raw, repository=repository, run_id=run_id) for raw in raw_artifacts]

    notes = ["run-head-sha-is-not-tested-sha", "artifacts-are-run-wide-not-attempt-bound"]
    notes.extend(attempt_notes)
    if not jobs_complete:
        notes.append("jobs-pagination-incomplete")
    if not artifacts_complete:
        notes.append("artifacts-pagination-incomplete")
    if inherited:
        notes.append("run-wide-rows-from-other-executions-present")

    # The run endpoint describes the latest attempt, which may differ.
    status = _clean_text(value=attempt_data.get("status")) or "unknown"
    conclusion = _optional_text(value=attempt_data.get("conclusion"))

    return {
        "schema": SCHEMA,
        "version": VERSION,
        "fetched_at": _now_utc(),
        "repository": repository,
        "run": {
            "id": run_id,
            "attempt": selected_attempt,
            "name": _clean_text(value=run.get("name")) or "unknown",
            "event": _clean_text(value=run.get("event")) or "unknown",
            "status": status,
            "conclusion": conclusion,
            # The attempt's head SHA, explicitly not the tested SHA.
            "head_sha": _clean_text(value=attempt_data.get("head_sha")) or "unknown",
            "created_at": _optional_utc(value=run.get("created_at"))
            or _optional_utc(value=attempt_data.get("created_at")),
            "started_at": _optional_utc(value=attempt_data.get("run_started_at")),
            "updated_at": _optional_utc(value=attempt_data.get("updated_at")),
            "url": _build_url(repository=repository, parts=(run_id,)),
            "workflow_path": _optional_text(value=run.get("path")),
        },
        "jobs": projected_jobs,
        "artifacts": projected_artifacts,
        "coverage": {
            "jobs_complete": jobs_complete and not inherited,
            "artifacts_complete": artifacts_complete,
            "test_results": "not-collected",
            "tested_revision": "unverified",
            "notes": notes,
        },
    }


def _now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _prepare_private_parent(*, parent: Path) -> None:
    """Ensure the output's parent directory exists and is safely private.

    A directory this call creates is mode 0700. An existing directory is
    only validated — never chmod'ed — and must be a real directory (not
    a symlink), owned by the current user, with no group or other
    access bits, so the snapshot never lands somewhere more permissive
    or outside the caller's control.
    """
    if parent.is_symlink():
        raise PilotError("output-parent-symlink")
    if parent.exists():
        info = parent.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise PilotError("output-parent-not-directory")
        if info.st_uid != os.getuid():
            raise PilotError("output-parent-not-owned")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise PilotError("output-parent-not-private")
        return
    parent.mkdir(parents=True, mode=0o700)
    info = parent.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise PilotError("output-parent-not-private")


def _write_private(*, path: Path, payload: Mapping[str, object]) -> None:
    """Write the snapshot to a new private file, never clobbering.

    The output path must not already exist: a refresh writes a new file
    name, so a previous snapshot is never overwritten, truncated, or
    followed through a symlink or hardlink.
    """
    _prepare_private_parent(parent=path.parent)
    body = json.dumps(payload, indent=2, allow_nan=False) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(body)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise PilotError("output-not-regular")


def main(*, argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m monitoring.ci_observability.pilot",
        description="Fetch one explicit GitHub run attempt into a private JSON snapshot.",
    )
    parser.add_argument("--repository", required=True, help="Repository in owner/name form")
    parser.add_argument("--run-id", required=True, type=_cli_positive_int, help="Workflow run id")
    parser.add_argument("--attempt", type=_cli_positive_int, default=None, help="Run attempt (default: latest)")
    parser.add_argument("--output", required=True, help="New private output file path")
    args = parser.parse_args(argv)

    try:
        snapshot = build_snapshot(repository=args.repository, run_id=args.run_id, attempt=args.attempt)
        _write_private(path=Path(args.output), payload=snapshot)
    except PilotError as error:
        print(f"pilot: {error}", file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError):
        print("pilot: gh-invocation-failed", file=sys.stderr)
        return 1
    jobs = cast(list[dict[str, object]], snapshot["jobs"])
    artifacts = cast(list[dict[str, object]], snapshot["artifacts"])
    print(f"pilot: snapshot written: {len(jobs)} jobs, {len(artifacts)} artifacts")
    return 0


def _cli_positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
