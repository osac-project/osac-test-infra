"""Real-PostgreSQL tests for the publication gate and reconciliation of
explicitly supplied trusted GitHub metadata (W01b).

Only a VERIFIED job binding from trusted mappings, a full-coverage trusted
redaction proof, and a non-contradicted source proof publish; every other
delivery stays durably quarantined with zero canonical writes. The ledger's
authoritative columns carry the trusted repository id and job identity,
never the payload's claims. Tests check ledger rows, observations, and
rejections through second connections.

Shared builders live in test_observability_sources.
"""

from __future__ import annotations

import itertools
import json
import os
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

from contract_helpers import MANIFEST_NAME, load_json
from psycopg import sql
from test_observability_sources import (
    TRUSTED_REPOSITORY,
    TRUSTED_REPOSITORY_ID,
    TRUSTED_RUN,
    artifact_record,
    direct_fetch,
    frozen_bundle,
    full_proof,
    job_record,
    mutate_record,
    publish,
    reidentify_bundle,
    run_record,
    sha256_hex,
    verified_evidence,
    zip_bundle,
)

from monitoring.ci_observability import ledger
from monitoring.ci_observability.bundle_reader import BundleArchive, BundleDirectory, BundleError
from monitoring.ci_observability.ingest import (
    ArtifactSourceContext,
    HostSourceContext,
    IngestError,
    Ingestor,
    SourceConflictError,
)
from monitoring.ci_observability.reconcile import GitHubEvidence, job_claim

MANIFEST_TESTED_SHA = "a31fea65edcf9ff0f6a0012e7150f6150bde4c58"
_job_ids = itertools.count(4588200000)


def unique_job_id() -> int:
    """A fresh numeric job id: the ledger's unique index allows only one
    executed record per job id, so every verified test needs its own."""

    return next(_job_ids)


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
    name = f"ci_obs_w01b_reconcile_{os.getpid()}"
    assert ledger.apply_migrations(dsn=dsn, schema=name) == [1, 2, 3]
    yield name
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


@pytest.fixture
def quarantine(tmp_path: Path) -> Path:
    return tmp_path / "quarantine"


def canonical_rows(dsn: str, test_schema: str, execution_id: str) -> tuple[int, int, int]:
    """(executions, events, conclusions) for one execution id, through a
    fresh connection."""

    with psycopg.connect(dsn, autocommit=True) as conn:
        executions = conn.execute(
            sql.SQL("SELECT count(*) FROM {schema}.ledger_execution WHERE execution_id = %s").format(
                schema=sql.Identifier(test_schema)
            ),
            (execution_id,),
        ).fetchone()[0]
        events = conn.execute(
            sql.SQL("SELECT count(*) FROM {schema}.ledger_event WHERE execution_id = %s").format(
                schema=sql.Identifier(test_schema)
            ),
            (execution_id,),
        ).fetchone()[0]
        conclusions = conn.execute(
            sql.SQL(
                "SELECT count(*) FROM {schema}.ledger_execution WHERE execution_id = %s"
                " AND (github_run_conclusion IS NOT NULL OR github_job_conclusion IS NOT NULL)"
            ).format(schema=sql.Identifier(test_schema)),
            (execution_id,),
        ).fetchone()[0]
    return executions, events, conclusions


# Verified bindings publish with trusted identity


def test_verified_job_publishes_with_trusted_repository_id(
    dsn: str, test_schema: str, quarantine: Path, tmp_path: Path
) -> None:
    """Publication stores the repository id from the trusted run record;
    the payload's conflicting claim is recorded as evidence only."""

    job = unique_job_id()
    bundle_dir = reidentify_bundle(
        tmp_path / "verified", execution_id="exec-verified", github_job_id=job, repository_id=42
    )
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        result = publish(ingestor, bundle_dir, origin="host-v", job_id=job)
        assert result.status == "committed"
        assert result.events_appended == frozen_bundle(bundle_dir).total_records
        row = direct_fetch(
            dsn,
            test_schema,
            "SELECT repository_id, github_job_id, executed, github_run_conclusion, github_job_conclusion"
            " FROM {schema}.ledger_execution WHERE execution_id = %s",
            ("exec-verified",),
        )
        assert row == (TRUSTED_REPOSITORY_ID, job, True, "success", "success")
        observation = ingestor.list_observations(execution_id="exec-verified")[0]
        assert observation.job_verdict == "verified"
        assert observation.numeric_job_id == job
        assert observation.executed_in_attempt is True
        assert observation.publication_authorized is True
        assert observation.quarantine_reason is None
        evidence = observation.binding_evidence["job"]
        assert evidence["claimed_repository_id"] == 42
        assert evidence["claimed_repository_id_mismatch"] is True
        assert evidence["claimed_job_id_mismatch"] is False
        assert observation.trusted_context["expected_job_id"] == job
        assert observation.run_record is not None
        assert observation.job_records is not None and len(observation.job_records) == 1
    finally:
        ingestor.close()


def test_expected_display_name_verifies(dsn: str, test_schema: str, quarantine: Path, tmp_path: Path) -> None:
    job = unique_job_id()
    bundle_dir = reidentify_bundle(tmp_path / "byname", execution_id="exec-byname", github_job_id=job)
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        result = publish(
            ingestor,
            bundle_dir,
            origin="host-v",
            job_id=job,
            evidence=verified_evidence(job_id=job, expected_job_id=None, expected_display_name="e2e-bmaas"),
        )
        assert result.status == "committed"
        assert "exec-byname" in [row.execution_id for row in ingestor.list_receipts(unbound=False)]
    finally:
        ingestor.close()


def test_claimed_job_id_mismatch_is_recorded_not_trusted(
    dsn: str, test_schema: str, quarantine: Path, tmp_path: Path
) -> None:
    job = unique_job_id()
    bundle_dir = reidentify_bundle(tmp_path / "claimid", execution_id="exec-claimid", github_job_id=987654321)
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        publish(ingestor, bundle_dir, origin="host-v", job_id=job)
        observation = ingestor.list_observations(execution_id="exec-claimid")[0]
        assert observation.binding_evidence["job"]["claimed_job_id"] == 987654321
        assert observation.binding_evidence["job"]["claimed_job_id_mismatch"] is True
        assert observation.numeric_job_id == job
    finally:
        ingestor.close()


# Unverified verdicts quarantine with zero canonical writes


def test_candidate_never_publishes(dsn: str, test_schema: str, quarantine: Path, tmp_path: Path) -> None:
    job = unique_job_id()
    bundle_dir = reidentify_bundle(tmp_path / "candidate", execution_id="exec-candidate", github_job_id=job)
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        result = publish(
            ingestor,
            bundle_dir,
            origin="host-c",
            job_id=job,
            evidence=verified_evidence(job_id=job, expected_job_id=None),
        )
        assert result.status == "quarantined"
        assert result.quarantine_reason == "unverified-job-binding"
        assert result.job_verdict == "candidate"
        assert canonical_rows(dsn, test_schema, "exec-candidate") == (0, 0, 0)
        observation = ingestor.list_observations(execution_id="exec-candidate")[0]
        assert observation.job_verdict == "candidate"
        assert observation.publication_authorized is False
        assert observation.quarantine_reason == "unverified-job-binding"
        assert observation.binding_evidence["job"]["candidate_count"] == 1
    finally:
        ingestor.close()


def test_no_metadata_stays_quarantined_and_queryable(
    dsn: str, test_schema: str, quarantine: Path, tmp_path: Path
) -> None:
    bundle_dir = reidentify_bundle(tmp_path / "nometa", execution_id="exec-nometa")
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        result = ingestor.ingest_delivery(
            delivery=BundleDirectory(path=bundle_dir), source=HostSourceContext(origin="host-n")
        )
        assert result.status == "quarantined"
        assert result.quarantine_reason == "no-metadata"
        assert canonical_rows(dsn, test_schema, "exec-nometa") == (0, 0, 0)
        observations = ingestor.list_observations(execution_id="exec-nometa")
        assert [(o.job_verdict, o.job_reason) for o in observations] == [("unknown", "no-metadata-supplied")]
        assert observations[0].run_record is None
        unbound = ingestor.list_receipts(unbound=True)
        assert "exec-nometa" in [row.execution_id for row in unbound]
    finally:
        ingestor.close()


def test_claim_run_mismatch_quarantines(dsn: str, test_schema: str, quarantine: Path, tmp_path: Path) -> None:
    job = unique_job_id()
    bundle_dir = reidentify_bundle(
        tmp_path / "mismatch", execution_id="exec-mismatch", run_id=999999, github_job_id=job
    )
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        result = publish(ingestor, bundle_dir, origin="host-m", job_id=job)
        assert result.status == "quarantined"
        assert result.quarantine_reason == "unverified-job-binding"
        observation = ingestor.list_observations(execution_id="exec-mismatch")[0]
        assert observation.job_reason == "claim_run_mismatch"
        assert canonical_rows(dsn, test_schema, "exec-mismatch") == (0, 0, 0)
    finally:
        ingestor.close()


def test_sibling_matrix_claims_stay_unverified(dsn: str, test_schema: str, quarantine: Path, tmp_path: Path) -> None:
    """Two matrix siblings share a logical job key. The claim's display
    name matches one of them uniquely, which is only ever a CANDIDATE; a
    trusted mapping binds that sibling, and a mapping to the other sibling
    contradicts the claim and stays unknown."""

    first_id = unique_job_id()
    second_id = unique_job_id()
    run = run_record(TRUSTED_RUN, 1)
    jobs = (
        job_record(first_id, TRUSTED_RUN, 1, "e2e (python-3.11)"),
        job_record(second_id, TRUSTED_RUN, 1, "e2e (python-3.12)"),
    )
    first_dir = reidentify_bundle(
        tmp_path / "sibling-1", execution_id="exec-sibling-1", job_name="e2e (python-3.11)", github_job_id=None
    )
    second_dir = reidentify_bundle(
        tmp_path / "sibling-2", execution_id="exec-sibling-2", job_name="e2e (python-3.11)", github_job_id=None
    )
    mutate_record(second_dir, "journals/main.jsonl", 10, "elapsed_ms", 55)
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        first = ingestor.ingest_bundle(
            bundle=frozen_bundle(first_dir),
            source=HostSourceContext(origin="host-s"),
            github=GitHubEvidence(run=run, jobs=jobs),
            redaction_attestation=full_proof(bundle=frozen_bundle(first_dir)),
        )
        assert first.status == "quarantined"
        assert canonical_rows(dsn, test_schema, "exec-sibling-1") == (0, 0, 0)

        second = ingestor.ingest_bundle(
            bundle=frozen_bundle(first_dir),
            source=HostSourceContext(origin="host-s"),
            github=GitHubEvidence(run=run, jobs=jobs, expected_job_id=first_id),
            redaction_attestation=full_proof(bundle=frozen_bundle(first_dir)),
        )
        assert second.status == "committed"
        row = direct_fetch(
            dsn,
            test_schema,
            "SELECT github_job_id, executed FROM {schema}.ledger_execution WHERE execution_id = %s",
            ("exec-sibling-1",),
        )
        assert row == (first_id, True)

        # A mapping to the other sibling contradicts the claim's own name
        # and never verifies, so an untrusted name cannot be re-bound.
        third = ingestor.ingest_bundle(
            bundle=frozen_bundle(second_dir),
            source=HostSourceContext(origin="host-s"),
            github=GitHubEvidence(run=run, jobs=jobs, expected_job_id=second_id),
            redaction_attestation=full_proof(bundle=frozen_bundle(second_dir)),
        )
        assert third.status == "quarantined"
        observation = ingestor.list_observations(execution_id="exec-sibling-2")[0]
        assert observation.job_reason == "claim_contradicts_job"
        assert canonical_rows(dsn, test_schema, "exec-sibling-2") == (0, 0, 0)
    finally:
        ingestor.close()


# Ownership and UUID collisions


def test_cross_job_uuid_collision_is_rejected(dsn: str, test_schema: str, quarantine: Path, tmp_path: Path) -> None:
    """The same content redelivered and VERIFIED for a different job than
    the one the execution is already bound to is rejected without
    changes: a producer UUID cannot be re-bound by later metadata."""

    first_job = unique_job_id()
    second_job = unique_job_id()
    bundle_dir = reidentify_bundle(tmp_path / "collide", execution_id="exec-collide", github_job_id=first_job)
    frozen = frozen_bundle(bundle_dir)
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        ingestor.ingest_bundle(
            bundle=frozen,
            source=HostSourceContext(origin="host-k"),
            github=verified_evidence(job_id=first_job, expected_job_id=first_job),
            redaction_attestation=full_proof(bundle=frozen),
        )
        # Two jobs carry the claim's display name, so binding the second
        # one verifies without contradicting the claim; it still must not
        # re-bind the already-bound execution.
        with pytest.raises(SourceConflictError) as excinfo:
            ingestor.ingest_bundle(
                bundle=frozen,
                source=HostSourceContext(origin="host-k"),
                github=GitHubEvidence(
                    run=run_record(TRUSTED_RUN, 1),
                    jobs=(
                        job_record(first_job, TRUSTED_RUN, 1, "e2e-bmaas"),
                        job_record(second_job, TRUSTED_RUN, 1, "e2e-bmaas"),
                    ),
                    expected_job_id=second_job,
                ),
                redaction_attestation=full_proof(bundle=frozen),
            )
        assert excinfo.value.code == "execution-bound-to-different-job"
        row = direct_fetch(
            dsn,
            test_schema,
            "SELECT github_job_id, executed FROM {schema}.ledger_execution WHERE execution_id = %s",
            ("exec-collide",),
        )
        assert row == (first_job, True)
    finally:
        ingestor.close()


def test_redelivery_without_authority_does_not_regress_the_receipt(
    dsn: str, test_schema: str, quarantine: Path, tmp_path: Path
) -> None:
    """Authority is supplied per delivery: a redelivery without metadata is
    quarantined and writes nothing, while the already-published receipt
    keeps its committed state and stored events. A different, unproven
    source stays quarantined too."""

    job = unique_job_id()
    bundle_dir = reidentify_bundle(tmp_path / "bound", execution_id="exec-bound", github_job_id=job)
    frozen = frozen_bundle(bundle_dir)
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        first = ingestor.ingest_bundle(
            bundle=frozen,
            source=HostSourceContext(origin="host-b"),
            github=verified_evidence(job_id=job, expected_job_id=job),
            redaction_attestation=full_proof(bundle=frozen),
        )
        assert first.status == "committed"
        events_before = direct_fetch(
            dsn, test_schema, "SELECT count(*) FROM {schema}.ledger_event WHERE execution_id = %s", ("exec-bound",)
        )
        redelivery = ingestor.ingest_bundle(bundle=frozen, source=HostSourceContext(origin="host-b"))
        assert redelivery.status == "quarantined"
        assert redelivery.quarantine_reason == "no-metadata"
        assert redelivery.receipt_id == first.receipt_id
        receipt = ingestor.get_receipt(receipt_id=first.receipt_id)
        assert receipt is not None
        assert receipt.status == "committed"
        assert receipt.events_committed == first.events_appended
        events_after = direct_fetch(
            dsn, test_schema, "SELECT count(*) FROM {schema}.ledger_event WHERE execution_id = %s", ("exec-bound",)
        )
        assert events_after == events_before
        other = ingestor.ingest_bundle(bundle=frozen, source=HostSourceContext(origin="host-other"))
        assert other.status == "quarantined"
        assert other.quarantine_reason == "no-metadata"
    finally:
        ingestor.close()


def test_claim_first_verify_later(dsn: str, test_schema: str, quarantine: Path, tmp_path: Path) -> None:
    job = unique_job_id()
    bundle_dir = reidentify_bundle(tmp_path / "later", execution_id="exec-later", github_job_id=job)
    frozen = frozen_bundle(bundle_dir)
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        first = ingestor.ingest_bundle(bundle=frozen, source=HostSourceContext(origin="host-42"))
        assert first.status == "quarantined"
        second = ingestor.ingest_bundle(
            bundle=frozen,
            source=HostSourceContext(origin="host-42"),
            github=verified_evidence(job_id=job, expected_job_id=job),
            redaction_attestation=full_proof(bundle=frozen),
        )
        assert second.receipt_id == first.receipt_id
        assert second.status == "committed"
        assert second.events_appended == frozen.total_records
        row = direct_fetch(
            dsn,
            test_schema,
            "SELECT repository_id, github_job_id, executed, github_run_conclusion FROM {schema}.ledger_execution"
            " WHERE execution_id = %s",
            ("exec-later",),
        )
        assert row == (TRUSTED_REPOSITORY_ID, job, True, "success")
    finally:
        ingestor.close()


def test_unbound_stale_claim_never_matches_by_repo_run_or_key(
    dsn: str, test_schema: str, quarantine: Path, tmp_path: Path
) -> None:
    """A claim that matches a published execution's repository, run, and
    logical key but carries its own execution id stays a separate
    quarantined row; nothing merges or appends by fuzzy match."""

    job = unique_job_id()
    verified_dir = reidentify_bundle(tmp_path / "stale-1", execution_id="exec-stale-verified", github_job_id=job)
    claim_dir = reidentify_bundle(tmp_path / "stale-2", execution_id="exec-stale-claim", github_job_id=job)
    mutate_record(claim_dir, "journals/main.jsonl", 10, "elapsed_ms", 7)
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        verified = publish(ingestor, verified_dir, origin="host-s", job_id=job)
        result = ingestor.ingest_delivery(
            delivery=BundleDirectory(path=claim_dir), source=HostSourceContext(origin="host-s")
        )
        assert result.status == "quarantined"
        assert canonical_rows(dsn, test_schema, "exec-stale-claim") == (0, 0, 0)
        events = direct_fetch(
            dsn,
            test_schema,
            "SELECT count(*) FROM {schema}.ledger_event WHERE execution_id = %s",
            ("exec-stale-verified",),
        )
        assert events == (verified.events_appended,)
    finally:
        ingestor.close()


# Attempts, inheritance, and immutable conclusions


def test_two_attempts_keep_the_first_failure(dsn: str, test_schema: str, quarantine: Path, tmp_path: Path) -> None:
    first_job = unique_job_id()
    second_job = unique_job_id()
    attempt1 = reidentify_bundle(
        tmp_path / "att1", execution_id="exec-attempts-1", run_attempt=1, github_job_id=first_job
    )
    attempt2 = reidentify_bundle(
        tmp_path / "att2", execution_id="exec-attempts-2", run_attempt=2, github_job_id=second_job
    )
    run1 = run_record(TRUSTED_RUN, 1, conclusion="failure")
    run2 = run_record(TRUSTED_RUN, 2, conclusion="success", started="2026-10-27T08:00:00Z")
    job1 = job_record(first_job, TRUSTED_RUN, 1, "e2e-bmaas", conclusion="failure")
    job2 = job_record(second_job, TRUSTED_RUN, 2, "e2e-bmaas", conclusion="success", started="2026-10-27T08:01:00Z")
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        first = ingestor.ingest_bundle(
            bundle=frozen_bundle(attempt1),
            source=HostSourceContext(origin="host-a"),
            github=GitHubEvidence(run=run1, jobs=(job1,), expected_job_id=first_job),
            redaction_attestation=full_proof(bundle=frozen_bundle(attempt1)),
        )
        second = ingestor.ingest_bundle(
            bundle=frozen_bundle(attempt2),
            source=HostSourceContext(origin="host-a"),
            github=GitHubEvidence(run=run2, jobs=(job1, job2), expected_job_id=second_job),
            redaction_attestation=full_proof(bundle=frozen_bundle(attempt2)),
        )
        assert first.status == "committed"
        assert second.status == "committed"
        first_row = direct_fetch(
            dsn,
            test_schema,
            "SELECT github_run_conclusion, github_job_conclusion FROM {schema}.ledger_execution"
            " WHERE execution_id = %s",
            ("exec-attempts-1",),
        )
        second_row = direct_fetch(
            dsn,
            test_schema,
            "SELECT github_run_conclusion, github_job_conclusion FROM {schema}.ledger_execution"
            " WHERE execution_id = %s",
            ("exec-attempts-2",),
        )
        assert first_row == ("failure", "failure")
        assert second_row == ("success", "success")

        # A later green claim for attempt 1 cannot overwrite its failure.
        with pytest.raises(ledger.ConflictError):
            ingestor.ingest_bundle(
                bundle=frozen_bundle(attempt1),
                source=HostSourceContext(origin="host-a"),
                github=GitHubEvidence(
                    run=run_record(TRUSTED_RUN, 1, conclusion="success"), jobs=(job1,), expected_job_id=first_job
                ),
                redaction_attestation=full_proof(bundle=frozen_bundle(attempt1)),
            )
        first_row = direct_fetch(
            dsn,
            test_schema,
            "SELECT github_run_conclusion FROM {schema}.ledger_execution WHERE execution_id = %s",
            ("exec-attempts-1",),
        )
        assert first_row == ("failure",)
    finally:
        ingestor.close()


def test_inherited_job_quarantines_instead_of_creating_an_execution(
    dsn: str, test_schema: str, quarantine: Path, tmp_path: Path
) -> None:
    """A re-run attempt's listing carries the earlier attempt's job record;
    binding the expected id to that inherited record quarantines the
    delivery and records the reference instead of counting an execution.
    The new attempt's own job id still publishes and promotes the same
    receipt."""

    old_job = unique_job_id()
    new_job = unique_job_id()
    bundle_dir = reidentify_bundle(
        tmp_path / "inherited", execution_id="exec-inherited", run_attempt=2, github_job_id=old_job
    )
    frozen = frozen_bundle(bundle_dir)
    run = run_record(TRUSTED_RUN, 2, started="2026-10-27T08:00:00Z")
    old_record = job_record(old_job, TRUSTED_RUN, 1, "e2e-bmaas", conclusion="failure")
    new_record = job_record(new_job, TRUSTED_RUN, 2, "e2e-bmaas", started="2026-10-27T08:01:00Z")
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        refused = ingestor.ingest_bundle(
            bundle=frozen,
            source=HostSourceContext(origin="host-i"),
            github=GitHubEvidence(run=run, jobs=(old_record, new_record), expected_job_id=old_job),
            redaction_attestation=full_proof(bundle=frozen),
        )
        assert refused.status == "quarantined"
        assert refused.quarantine_reason == "job-not-executed-in-attempt"
        assert canonical_rows(dsn, test_schema, "exec-inherited") == (0, 0, 0)
        observation = ingestor.list_observations(execution_id="exec-inherited")[0]
        assert observation.job_verdict == "verified"
        assert observation.executed_in_attempt is False
        assert observation.execution_attempt == 1
        receipt = ingestor.get_receipt(receipt_id=refused.receipt_id)
        assert receipt is not None
        assert receipt.status == "quarantined"

        promoted = ingestor.ingest_bundle(
            bundle=frozen,
            source=HostSourceContext(origin="host-i"),
            github=GitHubEvidence(run=run, jobs=(old_record, new_record), expected_job_id=new_job),
            redaction_attestation=full_proof(bundle=frozen),
        )
        assert promoted.status == "committed"
        assert promoted.receipt_id == refused.receipt_id
        receipt = ingestor.get_receipt(receipt_id=refused.receipt_id)
        assert receipt is not None
        assert receipt.status == "committed"
    finally:
        ingestor.close()


def test_unfinished_run_publishes_without_conclusions(
    dsn: str, test_schema: str, quarantine: Path, tmp_path: Path
) -> None:
    job = unique_job_id()
    bundle_dir = reidentify_bundle(tmp_path / "unfinished", execution_id="exec-unfinished", github_job_id=job)
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        result = ingestor.ingest_bundle(
            bundle=frozen_bundle(bundle_dir),
            source=HostSourceContext(origin="host-u"),
            github=verified_evidence(job_id=job, conclusion=None, job_conclusion=None, expected_job_id=job),
            redaction_attestation=full_proof(bundle=frozen_bundle(bundle_dir)),
        )
        assert result.status == "committed"
        row = direct_fetch(
            dsn,
            test_schema,
            "SELECT github_job_id, executed, github_run_conclusion, github_job_conclusion"
            " FROM {schema}.ledger_execution WHERE execution_id = %s",
            ("exec-unfinished",),
        )
        assert row == (job, True, None, None)
    finally:
        ingestor.close()


# Artifact proof


def test_artifact_binding_attests_run_and_digest(dsn: str, test_schema: str, quarantine: Path, tmp_path: Path) -> None:
    job = unique_job_id()
    bundle_dir = reidentify_bundle(tmp_path / "artifact", execution_id="exec-artifact", github_job_id=job)
    frozen = frozen_bundle(bundle_dir)
    archive = tmp_path / "bundle.zip"
    digest = zip_bundle(bundle_dir, archive)
    artifacts = (artifact_record(555001, TRUSTED_RUN, "telemetry-bundle", digest=digest),)
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        result = ingestor.ingest_delivery(
            delivery=BundleArchive(path=archive, received_digest=digest),
            source=ArtifactSourceContext(
                repository=TRUSTED_REPOSITORY, run_id=TRUSTED_RUN, artifact_name="telemetry-bundle"
            ),
            github=verified_evidence(job_id=job, artifacts=artifacts, expected_job_id=job),
            redaction_attestation=full_proof(bundle=frozen),
        )
        assert result.status == "committed"
        observation = ingestor.list_observations(execution_id=result.execution_id)[0]
        assert observation.artifact_verdict == "verified"
        assert observation.artifact_integrity == "verified"
        assert observation.artifact_expired is False
        receipt = ingestor.get_receipt(receipt_id=result.receipt_id)
        assert receipt is not None
        assert receipt.source_kind == "archive"
        assert receipt.source_artifact_name == "telemetry-bundle"
        assert receipt.received_digest == digest
    finally:
        ingestor.close()


def test_artifact_contradictions_quarantine_before_canonical_writes(
    dsn: str, test_schema: str, quarantine: Path, tmp_path: Path
) -> None:
    job = unique_job_id()
    bundle_dir = reidentify_bundle(tmp_path / "artstate", execution_id="exec-artstate", github_job_id=job)
    frozen = frozen_bundle(bundle_dir)
    archive = tmp_path / "bundle.zip"
    digest = zip_bundle(bundle_dir, archive)
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        cases = [
            {
                "name": "missing",
                "artifacts": (),
                "artifact_id": None,
                "reason": "artifact-proof-missing",
                "observation_reason": "artifact_name_not_in_run",
            },
            {
                "name": "mismatch",
                "artifacts": (artifact_record(555002, TRUSTED_RUN, "telemetry-bundle", digest="sha256:" + "0" * 64),),
                "artifact_id": None,
                "reason": "artifact-digest-contradiction",
                "observation_reason": "digest_mismatch",
            },
            {
                "name": "foreign",
                "artifacts": (artifact_record(555003, 7777777, "telemetry-bundle", digest=digest),),
                "artifact_id": None,
                "reason": "artifact-proof-missing",
                "observation_reason": "foreign_run_artifact",
            },
            {
                "name": "id-contradiction",
                "artifacts": (artifact_record(555004, TRUSTED_RUN, "telemetry-bundle", digest=digest),),
                "artifact_id": 555009,
                "reason": "artifact-id-contradiction",
                "observation_reason": None,
            },
            {
                "name": "integrity-unproven",
                "artifacts": (artifact_record(555005, TRUSTED_RUN, "telemetry-bundle"),),
                "artifact_id": None,
                "reason": "artifact-integrity-unproven",
                "observation_reason": None,
            },
        ]
        for case in cases:
            result = ingestor.ingest_delivery(
                delivery=BundleArchive(path=archive, received_digest=digest),
                source=ArtifactSourceContext(
                    repository=TRUSTED_REPOSITORY,
                    run_id=TRUSTED_RUN,
                    artifact_name="telemetry-bundle",
                    artifact_id=case["artifact_id"],
                ),
                github=verified_evidence(job_id=job, artifacts=case["artifacts"], expected_job_id=job),
                redaction_attestation=full_proof(bundle=frozen),
            )
            assert result.status == "quarantined", case["name"]
            assert result.quarantine_reason == case["reason"], case["name"]
            observation = ingestor.list_observations(execution_id=result.execution_id)[-1]
            if case["observation_reason"] is not None:
                assert observation.artifact_reason == case["observation_reason"], case["name"]
        assert canonical_rows(dsn, test_schema, "exec-artstate") == (0, 0, 0)

        # An expired artifact is still identified, with its expiry recorded
        # honestly; identity and integrity are proven, so it publishes.
        expired = (artifact_record(555006, TRUSTED_RUN, "telemetry-bundle", digest=digest, expired=True),)
        result = ingestor.ingest_delivery(
            delivery=BundleArchive(path=archive, received_digest=digest),
            source=ArtifactSourceContext(
                repository=TRUSTED_REPOSITORY, run_id=TRUSTED_RUN, artifact_name="telemetry-bundle"
            ),
            github=verified_evidence(job_id=job, artifacts=expired, expected_job_id=job),
            redaction_attestation=full_proof(bundle=frozen),
        )
        assert result.status == "committed"
        observation = ingestor.list_observations(execution_id=result.execution_id)[-1]
        assert observation.artifact_verdict == "verified"
        assert observation.artifact_expired is True
    finally:
        ingestor.close()


# Tested-revision provenance


def test_revision_provenance_rules(dsn: str, test_schema: str, quarantine: Path, tmp_path: Path) -> None:
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        # A producer-recorded tested source is a CLAIMED revision.
        job = unique_job_id()
        bundle_dir = reidentify_bundle(tmp_path / "rev-claimed", execution_id="exec-rev-claimed", github_job_id=job)
        result = publish(ingestor, bundle_dir, origin="host-rev", job_id=job)
        observation = ingestor.list_observations(execution_id=result.execution_id)[0]
        assert observation.revision_verdict == "claimed"
        assert observation.tested_sha == MANIFEST_TESTED_SHA
        assert observation.revision_provenance == "producer_override"

        # Without a claimed tested revision, the caller's checkout-policy
        # assertion verifies the head SHA for a head-following event.
        head_job = unique_job_id()
        unclaimed = reidentify_bundle(tmp_path / "rev-head", execution_id="exec-rev-head", github_job_id=head_job)
        _null_tested_revision(unclaimed)
        result = ingestor.ingest_bundle(
            bundle=frozen_bundle(unclaimed),
            source=HostSourceContext(origin="host-rev"),
            github=verified_evidence(job_id=head_job, expected_job_id=head_job, tests_run_head=True),
            redaction_attestation=full_proof(bundle=frozen_bundle(unclaimed)),
        )
        observation = ingestor.list_observations(execution_id=result.execution_id)[0]
        assert observation.revision_verdict == "verified"
        assert observation.tested_sha == "a" * 40
        assert observation.revision_provenance == "default_checkout_policy"

        # A trusted commit status verifies the dispatch run's tested SHA.
        dispatch_job = unique_job_id()
        dispatch = reidentify_bundle(
            tmp_path / "rev-dispatch", execution_id="exec-rev-dispatch", run_id=1122334478, github_job_id=dispatch_job
        )
        manifest = load_json(dispatch / MANIFEST_NAME)
        manifest["execution"]["event"] = "workflow_dispatch"
        (dispatch / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        dispatch_run = run_record(1122334478, 1, event="workflow_dispatch", head_sha="c" * 40)
        dispatch_job_record = job_record(dispatch_job, 1122334478, 1, "e2e-bmaas")
        status = {
            "context": "ci/tested-sha",
            "target_url": f"https://github.com/{TRUSTED_REPOSITORY}/actions/runs/1122334478/job/{dispatch_job}",
            "creator": {"id": 555001, "type": "Bot"},
        }
        result = ingestor.ingest_bundle(
            bundle=frozen_bundle(dispatch),
            source=HostSourceContext(origin="host-rev"),
            github=GitHubEvidence(
                run=dispatch_run,
                jobs=(dispatch_job_record,),
                statuses=(status,),
                status_commit_sha="c" * 40,
                expected_context="ci/tested-sha",
                trusted_creator_ids=frozenset({555001}),
                expected_job_id=dispatch_job,
            ),
            redaction_attestation=full_proof(bundle=frozen_bundle(dispatch)),
        )
        observation = ingestor.list_observations(execution_id=result.execution_id)[0]
        assert observation.revision_verdict == "verified"
        assert observation.tested_sha == "c" * 40
        assert observation.revision_provenance == "trusted_commit_status"
        assert observation.trusted_context["status_commit_sha"] == "c" * 40
        assert observation.trusted_context["trusted_creator_ids"] == [555001]

        # A pull_request run's checkout is a merge commit: no tested SHA is
        # invented, and the workflow head is not treated as one.
        pr_job = unique_job_id()
        pr = reidentify_bundle(tmp_path / "rev-pr", execution_id="exec-rev-pr", github_job_id=pr_job)
        manifest = load_json(pr / MANIFEST_NAME)
        manifest["execution"]["event"] = "pull_request"
        (pr / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        _null_tested_revision(pr)
        result = ingestor.ingest_bundle(
            bundle=frozen_bundle(pr),
            source=HostSourceContext(origin="host-rev"),
            github=verified_evidence(job_id=pr_job, event="pull_request", expected_job_id=pr_job),
            redaction_attestation=full_proof(bundle=frozen_bundle(pr)),
        )
        observation = ingestor.list_observations(execution_id=result.execution_id)[0]
        assert observation.revision_verdict == "unknown"
        assert observation.tested_sha is None
    finally:
        ingestor.close()


def _null_tested_revision(bundle_dir: Path) -> None:
    """Set the manifest's tested-source revision to null (never resolved)."""

    manifest = load_json(bundle_dir / MANIFEST_NAME)
    for source_entry in manifest["environment"]["sources"]:
        if source_entry["role"] == "tested":
            source_entry["revision"] = None
    (bundle_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


# Malformed and hostile inputs


def test_malformed_metadata_fails_before_any_write(
    dsn: str, test_schema: str, quarantine: Path, tmp_path: Path
) -> None:
    bundle_dir = reidentify_bundle(tmp_path / "malformed", execution_id="exec-malformed")
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        broken = run_record(TRUSTED_RUN, 1)
        del broken["repository"]
        with pytest.raises(IngestError) as excinfo:
            ingestor.ingest_delivery(
                delivery=BundleDirectory(path=bundle_dir),
                source=HostSourceContext(origin="host-mm"),
                github=GitHubEvidence(run=broken),
            )
        assert excinfo.value.code == "metadata-malformed"
        assert ingestor.list_receipts(execution_id="exec-malformed") == []
    finally:
        ingestor.close()


def test_errors_never_echo_payload_secrets(dsn: str, test_schema: str, quarantine: Path, tmp_path: Path) -> None:
    secret = "AKIAIOSFODNN7EXAMPLE"
    bundle_dir = reidentify_bundle(tmp_path / "secret", execution_id="exec-secret")
    journal = bundle_dir / "journals" / "main.jsonl"
    journal.write_text('{"broken": "' + secret + '"\n', encoding="utf-8")
    manifest = load_json(bundle_dir / MANIFEST_NAME)
    manifest["files"][0].update(
        {"sha256": sha256_hex(journal.read_bytes()), "size_bytes": journal.stat().st_size, "record_count": 1}
    )
    (bundle_dir / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        with pytest.raises(BundleError) as excinfo:
            ingestor.ingest_delivery(
                delivery=BundleDirectory(path=bundle_dir), source=HostSourceContext(origin="host-s")
            )
        assert secret not in str(excinfo.value)
        for row in ingestor.list_receipts(execution_id="exec-secret"):
            assert secret not in json.dumps(row.manifest)
    finally:
        ingestor.close()


def test_authorized_delivery_that_fails_publication_is_not_reported_published(
    dsn: str, test_schema: str, quarantine: Path, tmp_path: Path
) -> None:
    """The observation records the gate decision (publication_authorized),
    never the outcome: an authorized delivery whose publication fails on
    an immutable conflict keeps publication_authorized=true while the
    receipt — the append-only outcome record — says failed with zero
    events."""

    job = unique_job_id()
    base = reidentify_bundle(tmp_path / "audit-base", execution_id="review-audit", github_job_id=job)
    changed = tmp_path / "audit-changed"
    changed.mkdir()
    for path in base.rglob("*"):
        if path.is_file():
            target = changed / path.relative_to(base)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    mutate_record(changed, "journals/main.jsonl", 10, "elapsed_ms", 31337)
    evidence = GitHubEvidence(
        run=cast(Mapping[str, object], run_record(TRUSTED_RUN, 1)),
        jobs=(cast(Mapping[str, object], job_record(job, TRUSTED_RUN, 1, "e2e-bmaas")),),
        expected_job_id=job,
    )
    ingestor = Ingestor(dsn=dsn, schema=test_schema, quarantine_root=quarantine)
    try:
        first = ingestor.ingest_delivery(
            delivery=BundleDirectory(path=base),
            source=HostSourceContext(origin="host-audit"),
            github=evidence,
            redaction_attestation=full_proof(bundle=frozen_bundle(base)),
        )
        assert first.status == "committed"
        with pytest.raises(ledger.ConflictError):
            ingestor.ingest_delivery(
                delivery=BundleDirectory(path=changed),
                source=HostSourceContext(origin="host-audit"),
                github=evidence,
                redaction_attestation=full_proof(bundle=frozen_bundle(changed)),
            )
        receipts = {row.status: row for row in ingestor.list_receipts(execution_id="review-audit")}
        assert set(receipts) == {"committed", "failed"}
        failed = receipts["failed"]
        assert failed.failure_code == "ledger-error"
        assert failed.events_committed == 0
        # The authorized-but-failed delivery's observation tells the truth
        # on both axes: publication was authorized, and the outcome lives
        # in the receipt, not in a published claim.
        observations = ingestor.list_observations(receipt_id=failed.receipt_id)
        assert len(observations) == 1
        assert observations[0].publication_authorized is True
        assert observations[0].quarantine_reason is None
        events = direct_fetch(
            dsn, test_schema, "SELECT count(*) FROM {schema}.ledger_event WHERE execution_id = %s", ("review-audit",)
        )
        assert events == (first.events_appended,)
    finally:
        ingestor.close()


def test_evidence_input_validation() -> None:
    with pytest.raises(ValueError):
        GitHubEvidence(run="not-an-object")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        GitHubEvidence(run=run_record(1, 1), jobs=tuple(run_record(1, 1) for _ in range(1001)))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        GitHubEvidence(run=run_record(1, 1), expected_job_id=0)
    with pytest.raises(ValueError):
        GitHubEvidence(run=run_record(1, 1), tests_run_head="yes")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        GitHubEvidence(run=run_record(1, 1), trusted_creator_ids={1, 2})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        job_claim(execution={"job": "not-an-object", "repository": "a/b", "run_id": 1, "run_attempt": 1})  # type: ignore[arg-type]
