"""Native adapter import tests through the real PostgreSQL ledger.

Real database only, selected by CI_OBSERVABILITY_TEST_DSN (the same
policy as the W01 ledger tests): no SQLite, no mocks. The tests create
their own ci_obs_w03_native_* schema and drop it afterwards; they never
touch the production schema, other users' schemas, or the shared
containers. When the variable is unset the real-database boundary is an
explicit skip, never a silently green claim.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from monitoring.ci_observability import ledger
from monitoring.ci_observability.contracts.validation import validate_record
from monitoring.ci_observability.ledger import ExecutionRecord, JournalEvent, Ledger
from monitoring.ci_observability.native.ginkgo import ginkgo_journal_records, parse_ginkgo_report
from monitoring.ci_observability.native.gotest import go_test_journal_records, parse_go_test_stream
from monitoring.ci_observability.native.junit import junit_journal_records, parse_junit_xml
from monitoring.ci_observability.native.records import InvocationContext
from monitoring.ci_observability.native.vitest import parse_vitest_report, vitest_journal_records

try:
    import psycopg
    from psycopg import sql
except ImportError:
    psycopg = None  # type: ignore[assignment]

if os.environ.get("CI_OBSERVABILITY_TEST_DSN", "").strip():
    if psycopg is None:
        pytest.fail("CI_OBSERVABILITY_TEST_DSN is set but psycopg is not installed", pytrace=False)
elif psycopg is None:
    pytest.skip("psycopg is not installed and CI_OBSERVABILITY_TEST_DSN is not set", allow_module_level=True)

TESTS_DIR = Path(__file__).resolve().parent
CONTRACT_NATIVE = TESTS_DIR.parent.parent / "ci_observability" / "contracts" / "v1" / "examples" / "native"
FIXTURES = TESTS_DIR.parent / "fixtures" / "native_adapters"
IT_CAPTURES_DIR = os.environ.get("CI_OBSERVABILITY_IT_CAPTURES_DIR", "").strip()

EXECUTION_ID = "w03-native-import-exec"
REPOSITORY_ID = 812345678
RUN_ID = 1122334600


def invocation(name: str, framework: str) -> InvocationContext:
    return InvocationContext(
        execution_id=EXECUTION_ID,
        journal_id=f"{name}-journal",
        invocation_id=f"{name}-invocation",
        framework=framework,
        tier="component-integration",
        component="fulfillment-service",
        variant=name,
        observed_at="2026-10-28T12:00:00Z",
        child_exit_code=None,
    )


def _validated_events(records: list[dict[str, object]]) -> list[JournalEvent]:
    events: list[JournalEvent] = []
    for record in records:
        validate_record(record=record)
        events.append(
            JournalEvent(
                schema_version=1,
                execution_id=str(record["execution_id"]),
                journal_id=str(record["journal_id"]),
                sequence=int(record["sequence"]),  # type: ignore[arg-type]
                recorded_at=str(record["recorded_at"]),
                event=str(record["event"]),
                data=dict(record["data"]),  # type: ignore[arg-type]
                source_at=record.get("source_at"),  # type: ignore[arg-type]
                invocation_id=record.get("invocation_id"),  # type: ignore[arg-type]
                mono_ns=None,
            )
        )
    return events


def _test_dsn() -> str:
    value = os.environ.get("CI_OBSERVABILITY_TEST_DSN", "").strip()
    if not value:
        pytest.skip("CI_OBSERVABILITY_TEST_DSN is not set: these tests require a real PostgreSQL server")
    return value


@pytest.fixture(scope="module")
def dsn() -> str:
    value = _test_dsn()
    try:
        with psycopg.connect(value, autocommit=True, connect_timeout=5):
            pass
    except psycopg.OperationalError as exc:
        pytest.fail(
            f"CI_OBSERVABILITY_TEST_DSN is set but the database is unreachable: {type(exc).__name__}", pytrace=False
        )
    return value


@pytest.fixture(scope="module")
def schema(dsn: str) -> Iterator[str]:
    name = f"ci_obs_w03_native_{os.getpid()}"
    assert ledger.apply_migrations(dsn=dsn, schema=name) == [1, 2, 3]
    yield name
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


@pytest.fixture(scope="module")
def client(dsn: str, schema: str) -> Iterator[Ledger]:
    client = Ledger(dsn=dsn, schema=schema)
    client.register_execution(
        record=ExecutionRecord(
            execution_id=EXECUTION_ID,
            schema_version=1,
            repository_id=REPOSITORY_ID,
            run_id=RUN_ID,
            run_attempt=1,
            logical_job_key="tests.yml#native-adapter-import",
            executed=True,
        )
    )
    yield client
    client.close()


EXCERPT_PHASES = ("service-only", "manager-capability", "manager-readiness", "bmaas-disabled")


def _fixture_invocations() -> dict[str, list[dict[str, object]]]:
    """Converted records for every shipped safe fixture, one invocation
    each under one job execution: native processes in one job never
    become new executions."""

    ginkgo_suite = parse_ginkgo_report(raw=(CONTRACT_NATIVE / "ginkgo.json").read_bytes())
    go_report = parse_go_test_stream(raw=(CONTRACT_NATIVE / "go-test.jsonl").read_bytes())
    junit_report = parse_junit_xml(raw=(CONTRACT_NATIVE / "pytest-junit.xml").read_bytes())
    vitest_report = parse_vitest_report(raw=(CONTRACT_NATIVE / "vitest.json").read_bytes(), source_root="/checkout")
    vitest_411_report = parse_vitest_report(
        raw=(FIXTURES / "vitest-4.1.11.sanitized.json").read_bytes(), source_root="/checkout"
    )
    invocations: dict[str, list[dict[str, object]]] = {
        "tiny-ginkgo": ginkgo_journal_records(
            context=invocation("tiny-ginkgo", "ginkgo"), suite=ginkgo_suite, suite_path="fixture/ginkgo"
        ).records,
        "tiny-gotest": go_test_journal_records(context=invocation("tiny-gotest", "go-test"), report=go_report).records,
        "tiny-junit": junit_journal_records(context=invocation("tiny-junit", "pytest"), report=junit_report).records,
        "tiny-vitest": vitest_journal_records(
            context=invocation("tiny-vitest", "vitest"), report=vitest_report
        ).records,
        "vitest-411": vitest_journal_records(
            context=invocation("vitest-411", "vitest"), report=vitest_411_report
        ).records,
    }
    for phase in EXCERPT_PHASES:
        raw = (FIXTURES / "it-phases" / f"ginkgo-{phase}.EXCERPT.json").read_bytes()
        suite = parse_ginkgo_report(raw=raw)
        invocations[f"it-{phase}"] = ginkgo_journal_records(
            context=invocation(f"it-{phase}", "ginkgo"), suite=suite, suite_path="sanitized/it/suite/path"
        ).records
    return invocations


def test_all_fixture_invocations_import_and_roundtrip(client: Ledger) -> None:
    invocations = _fixture_invocations()
    expected_total = 0
    journal_count = 0
    for name, records in invocations.items():
        # Every conversion ends with its terminal collection state.
        assert records[-1]["event"] == "collection_state", name
        events = _validated_events(records)
        result = client.append_events(events=events)
        assert result.appended == len(events), name
        assert result.duplicates == 0, name
        expected_total += len(events)
        journal_count += 1

        stored = client.list_events(execution_id=EXECUTION_ID, journal_id=f"{name}-journal", limit=500)
        assert [event.sequence for event in stored] == list(range(1, len(events) + 1)), name
        assert all(event.invocation_id == f"{name}-invocation" for event in stored), name

    every = client.list_events(execution_id=EXECUTION_ID, limit=500, offset=0)
    assert len(every) == expected_total
    counts = client.execution_counts(execution_id=EXECUTION_ID)
    assert counts == (expected_total, journal_count)


def test_gap_persistence_survives_the_real_roundtrip(client: Ledger) -> None:
    """An excerpt's missing catalog must not silently disappear in
    delivery: the terminal collection state carries the gap through the
    real ledger, and a gapless conversion stays clean."""

    excerpt = client.list_events(execution_id=EXECUTION_ID, journal_id="it-service-only-journal", limit=500)
    states = [event for event in excerpt if event.event == "collection_state"]
    assert len(states) == 1
    state = states[0].data
    assert state["status"] == "degraded"
    assert state["complete"] is False
    assert state["dropped_records"] == 623 - 5
    assert state["errors"] == [{"message": "ginkgo-catalog-incomplete"}]

    clean = client.list_events(execution_id=EXECUTION_ID, journal_id="tiny-ginkgo-journal", limit=100)
    clean_states = [event for event in clean if event.event == "collection_state"]
    assert len(clean_states) == 1
    clean_state = clean_states[0].data
    assert clean_state["status"] == "stopped"
    assert clean_state["complete"] is True
    assert clean_state["dropped_records"] == 0


def test_native_facts_survive_the_roundtrip_exactly(client: Ledger) -> None:
    stored = client.list_events(execution_id=EXECUTION_ID, journal_id="tiny-ginkgo-journal", limit=100)
    results = [event for event in stored if event.event == "test_result"]
    retried = next(event for event in results if event.data.get("attempt_count") == 2)
    # Ginkgo NumAttempts is retained directly, including across storage.
    assert retried.data["outcome"] == "failed"
    assert retried.data["duration_ns"] == 175555
    # Original timestamp text with nine fractional digits survives.
    assert retried.data["source_started_at"] == "2026-10-27T22:24:30.038963102Z"
    # No raw native text was journaled.
    assert "fails on purpose" not in str(results)

    junit = client.list_events(execution_id=EXECUTION_ID, journal_id="tiny-junit-journal", limit=100)
    junit_outcomes = sorted(str(event.data.get("outcome")) for event in junit if event.event == "test_result")
    assert junit_outcomes == ["expected_failure", "failed", "passed", "passed", "skipped"]

    vitest = client.list_events(execution_id=EXECUTION_ID, journal_id="tiny-vitest-journal", limit=100)
    vitest_results = [event for event in vitest if event.event == "test_result"]
    assert all(event.data.get("attempt_count") is None for event in vitest_results)

    # The actual 4.1.11 lockfile boundary roundtrips through the real
    # ledger with the same honest nulls.
    vitest_411 = client.list_events(execution_id=EXECUTION_ID, journal_id="vitest-411-journal", limit=100)
    vitest_411_results = [event for event in vitest_411 if event.event == "test_result"]
    # The regenerated 4.1.11 capture declares one duplicate display name:
    # four results, four distinct identities, all attempt counts null.
    assert len(vitest_411_results) == 4
    assert sorted(str(event.data.get("outcome")) for event in vitest_411_results) == [
        "failed",
        "passed",
        "passed",
        "skipped",
    ]
    assert all(event.data.get("attempt_count") is None for event in vitest_411_results)
    assert len({str(event.data.get("test_id")) for event in vitest_411_results}) == 4


def test_duplicate_delivery_is_idempotent(client: Ledger) -> None:
    events = _validated_events(_fixture_invocations()["tiny-vitest"])
    replay = client.append_events(events=events)
    assert replay.appended == 0
    assert replay.duplicates == len(events)


@pytest.mark.skipif(
    not IT_CAPTURES_DIR,
    reason="full private IT captures not configured: set CI_OBSERVABILITY_IT_CAPTURES_DIR "
    "(local only; CI runs the safe excerpt fixtures)",
)
def test_full_it_captures_compatibility_and_import(client: Ledger) -> None:
    """The four real full Fulfillment IT reports: complete catalogs,
    shared identities across phases, and a full ledger import."""

    captures = Path(IT_CAPTURES_DIR)
    phase_records: dict[str, list[dict[str, object]]] = {}
    phase_ids: dict[str, set[str]] = {}
    for phase in ("service-only", "manager-capability", "manager-readiness", "bmaas-disabled"):
        suite = parse_ginkgo_report(raw=(captures / f"{phase}.json").read_bytes())
        context = InvocationContext(
            execution_id=EXECUTION_ID,
            journal_id=f"full-{phase}-journal",
            invocation_id=f"full-{phase}-invocation",
            framework="ginkgo",
            tier="component-integration",
            component="fulfillment-service",
            variant=phase,
            observed_at="2026-10-28T12:00:00Z",
        )
        result = ginkgo_journal_records(context=context, suite=suite, suite_path="real/it/suite/path")
        assert not [gap for gap in result.gaps], phase
        records = result.records
        phase_records[phase] = records
        results = [record["data"] for record in records if record["event"] == "test_result"]
        assert len(results) == 623, phase
        # The terminal collection state is clean for a full report.
        states = [record["data"] for record in records if record["event"] == "collection_state"]
        assert states and states[0]["status"] == "stopped" and states[0]["complete"] is True, phase
        collected = [record["data"] for record in records if record["event"] == "collection_finished"]
        assert collected == [
            {"outcome": "completed", "collected_count": 623, "deselected_count": 2 if phase == "service-only" else 622}
        ], phase
        phase_ids[phase] = {str(data["test_id"]) for data in results}
        for record in records:
            validate_record(record=record)

    # The same 623 It definitions across all four phases.
    assert phase_ids["service-only"] == phase_ids["manager-capability"]
    assert phase_ids["service-only"] == phase_ids["manager-readiness"]
    assert phase_ids["service-only"] == phase_ids["bmaas-disabled"]

    service_results = [record["data"] for record in phase_records["service-only"] if record["event"] == "test_result"]
    runtime_skips = [data for data in service_results if data["attempt_count"] == 1 and data["outcome"] == "skipped"]
    assert len(runtime_skips) == 1
    filtered = [data for data in service_results if data["attempt_count"] == 0]
    assert len(filtered) == 2
    assert all(data["outcome"] == "skipped" and "source_ended_at" not in data for data in filtered)

    for phase, records in phase_records.items():
        events = _validated_events(records)
        assert len(events) == 1250, phase
        result = client.append_events(events=events)
        assert result.appended == len(events), phase
        stored: list[object] = []
        offset = 0
        while True:
            page = client.list_events(
                execution_id=EXECUTION_ID, journal_id=f"full-{phase}-journal", limit=500, offset=offset
            )
            stored.extend(page)
            if len(page) < 500:
                break
            offset += 500
        assert len(stored) == len(events), phase
        assert [event.sequence for event in stored] == list(range(1, len(events) + 1)), phase
