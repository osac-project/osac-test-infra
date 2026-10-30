"""Read-only investigation of one published execution."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import cast

from .contracts.derivation import derive_attempt_result
from .contracts.validation import validate_record
from .ledger import DEFAULT_SCHEMA, ExecutionRow, JSONValue, Ledger, StoredEvent, parse_utc_text

SCHEMA = "osac.ci-observability.execution-report"
VERSION = 1
MAX_EVENTS = 100_000
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_REPORT_BYTES = 16 * 1024 * 1024
SECTIONS = (
    "summary",
    "selection",
    "tests",
    "attempts",
    "phases",
    "workers",
    "fixtures",
    "operations",
    "resources",
    "coverage",
    "all",
)
FAILURES = frozenset({"failed", "error", "timeout"})

JSONObject = dict[str, JSONValue]
TestKey = tuple[str, str]
AttemptKey = tuple[str, str, int | None, str | None]


class InvestigationError(ValueError):
    """A fixed-label failure without source content."""


@dataclass(kw_only=True)
class PhaseAttempt:
    invocation_id: str | None
    journal_id: str
    test_id: str
    attempt_index: int
    worker_id: str
    phases: dict[str, JSONObject] = field(default_factory=dict)


def query_execution(*, client: Ledger, execution_id: str) -> JSONObject:
    execution = client.get_execution(execution_id=execution_id)
    if execution is None:
        raise InvestigationError("execution-not-found")
    before = client.execution_counts(execution_id=execution_id)
    if before[0] > MAX_EVENTS:
        raise InvestigationError("execution-event-limit")
    events: list[StoredEvent] = []
    cursor = None
    source_bytes = 0
    while True:
        page = client.list_event_page(execution_id=execution_id, limit=500, after=cursor)
        for event in page.events:
            source_bytes += len(json.dumps(event.data, ensure_ascii=True, allow_nan=False).encode("utf-8"))
            if len(events) >= MAX_EVENTS or source_bytes > MAX_SOURCE_BYTES:
                raise InvestigationError("execution-source-limit")
            events.append(event)
        cursor = page.next_cursor
        if cursor is None:
            break
    after = client.execution_counts(execution_id=execution_id)
    report = build_report(execution=execution, events=events)
    coverage = cast(JSONObject, report["coverage"])
    coverage["stable_read"] = before == after and after[0] == len(events)
    coverage["read_consistency"] = "live-pages"
    if coverage["stable_read"] is not True:
        coverage["complete"] = False
    return report


def build_report(*, execution: ExecutionRow, events: Sequence[StoredEvent]) -> JSONObject:
    if len(events) > MAX_EVENTS:
        raise InvestigationError("execution-event-limit")
    attempts: dict[AttemptKey, PhaseAttempt] = {}
    aggregates: dict[TestKey, JSONObject] = {}
    catalog: dict[TestKey, JSONObject] = {}
    native_attempts: dict[AttemptKey, JSONObject] = {}
    phases: list[JSONObject] = []
    fixtures: list[JSONObject] = []
    workers: list[JSONObject] = []
    operations: list[JSONObject] = []
    resources: list[JSONObject] = []
    collection: dict[tuple[str, str], JSONObject] = {}
    open_phases: dict[tuple[str, str, int, str, str], StoredEvent] = {}
    open_fixtures: dict[tuple[str, str, str], StoredEvent] = {}
    open_operations: dict[tuple[str, str], StoredEvent] = {}
    open_workers: dict[tuple[str, str], StoredEvent] = {}
    fixture_users: dict[tuple[str, str], list[JSONObject]] = {}
    terminal: JSONObject | None = None
    execution_start: StoredEvent | None = None
    execution_end: StoredEvent | None = None
    last_observed_at: str | None = None
    identities: set[tuple[str, int]] = set()

    for event in sorted(events, key=_journal_order):
        if event.execution_id != execution.execution_id:
            raise InvestigationError("execution-event-mismatch")
        identity = (event.journal_id, event.sequence)
        if identity in identities:
            raise InvestigationError("duplicate-event")
        identities.add(identity)
        validate_record(record=_record(event=event))
        if last_observed_at is None or parse_utc_text(value=event.recorded_at) > parse_utc_text(value=last_observed_at):
            last_observed_at = event.recorded_at
        data = event.data
        invocation_id = _scope(event=event)
        if event.event == "execution_started":
            if execution_start is not None:
                raise InvestigationError("duplicate-execution-start")
            execution_start = event
        elif event.event == "execution_finished":
            if terminal is not None:
                raise InvestigationError("duplicate-execution-terminal")
            terminal = data
            execution_end = event
        elif event.event == "collection_state":
            collection[(invocation_id, cast(str, data["source"]))] = data
        elif event.event == "test_collected":
            key = (invocation_id, cast(str, data["test_id"]))
            if key in catalog:
                raise InvestigationError("duplicate-collected-test")
            catalog[key] = {
                "invocation_id": event.invocation_id,
                "journal_id": event.journal_id,
                "test_id": data["test_id"],
                "deselected": data["deselected"],
            }
        elif event.event == "test_phase_started":
            phase_key = (*_attempt_key(event=event), cast(str, data["phase"]))
            if phase_key in open_phases:
                raise InvestigationError("duplicate-phase-start")
            open_phases[phase_key] = event
        elif event.event == "test_phase_finished":
            attempt_key = _attempt_key(event=event)
            phase_name = cast(str, data["phase"])
            phase_key = (*attempt_key, phase_name)
            started = open_phases.pop(phase_key, None)
            phase = _interval(started=started, finished=event)
            phase.update(_test_identity(event=event))
            phase["phase"] = phase_name
            phases.append(phase)
            if attempt_key not in attempts:
                attempts[attempt_key] = PhaseAttempt(
                    invocation_id=event.invocation_id,
                    journal_id=event.journal_id,
                    test_id=attempt_key[1],
                    attempt_index=cast(int, attempt_key[2]),
                    worker_id=cast(str, attempt_key[3]),
                )
            attempt = attempts[attempt_key]
            if phase_name in attempt.phases:
                raise InvestigationError("duplicate-phase-finish")
            attempt.phases[phase_name] = dict(data)
            attempt.phases[phase_name]["duration_ns"] = phase["duration_ns"]
        elif event.event == "worker_started":
            key = (invocation_id, cast(str, data["worker_id"]))
            if key in open_workers:
                raise InvestigationError("duplicate-worker-start")
            open_workers[key] = event
        elif event.event == "worker_stopped":
            key = (invocation_id, cast(str, data["worker_id"]))
            started = open_workers.pop(key, None)
            row = _interval(started=started, finished=event)
            row.update(data)
            row["invocation_id"] = event.invocation_id
            workers.append(row)
        elif event.event == "test_result":
            row = dict(data)
            row["invocation_id"] = event.invocation_id
            row["journal_id"] = event.journal_id
            row["result_source"] = "native-report"
            if "duration_ns" not in row:
                row["duration_ns"] = None
            if data["result_scope"] == "aggregate":
                test_key = (invocation_id, cast(str, data["test_id"]))
                if test_key in aggregates:
                    raise InvestigationError("duplicate-test-aggregate")
                aggregates[test_key] = row
            else:
                key = _attempt_key(event=event)
                if key in native_attempts:
                    raise InvestigationError("duplicate-test-attempt")
                native_attempts[key] = row
        elif event.event == "fixture_started":
            key = (invocation_id, cast(str, data["fixture_instance_id"]), cast(str, data["phase"]))
            if key in open_fixtures:
                raise InvestigationError("duplicate-fixture-start")
            open_fixtures[key] = event
        elif event.event == "fixture_finished":
            key = (invocation_id, cast(str, data["fixture_instance_id"]), cast(str, data["phase"]))
            started = open_fixtures.pop(key, None)
            row = _interval(started=started, finished=event)
            row.update(data)
            row["invocation_id"] = event.invocation_id
            fixtures.append(row)
        elif event.event == "fixture_used":
            key = (invocation_id, cast(str, data["fixture_instance_id"]))
            if key not in fixture_users:
                fixture_users[key] = []
            fixture_users[key].append(_test_identity(event=event))
        elif event.event == "operation_started":
            key = (invocation_id, cast(str, data["operation_id"]))
            if key in open_operations:
                raise InvestigationError("duplicate-operation-start")
            open_operations[key] = event
        elif event.event == "operation_finished":
            key = (invocation_id, cast(str, data["operation_id"]))
            started = open_operations.pop(key, None)
            row = _interval(started=started, finished=event)
            if started is not None:
                row.update(started.data)
            row.update(data)
            row["invocation_id"] = event.invocation_id
            operations.append(row)
        elif event.event == "resource_observed":
            row = dict(data)
            row["observed_at"] = event.source_at if event.source_at is not None else event.recorded_at
            resources.append(row)

    for started in open_phases.values():
        row = _interval(started=started, finished=None)
        row.update(_test_identity(event=started))
        row["phase"] = started.data["phase"]
        phases.append(row)
        key = _attempt_key(event=started)
        if key not in attempts:
            attempts[key] = PhaseAttempt(
                invocation_id=started.invocation_id,
                journal_id=started.journal_id,
                test_id=key[1],
                attempt_index=cast(int, key[2]),
                worker_id=cast(str, key[3]),
            )
    for started in open_fixtures.values():
        row = _interval(started=started, finished=None)
        row.update(started.data)
        row["invocation_id"] = started.invocation_id
        fixtures.append(row)
    for started in open_operations.values():
        row = _interval(started=started, finished=None)
        row.update(started.data)
        row["invocation_id"] = started.invocation_id
        operations.append(row)
    for started in open_workers.values():
        row = _interval(started=started, finished=None)
        row.update(started.data)
        row["invocation_id"] = started.invocation_id
        workers.append(row)
    for fixture in fixtures:
        key = (_row_scope(row=fixture), cast(str, fixture["fixture_instance_id"]))
        fixture["used_by"] = cast(JSONValue, fixture_users[key]) if key in fixture_users else []

    attempt_rows = _attempt_rows(phases=attempts, native=native_attempts)
    tests = _test_rows(aggregates=aggregates, attempts=attempt_rows)
    test_keys = {(_row_scope(row=row), cast(str, row["test_id"])) for row in tests}
    for key, selected in catalog.items():
        selected["result_observed"] = key in test_keys if selected["invocation_id"] is not None else None
    unfinished_count = len(open_phases) + len(open_fixtures) + len(open_operations) + len(open_workers)
    incomplete_attempts = sum("complete" in row and row["complete"] is False for row in attempt_rows)
    known_gap = bool(unfinished_count or incomplete_attempts) or any(
        state["complete"] is False for state in collection.values()
    )
    complete: bool | None = None
    if terminal is None or known_gap:
        complete = False
    elif collection:
        complete = True
    summary: JSONObject = {
        "github_job_conclusion": execution.github_job_conclusion,
        "reported_execution_outcome": terminal["outcome"] if terminal is not None else None,
        "test_count": len(tests),
        "collected_test_count": len(catalog),
        "deselected_test_count": sum(row["deselected"] is True for row in catalog.values()),
        "failed_test_count": sum(row["outcome"] in FAILURES for row in tests),
        "unknown_test_count": sum(row["outcome"] == "unknown" for row in tests),
        "observed_attempt_count": len(attempt_rows),
        "fixture_interval_count": len(fixtures),
        "operation_count": len(operations),
        "worker_count": len(workers),
        "resource_observation_count": len(resources),
        "duration_ns": _measured_duration(started=execution_start, finished=execution_end),
        "measured_phase_work_ns": _sum_durations(rows=phases),
        "measured_fixture_work_ns": _sum_durations(rows=fixtures),
        "measured_operation_work_ns": _sum_durations(rows=operations),
    }
    _add_minutes(row=summary)
    for rows in (tests, attempt_rows, phases, workers, fixtures, operations):
        for row in rows:
            _add_minutes(row=row)
    return {
        "schema": SCHEMA,
        "version": VERSION,
        "source": {
            "kind": "ledger",
            "execution_id": execution.execution_id,
            "repository_id": execution.repository_id,
            "run_id": execution.run_id,
            "run_attempt": execution.run_attempt,
            "github_job_id": execution.github_job_id,
        },
        "summary": summary,
        "selection": cast(JSONValue, list(catalog.values())),
        "coverage": {
            "measurement_scope": "observed-records",
            "complete": complete,
            "event_count": len(events),
            "journal_count": len({event.journal_id for event in events}),
            "last_observed_at": last_observed_at,
            "execution_terminal_observed": terminal is not None,
            "unfinished_interval_count": unfinished_count,
            "incomplete_attempt_count": incomplete_attempts,
            "sources": cast(JSONValue, list(collection.values())),
            "notes": ["Work durations can overlap. Do not add phase, fixture and operation work to wall time."],
        },
        "tests": cast(JSONValue, tests),
        "attempts": cast(JSONValue, attempt_rows),
        "phases": cast(JSONValue, phases),
        "workers": cast(JSONValue, workers),
        "fixtures": cast(JSONValue, fixtures),
        "operations": cast(JSONValue, operations),
        "resources": cast(JSONValue, resources),
    }


def _record(*, event: StoredEvent) -> JSONObject:
    record = cast(JSONObject, asdict(event))
    del record["ingested_at"]
    record["schema"] = "osac.ci-observability.journal-record"
    for key in ("source_at", "invocation_id", "clock_id", "clock_uncertainty_ns"):
        if record[key] is None:
            del record[key]
    return record


def _scope(*, event: StoredEvent) -> str:
    return event.invocation_id if event.invocation_id is not None else f":journal:{event.journal_id}"


def _row_scope(*, row: JSONObject) -> str:
    invocation_id = row["invocation_id"]
    return cast(str, invocation_id) if invocation_id is not None else f":journal:{row['journal_id']}"


def _journal_order(event: StoredEvent) -> tuple[str, int]:
    return event.journal_id, event.sequence


def _attempt_key(*, event: StoredEvent) -> AttemptKey:
    data = event.data
    return (
        _scope(event=event),
        cast(str, data["test_id"]),
        cast(int | None, data["attempt_index"]),
        cast(str | None, data["worker_id"]) if "worker_id" in data else None,
    )


def _test_identity(*, event: StoredEvent) -> JSONObject:
    data = event.data
    return {
        "invocation_id": event.invocation_id,
        "journal_id": event.journal_id,
        "test_id": data["test_id"],
        "attempt_index": data["attempt_index"],
        "worker_id": data["worker_id"],
    }


def _interval(*, started: StoredEvent | None, finished: StoredEvent | None) -> JSONObject:
    row: JSONObject = {
        "journal_id": finished.journal_id if finished is not None else cast(StoredEvent, started).journal_id,
        "started_at": started.recorded_at if started is not None else None,
        "ended_at": finished.recorded_at if finished is not None else None,
        "duration_ns": _measured_duration(started=started, finished=finished),
        "outcome": finished.data["outcome"] if finished is not None else "unknown",
        "start_observed": started is not None,
        "finish_observed": finished is not None,
    }
    if finished is not None and "failure" in finished.data:
        row["failure"] = finished.data["failure"]
    return row


def _measured_duration(*, started: StoredEvent | None, finished: StoredEvent | None) -> int | None:
    if finished is None:
        return None
    if "duration_ns" in finished.data:
        return cast(int | None, finished.data["duration_ns"])
    if started is None or started.clock_id is None or started.clock_id != finished.clock_id:
        return None
    if started.mono_ns is None or finished.mono_ns is None:
        return None
    if finished.mono_ns < started.mono_ns:
        raise InvestigationError("monotonic-clock-reversed")
    return finished.mono_ns - started.mono_ns


def _attempt_rows(*, phases: dict[AttemptKey, PhaseAttempt], native: dict[AttemptKey, JSONObject]) -> list[JSONObject]:
    rows = dict(native)
    for key, attempt in phases.items():
        if key in rows:
            raise InvestigationError("attempt-has-phases-and-native-result")
        values = list(attempt.phases.values())
        result = derive_attempt_result(phases=values)
        complete = "setup" in attempt.phases and "teardown" in attempt.phases
        if "setup" in attempt.phases and attempt.phases["setup"]["outcome"] == "passed":
            complete = complete and "call" in attempt.phases
        outcome = result.outcome
        if not complete and outcome in ("passed", "expected_failure", "unexpected_pass", "skipped"):
            outcome = "unknown"
        duration = _sum_durations(rows=values) if complete else None
        rows[key] = {
            "invocation_id": attempt.invocation_id,
            "journal_id": attempt.journal_id,
            "test_id": attempt.test_id,
            "attempt_index": attempt.attempt_index,
            "worker_id": attempt.worker_id,
            "outcome": outcome,
            "duration_ns": duration,
            "complete": complete,
            "first_failure": cast(JSONValue, result.first_failure),
            "cleanup_failure": cast(JSONValue, result.cleanup_failure),
            "result_source": "pytest-phases",
            "result_scope": "attempt",
        }
    return list(rows.values())


def _test_rows(*, aggregates: dict[TestKey, JSONObject], attempts: list[JSONObject]) -> list[JSONObject]:
    grouped: dict[TestKey, list[JSONObject]] = {}
    for attempt in attempts:
        key = (_row_scope(row=attempt), cast(str, attempt["test_id"]))
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(attempt)
    tests = dict(aggregates)
    for key, values in grouped.items():
        if key in tests:
            continue
        indexes = [row["attempt_index"] for row in values]
        if None in indexes:
            tests[key] = {
                "invocation_id": values[0]["invocation_id"],
                "journal_id": values[0]["journal_id"],
                "test_id": key[1],
                "outcome": "unknown",
                "duration_ns": _sum_durations(rows=values),
                "attempt_count": None,
                "observed_attempt_count": len(values),
                "result_scope": "aggregate",
                "result_source": "observed-attempts",
                "complete": False,
            }
            continue
        if len(set(indexes)) != len(indexes):
            raise InvestigationError("ambiguous-test-attempt")
        values.sort(key=_attempt_index)
        row = dict(values[-1])
        indexes_complete = sorted(indexes) == list(range(1, len(indexes) + 1))
        row["attempt_count"] = len(values) if indexes_complete else None
        row["observed_attempt_count"] = len(values)
        row["result_scope"] = "aggregate"
        row["duration_ns"] = _sum_durations(rows=values)
        tests[key] = row
    return [tests[key] for key in sorted(tests)]


def _attempt_index(row: JSONObject) -> int:
    return cast(int, row["attempt_index"])


def _sum_durations(*, rows: Sequence[JSONObject]) -> int | None:
    if not rows or any("duration_ns" not in row or row["duration_ns"] is None for row in rows):
        return None
    return sum(cast(int, row["duration_ns"]) for row in rows)


def _add_minutes(*, row: JSONObject) -> None:
    duration = row["duration_ns"]
    row["duration_seconds"] = cast(int, duration) / 1_000_000_000 if duration is not None else None
    row["duration_minutes"] = cast(int, duration) / 60_000_000_000 if duration is not None else None


def main(*, argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Query one published execution as structured JSON.")
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA)
    parser.add_argument("--section", choices=SECTIONS, default="summary")
    arguments = parser.parse_args(argv)
    if "CI_OBSERVABILITY_DSN" not in os.environ or not os.environ["CI_OBSERVABILITY_DSN"].strip():
        print("investigation: dsn-not-configured", file=sys.stderr)
        return 1
    client: Ledger | None = None
    try:
        client = Ledger(dsn=os.environ["CI_OBSERVABILITY_DSN"], schema=arguments.schema)
        report = query_execution(client=client, execution_id=arguments.execution_id)
        if arguments.section == "all":
            document = report
        else:
            document = {
                "schema": report["schema"],
                "version": report["version"],
                "source": report["source"],
                "coverage": report["coverage"],
                "section": arguments.section,
                "data": report[arguments.section],
            }
        payload = json.dumps(document, ensure_ascii=True, allow_nan=False)
        if len(payload.encode("utf-8")) > MAX_REPORT_BYTES:
            raise InvestigationError("execution-report-limit")
    except InvestigationError as error:
        print(f"investigation: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"investigation: {type(error).__name__}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()
    sys.stdout.write(payload + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
