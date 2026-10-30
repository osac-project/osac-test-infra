"""Real-PostgreSQL and real-filesystem tests for bundle freezing, archive
safety, quarantine durability, receipts, and source privacy (W01b).

Every database test runs against a real server reached through
CI_OBSERVABILITY_TEST_DSN; there is no SQLite fallback and no mocked
connection. The tests create their own ci_obs_* schema (and drop it at the
end); they never touch the production ci_observability schema. When the
variable is not set and no driver is installed the module skips clearly;
when the variable is set, a missing driver or an unreachable database
fails the run.

This module also hosts the builders shared by the other W01b test modules
(test_observability_ingest, test_observability_reconcile), the same role
contract_helpers plays for the contract tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import threading
import zipfile
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import cast

import pytest

try:
    import psycopg
except ModuleNotFoundError:
    if os.environ.get("CI_OBSERVABILITY_TEST_DSN", "").strip():
        # A configured database with a missing driver is a broken setup.
        raise
    pytest.skip("psycopg is not installed and CI_OBSERVABILITY_TEST_DSN is not set", allow_module_level=True)

from contract_helpers import MANIFEST_NAME, VALID_BUNDLES, copy_bundle, load_json, rewrite_journal
from psycopg import sql
from psycopg.conninfo import make_conninfo

from monitoring.ci_observability import bundle_reader, ledger
from monitoring.ci_observability.bundle_reader import (
    BundleArchive,
    BundleDirectory,
    BundleError,
    FrozenBundle,
    FrozenFile,
    ensure_private_directory,
    freeze,
    materialize_quarantine,
    parse_journal_records,
    read_bundle_archive,
    read_bundle_directory,
)
from monitoring.ci_observability.ingest import (
    ArtifactSourceContext,
    HostSourceContext,
    IngestError,
    Ingestor,
    IngestResult,
    RedactionAttestation,
)
from monitoring.ci_observability.reconcile import GitHubEvidence

RawRecord = dict[str, object]
UNREACHABLE_DSN = "postgresql://ci_observability@127.0.0.1:1/ci_observability?connect_timeout=2"
TRUSTED_RUN = 1122334455
TRUSTED_REPOSITORY = "osac-project/osac-test-infra"
TRUSTED_REPOSITORY_ID = 998877665


# Shared fixtures and builders for the W01b test modules.


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
    name = f"ci_obs_w01b_sources_{os.getpid()}"
    assert ledger.apply_migrations(dsn=dsn, schema=name) == [1, 2, 3]
    yield name
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


@pytest.fixture
def quarantine(tmp_path: Path) -> Path:
    return tmp_path / "quarantine"


@pytest.fixture
def ingestor(dsn: str, test_schema: str, quarantine: Path) -> Iterator[Ingestor]:
    client = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    yield client
    client.close()


def run_record(
    run_id: int,
    run_attempt: int,
    *,
    repository: str = TRUSTED_REPOSITORY,
    repository_id: int = TRUSTED_REPOSITORY_ID,
    event: str = "schedule",
    conclusion: str | None = "success",
    started: str = "2026-10-27T04:00:00Z",
    head_sha: str = "a" * 40,
) -> RawRecord:
    """A raw runs-API record for one attempt, as a trusted caller supplies it."""

    return {
        "id": run_id,
        "run_attempt": run_attempt,
        "event": event,
        "head_sha": head_sha,
        "run_started_at": started,
        "conclusion": conclusion,
        "repository": {"full_name": repository, "id": repository_id},
    }


def job_record(
    job_id: int,
    run_id: int,
    run_attempt: int,
    name: str,
    *,
    conclusion: str | None = "success",
    started: str = "2026-10-27T04:01:00Z",
    completed: str = "2026-10-27T05:30:00Z",
    runner_name: str = "runner-1",
) -> RawRecord:
    """A raw jobs-API record of one attempt."""

    return {
        "id": job_id,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "name": name,
        "conclusion": conclusion,
        "started_at": started,
        "completed_at": completed,
        "runner_name": runner_name,
    }


def artifact_record(
    artifact_id: int,
    run_id: int,
    name: str,
    *,
    digest: str | None = None,
    expired: bool = False,
    created_at: str = "2026-10-27T05:31:00Z",
) -> RawRecord:
    """A raw artifacts-API record."""

    return {
        "id": artifact_id,
        "name": name,
        "digest": digest,
        "expired": expired,
        "created_at": created_at,
        "workflow_run": {"id": run_id},
    }


def verified_evidence(
    *,
    job_id: int,
    run_id: int = TRUSTED_RUN,
    repository_id: int = TRUSTED_REPOSITORY_ID,
    conclusion: str | None = "success",
    job_conclusion: str | None = "success",
    artifacts: tuple[RawRecord, ...] = (),
    statuses: tuple[RawRecord, ...] = (),
    status_commit_sha: str | None = None,
    expected_job_id: int | None = None,
    expected_display_name: str | None = None,
    expected_context: str | None = None,
    trusted_creator_ids: frozenset[int] = frozenset(),
    tests_run_head: bool = False,
    event: str = "schedule",
) -> GitHubEvidence:
    """Evidence whose trusted run and job records verify one specific job,
    matching a serial-style bundle claim."""

    return GitHubEvidence(
        run=cast(Mapping[str, object], run_record(run_id, 1, conclusion=conclusion, event=event)),
        jobs=(cast(Mapping[str, object], job_record(job_id, run_id, 1, "e2e-bmaas", conclusion=job_conclusion)),),
        artifacts=tuple(cast(Mapping[str, object], record) for record in artifacts),
        statuses=tuple(cast(Mapping[str, object], record) for record in statuses),
        status_commit_sha=status_commit_sha,
        expected_job_id=expected_job_id,
        expected_display_name=expected_display_name,
        expected_context=expected_context,
        trusted_creator_ids=trusted_creator_ids,
        tests_run_head=tests_run_head,
    )


def full_proof(*, bundle: FrozenBundle) -> RedactionAttestation:
    """A trusted redaction proof covering the manifest and every file."""

    return RedactionAttestation(
        policy="test-sanitizer", covered_digests=(bundle.manifest_sha256, *(file.sha256 for file in bundle.files))
    )


def frozen_bundle(bundle_dir: Path) -> FrozenBundle:
    return read_bundle_directory(bundle_dir=bundle_dir)


def publish(
    ingestor: Ingestor, bundle_dir: Path, *, origin: str, job_id: int, evidence: GitHubEvidence | None = None
) -> IngestResult:
    """Deliver a directory bundle with a verified job mapping and a full
    redaction proof, so it publishes."""

    bundle = frozen_bundle(bundle_dir)
    return ingestor.ingest_bundle(
        bundle=bundle,
        source=HostSourceContext(origin=origin),
        github=evidence if evidence is not None else verified_evidence(job_id=job_id, expected_job_id=job_id),
        redaction_attestation=full_proof(bundle=bundle),
    )


def reidentify_bundle(
    destination: Path,
    *,
    source_name: str = "serial",
    execution_id: str,
    run_id: int | None = None,
    run_attempt: int | None = None,
    repository: str | None = None,
    github_job_id: int | None = None,
    job_name: str | None = None,
    repository_id: int | None = None,
) -> Path:
    """Clone an example bundle under a new claimed execution identity.

    The manifest's execution block is rewritten and every journal record's
    execution id (and the execution_started echo) follows, so the clone
    still satisfies the frozen contract.
    """

    bundle = copy_bundle(source_name, destination / source_name)
    manifest = load_json(bundle / MANIFEST_NAME)
    execution = manifest["execution"]
    execution["execution_id"] = execution_id
    if run_id is not None:
        execution["run_id"] = run_id
    if run_attempt is not None:
        execution["run_attempt"] = run_attempt
    if repository is not None:
        execution["repository"] = repository
    if repository_id is not None:
        execution["repository_id"] = repository_id
    if github_job_id is not None:
        execution["job"]["github_job_id"] = github_job_id
    if job_name is not None:
        execution["job"]["job_name"] = job_name
    (bundle / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    for entry in manifest["files"]:
        if entry["kind"] != "journal":
            continue
        records = read_journal_records(bundle, entry["path"])
        for record in records:
            record["execution_id"] = execution_id
            if record["event"] == "execution_started":
                record["data"]["job"] = execution["job"]
        rewrite_journal(bundle, entry["path"], records)
    return bundle


def mutate_record(bundle_dir: Path, journal_relative: str, sequence: int, data_key: str, value: object) -> None:
    """Change one record's payload, keeping checksums consistent: a different
    content under the same execution id."""

    records = read_journal_records(bundle_dir, journal_relative)
    for record in records:
        if record["sequence"] == sequence:
            record["data"][data_key] = value
    rewrite_journal(bundle_dir, journal_relative, records)


def read_journal_records(bundle_dir: Path, journal_relative: str) -> list[dict[str, object]]:
    """Parse one journal of a bundle directory, without validation."""

    return [
        json.loads(line)
        for line in (bundle_dir / journal_relative).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def append_record(bundle_dir: Path, journal_relative: str, record: dict[str, object]) -> None:
    """Append one record to a journal, keeping checksums consistent."""

    records = read_journal_records(bundle_dir, journal_relative)
    record["sequence"] = len(records) + 1
    records.append(record)
    rewrite_journal(bundle_dir, journal_relative, records)


def bulk_bundle(destination: Path, *, execution_id: str, collected: int) -> Path:
    """A clone of the serial bundle whose journal is one large collection.

    collected test_collected records plus the envelope produce more than
    one ledger batch (the batch cap is 10 000 events), which exercises
    partial imports and their recovery.
    """

    bundle = reidentify_bundle(destination, execution_id=execution_id)
    manifest = load_json(bundle / MANIFEST_NAME)
    base = {
        "schema": "osac.ci-observability.journal-record",
        "schema_version": 1,
        "execution_id": execution_id,
        "journal_id": "main",
        "recorded_at": "2026-10-28T10:00:00Z",
        "mono_ns": 1000,
    }
    records: list[dict[str, object]] = [
        {
            **base,
            "sequence": 1,
            "event": "execution_started",
            "data": {"job": manifest["execution"]["job"], "suite": manifest["suite"]},
        },
        {
            **base,
            "sequence": 2,
            "mono_ns": 1001,
            "event": "collection_finished",
            "data": {"outcome": "completed", "collected_count": collected, "deselected_count": 0},
        },
    ]
    for index in range(collected):
        records.append(
            {
                **base,
                "sequence": 3 + index,
                "mono_ns": 1002 + index,
                "event": "test_collected",
                "data": {"test_id": f"tests/unit/test_bulk.py::test_item_{index:05d}", "deselected": False},
            }
        )
    records.append(
        {
            **base,
            "sequence": 3 + collected,
            "mono_ns": 4000 + collected,
            "event": "execution_finished",
            "data": {"outcome": "passed", "exit_code": 0},
        }
    )
    rewrite_journal(bundle, "journals/main.jsonl", records)
    return bundle


def zip_bundle(bundle_dir: Path, archive_path: Path) -> str:
    """Zip a bundle directory and return the sha256 digest of the archive."""

    with zipfile.ZipFile(archive_path, "w") as archive:
        for path in sorted(bundle_dir.rglob("*")):
            if path.is_file():
                archive.write(path, str(path.relative_to(bundle_dir)))
    return "sha256:" + hashlib.sha256(archive_path.read_bytes()).hexdigest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def direct_fetch(dsn: str, schema: str, statement: str, params: tuple[object, ...] = ()) -> tuple[object, ...] | None:
    """One row through a fresh connection, so only committed data is visible."""

    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(sql.SQL(statement).format(schema=sql.Identifier(schema)), params).fetchone()


def example_path(name: str) -> Path:
    return next(path for path in VALID_BUNDLES if path.name == name)


# Migrations


def test_migrations_apply_all_versions_idempotently(dsn: str) -> None:
    name = f"ci_obs_w01b_idem_{os.getpid()}"
    try:
        assert ledger.apply_migrations(dsn=dsn, schema=name) == [1, 2, 3]
        assert ledger.apply_migrations(dsn=dsn, schema=name) == []
        versions = direct_fetch(dsn, name, "SELECT array_agg(version ORDER BY version) FROM {schema}.schema_migrations")
        assert versions == ([1, 2, 3],)
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


# Freezing the example bundles


def test_freeze_every_example_bundle(tmp_path: Path) -> None:
    for bundle_dir in VALID_BUNDLES:
        frozen = read_bundle_directory(bundle_dir=bundle_dir)
        manifest = load_json(bundle_dir / MANIFEST_NAME)
        assert frozen.execution_id == manifest["execution"]["execution_id"]
        assert len(frozen.files) == len(manifest["files"])
        journals = [file for file in frozen.files if file.kind == "journal"]
        assert frozen.total_records == sum(file.record_count or 0 for file in journals)
        assert frozen.manifest == manifest
        assert frozen.manifest_sha256 == sha256_hex((bundle_dir / MANIFEST_NAME).read_bytes())
        for ordinal, file in enumerate(frozen.files, start=1):
            assert file.ordinal == ordinal
            assert file.sha256 == sha256_hex((bundle_dir / file.path).read_bytes())


def test_freeze_collects_claimed_source_states() -> None:
    frozen = read_bundle_directory(bundle_dir=example_path("partial"))
    assert frozen.source_states, "the partial bundle carries collection_state records"
    assert all(isinstance(source, str) for source in frozen.source_states)


def test_parse_journal_records_reverifies_the_frozen_bytes(tmp_path: Path) -> None:
    frozen = read_bundle_directory(bundle_dir=example_path("serial"))
    journal = frozen.journal_files[0]
    records = parse_journal_records(file=journal)
    assert len(records) == journal.record_count
    tampered = FrozenFile(
        path=journal.path,
        kind="journal",
        sha256=journal.sha256,
        size_bytes=journal.size_bytes,
        data=b'{"schema": "osac.ci-observability.journal-record"}\n',
        ordinal=journal.ordinal,
        record_count=journal.record_count,
        complete=True,
    )
    with pytest.raises(BundleError, match="journal-frozen-bytes-mismatch"):
        parse_journal_records(file=tampered)
    native = next(file for file in frozen.files if file.kind == "native-report")
    with pytest.raises(BundleError, match="not-a-journal"):
        parse_journal_records(file=native)


def test_frozen_records_survive_source_swaps(tmp_path: Path) -> None:
    """The freeze is the snapshot: later filesystem swaps cannot change it."""

    bundle = copy_bundle("serial", tmp_path / "swapped")
    frozen = freeze(delivery=BundleDirectory(path=bundle))
    original = parse_journal_records(file=frozen.journal_files[0])
    (bundle / "journals" / "main.jsonl").write_text('{"schema": "swapped"}\n', encoding="utf-8")
    (bundle / "native-report" / "junit.xml").write_bytes(b"<swapped/>")
    after = parse_journal_records(file=frozen.journal_files[0])
    assert after == original
    assert frozen.files[1].data != b"<swapped/>"


def test_revalidate_frozen_derives_claims_from_pinned_bytes(tmp_path: Path) -> None:
    """A mutated parsed manifest or swapped file tuple cannot bypass
    validation: everything is re-derived from the pinned bytes."""

    bundle = read_bundle_directory(bundle_dir=copy_bundle("serial", tmp_path / "mutated"))
    bundle.manifest["redaction"] = {"applied": False, "findings": 12345}  # type: ignore[assignment]
    bundle.manifest["execution"]["job"]["github_job_id"] = 9999999999
    revalidated = bundle_reader.revalidate_frozen(bundle=bundle)
    assert revalidated.manifest["redaction"] == json.loads(bundle.manifest_text)["redaction"]
    assert revalidated.manifest["execution"]["job"]["github_job_id"] == 4588887777
    assert revalidated.manifest_sha256 == bundle.manifest_sha256
    # A payload that no longer matches any manifest entry is rejected: a
    # forger constructs the bundle, so the constructor is used directly.
    stolen = FrozenFile(
        path=bundle.files[0].path,
        kind="journal",
        sha256=bundle.files[0].sha256,
        size_bytes=bundle.files[0].size_bytes,
        data=b"not the journal",
        ordinal=bundle.files[0].ordinal,
        record_count=bundle.files[0].record_count,
        complete=True,
    )
    forged = FrozenBundle(
        manifest=bundle.manifest,
        manifest_text=bundle.manifest_text,
        manifest_sha256=bundle.manifest_sha256,
        files=(stolen, *bundle.files[1:]),
        execution_id=bundle.execution_id,
        total_records=bundle.total_records,
        source_states=bundle.source_states,
    )
    with pytest.raises(BundleError) as excinfo:
        bundle_reader.revalidate_frozen(bundle=forged)
    assert "file-missing" in str(excinfo.value)


# Malicious manifests: rejected before any listed file is opened


def test_path_escape_is_rejected_without_reading_outside(tmp_path: Path) -> None:
    """A '..' path in the manifest is rejected identically whether the
    outside file exists or not, proving it is never read."""

    outside = tmp_path / "secret-outside"
    secret = b"top-secret-content-outside-bundle\n"
    results = []
    for present in (True, False):
        bundle = copy_bundle("serial", tmp_path / f"escape-{present}")
        if present:
            outside.write_bytes(secret)
        manifest = load_json(bundle / MANIFEST_NAME)
        manifest["files"].append(
            {"path": "../secret-outside", "kind": "other", "sha256": sha256_hex(secret), "size_bytes": len(secret)}
        )
        (bundle / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        try:
            read_bundle_directory(bundle_dir=bundle)
            results.append("accepted")
        except BundleError as exc:
            results.append(str(exc))
        if present:
            outside.unlink()
    assert results[0] == results[1], f"outside file existence changed the error: {results}"
    assert "path-not-canonical" in results[0] or "pattern" in results[0]


def test_declared_bounds_reject_before_any_file_read(tmp_path: Path) -> None:
    """A 202-file manifest is rejected by the schema cap before any of the
    listed files is opened, identically whether they exist or not."""

    errors = []
    for present in (True, False):
        bundle = copy_bundle("serial", tmp_path / f"many-{present}")
        manifest = load_json(bundle / MANIFEST_NAME)
        for index in range(200):
            data = f"pad-{index}".encode()
            if present:
                (bundle / "native-report" / f"pad-{index}.bin").write_bytes(data)
            manifest["files"].append(
                {
                    "path": f"native-report/pad-{index}.bin",
                    "kind": "other",
                    "sha256": sha256_hex(data),
                    "size_bytes": len(data),
                }
            )
        (bundle / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        try:
            read_bundle_directory(bundle_dir=bundle)
            errors.append("accepted")
        except BundleError as exc:
            errors.append(str(exc))
    assert errors[0] == errors[1], f"file existence changed the error: {errors}"
    assert "maxItems" in errors[0]


def test_malformed_manifests_raise_bundle_errors(tmp_path: Path) -> None:
    for name, text in (
        ("list-manifest", "[]"),
        ("null-manifest", "null"),
        ("files-not-list", '{"files": "abc"}'),
        ("entry-not-object", '{"files": ["n"]}'),
        ("entry-missing-path", '{"files": [{"kind": "other"}]}'),
        ("no-files-key", "{}"),
    ):
        bundle = tmp_path / name
        bundle.mkdir()
        (bundle / MANIFEST_NAME).write_text(text, encoding="utf-8")
        with pytest.raises(BundleError):
            read_bundle_directory(bundle_dir=bundle)


def test_freeze_rejects_checksum_mismatch(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path / "checksum")
    with (bundle / "journals" / "main.jsonl").open("ab") as handle:
        handle.write(b"\n")
    with pytest.raises(BundleError) as excinfo:
        read_bundle_directory(bundle_dir=bundle)
    assert excinfo.value.code == "contract-violation"
    assert "checksum-mismatch" in str(excinfo.value)


def test_freeze_rejects_size_and_count_mismatch(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path / "size")
    manifest = load_json(bundle / MANIFEST_NAME)
    manifest["files"][0]["size_bytes"] = 1
    (bundle / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BundleError, match="size-mismatch"):
        read_bundle_directory(bundle_dir=bundle)

    bundle = copy_bundle("serial", tmp_path / "count")
    manifest = load_json(bundle / MANIFEST_NAME)
    manifest["files"][0]["record_count"] = 999
    (bundle / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BundleError, match="record-count-mismatch"):
        read_bundle_directory(bundle_dir=bundle)


def test_freeze_rejects_missing_unlisted_symlink_hardlink_fifo(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path / "missing")
    (bundle / "journals" / "main.jsonl").unlink()
    with pytest.raises(BundleError, match="file-missing"):
        read_bundle_directory(bundle_dir=bundle)

    bundle = copy_bundle("serial", tmp_path / "unlisted")
    (bundle / "extra.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(BundleError, match="unlisted-file"):
        read_bundle_directory(bundle_dir=bundle)

    bundle = copy_bundle("serial", tmp_path / "symlink")
    journal = bundle / "journals" / "main.jsonl"
    data = journal.read_bytes()
    journal.unlink()
    (tmp_path / "outside.jsonl").write_bytes(data)
    journal.symlink_to(tmp_path / "outside.jsonl")
    with pytest.raises(BundleError, match="file-symlink"):
        read_bundle_directory(bundle_dir=bundle)

    bundle = copy_bundle("serial", tmp_path / "symlink-walk")
    (bundle / "link").symlink_to("bundle-manifest.json")
    with pytest.raises(BundleError, match="symlink-in-bundle"):
        read_bundle_directory(bundle_dir=bundle)

    bundle = copy_bundle("serial", tmp_path / "hardlink")
    (tmp_path / "alias.jsonl").hardlink_to(bundle / "journals" / "main.jsonl")
    with pytest.raises(BundleError, match="file-hardlink"):
        read_bundle_directory(bundle_dir=bundle)

    bundle = copy_bundle("serial", tmp_path / "fifo")
    os.mkfifo(bundle / "pipe")
    with pytest.raises(BundleError, match="special-file-in-bundle"):
        read_bundle_directory(bundle_dir=bundle)


def test_freeze_rejects_malformed_journal_lines(tmp_path: Path) -> None:
    cases = {
        "blank-line": lambda text: text.rstrip("\n") + "\n\n",
        "duplicate-keys": lambda text: '{"schema": "x", "schema": "y"}\n',
        "non-finite": lambda text: '{"schema": "x", "value": NaN}\n',
        "malformed": lambda text: '{"schema": "x"\n',
        "not-object": lambda text: "[1, 2, 3]\n",
    }
    for name, replace in cases.items():
        bundle = copy_bundle("serial", tmp_path / name)
        path = bundle / "journals" / "main.jsonl"
        path.write_text(replace(path.read_text(encoding="utf-8")), encoding="utf-8")
        manifest = load_json(bundle / MANIFEST_NAME)
        non_blank = len([line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()])
        manifest["files"][0].update(
            {"sha256": sha256_hex(path.read_bytes()), "size_bytes": path.stat().st_size, "record_count": non_blank}
        )
        (bundle / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(BundleError):
            read_bundle_directory(bundle_dir=bundle)


def test_freeze_rejects_non_utf8(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path / "utf8")
    path = bundle / "journals" / "main.jsonl"
    path.write_bytes(b'{"schema": "\xff\xfe"}\n')
    manifest = load_json(bundle / MANIFEST_NAME)
    manifest["files"][0].update(
        {"sha256": sha256_hex(path.read_bytes()), "size_bytes": path.stat().st_size, "record_count": 1}
    )
    (bundle / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BundleError, match="not-utf8"):
        read_bundle_directory(bundle_dir=bundle)


def test_freeze_rejects_oversize_line(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path / "longline")
    manifest = load_json(bundle / MANIFEST_NAME)
    record = {
        "schema": "osac.ci-observability.journal-record",
        "schema_version": 1,
        "execution_id": manifest["execution"]["execution_id"],
        "journal_id": "main",
        "sequence": 1,
        "recorded_at": "2026-10-27T04:00:00Z",
        "mono_ns": None,
        "event": "execution_started",
        "data": {"job": manifest["execution"]["job"], "suite": manifest["suite"], "pad": "x" * (1024 * 1024 + 64)},
    }
    path = bundle / "journals" / "main.jsonl"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    manifest["files"][0].update(
        {"sha256": sha256_hex(path.read_bytes()), "size_bytes": path.stat().st_size, "record_count": 1}
    )
    (bundle / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BundleError, match="line-oversize"):
        read_bundle_directory(bundle_dir=bundle)


def test_freeze_rejects_oversize_files_before_reading(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path / "bigfile")
    with (bundle / "journals" / "main.jsonl").open("wb") as handle:
        handle.truncate(32 * 1024 * 1024 + 1)
    with pytest.raises(BundleError, match="file-oversize"):
        read_bundle_directory(bundle_dir=bundle)

    bundle = copy_bundle("serial", tmp_path / "bigmanifest")
    with (bundle / MANIFEST_NAME).open("wb") as handle:
        handle.truncate(1024 * 1024 + 1)
    with pytest.raises(BundleError, match="file-oversize"):
        read_bundle_directory(bundle_dir=bundle)


def test_freeze_rejects_noncanonical_and_duplicate_paths(tmp_path: Path) -> None:
    for bad in ("journals//main.jsonl", "journals/./main.jsonl", "journals/../main.jsonl"):
        bundle = copy_bundle("serial", tmp_path / f"path-{abs(hash(bad)) % 10000}")
        manifest = load_json(bundle / MANIFEST_NAME)
        manifest["files"][0]["path"] = bad
        (bundle / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(BundleError) as excinfo:
            read_bundle_directory(bundle_dir=bundle)
        # The schema pattern and the explicit segment check both reject it;
        # either way no file is ever opened.
        assert "path-not-canonical" in str(excinfo.value) or "pattern" in str(excinfo.value)

    bundle = copy_bundle("parallel", tmp_path / "dup-path")
    manifest = load_json(bundle / MANIFEST_NAME)
    manifest["files"][1]["path"] = manifest["files"][0]["path"]
    (bundle / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BundleError, match="path-duplicate"):
        read_bundle_directory(bundle_dir=bundle)


def test_freeze_rejects_excess_depth_and_entries(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path / "deep")
    deep = bundle
    for index in range(17):
        deep = deep / f"d{index}"
    deep.mkdir(parents=True)
    (deep / "file.txt").write_text("deep", encoding="utf-8")
    with pytest.raises(BundleError, match="directory-depth-exceeded"):
        read_bundle_directory(bundle_dir=bundle)

    bundle = copy_bundle("serial", tmp_path / "many")
    for index in range(4097):
        (bundle / f"f{index:05d}.txt").write_text("x", encoding="utf-8")
    with pytest.raises(BundleError, match="directory-entries-oversize"):
        read_bundle_directory(bundle_dir=bundle)


def test_freeze_rejects_cross_execution_reference(tmp_path: Path) -> None:
    bundle = reidentify_bundle(tmp_path / "cross", execution_id="exec-cross-1")
    records = read_journal_records(bundle, "journals/main.jsonl")
    records[1]["execution_id"] = "someone-elses-execution"
    rewrite_journal(bundle, "journals/main.jsonl", records)
    with pytest.raises(BundleError, match="cross-execution-reference"):
        read_bundle_directory(bundle_dir=bundle)


def test_journal_diagnostics_use_ordinals_not_paths(tmp_path: Path) -> None:
    """Producer-chosen filenames never reach an error message."""

    secret_name = "SECRETJOURNALNAME"
    bundle = copy_bundle("serial", tmp_path / "names")
    manifest = load_json(bundle / MANIFEST_NAME)
    manifest["files"][0]["path"] = f"journals/{secret_name}.jsonl"
    (bundle / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    journal = bundle / "journals" / "main.jsonl"
    journal.rename(bundle / "journals" / f"{secret_name}.jsonl")
    path = bundle / "journals" / f"{secret_name}.jsonl"
    path.write_bytes(b'{"broken"\n')
    manifest = load_json(bundle / MANIFEST_NAME)
    manifest["files"][0].update(
        {"sha256": sha256_hex(path.read_bytes()), "size_bytes": path.stat().st_size, "record_count": 1}
    )
    (bundle / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BundleError) as excinfo:
        read_bundle_directory(bundle_dir=bundle)
    assert secret_name not in str(excinfo.value)
    assert "journal 1" in str(excinfo.value)


# Archives


def test_archive_freeze_matches_directory_freeze(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path / "arch")
    archive = tmp_path / "bundle.zip"
    digest = zip_bundle(bundle, archive)
    from_archive = read_bundle_archive(archive_path=archive, received_digest=digest)
    from_directory = read_bundle_directory(bundle_dir=bundle)
    assert from_archive.manifest_sha256 == from_directory.manifest_sha256
    assert [file.sha256 for file in from_archive.files] == [file.sha256 for file in from_directory.files]
    assert from_archive.total_records == from_directory.total_records


def test_archive_rejects_digest_and_manifest_problems(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path / "digest")
    archive = tmp_path / "bundle.zip"
    zip_bundle(bundle, archive)
    with pytest.raises(BundleError, match="archive-digest-mismatch"):
        read_bundle_archive(archive_path=archive, received_digest="sha256:" + "0" * 64)
    with pytest.raises(BundleError, match="malformed-received-digest"):
        read_bundle_archive(archive_path=archive, received_digest="not-a-digest")

    archive = tmp_path / "nomanifest.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("journals/main.jsonl", "{}\n")
    digest = "sha256:" + sha256_hex(archive.read_bytes())
    with pytest.raises(BundleError, match="archive-missing-manifest"):
        read_bundle_archive(archive_path=archive, received_digest=digest)

    archive = tmp_path / "not-a.zip"
    archive.write_bytes(b"this is not a zip file")
    digest = "sha256:" + sha256_hex(archive.read_bytes())
    with pytest.raises(BundleError, match="archive-not-readable"):
        read_bundle_archive(archive_path=archive, received_digest=digest)


@pytest.mark.filterwarnings("ignore:Duplicate name:UserWarning")
def test_archive_rejects_duplicate_members(tmp_path: Path) -> None:
    archive = tmp_path / "dupe.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("bundle-manifest.json", "{}")
        zf.writestr("a.txt", "one")
        zf.writestr("a.txt", "two")
    digest = "sha256:" + sha256_hex(archive.read_bytes())
    with pytest.raises(BundleError, match="archive-duplicate-member"):
        read_bundle_archive(archive_path=archive, received_digest=digest)


def test_archive_rejects_unsafe_members(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path / "extra-member")
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                zf.write(path, str(path.relative_to(bundle)))
        zf.writestr("evil.txt", "extra")
    digest = "sha256:" + sha256_hex(archive.read_bytes())
    with pytest.raises(BundleError, match="unlisted-file"):
        read_bundle_archive(archive_path=archive, received_digest=digest)


def test_archive_rejects_unsafe_member_names_and_attributes(tmp_path: Path) -> None:
    for name in ("../evil.txt", "/etc/passwd", "a/../../evil", "a//b", "a/./b", "C:\\evil"):
        archive = tmp_path / f"names-{abs(hash(name)) % 10000}.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("bundle-manifest.json", "{}")
            zf.writestr(name, "x")
        digest = "sha256:" + sha256_hex(archive.read_bytes())
        with pytest.raises(BundleError, match="archive-member-path-unsafe"):
            read_bundle_archive(archive_path=archive, received_digest=digest)

    archive = tmp_path / "symlink-member.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        info = zipfile.ZipInfo("bundle-manifest.json")
        info.external_attr = (0o120777 << 16) | 0o755
        zf.writestr(info, "{}")
    digest = "sha256:" + sha256_hex(archive.read_bytes())
    with pytest.raises(BundleError, match="archive-member-not-regular"):
        read_bundle_archive(archive_path=archive, received_digest=digest)

    archive = tmp_path / "bzip2.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("bundle-manifest.json", "{}", compress_type=zipfile.ZIP_BZIP2)
    digest = "sha256:" + sha256_hex(archive.read_bytes())
    with pytest.raises(BundleError, match="archive-unsupported-compression"):
        read_bundle_archive(archive_path=archive, received_digest=digest)


def test_archive_rejects_member_and_total_bombs(tmp_path: Path) -> None:
    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bundle-manifest.json", "{}")
        zf.writestr("journals/main.jsonl", b"\0" * (32 * 1024 * 1024 + 1))
    digest = "sha256:" + sha256_hex(archive.read_bytes())
    with pytest.raises(BundleError, match="archive-member-oversize"):
        read_bundle_archive(archive_path=archive, received_digest=digest)

    archive = tmp_path / "total-bomb.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bundle-manifest.json", "{}")
        for index in range(9):
            zf.writestr(f"journals/part{index}", b"\0" * (30 * 1024 * 1024))
    digest = "sha256:" + sha256_hex(archive.read_bytes())
    with pytest.raises(BundleError, match="archive-total-oversize"):
        read_bundle_archive(archive_path=archive, received_digest=digest)

    archive = tmp_path / "huge.zip"
    with archive.open("wb") as handle:
        handle.truncate(270 * 1024 * 1024)
    with pytest.raises(BundleError, match="archive-oversize"):
        read_bundle_archive(archive_path=archive, received_digest="sha256:" + "0" * 64)


def test_archive_rejects_fifo_and_symlink_paths_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "spooled-archive.zip"
    os.mkfifo(fifo)
    outcome: dict[str, str] = {}

    def opener() -> None:
        try:
            read_bundle_archive(archive_path=fifo, received_digest="sha256:" + "0" * 64)
            outcome["result"] = "returned"
        except BundleError as exc:
            outcome["result"] = exc.code

    thread = threading.Thread(target=opener, daemon=True)
    thread.start()
    thread.join(timeout=3.0)
    blocked = thread.is_alive()
    if blocked:
        writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(writer)
        thread.join(timeout=5.0)
    assert not blocked, "read_bundle_archive blocked on a FIFO"
    assert outcome["result"] == "archive-not-regular"

    bundle = copy_bundle("serial", tmp_path / "symlinked")
    archive = tmp_path / "real.zip"
    zip_bundle(bundle, archive)
    digest = "sha256:" + sha256_hex(archive.read_bytes())
    link = tmp_path / "link.zip"
    link.symlink_to(archive)
    with pytest.raises(BundleError, match="archive-not-readable"):
        read_bundle_archive(archive_path=link, received_digest=digest)

    alias = tmp_path / "alias.zip"
    alias.hardlink_to(archive)
    with pytest.raises(BundleError, match="archive-hardlink"):
        read_bundle_archive(archive_path=alias, received_digest=digest)


def test_archive_rejects_encrypted_member_flag(tmp_path: Path) -> None:
    """A handcrafted general-purpose flag bit 0 (encrypted) is rejected."""

    bundle = copy_bundle("serial", tmp_path / "enc")
    archive = tmp_path / "encrypted.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                zf.write(path, str(path.relative_to(bundle)))
    raw = bytearray(archive.read_bytes())
    signature = struct.pack("<I", 0x02014B50)
    position = raw.find(signature)
    assert position > 0
    flags_at = position + 8
    raw[flags_at] |= 0x01
    archive.write_bytes(bytes(raw))
    digest = "sha256:" + sha256_hex(archive.read_bytes())
    with pytest.raises(BundleError, match="archive-member-encrypted"):
        read_bundle_archive(archive_path=archive, received_digest=digest)


def test_archive_preflight_bounds_allocation(tmp_path: Path) -> None:
    """A forged central directory with millions of entries is rejected
    before ZipFile allocates anything, with bounded peak memory."""

    def craft_central_directory_bomb(entries: int) -> bytes:
        one = struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0) + b"a"
        directory = one * entries
        zip64_eocd = struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, 20, 20, 0, 0, entries, entries, len(directory), 0)
        zip64_locator = struct.pack("<IIQI", 0x07064B50, 0, len(directory), 1)
        end = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, 0xFFFF, 0xFFFF, len(directory), 0, 0)
        return directory + zip64_eocd + zip64_locator + end

    archive = tmp_path / "bomb.zip"
    archive.write_bytes(craft_central_directory_bomb(2_000_000))
    assert len(archive.read_bytes()) < bundle_reader.MAX_ARCHIVE_BYTES
    digest = "sha256:" + sha256_hex(archive.read_bytes())
    probe = (
        "import sys; sys.path.insert(0, '.');\n"
        "from monitoring.ci_observability.bundle_reader import read_bundle_archive, BundleError\n"
        "try:\n"
        "    read_bundle_archive(archive_path=sys.argv[1], received_digest=sys.argv[2])\n"
        "    print('accepted')\n"
        "except BundleError as exc:\n"
        "    peak = [line for line in open('/proc/self/status') if line.startswith('VmHWM')][0].split()[1]\n"
        "    print(exc.code, peak)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe, str(archive), digest],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=Path(__file__).resolve().parents[3],
    )
    assert result.returncode == 0, result.stderr
    code, peak_kib = result.stdout.split()
    assert code == "archive-member-limit"
    assert int(peak_kib) < 300 * 1024, f"peak RSS {int(peak_kib) // 1024} MiB before rejection"


def test_journal_parsing_streams_under_bounded_memory(tmp_path: Path) -> None:
    """A 32 MiB journal of tiny lines is rejected on the first line,
    without splitting the whole file into a list first."""

    journal = tmp_path / "huge.jsonl"
    with journal.open("wb") as handle:
        for _ in range(11_184_810):
            handle.write(b"xy\n")
    runner = (
        "import sys\n"
        "sys.path.insert(0, '.')\n"
        "from monitoring.ci_observability.bundle_reader import BundleError, _parse_journal_line\n"
        "data = open(sys.argv[1], 'rb').read()\n"
        "count = 0\n"
        "offset = 0\n"
        "try:\n"
        "    while offset < len(data):\n"
        "        end = data.find(b'\\n', offset)\n"
        "        line = data[offset:end if end != -1 else len(data)]\n"
        "        _parse_journal_line(line, 1, count)\n"
        "        count += 1\n"
        "        offset = (end + 1) if end != -1 else len(data)\n"
        "    print('accepted', count)\n"
        "except BundleError as exc:\n"
        "    peak = [line for line in open('/proc/self/status') if line.startswith('VmHWM')][0].split()[1]\n"
        "    print(exc.code, peak)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", runner, str(journal)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=Path(__file__).resolve().parents[3],
    )
    assert result.returncode == 0, result.stderr
    code, peak_kib = result.stdout.split()
    assert code == "contract-violation"
    # The 32 MiB payload plus the interpreter; the pre-revision line-split
    # allocation peaked above 600 MiB.
    assert int(peak_kib) < 150 * 1024, f"peak RSS {int(peak_kib) // 1024} MiB while parsing"


def test_archive_accepts_directory_entries(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path / "dirs")
    archive = tmp_path / "dirs.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for path in sorted(bundle.rglob("*")):
            relative = str(path.relative_to(bundle))
            if path.is_dir():
                zf.writestr(relative + "/", "")
            elif path.is_file():
                zf.write(path, relative)
    digest = "sha256:" + sha256_hex(archive.read_bytes())
    frozen = read_bundle_archive(archive_path=archive, received_digest=digest)
    assert frozen.total_records == 18


# Quarantine: durability and symlink safety


def test_quarantine_materializes_frozen_bytes(tmp_path: Path) -> None:
    source = example_path("serial")
    frozen = read_bundle_directory(bundle_dir=source)
    directory = tmp_path / "quarantine"
    materialize_quarantine(bundle=frozen, directory=directory)
    assert (directory / MANIFEST_NAME).read_text(encoding="utf-8") == frozen.manifest_text
    for file in frozen.files:
        target = directory / file.path
        assert target.read_bytes() == file.data
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert target.stat().st_nlink == 1
    assert stat.S_IMODE((directory / "journals").stat().st_mode) == 0o700
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    # The native report stays opaque: retained byte-for-byte with its digest.
    native = next(file for file in frozen.files if file.kind == "native-report")
    assert (directory / native.path).read_bytes() == (source / native.path).read_bytes()


def test_quarantine_retry_and_conflict(tmp_path: Path) -> None:
    frozen = read_bundle_directory(bundle_dir=example_path("serial"))
    directory = tmp_path / "quarantine"
    materialize_quarantine(bundle=frozen, directory=directory)
    materialize_quarantine(bundle=frozen, directory=directory)
    files = [path for path in directory.rglob("*") if path.is_file()]
    assert len(files) == len(frozen.files) + 1  # every bundle file plus the manifest

    conflicting = tmp_path / "conflict"
    conflicting.mkdir(mode=0o700)
    (conflicting / "journals").mkdir(mode=0o700)
    (conflicting / "journals" / "main.jsonl").write_text("different", encoding="utf-8")
    with pytest.raises(BundleError, match="quarantine-conflict"):
        materialize_quarantine(bundle=frozen, directory=conflicting)


def test_quarantine_short_writes_complete(tmp_path: Path) -> None:
    """A short os.write is retried to completion: the stored bytes are
    never silently truncated."""

    frozen = read_bundle_directory(bundle_dir=copy_bundle("serial", tmp_path / "bundle"))
    directory = tmp_path / "quarantine"
    real_write = os.write
    state = {"cut": False}

    def short_write(descriptor: int, data: bytes) -> int:
        if not state["cut"] and len(data) > 512:
            state["cut"] = True
            return real_write(descriptor, data[: len(data) // 2])
        return real_write(descriptor, data)

    os.write = short_write
    try:
        materialize_quarantine(bundle=frozen, directory=directory)
    finally:
        os.write = real_write
    assert state["cut"]
    assert (directory / MANIFEST_NAME).read_text(encoding="utf-8") == frozen.manifest_text
    for file in frozen.files:
        assert (directory / file.path).read_bytes() == file.data


def test_quarantine_raises_when_durability_fails(tmp_path: Path) -> None:
    """Removing the root after the last publish, or a failing fsync,
    raises instead of acknowledging an unverifiable quarantine."""

    frozen = read_bundle_directory(bundle_dir=copy_bundle("serial", tmp_path / "bundle"))
    directory = tmp_path / "quarantine"
    expected_renames = len(frozen.files) + 1
    state = {"links": 0, "removed": False}
    real_link = os.link

    def removing_link(src: str, dst: str, **kwargs: object) -> None:
        real_link(src, dst, **kwargs)  # type: ignore[arg-type]
        state["links"] += 1
        if state["links"] == expected_renames:
            import shutil

            shutil.rmtree(directory)
            state["removed"] = True

    os.link = removing_link
    try:
        with pytest.raises(BundleError):
            materialize_quarantine(bundle=frozen, directory=directory)
    finally:
        os.link = real_link
    assert state["removed"]

    directory2 = tmp_path / "quarantine2"
    real_fsync = os.fsync
    state2 = {"calls": 0}

    def failing_fsync(descriptor: int) -> None:
        state2["calls"] += 1
        raise OSError("injected fsync failure")

    os.fsync = failing_fsync
    try:
        with pytest.raises(BundleError, match="quarantine-not-durable"):
            materialize_quarantine(bundle=frozen, directory=directory2)
    finally:
        os.fsync = real_fsync
    assert state2["calls"] > 0


def test_quarantine_fsyncs_nested_directories(tmp_path: Path) -> None:
    """Every directory that received an entry is fsynced, not just the
    root."""

    frozen = read_bundle_directory(bundle_dir=copy_bundle("parallel", tmp_path / "bundle"))
    directory = tmp_path / "quarantine"
    synced: set[tuple[int, int]] = set()
    real_fsync = os.fsync

    def tracking_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        synced.add((metadata.st_dev, metadata.st_ino))
        real_fsync(descriptor)

    os.fsync = tracking_fsync
    try:
        materialize_quarantine(bundle=frozen, directory=directory)
    finally:
        os.fsync = real_fsync
    subdirectories = [path for path in directory.rglob("*") if path.is_dir()]
    assert subdirectories
    for subdirectory in subdirectories:
        metadata = os.stat(subdirectory)
        assert (metadata.st_dev, metadata.st_ino) in synced, f"{subdirectory} was never fsynced"


def test_new_quarantine_root_creation_fsyncs_the_parent(tmp_path: Path) -> None:
    """Creating a fresh root directory flushes the parent entry that holds
    it before anything is acknowledged, and every new ancestor entry
    along the way."""

    frozen = read_bundle_directory(bundle_dir=copy_bundle("serial", tmp_path / "bundle"))
    parent = tmp_path / "quarantine"
    parent.mkdir(mode=0o700)
    directory = parent / "deep" / "nest" / "root"
    synced: set[tuple[int, int]] = set()
    real_fsync = os.fsync

    def tracking_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        synced.add((metadata.st_dev, metadata.st_ino))
        real_fsync(descriptor)

    os.fsync = tracking_fsync
    try:
        materialize_quarantine(bundle=frozen, directory=directory)
    finally:
        os.fsync = real_fsync
    # The parent entry holding the new root, and each intermediate
    # directory created on the way down, were fsynced.
    for created in (parent, directory.parent, directory.parent.parent):
        metadata = os.stat(created)
        assert (metadata.st_dev, metadata.st_ino) in synced, f"{created} was never fsynced as a parent"
    assert (directory / "journals" / "main.jsonl").is_file()


def test_existing_matching_file_fsyncs_its_parent(tmp_path: Path) -> None:
    """A pre-existing file with matching bytes still establishes the
    durability of its own directory entry: another publisher may have
    linked it without syncing the parent."""

    frozen = read_bundle_directory(bundle_dir=copy_bundle("serial", tmp_path / "bundle"))
    directory = tmp_path / "quarantine"
    materialize_quarantine(bundle=frozen, directory=directory)
    journals_identity = os.stat(directory / "journals")
    real_fsync = os.fsync

    def tracking_fsync(descriptor: int) -> None:
        real_fsync(descriptor)

    def failing_parent_fsync(descriptor: int) -> None:
        # The nested journals directory is the existing file's parent.
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == (journals_identity.st_dev, journals_identity.st_ino):
            raise OSError("injected parent fsync failure")
        tracking_fsync(descriptor)

    os.fsync = failing_parent_fsync
    try:
        with pytest.raises(BundleError, match="quarantine-not-durable"):
            materialize_quarantine(bundle=frozen, directory=directory)
    finally:
        os.fsync = real_fsync
    # Without the fix, the matching fastpath returned before any fsync of
    # the nested parent.
    assert (directory / "journals" / "main.jsonl").is_file()


def test_replaced_quarantine_root_is_not_acknowledged(tmp_path: Path) -> None:
    """Renaming the root away and putting a fresh private directory in its
    place mid-materialization fails the final durability check without
    modifying either directory."""

    frozen = read_bundle_directory(bundle_dir=copy_bundle("serial", tmp_path / "bundle"))
    parent = tmp_path / "quarantine"
    parent.mkdir(mode=0o700)
    directory = parent / "root"
    state = {"swapped": False}
    real_link = os.link

    def swapping_link(src: str, dst: str, **kwargs: object) -> None:
        real_link(src, dst, **kwargs)  # type: ignore[arg-type]
        if not state["swapped"]:
            state["swapped"] = True
            shutil.move(str(directory), str(tmp_path / "moved-away"))
            directory.mkdir(mode=0o700)

    os.link = swapping_link
    try:
        with pytest.raises(BundleError, match="quarantine-not-durable"):
            materialize_quarantine(bundle=frozen, directory=directory)
    finally:
        os.link = real_link
    assert state["swapped"]
    # The replacement was never written into, and the original survives.
    assert list(directory.iterdir()) == []
    assert (tmp_path / "moved-away" / "journals" / "main.jsonl").is_file()


def test_temporary_file_cleanup_on_write_and_fsync_failures(tmp_path: Path) -> None:
    """Partial writes and fsync failures after a full write leave no
    temporary files; an unlink failure after a successful publish is
    never acknowledged."""

    frozen = read_bundle_directory(bundle_dir=copy_bundle("serial", tmp_path / "bundle"))
    real_write = os.write

    # Partial write failure: the temp name is cleaned up.
    directory = tmp_path / "quarantine-partial"
    state = {"cut": False}

    def short_write(descriptor: int, data: bytes) -> int:
        if not state["cut"] and len(data) > 128:
            state["cut"] = True
            raise OSError("injected partial write failure")
        return real_write(descriptor, data)

    os.write = short_write
    try:
        with pytest.raises(BundleError):
            materialize_quarantine(bundle=frozen, directory=directory)
    finally:
        os.write = real_write
    assert state["cut"]
    assert [p for p in directory.rglob("*.tmp")] == []

    # Fsync failure after a full write: the temp name is cleaned up.
    directory = tmp_path / "quarantine-fsync"
    real_fsync = os.fsync

    def failing_fsync(descriptor: int) -> None:
        raise OSError("injected fsync failure")

    os.fsync = failing_fsync
    try:
        with pytest.raises(BundleError, match="quarantine-not-durable"):
            materialize_quarantine(bundle=frozen, directory=directory)
    finally:
        os.fsync = real_fsync
    assert [p for p in directory.rglob("*.tmp")] == []

    # Unlink failure after a successful publish: never acknowledged.
    directory = tmp_path / "quarantine-unlink"
    real_unlink = os.unlink

    def failing_unlink(path: object, **kwargs: object) -> None:
        if isinstance(path, str) and path.endswith(".tmp"):
            raise OSError("injected unlink failure")
        real_unlink(path, **kwargs)  # type: ignore[arg-type]

    os.unlink = failing_unlink
    try:
        with pytest.raises(BundleError, match="quarantine-not-durable"):
            materialize_quarantine(bundle=frozen, directory=directory)
    finally:
        os.unlink = real_unlink
    # The first published target exists (its link succeeded), but the
    # operation was not acknowledged.
    assert (directory / "bundle-manifest.json").is_file()


def test_component_validation_failure_leaks_no_descriptor(tmp_path: Path) -> None:
    """A component directory that fails private-mode validation is
    refused and its descriptor closed: no fd leak per invalid open."""

    frozen = read_bundle_directory(bundle_dir=copy_bundle("serial", tmp_path / "bundle"))
    directory = tmp_path / "quarantine"
    directory.mkdir(mode=0o700)
    # A journals component with group permissions fails validation.
    (directory / "journals").mkdir(mode=0o755)

    def open_descriptors() -> set[int]:
        return set(os.listdir("/proc/self/fd"))

    before = open_descriptors()
    with pytest.raises(BundleError):
        materialize_quarantine(bundle=frozen, directory=directory)
    after = open_descriptors()
    assert before == after, f"descriptor leaked: {sorted(after - before)}"


def _failing_fsync_for(identity: tuple[int, int], real_fsync: Callable[[int], None]) -> Callable[[int], None]:
    """An fsync replacement that fails only for one directory identity."""

    def failing(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == identity:
            raise OSError("injected parent fsync failure")
        real_fsync(descriptor)

    return failing


def test_anchor_adoption_repairs_the_topmost_parent_entry(tmp_path: Path) -> None:
    """A failed parent fsync leaves the first ancestor present but
    unflushed; the retry that adopts it as its anchor must flush the
    topmost parent before anything is acknowledged (public helper)."""

    base = tmp_path / "base"
    base.mkdir(mode=0o700)
    base_identity = os.stat(base)
    real_fsync = os.fsync

    os.fsync = _failing_fsync_for((base_identity.st_dev, base_identity.st_ino), real_fsync)
    try:
        with pytest.raises(BundleError):
            ensure_private_directory(directory=base / "a")
    finally:
        os.fsync = real_fsync
    # The ancestor exists, but the entry holding it was never flushed.
    assert (base / "a").is_dir()

    synced_order: list[tuple[int, int]] = []

    def tracking_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        synced_order.append((metadata.st_dev, metadata.st_ino))
        real_fsync(descriptor)

    os.fsync = tracking_fsync
    try:
        ensure_private_directory(directory=base / "a" / "b" / "c")
    finally:
        os.fsync = real_fsync
    # The formerly missed topmost parent is flushed first — the repair
    # runs before the newly created descendants are used.
    assert synced_order[0] == (base_identity.st_dev, base_identity.st_ino)
    for created in (base / "a", base / "a" / "b"):
        identity = os.stat(created)
        assert (identity.st_dev, identity.st_ino) in synced_order
    assert (base / "a" / "b" / "c").is_dir()


def test_existing_digest_root_retry_syncs_quarantine_base(tmp_path: Path) -> None:
    """A failed quarantine-base fsync leaves the digest root present but
    unflushed; the retry through public materialization must flush the
    quarantine base before acknowledgement."""

    frozen = read_bundle_directory(bundle_dir=example_path("serial"))
    quarantine_base = tmp_path / "quarantine"
    quarantine_base.mkdir(mode=0o700)
    digest_root = quarantine_base / frozen.manifest_sha256
    base_identity = os.stat(quarantine_base)
    real_fsync = os.fsync

    os.fsync = _failing_fsync_for((base_identity.st_dev, base_identity.st_ino), real_fsync)
    try:
        with pytest.raises(BundleError):
            materialize_quarantine(bundle=frozen, directory=digest_root)
    finally:
        os.fsync = real_fsync
    # The digest root exists, but nothing was written into it.
    assert digest_root.is_dir()
    assert not any(digest_root.iterdir())

    synced_order: list[tuple[int, int]] = []

    def tracking_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        synced_order.append((metadata.st_dev, metadata.st_ino))
        real_fsync(descriptor)

    os.fsync = tracking_fsync
    try:
        materialize_quarantine(bundle=frozen, directory=digest_root)
    finally:
        os.fsync = real_fsync
    # The quarantine base repair runs first, before every content fsync
    # that precedes the acknowledgement.
    assert synced_order[0] == (base_identity.st_dev, base_identity.st_ino)
    assert (digest_root / "journals" / "main.jsonl").is_file()


def test_failed_repair_sync_never_acknowledges(dsn: str, test_schema: str, quarantine: Path, tmp_path: Path) -> None:
    """When the repair sync of the adopted root's parent fails too, the
    retry through the ingestor's materialization raises, leaves the
    receipt unacknowledged, and writes nothing into the digest root."""

    frozen = read_bundle_directory(bundle_dir=example_path("serial"))
    quarantine_base = tmp_path / "quarantine"
    quarantine_base.mkdir(mode=0o700)
    digest_root = quarantine_base / frozen.manifest_sha256
    base_identity = os.stat(quarantine_base)
    real_fsync = os.fsync

    os.fsync = _failing_fsync_for((base_identity.st_dev, base_identity.st_ino), real_fsync)
    try:
        # The digest root is created and left unflushed.
        with pytest.raises(BundleError):
            materialize_quarantine(bundle=frozen, directory=digest_root)
        assert digest_root.is_dir()
        # The retry goes through the ingest pipeline: constructing the
        # ingestor flushes the quarantine base's own parent (a different
        # directory, so the sabotage does not fire), and the delivery
        # reaches materialization with a durably recorded receipt.
        ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine_base)
        try:
            with pytest.raises(IngestError) as excinfo:
                ingestor.ingest_bundle(bundle=frozen, source=HostSourceContext(origin="host-repair"))
            assert "quarantine-not-durable" in str(excinfo.value)
            receipts = ingestor.list_receipts(execution_id=frozen.execution_id)
            assert len(receipts) == 1
            assert receipts[0].status == "received"
            assert receipts[0].events_committed == 0
        finally:
            ingestor.close()
        # Nothing was acknowledged into the digest root.
        assert not any(digest_root.iterdir())
    finally:
        os.fsync = real_fsync


def test_failed_anchor_repair_leaks_no_descriptor(tmp_path: Path) -> None:
    """A failed repair of an adopted anchor closes the anchor descriptor.

    The anchor branch (existing anchor, missing descendants) holds the
    anchor descriptor while repairing its parent; a failed repair must
    close it, not leak one descriptor per call. This is not the
    existing-root branch, which holds no descriptor during its repair."""

    base = tmp_path / "base"
    base.mkdir(mode=0o700)
    # The anchor exists before the call; the descendants do not.
    (base / "a").mkdir(mode=0o700)
    base_identity = os.stat(base)
    real_fsync = os.fsync

    os.fsync = _failing_fsync_for((base_identity.st_dev, base_identity.st_ino), real_fsync)
    try:

        def open_descriptors() -> set[int]:
            return set(os.listdir("/proc/self/fd"))

        before = open_descriptors()
        with pytest.raises(BundleError):
            ensure_private_directory(directory=base / "a" / "b" / "c")
        after = open_descriptors()
        assert before == after, f"descriptor leaked: {sorted(after - before)}"
    finally:
        os.fsync = real_fsync
    # The repair refused before descending: no descendant was created.
    assert not (base / "a" / "b").exists()


def _deep_journal_bundle(destination: Path) -> FrozenBundle:
    """A valid clone of the serial bundle whose journal lives one level
    deeper, at journals/deep/main.jsonl — a nested component path the
    frozen v1 contract accepts (the journal id stays the file stem)."""

    bundle = copy_bundle("serial", destination)
    (bundle / "journals" / "deep").mkdir()
    (bundle / "journals" / "main.jsonl").rename(bundle / "journals" / "deep" / "main.jsonl")
    manifest = load_json(bundle / MANIFEST_NAME)
    for entry in manifest["files"]:
        if entry["path"] == "journals/main.jsonl":
            entry["path"] = "journals/deep/main.jsonl"
    (bundle / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return read_bundle_directory(bundle_dir=bundle)


def _failing_fsync_for_path_suffix(suffix: str, real_fsync: Callable[[int], None]) -> Callable[[int], None]:
    """An fsync replacement that fails for directories whose open path
    ends with the suffix — the target directory is created mid-call, so
    its identity cannot be captured up front."""

    def failing(descriptor: int) -> None:
        try:
            path = os.readlink(f"/proc/self/fd/{descriptor}")
        except OSError:
            path = ""
        if path.endswith(suffix):
            raise OSError("injected component fsync failure")
        real_fsync(descriptor)

    return failing


def test_nested_component_adoption_repairs_the_missed_parent(tmp_path: Path) -> None:
    """Creating deep succeeds but syncing journals fails; the public retry
    adopts both directories and must flush journals — the parent holding
    deep — before acknowledging."""

    frozen = _deep_journal_bundle(tmp_path / "bundle")
    quarantine_base = tmp_path / "quarantine"
    quarantine_base.mkdir(mode=0o700)
    digest_root = quarantine_base / frozen.manifest_sha256
    real_fsync = os.fsync

    os.fsync = _failing_fsync_for_path_suffix("/journals", real_fsync)
    try:
        with pytest.raises(BundleError):
            materialize_quarantine(bundle=frozen, directory=digest_root)
    finally:
        os.fsync = real_fsync
    # Both component directories exist; the deep entry was never flushed
    # and no file was written inside it.
    assert (digest_root / "journals" / "deep").is_dir()
    assert not any((digest_root / "journals" / "deep").iterdir())

    synced_paths: list[str] = []

    def tracking_fsync(descriptor: int) -> None:
        try:
            synced_paths.append(os.readlink(f"/proc/self/fd/{descriptor}"))
        except OSError:
            synced_paths.append("")
        real_fsync(descriptor)

    os.fsync = tracking_fsync
    try:
        materialize_quarantine(bundle=frozen, directory=digest_root)
    finally:
        os.fsync = real_fsync
    # The formerly missed journals parent is flushed before the
    # acknowledgement, through the public retry alone.
    assert any(path.endswith("/journals") for path in synced_paths), sorted(synced_paths)
    assert (digest_root / "journals" / "deep" / "main.jsonl").is_file()


def test_failed_nested_component_repair_never_acks_and_leaks_no_descriptor(tmp_path: Path) -> None:
    """When the nested adoption repair fails too, the public retry raises
    quarantine-not-durable, acknowledges nothing, and leaks no
    descriptor."""

    frozen = _deep_journal_bundle(tmp_path / "bundle")
    quarantine_base = tmp_path / "quarantine"
    quarantine_base.mkdir(mode=0o700)
    digest_root = quarantine_base / frozen.manifest_sha256
    real_fsync = os.fsync
    failing = _failing_fsync_for_path_suffix("/journals", real_fsync)

    os.fsync = failing
    try:
        with pytest.raises(BundleError):
            materialize_quarantine(bundle=frozen, directory=digest_root)
        assert (digest_root / "journals" / "deep").is_dir()

        def open_descriptors() -> set[int]:
            return set(os.listdir("/proc/self/fd"))

        before = open_descriptors()
        with pytest.raises(BundleError) as excinfo:
            materialize_quarantine(bundle=frozen, directory=digest_root)
        after = open_descriptors()
        assert excinfo.value.code == "quarantine-not-durable"
        assert before == after, f"descriptor leaked: {sorted(after - before)}"
        # Nothing was acknowledged into the deep component.
        assert not any((digest_root / "journals" / "deep").iterdir())
    finally:
        os.fsync = real_fsync


def test_foreign_temporary_nonce_is_refused_and_never_deleted(tmp_path: Path) -> None:
    """A preexisting foreign file at the chosen temporary name makes the
    exclusive create refuse — and the refusal must not unlink the foreign
    entry, because a refusal does not establish ownership."""

    frozen = read_bundle_directory(bundle_dir=example_path("serial"))
    quarantine_base = tmp_path / "quarantine"
    quarantine_base.mkdir(mode=0o700)
    digest_root = quarantine_base / frozen.manifest_sha256
    digest_root.mkdir(mode=0o700)
    # The manifest is the first file materialized; its temporary name uses
    # the controlled nonce, so the foreign file collides with it.
    nonce = b"\x01\x02\x03\x04\x05\x06\x07\x08"
    foreign_bytes = b"foreign bytes that must survive the refusal"
    foreign = digest_root / f".bundle-manifest.json.{nonce.hex()}.tmp"
    foreign.write_bytes(foreign_bytes)
    real_urandom = os.urandom

    def controlled_urandom(size: int) -> bytes:
        return nonce[:size]

    os.urandom = controlled_urandom
    try:
        with pytest.raises(BundleError) as excinfo:
            materialize_quarantine(bundle=frozen, directory=digest_root)
    finally:
        os.urandom = real_urandom
    assert excinfo.value.code == "quarantine-not-writable"
    # The foreign entry is untouched, byte for byte, and nothing of this
    # call's was published.
    assert foreign.read_bytes() == foreign_bytes
    assert not (digest_root / "bundle-manifest.json").exists()


def test_quarantine_refuses_symlinked_roots_and_components(tmp_path: Path) -> None:
    """A symlinked quarantine root or component is refused: nothing
    outside the quarantine is ever chmodded or written."""

    frozen = read_bundle_directory(bundle_dir=copy_bundle("serial", tmp_path / "bundle"))
    external = tmp_path / "external"
    external.mkdir(mode=0o755)
    os.chmod(external, 0o755)
    link = tmp_path / "link-to-external"
    link.symlink_to(external, target_is_directory=True)
    with pytest.raises(BundleError, match="quarantine-root-not-private"):
        materialize_quarantine(bundle=frozen, directory=link)
    assert stat.S_IMODE(os.stat(external).st_mode) == 0o755
    assert list(external.iterdir()) == []

    root = tmp_path / "root"
    ensure_private_directory(directory=root)
    (root / "journals").symlink_to(external)
    with pytest.raises(BundleError):
        materialize_quarantine(bundle=frozen, directory=root)
    assert list(external.iterdir()) == []
    assert stat.S_IMODE(os.stat(external).st_mode) == 0o755


def test_quarantine_refuses_non_private_existing_targets(tmp_path: Path) -> None:
    frozen = read_bundle_directory(bundle_dir=copy_bundle("serial", tmp_path / "bundle"))
    directory = tmp_path / "quarantine"
    directory.mkdir(mode=0o700)
    (directory / "journals").mkdir(mode=0o700)
    target = directory / "journals" / "main.jsonl"
    target.write_bytes(b"x")
    os.chmod(target, 0o644)
    with pytest.raises(BundleError, match="quarantine-conflict"):
        materialize_quarantine(bundle=frozen, directory=directory)

    os.chmod(target, 0o600)
    alias = directory / "journals" / "alias.jsonl"
    alias.hardlink_to(target)
    with pytest.raises(BundleError, match="quarantine-conflict"):
        materialize_quarantine(bundle=frozen, directory=directory)


def test_quarantine_concurrent_materialization_is_exclusive(tmp_path: Path) -> None:
    frozen = read_bundle_directory(bundle_dir=copy_bundle("serial", tmp_path / "bundle"))
    directory = tmp_path / "quarantine"
    errors: list[Exception] = []

    def materialize() -> None:
        try:
            materialize_quarantine(bundle=frozen, directory=directory)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=materialize) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, [type(error).__name__ for error in errors]
    assert (directory / MANIFEST_NAME).read_text(encoding="utf-8") == frozen.manifest_text
    leftovers = [path for path in directory.rglob("*") if path.name.endswith(".tmp")]
    assert leftovers == []


# Receipts, proofs, and query helpers


def test_unverified_delivery_is_quarantined_with_zero_canonical_writes(
    dsn: str, test_schema: str, quarantine: Path, tmp_path: Path
) -> None:
    bundle_dir = reidentify_bundle(tmp_path / "quarantined", execution_id="exec-quarantined")
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        result = ingestor.ingest_delivery(
            delivery=BundleDirectory(path=bundle_dir), source=HostSourceContext(origin="host-q")
        )
        assert result.status == "quarantined"
        assert result.quarantine_reason == "no-metadata"
        assert result.events_appended == 0
        receipt = ingestor.get_receipt(receipt_id=result.receipt_id)
        assert receipt is not None
        assert receipt.status == "quarantined"
        assert receipt.redaction_state == "unverified-claim"
        assert receipt.events_committed == 0
        assert receipt.execution_id == "exec-quarantined"
        rows = direct_fetch(
            dsn,
            test_schema,
            "SELECT count(*) FROM {schema}.ledger_event WHERE execution_id = %s",
            ("exec-quarantined",),
        )
        assert rows == (0,)
        execution = direct_fetch(
            dsn,
            test_schema,
            "SELECT count(*) FROM {schema}.ledger_execution WHERE execution_id = %s",
            ("exec-quarantined",),
        )
        assert execution == (0,)
        # The bytes are durably retained even though nothing was published.
        assert (quarantine / receipt.quarantine_dir / "journals" / "main.jsonl").is_file()
    finally:
        ingestor.close()


def test_same_source_proof_promotion_publishes(dsn: str, test_schema: str, quarantine: Path, tmp_path: Path) -> None:
    """Redelivering identical bytes from the same source with a full proof
    promotes the quarantined receipt; returned and persisted states
    agree, verified through a fresh connection."""

    bundle_dir = reidentify_bundle(tmp_path / "promotion", execution_id="exec-promotion")
    frozen = frozen_bundle(bundle_dir)
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        first = ingestor.ingest_delivery(
            delivery=BundleDirectory(path=bundle_dir), source=HostSourceContext(origin="host-promote")
        )
        assert first.status == "quarantined"
        second = ingestor.ingest_delivery(
            delivery=BundleDirectory(path=bundle_dir),
            source=HostSourceContext(origin="host-promote"),
            github=verified_evidence(job_id=458900100, expected_job_id=458900100),
            redaction_attestation=full_proof(bundle=frozen),
        )
        assert second.status == "committed"
        assert second.receipt_id == first.receipt_id
        assert second.events_appended == frozen.total_records
        assert second.redaction_state == "attested"
        receipt = ingestor.get_receipt(receipt_id=first.receipt_id)
        assert receipt is not None
        assert receipt.status == "committed"
        assert receipt.redaction_state == "attested"
        stored = direct_fetch(
            dsn, test_schema, "SELECT status FROM {schema}.source_receipt WHERE receipt_id = %s", (first.receipt_id,)
        )
        assert stored == ("committed",)
        proofs = ingestor.list_redaction_proofs(receipt_id=first.receipt_id)
        assert len(proofs) == 1
        assert proofs[0].covered_all is True
        assert proofs[0].policy == "test-sanitizer"
    finally:
        ingestor.close()


def test_partial_coverage_does_not_publish(dsn: str, test_schema: str, quarantine: Path, tmp_path: Path) -> None:
    bundle_dir = reidentify_bundle(tmp_path / "partial-proof", execution_id="exec-partial-proof")
    frozen = frozen_bundle(bundle_dir)
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        result = ingestor.ingest_delivery(
            delivery=BundleDirectory(path=bundle_dir),
            source=HostSourceContext(origin="host-pp"),
            github=verified_evidence(job_id=458900101, expected_job_id=458900101),
            redaction_attestation=RedactionAttestation(
                policy="test-sanitizer", covered_digests=(frozen.files[0].sha256,)
            ),
        )
        assert result.status == "quarantined"
        assert result.quarantine_reason == "redaction-unproven"
        assert result.redaction_state == "partially-attested"
        receipt = ingestor.get_receipt(receipt_id=result.receipt_id)
        assert receipt is not None
        assert receipt.status == "quarantined"
        assert receipt.redaction_state == "partially-attested"
        rows = direct_fetch(
            dsn,
            test_schema,
            "SELECT count(*) FROM {schema}.ledger_event WHERE execution_id = %s",
            ("exec-partial-proof",),
        )
        assert rows == (0,)
    finally:
        ingestor.close()


def test_receipt_deduplicates_and_shares_execution_across_hosts(
    dsn: str, test_schema: str, quarantine: Path, tmp_path: Path
) -> None:
    bundle_dir = reidentify_bundle(tmp_path / "dedup", execution_id="exec-receipt-dedup")
    frozen = frozen_bundle(bundle_dir)
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        first = publish(ingestor, bundle_dir, origin="host-a", job_id=458900102)
        second = publish(ingestor, bundle_dir, origin="host-a", job_id=458900102)
        assert second.receipt_id == first.receipt_id
        assert second.status == "committed"
        assert second.events_appended == 0
        assert second.events_duplicates == first.events_appended
        other_host = publish(ingestor, bundle_dir, origin="host-b", job_id=458900102)
        assert other_host.receipt_id != first.receipt_id
        assert other_host.events_duplicates == frozen.total_records
        rows = ingestor.list_receipts(execution_id="exec-receipt-dedup")
        assert len(rows) == 2
        assert all(row.status == "committed" for row in rows)
    finally:
        ingestor.close()


def test_receipt_guards_reject_history_changes(dsn: str, test_schema: str, quarantine: Path, tmp_path: Path) -> None:
    bundle_dir = reidentify_bundle(tmp_path / "guard", execution_id="exec-guard")
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        result = publish(ingestor, bundle_dir, origin="host-guard", job_id=458900103)
        receipt_id = result.receipt_id
    finally:
        ingestor.close()
    for statement in (
        "UPDATE {schema}.source_receipt SET manifest_sha256 = repeat('f', 64) WHERE receipt_id = %s",
        "UPDATE {schema}.source_receipt SET status = 'received' WHERE receipt_id = %s",
        "UPDATE {schema}.source_receipt SET events_committed = 0 WHERE receipt_id = %s",
        "DELETE FROM {schema}.source_receipt WHERE receipt_id = %s",
    ):
        with psycopg.connect(dsn, autocommit=True) as conn, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute(sql.SQL(statement).format(schema=sql.Identifier(test_schema)), (receipt_id,))
    with psycopg.connect(dsn, autocommit=True) as conn, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        conn.execute(
            sql.SQL("DELETE FROM {schema}.source_execution WHERE execution_id = %s").format(
                schema=sql.Identifier(test_schema)
            ),
            ("exec-guard",),
        )
    with psycopg.connect(dsn, autocommit=True) as conn, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        conn.execute(
            sql.SQL("UPDATE {schema}.source_github_observation SET publication_authorized = false").format(
                schema=sql.Identifier(test_schema)
            )
        )
    with psycopg.connect(dsn, autocommit=True) as conn, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        conn.execute(
            sql.SQL("DELETE FROM {schema}.source_redaction_proof WHERE receipt_id = %s").format(
                schema=sql.Identifier(test_schema)
            ),
            (receipt_id,),
        )


def test_query_helpers_validate_input(dsn: str, test_schema: str, quarantine: Path) -> None:
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        with pytest.raises(ledger.ValidationError):
            ingestor.get_receipt(receipt_id=0)
        with pytest.raises(ledger.ValidationError):
            ingestor.list_receipts(status="nonsense")
        with pytest.raises(ledger.ValidationError):
            ingestor.list_receipts(repository="not-a-repo")
        with pytest.raises(ledger.ValidationError):
            ingestor.list_receipts(limit=0)
        with pytest.raises(ledger.ValidationError):
            ingestor.list_receipts(offset=-1)
        with pytest.raises(ledger.ValidationError):
            ingestor.list_observations()
        with pytest.raises(ledger.ValidationError):
            ingestor.list_redaction_proofs(receipt_id=0)
        listed = ingestor.list_receipts(limit=5)
        assert len(listed) <= 5
        for row in listed:
            assert row.status in ("received", "partial", "committed", "quarantined", "failed")
    finally:
        ingestor.close()


def test_unreachable_database_fails_value_free(tmp_path: Path) -> None:
    ingestor = Ingestor(dsn=UNREACHABLE_DSN, schema="ci_observability", quarantine_root=tmp_path / "q")
    try:
        frozen = read_bundle_directory(bundle_dir=example_path("serial"))
        with pytest.raises(IngestError) as excinfo:
            ingestor.ingest_bundle(bundle=frozen, source=HostSourceContext(origin="host-x"))
        assert excinfo.value.code == "database-error"
        assert "postgresql" not in str(excinfo.value)
    finally:
        ingestor.close()


def test_source_context_and_ingestor_input_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ArtifactSourceContext(repository="not-a-repo", run_id=1, artifact_name="bundle")
    with pytest.raises(ValueError):
        ArtifactSourceContext(repository="osac-project/osac", run_id=0, artifact_name="bundle")
    with pytest.raises(ValueError):
        ArtifactSourceContext(repository="osac-project/osac", run_id=1, artifact_name="")
    with pytest.raises(ValueError):
        ArtifactSourceContext(repository="osac-project/osac", run_id=1, artifact_name="b", artifact_id=0)
    with pytest.raises(ValueError):
        ArtifactSourceContext(repository="osac-project/osac", run_id=1, artifact_name="b", run_attempt=0)
    with pytest.raises(ValueError):
        HostSourceContext(origin="")
    with pytest.raises(ValueError):
        Ingestor(dsn="postgresql://x", schema="BAD-SCHEMA", quarantine_root=tmp_path)
    with pytest.raises(ValueError):
        Ingestor(dsn="postgresql://x", quarantine_root=tmp_path, connect_timeout_seconds=0)
    with pytest.raises(ValueError):
        Ingestor(dsn="postgresql://x", quarantine_root=tmp_path, statement_timeout_ms=0)
    with pytest.raises(ValueError):
        Ingestor(dsn="postgresql://x", quarantine_root=tmp_path, lock_timeout_ms=0)
    with pytest.raises(ValueError):
        RedactionAttestation(policy="p", covered_digests=("not-hex",))
    with pytest.raises(ValueError):
        RedactionAttestation(policy="", covered_digests=())


def test_ingest_rejects_wrong_source_kind_combination(
    tmp_path: Path, dsn: str, test_schema: str, quarantine: Path
) -> None:
    bundle = copy_bundle("serial", tmp_path / "kind")
    archive = tmp_path / "bundle.zip"
    digest = zip_bundle(bundle, archive)
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        with pytest.raises(IngestError, match="source-kind-mismatch"):
            ingestor.ingest_delivery(
                delivery=BundleArchive(path=archive, received_digest=digest), source=HostSourceContext(origin="h")
            )
        with pytest.raises(IngestError, match="source-kind-mismatch"):
            ingestor.ingest_delivery(
                delivery=BundleDirectory(path=bundle),
                source=ArtifactSourceContext(repository="osac-project/osac", run_id=1, artifact_name="bundle"),
            )
    finally:
        ingestor.close()


# Source privacy: the reader role serves ledger tables only.


def test_reader_role_cannot_read_source_tables(dsn: str) -> None:
    """A reader role granted SELECT the way the W05 bootstrap does (default
    privileges over the schema) can read the ledger tables but not the
    source tables: the migration's revoke holds."""

    schema = f"ci_obs_w01b_reader_{os.getpid()}"
    role = f"ci_obs_w01b_reader_role_{os.getpid()}"
    reader_dsn = make_conninfo(dsn, user=role)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
            # Mirror the deployment: a dedicated schema owned by the ingest
            # role, with default privileges granting the reader SELECT on
            # every table created in it.
            conn.execute(sql.SQL("CREATE SCHEMA {} AUTHORIZATION ci_observability").format(sql.Identifier(schema)))
            conn.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE ci_observability IN SCHEMA {schema}"
                    " GRANT SELECT ON TABLES TO {role}"
                ).format(schema=sql.Identifier(schema), role=sql.Identifier(role))
            )
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
        assert ledger.apply_migrations(dsn=dsn, schema=schema) == [1, 2, 3]
        with psycopg.connect(reader_dsn, autocommit=True) as reader:
            reader.execute(
                sql.SQL("SELECT count(*) FROM {}.ledger_execution").format(sql.Identifier(schema))
            ).fetchone()
            reader.execute(
                sql.SQL("SELECT count(*) FROM {}.schema_migrations").format(sql.Identifier(schema))
            ).fetchone()
            for table in ("source_receipt", "source_execution", "source_github_observation", "source_redaction_proof"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    reader.execute(
                        sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table))
                    ).fetchone()
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def test_reader_isolation_covers_public_column_and_inherited_grants(dsn: str) -> None:
    """The migration's revoke handles the grant shapes the table-grant
    view misses: PUBLIC (a keyword, not a quoted role), column-level
    SELECT grants, and access inherited through role membership — while
    ledger tables stay readable and the migration completes."""

    schema = f"ci_obs_w01b_acl_{os.getpid()}"
    grantee = f"ci_obs_w01b_acl_grantee_{os.getpid()}"
    member = f"ci_obs_w01b_acl_member_{os.getpid()}"
    member_dsn = make_conninfo(dsn, user=member)
    migration_text = (Path(ledger.__file__).parent / "migrations" / "0002_sources.sql").read_text()
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(grantee)))
            # member inherits everything granted to grantee.
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(member)))
            conn.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(grantee), sql.Identifier(member)))
            conn.execute(sql.SQL("CREATE SCHEMA {} AUTHORIZATION ci_observability").format(sql.Identifier(schema)))
            # Hostile default privileges: every table the migration
            # creates in this schema is readable by PUBLIC and by grantee
            # (whose access member inherits).
            conn.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE ci_observability IN SCHEMA {schema}"
                    " GRANT SELECT ON TABLES TO PUBLIC"
                ).format(schema=sql.Identifier(schema))
            )
            conn.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE ci_observability IN SCHEMA {schema}"
                    " GRANT SELECT ON TABLES TO {role}"
                ).format(schema=sql.Identifier(schema), role=sql.Identifier(grantee))
            )
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO PUBLIC").format(sql.Identifier(schema)))
        # The migration completes with the hostile grants attached to its
        # own tables, and its revoke clears the PUBLIC keyword case and
        # the inherited direct grant.
        assert ledger.apply_migrations(dsn=dsn, schema=schema) == [1, 2, 3]
        with psycopg.connect(member_dsn, autocommit=True) as reader:
            for table in ("source_receipt", "source_github_observation"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    reader.execute(
                        sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table))
                    ).fetchone()
            reader.execute(
                sql.SQL("SELECT count(*) FROM {}.ledger_execution").format(sql.Identifier(schema))
            ).fetchone()
        # A column-level SELECT grant is invisible to the table-grant
        # view: re-running the migration's own SQL (idempotent) must
        # revoke it too.
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                sql.SQL("GRANT SELECT (receipt_id) ON {schema}.source_execution TO {role}").format(
                    schema=sql.Identifier(schema), role=sql.Identifier(grantee)
                )
            )
            conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            conn.execute(migration_text)
        with (
            psycopg.connect(member_dsn, autocommit=True) as reader,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            reader.execute(
                sql.SQL("SELECT receipt_id FROM {}.source_execution LIMIT 1").format(sql.Identifier(schema))
            ).fetchone()
        with psycopg.connect(dsn, autocommit=True) as conn:
            remaining = conn.execute(
                sql.SQL(
                    "SELECT count(*) FROM information_schema.role_table_grants"
                    " WHERE table_schema = %s AND table_name IN ('source_receipt','source_execution',"
                    "'source_github_observation','source_redaction_proof')"
                    " AND privilege_type = 'SELECT' AND grantee <> current_user"
                ),
                (schema,),
            ).fetchone()
            assert remaining == (0,)
            remaining_columns = conn.execute(
                sql.SQL(
                    "SELECT count(*) FROM information_schema.role_column_grants"
                    " WHERE table_schema = %s AND table_name IN ('source_receipt','source_execution',"
                    "'source_github_observation','source_redaction_proof')"
                    " AND privilege_type = 'SELECT' AND grantee <> current_user"
                ),
                (schema,),
            ).fetchone()
            assert remaining_columns == (0,)
        assert ledger.apply_migrations(dsn=dsn, schema=schema) == []
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(member)))
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(grantee)))
