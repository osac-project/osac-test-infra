from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from monitoring.ci_observability import investigation
from monitoring.ci_observability.contracts.validation import ContractError, validate_record
from monitoring.ci_observability.investigation import InvestigationError, build_report, query_execution
from monitoring.ci_observability.ledger import (
    ExecutionRecord,
    ExecutionRow,
    JournalEvent,
    JSONValue,
    Ledger,
    StoredEvent,
    apply_migrations,
)

EXAMPLES = Path(__file__).resolve().parents[2] / "ci_observability/contracts/v1/examples"
REPO_ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 28, tzinfo=UTC)


def example(*, name: str) -> tuple[ExecutionRow, list[StoredEvent]]:
    directory = EXAMPLES / name
    manifest = json.loads((directory / "bundle-manifest.json").read_bytes())
    identity = manifest["execution"]
    execution = ExecutionRow(
        execution_id=identity["execution_id"],
        schema_version=1,
        repository_id=identity["repository_id"],
        run_id=identity["run_id"],
        run_attempt=identity["run_attempt"],
        logical_job_key=identity["job"]["logical_job_key"],
        github_job_id=identity["job"]["github_job_id"],
        executed=True,
        github_run_conclusion="failure",
        github_job_conclusion="failure",
        registered_at=NOW,
    )
    events: list[StoredEvent] = []
    for entry in manifest["files"]:
        if entry["kind"] != "journal":
            continue
        for line in (directory / entry["path"]).read_bytes().splitlines():
            record = json.loads(line)
            del record["schema"]
            events.append(StoredEvent(**record, ingested_at=NOW))
    return execution, events


@pytest.mark.parametrize(
    "name", ["serial", "parallel", "partial", "rerun", "campaign", "unit-ginkgo", "unit-build-fail"]
)
def test_reference_bundles_produce_structured_reports(*, name: str) -> None:
    execution, events = example(name=name)
    report = build_report(execution=execution, events=events)
    assert report["source"]["execution_id"] == execution.execution_id
    assert report["summary"]["test_count"] == len(report["tests"])
    assert report["coverage"]["event_count"] == len(events)
    assert report["summary"]["github_job_conclusion"] == "failure"
    json.dumps(report, allow_nan=False)


def test_phase_failure_and_cleanup_have_separate_evidence() -> None:
    execution, events = example(name="parallel")
    report = build_report(execution=execution, events=events)
    failure = next(row for row in report["attempts"] if row["outcome"] == "failed")
    assert failure["first_failure"]["signature"].startswith("AssertionError")
    assert failure["cleanup_failure"] is None
    call = next(event for event in events if event.event == "test_phase_finished" and event.data["outcome"] == "failed")
    changed: list[StoredEvent] = []
    for event in events:
        if (
            event.event == "test_phase_finished"
            and event.data["phase"] == "teardown"
            and event.data["test_id"] == call.data["test_id"]
        ):
            data = dict(event.data)
            data["outcome"] = "error"
            data["failure"] = {"signature": "CleanupError"}
            changed.append(replace(event, data=data))
        else:
            changed.append(event)
    failed = next(
        row for row in build_report(execution=execution, events=changed)["attempts"] if row["outcome"] == "failed"
    )
    assert failed["first_failure"] == failure["first_failure"]
    assert failed["cleanup_failure"] == {"signature": "CleanupError"}


def test_work_and_wall_time_remain_separate() -> None:
    execution, events = example(name="parallel")
    report = build_report(execution=execution, events=events)
    summary = report["summary"]
    assert summary["duration_ns"] == 2_583_950_000_000
    assert summary["duration_minutes"] == summary["duration_ns"] / 60_000_000_000
    assert summary["measured_fixture_work_ns"] == sum(row["duration_ns"] for row in report["fixtures"])
    shared = next(row for row in report["fixtures"] if row["fixture_instance_id"] == "fx-default-subnet-gw0")
    assert len(shared["used_by"]) == 2
    assert shared["duration_ns"] == 571_001_332
    assert summary["worker_count"] == 2


def test_missing_terminals_are_unknown_with_no_fabricated_end() -> None:
    execution, events = example(name="partial")
    report = build_report(execution=execution, events=events)
    assert report["coverage"]["complete"] is False
    assert report["summary"]["duration_ns"] is None
    assert report["tests"][0]["outcome"] == "unknown"
    unfinished = [row for row in report["phases"] if not row["finish_observed"]]
    assert len(unfinished) == 1
    assert unfinished[0]["duration_ns"] is None
    assert unfinished[0]["ended_at"] is None
    assert report["coverage"]["unfinished_interval_count"] >= 2


def test_native_aggregate_does_not_invent_retry_intervals() -> None:
    execution, events = example(name="unit-ginkgo")
    events = [event for event in events if event.event == "test_result" and event.data["result_scope"] == "aggregate"]
    report = build_report(execution=execution, events=events)
    retried = next(row for row in report["tests"] if row["attempt_count"] == 2)
    assert retried["duration_ns"] == 175_555
    assert not [
        row
        for row in report["attempts"]
        if row["test_id"] == retried["test_id"] and row["invocation_id"] == retried["invocation_id"]
    ]
    assert report["phases"] == []


def test_resource_and_wait_identity_are_retained() -> None:
    execution, events = example(name="serial")
    report = build_report(execution=execution, events=events)
    operation = report["operations"][0]
    resource = report["resources"][0]
    assert operation["resource"]["uid"] == resource["resource"]["uid"]
    assert operation["test"]["test_id"].startswith("tests/e2e/")
    assert operation["duration_ns"] == 945_003_000_000
    assert resource["status"] == "Provisioning"


def test_measured_zero_and_missing_clock_stay_distinct() -> None:
    execution, events = example(name="serial")
    changed = []
    for event in events:
        if event.event == "operation_finished":
            data = dict(event.data)
            data["duration_ns"] = 0
            changed.append(replace(event, data=data, clock_id=None, mono_ns=None))
        else:
            changed.append(replace(event, clock_id=None, mono_ns=None))
    report = build_report(execution=execution, events=changed)
    assert report["operations"][0]["duration_ns"] == 0
    assert report["operations"][0]["duration_minutes"] == 0
    assert report["summary"]["duration_ns"] is None
    assert all(row["duration_ns"] is None for row in report["phases"])


def test_wall_clock_order_does_not_break_same_process_pairing() -> None:
    execution, events = example(name="serial")
    changed = [
        replace(event, recorded_at="2026-10-26T00:00:00Z") if event.event == "operation_finished" else event
        for event in events
    ]
    changed.reverse()
    report = build_report(execution=execution, events=changed)
    assert report["operations"][0]["duration_ns"] == 945_003_000_000
    assert report["operations"][0]["finish_observed"] is True


def test_duplicate_and_cross_execution_events_fail_explicitly() -> None:
    execution, events = example(name="serial")
    with pytest.raises(InvestigationError, match="duplicate-event"):
        build_report(execution=execution, events=[*events, events[0]])
    with pytest.raises(InvestigationError, match="execution-event-mismatch"):
        build_report(execution=execution, events=[replace(events[0], execution_id="different")])


def test_missing_invocation_is_not_replaced_by_a_synthetic_identity() -> None:
    execution, events = example(name="serial")
    report = build_report(execution=execution, events=events)
    assert all(row["invocation_id"] is None for row in report["tests"])
    assert all(row["result_observed"] is None for row in report["selection"])


def test_deselected_tests_are_kept_outside_result_counts() -> None:
    execution, events = example(name="parallel")
    report = build_report(execution=execution, events=events)
    assert report["summary"]["collected_test_count"] == 7
    assert report["summary"]["deselected_test_count"] == 1
    assert len(report["selection"]) == 7
    assert not [row for row in report["tests"] if "test_migration" in row["test_id"]]


def test_explicit_nanoseconds_do_not_require_matching_clocks() -> None:
    execution, events = example(name="parallel")
    changed = [replace(event, clock_id=None, mono_ns=None) for event in events]
    report = build_report(execution=execution, events=changed)
    shared = next(row for row in report["fixtures"] if row["fixture_instance_id"] == "fx-default-subnet-gw0")
    assert shared["duration_ns"] == 571_001_332
    assert report["summary"]["duration_ns"] is None


def test_absent_source_health_is_unknown_and_measured_health_can_be_complete() -> None:
    execution, events = example(name="serial")
    events = [event for event in events if event.event != "test_phase_started" and event.event != "test_phase_finished"]
    report = build_report(execution=execution, events=events)
    assert report["coverage"]["complete"] is None
    state = replace(
        events[-1],
        journal_id="health",
        sequence=1,
        event="collection_state",
        data={"source": "test-journal", "status": "stopped", "complete": True, "dropped_records": 0},
    )
    report = build_report(execution=execution, events=[*events, state])
    assert report["coverage"]["complete"] is True
    degraded = replace(state, data={**state.data, "complete": False, "status": "degraded"})
    assert build_report(execution=execution, events=[*events, degraded])["coverage"]["complete"] is False


def test_missing_call_or_teardown_cannot_be_a_passing_attempt() -> None:
    execution, events = example(name="parallel")
    report = build_report(execution=execution, events=events)
    missing = [
        row
        for row in report["attempts"]
        if "test_external_ip_allocation" in row["test_id"] or "test_subnet_lifecycle" in row["test_id"]
    ]
    assert len(missing) == 2
    assert all(row["outcome"] == "unknown" and row["duration_ns"] is None for row in missing)
    assert report["coverage"]["complete"] is False
    assert report["coverage"]["incomplete_attempt_count"] >= 2


def test_attempt_scope_requires_an_index_and_preserves_unknown_index() -> None:
    execution, events = example(name="serial")
    data: dict[str, JSONValue] = {
        "result_scope": "attempt",
        "test_id": "a-native-test",
        "outcome": "passed",
        "attempt_index": None,
    }
    event = replace(events[0], event="test_result", data=data)
    report = build_report(execution=execution, events=[event])
    assert report["tests"][0]["attempt_count"] is None
    assert report["tests"][0]["outcome"] == "unknown"
    record = {
        "schema": "osac.ci-observability.journal-record",
        "schema_version": 1,
        "execution_id": execution.execution_id,
        "journal_id": "main",
        "sequence": 1,
        "recorded_at": "2026-10-28T00:00:00Z",
        "mono_ns": None,
        "event": "test_result",
        "data": dict(data),
    }
    del record["data"]["attempt_index"]
    with pytest.raises(ContractError):
        validate_record(record=record)


def test_event_limit_is_explicit() -> None:
    execution, events = example(name="serial")
    with pytest.raises(InvestigationError, match="execution-event-limit"):
        build_report(execution=execution, events=[events[0]] * (investigation.MAX_EVENTS + 1))


def test_cli_without_a_dsn_has_a_safe_diagnostic() -> None:
    environment = dict(os.environ)
    environment.pop("CI_OBSERVABILITY_DSN", None)
    result = subprocess.run(
        [sys.executable, "-m", "monitoring.ci_observability.investigation", "--execution-id", "missing"],
        env=environment,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.strip() == "investigation: dsn-not-configured"


@pytest.fixture
def database() -> Iterator[tuple[Ledger, Ledger, str]]:
    if "CI_OBSERVABILITY_TEST_DSN" not in os.environ:
        pytest.skip("CI_OBSERVABILITY_TEST_DSN is not configured")
    dsn = os.environ["CI_OBSERVABILITY_TEST_DSN"]
    suffix = uuid.uuid4().hex[:12]
    schema = f"ci_investigation_{suffix}"
    reader_name = f"ci_investigation_reader_{suffix}"
    password = uuid.uuid4().hex
    apply_migrations(dsn=dsn, schema=schema)
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(reader_name), sql.Literal(password))
        )
        connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(reader_name))
        )
        for table in ("ledger_execution", "ledger_event", "ledger_journal"):
            connection.execute(
                sql.SQL("GRANT SELECT ON {}.{} TO {}").format(
                    sql.Identifier(schema), sql.Identifier(table), sql.Identifier(reader_name)
                )
            )
    writer = Ledger(dsn=dsn, schema=schema)
    reader_dsn = make_conninfo(dsn, user=reader_name, password=password)
    reader = Ledger(dsn=reader_dsn, schema=schema)
    try:
        yield writer, reader, reader_dsn
    finally:
        writer.close()
        reader.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(reader_name)))


def test_real_reader_queries_all_cursor_pages(*, database: tuple[Ledger, Ledger, str]) -> None:
    writer, reader, _reader_dsn = database
    execution, original = example(name="serial")
    writer.register_execution(
        record=ExecutionRecord(
            execution_id=execution.execution_id,
            schema_version=1,
            repository_id=execution.repository_id,
            run_id=execution.run_id,
            run_attempt=execution.run_attempt,
            logical_job_key=execution.logical_job_key,
            github_job_id=execution.github_job_id,
            executed=True,
        )
    )
    events = []
    for index in range(1201):
        data: dict[str, JSONValue] = {
            "result_scope": "aggregate",
            "test_id": f"test-{index}",
            "outcome": "passed",
            "attempt_count": None,
            "duration_ns": index,
        }
        events.append(
            JournalEvent(
                schema_version=1,
                execution_id=execution.execution_id,
                journal_id="native",
                sequence=index + 1,
                recorded_at=original[0].recorded_at,
                event="test_result",
                data=data,
            )
        )
    writer.append_events(events=events)
    report = query_execution(client=reader, execution_id=execution.execution_id)
    assert report["summary"]["test_count"] == 1201
    assert report["coverage"]["stable_read"] is True
    assert report["coverage"]["event_count"] == 1201
    assert len(report["tests"]) == 1201
    assert all(row["attempt_count"] is None for row in report["tests"])
    assert report["summary"]["github_job_conclusion"] is None


def test_real_reader_missing_execution_is_explicit(*, database: tuple[Ledger, Ledger, str]) -> None:
    _writer, reader, _reader_dsn = database
    with pytest.raises(InvestigationError, match="execution-not-found"):
        query_execution(client=reader, execution_id="absent")


def test_real_reader_cannot_write(*, database: tuple[Ledger, Ledger, str]) -> None:
    _writer, reader, _reader_dsn = database
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        reader.register_execution(
            record=ExecutionRecord(
                execution_id="blocked",
                schema_version=1,
                repository_id=1,
                run_id=1,
                run_attempt=1,
                logical_job_key="tests#blocked",
                executed=True,
            )
        )


def test_cli_preserves_safe_missing_execution_code(*, database: tuple[Ledger, Ledger, str]) -> None:
    _writer, reader, reader_dsn = database
    environment = dict(os.environ)
    environment["CI_OBSERVABILITY_DSN"] = reader_dsn
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "monitoring.ci_observability.investigation",
            "--execution-id",
            "absent",
            "--schema",
            reader.schema,
        ],
        env=environment,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.strip() == "investigation: execution-not-found"
    assert reader_dsn not in result.stderr


def test_query_and_cli_enforce_byte_limits_with_real_storage(
    *, database: tuple[Ledger, Ledger, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    writer, reader, reader_dsn = database
    execution, events = example(name="serial")
    writer.register_execution(
        record=ExecutionRecord(
            execution_id=execution.execution_id,
            schema_version=1,
            repository_id=execution.repository_id,
            run_id=execution.run_id,
            run_attempt=execution.run_attempt,
            logical_job_key=execution.logical_job_key,
            github_job_id=execution.github_job_id,
            executed=True,
        )
    )
    event = events[0]
    writer.append_events(
        events=[
            JournalEvent(
                schema_version=1,
                execution_id=event.execution_id,
                journal_id=event.journal_id,
                sequence=event.sequence,
                recorded_at=event.recorded_at,
                event=event.event,
                data=event.data,
            )
        ]
    )
    monkeypatch.setenv("CI_OBSERVABILITY_DSN", reader_dsn)
    arguments = ["--execution-id", execution.execution_id, "--schema", reader.schema]
    assert investigation.main(argv=arguments) == 0
    result = capsys.readouterr()
    assert json.loads(result.out)["section"] == "summary"
    assert result.err == ""
    monkeypatch.setattr(investigation, "MAX_REPORT_BYTES", 8)
    assert investigation.main(argv=arguments) == 1
    result = capsys.readouterr()
    assert result.out == ""
    assert result.err.strip() == "investigation: execution-report-limit"
    monkeypatch.setattr(investigation, "MAX_SOURCE_BYTES", 8)
    with pytest.raises(InvestigationError, match="execution-source-limit"):
        query_execution(client=reader, execution_id=execution.execution_id)
