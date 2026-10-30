"""Shared derived facts and a read-only JSON query interface for pilot snapshots.

Two real consumers share this module: the human Grafana dashboard and the
agent-query CLI. It owns the snapshot loader, validation, safe identity,
timing and derived-fact rules copied from the accepted base dashboard so
both consumers compute identical facts. The module never imports the
dashboard, performs no network access, and writes nothing: it reads one
local pilot snapshot (``osac.ci-observability.pilot`` version 1) and
derives a report (``osac.ci-observability.pilot-report`` version 1).

Loading a snapshot does not authenticate its origin or refetch GitHub:
the report describes the supplied file, and agents must preserve that
distinction.

CLI:

    python -m monitoring.ci_observability.pilot_report \
        --snapshot /private/snapshot.json --section summary
"""

from __future__ import annotations

import argparse
import json
import math
import operator
import os
import re
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import TypeAlias

JSONValue: TypeAlias = "bool | int | float | str | list[JSONValue] | dict[str, JSONValue] | None"
JSONObject: TypeAlias = "dict[str, JSONValue]"

REPORT_SCHEMA = "osac.ci-observability.pilot-report"
REPORT_VERSION = 1
SNAPSHOT_SCHEMA = "osac.ci-observability.pilot"
SNAPSHOT_VERSION = 1

MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_JOBS = 500
MAX_STEPS_PER_JOB = 200
MAX_ARTIFACTS = 1000
SLOWEST_STEPS_LIMIT = 8

REQUIRED_SNAPSHOT_KEYS = ("schema", "version", "fetched_at", "repository", "run", "jobs", "coverage")
REQUIRED_RUN_KEYS = ("id", "attempt", "name", "event", "status", "conclusion", "head_sha", "created_at", "updated_at")
REQUIRED_JOB_KEYS = ("id", "name", "status", "conclusion", "started_at", "completed_at", "url", "steps")
REQUIRED_STEP_KEYS = ("number", "name", "status", "conclusion")

# Attention conclusions: failure counts exactly; the others are listed
# separately as observations without cause inference.
FAILED_CONCLUSION = "failure"
ATTENTION_CONCLUSIONS = ("failure", "cancelled", "timed_out", "startup_failure", "action_required")

SECTIONS = ("summary", "jobs", "steps", "failures", "artifacts", "slowest_steps", "all")

_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class SnapshotError(ValueError):
    """Constant-safe: the message names the violation, never the value."""


def load_snapshot(*, path: Path) -> JSONObject:
    """Read one pilot snapshot through a bounded, non-following reader."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SnapshotError("snapshot must be a regular file")
        if metadata.st_size > MAX_SNAPSHOT_BYTES:
            raise SnapshotError("snapshot exceeds the size bound")
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_SNAPSHOT_BYTES:
                raise SnapshotError("snapshot exceeds the size bound")
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise SnapshotError("snapshot must be strict UTF-8") from None
    document = _loads_strict(text=text)
    if not isinstance(document, dict):
        raise SnapshotError("snapshot must be a JSON object")
    return document


def _loads_strict(*, text: str) -> object:
    """Reject NaN and Infinity literals. Duplicate keys keep the last value."""
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError, RecursionError):
        raise SnapshotError("snapshot is not valid JSON") from None


def _reject_constant(name: str) -> object:
    """json's parse_constant callback: called positionally by the json API."""
    raise ValueError(name)


def _validate_snapshot(*, snapshot: JSONObject) -> None:
    for key in REQUIRED_SNAPSHOT_KEYS:
        if key not in snapshot:
            raise SnapshotError(f"snapshot is missing the field: {key}")
    if snapshot["schema"] != SNAPSHOT_SCHEMA:
        raise SnapshotError("snapshot schema is not recognized")
    if snapshot["version"] != SNAPSHOT_VERSION:
        raise SnapshotError("snapshot version is not supported")
    run = snapshot["run"]
    if not isinstance(run, dict):
        raise SnapshotError("snapshot run must be an object")
    for key in REQUIRED_RUN_KEYS:
        if key not in run:
            raise SnapshotError(f"snapshot run is missing the field: {key}")
    jobs = snapshot["jobs"]
    if not isinstance(jobs, list) or len(jobs) > MAX_JOBS:
        raise SnapshotError("snapshot jobs must be a bounded list")
    for job in jobs:
        if not isinstance(job, dict):
            raise SnapshotError("snapshot job must be an object")
        for key in REQUIRED_JOB_KEYS:
            if key not in job:
                raise SnapshotError(f"snapshot job is missing the field: {key}")
        steps = job["steps"]
        if not isinstance(steps, list) or len(steps) > MAX_STEPS_PER_JOB:
            raise SnapshotError("snapshot job steps must be a bounded list")
        for step in steps:
            if not isinstance(step, dict):
                raise SnapshotError("snapshot step must be an object")
            for key in REQUIRED_STEP_KEYS:
                if key not in step:
                    raise SnapshotError(f"snapshot step is missing the field: {key}")
    coverage = snapshot["coverage"]
    if not isinstance(coverage, dict):
        raise SnapshotError("snapshot coverage must be an object")
    artifacts = snapshot.get("artifacts")
    if artifacts is not None:
        if not isinstance(artifacts, list) or len(artifacts) > MAX_ARTIFACTS:
            raise SnapshotError("snapshot artifacts must be a bounded list")
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise SnapshotError("snapshot artifact must be an object")
            for key in ("id", "name", "size_bytes", "expired"):
                if key not in artifact:
                    raise SnapshotError(f"snapshot artifact is missing the field: {key}")
            if not isinstance(artifact["id"], int) or isinstance(artifact["id"], bool) or artifact["id"] < 1:
                raise SnapshotError("artifact id must be a positive integer")
            if not isinstance(artifact["name"], str):
                raise SnapshotError("artifact name must be a string")
            size = artifact["size_bytes"]
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise SnapshotError("artifact size_bytes must be a non-negative integer")
            if not isinstance(artifact["expired"], bool):
                raise SnapshotError("artifact expired must be a boolean")


def _validated_repository(*, repository: object) -> str:
    if not isinstance(repository, str) or not _REPOSITORY_RE.fullmatch(repository):
        raise SnapshotError("repository is not in owner/name form")
    return repository


def _validated_positive_int(*, value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SnapshotError(f"{name} must be a positive integer")
    return value


def _as_conclusion(*, value: object) -> str | None:
    """A conclusion is a string or null; anything else is malformed input."""
    if value is None or isinstance(value, str):
        return value
    raise SnapshotError("snapshot conclusion must be a string or null")


def _duration_seconds(*, started: object, ended: object) -> float | None:
    """Duration from two ISO strings, or None when either bound is absent.

    A measured zero stays zero; unknown stays None. Negative or
    non-finite spans are malformed input, not facts. The value is
    unrounded: a fractional timestamp keeps its fractional seconds.
    """
    if started is None or ended is None:
        return None
    if not isinstance(started, str) or not isinstance(ended, str):
        raise SnapshotError("snapshot timestamp must be a string")
    start = _parse_timestamp(value=started)
    end = _parse_timestamp(value=ended)
    seconds = (end - start).total_seconds()
    if seconds < 0 or not math.isfinite(seconds):
        raise SnapshotError("snapshot duration is invalid")
    return seconds


def _minutes(*, seconds: float | None) -> float | None:
    """The same duration in minutes, unrounded: machine precision.

    The human view alone formats two decimals for display; rounding here
    would make an AI sum prematurely rounded values.
    """
    if seconds is None:
        return None
    return seconds / 60


def _github_url(*, repository: str, kind: str, ident: int) -> str:
    return f"https://github.com/{repository}/{kind}/{ident}"


def _run_url(*, repository: str, run_id: int, attempt: int) -> str:
    return f"https://github.com/{repository}/actions/runs/{run_id}/attempts/{attempt}"


def build_report(*, snapshot: JSONObject) -> JSONObject:
    """Build the shared report from one validated snapshot."""
    _validate_snapshot(snapshot=snapshot)
    repository = _validated_repository(repository=snapshot["repository"])
    run = snapshot["run"]
    assert isinstance(run, dict)
    run_id = _validated_positive_int(value=run["id"], name="run id")
    attempt = _validated_positive_int(value=run["attempt"], name="run attempt")
    coverage = snapshot["coverage"]
    assert isinstance(coverage, dict)
    jobs = snapshot["jobs"]
    assert isinstance(jobs, list)

    job_rows: list[JSONObject] = []
    step_rows: list[JSONObject] = []
    measured_steps: list[JSONObject] = []
    failed_jobs: list[JSONObject] = []
    attention_jobs: list[JSONObject] = []
    failed_steps: list[JSONObject] = []
    attention_steps: list[JSONObject] = []
    starts: list[str] = []
    ends: list[str] = []
    all_bounds_known = bool(jobs)

    for job in jobs:
        assert isinstance(job, dict)
        job_id = _validated_positive_int(value=job["id"], name="job id")
        job_conclusion = _as_conclusion(value=job["conclusion"])
        job_duration = _duration_seconds(started=job["started_at"], ended=job["completed_at"])
        job_url = _github_url(repository=repository, kind="actions/runs", ident=run_id) + f"/job/{job_id}"
        job_row: JSONObject = {
            "id": job_id,
            "name": job["name"],
            "status": job["status"],
            "conclusion": job_conclusion,
            "started_at": job["started_at"],
            "completed_at": job["completed_at"],
            "duration_seconds": job_duration,
            "duration_minutes": _minutes(seconds=job_duration),
            "url": job_url,
        }
        job_rows.append(job_row)
        if job_conclusion == FAILED_CONCLUSION:
            failed_jobs.append(job_row)
        if job_conclusion in ATTENTION_CONCLUSIONS:
            attention_jobs.append(job_row)
        if isinstance(job["started_at"], str):
            starts.append(job["started_at"])
        else:
            all_bounds_known = False
        if isinstance(job["completed_at"], str):
            ends.append(job["completed_at"])
        else:
            all_bounds_known = False

        steps = job["steps"]
        assert isinstance(steps, list)
        for step in steps:
            assert isinstance(step, dict)
            step_conclusion = _as_conclusion(value=step["conclusion"])
            started_at = step.get("started_at")
            completed_at = step.get("completed_at")
            step_duration = _duration_seconds(started=started_at, ended=completed_at)
            step_row: JSONObject = {
                "job_id": job_id,
                "job_name": job["name"],
                "number": step["number"],
                "name": step["name"],
                "status": step["status"],
                "conclusion": step_conclusion,
                "started_at": started_at,
                "completed_at": completed_at,
                "duration_seconds": step_duration,
                "duration_minutes": _minutes(seconds=step_duration),
                "url": job_url,
            }
            step_rows.append(step_row)
            if step_conclusion == FAILED_CONCLUSION:
                failed_steps.append(step_row)
            if step_conclusion in ATTENTION_CONCLUSIONS:
                attention_steps.append(step_row)
            if step_duration is not None and step_duration > 0:
                measured_steps.append(step_row)

    span_seconds = _job_span_seconds(
        starts=starts,
        ends=ends,
        jobs=bool(jobs),
        jobs_complete=coverage.get("jobs_complete") is True,
        all_bounds_known=all_bounds_known,
    )

    slowest_top = sorted(measured_steps, key=operator.itemgetter("duration_seconds"), reverse=True)[
        :SLOWEST_STEPS_LIMIT
    ]

    artifact_rows: list[JSONObject] = []
    raw_artifacts = snapshot.get("artifacts")
    if raw_artifacts is not None:
        assert isinstance(raw_artifacts, list)
        for artifact in raw_artifacts:
            assert isinstance(artifact, dict)
            artifact_id = _validated_positive_int(value=artifact["id"], name="artifact id")
            artifact_rows.append(
                {
                    "id": artifact_id,
                    "name": artifact["name"],
                    "size_bytes": artifact["size_bytes"],
                    "expired": artifact["expired"],
                    "url": _github_url(repository=repository, kind="actions/runs", ident=run_id)
                    + f"/artifacts/{artifact_id}",
                }
            )

    source: JSONObject = {
        "kind": "snapshot",
        "repository": repository,
        "run_id": run_id,
        "run_attempt": attempt,
        "run_name": run["name"],
        "fetched_at": snapshot["fetched_at"],
        "github_url": _run_url(repository=repository, run_id=run_id, attempt=attempt),
        "head_sha": run["head_sha"],
        "tested_revision": coverage.get("tested_revision"),
    }
    summary: JSONObject = {
        "status": run["status"],
        "conclusion": _as_conclusion(value=run["conclusion"]),
        "job_count": len(job_rows),
        "failed_job_count": len(failed_jobs),
        "step_count": len(step_rows),
        "failed_step_count": len(failed_steps),
        "job_span_seconds": span_seconds,
        "job_span_minutes": _minutes(seconds=span_seconds),
    }
    return {
        "schema": REPORT_SCHEMA,
        "version": REPORT_VERSION,
        "source": source,
        "summary": summary,
        "jobs": job_rows,
        "steps": step_rows,
        "failures": {"jobs": attention_jobs, "steps": attention_steps},
        "artifacts": artifact_rows,
        "slowest_steps": slowest_top,
        "slowest_steps_total": len(measured_steps),
        "coverage": coverage,
    }


def _job_span_seconds(
    *, starts: list[str], ends: list[str], jobs: bool, jobs_complete: bool, all_bounds_known: bool
) -> float | None:
    """Earliest job start to latest job end, only when fully measured.

    The span is a wall-clock envelope, never the sum of parallel job
    durations, and excludes pre-job queueing. Any missing bound, an
    empty job list, or incomplete job coverage leaves it unknown (null),
    never zero. Timestamps are parsed before comparison: mixed-precision
    ISO strings do not sort correctly as strings.
    """
    if not jobs or not jobs_complete or not all_bounds_known or not starts or not ends:
        return None
    start = min(_parse_timestamp(value=value) for value in starts)
    end = max(_parse_timestamp(value=value) for value in ends)
    seconds = (end - start).total_seconds()
    if seconds < 0 or not math.isfinite(seconds):
        raise SnapshotError("snapshot duration is invalid")
    return seconds


def _parse_timestamp(*, value: str) -> datetime:
    """One ISO timestamp as a datetime; malformed input is an error."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SnapshotError("snapshot timestamp is invalid") from None


def main(*, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m monitoring.ci_observability.pilot_report",
        description="Query derived facts from one local pilot snapshot as JSON.",
    )
    parser.add_argument("--snapshot", required=True, help="Path to the snapshot JSON file")
    parser.add_argument(
        "--section", choices=SECTIONS, default="summary", help="Report section to print (default: summary)"
    )
    args = parser.parse_args(argv)

    try:
        snapshot = load_snapshot(path=Path(args.snapshot))
        report = build_report(snapshot=snapshot)
        document = _section_document(report=report, section=args.section)
        payload = json.dumps(document, indent=2, allow_nan=False) + "\n"
    except SnapshotError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except (OSError, TypeError, ValueError):
        print("ERROR: snapshot cannot be processed", file=sys.stderr)
        return 1

    sys.stdout.write(payload)
    return 0


def _section_document(*, report: JSONObject, section: str) -> JSONObject:
    if section == "all":
        return report
    document: JSONObject = {
        "schema": report["schema"],
        "version": report["version"],
        "source": report["source"],
        "coverage": report["coverage"],
        "section": section,
        "data": report[section],
    }
    if section == "slowest_steps":
        # The section must say it is a top-eight subset of a larger
        # measured set, so a reader of it alone needs no second query.
        data = report["slowest_steps"]
        assert isinstance(data, list)
        document["selection"] = {
            "limit": SLOWEST_STEPS_LIMIT,
            "returned": len(data),
            "total_measured_positive": report["slowest_steps_total"],
        }
    return document


if __name__ == "__main__":
    sys.exit(main())
