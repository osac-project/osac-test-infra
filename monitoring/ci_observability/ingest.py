"""Central bundle ingestion: durable receipts, quarantine, and gated
canonical publication (W01b).

The trusted caller hands over a frozen delivery (an explicit private bundle
directory, or an archive with the verified digest of the exact bytes it
received), the trusted source context, optionally the GitHub metadata to
reconcile against, and optionally a trusted redaction attestation (the W06
sanitizer's interface; nothing in production mints one yet).

Every payload field stays a claim. A delivery reaches the ledger only when
publication is proven: a VERIFIED job binding from the trusted mappings, a
full-coverage trusted redaction proof (the manifest plus every listed
file), and, for archives, a non-contradicted artifact proof. Everything
else stays durably quarantined: an immutable receipt, append-only
observations and proofs, the retained bytes, and zero canonical
execution, event, or conclusion writes. Raw sources are private to the
ingest role; the reader role keeps ledger access only.

Publication for one execution id is serialized by a per-execution
PostgreSQL advisory lock with a finite wait, held across authorization,
registration, conclusions, ownership, and the event writes; no read
transaction wraps the ledger writes, so the W01a acknowledgement
guarantee is preserved. An execution may grow over time: later deliveries
extend the stored event prefixes with new records, journals, and files,
while existing event hashes stay immutable and a delivery digest only
identifies its receipt. The acknowledgement is returned only after a
fresh second connection, with finite SQL and lock timeouts, sees the
committed receipt, every delivered journal's stored prefix, and the
expected authoritative columns.

Not in this unit: the shared GitHub poller and artifact fetching (W01c),
the transport and redaction sanitizer that produces attestations (W06),
native report adapters (W03), and quarantine retention or maintenance
(W05/W13).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TypeAlias, cast

import psycopg
from psycopg import sql

from . import bundle_reader, ledger, reconcile
from .bundle_reader import BundleArchive, BundleDirectory, BundleError, Delivery, FrozenBundle, FrozenFile
from .github_binding import IntegrityStatus, MalformedGitHubMetadataError, Verdict
from .ledger import (
    DEFAULT_LIST_ROWS,
    DEFAULT_SCHEMA,
    MAX_LIST_ROWS,
    MAX_OFFSET,
    SCHEMA_NAME_PATTERN,
    ExecutionRecord,
    JournalEvent,
    LedgerError,
    ValidationError,
)
from .reconcile import GitHubEvidence, ReconciledBinding, verified_job

__all__ = [
    "AckVerificationError",
    "ArtifactSourceContext",
    "ClaimedExecution",
    "HostSourceContext",
    "IngestError",
    "IngestResult",
    "Ingestor",
    "ObservationRow",
    "ProofRow",
    "ReceiptRow",
    "RedactionAttestation",
    "SourceConflictError",
    "SourceContext",
    "claimed_execution",
]

JSONValue: TypeAlias = bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"] | None

MAX_OBSERVATION_RECORD_BYTES = 4 * 1024 * 1024
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_STATUSES = ("received", "partial", "committed", "quarantined", "failed")

_RECEIPT_COLUMNS = (
    "receipt_id, source_kind, source_repository, source_run_id, source_artifact_name, source_artifact_id,"
    " source_origin, received_digest, manifest_sha256, manifest, files, file_count, total_bytes, total_records,"
    " execution_id, quarantine_dir, redaction_claim, source_states, status, events_committed, failure_code,"
    " first_seen_at, updated_at"
)
_OBSERVATION_COLUMNS = (
    "observation_id, receipt_id, execution_id, observed_at, run_record, job_records, artifact_records,"
    " status_records, trusted_context, job_verdict, job_reason, numeric_job_id, executed_in_attempt,"
    " execution_attempt, job_conclusion, run_conclusion, artifact_verdict, artifact_reason, artifact_integrity,"
    " artifact_expired, attributed_attempt, revision_verdict, revision_reason, tested_sha, revision_provenance,"
    " publication_authorized, quarantine_reason, binding_evidence"
)
_OBSERVATION_INSERT_COLUMNS = (
    "receipt_id, execution_id, run_record, job_records, artifact_records, status_records, trusted_context,"
    " job_verdict, job_reason, numeric_job_id, executed_in_attempt, execution_attempt, job_conclusion,"
    " run_conclusion, artifact_verdict, artifact_reason, artifact_integrity, artifact_expired,"
    " attributed_attempt, revision_verdict, revision_reason, tested_sha, revision_provenance,"
    " publication_authorized, quarantine_reason, binding_evidence"
)


class IngestError(Exception):
    """Base of the ingest pipeline's errors.

    The code is a fixed identifier and the message carries no untrusted
    text: no payload content, paths, URLs, tokens, SQL, or DSNs, and no
    exception chains.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class SourceConflictError(IngestError):
    """A delivery deterministically contradicts stored ownership or
    binding; nothing changed. receipt_id names the receipt to mark
    failed, when the error happens after one exists."""

    def __init__(self, code: str, *, receipt_id: int | None = None) -> None:
        super().__init__(code)
        self.receipt_id = receipt_id


class AckVerificationError(IngestError):
    """A commit the pipeline expected is not visible through a second
    connection."""


@dataclass(frozen=True, kw_only=True)
class ArtifactSourceContext:
    """Trusted context: the bytes were received as one GitHub artifact of
    one run.

    Identity comes from the central caller, which used its own credentials
    and job catalog; never from the payload. artifact_id is the
    artifacts-API identity when the caller fetched it: publication
    requires the binding to select exactly that artifact. run_attempt is
    the caller's knowledge of the producing attempt, recorded as a claim
    because the artifacts API does not attest it.
    """

    repository: str
    run_id: int
    artifact_name: str
    artifact_id: int | None = None
    run_attempt: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.repository, str) or not _REPOSITORY_RE.fullmatch(self.repository):
            raise ValueError("repository must be in owner/name form")
        if not _is_int(self.run_id) or self.run_id < 1:
            raise ValueError("run_id must be an integer >= 1")
        if not isinstance(self.artifact_name, str) or not 1 <= len(self.artifact_name) <= 256:
            raise ValueError("artifact_name must be a string of 1 to 256 characters")
        if self.artifact_id is not None and (not _is_int(self.artifact_id) or self.artifact_id < 1):
            raise ValueError("artifact_id must be None or an integer >= 1")
        if self.run_attempt is not None and (not _is_int(self.run_attempt) or self.run_attempt < 1):
            raise ValueError("run_attempt must be None or an integer >= 1")


@dataclass(frozen=True, kw_only=True)
class HostSourceContext:
    """Trusted context: an explicit private bundle directory from a
    registered, named host or collector. The named origin is the trusted
    external origin proof for the bytes; it proves nothing about GitHub
    identity, which still requires a VERIFIED job binding."""

    origin: str

    def __post_init__(self) -> None:
        if not isinstance(self.origin, str) or not 1 <= len(self.origin) <= 128:
            raise ValueError("origin must be a string of 1 to 128 characters")


SourceContext: TypeAlias = ArtifactSourceContext | HostSourceContext


@dataclass(frozen=True, kw_only=True)
class RedactionAttestation:
    """Trusted redaction proof from outside the payload (the W06
    sanitizer; no production path mints one yet).

    A bundle's own redaction flag is a claim, never proof. covered_digests
    names the digests the policy actually covered; publication requires
    the manifest and every listed file to be covered, so partial coverage
    stays visible instead of looking like full proof.
    """

    policy: str
    covered_digests: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.policy, str) or not 1 <= len(self.policy) <= 128:
            raise ValueError("policy must be a string of 1 to 128 characters")
        if not isinstance(self.covered_digests, tuple) or not all(
            isinstance(digest, str) and _SHA256_HEX_RE.fullmatch(digest) for digest in self.covered_digests
        ):
            raise ValueError("covered_digests must be a tuple of 64-character hex digests")


@dataclass(frozen=True, kw_only=True)
class ClaimedExecution:
    """The execution identity a manifest claims. Claims never bind
    anything; the ledger's authoritative columns come only from a VERIFIED
    reconciliation, including the repository id, which is taken from the
    trusted run record rather than the payload."""

    execution_id: str
    schema_version: int
    repository_id: int
    repository: str
    run_id: int
    run_attempt: int
    event: str
    logical_job_key: str
    github_job_id: int | None
    job_name: str | None


def claimed_execution(*, manifest: Mapping[str, JSONValue]) -> ClaimedExecution:
    """Extract the claimed execution identity from a validated manifest."""

    execution = cast(Mapping[str, JSONValue], manifest["execution"])
    job = cast(Mapping[str, JSONValue], execution["job"])
    return ClaimedExecution(
        execution_id=cast(str, execution["execution_id"]),
        schema_version=cast(int, manifest["schema_version"]),
        repository_id=cast(int, execution["repository_id"]),
        repository=cast(str, execution["repository"]),
        run_id=cast(int, execution["run_id"]),
        run_attempt=cast(int, execution["run_attempt"]),
        event=cast(str, execution["event"]),
        logical_job_key=cast(str, job["logical_job_key"]),
        github_job_id=cast(int | None, job["github_job_id"]),
        job_name=cast(str | None, job.get("job_name")),
    )


@dataclass(frozen=True, kw_only=True)
class IngestResult:
    """Acknowledgement of one durably recorded delivery.

    status is 'committed' (published: every delivered record is in the
    ledger) or 'quarantined' (retained privately with zero canonical
    writes). redaction_state is derived from the trusted proofs recorded
    for the receipt, so it agrees with what a fresh connection persists.
    """

    receipt_id: int
    execution_id: str
    status: str
    job_verdict: str
    redaction_state: str
    quarantine_reason: str | None
    events_appended: int
    events_duplicates: int


@dataclass(frozen=True, kw_only=True)
class ReceiptRow:
    """One stored source receipt. redaction_state is derived from the
    receipt's trusted redaction proofs, never from the payload's claim."""

    receipt_id: int
    source_kind: str
    source_repository: str
    source_run_id: int | None
    source_artifact_name: str
    source_artifact_id: int | None
    source_origin: str
    received_digest: str
    manifest_sha256: str
    manifest: dict[str, JSONValue]
    files: list[dict[str, JSONValue]]
    file_count: int
    total_bytes: int
    total_records: int
    execution_id: str
    quarantine_dir: str
    redaction_state: str
    redaction_claim: dict[str, JSONValue]
    source_states: dict[str, JSONValue]
    status: str
    events_committed: int
    failure_code: str | None
    first_seen_at: datetime
    updated_at: datetime


@dataclass(frozen=True, kw_only=True)
class ObservationRow:
    """One stored reconciliation observation: the raw records supplied,
    the bounded trusted context used, and whether that delivery was
    authorized to publish."""

    observation_id: int
    receipt_id: int
    execution_id: str
    observed_at: datetime
    run_record: dict[str, JSONValue] | None
    job_records: list[JSONValue] | None
    artifact_records: list[JSONValue] | None
    status_records: list[JSONValue] | None
    trusted_context: dict[str, JSONValue]
    job_verdict: str
    job_reason: str
    numeric_job_id: int | None
    executed_in_attempt: bool | None
    execution_attempt: int | None
    job_conclusion: str | None
    run_conclusion: str | None
    artifact_verdict: str | None
    artifact_reason: str | None
    artifact_integrity: str | None
    artifact_expired: bool | None
    attributed_attempt: int | None
    revision_verdict: str | None
    revision_reason: str | None
    tested_sha: str | None
    revision_provenance: str | None
    publication_authorized: bool
    quarantine_reason: str | None
    binding_evidence: dict[str, JSONValue]


@dataclass(frozen=True, kw_only=True)
class ProofRow:
    """One stored trusted redaction proof."""

    proof_id: int
    receipt_id: int
    observed_at: datetime
    policy: str
    covered_digests: list[str]
    covered_all: bool


class Ingestor:
    """Ingest client bound to one schema, for use by one thread.

    Follows the ledger client's connection rules: autocommit, one explicit
    transaction per write, a dropped cached connection replaced at the
    next operation, and explicit connect, statement, and lock timeouts
    that override the DSN. The ledger tables are written through the
    accepted Ledger API; the source tables through this class's own
    connection. The acknowledgement path opens a fresh second connection
    with the same finite timeouts, so it never reads back through the
    connection that wrote.
    """

    def __init__(
        self,
        *,
        dsn: str,
        schema: str = DEFAULT_SCHEMA,
        quarantine_root: Path,
        application_name: str = "ci-observability-ingest",
        connect_timeout_seconds: float = 10.0,
        statement_timeout_ms: int = 30_000,
        lock_timeout_ms: int = 10_000,
    ) -> None:
        if not isinstance(schema, str) or not SCHEMA_NAME_PATTERN.fullmatch(schema):
            raise ValueError("schema name must be a lowercase identifier of at most 63 characters")
        if connect_timeout_seconds < 1 or statement_timeout_ms < 1 or lock_timeout_ms < 1:
            raise ValueError("connection timeouts must be positive")
        self._dsn = dsn
        self._schema = schema
        self._application_name = application_name
        self._connect_timeout_seconds = connect_timeout_seconds
        self._statement_timeout_ms = statement_timeout_ms
        self._lock_timeout_ms = lock_timeout_ms
        self._conn: psycopg.Connection | None = None
        self._ledger = ledger.Ledger(
            dsn=dsn,
            schema=schema,
            application_name=application_name,
            connect_timeout_seconds=connect_timeout_seconds,
            statement_timeout_ms=statement_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
        )
        self._quarantine_root = quarantine_root
        # Creates the root or validates the existing one; never follows a
        # symlinked leaf and never chmods anything that already existed.
        bundle_reader.ensure_private_directory(directory=quarantine_root)

    @property
    def schema(self) -> str:
        return self._schema

    @property
    def quarantine_root(self) -> Path:
        return self._quarantine_root

    def close(self) -> None:
        self._drop_connection()
        self._ledger.close()

    def ingest_delivery(
        self,
        *,
        delivery: Delivery,
        source: SourceContext,
        github: GitHubEvidence | None = None,
        redaction_attestation: RedactionAttestation | None = None,
    ) -> IngestResult:
        """Freeze one delivery and ingest it.

        An archive must arrive with an ArtifactSourceContext and a
        directory with a HostSourceContext, so the receipt's source kind
        always says how the bytes were actually received.
        """

        if isinstance(delivery, BundleArchive) and not isinstance(source, ArtifactSourceContext):
            raise IngestError("source-kind-mismatch", "an archive delivery requires an artifact source context")
        if isinstance(delivery, BundleDirectory) and not isinstance(source, HostSourceContext):
            raise IngestError("source-kind-mismatch", "a directory delivery requires a host source context")
        bundle = bundle_reader.freeze(delivery=delivery)
        received_digest = delivery.received_digest if isinstance(delivery, BundleArchive) else None
        return self.ingest_bundle(
            bundle=bundle,
            source=source,
            github=github,
            redaction_attestation=redaction_attestation,
            received_digest=received_digest,
        )

    def ingest_bundle(
        self,
        *,
        bundle: FrozenBundle,
        source: SourceContext,
        github: GitHubEvidence | None = None,
        redaction_attestation: RedactionAttestation | None = None,
        received_digest: str | None = None,
    ) -> IngestResult:
        """Ingest one frozen bundle.

        The bundle is first revalidated from its pinned bytes
        (revalidate_frozen), so a mutated parsed manifest or a forged file
        tuple cannot bypass validation or change what is stored.
        received_digest is the sha256 of the exact archive bytes the
        trusted caller received (the freeze verified the archive against
        it); it anchors the receipt and the artifact binding. Without it
        the receipt deduplicates on the manifest content digest, and the
        artifact binding records byte integrity as not checked (which
        refuses publication).
        """

        try:
            result = self._ingest(
                bundle=bundle,
                source=source,
                github=github,
                redaction_attestation=redaction_attestation,
                received_digest=received_digest,
            )
        except SourceConflictError as exc:
            if exc.receipt_id is not None:
                self._mark_failed(receipt_id=exc.receipt_id, code=exc.code)
            raise
        except psycopg.Error:
            # Transient infrastructure failure: the receipt keeps its last
            # durable state and the delivery is not acknowledged.
            raise IngestError("database-error") from None
        return result

    def _ingest(
        self,
        *,
        bundle: FrozenBundle,
        source: SourceContext,
        github: GitHubEvidence | None,
        redaction_attestation: RedactionAttestation | None,
        received_digest: str | None,
    ) -> IngestResult:
        bundle = bundle_reader.revalidate_frozen(bundle=bundle)
        claim = claimed_execution(manifest=bundle.manifest)
        binding = self._reconcile(bundle=bundle, source=source, github=github, received_digest=received_digest)
        receipt_digest = received_digest if received_digest is not None else "sha256:" + bundle.manifest_sha256
        receipt_id = self._record_receipt(bundle=bundle, source=source, claim=claim, received_digest=receipt_digest)
        try:
            if redaction_attestation is not None:
                self._record_proof(receipt_id=receipt_id, bundle=bundle, attestation=redaction_attestation)
            # The durable private quarantine copy is written for every
            # delivery, published or quarantined: it is the retained
            # evidence either way.
            self._materialize(bundle=bundle)
            refusal = _publication_refusal(
                binding=binding, source=source, has_full_proof=self._has_full_proof(receipt_id=receipt_id)
            )
            self._record_observation(
                receipt_id=receipt_id,
                bundle=bundle,
                claim=claim,
                binding=binding,
                github=github,
                received_digest=received_digest,
                publication_authorized=refusal is None,
                quarantine_reason=refusal,
            )
            redaction_state = self._redaction_state(receipt_id=receipt_id, bundle=bundle)
            job_verdict = binding.job.verdict.value if binding is not None and binding.job is not None else "unknown"
            if refusal is not None:
                self._mark_quarantined(receipt_id=receipt_id)
                return IngestResult(
                    receipt_id=receipt_id,
                    execution_id=claim.execution_id,
                    status="quarantined",
                    job_verdict=job_verdict,
                    redaction_state=redaction_state,
                    quarantine_reason=refusal,
                    events_appended=0,
                    events_duplicates=0,
                )
            appended, duplicates = self._publish(claim=claim, bundle=bundle, binding=binding, receipt_id=receipt_id)
            self._verify_commit(receipt_id=receipt_id, bundle=bundle, claim=claim, binding=binding)
        except LedgerError:
            self._mark_failed(receipt_id=receipt_id, code="ledger-error")
            raise
        return IngestResult(
            receipt_id=receipt_id,
            execution_id=claim.execution_id,
            status="committed",
            job_verdict=job_verdict,
            redaction_state=redaction_state,
            quarantine_reason=None,
            events_appended=appended,
            events_duplicates=duplicates,
        )

    def get_receipt(self, *, receipt_id: int) -> ReceiptRow | None:
        """Return one receipt, or None when it does not exist."""

        if not _is_int(receipt_id) or receipt_id < 1:
            raise ValidationError("receipt_id must be an integer >= 1")
        with self._connection().cursor() as cur:
            cur.execute(
                sql.SQL(f"SELECT {_RECEIPT_COLUMNS} FROM {{schema}}.source_receipt WHERE receipt_id = %s").format(
                    schema=sql.Identifier(self._schema)
                ),
                (receipt_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return _receipt_row(row=row, redaction_state=self._redaction_state_for_rows([row])[receipt_id])

    def list_receipts(
        self,
        *,
        repository: str | None = None,
        run_id: int | None = None,
        execution_id: str | None = None,
        status: str | None = None,
        unbound: bool | None = None,
        limit: int = DEFAULT_LIST_ROWS,
        offset: int = 0,
    ) -> list[ReceiptRow]:
        """List receipts matching every provided filter, bounded.

        unbound selects receipts with no VERIFIED job observation: the
        durable, queryable record of deliveries whose job binding was
        never proven. Receipts stay queryable whether quarantined,
        committed, or rejected, so unknown origins are never silently
        dropped.
        """

        clauses: list[sql.Composed] = []
        params: list[object] = []
        if repository is not None:
            if not isinstance(repository, str) or not _REPOSITORY_RE.fullmatch(repository):
                raise ValidationError("repository must be in owner/name form")
            clauses.append(sql.SQL("source_repository = %s"))
            params.append(repository)
        if run_id is not None:
            if not _is_int(run_id) or run_id < 1:
                raise ValidationError("run_id must be an integer >= 1")
            clauses.append(sql.SQL("source_run_id = %s"))
            params.append(run_id)
        if execution_id is not None:
            if not isinstance(execution_id, str) or not ledger.EXECUTION_ID_PATTERN.fullmatch(execution_id):
                raise ValidationError("execution_id must be a bounded filename-safe identifier")
            clauses.append(sql.SQL("execution_id = %s"))
            params.append(execution_id)
        if status is not None:
            if status not in _RECEIPT_STATUSES:
                raise ValidationError("status must be one of " + ", ".join(_RECEIPT_STATUSES))
            clauses.append(sql.SQL("status = %s"))
            params.append(status)
        if unbound is not None:
            if not isinstance(unbound, bool):
                raise ValidationError("unbound must be a boolean")
            verified_exists = sql.SQL(
                "EXISTS (SELECT 1 FROM {schema}.source_github_observation o"
                " WHERE o.receipt_id = source_receipt.receipt_id AND o.job_verdict = 'verified')"
            ).format(schema=sql.Identifier(self._schema))
            clauses.append((sql.SQL("NOT ") + verified_exists) if unbound else verified_exists)
        _validate_page(limit=limit, offset=offset)
        where = sql.SQL(" AND ").join(clauses) if clauses else sql.SQL("TRUE")
        query = sql.SQL(
            f"SELECT {_RECEIPT_COLUMNS} FROM {{schema}}.source_receipt WHERE {{where}}"
            " ORDER BY receipt_id DESC LIMIT %s OFFSET %s"
        ).format(schema=sql.Identifier(self._schema), where=where)
        with self._connection().cursor() as cur:
            cur.execute(query, (*params, limit, offset))
            rows = cur.fetchall()
            states = self._redaction_state_for_rows(rows)
            return [_receipt_row(row=row, redaction_state=states[cast(int, row[0])]) for row in rows]

    def list_observations(
        self,
        *,
        execution_id: str | None = None,
        receipt_id: int | None = None,
        limit: int = DEFAULT_LIST_ROWS,
        offset: int = 0,
    ) -> list[ObservationRow]:
        """List reconciliation observations, bounded, oldest first."""

        clauses: list[sql.Composed] = []
        params: list[object] = []
        if execution_id is not None:
            if not isinstance(execution_id, str) or not ledger.EXECUTION_ID_PATTERN.fullmatch(execution_id):
                raise ValidationError("execution_id must be a bounded filename-safe identifier")
            clauses.append(sql.SQL("execution_id = %s"))
            params.append(execution_id)
        if receipt_id is not None:
            if not _is_int(receipt_id) or receipt_id < 1:
                raise ValidationError("receipt_id must be an integer >= 1")
            clauses.append(sql.SQL("receipt_id = %s"))
            params.append(receipt_id)
        if not clauses:
            raise ValidationError("execution_id or receipt_id is required")
        _validate_page(limit=limit, offset=offset)
        where = sql.SQL(" AND ").join(clauses)
        query = sql.SQL(
            f"SELECT {_OBSERVATION_COLUMNS} FROM {{schema}}.source_github_observation WHERE {{where}}"
            " ORDER BY observation_id LIMIT %s OFFSET %s"
        ).format(schema=sql.Identifier(self._schema), where=where)
        with self._connection().cursor() as cur:
            cur.execute(query, (*params, limit, offset))
            return [_observation_row(row=row) for row in cur.fetchall()]

    def list_redaction_proofs(
        self, *, receipt_id: int, limit: int = DEFAULT_LIST_ROWS, offset: int = 0
    ) -> list[ProofRow]:
        """List the trusted redaction proofs recorded for one receipt,
        oldest first."""

        if not _is_int(receipt_id) or receipt_id < 1:
            raise ValidationError("receipt_id must be an integer >= 1")
        _validate_page(limit=limit, offset=offset)
        query = sql.SQL(
            "SELECT proof_id, receipt_id, observed_at, policy, covered_digests, covered_all"
            " FROM {schema}.source_redaction_proof WHERE receipt_id = %s"
            " ORDER BY proof_id LIMIT %s OFFSET %s"
        ).format(schema=sql.Identifier(self._schema))
        with self._connection().cursor() as cur:
            cur.execute(query, (receipt_id, limit, offset))
            return [
                ProofRow(
                    proof_id=cast(int, row[0]),
                    receipt_id=cast(int, row[1]),
                    observed_at=cast(datetime, row[2]),
                    policy=cast(str, row[3]),
                    covered_digests=cast(list[str], row[4]),
                    covered_all=cast(bool, row[5]),
                )
                for row in cur.fetchall()
            ]

    # Reconciliation, receipts, proofs, and the publication gate.

    def _reconcile(
        self, *, bundle: FrozenBundle, source: SourceContext, github: GitHubEvidence | None, received_digest: str | None
    ) -> ReconciledBinding | None:
        """Compute the bindings from the supplied trusted metadata, if any.

        Malformed metadata is a caller error and fails before any write:
        no receipt exists for a delivery whose trusted context could not
        even be parsed.
        """

        if github is None:
            return None
        claim = reconcile.job_claim(execution=cast(Mapping[str, JSONValue], bundle.manifest["execution"]))
        artifact = None
        if isinstance(source, ArtifactSourceContext):
            artifact = reconcile.artifact_claim(
                repository=source.repository,
                run_id=source.run_id,
                artifact_name=source.artifact_name,
                run_attempt=source.run_attempt,
            )
        try:
            return reconcile.reconcile(
                evidence=github,
                claim=claim,
                artifact_claim=artifact,
                received_digest=received_digest,
                revision_override=reconcile.claimed_tested_override(manifest=bundle.manifest),
            )
        except MalformedGitHubMetadataError as exc:
            raise IngestError("metadata-malformed", str(exc)) from None

    def _record_receipt(
        self, *, bundle: FrozenBundle, source: SourceContext, claim: ClaimedExecution, received_digest: str
    ) -> int:
        """Insert the idempotent receipt: one row per trusted source
        identity plus received digest, content-pinned by the manifest
        digest."""

        files_json = json.dumps(
            [
                {
                    "path": file.path,
                    "kind": file.kind,
                    "sha256": file.sha256,
                    "size_bytes": file.size_bytes,
                    "record_count": file.record_count,
                    "complete": file.complete,
                }
                for file in bundle.files
            ],
            sort_keys=True,
        )
        source_states_json = json.dumps(bundle.source_states, sort_keys=True)
        redaction_claim_json = json.dumps(bundle.manifest["redaction"], sort_keys=True)
        identity = _source_identity(source)
        conn = self._connection()
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "INSERT INTO {schema}.source_receipt (source_kind, source_repository, source_run_id,"
                    " source_artifact_name, source_artifact_id, source_origin, received_digest, manifest_sha256,"
                    " manifest, files, file_count, total_bytes, total_records, execution_id, quarantine_dir,"
                    " redaction_claim, source_states, status)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::json, %s::json, %s, %s, %s, %s, %s,"
                    " %s::json, %s::json, 'received')"
                    " ON CONFLICT (source_kind, source_repository, source_run_id, source_artifact_name,"
                    " source_artifact_id, source_origin, received_digest) DO NOTHING"
                ).format(schema=sql.Identifier(self._schema)),
                (
                    *identity,
                    received_digest,
                    bundle.manifest_sha256,
                    bundle.manifest_text,
                    files_json,
                    len(bundle.files),
                    sum(file.size_bytes for file in bundle.files),
                    bundle.total_records,
                    claim.execution_id,
                    bundle.manifest_sha256,
                    redaction_claim_json,
                    source_states_json,
                ),
            )
            cur.execute(
                sql.SQL(
                    "SELECT receipt_id, manifest_sha256 FROM {schema}.source_receipt"
                    " WHERE source_kind = %s AND source_repository = %s AND source_run_id = %s"
                    " AND source_artifact_name = %s AND source_artifact_id = %s AND source_origin = %s"
                    " AND received_digest = %s FOR UPDATE"
                ).format(schema=sql.Identifier(self._schema)),
                (*identity, received_digest),
            )
            row = cur.fetchone()
            if row is None:
                raise IngestError("receipt-vanished")
            if row[1] != bundle.manifest_sha256:
                raise SourceConflictError("receipt-content-conflict", receipt_id=cast(int, row[0]))
            return cast(int, row[0])

    def _record_proof(self, *, receipt_id: int, bundle: FrozenBundle, attestation: RedactionAttestation) -> None:
        """Append one trusted redaction proof. covered_all says whether it
        covers the manifest and every listed file, which is the publication
        gate; the receipt's immutable content is never rewritten."""

        required = {bundle.manifest_sha256} | {file.sha256 for file in bundle.files}
        covered = set(attestation.covered_digests)
        conn = self._connection()
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "INSERT INTO {schema}.source_redaction_proof (receipt_id, policy, covered_digests, covered_all)"
                    " VALUES (%s, %s, %s::json, %s)"
                ).format(schema=sql.Identifier(self._schema)),
                (receipt_id, attestation.policy, json.dumps(sorted(attestation.covered_digests)), required <= covered),
            )

    def _has_full_proof(self, *, receipt_id: int) -> bool:
        with self._connection().cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT EXISTS (SELECT 1 FROM {schema}.source_redaction_proof"
                    " WHERE receipt_id = %s AND covered_all)"
                ).format(schema=sql.Identifier(self._schema)),
                (receipt_id,),
            )
            return cur.fetchone()[0]

    def _redaction_state_for_rows(self, rows: list[tuple[object, ...]]) -> dict[int, str]:
        """The derived redaction state for a page of receipts: attested
        when any proof covers everything, partially-attested when a proof
        covers something, otherwise the unverified claim."""

        states: dict[int, str] = {}
        if not rows:
            return states
        receipt_ids = [cast(int, row[0]) for row in rows]
        proofs: dict[int, list[tuple[bool, set[str]]]] = {}
        with self._connection().cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT receipt_id, covered_all, covered_digests FROM {schema}.source_redaction_proof"
                    " WHERE receipt_id = ANY(%s)"
                ).format(schema=sql.Identifier(self._schema)),
                (receipt_ids,),
            )
            for receipt_id, covered_all, covered_digests in cur.fetchall():
                proofs.setdefault(cast(int, receipt_id), []).append(
                    (cast(bool, covered_all), set(cast(list[str], covered_digests)))
                )
        for row in rows:
            receipt_id = cast(int, row[0])
            manifest_sha256 = cast(str, row[8])
            file_digests = {cast(str, entry["sha256"]) for entry in cast(list[dict[str, object]], row[10])}
            required = {manifest_sha256} | file_digests
            recorded = proofs.get(receipt_id, [])
            if any(covered_all for covered_all, _ in recorded):
                states[receipt_id] = "attested"
            elif any(covered & required for _, covered in recorded):
                states[receipt_id] = "partially-attested"
            else:
                states[receipt_id] = "unverified-claim"
        return states

    def _redaction_state(self, *, receipt_id: int, bundle: FrozenBundle) -> str:
        """The receipt's derived redaction state after this delivery."""

        required = {bundle.manifest_sha256} | {file.sha256 for file in bundle.files}
        with self._connection().cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT covered_all, covered_digests FROM {schema}.source_redaction_proof WHERE receipt_id = %s"
                ).format(schema=sql.Identifier(self._schema)),
                (receipt_id,),
            )
            recorded = cur.fetchall()
        if any(cast(bool, covered_all) for covered_all, _ in recorded):
            return "attested"
        if any(set(cast(list[str], covered)) & required for _, covered in recorded):
            return "partially-attested"
        return "unverified-claim"

    def _record_observation(
        self,
        *,
        receipt_id: int,
        bundle: FrozenBundle,
        claim: ClaimedExecution,
        binding: ReconciledBinding | None,
        github: GitHubEvidence | None,
        received_digest: str | None,
        publication_authorized: bool,
        quarantine_reason: str | None,
    ) -> None:
        """Append one reconciliation observation: the raw records as
        supplied, the bounded trusted context used, and whether publication
        was authorized (the gate decision, not the outcome: the actual
        outcome is the receipt's forward-only status). Earlier observations
        never change."""

        trusted_context = (
            reconcile.trusted_context(evidence=github, received_digest=received_digest)
            if github is not None
            else {"received_digest": received_digest}
        )
        if binding is None or github is None:
            # Columns: identity(2), raw records(4, absent), trusted context,
            # verdict(2), verdict details(14, absent), decision(2), evidence.
            values: tuple[object, ...] = (
                receipt_id,
                claim.execution_id,
                None,
                None,
                None,
                None,
                json.dumps(trusted_context, sort_keys=True),
                "unknown",
                "no-metadata-supplied",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                publication_authorized,
                quarantine_reason,
                json.dumps(
                    {
                        "claimed_execution_id": claim.execution_id,
                        "claimed_repository": claim.repository,
                        "claimed_repository_id": claim.repository_id,
                        "claimed_run_id": claim.run_id,
                        "claimed_run_attempt": claim.run_attempt,
                        "claimed_logical_job_key": claim.logical_job_key,
                    },
                    sort_keys=True,
                ),
            )
        else:
            job = binding.job
            artifact = binding.artifact
            revision = binding.revision
            run_text = json.dumps(dict(github.run), sort_keys=True)
            jobs_text = json.dumps([dict(record) for record in github.jobs], sort_keys=True)
            artifacts_text = json.dumps([dict(record) for record in github.artifacts], sort_keys=True)
            statuses_text = json.dumps([dict(record) for record in github.statuses], sort_keys=True)
            if len(run_text) + len(jobs_text) + len(artifacts_text) + len(statuses_text) > (
                MAX_OBSERVATION_RECORD_BYTES
            ):
                raise IngestError("metadata-oversize")
            values = (
                receipt_id,
                claim.execution_id,
                run_text,
                jobs_text,
                artifacts_text,
                statuses_text,
                json.dumps(trusted_context, sort_keys=True),
                job.verdict.value,
                job.reason,
                job.numeric_job_id,
                job.executed_in_attempt,
                job.execution_attempt,
                job.github_conclusion,
                binding.run_conclusion,
                artifact.verdict.value if artifact is not None else None,
                artifact.reason if artifact is not None else None,
                artifact.integrity.value if artifact is not None else None,
                artifact.expired if artifact is not None else None,
                artifact.attributed_attempt if artifact is not None else None,
                revision.verdict.value if revision is not None else None,
                revision.reason if revision is not None else None,
                revision.tested_sha if revision is not None else None,
                revision.provenance.value if revision is not None and revision.provenance is not None else None,
                publication_authorized,
                quarantine_reason,
                json.dumps(_binding_evidence(claim=claim, binding=binding), sort_keys=True),
            )
        conn = self._connection()
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    f"INSERT INTO {{schema}}.source_github_observation ({_OBSERVATION_INSERT_COLUMNS})"
                    " VALUES (%s, %s, %s::json, %s::json, %s::json, %s::json, %s::json, %s, %s, %s, %s, %s, %s,"
                    " %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::json)"
                ).format(schema=sql.Identifier(self._schema)),
                values,
            )

    def _mark_quarantined(self, *, receipt_id: int) -> None:
        """Move a receipt to quarantined, unless it already progressed
        further (a published receipt never regresses)."""

        conn = self._connection()
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "UPDATE {schema}.source_receipt SET status = 'quarantined'"
                    " WHERE receipt_id = %s AND status IN ('received', 'quarantined')"
                ).format(schema=sql.Identifier(self._schema)),
                (receipt_id,),
            )

    # Publication: serialized per execution, ledger writes only through
    # the accepted Ledger API.

    def _publish(
        self, *, claim: ClaimedExecution, bundle: FrozenBundle, binding: ReconciledBinding, receipt_id: int
    ) -> tuple[int, int]:
        """Register, bind, and append the canonical event prefixes.

        The whole sequence runs under a per-execution advisory lock with a
        finite wait, so authorization, ownership, registration,
        conclusions, and event appends are serialized across every
        connection and process: a racing delivery cannot slip between the
        authorization check and the ownership claim. The lock is a session
        lock on the source connection; no read transaction wraps the ledger
        writes.
        """

        verified = verified_job(binding)
        if verified is None:  # unreachable: the gate refused those deliveries
            raise IngestError("internal-error")
        key = _execution_lock_key(claim.execution_id)
        conn = self._connection()
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT pg_advisory_lock(%s)", (key,))
            except (psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled):
                raise IngestError("execution-lock-timeout") from None
        try:
            self._authorize_locked(claim=claim, binding=binding, receipt_id=receipt_id)
            self._ledger.register_execution(
                record=ExecutionRecord(
                    execution_id=claim.execution_id,
                    schema_version=claim.schema_version,
                    repository_id=binding.repository_id,
                    run_id=claim.run_id,
                    run_attempt=claim.run_attempt,
                    logical_job_key=claim.logical_job_key,
                    executed=verified.executed_in_attempt,
                    github_job_id=verified.numeric_job_id,
                )
            )
            if binding.run_conclusion is not None or verified.github_conclusion is not None:
                self._ledger.record_authoritative_conclusion(
                    execution_id=claim.execution_id,
                    github_run_conclusion=binding.run_conclusion,
                    github_job_conclusion=verified.github_conclusion,
                )
            self._claim_ownership(
                execution_id=claim.execution_id, receipt_id=receipt_id, job_id=verified.numeric_job_id
            )
            appended = 0
            duplicates = 0
            confirmed = 0
            for batch in _event_batches(bundle=bundle):
                result = self._ledger.append_events(events=batch)
                appended += result.appended
                duplicates += result.duplicates
                confirmed += len(batch)
                self._update_progress(receipt_id=receipt_id, confirmed=confirmed, committed=False)
            self._update_progress(receipt_id=receipt_id, confirmed=confirmed, committed=True)
            return appended, duplicates
        finally:
            # A dropped session releases the lock server-side; a failed
            # unlock must not mask the operation's own outcome.
            with contextlib.suppress(psycopg.Error), conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (key,))

    def _authorize_locked(self, *, claim: ClaimedExecution, binding: ReconciledBinding, receipt_id: int) -> None:
        """Binding authorization under the execution lock, before any
        ledger write: an execution already bound to a different verified
        job rejects this delivery without changing anything."""

        verified = verified_job(binding)
        if verified is None:
            # Unreachable: the publication gate refused those deliveries.
            raise IngestError("internal-error")
        row = self._ledger.get_execution(execution_id=claim.execution_id)
        if row is not None and row.github_job_id is not None and row.github_job_id != verified.numeric_job_id:
            raise SourceConflictError("execution-bound-to-different-job", receipt_id=receipt_id)
        owner = self._owner(execution_id=claim.execution_id)
        if owner is not None and owner != verified.numeric_job_id:
            raise SourceConflictError("execution-bound-to-different-job", receipt_id=receipt_id)

    def _claim_ownership(self, *, execution_id: str, receipt_id: int, job_id: int) -> None:
        """Record the first receipt that published this execution and the
        verified job it is bound to. Ownership is stable: later deliveries
        for the same job extend the prefixes; a different job was already
        rejected under the lock."""

        conn = self._connection()
        try:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    sql.SQL(
                        "INSERT INTO {schema}.source_execution (execution_id, receipt_id, verified_job_id)"
                        " VALUES (%s, %s, %s) ON CONFLICT (execution_id) DO NOTHING"
                    ).format(schema=sql.Identifier(self._schema)),
                    (execution_id, receipt_id, job_id),
                )
                cur.execute(
                    sql.SQL(
                        "SELECT verified_job_id FROM {schema}.source_execution WHERE execution_id = %s FOR UPDATE"
                    ).format(schema=sql.Identifier(self._schema)),
                    (execution_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise IngestError("ownership-vanished")
                if row[0] != job_id:
                    raise SourceConflictError("execution-bound-to-different-job", receipt_id=receipt_id)
        except psycopg.errors.ForeignKeyViolation:
            raise IngestError("ownership-order-violation") from None

    def _update_progress(self, *, receipt_id: int, confirmed: int, committed: bool) -> None:
        """Move the receipt forward: partial after each committed batch,
        committed when every delivered record is stored.

        events_committed uses GREATEST so concurrent redeliveries of
        overlapping prefixes converge instead of fighting the monotonic
        guard: once records are confirmed stored they stay stored, because
        events are append-only. A partial update skips receipts that
        already progressed further, so a stale shorter replay never
        downgrades one.
        """

        conn = self._connection()
        try:
            with conn.transaction(), conn.cursor() as cur:
                if committed:
                    cur.execute(
                        sql.SQL(
                            "UPDATE {schema}.source_receipt SET status = 'committed',"
                            " events_committed = GREATEST(events_committed, %s) WHERE receipt_id = %s"
                        ).format(schema=sql.Identifier(self._schema)),
                        (confirmed, receipt_id),
                    )
                    if cur.rowcount != 1:
                        raise IngestError("receipt-vanished")
                else:
                    cur.execute(
                        sql.SQL(
                            "UPDATE {schema}.source_receipt SET status = 'partial',"
                            " events_committed = GREATEST(events_committed, %s)"
                            " WHERE receipt_id = %s AND status IN ('received', 'partial', 'quarantined')"
                        ).format(schema=sql.Identifier(self._schema)),
                        (confirmed, receipt_id),
                    )
        except psycopg.errors.ReadOnlySqlTransaction:
            # The receipt guard rejected the transition, the same condition
            # the ledger maps to an immutable-conflict error.
            raise SourceConflictError("receipt-state-conflict", receipt_id=receipt_id) from None

    def _mark_failed(self, *, receipt_id: int, code: str) -> None:
        """Best-effort terminal failure mark for deterministic conflicts.

        A receipt that is already terminal keeps its state, and a database
        that stays unreachable leaves the receipt at its last honest
        status; either way the error that caused the mark is what the
        caller sees.
        """

        try:
            conn = self._connection()
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    sql.SQL(
                        "UPDATE {schema}.source_receipt SET status = 'failed', failure_code = %s"
                        " WHERE receipt_id = %s AND status IN ('received', 'partial')"
                    ).format(schema=sql.Identifier(self._schema)),
                    (code, receipt_id),
                )
        except psycopg.Error:
            pass

    def _verify_commit(
        self, *, receipt_id: int, bundle: FrozenBundle, claim: ClaimedExecution, binding: ReconciledBinding
    ) -> None:
        """Acknowledge only what a second connection can see.

        Opens a fresh connection with finite SQL and lock timeouts, so the
        acknowledgement never reads back through the connection that
        wrote, and checks the receipt, every delivered journal's stored
        prefix (a stale shorter replay acknowledges its own keys, not a
        global count), and the execution's authoritative columns.
        """

        verified = verified_job(binding)
        try:
            conn = psycopg.connect(
                self._dsn,
                autocommit=True,
                application_name=f"{self._application_name}-verify",
                connect_timeout=int(self._connect_timeout_seconds),
                options=f"-c statement_timeout={self._statement_timeout_ms} -c lock_timeout={self._lock_timeout_ms}",
            )
        except psycopg.OperationalError:
            raise AckVerificationError("verify-connection-failed") from None
        try:
            receipt = conn.execute(
                sql.SQL("SELECT status FROM {schema}.source_receipt WHERE receipt_id = %s").format(
                    schema=sql.Identifier(self._schema)
                ),
                (receipt_id,),
            ).fetchone()
            if receipt is None or receipt[0] != "committed":
                raise AckVerificationError("receipt-commit-not-visible")
            for file in bundle.journal_files:
                if not file.record_count:
                    continue
                prefix = conn.execute(
                    sql.SQL(
                        "SELECT prefix_sequence FROM {schema}.ledger_journal"
                        " WHERE execution_id = %s AND journal_id = %s"
                    ).format(schema=sql.Identifier(self._schema)),
                    (claim.execution_id, _journal_id_of(file)),
                ).fetchone()
                if prefix is None or prefix[0] < file.record_count:
                    raise AckVerificationError("ledger-events-not-visible")
            execution = conn.execute(
                sql.SQL("SELECT github_job_id, executed FROM {schema}.ledger_execution WHERE execution_id = %s").format(
                    schema=sql.Identifier(self._schema)
                ),
                (claim.execution_id,),
            ).fetchone()
            if execution is None:
                raise AckVerificationError("execution-not-visible")
            if verified is None:
                if execution[0] is not None or execution[1] is not None:
                    raise AckVerificationError("unverified-delivery-appeared-bound")
            elif execution[0] != verified.numeric_job_id or execution[1] != verified.executed_in_attempt:
                raise AckVerificationError("binding-not-visible")
        finally:
            conn.close()

    def _owner(self, *, execution_id: str) -> int | None:
        with self._connection().cursor() as cur:
            cur.execute(
                sql.SQL("SELECT verified_job_id FROM {schema}.source_execution WHERE execution_id = %s").format(
                    schema=sql.Identifier(self._schema)
                ),
                (execution_id,),
            )
            row = cur.fetchone()
            return None if row is None else cast(int, row[0])

    def _materialize(self, *, bundle: FrozenBundle) -> None:
        """Write the durable private quarantine copy, content-addressed by
        the manifest digest, before any event is stored. A durability
        failure raises: no acknowledgement without a durable quarantine."""

        directory = self._quarantine_root / bundle.manifest_sha256
        try:
            bundle_reader.materialize_quarantine(bundle=bundle, directory=directory)
        except BundleError as exc:
            raise IngestError("quarantine-error", exc.code) from None
        except OSError:
            raise IngestError("quarantine-error") from None

    # Source-table connection handling, mirroring the ledger client.

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(
            self._dsn,
            autocommit=True,
            application_name=self._application_name,
            connect_timeout=int(self._connect_timeout_seconds),
            options=f"-c statement_timeout={self._statement_timeout_ms} -c lock_timeout={self._lock_timeout_ms}",
        )

    def _connection(self) -> psycopg.Connection:
        """Return a live connection, replacing a cached one the server
        dropped. An operation that fails mid-statement reports the
        failure; a database that stays down fails here."""

        if self._conn is None or self._conn.closed:
            self._conn = self._connect()
            return self._conn
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        except psycopg.OperationalError:
            self._drop_connection()
            self._conn = self._connect()
        return self._conn

    def _drop_connection(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(psycopg.Error):
                self._conn.close()
            self._conn = None


def _publication_refusal(
    *, binding: ReconciledBinding | None, source: SourceContext, has_full_proof: bool
) -> str | None:
    """None when the delivery may publish, else the fixed quarantine
    reason.

    Publication requires a VERIFIED job binding from the trusted
    mappings, a full-coverage trusted redaction proof, and, for archive
    sources, an artifact proof that is not contradicted. Artifact names
    and payload UUIDs prove nothing: an unknown proof quarantines rather
    than fake an association."""

    if binding is None:
        return "no-metadata"
    verified = verified_job(binding)
    if verified is None:
        return "unverified-job-binding"
    if verified.executed_in_attempt is False:
        return "job-not-executed-in-attempt"
    if not has_full_proof:
        return "redaction-unproven"
    if isinstance(source, ArtifactSourceContext):
        artifact = binding.artifact
        if artifact is None:
            return "artifact-proof-missing"
        if artifact.integrity is IntegrityStatus.MISMATCH:
            return "artifact-digest-contradiction"
        if artifact.verdict is not Verdict.VERIFIED:
            return "artifact-proof-missing"
        if artifact.integrity is not IntegrityStatus.VERIFIED:
            return "artifact-integrity-unproven"
        if artifact.attempt_conflict:
            return "artifact-attempt-contradiction"
        if source.artifact_id is not None and artifact.artifact_id != source.artifact_id:
            return "artifact-id-contradiction"
    return None


def _execution_lock_key(execution_id: str) -> int:
    digest = hashlib.sha256(("ci-observability-execution:" + execution_id).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") >> 1


def _journal_id_of(file: FrozenFile) -> str:
    return Path(file.path).stem


def _binding_evidence(*, claim: ClaimedExecution, binding: ReconciledBinding) -> dict[str, JSONValue]:
    """Structured, value-safe evidence for the observation row: verdicts,
    corroboration, and where the payload's claims disagreed with trusted
    metadata. Corroboration detail strings come from the binding module's
    own fixed vocabulary."""

    job = binding.job
    artifact = binding.artifact
    revision = binding.revision
    evidence: dict[str, JSONValue] = {
        "job": {
            "verdict": job.verdict.value,
            "reason": job.reason,
            "numeric_job_id": job.numeric_job_id,
            "executed_in_attempt": job.executed_in_attempt,
            "execution_attempt": job.execution_attempt,
            "candidate_count": len(job.candidates),
            "inherited_ref_count": len(job.inherited_refs),
            "foreign_job_count": len(job.foreign_jobs),
            "corroborations": [
                {"kind": item.kind, "status": item.status.value, "detail": item.detail} for item in job.corroborations
            ],
            "claimed_job_id": claim.github_job_id,
            "claimed_job_id_mismatch": (
                claim.github_job_id is not None
                and job.numeric_job_id is not None
                and claim.github_job_id != job.numeric_job_id
            ),
            "claimed_repository": claim.repository,
            "claimed_repository_mismatch": claim.repository != job.repo,
            "claimed_repository_id": claim.repository_id,
            "claimed_repository_id_mismatch": claim.repository_id != binding.repository_id,
        }
    }
    if artifact is not None:
        evidence["artifact"] = {
            "verdict": artifact.verdict.value,
            "reason": artifact.reason,
            "artifact_id": artifact.artifact_id,
            "integrity": artifact.integrity.value,
            "expired": artifact.expired,
            "claimed_attempt": artifact.claimed_attempt,
            "attributed_attempt": artifact.attributed_attempt,
            "timing_attribution": artifact.timing_attribution.value,
            "attempt_conflict": artifact.attempt_conflict,
        }
    if revision is not None:
        evidence["revision"] = {
            "verdict": revision.verdict.value,
            "reason": revision.reason,
            "tested_sha": revision.tested_sha,
            "provenance": revision.provenance.value if revision.provenance is not None else None,
        }
    return evidence


def _event_batches(*, bundle: FrozenBundle) -> Iterator[list[JournalEvent]]:
    """Split the bundle's journal records into ledger-sized batches.

    Journals are parsed one at a time from the frozen bytes, and batches
    respect the ledger's event-count and byte caps, so a maximal bundle
    streams through instead of being rebuilt as one huge batch.
    """

    batch: list[JournalEvent] = []
    batch_bytes = 0
    for file in sorted(bundle.journal_files, key=lambda item: item.path):
        for record in bundle_reader.parse_journal_records(file=file):
            event = _journal_event(record=record)
            size = len(ledger.canonical_payload(data=event.data).encode("utf-8"))
            if batch and (len(batch) >= ledger.MAX_BATCH_EVENTS or batch_bytes + size > ledger.MAX_BATCH_BYTES):
                yield batch
                batch = []
                batch_bytes = 0
            batch.append(event)
            batch_bytes += size
    if batch:
        yield batch


def _journal_event(*, record: Mapping[str, JSONValue]) -> JournalEvent:
    return JournalEvent(
        schema_version=cast(int, record["schema_version"]),
        execution_id=cast(str, record["execution_id"]),
        journal_id=cast(str, record["journal_id"]),
        sequence=cast(int, record["sequence"]),
        recorded_at=cast(str, record["recorded_at"]),
        event=cast(str, record["event"]),
        data=cast(dict[str, JSONValue], record["data"]),
        source_at=cast(str | None, record.get("source_at")),
        invocation_id=cast(str | None, record.get("invocation_id")),
        clock_id=cast(str | None, record.get("clock_id")),
        clock_uncertainty_ns=cast(int | None, record.get("clock_uncertainty_ns")),
        mono_ns=cast(int | None, record.get("mono_ns")),
    )


def _source_identity(source: SourceContext) -> tuple[str, str, int, str, int, str]:
    """The trusted source identity columns of a receipt: kind,
    repository, run, artifact name, artifact id, origin. Absent parts are
    the sentinel '' or 0, because unique indexes treat NULLs as distinct."""

    if isinstance(source, ArtifactSourceContext):
        return (
            "archive",
            source.repository,
            source.run_id,
            source.artifact_name,
            source.artifact_id if source.artifact_id is not None else 0,
            "",
        )
    return ("directory", "", 0, "", 0, source.origin)


def _receipt_row(*, row: tuple[object, ...], redaction_state: str) -> ReceiptRow:
    return ReceiptRow(
        receipt_id=cast(int, row[0]),
        source_kind=cast(str, row[1]),
        source_repository=cast(str, row[2]),
        source_run_id=cast(int | None, row[3]) or None,
        source_artifact_name=cast(str, row[4]),
        source_artifact_id=cast(int | None, row[5]) or None,
        source_origin=cast(str, row[6]),
        received_digest=cast(str, row[7]),
        manifest_sha256=cast(str, row[8]),
        manifest=cast(dict[str, JSONValue], row[9]),
        files=cast(list[dict[str, JSONValue]], row[10]),
        file_count=cast(int, row[11]),
        total_bytes=cast(int, row[12]),
        total_records=cast(int, row[13]),
        execution_id=cast(str, row[14]),
        quarantine_dir=cast(str, row[15]),
        redaction_state=redaction_state,
        redaction_claim=cast(dict[str, JSONValue], row[16]),
        source_states=cast(dict[str, JSONValue], row[17]),
        status=cast(str, row[18]),
        events_committed=cast(int, row[19]),
        failure_code=cast(str | None, row[20]),
        first_seen_at=cast(datetime, row[21]),
        updated_at=cast(datetime, row[22]),
    )


def _observation_row(row: tuple[object, ...]) -> ObservationRow:
    return ObservationRow(
        observation_id=cast(int, row[0]),
        receipt_id=cast(int, row[1]),
        execution_id=cast(str, row[2]),
        observed_at=cast(datetime, row[3]),
        run_record=cast(dict[str, JSONValue] | None, row[4]),
        job_records=cast(list[JSONValue] | None, row[5]),
        artifact_records=cast(list[JSONValue] | None, row[6]),
        status_records=cast(list[JSONValue] | None, row[7]),
        trusted_context=cast(dict[str, JSONValue], row[8]),
        job_verdict=cast(str, row[9]),
        job_reason=cast(str, row[10]),
        numeric_job_id=cast(int | None, row[11]),
        executed_in_attempt=cast(bool | None, row[12]),
        execution_attempt=cast(int | None, row[13]),
        job_conclusion=cast(str | None, row[14]),
        run_conclusion=cast(str | None, row[15]),
        artifact_verdict=cast(str | None, row[16]),
        artifact_reason=cast(str | None, row[17]),
        artifact_integrity=cast(str | None, row[18]),
        artifact_expired=cast(bool | None, row[19]),
        attributed_attempt=cast(int | None, row[20]),
        revision_verdict=cast(str | None, row[21]),
        revision_reason=cast(str | None, row[22]),
        tested_sha=cast(str | None, row[23]),
        revision_provenance=cast(str | None, row[24]),
        publication_authorized=cast(bool, row[25]),
        quarantine_reason=cast(str | None, row[26]),
        binding_evidence=cast(dict[str, JSONValue], row[27]),
    )


def _validate_page(*, limit: object, offset: object) -> None:
    if not _is_int(limit) or not 1 <= limit <= MAX_LIST_ROWS:
        raise ValidationError(f"limit must be an integer between 1 and {MAX_LIST_ROWS}")
    if not _is_int(offset) or not 0 <= offset <= MAX_OFFSET:
        raise ValidationError(f"offset must be an integer between 0 and {MAX_OFFSET}")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
