"""Reconcile explicitly supplied GitHub metadata with bundle claims (W01b).

The trusted caller supplies raw GitHub API records and the central trusted
mappings; the payload supplies only claims. This module drives the accepted
pure binding module (github_binding) over both and returns the verdicts.
Verdicts below VERIFIED never authorize the ledger's authoritative columns;
applying them under the ownership rules is ingest's job. No URL is taken
from a payload, a display name alone binds nothing, and no network fetch
happens here: shared polling arrives in W01c.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeAlias

from .github_binding import (
    ArtifactBinding,
    ArtifactClaim,
    ArtifactMetadata,
    CommitStatus,
    GitHubJob,
    GitHubRun,
    JobBinding,
    JobClaim,
    MalformedGitHubMetadataError,
    RevisionBinding,
    RevisionOverride,
    Verdict,
    bind_artifact,
    bind_job_execution,
    resolve_tested_revision,
)

__all__ = [
    "GitHubEvidence",
    "ReconciledBinding",
    "artifact_claim",
    "claimed_tested_override",
    "job_claim",
    "reconcile",
    "verified_job",
]

JSONValue: TypeAlias = bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"] | None

MAX_JOB_RECORDS = 1000
MAX_ARTIFACT_RECORDS = 1000
MAX_STATUS_RECORDS = 1000


@dataclass(frozen=True, kw_only=True)
class GitHubEvidence:
    """Raw GitHub API records for one run attempt plus the trusted central
    mappings used to bind a delivery.

    Everything here comes from the trusted caller, which used its own
    credentials and job catalog; nothing comes from a payload. ``run`` is
    the record of the attempt being bound (the attempts API response, or
    the run object when its run_attempt is that attempt); ``jobs`` is the
    attempt's jobs listing, which may also carry earlier attempts' records
    (treated as inherited references). ``statuses`` are the commit
    statuses fetched for one commit, ``status_commit_sha`` (the run's head
    SHA when omitted). The trusted mappings are the only path to a
    VERIFIED verdict: without them a claim match stays a CANDIDATE.
    """

    run: Mapping[str, JSONValue]
    jobs: tuple[Mapping[str, JSONValue], ...] = ()
    artifacts: tuple[Mapping[str, JSONValue], ...] = ()
    statuses: tuple[Mapping[str, JSONValue], ...] = ()
    status_commit_sha: str | None = None
    expected_job_id: int | None = None
    expected_display_name: str | None = None
    expected_context: str | None = None
    trusted_creator_ids: frozenset[int] = frozenset()
    tests_run_head: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.run, Mapping):
            raise ValueError("run must be a raw GitHub run record object")
        for name, records, limit in (
            ("jobs", self.jobs, MAX_JOB_RECORDS),
            ("artifacts", self.artifacts, MAX_ARTIFACT_RECORDS),
            ("statuses", self.statuses, MAX_STATUS_RECORDS),
        ):
            if not isinstance(records, tuple) or len(records) > limit:
                raise ValueError(f"{name} must be a tuple of at most {limit} raw records")
        if self.expected_job_id is not None and (not _is_int(self.expected_job_id) or self.expected_job_id < 1):
            raise ValueError("expected_job_id must be None or an integer >= 1")
        if self.expected_display_name is not None and (
            not isinstance(self.expected_display_name, str) or not 1 <= len(self.expected_display_name) <= 256
        ):
            raise ValueError("expected_display_name must be None or a string of 1 to 256 characters")
        if self.expected_context is not None and (
            not isinstance(self.expected_context, str) or not 1 <= len(self.expected_context) <= 256
        ):
            raise ValueError("expected_context must be None or a bounded string")
        if not isinstance(self.trusted_creator_ids, frozenset) or not all(
            _is_int(value) for value in self.trusted_creator_ids
        ):
            raise ValueError("trusted_creator_ids must be a frozenset of integers")
        if not isinstance(self.tests_run_head, bool):
            raise ValueError("tests_run_head must be a boolean")
        if self.status_commit_sha is not None and (
            not isinstance(self.status_commit_sha, str) or len(self.status_commit_sha) != 40
        ):
            raise ValueError("status_commit_sha must be None or a 40-character commit SHA")


@dataclass(frozen=True, kw_only=True)
class ReconciledBinding:
    """The verdicts computed for one delivery, with the claims they came
    from. ``run_conclusion`` is the trusted run record's own conclusion,
    None while the run is unfinished. ``repository_id`` is the positive
    repository id from the trusted run record: the authoritative identity
    for publication, never the payload's claim."""

    job: JobBinding | None
    artifact: ArtifactBinding | None
    revision: RevisionBinding | None
    run_conclusion: str | None
    repository_id: int
    job_claim: JobClaim
    artifact_claim: ArtifactClaim | None


def trusted_context(*, evidence: GitHubEvidence, received_digest: str | None) -> dict[str, JSONValue]:
    """The bounded trusted-context inputs a reconciliation used, recorded
    beside its outcomes so a replay can explain the authority: the trusted
    job mapping, commit-status join, creator allowlist, and checkout
    policy. Values are caller-supplied identifiers, never payload data."""

    return {
        "expected_job_id": evidence.expected_job_id,
        "expected_display_name": evidence.expected_display_name,
        "expected_context": evidence.expected_context,
        "trusted_creator_ids": sorted(evidence.trusted_creator_ids),
        "tests_run_head": evidence.tests_run_head,
        "status_commit_sha": evidence.status_commit_sha,
        "received_digest": received_digest,
    }


def job_claim(*, execution: Mapping[str, JSONValue]) -> JobClaim:
    """The payload's job claim, built from a manifest's execution block.

    The manifest's numeric github_job_id is deliberately not part of the
    claim: a claim can never pick the job it is bound to. The manifest
    carries no GITHUB_JOB string either (its logical_job_key is not a
    display name), so the claim binds, at best, through its claimed
    display name, and only ever as a CANDIDATE.
    """

    job = execution["job"]
    if not isinstance(job, Mapping):
        raise ValueError("execution job must be an object")
    job_name = job.get("job_name")
    return JobClaim(
        repo=execution["repository"],
        run_id=execution["run_id"],
        run_attempt=execution["run_attempt"],
        job_key=None,
        job_display_name=job_name if isinstance(job_name, str) else None,
    )


def artifact_claim(*, repository: str, run_id: int, artifact_name: str, run_attempt: int | None) -> ArtifactClaim:
    """The artifact claim for an archive delivery, from the trusted source
    context: the artifacts API must confirm name and digest, so the claim
    starts from what the caller asked for, never from the payload."""

    return ArtifactClaim(repo=repository, run_id=run_id, artifact_name=artifact_name, run_attempt=run_attempt)


def claimed_tested_override(*, manifest: Mapping[str, JSONValue]) -> RevisionOverride | None:
    """The producer's claimed tested revision (the environment source with
    role 'tested'), recorded with a CLAIMED verdict when the trusted rules
    do not establish one."""

    sources = manifest["environment"]["sources"]
    for source in sources:
        if source["role"] == "tested" and isinstance(source["revision"], str):
            return RevisionOverride(sha=source["revision"], description="manifest-tested-source")
    return None


def reconcile(
    *,
    evidence: GitHubEvidence,
    claim: JobClaim,
    artifact_claim: ArtifactClaim | None = None,
    received_digest: str | None = None,
    revision_override: RevisionOverride | None = None,
) -> ReconciledBinding:
    """Compute the bindings for one delivery from trusted metadata.

    Binds the job (VERIFIED only through the trusted mappings), the
    artifact (the artifacts API attests repository, run, and digest; it
    never attests a job or attempt), and the tested revision (a trusted
    commit status for a workflow_dispatch run, or the caller asserting the
    workflow's checkout policy; a producer override stays CLAIMED).
    """

    run = GitHubRun.from_api(evidence.run)
    jobs = tuple(GitHubJob.from_api(raw) for raw in evidence.jobs)
    artifacts = tuple(ArtifactMetadata.from_api(raw) for raw in evidence.artifacts)
    statuses = tuple(
        CommitStatus.from_api(raw, sha=evidence.status_commit_sha or run.head_sha) for raw in evidence.statuses
    )
    job_binding = bind_job_execution(
        run, jobs, claim, expected_job_id=evidence.expected_job_id, expected_display_name=evidence.expected_display_name
    )
    binding: ArtifactBinding | None = None
    if artifact_claim is not None:
        binding = bind_artifact(run, jobs, artifacts, artifact_claim, received_digest=received_digest)
    revision_binding = resolve_tested_revision(
        run,
        jobs,
        statuses,
        expected_context=evidence.expected_context,
        trusted_creator_ids=evidence.trusted_creator_ids,
        expected_job_id=evidence.expected_job_id,
        override=revision_override,
        tests_run_head=evidence.tests_run_head,
    )
    return ReconciledBinding(
        job=job_binding,
        artifact=binding,
        revision=revision_binding,
        run_conclusion=_run_conclusion(evidence.run),
        repository_id=_repository_id(evidence.run),
        job_claim=claim,
        artifact_claim=artifact_claim,
    )


def verified_job(binding: ReconciledBinding | None) -> JobBinding | None:
    """The binding's job verdict, or None unless it is VERIFIED.

    CANDIDATE, CLAIMED, and UNKNOWN never look verified: callers use this
    so an unverified verdict cannot reach an authoritative column by
    accident.
    """

    if binding is None or binding.job is None or binding.job.verdict is not Verdict.VERIFIED:
        return None
    return binding.job


def _run_conclusion(raw: Mapping[str, JSONValue]) -> str | None:
    """The trusted run record's conclusion, None while the run is
    unfinished. The ledger learns it at most once."""

    conclusion = raw.get("conclusion")
    if conclusion is None or conclusion == "":
        return None
    if not isinstance(conclusion, str) or not 1 <= len(conclusion) <= 64:
        raise MalformedGitHubMetadataError("run conclusion must be a bounded string or null")
    return conclusion


def _repository_id(raw: Mapping[str, JSONValue]) -> int:
    """The positive repository id from the trusted run record: the
    authoritative repository identity for publication. A payload's claimed
    repository id is only evidence."""

    repository = raw.get("repository")
    if not isinstance(repository, Mapping):
        raise MalformedGitHubMetadataError("run record has no repository object")
    identifier = repository.get("id")
    if not _is_int(identifier) or identifier < 1:
        raise MalformedGitHubMetadataError("run repository.id must be a positive integer")
    return identifier


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
