"""DEV PostgreSQL and HTTP checks for the public environment projection.

The trusted fixture proof exercises publication and reader permissions. It
does not exercise W06 sanitization or establish a real CI execution.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import pytest

try:
    import psycopg
except ModuleNotFoundError:
    if os.environ.get("CI_OBSERVABILITY_TEST_DSN", "").strip():
        raise
    pytest.skip("psycopg is not installed and CI_OBSERVABILITY_TEST_DSN is not set", allow_module_level=True)

from contract_helpers import MANIFEST_NAME, load_json, rewrite_journal, set_manifest_field
from psycopg import sql
from psycopg.conninfo import make_conninfo
from test_observability_sources import dsn as dsn
from test_observability_sources import full_proof, job_record, read_journal_records, reidentify_bundle, run_record

from monitoring.ci_observability import api, ledger
from monitoring.ci_observability.bundle_reader import FrozenBundle, read_bundle_directory
from monitoring.ci_observability.ingest import HostSourceContext, Ingestor, SourceConflictError
from monitoring.ci_observability.ledger import JSONValue
from monitoring.ci_observability.reconcile import GitHubEvidence

EXECUTION_ID = "environment-execution"
JOB_ID = 4588887777
RUN_ID = 1122334455
PUBLIC_COLUMNS = {
    "receipt_id",
    "execution_id",
    "manifest_sha256",
    "repository_id",
    "run_id",
    "run_attempt",
    "github_job_id",
    "source_created_at",
    "first_seen_at",
    "claimed_workflow_path",
    "claimed_workflow_revision",
    "claimed_event",
    "claimed_cohort",
    "claimed_suite",
    "claimed_sources",
    "claimed_image_digests",
    "claimed_cluster_snapshot",
    "claimed_config_hash",
    "claimed_complete",
    "claimed_end_reason",
    "claimed_dropped_records",
    "tested_sha",
    "revision_verdict",
    "revision_reason",
    "revision_provenance",
}


@dataclass(frozen=True, kw_only=True)
class Database:
    dsn: str
    schema: str
    reader_dsn: str


@pytest.fixture
def database(*, dsn: str) -> Iterator[Database]:
    suffix = uuid.uuid4().hex[:12]
    schema = f"ci_obs_environment_{suffix}"
    reader = f"ci_obs_environment_reader_{suffix}"
    try:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(reader)))
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(reader))
            )
            connection.execute(
                sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT ON TABLES TO {}").format(
                    sql.Identifier(schema), sql.Identifier(reader)
                )
            )
        ledger.apply_migrations(dsn=dsn, schema=schema)
        yield Database(dsn=dsn, schema=schema, reader_dsn=make_conninfo(dsn, user=reader))
    finally:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
            connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(reader)))


@pytest.fixture
def ingestor(*, database: Database, tmp_path: Path) -> Iterator[Ingestor]:
    client = Ingestor(dsn=database.dsn, schema=database.schema, quarantine_root=tmp_path / "quarantine")
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def bundle_directory(*, tmp_path: Path) -> Path:
    directory = reidentify_bundle(destination=tmp_path / "bundle", execution_id=EXECUTION_ID)
    set_manifest_field(bundle_dir=directory, keys=("execution", "event"), value="workflow_dispatch")
    set_manifest_field(bundle_dir=directory, keys=("execution", "cohort"), value="manual")
    return directory


@pytest.fixture
def bundle(*, bundle_directory: Path) -> FrozenBundle:
    return read_bundle_directory(bundle_dir=bundle_directory)


@pytest.fixture
def evidence() -> GitHubEvidence:
    return GitHubEvidence(
        run=run_record(run_id=RUN_ID, run_attempt=1, event="workflow_dispatch"),
        jobs=(job_record(job_id=JOB_ID, run_id=RUN_ID, run_attempt=1, name="e2e-bmaas"),),
        expected_job_id=JOB_ID,
        tests_run_head=True,
    )


def metadata_rows(*, database: Database) -> list[dict[str, JSONValue]]:
    client = ledger.Ledger(dsn=database.reader_dsn, schema=database.schema)
    try:
        return client.list_environments(execution_id=EXECUTION_ID)
    finally:
        client.close()


def http_get(*, url: str) -> tuple[int, dict[str, JSONValue]]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_view_has_exact_public_columns_and_security_barrier(*, database: Database) -> None:
    with psycopg.connect(database.dsn, autocommit=True) as connection:
        columns = connection.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s",
            (database.schema, "ledger_environment"),
        ).fetchall()
        options = connection.execute(
            "SELECT reloptions FROM pg_class WHERE oid = to_regclass(%s)", (f"{database.schema}.ledger_environment",)
        ).fetchone()
    assert {column[0] for column in columns} == PUBLIC_COLUMNS
    assert options is not None
    assert "security_barrier=true" in options[0]
    assert "security_invoker=true" not in options[0]


@pytest.mark.parametrize(("has_github", "has_proof"), [(False, False), (True, False), (False, True)])
def test_quarantined_receipt_exposes_no_environment(
    *,
    database: Database,
    ingestor: Ingestor,
    bundle: FrozenBundle,
    evidence: GitHubEvidence,
    has_github: bool,
    has_proof: bool,
) -> None:
    result = ingestor.ingest_bundle(
        bundle=bundle,
        source=HostSourceContext(origin="private-source-canary"),
        github=evidence if has_github else None,
        redaction_attestation=full_proof(bundle=bundle) if has_proof else None,
    )
    assert result.status == "quarantined"
    assert metadata_rows(database=database) == []
    client = ledger.Ledger(dsn=database.reader_dsn, schema=database.schema)
    try:
        assert client.get_execution(execution_id=EXECUTION_ID) is None
    finally:
        client.close()


def test_promotion_and_duplicate_delivery_preserve_claimed_and_verified_revisions(
    *, database: Database, ingestor: Ingestor, bundle: FrozenBundle, evidence: GitHubEvidence
) -> None:
    source = HostSourceContext(origin="private-source-canary")
    first = ingestor.ingest_bundle(bundle=bundle, source=source, github=evidence)
    assert first.status == "quarantined"
    assert metadata_rows(database=database) == []
    published = ingestor.ingest_bundle(
        bundle=bundle, source=source, github=evidence, redaction_attestation=full_proof(bundle=bundle)
    )
    repeated = ingestor.ingest_bundle(
        bundle=bundle, source=source, github=evidence, redaction_attestation=full_proof(bundle=bundle)
    )
    assert published.status == repeated.status == "committed"
    assert first.receipt_id == published.receipt_id == repeated.receipt_id
    rows = metadata_rows(database=database)
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == PUBLIC_COLUMNS
    assert row["receipt_id"] == first.receipt_id
    assert row["github_job_id"] == JOB_ID
    assert row["repository_id"] == 998877665
    assert row["manifest_sha256"] == bundle.manifest_sha256
    assert row["source_created_at"] == bundle.manifest["created_at"]
    assert row["tested_sha"] == "a" * 40
    assert row["revision_verdict"] == "verified"
    assert row["claimed_sources"] == bundle.manifest["environment"]["sources"]
    assert row["claimed_sources"][0]["revision"] != row["tested_sha"]
    assert row["claimed_workflow_revision"] == bundle.manifest["execution"]["workflow"]["revision"]
    assert row["claimed_suite"]["selection"]["worker_count"] == 0
    assert row["claimed_dropped_records"] == 0
    assert row["claimed_complete"] is True
    assert "private-source-canary" not in json.dumps(row)


@pytest.mark.parametrize("conflicting_job", [False, True])
def test_rejected_replay_cannot_replace_established_revision(
    *, database: Database, ingestor: Ingestor, bundle: FrozenBundle, evidence: GitHubEvidence, conflicting_job: bool
) -> None:
    source = HostSourceContext(origin="environment-replay")
    ingestor.ingest_bundle(
        bundle=bundle, source=source, github=evidence, redaction_attestation=full_proof(bundle=bundle)
    )
    before = metadata_rows(database=database)
    new_job = JOB_ID + 1 if conflicting_job else JOB_ID
    replay = GitHubEvidence(
        run=run_record(run_id=RUN_ID, run_attempt=1, event="workflow_dispatch", head_sha="b" * 40),
        jobs=(job_record(job_id=new_job, run_id=RUN_ID, run_attempt=1, name="e2e-bmaas"),),
        expected_job_id=new_job if conflicting_job else None,
        tests_run_head=True,
    )
    if conflicting_job:
        with pytest.raises(SourceConflictError, match="execution-bound-to-different-job"):
            ingestor.ingest_bundle(bundle=bundle, source=source, github=replay)
    else:
        result = ingestor.ingest_bundle(bundle=bundle, source=source, github=replay)
        assert result.status == "quarantined"
    with psycopg.connect(database.dsn, autocommit=True) as connection:
        observation = connection.execute(
            sql.SQL(
                "SELECT numeric_job_id, publication_authorized, tested_sha FROM {}.source_github_observation"
                " ORDER BY observation_id DESC LIMIT 1"
            ).format(sql.Identifier(database.schema))
        ).fetchone()
    assert observation is not None
    assert observation[1] is conflicting_job
    assert observation[2] == "b" * 40
    assert metadata_rows(database=database) == before


def test_same_job_evidence_can_resolve_unknown_revision(
    *, database: Database, ingestor: Ingestor, bundle: FrozenBundle, evidence: GitHubEvidence
) -> None:
    source = HostSourceContext(origin="environment-resolution")
    ingestor.ingest_bundle(
        bundle=bundle,
        source=source,
        github=replace(evidence, tests_run_head=False),
        redaction_attestation=full_proof(bundle=bundle),
    )
    assert metadata_rows(database=database)[0]["revision_verdict"] == "claimed"
    ingestor.ingest_bundle(bundle=bundle, source=source, github=evidence)
    rows = metadata_rows(database=database)
    assert len(rows) == 1
    assert rows[0]["revision_verdict"] == "verified"
    assert rows[0]["tested_sha"] == "a" * 40


@pytest.mark.parametrize("missing_timing", ["job-start", "attempt-start"])
def test_published_unknown_execution_timing_keeps_environment_visible(
    *, database: Database, ingestor: Ingestor, bundle_directory: Path, missing_timing: str
) -> None:
    attempt = 2 if missing_timing == "attempt-start" else 1
    set_manifest_field(bundle_dir=bundle_directory, keys=("execution", "run_attempt"), value=attempt)
    run = run_record(run_id=RUN_ID, run_attempt=attempt, event="workflow_dispatch")
    job = job_record(job_id=JOB_ID, run_id=RUN_ID, run_attempt=attempt, name="e2e-bmaas")
    if missing_timing == "job-start":
        job["started_at"] = None
    else:
        run["run_started_at"] = None
    changed = read_bundle_directory(bundle_dir=bundle_directory)
    result = ingestor.ingest_bundle(
        bundle=changed,
        source=HostSourceContext(origin="environment-unknown-timing"),
        github=GitHubEvidence(run=run, jobs=(job,), expected_job_id=JOB_ID, tests_run_head=True),
        redaction_attestation=full_proof(bundle=changed),
    )
    assert result.status == "committed"
    client = ledger.Ledger(dsn=database.reader_dsn, schema=database.schema)
    try:
        execution = client.get_execution(execution_id=EXECUTION_ID)
        assert execution is not None
        assert execution.executed is None
    finally:
        client.close()
    rows = metadata_rows(database=database)
    assert len(rows) == 1
    assert rows[0]["receipt_id"] == result.receipt_id
    assert rows[0]["github_job_id"] == JOB_ID
    assert rows[0]["run_attempt"] == attempt


def test_partial_longer_and_stale_receipts_keep_their_own_completeness(
    *,
    database: Database,
    ingestor: Ingestor,
    bundle: FrozenBundle,
    evidence: GitHubEvidence,
    bundle_directory: Path,
    tmp_path: Path,
) -> None:
    partial = tmp_path / "partial"
    shutil.copytree(bundle_directory, partial)
    set_manifest_field(bundle_dir=partial, keys=("completeness", "complete"), value=False)
    set_manifest_field(bundle_dir=partial, keys=("completeness", "end_reason"), value="interrupted")
    set_manifest_field(bundle_dir=partial, keys=("files", 0, "complete"), value=False)
    records = read_journal_records(bundle_dir=partial, journal_relative="journals/main.jsonl")
    rewrite_journal(bundle_dir=partial, relative="journals/main.jsonl", records=records[:10])
    prefix = read_bundle_directory(bundle_dir=partial)
    source = HostSourceContext(origin="environment-growth")
    receipt_ids: list[int] = []
    for delivered in (prefix, bundle, prefix):
        result = ingestor.ingest_bundle(
            bundle=delivered, source=source, github=evidence, redaction_attestation=full_proof(bundle=delivered)
        )
        assert result.status == "committed"
        receipt_ids.append(result.receipt_id)
    assert receipt_ids[0] == receipt_ids[2]
    rows = metadata_rows(database=database)
    assert [row["receipt_id"] for row in rows] == receipt_ids[:2]
    assert [row["claimed_complete"] for row in rows] == [False, True]
    assert [row["claimed_end_reason"] for row in rows] == ["interrupted", "finished"]


@pytest.mark.parametrize(("worker_count", "dropped_records"), [(None, 0), (0, 10**30)])
def test_null_revisions_zero_and_large_counts_keep_their_types(
    *,
    database: Database,
    ingestor: Ingestor,
    bundle_directory: Path,
    evidence: GitHubEvidence,
    worker_count: int | None,
    dropped_records: int,
) -> None:
    manifest = cast(dict[str, JSONValue], load_json(path=bundle_directory / MANIFEST_NAME))
    suite = cast(dict[str, JSONValue], manifest["suite"])
    selection = cast(dict[str, JSONValue], suite["selection"])
    selection["worker_count"] = worker_count
    execution = cast(dict[str, JSONValue], manifest["execution"])
    workflow = cast(dict[str, JSONValue], execution["workflow"])
    workflow["revision"] = None
    environment = cast(dict[str, JSONValue], manifest["environment"])
    sources = cast(list[dict[str, JSONValue]], environment["sources"])
    for source in sources:
        if source["role"] == "tested":
            source["revision"] = None
    environment["image_digests"] = {}
    completeness = cast(dict[str, JSONValue], manifest["completeness"])
    completeness["dropped_records"] = dropped_records
    completeness["complete"] = dropped_records == 0
    manifest["created_at"] = "2026-10-28T12:00:00.123456789Z"
    (bundle_directory / MANIFEST_NAME).write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    records = read_journal_records(bundle_dir=bundle_directory, journal_relative="journals/main.jsonl")
    opening = cast(dict[str, JSONValue], records[0]["data"])
    opening["suite"] = suite
    rewrite_journal(bundle_dir=bundle_directory, relative="journals/main.jsonl", records=records)
    changed = read_bundle_directory(bundle_dir=bundle_directory)
    result = ingestor.ingest_bundle(
        bundle=changed,
        source=HostSourceContext(origin="environment-unknown"),
        github=replace(evidence, tests_run_head=False),
        redaction_attestation=full_proof(bundle=changed),
    )
    assert result.status == "committed"
    row = metadata_rows(database=database)[0]
    assert row["source_created_at"] == "2026-10-28T12:00:00.123456789Z"
    assert row["claimed_workflow_revision"] is None
    assert row["tested_sha"] is None
    assert row["revision_verdict"] == "unknown"
    assert row["claimed_sources"][0]["revision"] is None
    assert row["claimed_suite"]["selection"]["worker_count"] == worker_count
    assert row["claimed_suite"]["campaign"] is None
    assert row["claimed_dropped_records"] == dropped_records
    assert type(row["claimed_dropped_records"]) is int
    assert row["claimed_image_digests"] == {}
    assert row["claimed_config_hash"] is None


def test_reader_has_no_source_table_or_write_access(*, database: Database) -> None:
    with psycopg.connect(database.reader_dsn, autocommit=True) as connection:
        assert connection.execute(
            sql.SQL("SELECT count(*) FROM {}.ledger_environment").format(sql.Identifier(database.schema))
        ).fetchone() == (0,)
        for table in ("source_receipt", "source_execution", "source_github_observation", "source_redaction_proof"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(
                    sql.SQL("SELECT * FROM {}.{}").format(sql.Identifier(database.schema), sql.Identifier(table))
                )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(sql.SQL("DELETE FROM {}.ledger_execution").format(sql.Identifier(database.schema)))


def test_environment_http_pages_use_reader_permissions_and_receipt_order(
    *, database: Database, ingestor: Ingestor, bundle: FrozenBundle, evidence: GitHubEvidence
) -> None:
    receipt_ids: list[int] = []
    for origin in ("environment-host-one", "environment-host-two"):
        result = ingestor.ingest_bundle(
            bundle=bundle,
            source=HostSourceContext(origin=origin),
            github=evidence,
            redaction_attestation=full_proof(bundle=bundle),
        )
        receipt_ids.append(result.receipt_id)
    server = api.ReadAPIServer(dsn=database.reader_dsn, schema=database.schema, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.port}/api/v1/executions"
    try:
        for offset, expected in enumerate(receipt_ids):
            status, body = http_get(url=f"{base_url}/{EXECUTION_ID}/environment?limit=1&offset={offset}")
            assert status == 200
            assert body["count"] == 1
            assert body["environments"][0]["receipt_id"] == expected
            assert body["execution_id"] == EXECUTION_ID
            assert body["limit"] == 1 and body["offset"] == offset
        status, body = http_get(url=f"{base_url}/{EXECUTION_ID}/environment?offset=2")
        assert status == 200 and body["environments"] == []
        status, _ = http_get(url=f"{base_url}/absent/environment")
        assert status == 404
        for query in ("limit=0", "limit=501", "offset=-1", "offset=10001", "limit=1&limit=2", "private=canary"):
            status, body = http_get(url=f"{base_url}/{EXECUTION_ID}/environment?{query}")
            assert status == 400
            assert "canary" not in json.dumps(body)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_migration_projects_preexisting_committed_receipt(
    *, dsn: str, bundle: FrozenBundle, evidence: GitHubEvidence, tmp_path: Path
) -> None:
    schema = f"ci_obs_environment_upgrade_{uuid.uuid4().hex[:12]}"
    try:
        with psycopg.connect(dsn, autocommit=True) as connection, connection.transaction():
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            connection.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
            for version, filename in ((1, "0001_ledger.sql"), (2, "0002_sources.sql")):
                connection.execute((ledger.MIGRATIONS_DIR / filename).read_text())
                connection.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
        client = Ingestor(dsn=dsn, schema=schema, quarantine_root=tmp_path / "upgrade-quarantine")
        try:
            result = client.ingest_bundle(
                bundle=bundle,
                source=HostSourceContext(origin="environment-upgrade"),
                github=evidence,
                redaction_attestation=full_proof(bundle=bundle),
            )
        finally:
            client.close()
        assert result.status == "committed"
        assert ledger.apply_migrations(dsn=dsn, schema=schema) == [3]
        assert ledger.apply_migrations(dsn=dsn, schema=schema) == []
        rows = metadata_rows(database=Database(dsn=dsn, schema=schema, reader_dsn=dsn))
        assert len(rows) == 1
        assert rows[0]["receipt_id"] == result.receipt_id
    finally:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
