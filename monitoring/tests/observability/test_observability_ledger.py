"""Real-PostgreSQL tests for the CI observability ledger (W01a).

Every test runs against a real database server reached through
CI_OBSERVABILITY_TEST_DSN; there is no SQLite fallback and no mocked
connection. The tests create their own schema (and drop it at the end);
they never touch the production ci_observability schema. When the
variable is not set and no driver is installed the module skips clearly;
when the variable is set, a missing driver or an unreachable database
fails the run.

Run from the repository root (the monitoring test config adds the
repository root to pythonpath):

    CI_OBSERVABILITY_TEST_DSN=postgresql://... \\
        python -m pytest monitoring/tests/observability/test_observability_ledger.py --no-cov
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest

try:
    import psycopg
except ModuleNotFoundError:
    if os.environ.get("CI_OBSERVABILITY_TEST_DSN", "").strip():
        # A configured database with a missing driver is a broken setup.
        raise
    pytest.skip("psycopg is not installed and CI_OBSERVABILITY_TEST_DSN is not set", allow_module_level=True)

from psycopg import sql
from psycopg.conninfo import make_conninfo

from monitoring.ci_observability import ledger
from monitoring.ci_observability.contracts.validation import CONTRACTS_V1_DIR, validate_bundle
from monitoring.ci_observability.ledger import (
    AppendResult,
    ConflictError,
    ExecutionRecord,
    JournalEvent,
    LedgerError,
    NotFoundError,
    SequenceError,
    StoredEvent,
    ValidationError,
)

BASE_TIME = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)
UNREACHABLE_DSN = "postgresql://ci_observability@127.0.0.1:1/ci_observability?connect_timeout=2"


def make_execution(*, execution_id: str, **overrides: object) -> ExecutionRecord:
    values: dict[str, object] = {
        "execution_id": execution_id,
        "schema_version": 1,
        "repository_id": 1,
        "run_id": 100,
        "run_attempt": 1,
        "logical_job_key": "tests.yml#e2e",
        "executed": True,
        "github_job_id": None,
    }
    values.update(overrides)
    return ExecutionRecord(**values)  # type: ignore[arg-type]


def ts(*, offset_seconds: float) -> str:
    """Original UTC-Z timestamp text for a fixture time."""

    return ledger.format_utc(value=BASE_TIME + timedelta(seconds=offset_seconds))


def make_event(*, execution_id: str, journal_id: str, sequence: int, **overrides: object) -> JournalEvent:
    values: dict[str, object] = {
        "schema_version": 1,
        "execution_id": execution_id,
        "journal_id": journal_id,
        "sequence": sequence,
        "recorded_at": ts(offset_seconds=sequence),
        "event": "test_phase_started",
        "data": {"test_id": f"tests/e2e/caas/test_cluster.py::test_case_{sequence}"},
        "source_at": None,
        "invocation_id": None,
        "clock_id": None,
        "clock_uncertainty_ns": None,
        "mono_ns": None,
    }
    values.update(overrides)
    return JournalEvent(**values)  # type: ignore[arg-type]


def _test_dsn() -> str:
    value = os.environ.get("CI_OBSERVABILITY_TEST_DSN", "").strip()
    if not value:
        pytest.skip("CI_OBSERVABILITY_TEST_DSN is not set: these tests require a real PostgreSQL server")
    return value


@pytest.fixture(scope="session")
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


@pytest.fixture(scope="session")
def test_schema(dsn: str) -> Iterator[str]:
    name = f"ci_obs_w01a_ledger_{os.getpid()}"
    assert ledger.apply_migrations(dsn=dsn, schema=name) == [1, 2, 3]
    yield name
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


@pytest.fixture
def client(dsn: str, test_schema: str) -> Iterator[ledger.Ledger]:
    client = ledger.Ledger(dsn=dsn, schema=test_schema)
    yield client
    client.close()


def direct_count(*, dsn: str, schema: str, table: str, column: str, value: object) -> int:
    """Count rows through a second connection, so only committed data is visible."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        cursor = conn.execute(
            sql.SQL("SELECT count(*) FROM {}.{} WHERE {} = %s").format(
                sql.Identifier(schema), sql.Identifier(table), sql.Identifier(column)
            ),
            (value,),
        )
        return cursor.fetchone()[0]


def direct_prefix(*, dsn: str, schema: str, execution_id: str, journal_id: str) -> int:
    with psycopg.connect(dsn, autocommit=True) as conn:
        cursor = conn.execute(
            sql.SQL("SELECT prefix_sequence FROM {}.ledger_journal WHERE execution_id = %s AND journal_id = %s").format(
                sql.Identifier(schema)
            ),
            (execution_id, journal_id),
        )
        row = cursor.fetchone()
        return -1 if row is None else row[0]


def direct_conclusions(*, dsn: str, schema: str, execution_id: str) -> tuple[str | None, str | None]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        cursor = conn.execute(
            sql.SQL(
                "SELECT github_run_conclusion, github_job_conclusion FROM {}.ledger_execution WHERE execution_id = %s"
            ).format(sql.Identifier(schema)),
            (execution_id,),
        )
        row = cursor.fetchone()
        return (None, None) if row is None else (row[0], row[1])


def test_migrate_is_idempotent(dsn: str) -> None:
    name = f"ci_obs_w01a_idem_{os.getpid()}"
    try:
        assert ledger.apply_migrations(dsn=dsn, schema=name) == [1, 2, 3]
        assert ledger.apply_migrations(dsn=dsn, schema=name) == []
        with psycopg.connect(dsn, autocommit=True) as conn:
            cursor = conn.execute(sql.SQL("SELECT version FROM {}.schema_migrations").format(sql.Identifier(name)))
            assert cursor.fetchone() == (1,)
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


def test_migrate_rejects_unsafe_schema_name(dsn: str) -> None:
    with pytest.raises(ValueError, match="schema name"):
        ledger.apply_migrations(dsn=dsn, schema='evil"; DROP SCHEMA x')


def test_schema_owner_can_migrate_without_database_create_privilege(dsn: str) -> None:
    suffix = uuid.uuid4().hex[:10]
    schema = f"ci_obs_restricted_migration_{suffix}"
    role = f"ci_obs_migration_owner_{suffix}"
    try:
        with psycopg.connect(dsn, autocommit=True) as administrator:
            administrator.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
            administrator.execute(
                sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(sql.Identifier(schema), sql.Identifier(role))
            )
            allowed = administrator.execute(
                "SELECT has_database_privilege(%s, current_database(), 'CREATE')", (role,)
            ).fetchone()[0]
            assert allowed is False
        restricted_dsn = make_conninfo(dsn, user=role)
        assert ledger.apply_migrations(dsn=restricted_dsn, schema=schema) == [1, 2, 3]
        assert ledger.apply_migrations(dsn=restricted_dsn, schema=schema) == []
    finally:
        with psycopg.connect(dsn, autocommit=True) as administrator:
            administrator.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
            administrator.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def test_migrate_serializes_first_schema_creation(dsn: str) -> None:
    name = f"ci_obs_w01a_first_migration_{os.getpid()}"
    try:
        results = _run_concurrently(worker_count=2, body=lambda _: ledger.apply_migrations(dsn=dsn, schema=name))
        assert all(succeeded for succeeded, _ in results), results
        assert sorted(result for _, result in results) == [[], [1, 2, 3]]
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


def test_writes_commit_across_reads_and_are_visible_to_second_connections(dsn: str, test_schema: str) -> None:
    """Regression test for the durability bug the review reproduced.

    Reads used to open psycopg's implicit transaction, so a later write
    transaction was only a savepoint and its commit left the outer
    transaction open: a second connection saw nothing and the close
    rolled everything back. The client now runs in autocommit mode with
    one explicit transaction per write.
    """
    execution_id = "gh-1-r1300-a1-j1"
    writer = ledger.Ledger(dsn=dsn, schema=test_schema)
    try:
        writer.health_check()
        assert (
            writer.register_execution(record=make_execution(execution_id=execution_id, run_id=1300, github_job_id=1301))
            is True
        )
        assert (
            direct_count(
                dsn=dsn, schema=test_schema, table="ledger_execution", column="execution_id", value=execution_id
            )
            == 1
        )

        assert writer.get_execution(execution_id=execution_id) is not None  # read between writes
        assert writer.append_events(
            events=[
                make_event(execution_id=execution_id, journal_id="main", sequence=1),
                make_event(execution_id=execution_id, journal_id="main", sequence=2),
            ]
        ) == AppendResult(appended=2, duplicates=0)
        assert (
            direct_count(dsn=dsn, schema=test_schema, table="ledger_event", column="execution_id", value=execution_id)
            == 2
        )
        assert direct_prefix(dsn=dsn, schema=test_schema, execution_id=execution_id, journal_id="main") == 2

        writer.record_authoritative_conclusion(
            execution_id=execution_id, github_run_conclusion="failure", github_job_conclusion="failure"
        )
        assert direct_conclusions(dsn=dsn, schema=test_schema, execution_id=execution_id) == ("failure", "failure")

        assert len(writer.list_events(execution_id=execution_id, limit=10)) == 2  # read after the conclusion
        assert writer.append_events(
            events=[make_event(execution_id=execution_id, journal_id="main", sequence=3)]
        ) == AppendResult(appended=1, duplicates=0)  # append after the conclusion
        assert (
            direct_count(dsn=dsn, schema=test_schema, table="ledger_event", column="execution_id", value=execution_id)
            == 3
        )
    finally:
        writer.close()
    # After the writer closed, every commit is still there.
    assert (
        direct_count(dsn=dsn, schema=test_schema, table="ledger_execution", column="execution_id", value=execution_id)
        == 1
    )
    assert (
        direct_count(dsn=dsn, schema=test_schema, table="ledger_event", column="execution_id", value=execution_id) == 3
    )
    assert direct_prefix(dsn=dsn, schema=test_schema, execution_id=execution_id, journal_id="main") == 3
    assert direct_conclusions(dsn=dsn, schema=test_schema, execution_id=execution_id) == ("failure", "failure")


def test_reconnect_after_backend_termination_commits_visibly(dsn: str, test_schema: str) -> None:
    application_name = f"ci-observability-ledger-test-{os.getpid()}"
    writer = ledger.Ledger(dsn=dsn, schema=test_schema, application_name=application_name)
    try:
        execution_id = "gh-1-r1300-a2-j2"
        writer.register_execution(record=make_execution(execution_id=execution_id, run_id=1300))
        writer.append_events(events=[make_event(execution_id=execution_id, journal_id="main", sequence=1)])
        assert (
            direct_count(dsn=dsn, schema=test_schema, table="ledger_event", column="execution_id", value=execution_id)
            == 1
        )

        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE application_name = %s AND usename = current_user",
                (application_name,),
            )
        with psycopg.connect(dsn, autocommit=True) as conn:
            cursor = conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s AND usename = current_user",
                (application_name,),
            )
            assert cursor.fetchone() == (0,)

        # The next operations transparently reconnect, and what they commit
        # is visible to a second connection.
        assert writer.append_events(
            events=[make_event(execution_id=execution_id, journal_id="main", sequence=2)]
        ) == AppendResult(appended=1, duplicates=0)
        assert (
            direct_count(dsn=dsn, schema=test_schema, table="ledger_event", column="execution_id", value=execution_id)
            == 2
        )
        assert writer.register_execution(record=make_execution(execution_id="gh-1-r1300-a2-j3", run_id=1300)) is True
        assert (
            direct_count(
                dsn=dsn, schema=test_schema, table="ledger_execution", column="execution_id", value="gh-1-r1300-a2-j3"
            )
            == 1
        )
    finally:
        writer.close()


def test_persistent_database_failure_is_not_masked() -> None:
    writer = ledger.Ledger(dsn=UNREACHABLE_DSN, schema="ci_observability")
    try:
        with pytest.raises(psycopg.OperationalError):
            writer.register_execution(record=make_execution(execution_id="gh-1-r1300-a3-j4"))
        with pytest.raises(psycopg.OperationalError):
            writer.health_check()
    finally:
        writer.close()


def test_register_execution_insert_once_then_identical_replay_is_noop(client: ledger.Ledger) -> None:
    record = make_execution(execution_id="gh-1-r100-a1-j1", github_job_id=555)
    assert client.register_execution(record=record) is True
    assert client.register_execution(record=record) is False
    row = client.get_execution(execution_id="gh-1-r100-a1-j1")
    assert row is not None
    assert row.github_job_id == 555
    assert row.executed is True
    assert row.repository_id == 1
    assert row.run_id == 100
    assert row.run_attempt == 1
    assert row.logical_job_key == "tests.yml#e2e"


def test_register_execution_conflicting_identity_changes_nothing(client: ledger.Ledger) -> None:
    client.register_execution(record=make_execution(execution_id="gh-1-r100-a1-j2", run_id=100))
    with pytest.raises(ConflictError, match="different"):
        client.register_execution(record=make_execution(execution_id="gh-1-r100-a1-j2", run_id=101))
    with pytest.raises(ConflictError, match="different"):
        client.register_execution(
            record=make_execution(execution_id="gh-1-r100-a1-j2", logical_job_key="tests.yml#other")
        )
    with pytest.raises(ConflictError, match="different"):
        client.register_execution(record=make_execution(execution_id="gh-1-r100-a1-j2", repository_id=2))
    row = client.get_execution(execution_id="gh-1-r100-a1-j2")
    assert row is not None
    assert row.run_id == 100
    assert row.repository_id == 1
    assert row.logical_job_key == "tests.yml#e2e"


def test_register_execution_binds_unknown_job_id_once(client: ledger.Ledger) -> None:
    client.register_execution(record=make_execution(execution_id="gh-1-r100-a1-j3", github_job_id=None))
    # The binding is learned later by the trusted boundary: it binds once.
    assert client.register_execution(record=make_execution(execution_id="gh-1-r100-a1-j3", github_job_id=777)) is False
    # Re-delivering the bound id is a no-op.
    client.register_execution(record=make_execution(execution_id="gh-1-r100-a1-j3", github_job_id=777))
    # A different id for the same execution can never replace the binding.
    with pytest.raises(ConflictError, match="different"):
        client.register_execution(record=make_execution(execution_id="gh-1-r100-a1-j3", github_job_id=778))
    row = client.get_execution(execution_id="gh-1-r100-a1-j3")
    assert row is not None
    assert row.github_job_id == 777


def test_stale_unbound_replay_after_binding_does_not_conflict(client: ledger.Ledger) -> None:
    """An old producer claim that omits a newly learned job id stays valid."""
    client.register_execution(record=make_execution(execution_id="gh-1-r100-a1-j3b", github_job_id=None))
    client.register_execution(record=make_execution(execution_id="gh-1-r100-a1-j3b", github_job_id=909))
    # A replay of the original, still-unbound claim must not conflict.
    assert (
        client.register_execution(record=make_execution(execution_id="gh-1-r100-a1-j3b", github_job_id=None)) is False
    )
    row = client.get_execution(execution_id="gh-1-r100-a1-j3b")
    assert row is not None
    assert row.github_job_id == 909


def test_register_execution_job_id_claimed_by_one_executed_record(client: ledger.Ledger) -> None:
    client.register_execution(record=make_execution(execution_id="gh-1-r100-a1-j4", github_job_id=888))
    with pytest.raises(ConflictError, match="already claimed"):
        client.register_execution(record=make_execution(execution_id="gh-1-r100-a1-j5", github_job_id=888))
    assert client.get_execution(execution_id="gh-1-r100-a1-j5") is None


def test_repository_id_is_required_and_positive(dsn: str, test_schema: str, client: ledger.Ledger) -> None:
    with pytest.raises(ValidationError, match="repository_id"):
        make_execution(execution_id="gh-1-r100-a1-j4b", repository_id=0)
    with pytest.raises(ValidationError, match="repository_id"):
        make_execution(execution_id="gh-1-r100-a1-j4b", repository_id=None)
    # The column itself rejects a null repository: it is a required fact of
    # the manifest, not an unknown.
    with psycopg.connect(dsn, autocommit=True) as conn, pytest.raises(psycopg.errors.NotNullViolation):
        conn.execute(
            sql.SQL(
                "INSERT INTO {}.ledger_execution (execution_id, schema_version, repository_id, run_id,"
                " run_attempt, logical_job_key, executed)"
                " VALUES ('gh-1-r100-a1-j4c', 1, NULL, 1, 1, 'k', NULL)"
            ).format(sql.Identifier(test_schema))
        )


def test_unknown_execution_state_resolves_once(client: ledger.Ledger) -> None:
    """A queued or unstarted job is unknown, not executed, and resolves once."""
    queued = make_execution(execution_id="gh-1-r1400-a1-j1", run_id=1400, github_job_id=1401, executed=None)
    client.register_execution(record=queued)
    row = client.get_execution(execution_id="gh-1-r1400-a1-j1")
    assert row is not None
    assert row.executed is None
    # An unknown row claims no job id: another unknown row may reference it.
    client.register_execution(
        record=make_execution(execution_id="gh-1-r1400-a2-j1", run_id=1400, github_job_id=1401, executed=None)
    )

    # The trusted boundary later learns the job really executed.
    client.register_execution(
        record=make_execution(execution_id="gh-1-r1400-a1-j1", run_id=1400, github_job_id=1401, executed=True)
    )
    row = client.get_execution(execution_id="gh-1-r1400-a1-j1")
    assert row is not None
    assert row.executed is True

    # A stale replay of the original unknown claim omits the learned state.
    assert client.register_execution(record=queued) is False
    # The resolution is one-way.
    with pytest.raises(ConflictError, match="different"):
        client.register_execution(record=make_execution(execution_id="gh-1-r1400-a1-j1", run_id=1400, executed=False))
    # Now that the first row is executed, it claims the job id exclusively.
    with pytest.raises(ConflictError, match="already claimed"):
        client.register_execution(
            record=make_execution(execution_id="gh-1-r1400-a3-j1", run_id=1400, github_job_id=1401)
        )


def test_authoritative_conclusion_written_once_and_never_changed(client: ledger.Ledger) -> None:
    client.register_execution(record=make_execution(execution_id="gh-1-r100-a1-j6"))
    client.record_authoritative_conclusion(
        execution_id="gh-1-r100-a1-j6", github_run_conclusion="failure", github_job_conclusion="failure"
    )
    # Identical redelivery is a no-op.
    client.record_authoritative_conclusion(
        execution_id="gh-1-r100-a1-j6", github_run_conclusion="failure", github_job_conclusion="failure"
    )
    # A later pass cannot overwrite the recorded failure.
    with pytest.raises(ConflictError, match="already recorded"):
        client.record_authoritative_conclusion(
            execution_id="gh-1-r100-a1-j6", github_run_conclusion="success", github_job_conclusion="success"
        )
    # Filling only the still-unknown half is allowed.
    client.record_authoritative_conclusion(execution_id="gh-1-r100-a1-j6", github_job_conclusion="failure")
    row = client.get_execution(execution_id="gh-1-r100-a1-j6")
    assert row is not None
    assert row.github_run_conclusion == "failure"
    assert row.github_job_conclusion == "failure"


def test_authoritative_conclusion_requires_registration(client: ledger.Ledger) -> None:
    with pytest.raises(NotFoundError, match="not registered"):
        client.record_authoritative_conclusion(execution_id="gh-1-r100-a1-j7", github_run_conclusion="success")
    with pytest.raises(ValidationError, match="at least one conclusion"):
        client.record_authoritative_conclusion(execution_id="gh-1-r100-a1-j7")


def test_authoritative_conclusion_stays_separate_from_event_claims(client: ledger.Ledger) -> None:
    """Producer claims live in events; GitHub conclusions live in columns."""
    execution_id = "gh-1-r100-a1-j8"
    client.register_execution(record=make_execution(execution_id=execution_id))
    client.append_events(
        events=[
            make_event(execution_id=execution_id, journal_id="main", sequence=1, event="execution_started"),
            make_event(
                execution_id=execution_id,
                journal_id="main",
                sequence=2,
                event="execution_finished",
                data={"outcome": "passed", "exit_code": 0},
            ),
        ]
    )
    client.record_authoritative_conclusion(execution_id=execution_id, github_job_conclusion="failure")
    events = client.list_events(execution_id=execution_id)
    assert [event.event for event in events] == ["execution_started", "execution_finished"]
    assert events[1].data == {"outcome": "passed", "exit_code": 0}
    row = client.get_execution(execution_id=execution_id)
    assert row is not None
    assert row.github_job_conclusion == "failure"
    assert row.github_run_conclusion is None


def test_append_events_identical_replay_is_noop(client: ledger.Ledger) -> None:
    execution_id = "gh-1-r100-a1-j9"
    client.register_execution(record=make_execution(execution_id=execution_id))
    batch = [make_event(execution_id=execution_id, journal_id="main", sequence=seq) for seq in (1, 2, 3)]
    result = client.append_events(events=batch)
    assert result == AppendResult(appended=3, duplicates=0)
    replay = client.append_events(events=batch)
    assert replay == AppendResult(appended=0, duplicates=3)
    assert client.execution_counts(execution_id=execution_id) == (3, 1)


def test_append_events_conflicting_content_fails_without_partial_commit(
    dsn: str, test_schema: str, client: ledger.Ledger
) -> None:
    execution_id = "gh-1-r100-a1-j10"
    client.register_execution(record=make_execution(execution_id=execution_id))
    client.append_events(events=[make_event(execution_id=execution_id, journal_id="main", sequence=1)])
    conflicting = [
        make_event(execution_id=execution_id, journal_id="main", sequence=2),
        make_event(execution_id=execution_id, journal_id="main", sequence=1, data={"different": "content"}),
    ]
    with pytest.raises(ConflictError, match="different content at sequence 1"):
        client.append_events(events=conflicting)
    # Sequence 2 from the failed batch and no checkpoint advance were committed.
    assert client.execution_counts(execution_id=execution_id) == (1, 1)
    assert direct_prefix(dsn=dsn, schema=test_schema, execution_id=execution_id, journal_id="main") == 1


def test_append_events_partial_batch_appends_only_consistent_prefixes(
    dsn: str, test_schema: str, client: ledger.Ledger
) -> None:
    execution_id = "gh-1-r100-a1-j11"
    client.register_execution(record=make_execution(execution_id=execution_id))
    client.append_events(
        events=[make_event(execution_id=execution_id, journal_id="main", sequence=seq) for seq in (1, 2, 3)]
    )

    # A later partial batch continues the prefix.
    result = client.append_events(
        events=[
            make_event(execution_id=execution_id, journal_id="main", sequence=4),
            make_event(execution_id=execution_id, journal_id="main", sequence=5),
        ]
    )
    assert result == AppendResult(appended=2, duplicates=0)

    # A full replay of the journal over the stored prefix replays and extends.
    full = [make_event(execution_id=execution_id, journal_id="main", sequence=seq) for seq in range(1, 8)]
    assert client.append_events(events=full) == AppendResult(appended=2, duplicates=5)

    # A gap inside the batch is rejected, and nothing is committed.
    with pytest.raises(SequenceError, match="continue the prefix"):
        client.append_events(
            events=[
                make_event(execution_id=execution_id, journal_id="main", sequence=9),
                make_event(execution_id=execution_id, journal_id="main", sequence=11),
            ]
        )
    assert client.execution_counts(execution_id=execution_id) == (7, 1)
    assert direct_prefix(dsn=dsn, schema=test_schema, execution_id=execution_id, journal_id="main") == 7

    # A batch that jumps past the stored prefix is rejected.
    with pytest.raises(SequenceError, match="continue the prefix"):
        client.append_events(events=[make_event(execution_id=execution_id, journal_id="main", sequence=20)])
    assert client.execution_counts(execution_id=execution_id) == (7, 1)

    # A repeated sequence inside one batch is rejected before any insert.
    with pytest.raises(SequenceError, match="batch repeats sequence"):
        client.append_events(
            events=[
                make_event(execution_id=execution_id, journal_id="main", sequence=8),
                make_event(execution_id=execution_id, journal_id="main", sequence=8),
            ]
        )


def test_append_events_new_journal_must_start_at_sequence_one(client: ledger.Ledger) -> None:
    execution_id = "gh-1-r100-a1-j12"
    client.register_execution(record=make_execution(execution_id=execution_id))
    with pytest.raises(SequenceError, match="continue the prefix"):
        client.append_events(events=[make_event(execution_id=execution_id, journal_id="gw1", sequence=2)])
    assert client.execution_counts(execution_id=execution_id) == (0, 0)
    client.append_events(events=[make_event(execution_id=execution_id, journal_id="gw1", sequence=1)])
    assert client.execution_counts(execution_id=execution_id) == (1, 1)


def test_append_events_requires_registered_execution(client: ledger.Ledger) -> None:
    with pytest.raises(NotFoundError, match="unregistered execution"):
        client.append_events(events=[make_event(execution_id="gh-1-r100-a1-j13", journal_id="main", sequence=1)])


def test_append_events_accepts_unknown_event_names(client: ledger.Ledger) -> None:
    """The event vocabulary is contract-owned; storage must not gate it."""
    execution_id = "gh-1-r100-a1-j14"
    client.register_execution(record=make_execution(execution_id=execution_id))
    client.append_events(
        events=[
            make_event(
                execution_id=execution_id,
                journal_id="main",
                sequence=1,
                event="clock_skew_detected",
                data={"offset_ms": 12},
            ),
            make_event(
                execution_id=execution_id,
                journal_id="main",
                sequence=2,
                event="resource_pressure",
                data={"kind": "Pod", "metric": "cpu"},
            ),
            make_event(
                execution_id=execution_id,
                journal_id="main",
                sequence=3,
                event="fixture_scope_entered",
                data={"scope": "module"},
            ),
        ]
    )
    events = client.list_events(execution_id=execution_id)
    assert [event.event for event in events] == ["clock_skew_detected", "resource_pressure", "fixture_scope_entered"]


def test_append_events_bounds_batch_size_and_bytes(client: ledger.Ledger) -> None:
    execution_id = "gh-1-r100-a1-j16"
    client.register_execution(record=make_execution(execution_id=execution_id))
    too_many = [
        make_event(execution_id=execution_id, journal_id="main", sequence=seq)
        for seq in range(1, ledger.MAX_BATCH_EVENTS + 2)
    ]
    with pytest.raises(ValidationError, match="at most"):
        client.append_events(events=too_many)
    with pytest.raises(ValidationError, match="at least one event"):
        client.append_events(events=[])
    # Nineteen events of about 900 KiB each stay under the per-event bound
    # but exceed the 16 MiB batch bound together.
    heavy = [
        make_event(execution_id=execution_id, journal_id="main", sequence=seq, data={"blob": "x" * 900_000})
        for seq in range(1, 20)
    ]
    with pytest.raises(ValidationError, match="batch byte bound"):
        client.append_events(events=heavy)
    assert client.execution_counts(execution_id=execution_id) == (0, 0)


def test_many_incremental_batches_advance_the_checkpoint(dsn: str, test_schema: str, client: ledger.Ledger) -> None:
    execution_id = "gh-1-r1500-a1-j1"
    client.register_execution(record=make_execution(execution_id=execution_id, run_id=1500))
    for sequence in range(1, 51):
        result = client.append_events(
            events=[make_event(execution_id=execution_id, journal_id="main", sequence=sequence)]
        )
        assert result == AppendResult(appended=1, duplicates=0)
    assert client.execution_counts(execution_id=execution_id) == (50, 1)
    assert direct_prefix(dsn=dsn, schema=test_schema, execution_id=execution_id, journal_id="main") == 50
    full = [make_event(execution_id=execution_id, journal_id="main", sequence=sequence) for sequence in range(1, 51)]
    assert client.append_events(events=full) == AppendResult(appended=0, duplicates=50)


def _event_index_tuples_fetched(*, dsn: str, schema: str) -> int:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_stat_force_next_flush()")
        cursor = conn.execute(
            "SELECT idx_tup_fetch FROM pg_stat_user_tables WHERE schemaname = %s AND relname = 'ledger_event'",
            (schema,),
        )
        return cursor.fetchone()[0]


def test_one_event_append_to_a_long_journal_reads_bounded_rows(dsn: str, test_schema: str) -> None:
    """The append path must not read the journal's history.

    A pre-checkpoint implementation selected and re-scanned the whole
    journal per batch (quadratic over a live stream); the checkpoint
    keeps the verification read at the delivered keys, which
    pg_stat_user_tables.idx_tup_fetch measures directly.
    """
    execution_id = "gh-1-r1500-a2-j1"
    client = ledger.Ledger(dsn=dsn, schema=test_schema)
    try:
        client.register_execution(record=make_execution(execution_id=execution_id, run_id=1500))
        client.append_events(
            events=[make_event(execution_id=execution_id, journal_id="main", sequence=seq) for seq in range(1, 2001)]
        )
        before = _event_index_tuples_fetched(dsn=dsn, schema=test_schema)

        assert client.append_events(
            events=[make_event(execution_id=execution_id, journal_id="main", sequence=2001)]
        ) == AppendResult(appended=1, duplicates=0)
        after = _event_index_tuples_fetched(dsn=dsn, schema=test_schema)
    finally:
        client.close()
    # The whole-journal scan of the previous implementation would read
    # about 4000 index tuples here; only the delivered key is read now.
    assert after - before <= 64
    assert direct_prefix(dsn=dsn, schema=test_schema, execution_id=execution_id, journal_id="main") == 2001


def test_unknown_is_preserved_distinct_from_measured_zero(client: ledger.Ledger) -> None:
    execution_id = "gh-1-r100-a1-j17"
    client.register_execution(record=make_execution(execution_id=execution_id, github_job_id=None))
    client.append_events(
        events=[
            make_event(execution_id=execution_id, journal_id="main", sequence=1, mono_ns=0),
            make_event(execution_id=execution_id, journal_id="main", sequence=2, mono_ns=None),
            make_event(execution_id=execution_id, journal_id="main", sequence=3, source_at=ts(offset_seconds=90)),
            make_event(execution_id=execution_id, journal_id="main", sequence=4, data={"dropped_records": 0}),
            make_event(execution_id=execution_id, journal_id="main", sequence=5, data={"dropped_records": None}),
            make_event(execution_id=execution_id, journal_id="main", sequence=6, data={}),
        ]
    )
    events = client.list_events(execution_id=execution_id)
    by_sequence = {event.sequence: event for event in events}
    assert by_sequence[1].mono_ns == 0
    assert by_sequence[2].mono_ns is None
    assert by_sequence[3].source_at == ts(offset_seconds=90)
    assert by_sequence[4].source_at is None
    assert by_sequence[4].data == {"dropped_records": 0}
    assert by_sequence[5].data == {"dropped_records": None}
    assert by_sequence[6].data == {}
    row = client.get_execution(execution_id=execution_id)
    assert row is not None
    assert row.github_job_id is None  # unknown binding, not job id 0


def test_timestamp_text_is_preserved_verbatim(client: ledger.Ledger) -> None:
    """The original timestamp text is the evidence; up to nine fractional digits survive.

    The indexed timestamptz keeps microsecond precision for filters, but
    silently truncating the text would make an exact replay impossible.
    """
    execution_id = "gh-1-r100-a1-j17b"
    client.register_execution(record=make_execution(execution_id=execution_id))
    recorded = "2026-10-28T12:00:01.123456789Z"
    source = "2026-10-28T12:00:00.987654321Z"
    event = make_event(execution_id=execution_id, journal_id="main", sequence=1, recorded_at=recorded, source_at=source)
    client.append_events(events=[event])
    stored = client.list_events(execution_id=execution_id)[0]
    assert stored.recorded_at == recorded
    assert stored.source_at == source
    # Identical text replays; a different ninth digit is different evidence.
    assert client.append_events(events=[event]) == AppendResult(appended=0, duplicates=1)
    with pytest.raises(ConflictError, match="different content at sequence 1"):
        client.append_events(
            events=[
                make_event(
                    execution_id=execution_id,
                    journal_id="main",
                    sequence=1,
                    recorded_at="2026-10-28T12:00:01.123456788Z",
                )
            ]
        )
    for bad in (
        "2026-10-28T12:00:01+02:00",
        "2026-10-28 12:00:01Z",
        "2026-10-28T12:00:01.1234567891Z",
        "2026-13-01T00:00:00Z",
    ):
        with pytest.raises(ValidationError, match="timestamp"):
            make_event(execution_id=execution_id, journal_id="main", sequence=2, recorded_at=bad)


def test_envelope_fields_are_preserved_and_compared(client: ledger.Ledger) -> None:
    execution_id = "gh-1-r100-a1-j17c"
    client.register_execution(record=make_execution(execution_id=execution_id))
    event = make_event(
        execution_id=execution_id,
        journal_id="main",
        sequence=1,
        invocation_id="inv-42",
        clock_id="mono-a",
        clock_uncertainty_ns=0,
    )
    client.append_events(events=[event])
    stored = client.list_events(execution_id=execution_id)[0]
    assert stored.invocation_id == "inv-42"
    assert stored.clock_id == "mono-a"
    assert stored.clock_uncertainty_ns == 0
    assert client.append_events(events=[event]) == AppendResult(appended=0, duplicates=1)
    # Differing envelope fields are different content.
    for override in ({"invocation_id": "inv-43"}, {"clock_id": "mono-b"}, {"clock_uncertainty_ns": 5}):
        with pytest.raises(ConflictError, match="different content at sequence 1"):
            client.append_events(
                events=[make_event(execution_id=execution_id, journal_id="main", sequence=1, **override)]
            )
    # Not supplied stays distinct from a measured zero.
    client.append_events(events=[make_event(execution_id=execution_id, journal_id="main", sequence=2)])
    by_sequence = {event.sequence: event for event in client.list_events(execution_id=execution_id)}
    assert by_sequence[2].invocation_id is None
    assert by_sequence[2].clock_id is None
    assert by_sequence[2].clock_uncertainty_ns is None
    with pytest.raises(ValidationError, match="invocation_id"):
        make_event(execution_id=execution_id, journal_id="main", sequence=3, invocation_id="has space")
    with pytest.raises(ValidationError, match="clock_uncertainty_ns"):
        make_event(execution_id=execution_id, journal_id="main", sequence=3, clock_uncertainty_ns=-1)


def test_payload_roundtrip_preserves_exact_types_and_text(dsn: str, test_schema: str, client: ledger.Ledger) -> None:
    """The stored payload is the canonical serialization of the delivered one.

    jsonb normalizes numbers (1e23 becomes an integer, -0.0 becomes 0.0);
    the json column keeps the canonical text verbatim, so types, values,
    and the replay hash all match the delivered evidence.
    """
    execution_id = "gh-1-r100-a1-j18"
    client.register_execution(record=make_execution(execution_id=execution_id))
    payload = {
        "big_float": 1e23,
        "exp": 1.5e300,
        "negative_zero": -0.0,
        "small": 1e-7,
        "big_int": 10**25,
        "flag": True,
        "none": None,
        "nested": {"list": [1, 2.5, "s", True, None], "deep": {"x": [1e23]}},
    }
    event = make_event(execution_id=execution_id, journal_id="main", sequence=1, data=payload)
    assert client.append_events(events=[event]) == AppendResult(appended=1, duplicates=0)
    assert client.append_events(events=[event]) == AppendResult(appended=0, duplicates=1)
    stored = client.list_events(execution_id=execution_id)[0]
    assert stored.data == payload
    assert type(stored.data["big_float"]) is float
    assert type(stored.data["big_int"]) is int
    assert type(stored.data["nested"]["list"][1]) is float
    assert stored.data["negative_zero"] == 0.0
    assert str(stored.data["negative_zero"]) == "-0.0"
    # The stored text is the canonical serialization, byte for byte.
    with psycopg.connect(dsn, autocommit=True) as conn:
        stored_text = conn.execute(
            sql.SQL("SELECT data::text FROM {}.ledger_event WHERE execution_id = %s AND sequence = 1").format(
                sql.Identifier(test_schema)
            ),
            (execution_id,),
        ).fetchone()[0]
    assert stored_text == ledger.canonical_payload(data=payload)
    # Distinct JSON values stay conflicts even when the numbers are equal.
    client.append_events(
        events=[make_event(execution_id=execution_id, journal_id="main", sequence=2, data={"flag": True})]
    )
    with pytest.raises(ConflictError, match="different content at sequence 2"):
        client.append_events(
            events=[make_event(execution_id=execution_id, journal_id="main", sequence=2, data={"flag": 1})]
        )
    client.append_events(events=[make_event(execution_id=execution_id, journal_id="main", sequence=3, data={"n": 1})])
    with pytest.raises(ConflictError, match="different content at sequence 3"):
        client.append_events(
            events=[make_event(execution_id=execution_id, journal_id="main", sequence=3, data={"n": 1.0})]
        )


def test_payload_bound_agrees_between_canonical_text_and_storage(
    dsn: str, test_schema: str, client: ledger.Ledger
) -> None:
    """The one-megabyte event bound is the delivered canonical size, not a jsonb inflation of it."""
    execution_id = "gh-1-r100-a1-j18b"
    client.register_execution(record=make_execution(execution_id=execution_id))
    # 215 floats of 1.5e300: about 2 KiB canonical, which a jsonb text
    # bound would inflate to about 65 KiB and could reject.
    wide = {"values": [1.5e300] * 215}
    client.append_events(events=[make_event(execution_id=execution_id, journal_id="main", sequence=1, data=wide)])
    stored = client.list_events(execution_id=execution_id)[0]
    assert stored.data == wide
    assert all(type(value) is float for value in stored.data["values"])

    near_bound = {"blob": "x" * (ledger.MAX_EVENT_DATA_BYTES - 1000)}
    client.append_events(events=[make_event(execution_id=execution_id, journal_id="main", sequence=2, data=near_bound)])
    over_bound = {"blob": "x" * (ledger.MAX_EVENT_DATA_BYTES + 100)}
    with pytest.raises(ValidationError, match="payload bound"):
        client.append_events(
            events=[make_event(execution_id=execution_id, journal_id="main", sequence=3, data=over_bound)]
        )
    with psycopg.connect(dsn, autocommit=True) as conn:
        sizes = conn.execute(
            sql.SQL(
                "SELECT octet_length(data::text) FROM {}.ledger_event WHERE execution_id = %s ORDER BY sequence"
            ).format(sql.Identifier(test_schema)),
            (execution_id,),
        ).fetchall()
    assert sizes[0][0] == len(ledger.canonical_payload(data=wide).encode("utf-8"))
    assert sizes[1][0] <= ledger.MAX_EVENT_DATA_BYTES
    assert client.execution_counts(execution_id=execution_id) == (2, 1)


def test_non_json_payloads_are_rejected_before_any_write(dsn: str, test_schema: str, client: ledger.Ledger) -> None:
    execution_id = "gh-1-r100-a1-j19"
    client.register_execution(record=make_execution(execution_id=execution_id))
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    rejects: list[dict[str, object]] = [
        {"bad": float("nan")},
        {"bad": float("inf")},
        {"bad": object()},
        {"bad": (1, 2)},
        {"nested": {"bad": object()}},
        {"nested": {1: "int key"}},
        cyclic,
    ]
    for data in rejects:
        with pytest.raises(ValidationError, match="event data"):
            client.append_events(
                events=[make_event(execution_id=execution_id, journal_id="main", sequence=1, data=data)]
            )
    assert (
        direct_count(dsn=dsn, schema=test_schema, table="ledger_event", column="execution_id", value=execution_id) == 0
    )
    assert client.execution_counts(execution_id=execution_id) == (0, 0)


def test_two_attempts_keep_failed_first_attempt_intact(client: ledger.Ledger) -> None:
    first = make_execution(execution_id="gh-1-r1100-a1-j19", run_id=1100, github_job_id=601)
    second = make_execution(execution_id="gh-1-r1100-a2-j19", run_id=1100, github_job_id=602, run_attempt=2)
    client.register_execution(record=first)
    client.register_execution(record=second)
    client.record_authoritative_conclusion(
        execution_id=first.execution_id, github_run_conclusion="failure", github_job_conclusion="failure"
    )
    client.record_authoritative_conclusion(
        execution_id=second.execution_id, github_run_conclusion="success", github_job_conclusion="success"
    )
    # The later pass cannot rewrite the first attempt's failure...
    with pytest.raises(ConflictError, match="already recorded"):
        client.record_authoritative_conclusion(execution_id=first.execution_id, github_job_conclusion="success")
    # ...and both attempts stay queryable.
    rows = client.list_executions(run_id=1100, logical_job_key="tests.yml#e2e")
    assert [row.execution_id for row in rows] == ["gh-1-r1100-a1-j19", "gh-1-r1100-a2-j19"]
    by_id = {row.execution_id: row for row in rows}
    assert by_id["gh-1-r1100-a1-j19"].github_job_conclusion == "failure"
    assert by_id["gh-1-r1100-a2-j19"].github_job_conclusion == "success"
    # A retry is a new record, never an overwrite of the old one.
    assert client.register_execution(record=first) is False


def test_not_executed_records_do_not_count_as_executions(client: ledger.Ledger) -> None:
    """A job that did not run in this attempt references the attempt that ran."""
    client.register_execution(record=make_execution(execution_id="gh-1-r1200-a1-j20", run_id=1200, github_job_id=701))
    client.register_execution(
        record=make_execution(
            execution_id="gh-1-r1200-a2-j20", run_id=1200, github_job_id=701, run_attempt=2, executed=False
        )
    )
    # Another attempt may reference the same executed job id again.
    client.register_execution(
        record=make_execution(
            execution_id="gh-1-r1200-a3-j20", run_id=1200, github_job_id=701, run_attempt=3, executed=False
        )
    )
    not_executed = client.list_executions(run_id=1200, executed=False)
    assert {row.execution_id for row in not_executed} == {"gh-1-r1200-a2-j20", "gh-1-r1200-a3-j20"}
    executed_rows = client.list_executions(run_id=1200, executed=True)
    assert [row.execution_id for row in executed_rows] == ["gh-1-r1200-a1-j20"]
    # The executed claim on the job id is still exclusive to the real execution.
    with pytest.raises(ConflictError, match="already claimed"):
        client.register_execution(
            record=make_execution(execution_id="gh-1-r1200-a4-j20", run_id=1200, github_job_id=701)
        )
    # Run summaries count executions and registrations separately.
    run_1200 = next(run for run in client.list_runs(repository_id=1, limit=ledger.MAX_LIST_ROWS) if run.run_id == 1200)
    assert run_1200.execution_count == 1
    assert run_1200.registration_count == 3


def test_history_is_immutable_at_the_storage_layer(dsn: str, test_schema: str, client: ledger.Ledger) -> None:
    execution_id = "gh-1-r100-a1-j21"
    client.register_execution(record=make_execution(execution_id=execution_id))
    client.append_events(events=[make_event(execution_id=execution_id, journal_id="main", sequence=1)])
    # Each statement runs in its own transaction: one rejection must not
    # abort the checks that follow.
    with psycopg.connect(dsn, autocommit=True) as conn:
        table = sql.Identifier(test_schema)
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute(sql.SQL("UPDATE {}.ledger_event SET event = 'tampered'").format(table))
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute(sql.SQL("DELETE FROM {}.ledger_event").format(table))
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute(sql.SQL("DELETE FROM {}.ledger_execution").format(table))
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute(sql.SQL("UPDATE {}.ledger_execution SET run_id = 999").format(table))
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute(sql.SQL("UPDATE {}.ledger_execution SET github_job_conclusion = 'success'").format(table))
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute(sql.SQL("UPDATE {}.ledger_journal SET prefix_sequence = 0").format(table))
    events = client.list_events(execution_id=execution_id)
    assert [event.event for event in events] == ["test_phase_started"]


def test_late_events_and_late_journals_are_accepted(client: ledger.Ledger) -> None:
    execution_id = "gh-1-r100-a1-j22"
    client.register_execution(record=make_execution(execution_id=execution_id))
    client.append_events(
        events=[
            make_event(execution_id=execution_id, journal_id="main", sequence=1, recorded_at=ts(offset_seconds=10)),
            make_event(execution_id=execution_id, journal_id="main", sequence=2, recorded_at=ts(offset_seconds=20)),
        ]
    )
    client.record_authoritative_conclusion(execution_id=execution_id, github_job_conclusion="success")

    # A straggler journal is delivered after the conclusion was recorded,
    # and its wall-clock times precede the main journal's.
    late = [
        make_event(
            execution_id=execution_id,
            journal_id="controller",
            sequence=1,
            recorded_at=ts(offset_seconds=5),
            source_at=ts(offset_seconds=4),
        ),
        make_event(
            execution_id=execution_id,
            journal_id="controller",
            sequence=2,
            recorded_at=ts(offset_seconds=15),
            source_at=ts(offset_seconds=14),
        ),
    ]
    assert client.append_events(events=late) == AppendResult(appended=2, duplicates=0)

    events = client.list_events(execution_id=execution_id)
    assert [(event.journal_id, event.sequence) for event in events] == [
        ("controller", 1),
        ("main", 1),
        ("controller", 2),
        ("main", 2),
    ]
    # A late continuation of the main journal still extends its prefix.
    client.append_events(
        events=[make_event(execution_id=execution_id, journal_id="main", sequence=3, recorded_at=ts(offset_seconds=30))]
    )
    assert client.execution_counts(execution_id=execution_id) == (5, 2)


def _run_concurrently(*, worker_count: int, body: Callable[[int], object]) -> list[tuple[bool, object]]:
    barrier = threading.Barrier(worker_count)
    results: list[tuple[bool, object]] = [(False, None)] * worker_count

    def run(index: int) -> None:
        barrier.wait()
        try:
            results[index] = (True, body(index))
        except BaseException as exc:
            results[index] = (False, exc)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(worker_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


def test_concurrent_identical_append_is_a_noop_for_the_loser(dsn: str, test_schema: str) -> None:
    execution_id = "gh-1-r100-a1-j23"
    ledger.Ledger(dsn=dsn, schema=test_schema).register_execution(record=make_execution(execution_id=execution_id))
    batch = [make_event(execution_id=execution_id, journal_id="main", sequence=seq) for seq in range(1, 21)]

    clients = [ledger.Ledger(dsn=dsn, schema=test_schema) for _ in range(2)]
    try:
        results = _run_concurrently(worker_count=2, body=lambda index: clients[index].append_events(events=batch))
    finally:
        for client in clients:
            client.close()
    assert all(result[0] for result in results), [result[1] for result in results]
    appended = [result[1].appended for result in results]  # type: ignore[union-attr]
    assert sorted(appended) == [0, 20]
    assert (
        direct_count(dsn=dsn, schema=test_schema, table="ledger_event", column="execution_id", value=execution_id) == 20
    )
    assert direct_prefix(dsn=dsn, schema=test_schema, execution_id=execution_id, journal_id="main") == 20


def test_concurrent_conflicting_append_leaves_one_winner(dsn: str, test_schema: str) -> None:
    execution_id = "gh-1-r100-a1-j24"
    ledger.Ledger(dsn=dsn, schema=test_schema).register_execution(record=make_execution(execution_id=execution_id))
    batches = [
        [make_event(execution_id=execution_id, journal_id="main", sequence=1, data={"worker": "a"})],
        [make_event(execution_id=execution_id, journal_id="main", sequence=1, data={"worker": "b"})],
    ]
    clients = [ledger.Ledger(dsn=dsn, schema=test_schema) for _ in range(2)]
    try:
        results = _run_concurrently(
            worker_count=2, body=lambda index: clients[index].append_events(events=batches[index])
        )
    finally:
        for client in clients:
            client.close()
    outcomes = sorted("ok" if result[0] else type(result[1]).__name__ for result in results)
    assert outcomes == ["ConflictError", "ok"], results
    assert (
        direct_count(dsn=dsn, schema=test_schema, table="ledger_event", column="execution_id", value=execution_id) == 1
    )
    check = ledger.Ledger(dsn=dsn, schema=test_schema)
    try:
        events = check.list_events(execution_id=execution_id)
        assert events[0].data["worker"] in ("a", "b")
    finally:
        check.close()


def test_concurrent_registration_conflict(dsn: str, test_schema: str) -> None:
    clients = [ledger.Ledger(dsn=dsn, schema=test_schema) for _ in range(2)]
    records = [
        make_execution(execution_id="gh-1-r100-a1-j25", run_id=100),
        make_execution(execution_id="gh-1-r100-a1-j25", run_id=101),
    ]
    try:
        results = _run_concurrently(
            worker_count=2, body=lambda index: clients[index].register_execution(record=records[index])
        )
    finally:
        for client in clients:
            client.close()
    outcomes = sorted("ok" if result[0] else type(result[1]).__name__ for result in results)
    assert outcomes == ["ConflictError", "ok"], results
    check = ledger.Ledger(dsn=dsn, schema=test_schema)
    try:
        row = check.get_execution(execution_id="gh-1-r100-a1-j25")
        assert row is not None
        assert row.run_id in (100, 101)
    finally:
        check.close()


def test_concurrent_binding_of_an_unbound_execution_returns_typed_conflict(
    dsn: str, test_schema: str, client: ledger.Ledger
) -> None:
    execution_id = "concurrent-binding"
    client.register_execution(record=make_execution(execution_id=execution_id, executed=None))
    names = [f"{test_schema}_bind_a", f"{test_schema}_bind_b"]
    writers = [ledger.Ledger(dsn=dsn, schema=test_schema, application_name=name) for name in names]
    outcomes: list[str | None] = [None, None]

    def bind(index: int) -> None:
        try:
            writers[index].register_execution(
                record=make_execution(execution_id=execution_id, executed=True, github_job_id=990001 + index)
            )
            outcomes[index] = "ok"
        except Exception as error:
            outcomes[index] = type(error).__name__

    threads = [threading.Thread(target=bind, args=(index,)) for index in (0, 1)]
    try:
        with psycopg.connect(dsn) as blocker:
            blocker.execute(
                sql.SQL("SELECT 1 FROM {}.ledger_execution WHERE execution_id = %s FOR UPDATE").format(
                    sql.Identifier(test_schema)
                ),
                (execution_id,),
            )
            for thread in threads:
                thread.start()
            with psycopg.connect(dsn, autocommit=True) as observer:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    waiting = observer.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE application_name = ANY(%s)"
                        " AND wait_event_type = 'Lock'",
                        (names,),
                    ).fetchone()[0]
                    if waiting == 2:
                        break
                    time.sleep(0.02)
                assert waiting == 2
            blocker.commit()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()
        assert sorted(outcomes) == ["ConflictError", "ok"]
        row = client.get_execution(execution_id=execution_id)
        assert row is not None
        assert row.executed is True
        assert row.github_job_id in (990001, 990002)
    finally:
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=10)
        for writer in writers:
            writer.close()


def test_health_check(dsn: str, test_schema: str) -> None:
    client = ledger.Ledger(dsn=dsn, schema=test_schema)
    try:
        client.health_check()
    finally:
        client.close()
    broken = ledger.Ledger(dsn=dsn, schema="ci_obs_w01a_missing")
    try:
        with pytest.raises(LedgerError, match="not migrated"):
            broken.health_check()
    finally:
        broken.close()


def _seed_listing_data(*, client: ledger.Ledger) -> None:
    client.register_execution(record=make_execution(execution_id="gh-1-r300-a1-j1", run_id=300, github_job_id=801))
    client.register_execution(
        record=make_execution(execution_id="gh-1-r300-a2-j1", run_id=300, run_attempt=2, github_job_id=802)
    )
    client.register_execution(
        record=make_execution(execution_id="gh-1-r300-a2-j1-inh", run_id=300, run_attempt=2, executed=False)
    )
    client.register_execution(
        record=make_execution(execution_id="gh-2-r400-a1-j1", repository_id=2, run_id=400, github_job_id=803)
    )


def test_list_executions_filters_and_order(client: ledger.Ledger) -> None:
    _seed_listing_data(client=client)
    assert [row.execution_id for row in client.list_executions(run_id=300)] == [
        "gh-1-r300-a1-j1",
        "gh-1-r300-a2-j1",
        "gh-1-r300-a2-j1-inh",
    ]
    assert [row.execution_id for row in client.list_executions(run_id=300, run_attempt=2)] == [
        "gh-1-r300-a2-j1",
        "gh-1-r300-a2-j1-inh",
    ]
    assert [row.execution_id for row in client.list_executions(github_job_id=802)] == ["gh-1-r300-a2-j1"]
    assert [row.execution_id for row in client.list_executions(run_id=300, executed=False)] == ["gh-1-r300-a2-j1-inh"]
    assert [row.execution_id for row in client.list_executions(repository_id=2)] == ["gh-2-r400-a1-j1"]
    assert [row.execution_id for row in client.list_executions(logical_job_key="tests.yml#e2e", run_id=400)] == [
        "gh-2-r400-a1-j1"
    ]


def test_list_executions_time_window_pagination_and_offset_cap(client: ledger.Ledger) -> None:
    _seed_listing_data(client=client)
    registered = client.get_execution(execution_id="gh-1-r300-a1-j1")
    assert registered is not None
    after = registered.registered_at
    assert client.list_executions(run_id=300, registered_after=after + timedelta(seconds=1)) == []
    rows = client.list_executions(run_id=300, registered_before=after + timedelta(seconds=1))
    assert len(rows) == 3
    assert len(client.list_executions(repository_id=2, registered_before=after + timedelta(seconds=1))) == 1

    page_one = client.list_executions(run_id=300, limit=2, offset=0)
    page_two = client.list_executions(run_id=300, limit=2, offset=2)
    assert len(page_one) == 2
    assert len(page_two) == 1
    assert [row.execution_id for row in page_one + page_two] == [
        row.execution_id for row in client.list_executions(run_id=300, limit=10)
    ]
    with pytest.raises(ValidationError, match="limit"):
        client.list_executions(limit=0)
    with pytest.raises(ValidationError, match="limit"):
        client.list_executions(limit=ledger.MAX_LIST_ROWS + 1)
    with pytest.raises(ValidationError, match="offset"):
        client.list_executions(offset=-1)
    with pytest.raises(ValidationError, match="offset"):
        client.list_executions(offset=ledger.MAX_OFFSET + 1)
    assert client.list_executions(run_id=300, limit=1, offset=ledger.MAX_OFFSET) == []


def test_list_runs_separates_executions_from_registrations(client: ledger.Ledger) -> None:
    _seed_listing_data(client=client)
    repo_two = client.list_runs(repository_id=2)
    assert [(run.repository_id, run.run_id) for run in repo_two] == [(2, 400)]
    assert repo_two[0].run_attempts == (1,)
    assert repo_two[0].execution_count == 1
    assert repo_two[0].registration_count == 1
    all_runs = client.list_runs(repository_id=1, limit=ledger.MAX_LIST_ROWS)
    run_300 = next(run for run in all_runs if run.run_id == 300)
    assert run_300.run_attempts == (1, 2)
    assert run_300.execution_count == 2
    assert run_300.registration_count == 3


def test_list_events_filters_timeline_and_pagination(client: ledger.Ledger) -> None:
    execution_id = "gh-1-r500-a1-j1"
    client.register_execution(record=make_execution(execution_id=execution_id, run_id=500))
    events = [
        make_event(execution_id=execution_id, journal_id="gw0", sequence=seq, recorded_at=ts(offset_seconds=seq))
        for seq in range(1, 8)
    ]
    events += [
        make_event(execution_id=execution_id, journal_id="gw1", sequence=seq, recorded_at=ts(offset_seconds=seq + 100))
        for seq in range(1, 6)
    ]
    client.append_events(events=events)

    all_events = client.list_events(execution_id=execution_id, limit=100)
    assert [(event.journal_id, event.sequence) for event in all_events] == [("gw0", seq) for seq in range(1, 8)] + [
        ("gw1", seq) for seq in range(1, 6)
    ]
    only_gw0 = client.list_events(execution_id=execution_id, journal_id="gw0")
    assert [event.sequence for event in only_gw0] == list(range(1, 8))
    window = client.list_events(
        execution_id=execution_id,
        recorded_after=BASE_TIME + timedelta(seconds=3),
        recorded_before=BASE_TIME + timedelta(seconds=6),
    )
    assert [event.sequence for event in window] == [3, 4, 5, 6]
    page = client.list_events(execution_id=execution_id, limit=5, offset=5)
    assert [event.sequence for event in page] == [6, 7, 1, 2, 3]
    assert [event.journal_id for event in page] == ["gw0", "gw0", "gw1", "gw1", "gw1"]
    with pytest.raises(ValidationError, match="journal_id"):
        client.list_events(execution_id=execution_id, journal_id="bad journal id")
    with pytest.raises(ValidationError, match="recorded_after"):
        client.list_events(execution_id=execution_id, recorded_after=BASE_TIME.replace(tzinfo=None))


def test_filter_values_never_reach_sql_as_code(client: ledger.Ledger) -> None:
    _seed_listing_data(client=client)
    with pytest.raises(ValidationError, match="logical_job_key"):
        client.list_executions(logical_job_key="'; DROP TABLE ledger_event; --")
    # Pattern-legal characters that still look like SQL: parameterized, no match.
    assert client.list_executions(logical_job_key="x=1--") == []
    assert client.get_execution(execution_id="gh-1-r300-a1-j1") is not None
    assert [row.execution_id for row in client.list_executions(run_id=300)] == [
        "gh-1-r300-a1-j1",
        "gh-1-r300-a2-j1",
        "gh-1-r300-a2-j1-inh",
    ]


def test_record_construction_rejects_malformed_input() -> None:
    with pytest.raises(ValidationError, match="execution_id"):
        make_execution(execution_id="has space")
    with pytest.raises(ValidationError, match="execution_id"):
        make_execution(execution_id="")
    with pytest.raises(ValidationError, match="repository_id"):
        make_execution(execution_id="gh-1-r100-a1-j27", repository_id=0)
    with pytest.raises(ValidationError, match="run_id"):
        make_execution(execution_id="gh-1-r100-a1-j27", run_id=0)
    with pytest.raises(ValidationError, match="logical_job_key"):
        make_execution(execution_id="gh-1-r100-a1-j27", logical_job_key="job key with spaces")
    with pytest.raises(ValidationError, match="executed"):
        make_execution(execution_id="gh-1-r100-a1-j27", executed="yes")
    with pytest.raises(ValidationError, match="timestamp"):
        make_event(execution_id="gh-1-r100-a1-j27", journal_id="main", sequence=1, recorded_at="2026-10-28T12:00:00")
    with pytest.raises(ValidationError, match="sequence"):
        make_event(execution_id="gh-1-r100-a1-j27", journal_id="main", sequence=0)
    with pytest.raises(ValidationError, match="mono_ns"):
        make_event(execution_id="gh-1-r100-a1-j27", journal_id="main", sequence=1, mono_ns=-1)
    with pytest.raises(ValidationError, match="event"):
        make_event(execution_id="gh-1-r100-a1-j27", journal_id="main", sequence=1, event="")
    with pytest.raises(ValidationError, match="data"):
        make_event(execution_id="gh-1-r100-a1-j27", journal_id="main", sequence=1, data={1: "int key"})


def test_validation_error_messages_carry_no_values(client: ledger.Ledger) -> None:
    """Malformed input must not be echoed into error text."""
    with pytest.raises(ValidationError) as excinfo:
        make_execution(execution_id="x" * 500)
    assert "x" * 500 not in str(excinfo.value)

    with pytest.raises(ValidationError) as excinfo:
        ledger.parse_utc_text(value="not a timestamp at all")
    assert "not a timestamp at all" not in str(excinfo.value)

    execution_id = "gh-1-r100-a1-j29"
    client.register_execution(record=make_execution(execution_id=execution_id))
    with pytest.raises(ValidationError) as excinfo:
        client.append_events(
            events=[
                make_event(
                    execution_id=execution_id, journal_id="main", sequence=1, data={"secret-looking": float("nan")}
                )
            ]
        )
    assert str(excinfo.value) == "event data contains NaN or Infinity, which is not JSON"


def test_stored_event_carries_ingested_at(client: ledger.Ledger) -> None:
    execution_id = "gh-1-r100-a1-j28"
    client.register_execution(record=make_execution(execution_id=execution_id))
    before = datetime.now(tz=UTC) - timedelta(seconds=5)
    client.append_events(events=[make_event(execution_id=execution_id, journal_id="main", sequence=1)])
    events = client.list_events(execution_id=execution_id)
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, StoredEvent)
    assert before <= event.ingested_at <= datetime.now(tz=UTC) + timedelta(seconds=5)


def test_frozen_contract_examples_roundtrip_through_real_postgres(client: ledger.Ledger) -> None:
    for manifest_path in sorted((CONTRACTS_V1_DIR / "examples").glob("*/bundle-manifest.json")):
        validate_bundle(bundle_dir=manifest_path.parent)
        manifest = json.loads(manifest_path.read_text())
        execution = manifest["execution"]
        client.register_execution(
            record=ExecutionRecord(
                execution_id=execution["execution_id"],
                schema_version=manifest["schema_version"],
                repository_id=execution["repository_id"],
                run_id=execution["run_id"],
                run_attempt=execution["run_attempt"],
                logical_job_key=execution["job"]["logical_job_key"],
                executed=None,
            )
        )
        events: list[JournalEvent] = []
        for entry in manifest["files"]:
            if entry["kind"] != "journal":
                continue
            for line in (manifest_path.parent / entry["path"]).read_text().splitlines():
                record = json.loads(line)
                events.append(
                    JournalEvent(
                        schema_version=record["schema_version"],
                        execution_id=record["execution_id"],
                        journal_id=record["journal_id"],
                        sequence=record["sequence"],
                        recorded_at=record["recorded_at"],
                        source_at=record.get("source_at"),
                        mono_ns=record["mono_ns"],
                        invocation_id=record.get("invocation_id"),
                        clock_id=record.get("clock_id"),
                        clock_uncertainty_ns=record.get("clock_uncertainty_ns"),
                        event=record["event"],
                        data=record["data"],
                    )
                )
        assert client.append_events(events=events).appended == len(events)
        stored = client.list_events(execution_id=execution["execution_id"], limit=ledger.MAX_LIST_ROWS)
        by_identity = {(event.journal_id, event.sequence): event for event in stored}
        assert len(stored) == len(events)
        for event in events:
            actual = by_identity[(event.journal_id, event.sequence)]
            assert actual.recorded_at == event.recorded_at
            assert actual.source_at == event.source_at
            assert actual.invocation_id == event.invocation_id
            assert actual.clock_id == event.clock_id
            assert actual.clock_uncertainty_ns == event.clock_uncertainty_ns
            assert actual.mono_ns == event.mono_ns
            assert actual.data == event.data
        assert client.append_events(events=events).duplicates == len(events)
        row = client.get_execution(execution_id=execution["execution_id"])
        assert row is not None
        assert row.github_job_id is None
        assert row.executed is None
