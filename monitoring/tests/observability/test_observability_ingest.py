"""End-to-end ingestion tests on real PostgreSQL and the real filesystem
(W01b): the publication gate over every example bundle fixture, canonical
event prefixes through the ledger API, prefix growth, races, partial
multi-batch recovery, second-connection acknowledgements, and the freeze
guarantee that filesystem swaps after the freeze cannot change what is
stored. Quarantined deliveries leave nothing in the canonical store or
the existing read API.

Shared builders live in test_observability_sources.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import urllib.request
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import cast

import pytest

try:
    import psycopg
except ModuleNotFoundError:
    if os.environ.get("CI_OBSERVABILITY_TEST_DSN", "").strip():
        raise
    pytest.skip("psycopg is not installed and CI_OBSERVABILITY_TEST_DSN is not set", allow_module_level=True)

from contract_helpers import MANIFEST_NAME, VALID_BUNDLES, load_json, rewrite_journal
from psycopg import sql
from test_observability_sources import (
    bulk_bundle,
    direct_fetch,
    frozen_bundle,
    full_proof,
    job_record,
    mutate_record,
    read_journal_records,
    reidentify_bundle,
    run_record,
)

from monitoring.ci_observability import api, ledger
from monitoring.ci_observability.bundle_reader import BundleDirectory, freeze
from monitoring.ci_observability.ingest import HostSourceContext, IngestError, Ingestor, IngestResult
from monitoring.ci_observability.reconcile import GitHubEvidence

_job_ids = iter(range(4588400000, 4588500000))


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
    name = f"ci_obs_w01b_ingest_{os.getpid()}"
    assert ledger.apply_migrations(dsn=dsn, schema=name) == [1, 2, 3]
    yield name
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


@pytest.fixture
def quarantine(tmp_path: Path) -> Path:
    return tmp_path / "quarantine"


def _reidentify_job(bundle_dir: Path, job_id: int) -> None:
    """Point a clone's claimed job id (and its echoes) at a fresh id."""

    manifest = load_json(bundle_dir / MANIFEST_NAME)
    manifest["execution"]["job"]["github_job_id"] = job_id
    (bundle_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    for entry in manifest["files"]:
        if entry["kind"] != "journal":
            continue
        records = read_journal_records(bundle_dir, entry["path"])
        for record in records:
            if record["event"] == "execution_started":
                record["data"]["job"] = manifest["execution"]["job"]
        rewrite_journal(bundle_dir, entry["path"], records)


def unique_job_id() -> int:
    return next(_job_ids)


def example_evidence(manifest: Mapping[str, object]) -> GitHubEvidence:
    """Evidence whose trusted records verify the job an example bundle's
    manifest claims, using the manifest's own repository identity."""

    execution = cast(Mapping[str, object], manifest["execution"])
    job = cast(Mapping[str, object], execution["job"])
    claimed_id = job.get("github_job_id")
    effective = claimed_id if isinstance(claimed_id, int) else unique_job_id()
    return GitHubEvidence(
        run=cast(
            Mapping[str, object],
            run_record(
                cast(int, execution["run_id"]),
                cast(int, execution["run_attempt"]),
                repository=cast(str, execution["repository"]),
                repository_id=cast(int, execution["repository_id"]),
            ),
        ),
        jobs=(
            cast(
                Mapping[str, object],
                job_record(
                    effective,
                    cast(int, execution["run_id"]),
                    cast(int, execution["run_attempt"]),
                    cast(str, job.get("job_name", "job")),
                ),
            ),
        ),
        expected_job_id=effective,
    )


# The example fixtures, end to end


def test_every_example_bundle_publishes_with_full_proof(
    dsn: str, test_schema: str, quarantine: Path, tmp_path: Path
) -> None:
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        for bundle_dir in VALID_BUNDLES:
            frozen = frozen_bundle(bundle_dir)
            result = ingestor.ingest_bundle(
                bundle=frozen,
                source=HostSourceContext(origin="host-e2e"),
                github=example_evidence(frozen.manifest),
                redaction_attestation=full_proof(bundle=frozen),
            )
            assert result.status == "committed", bundle_dir.name
            assert result.redaction_state == "attested"
            receipt = ingestor.get_receipt(receipt_id=result.receipt_id)
            assert receipt is not None
            assert receipt.status == "committed"
            assert receipt.events_committed == receipt.total_records
            assert receipt.quarantine_dir == receipt.manifest_sha256
            events = direct_fetch(
                dsn,
                test_schema,
                "SELECT coalesce(sum(prefix_sequence), 0) FROM {schema}.ledger_journal WHERE execution_id = %s",
                (result.execution_id,),
            )
            assert events == (receipt.total_records,)
            assert (quarantine / receipt.quarantine_dir).is_dir()
    finally:
        ingestor.close()


def test_every_example_bundle_quarantines_without_proof(
    dsn: str, test_schema: str, quarantine: Path, tmp_path: Path
) -> None:
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        for bundle_source in VALID_BUNDLES:
            # A fresh clone per example: the publication sweep in this
            # module already created executions for the fixture ids.
            bundle_dir = reidentify_bundle(
                tmp_path / f"quarantined-{bundle_source.name}",
                source_name=bundle_source.name,
                execution_id=f"quarantined-{bundle_source.name}",
            )
            result = ingestor.ingest_delivery(
                delivery=BundleDirectory(path=bundle_dir), source=HostSourceContext(origin="host-e2e-quarantine")
            )
            assert result.status == "quarantined", bundle_source.name
            assert result.quarantine_reason == "no-metadata"
            assert result.events_appended == 0
            with psycopg.connect(dsn, autocommit=True) as conn:
                executions = conn.execute(
                    sql.SQL("SELECT count(*) FROM {schema}.ledger_execution WHERE execution_id = %s").format(
                        schema=sql.Identifier(test_schema)
                    ),
                    (result.execution_id,),
                ).fetchone()
            assert executions == (0,), bundle_source.name
    finally:
        ingestor.close()


def test_quarantined_secret_never_reaches_canonical_storage_or_the_read_api(
    dsn: str, test_schema: str, quarantine: Path, tmp_path: Path
) -> None:
    """An unredacted producer value in a quarantined delivery appears in
    no canonical table and is absent from the existing read API."""

    secret = "SECRETTOKEN-unredacted-producer-value"
    bundle_dir = reidentify_bundle(tmp_path / "unverified", execution_id="review-unverified")
    manifest_path = bundle_dir / MANIFEST_NAME
    manifest = load_json(manifest_path)
    manifest["suite"]["variant"] = secret
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    records = read_journal_records(bundle_dir, "journals/main.jsonl")
    cast(dict[str, object], records[0]["data"])["suite"] = manifest["suite"]  # type: ignore[arg-type]
    rewrite_journal(bundle_dir, "journals/main.jsonl", records)

    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        result = ingestor.ingest_delivery(
            delivery=BundleDirectory(path=bundle_dir), source=HostSourceContext(origin="parent-review-host")
        )
        assert result.status == "quarantined"
        assert result.events_appended == 0
        for table, column in (
            ("ledger_event", "data::text"),
            ("ledger_execution", "execution_id"),
            ("ledger_journal", "execution_id"),
        ):
            with psycopg.connect(dsn, autocommit=True) as conn:
                found = conn.execute(
                    sql.SQL("SELECT count(*) FROM {schema}.{table} WHERE {column} LIKE %s").format(
                        schema=sql.Identifier(test_schema), table=sql.Identifier(table), column=sql.SQL(column)
                    ),
                    (f"%{secret}%",),
                ).fetchone()
            assert found == (0,), table
    finally:
        ingestor.close()

    server = api.ReadAPIServer(dsn=dsn, schema=test_schema, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(
                f"http://127.0.0.1:{server.port}/api/v1/executions/review-unverified/events", timeout=10
            )
        assert excinfo.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(f"http://127.0.0.1:{server.port}/api/v1/executions/review-unverified", timeout=10)
        assert excinfo.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_all_schema_fields_roundtrip(dsn: str, test_schema: str, quarantine: Path, tmp_path: Path) -> None:
    """Every envelope field survives publication exactly: the original
    timestamp text with nine fractional digits, source_at, invocation and
    clock identity, monotonic and uncertainty values, and payload
    equality."""

    bundle_dir = reidentify_bundle(
        tmp_path / "roundtrip", execution_id="exec-roundtrip", source_name="unit-ginkgo", github_job_id=unique_job_id()
    )
    records = read_journal_records(bundle_dir, "journals/ginkgo-run-1.jsonl")
    records[2]["recorded_at"] = "2026-10-28T00:40:12.123456789Z"
    records[2]["source_at"] = "2026-10-28T00:40:11.000000001Z"
    records[2]["clock_uncertainty_ns"] = 42
    rewrite_journal(bundle_dir, "journals/ginkgo-run-1.jsonl", records)
    frozen = frozen_bundle(bundle_dir)
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        result = ingestor.ingest_bundle(
            bundle=frozen,
            source=HostSourceContext(origin="host-r"),
            github=example_evidence(frozen.manifest),
            redaction_attestation=full_proof(bundle=frozen),
        )
        assert result.status == "committed"
        client = ledger.Ledger(dsn=dsn, schema=test_schema)
        try:
            stored = client.list_events(execution_id=result.execution_id, journal_id="ginkgo-run-1", limit=100)
        finally:
            client.close()
        expected = read_journal_records(bundle_dir, "journals/ginkgo-run-1.jsonl")
        assert len(stored) == len(expected)
        stored_by_sequence = {event.sequence: event for event in stored}
        modified = stored_by_sequence[3]
        assert modified.recorded_at == "2026-10-28T00:40:12.123456789Z"
        assert modified.source_at == "2026-10-28T00:40:11.000000001Z"
        assert modified.invocation_id == expected[2]["invocation_id"]
        assert modified.clock_uncertainty_ns == 42
        for record in expected:
            matching = stored_by_sequence[record["sequence"]]
            assert matching.schema_version == record["schema_version"]
            assert matching.execution_id == record["execution_id"]
            assert matching.journal_id == record["journal_id"]
            assert matching.sequence == record["sequence"]
            assert matching.recorded_at == record["recorded_at"]
            assert matching.source_at == record.get("source_at")
            assert matching.invocation_id == record.get("invocation_id")
            assert matching.clock_id == record.get("clock_id")
            assert matching.clock_uncertainty_ns == record.get("clock_uncertainty_ns")
            assert matching.mono_ns == record.get("mono_ns")
            assert matching.event == record["event"]
            assert matching.data == record["data"]
    finally:
        ingestor.close()


def test_publication_writes_through_the_ledger_surface(
    dsn: str, test_schema: str, quarantine: Path, tmp_path: Path
) -> None:
    """Events land through the ledger API: canonical payload hashes, journal
    checkpoints, and contiguous prefixes, never hand-inserted rows."""

    bundle_dir = reidentify_bundle(
        tmp_path / "surface", execution_id="exec-surface", source_name="parallel", github_job_id=unique_job_id()
    )
    frozen = frozen_bundle(bundle_dir)
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        result = ingestor.ingest_bundle(
            bundle=frozen,
            source=HostSourceContext(origin="host-s"),
            github=example_evidence(frozen.manifest),
            redaction_attestation=full_proof(bundle=frozen),
        )
        journals = direct_fetch(
            dsn,
            test_schema,
            "SELECT count(*), count(*) FILTER (WHERE prefix_sequence > 0) FROM {schema}.ledger_journal"
            " WHERE execution_id = %s",
            (result.execution_id,),
        )
        assert journals is not None
        assert journals[0] == 3  # controller, gw0, gw1
        assert journals[1] == 3
        hashed = direct_fetch(
            dsn,
            test_schema,
            "SELECT count(*) FROM {schema}.ledger_event WHERE execution_id = %s AND data_sha256 ~ '^[0-9a-f]{{64}}$'",
            (result.execution_id,),
        )
        assert hashed == (result.events_appended,)
        prefixes = direct_fetch(
            dsn,
            test_schema,
            "SELECT min(prefix_sequence), max(prefix_sequence) FROM {schema}.ledger_journal WHERE execution_id = %s",
            (result.execution_id,),
        )
        assert prefixes is not None and prefixes[0] > 0
    finally:
        ingestor.close()


# Prefix growth, staleness, and conflicts


def test_partial_then_full_then_stale_replay(dsn: str, test_schema: str, quarantine: Path, tmp_path: Path) -> None:
    """A valid shorter prefix publishes, its longer continuation with the
    same verified job extends it, and a stale shorter replay acknowledges
    its own delivered keys without duplicate counts."""

    job = unique_job_id()
    full = reidentify_bundle(tmp_path / "late-full", execution_id="review-late", github_job_id=job)
    partial = tmp_path / "late-partial"
    partial.mkdir()
    for path in full.rglob("*"):
        if path.is_file():
            target = partial / path.relative_to(full)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    manifest = load_json(partial / MANIFEST_NAME)
    manifest["completeness"].update({"complete": False, "end_reason": "interrupted"})
    manifest["files"][0]["complete"] = False
    (partial / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    records = read_journal_records(partial, "journals/main.jsonl")
    rewrite_journal(partial, "journals/main.jsonl", records[:10])

    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        evidence = GitHubEvidence(
            run=cast(Mapping[str, object], run_record(1122334455, 1)),
            jobs=(cast(Mapping[str, object], job_record(job, 1122334455, 1, "e2e-bmaas")),),
            expected_job_id=job,
        )
        first = ingestor.ingest_delivery(
            delivery=BundleDirectory(path=partial),
            source=HostSourceContext(origin="host-late"),
            github=evidence,
            redaction_attestation=full_proof(bundle=frozen_bundle(partial)),
        )
        assert first.status == "committed"
        assert first.events_appended == 10
        second = ingestor.ingest_delivery(
            delivery=BundleDirectory(path=full),
            source=HostSourceContext(origin="host-late"),
            github=evidence,
            redaction_attestation=full_proof(bundle=frozen_bundle(full)),
        )
        assert second.status == "committed"
        assert second.receipt_id != first.receipt_id
        assert second.events_appended == 8
        assert second.events_duplicates == 10
        stale = ingestor.ingest_delivery(
            delivery=BundleDirectory(path=partial),
            source=HostSourceContext(origin="host-late"),
            github=evidence,
            redaction_attestation=full_proof(bundle=frozen_bundle(partial)),
        )
        assert stale.status == "committed"
        assert stale.receipt_id == first.receipt_id
        assert stale.events_appended == 0
        assert stale.events_duplicates == 10
        receipt = ingestor.get_receipt(receipt_id=stale.receipt_id)
        assert receipt is not None
        assert receipt.status == "committed"
        assert receipt.events_committed == 10
        events = direct_fetch(
            dsn, test_schema, "SELECT count(*) FROM {schema}.ledger_event WHERE execution_id = %s", ("review-late",)
        )
        assert events == (18,)
    finally:
        ingestor.close()


def test_continuation_with_new_journal_and_native_file(
    dsn: str, test_schema: str, quarantine: Path, tmp_path: Path
) -> None:
    """A continuation may add a new journal and a new native file for the
    same stable execution; both land beside the extended prefix."""

    job = unique_job_id()
    base = reidentify_bundle(tmp_path / "grow-base", execution_id="review-grow", github_job_id=job)
    _reidentify_job(base, job)
    grown = tmp_path / "grow-full"
    grown.mkdir()
    for path in base.rglob("*"):
        if path.is_file():
            target = grown / path.relative_to(base)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    # A new journal with four records that do not disturb the bundle-wide
    # collection counts: a worker start/stop pair and two observations.
    extra_records = []
    for sequence, (event, data) in enumerate(
        (
            ("worker_started", {"worker_id": "gw9", "host": "runner-9", "pid": 4242}),
            ("worker_stopped", {"worker_id": "gw9", "outcome": "completed"}),
            (
                "resource_observed",
                {"resource": {"kind": "Pod", "uid": "9" * 36, "cluster_uid": "8" * 36, "cluster_role": "management"}},
            ),
            (
                "resource_observed",
                {
                    "resource": {"kind": "Pod", "uid": "7" * 36, "cluster_uid": "8" * 36, "cluster_role": "management"},
                    "deleted": True,
                },
            ),
        ),
        start=1,
    ):
        extra_records.append(
            {
                "schema": "osac.ci-observability.journal-record",
                "schema_version": 1,
                "execution_id": "review-grow",
                "journal_id": "gw9",
                "sequence": sequence,
                "recorded_at": "2026-10-27T06:00:00Z",
                "mono_ns": 100 + sequence,
                "clock_id": "main",
                "event": event,
                "data": data,
            }
        )
    (grown / "journals" / "gw9.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in extra_records), encoding="utf-8"
    )
    # A new native file, opaque.
    (grown / "native-report" / "extra.xml").write_text("<extra/>", encoding="utf-8")
    manifest = load_json(grown / MANIFEST_NAME)
    manifest["files"].append(
        {
            "path": "journals/gw9.jsonl",
            "kind": "journal",
            "sha256": hashlib.sha256((grown / "journals" / "gw9.jsonl").read_bytes()).hexdigest(),
            "size_bytes": (grown / "journals" / "gw9.jsonl").stat().st_size,
            "record_count": 4,
            "complete": True,
        }
    )
    manifest["files"].append(
        {
            "path": "native-report/extra.xml",
            "kind": "native-report",
            "sha256": hashlib.sha256((grown / "native-report" / "extra.xml").read_bytes()).hexdigest(),
            "size_bytes": (grown / "native-report" / "extra.xml").stat().st_size,
        }
    )
    (grown / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        evidence = GitHubEvidence(
            run=cast(Mapping[str, object], run_record(1122334455, 1)),
            jobs=(cast(Mapping[str, object], job_record(job, 1122334455, 1, "e2e-bmaas")),),
            expected_job_id=job,
        )
        first = ingestor.ingest_delivery(
            delivery=BundleDirectory(path=base),
            source=HostSourceContext(origin="host-grow"),
            github=evidence,
            redaction_attestation=full_proof(bundle=frozen_bundle(base)),
        )
        assert first.status == "committed"
        second = ingestor.ingest_delivery(
            delivery=BundleDirectory(path=grown),
            source=HostSourceContext(origin="host-grow"),
            github=evidence,
            redaction_attestation=full_proof(bundle=frozen_bundle(grown)),
        )
        assert second.status == "committed"
        assert second.events_appended == 4
        journals = direct_fetch(
            dsn, test_schema, "SELECT count(*) FROM {schema}.ledger_journal WHERE execution_id = %s", ("review-grow",)
        )
        assert journals == (2,)
        native = quarantine / frozen_bundle(grown).manifest_sha256 / "native-report" / "extra.xml"
        assert native.read_text(encoding="utf-8") == "<extra/>"
    finally:
        ingestor.close()


def test_changed_existing_record_is_rejected(dsn: str, test_schema: str, quarantine: Path, tmp_path: Path) -> None:
    """A continuation that changes an already-stored record conflicts on
    its immutable hash and writes nothing."""

    job = unique_job_id()
    base = reidentify_bundle(tmp_path / "hash-base", execution_id="review-hash", github_job_id=job)
    bad = tmp_path / "hash-bad"
    bad.mkdir()
    for path in base.rglob("*"):
        if path.is_file():
            target = bad / path.relative_to(base)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    mutate_record(bad, "journals/main.jsonl", 10, "elapsed_ms", 424242)

    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        evidence = GitHubEvidence(
            run=cast(Mapping[str, object], run_record(1122334455, 1)),
            jobs=(cast(Mapping[str, object], job_record(job, 1122334455, 1, "e2e-bmaas")),),
            expected_job_id=job,
        )
        first = ingestor.ingest_delivery(
            delivery=BundleDirectory(path=base),
            source=HostSourceContext(origin="host-hash"),
            github=evidence,
            redaction_attestation=full_proof(bundle=frozen_bundle(base)),
        )
        assert first.status == "committed"
        with pytest.raises(ledger.ConflictError):
            ingestor.ingest_delivery(
                delivery=BundleDirectory(path=bad),
                source=HostSourceContext(origin="host-hash"),
                github=evidence,
                redaction_attestation=full_proof(bundle=frozen_bundle(bad)),
            )
        receipts = ingestor.list_receipts(execution_id="review-hash")
        statuses = {row.status for row in receipts}
        assert statuses == {"committed", "failed"}
        failed = next(row for row in receipts if row.status == "failed")
        assert failed.failure_code == "ledger-error"
        events = direct_fetch(
            dsn, test_schema, "SELECT count(*) FROM {schema}.ledger_event WHERE execution_id = %s", ("review-hash",)
        )
        assert events == (first.events_appended,)
    finally:
        ingestor.close()


# Races


def run_delivery(
    results: list[tuple[str, IngestResult]],
    errors: list[tuple[str, str]],
    dsn: str,
    test_schema: str,
    quarantine: Path,
    bundle_dir: Path,
    origin: str,
    evidence: GitHubEvidence | None,
    proof: object,
) -> None:
    try:
        client = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
        try:
            results.append(
                (
                    origin,
                    client.ingest_delivery(
                        delivery=BundleDirectory(path=bundle_dir),
                        source=HostSourceContext(origin=origin),
                        github=evidence,
                        redaction_attestation=cast_proof(proof),
                    ),
                )
            )
        finally:
            client.close()
    except Exception as exc:
        errors.append((origin, type(exc).__name__))


def cast_proof(proof: object) -> object:
    from test_observability_sources import RedactionAttestation

    return cast(RedactionAttestation, proof)


def test_concurrent_authorized_and_unverified_race(
    dsn: str, test_schema: str, quarantine: Path, tmp_path: Path
) -> None:
    """An unverified delivery racing a verified one for the same execution
    id never writes: the verified caller's data is the only thing that
    lands, and the unverified delivery is quarantined."""

    job = unique_job_id()
    good = reidentify_bundle(tmp_path / "race-good", execution_id="review-race", github_job_id=job)
    bad = reidentify_bundle(tmp_path / "race-bad", execution_id="review-race", github_job_id=job)
    mutate_record(bad, "journals/main.jsonl", 10, "elapsed_ms", 1234)
    results: list[tuple[str, IngestResult]] = []
    errors: list[tuple[str, str]] = []
    evidence = GitHubEvidence(
        run=cast(Mapping[str, object], run_record(1122334455, 1)),
        jobs=(cast(Mapping[str, object], job_record(job, 1122334455, 1, "e2e-bmaas")),),
        expected_job_id=job,
    )
    threads = [
        threading.Thread(
            target=run_delivery,
            args=(
                results,
                errors,
                dsn,
                test_schema,
                quarantine / "good",
                good,
                "race-good",
                evidence,
                full_proof(bundle=frozen_bundle(good)),
            ),
        ),
        threading.Thread(
            target=run_delivery,
            args=(
                results,
                errors,
                dsn,
                test_schema,
                quarantine / "bad",
                bad,
                "race-bad",
                None,
                full_proof(bundle=frozen_bundle(bad)),
            ),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    by_origin = {origin: result for origin, result in results}
    assert by_origin["race-good"].status == "committed"
    assert by_origin["race-bad"].status == "quarantined"
    row = direct_fetch(
        dsn,
        test_schema,
        "SELECT x.github_job_id, e.data->>'elapsed_ms' FROM {schema}.ledger_execution x"
        " JOIN {schema}.ledger_event e USING (execution_id)"
        " WHERE x.execution_id = %s AND e.journal_id = 'main' AND e.sequence = 10",
        ("review-race",),
    )
    assert row is not None
    assert row[0] == job
    assert row[1] != "1234", "the unverified payload's changed record reached the verified execution"


def test_concurrent_verified_callers_keep_one_binding(
    dsn: str, test_schema: str, quarantine: Path, tmp_path: Path
) -> None:
    """Two verified callers for different jobs racing on the same execution
    id: exactly one binding wins, the loser is rejected without changes,
    and the stored record is consistent with the winner."""

    first_job = unique_job_id()
    second_job = unique_job_id()
    first_dir = reidentify_bundle(tmp_path / "race2-a", execution_id="review-race2", github_job_id=first_job)
    second_dir = reidentify_bundle(tmp_path / "race2-b", execution_id="review-race2", github_job_id=second_job)
    mutate_record(second_dir, "journals/main.jsonl", 10, "elapsed_ms", 777)
    results: list[tuple[str, IngestResult]] = []
    errors: list[tuple[str, str]] = []

    def evidence_for(job: int) -> GitHubEvidence:
        return GitHubEvidence(
            run=cast(Mapping[str, object], run_record(1122334455, 1)),
            jobs=(cast(Mapping[str, object], job_record(job, 1122334455, 1, "e2e-bmaas")),),
            expected_job_id=job,
        )

    threads = [
        threading.Thread(
            target=run_delivery,
            args=(
                results,
                errors,
                dsn,
                test_schema,
                quarantine / "a",
                first_dir,
                "race-a",
                evidence_for(first_job),
                full_proof(bundle=frozen_bundle(first_dir)),
            ),
        ),
        threading.Thread(
            target=run_delivery,
            args=(
                results,
                errors,
                dsn,
                test_schema,
                quarantine / "b",
                second_dir,
                "race-b",
                evidence_for(second_job),
                full_proof(bundle=frozen_bundle(second_dir)),
            ),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert len(results) == 1, "exactly one verified caller wins"
    assert len(errors) == 1 and errors[0][1] == "SourceConflictError", errors
    winning_origin, winner = results[0]
    assert winner.status == "committed"
    row = direct_fetch(
        dsn,
        test_schema,
        "SELECT github_job_id FROM {schema}.ledger_execution WHERE execution_id = %s",
        ("review-race2",),
    )
    assert row is not None
    # The bound job, the winning origin, and the stored event bytes all
    # belong to the same caller; the loser wrote nothing.
    winner_job = first_job if winning_origin == "race-a" else second_job
    loser_job = second_job if winning_origin == "race-a" else first_job
    assert row[0] == winner_job
    elapsed = direct_fetch(
        dsn,
        test_schema,
        "SELECT data->>'elapsed_ms' FROM {schema}.ledger_event WHERE execution_id = %s"
        " AND journal_id = 'main' AND sequence = 10",
        ("review-race2",),
    )
    assert elapsed is not None
    if winning_origin == "race-a":
        # The race-a bundle kept the fixture's original record: the
        # race-b mutation (777) must be absent.
        assert elapsed[0] != "777"
    else:
        assert elapsed[0] == "777"
    assert winner.events_appended > 0
    events = direct_fetch(
        dsn, test_schema, "SELECT count(*) FROM {schema}.ledger_event WHERE execution_id = %s", ("review-race2",)
    )
    assert events == (winner.events_appended,)
    # The rejected caller's delivery left no receipt content behind.
    with psycopg.connect(dsn, autocommit=True) as conn:
        failed = conn.execute(
            sql.SQL(
                "SELECT status, events_committed FROM {schema}.source_receipt"
                " WHERE execution_id = %s AND status = 'failed'"
            ).format(schema=sql.Identifier(test_schema)),
            ("review-race2",),
        ).fetchall()
    assert len(failed) == 1
    assert failed[0][1] == 0
    assert loser_job != row[0]


def test_concurrent_duplicate_delivery(dsn: str, test_schema: str, quarantine: Path, tmp_path: Path) -> None:
    bundle_dir = reidentify_bundle(
        tmp_path / "concurrent", execution_id="exec-concurrent", github_job_id=unique_job_id()
    )
    frozen = frozen_bundle(bundle_dir)
    results: list[tuple[str, IngestResult]] = []
    errors: list[tuple[str, str]] = []
    evidence = example_evidence(frozen.manifest)
    threads = [
        threading.Thread(
            target=run_delivery,
            args=(
                results,
                errors,
                dsn,
                test_schema,
                quarantine / "one",
                bundle_dir,
                "host-cc",
                evidence,
                full_proof(bundle=frozen),
            ),
        ),
        threading.Thread(
            target=run_delivery,
            args=(
                results,
                errors,
                dsn,
                test_schema,
                quarantine / "two",
                bundle_dir,
                "host-cc",
                evidence,
                full_proof(bundle=frozen),
            ),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    assert len(results) == 2
    assert {result.receipt_id for result in (r for _, r in results)} == {results[0][1].receipt_id}
    total = sum(result.events_appended + result.events_duplicates for _, result in results)
    assert total == 2 * frozen.total_records
    events = direct_fetch(
        dsn, test_schema, "SELECT count(*) FROM {schema}.ledger_event WHERE execution_id = %s", ("exec-concurrent",)
    )
    assert events == (frozen.total_records,)


# Restart, recovery, and acknowledgement


def test_partial_multi_batch_import_recovers(dsn: str, test_schema: str, quarantine: Path, tmp_path: Path) -> None:
    """A database failure mid-import leaves a consistent prefix and a
    partial receipt; a redelivery completes it without duplicate counts."""

    bundle_dir = bulk_bundle(tmp_path / "bulk", execution_id="exec-bulk", collected=10005)
    _reidentify_job(bundle_dir, unique_job_id())
    frozen = frozen_bundle(bundle_dir)
    evidence = example_evidence(frozen.manifest)
    # Reject event inserts with sequence above 10 000, so the second batch
    # of this 10 008-record bundle fails while the first stays committed.
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL(
                "CREATE OR REPLACE FUNCTION {schema}.w01b_reject_late_sequences() RETURNS trigger AS $$"
                " BEGIN"
                "  IF NEW.sequence > 10000 THEN"
                "   RAISE EXCEPTION 'injected batch failure' USING ERRCODE = 'P0001';"
                "  END IF;"
                "  RETURN NEW;"
                " END"
                " $$ LANGUAGE plpgsql"
            ).format(schema=sql.Identifier(test_schema))
        )
        conn.execute(
            sql.SQL(
                "CREATE TRIGGER trg_w01b_inject BEFORE INSERT ON {schema}.ledger_event"
                " FOR EACH ROW EXECUTE FUNCTION {schema}.w01b_reject_late_sequences()"
            ).format(schema=sql.Identifier(test_schema))
        )
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        with pytest.raises(IngestError) as excinfo:
            ingestor.ingest_delivery(
                delivery=BundleDirectory(path=bundle_dir),
                source=HostSourceContext(origin="host-bulk"),
                github=evidence,
                redaction_attestation=full_proof(bundle=frozen),
            )
        assert excinfo.value.code == "database-error"
        receipt = ingestor.list_receipts(execution_id="exec-bulk")[0]
        assert receipt.status == "partial"
        assert receipt.events_committed == 10000
        assert receipt.total_records == 10008
        prefix = direct_fetch(
            dsn,
            test_schema,
            "SELECT coalesce(sum(prefix_sequence), 0) FROM {schema}.ledger_journal WHERE execution_id = %s",
            ("exec-bulk",),
        )
        assert prefix == (10000,)
    finally:
        ingestor.close()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP TRIGGER IF EXISTS trg_w01b_inject ON {schema}.ledger_event").format(
                schema=sql.Identifier(test_schema)
            )
        )
        conn.execute(
            sql.SQL("DROP FUNCTION IF EXISTS {schema}.w01b_reject_late_sequences()").format(
                schema=sql.Identifier(test_schema)
            )
        )
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        result = ingestor.ingest_delivery(
            delivery=BundleDirectory(path=bundle_dir),
            source=HostSourceContext(origin="host-bulk"),
            github=evidence,
            redaction_attestation=full_proof(bundle=frozen),
        )
        assert result.status == "committed"
        assert result.events_appended == 8
        assert result.events_duplicates == 10000
        receipt = ingestor.get_receipt(receipt_id=result.receipt_id)
        assert receipt is not None
        assert receipt.status == "committed"
        assert receipt.events_committed == receipt.total_records == 10008
    finally:
        ingestor.close()


def test_idempotent_redelivery_changes_no_counts(dsn: str, test_schema: str, quarantine: Path, tmp_path: Path) -> None:
    bundle_dir = reidentify_bundle(tmp_path / "idem", execution_id="exec-idem", github_job_id=unique_job_id())
    frozen = frozen_bundle(bundle_dir)
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        first = ingestor.ingest_bundle(
            bundle=frozen,
            source=HostSourceContext(origin="host-idem"),
            github=example_evidence(frozen.manifest),
            redaction_attestation=full_proof(bundle=frozen),
        )
        second = ingestor.ingest_bundle(
            bundle=frozen,
            source=HostSourceContext(origin="host-idem"),
            github=example_evidence(frozen.manifest),
            redaction_attestation=full_proof(bundle=frozen),
        )
        assert second.receipt_id == first.receipt_id
        assert second.events_appended == 0
        assert second.events_duplicates == first.events_appended
        counts = direct_fetch(
            dsn,
            test_schema,
            "SELECT (SELECT count(*) FROM {schema}.ledger_event WHERE execution_id = %s),"
            " (SELECT count(*) FROM {schema}.source_receipt WHERE execution_id = %s)",
            (first.execution_id, first.execution_id),
        )
        assert counts == (first.events_appended, 1)
    finally:
        ingestor.close()


def test_ack_reflects_only_committed_state(dsn: str, test_schema: str, quarantine: Path, tmp_path: Path) -> None:
    bundle_dir = reidentify_bundle(tmp_path / "ack", execution_id="exec-ack", github_job_id=unique_job_id())
    frozen = frozen_bundle(bundle_dir)
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        result = ingestor.ingest_bundle(
            bundle=frozen,
            source=HostSourceContext(origin="host-ack"),
            github=example_evidence(frozen.manifest),
            redaction_attestation=full_proof(bundle=frozen),
        )
        with psycopg.connect(dsn, autocommit=True) as conn:
            receipt = conn.execute(
                sql.SQL(
                    "SELECT status, events_committed, total_records FROM {schema}.source_receipt WHERE receipt_id = %s"
                ).format(schema=sql.Identifier(test_schema)),
                (result.receipt_id,),
            ).fetchone()
            events = conn.execute(
                sql.SQL("SELECT count(*) FROM {schema}.ledger_event WHERE execution_id = %s").format(
                    schema=sql.Identifier(test_schema)
                ),
                (result.execution_id,),
            ).fetchone()
        assert receipt == ("committed", result.events_appended, result.events_appended)
        assert events == (result.events_appended,)
    finally:
        ingestor.close()


# The freeze guarantee


def test_publication_uses_frozen_bytes_after_filesystem_swaps(
    dsn: str, test_schema: str, quarantine: Path, tmp_path: Path
) -> None:
    """Files swapped after the freeze never reach storage: the frozen bytes
    are what gets validated, hashed, and appended."""

    job = unique_job_id()
    bundle_dir = reidentify_bundle(tmp_path / "swap", execution_id="exec-swap", github_job_id=job)
    frozen = freeze(delivery=BundleDirectory(path=bundle_dir))
    frozen_records = {
        index: record for index, record in enumerate(read_journal_records(bundle_dir, "journals/main.jsonl"))
    }
    (bundle_dir / "journals" / "main.jsonl").write_text('{"schema": "swapped"}\n', encoding="utf-8")
    (bundle_dir / "native-report" / "junit.xml").write_bytes(b"<swapped/>")
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        result = ingestor.ingest_bundle(
            bundle=frozen,
            source=HostSourceContext(origin="host-swap"),
            github=example_evidence(frozen.manifest),
            redaction_attestation=full_proof(bundle=frozen),
        )
        client = ledger.Ledger(dsn=dsn, schema=test_schema)
        try:
            stored = client.list_events(execution_id=result.execution_id, limit=100)
        finally:
            client.close()
        assert len(stored) == frozen.total_records
        for event in stored:
            original = frozen_records[event.sequence - 1]
            assert event.data == original["data"]
            assert event.event == original["event"]
        quarantine_journal = quarantine / frozen.manifest_sha256 / "journals" / "main.jsonl"
        assert quarantine_journal.read_bytes() == frozen.journal_files[0].data
    finally:
        ingestor.close()
