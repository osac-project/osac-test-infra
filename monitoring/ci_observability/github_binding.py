"""Join producer claims to GitHub metadata without fetching URLs or changing state.

Verified job identity requires a trusted caller mapping. Producer-selected
matches remain candidates. Artifact identity, received-byte integrity, and
attempt timing remain separate facts. Producer payloads always remain claims.
Diagnostics contain fixed reason codes rather than untrusted values.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TypeAlias

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPO_RE = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}")
_STATUS_TARGET_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100})/actions/runs/"
    r"([1-9][0-9]{0,18})/job/([1-9][0-9]{0,18})"
)

JSONValue: TypeAlias = bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"] | None


class MalformedGitHubMetadataError(ValueError):
    """A raw GitHub API record does not have the shape this module consumes.

    The message names the field and the problem; it never includes the
    offending value.
    """


class Verdict(Enum):
    """How a binding was established.

    VERIFIED comes from GitHub metadata plus trusted caller context.
    CANDIDATE is a unique match selected only by the untrusted claim.
    CLAIMED is a producer-recorded value that was not verified. UNKNOWN
    means no binding.
    """

    VERIFIED = "verified"
    CANDIDATE = "candidate"
    CLAIMED = "claimed"
    UNKNOWN = "unknown"


class RevisionProvenance(Enum):
    TRUSTED_COMMIT_STATUS = "trusted_commit_status"
    DEFAULT_CHECKOUT_POLICY = "default_checkout_policy"
    PRODUCER_OVERRIDE = "producer_override"


class CorroborationStatus(Enum):
    CONSISTENT = "consistent"
    CONTRADICTORY = "contradictory"
    NOT_CHECKED = "not_checked"


class IntegrityStatus(Enum):
    VERIFIED = "verified"
    MISMATCH = "mismatch"
    NOT_CHECKED = "not_checked"


class TimingAttribution(Enum):
    UNIQUE_JOB_WINDOW = "unique_job_window"
    AMBIGUOUS = "ambiguous"
    NO_JOB_WINDOW = "no_job_window"
    NO_TIMING_DATA = "no_timing_data"


@dataclass(frozen=True)
class GitHubRun:
    """One attempt of a workflow run, as recorded by the runs API.

    Supply the run as recorded for the attempt being bound (fetch
    /actions/runs/{id}/attempts/{n}, or the run object when its
    run_attempt is that attempt): run_started_at is the start of
    run_attempt, and the inherited-execution rules depend on it.
    """

    repo: str
    id: int
    run_attempt: int
    event: str
    head_sha: str
    run_started_at: datetime | None

    @classmethod
    def from_api(cls, raw: Mapping[str, JSONValue]) -> GitHubRun:
        return cls(
            repo=_repo_of_run(raw),
            id=_positive_int(raw.get("id"), "run id"),
            run_attempt=_positive_int(raw.get("run_attempt"), "run run_attempt"),
            event=_required_str(raw.get("event"), "run event"),
            head_sha=_sha(raw.get("head_sha"), "run head_sha"),
            run_started_at=_parse_timestamp(raw.get("run_started_at"), "run run_started_at"),
        )


@dataclass(frozen=True)
class GitHubJob:
    """One job record of a run attempt, as recorded by the jobs API.

    The numeric id is the only authoritative job identity. `name` is the
    display name ("caller job / innermost job name", matrix values in
    parentheses), not the GITHUB_JOB key. A carried-over job of a re-run
    attempt keeps its earlier timestamps while the listing relabels its
    run_attempt (verified live: the attempt-2 listing gives carried-over
    jobs new ids, the new attempt number, and the old timestamps), so the
    execution rules in bind_job_execution use timestamps, not the label.
    """

    id: int
    run_id: int
    run_attempt: int
    name: str
    conclusion: str | None
    started_at: datetime | None
    completed_at: datetime | None
    runner_name: str | None

    @classmethod
    def from_api(cls, raw: Mapping[str, JSONValue]) -> GitHubJob:
        job_id = _positive_int(raw.get("id"), "job id")
        return cls(
            id=job_id,
            run_id=_positive_int(raw.get("run_id"), "job run_id"),
            run_attempt=_positive_int(raw.get("run_attempt"), "job run_attempt"),
            name=_required_str(raw.get("name"), "job name"),
            conclusion=_optional_str(raw.get("conclusion"), "job conclusion"),
            started_at=_parse_timestamp(raw.get("started_at"), "job started_at"),
            completed_at=_parse_timestamp(raw.get("completed_at"), "job completed_at"),
            # The API reports an empty runner name for jobs that have not
            # run (queued, skipped); that is an absence, not a name.
            runner_name=_optional_str(raw.get("runner_name"), "job runner_name"),
        )


@dataclass(frozen=True)
class CommitStatus:
    """One commit status, as recorded by the statuses API of one commit.

    `sha` is the commit the statuses were fetched for (the path parameter
    of that call). Commit statuses can be created by any user with push
    access, so trust comes only from the creator id being configured as
    trusted and the creator being a Bot.
    """

    sha: str
    context: str
    target_url: str | None
    creator_id: int | None
    creator_type: str | None

    @classmethod
    def from_api(cls, raw: Mapping[str, JSONValue], sha: str) -> CommitStatus:
        creator = raw.get("creator")
        creator_id: int | None = None
        creator_type: str | None = None
        if isinstance(creator, Mapping):
            raw_creator_id = creator.get("id")
            if raw_creator_id is not None:
                creator_id = _positive_int(raw_creator_id, "status creator id")
            creator_type = _optional_str(creator.get("type"), "status creator type")
        elif creator is not None:
            raise MalformedGitHubMetadataError("status creator must be an object or null")
        return cls(
            sha=_sha(sha, "status sha"),
            context=_required_str(raw.get("context"), "status context"),
            target_url=_optional_str(raw.get("target_url"), "status target_url"),
            creator_id=creator_id,
            creator_type=creator_type,
        )


@dataclass(frozen=True)
class ArtifactMetadata:
    """One artifact of a run, as recorded by the artifacts API.

    workflow_run_id is the run the API itself attaches to the artifact;
    it is what rejects artifacts of another run. The API exposes no
    run_attempt for an artifact, and the name is chosen by the producing
    job, so neither says which attempt produced it.
    """

    id: int
    name: str
    digest: str | None
    expired: bool
    created_at: datetime | None
    workflow_run_id: int

    @classmethod
    def from_api(cls, raw: Mapping[str, JSONValue]) -> ArtifactMetadata:
        artifact_id = _positive_int(raw.get("id"), "artifact id")
        workflow_run = raw.get("workflow_run")
        if not isinstance(workflow_run, Mapping):
            raise MalformedGitHubMetadataError("artifact record has no workflow_run object")
        expired = raw.get("expired")
        if not isinstance(expired, bool):
            raise MalformedGitHubMetadataError("artifact expired must be a boolean")
        return cls(
            id=artifact_id,
            name=_required_str(raw.get("name"), "artifact name"),
            digest=_optional_digest(raw.get("digest"), "artifact digest"),
            expired=expired,
            created_at=_parse_timestamp(raw.get("created_at"), "artifact created_at"),
            workflow_run_id=_positive_int(workflow_run.get("id"), "artifact workflow_run id"),
        )


@dataclass(frozen=True)
class JobClaim:
    """A producer's claim that its payload belongs to one job execution.

    `job_key` is the GITHUB_JOB string; matching it against job display
    names is a heuristic that yields at most a CANDIDATE. A claimed
    display name equally yields at most a CANDIDATE: an untrusted name
    must not choose among matrix siblings. `runner_name` and the observed
    timestamps are corroboration: they can reject a contradictory match,
    never establish one.
    """

    repo: str
    run_id: int
    run_attempt: int
    job_key: str | None = None
    job_display_name: str | None = None
    runner_name: str | None = None
    observed_started_at: datetime | None = None
    observed_completed_at: datetime | None = None


@dataclass(frozen=True)
class ArtifactClaim:
    """A producer's claim that its payload is one artifact of a run.

    `run_attempt` is the producer's claim of the producing attempt; the
    artifacts API does not confirm it, so it is recorded, not trusted.
    The digest of the received bytes is not part of the claim: the
    trusted caller passes it to bind_artifact separately.
    """

    repo: str
    run_id: int
    artifact_name: str
    run_attempt: int | None = None


@dataclass(frozen=True)
class RevisionOverride:
    """A producer-recorded tested revision, such as the fixed commit of a
    flake campaign or a pinned native checkout. Always a CLAIMED verdict.
    """

    sha: str
    description: str


@dataclass(frozen=True)
class Corroboration:
    """One corroborating check between a claim and a job."""

    kind: str
    status: CorroborationStatus
    detail: str


@dataclass(frozen=True)
class StatusEvidence:
    """The commit status that proved a tested revision."""

    context: str
    creator_id: int | None
    target_run_id: int
    target_job_id: int


@dataclass(frozen=True)
class JobBinding:
    """The result of binding a job claim to one run attempt.

    executed_in_attempt is False when the job did not execute in the
    bound attempt: either its record comes from an earlier attempt's
    listing (execution_attempt then holds that attempt), or it is a
    carried-over record of a re-run attempt whose timestamps predate the
    attempt start (execution_attempt is then None: the listing relabels
    the attempt, and the real one is not recoverable from it). No new
    execution may be created for a job with executed_in_attempt False.
    """

    verdict: Verdict
    repo: str
    run_id: int
    run_attempt: int
    reason: str = ""
    numeric_job_id: int | None = None
    job: GitHubJob | None = None
    executed_in_attempt: bool | None = None
    execution_attempt: int | None = None
    github_conclusion: str | None = None
    candidates: tuple[GitHubJob, ...] = ()
    corroborations: tuple[Corroboration, ...] = ()
    inherited_refs: tuple[GitHubJob, ...] = ()
    foreign_jobs: tuple[GitHubJob, ...] = ()


@dataclass(frozen=True)
class RevisionBinding:
    """The tested revision of one run.

    run_head_sha is the head SHA the runs API records for the attempt.
    It is not the workflow-file revision (reusable workflow files run
    from their own refs, which these APIs do not expose) and not by
    itself the tested revision. tested_sha carries a value only for
    VERIFIED and CLAIMED verdicts.
    """

    verdict: Verdict
    repo: str
    run_id: int
    run_head_sha: str
    tested_sha: str | None = None
    provenance: RevisionProvenance | None = None
    reason: str = ""
    status_evidence: StatusEvidence | None = None


@dataclass(frozen=True)
class ArtifactBinding:
    """The result of binding an artifact claim to one run.

    verdict is the artifact identity association. integrity compares the
    bytes the trusted caller received with the digest the API recorded;
    it is a separate dimension, as is expired. timing_attribution and
    attributed_attempt are corroboration over jobs that executed in the
    bound attempt; they never upgrade the verdict, and a claimed attempt
    that conflicts with them is flagged, not corrected.
    """

    verdict: Verdict
    repo: str
    run_id: int
    reason: str = ""
    artifact_id: int | None = None
    integrity: IntegrityStatus = IntegrityStatus.NOT_CHECKED
    expired: bool | None = None
    claimed_attempt: int | None = None
    attributed_attempt: int | None = None
    timing_attribution: TimingAttribution = TimingAttribution.NO_TIMING_DATA
    attempt_conflict: bool = False


@dataclass(frozen=True)
class StatusTarget:
    """The run and job a commit-status target URL points at."""

    repo: str
    run_id: int
    job_id: int


def parse_status_target_url(url: str | None) -> StatusTarget | None:
    """Accept the canonical GitHub job URL without URL normalization."""
    if url is None:
        return None
    match = _STATUS_TARGET_RE.fullmatch(url)
    if match is None:
        return None
    owner, repository, run_id, job_id = match.groups()
    return StatusTarget(repo=f"{owner}/{repository}", run_id=int(run_id), job_id=int(job_id))


def bind_job_execution(
    run: GitHubRun,
    jobs: Sequence[GitHubJob],
    claim: JobClaim,
    *,
    expected_job_id: int | None = None,
    expected_display_name: str | None = None,
) -> JobBinding:
    """Bind a producer claim to the numeric job id of one run attempt.

    The claim must name the same repository, run, and attempt as the run
    record. A binding is VERIFIED only through trusted caller context:
    expected_job_id (a central catalog identity or a previously bound id)
    or expected_display_name (a central mapping). Without them, a unique
    claim match is a CANDIDATE and an ambiguous match is UNKNOWN.
    """
    if claim.repo != run.repo:
        return _unknown_job(run, "claim_repo_mismatch")
    if claim.run_id != run.id:
        return _unknown_job(run, "claim_run_mismatch")
    if claim.run_attempt != run.run_attempt:
        return _unknown_job(run, "claim_attempt_mismatch")
    if claim.job_key is not None and claim.job_key.isdigit():
        return _unknown_job(run, "numeric_job_key_rejected")

    current, inherited_refs, foreign = _partition_jobs(run, _dedupe_jobs(jobs))
    if not current and not inherited_refs:
        return _unknown_job(run, "no_attempt_jobs", inherited_refs=inherited_refs, foreign_jobs=foreign)

    job: GitHubJob | None = None
    verdict = Verdict.UNKNOWN
    reason = ""
    if expected_job_id is not None:
        job = _find_by_id(current, expected_job_id)
        if job is None:
            job = _find_by_id(inherited_refs, expected_job_id)
        if job is None:
            reason = "expected_job_not_in_attempt"
        else:
            verdict = Verdict.VERIFIED
    elif expected_display_name is not None:
        matches = [entry for entry in current if entry.name == expected_display_name]
        if len(matches) == 1:
            job = matches[0]
            verdict = Verdict.VERIFIED
        elif len(matches) > 1:
            reason = "ambiguous_expected_job"
        else:
            matches = [entry for entry in inherited_refs if entry.name == expected_display_name]
            if len(matches) == 1:
                job = matches[0]
                verdict = Verdict.VERIFIED
            elif len(matches) > 1:
                reason = "ambiguous_expected_job"
            else:
                reason = "expected_job_not_in_attempt"
    else:
        matches, failure = _claim_matches(current, inherited_refs, claim)
        if failure is not None:
            reason = failure
        elif len(matches) > 1:
            return _unknown_job(
                run,
                "ambiguous_logical_job",
                candidates=tuple(matches),
                inherited_refs=inherited_refs,
                foreign_jobs=foreign,
            )
        else:
            job = matches[0]
            verdict = Verdict.CANDIDATE

    if job is None:
        return _unknown_job(run, reason, inherited_refs=inherited_refs, foreign_jobs=foreign)

    if claim.job_display_name is not None and claim.job_display_name != job.name:
        return _unknown_job(run, "claim_contradicts_job", inherited_refs=inherited_refs, foreign_jobs=foreign)

    corroborations, contradiction = _corroborations(claim, job)
    if contradiction is not None:
        return _unknown_job(
            run,
            "claim_contradicts_job",
            inherited_refs=inherited_refs,
            foreign_jobs=foreign,
            corroborations=corroborations,
        )
    executed, execution_attempt = _execution_facts(run, job)
    return JobBinding(
        verdict=verdict,
        repo=run.repo,
        run_id=run.id,
        run_attempt=run.run_attempt,
        numeric_job_id=job.id,
        job=job,
        executed_in_attempt=executed,
        execution_attempt=execution_attempt,
        github_conclusion=job.conclusion,
        candidates=(job,) if verdict is Verdict.CANDIDATE else (),
        corroborations=corroborations,
        inherited_refs=tuple(inherited_refs),
        foreign_jobs=tuple(foreign),
    )


def resolve_tested_revision(
    run: GitHubRun,
    jobs: Sequence[GitHubJob],
    statuses: Sequence[CommitStatus] = (),
    *,
    expected_context: str | None = None,
    trusted_creator_ids: frozenset[int] = frozenset(),
    expected_job_id: int | None = None,
    override: RevisionOverride | None = None,
    tests_run_head: bool = False,
) -> RevisionBinding:
    """Resolve the tested revision of one run from GitHub metadata.

    run_head_sha is always recorded. The tested revision is VERIFIED
    only through: a trusted commit status for a workflow_dispatch run
    (trusted creator id and Bot type, expected context, target URL
    binding the exact run and the exact expected numeric job), or the
    caller asserting tests_run_head, the workflow's known checkout
    policy, for a head-following event. A producer override is recorded
    with a CLAIMED verdict. A pull_request run stays UNKNOWN: GitHub
    checks out a merge commit whose SHA the runs API does not expose.
    """
    if run.event == "workflow_dispatch":
        status_binding = _tested_revision_from_status(
            run, jobs, statuses, expected_context, trusted_creator_ids, expected_job_id
        )
        if status_binding.verdict is Verdict.VERIFIED:
            return status_binding
        if status_binding.reason == "trusted_status_ambiguous_sha":
            return status_binding
        if tests_run_head:
            return _head_policy_binding(run)
        if override is not None:
            return _claimed_binding(run, override)
        return status_binding
    if override is not None:
        return _claimed_binding(run, override)
    if run.event == "pull_request":
        return _unknown_revision(run, "pull_request_checkout_is_merge_commit")
    if run.event in ("schedule", "merge_group", "push"):
        if tests_run_head:
            return _head_policy_binding(run)
        return _unknown_revision(run, "checkout_policy_not_supplied")
    return _unknown_revision(run, "no_rule_for_event")


def bind_artifact(
    run: GitHubRun,
    jobs: Sequence[GitHubJob],
    artifacts: Sequence[ArtifactMetadata],
    claim: ArtifactClaim,
    *,
    received_digest: str | None = None,
) -> ArtifactBinding:
    """Bind an artifact claim to one artifact of a run and verify the bytes.

    The claim must name the same repository and run, and artifacts of
    another run are rejected even when name and digest match. Same-name
    artifacts of the run are disambiguated by received_digest, the
    sha256 of the exact bytes the trusted caller received (producers
    never supply it). A digest that matches no same-name artifact, or a
    single artifact whose recorded digest differs from the received
    bytes, leaves the claim unbound with a mismatch.
    """
    if claim.repo != run.repo:
        return _unknown_artifact(run, claim, "claim_repo_mismatch")
    if claim.run_id != run.id:
        return _unknown_artifact(run, claim, "claim_run_mismatch")
    if received_digest is not None and not _SHA256_RE.fullmatch(received_digest):
        return _unknown_artifact(run, claim, "malformed_received_digest")

    same_name = [entry for entry in _dedupe_artifacts(artifacts) if entry.name == claim.artifact_name]
    local = [entry for entry in same_name if entry.workflow_run_id == run.id]
    if not local:
        if same_name:
            return _unknown_artifact(run, claim, "foreign_run_artifact")
        return _unknown_artifact(run, claim, "artifact_name_not_in_run")

    integrity = IntegrityStatus.NOT_CHECKED
    artifact: ArtifactMetadata
    if len(local) > 1:
        if received_digest is None:
            return _unknown_artifact(run, claim, "same_name_no_digest")
        matches = [entry for entry in local if entry.digest == received_digest]
        if not matches:
            return _unknown_artifact(run, claim, "digest_matches_no_same_name_artifact")
        if len(matches) > 1:
            return _unknown_artifact(run, claim, "same_name_digest_ambiguous")
        artifact = matches[0]
        integrity = IntegrityStatus.VERIFIED
    else:
        artifact = local[0]
        if received_digest is not None and artifact.digest is not None:
            if received_digest != artifact.digest:
                return ArtifactBinding(
                    verdict=Verdict.UNKNOWN,
                    repo=run.repo,
                    run_id=run.id,
                    reason="digest_mismatch",
                    artifact_id=artifact.id,
                    integrity=IntegrityStatus.MISMATCH,
                    expired=artifact.expired,
                    claimed_attempt=claim.run_attempt,
                )
            integrity = IntegrityStatus.VERIFIED

    current, _, _ = _partition_jobs(run, _dedupe_jobs(jobs))
    executed_jobs = [job for job in current if _execution_facts(run, job)[0] is True]
    attribution, attributed_attempt = _attribute_attempt(run, executed_jobs, artifact.created_at)
    attempt_conflict = (
        claim.run_attempt is not None and attributed_attempt is not None and claim.run_attempt != attributed_attempt
    )
    return ArtifactBinding(
        verdict=Verdict.VERIFIED,
        repo=run.repo,
        run_id=run.id,
        artifact_id=artifact.id,
        integrity=integrity,
        expired=artifact.expired,
        claimed_attempt=claim.run_attempt,
        attributed_attempt=attributed_attempt,
        timing_attribution=attribution,
        attempt_conflict=attempt_conflict,
    )


def _unknown_job(
    run: GitHubRun,
    reason: str,
    candidates: Sequence[GitHubJob] = (),
    inherited_refs: Sequence[GitHubJob] = (),
    foreign_jobs: Sequence[GitHubJob] = (),
    corroborations: Sequence[Corroboration] = (),
) -> JobBinding:
    return JobBinding(
        verdict=Verdict.UNKNOWN,
        repo=run.repo,
        run_id=run.id,
        run_attempt=run.run_attempt,
        reason=reason,
        candidates=tuple(candidates),
        inherited_refs=tuple(inherited_refs),
        foreign_jobs=tuple(foreign_jobs),
        corroborations=tuple(corroborations),
    )


def _unknown_artifact(run: GitHubRun, claim: ArtifactClaim, reason: str) -> ArtifactBinding:
    return ArtifactBinding(
        verdict=Verdict.UNKNOWN, repo=run.repo, run_id=run.id, reason=reason, claimed_attempt=claim.run_attempt
    )


def _unknown_revision(run: GitHubRun, reason: str) -> RevisionBinding:
    return RevisionBinding(
        verdict=Verdict.UNKNOWN, repo=run.repo, run_id=run.id, run_head_sha=run.head_sha, reason=reason
    )


def _claimed_binding(run: GitHubRun, override: RevisionOverride) -> RevisionBinding:
    return RevisionBinding(
        verdict=Verdict.CLAIMED,
        repo=run.repo,
        run_id=run.id,
        run_head_sha=run.head_sha,
        tested_sha=override.sha,
        provenance=RevisionProvenance.PRODUCER_OVERRIDE,
    )


def _head_policy_binding(run: GitHubRun) -> RevisionBinding:
    return RevisionBinding(
        verdict=Verdict.VERIFIED,
        repo=run.repo,
        run_id=run.id,
        run_head_sha=run.head_sha,
        tested_sha=run.head_sha,
        provenance=RevisionProvenance.DEFAULT_CHECKOUT_POLICY,
    )


def _tested_revision_from_status(
    run: GitHubRun,
    jobs: Sequence[GitHubJob],
    statuses: Sequence[CommitStatus],
    expected_context: str | None,
    trusted_creator_ids: frozenset[int],
    expected_job_id: int | None,
) -> RevisionBinding:
    if expected_context is None:
        return _unknown_revision(run, "missing_expected_context")
    if not trusted_creator_ids:
        return _unknown_revision(run, "missing_trusted_creator_ids")
    if expected_job_id is None:
        return _unknown_revision(run, "missing_expected_job_id")
    current, _, _ = _partition_jobs(run, _dedupe_jobs(jobs))
    if not any(job.id == expected_job_id for job in current):
        return _unknown_revision(run, "no_trusted_status_binding")
    matches: list[CommitStatus] = []
    for status in statuses:
        if status.context != expected_context:
            continue
        if status.creator_id not in trusted_creator_ids or status.creator_type != "Bot":
            continue
        target = parse_status_target_url(status.target_url)
        if target is None:
            continue
        if target.repo != run.repo or target.run_id != run.id or target.job_id != expected_job_id:
            continue
        matches.append(status)
    if not matches:
        return _unknown_revision(run, "no_trusted_status_binding")
    shas = {status.sha for status in matches}
    if len(shas) > 1:
        return _unknown_revision(run, "trusted_status_ambiguous_sha")
    return RevisionBinding(
        verdict=Verdict.VERIFIED,
        repo=run.repo,
        run_id=run.id,
        run_head_sha=run.head_sha,
        tested_sha=next(iter(shas)),
        provenance=RevisionProvenance.TRUSTED_COMMIT_STATUS,
        status_evidence=StatusEvidence(
            context=expected_context,
            creator_id=matches[0].creator_id,
            target_run_id=run.id,
            target_job_id=expected_job_id,
        ),
    )


def _partition_jobs(
    run: GitHubRun, jobs: Sequence[GitHubJob]
) -> tuple[list[GitHubJob], list[GitHubJob], list[GitHubJob]]:
    """Split job records into current-attempt, inherited, and foreign.

    Current records carry the bound attempt. Inherited records are
    earlier attempts' references for the same run (their run_attempt is
    the attempt that actually executed them). Foreign records belong to
    another run or a later attempt.
    """
    current: list[GitHubJob] = []
    inherited: list[GitHubJob] = []
    foreign: list[GitHubJob] = []
    for job in jobs:
        if job.run_id != run.id or job.run_attempt > run.run_attempt:
            foreign.append(job)
        elif job.run_attempt < run.run_attempt:
            inherited.append(job)
        else:
            current.append(job)
    return current, inherited, foreign


def _dedupe_jobs(jobs: Sequence[GitHubJob]) -> list[GitHubJob]:
    """Collapse repeated records of the same job id.

    Identical records collapse; the same id with conflicting data is
    malformed and raises.
    """
    seen: dict[int, GitHubJob] = {}
    for job in jobs:
        known = seen.get(job.id)
        if known is None:
            seen[job.id] = job
        elif known != job:
            raise MalformedGitHubMetadataError(f"job records conflict for id {job.id}")
    return list(seen.values())


def _find_by_id(jobs: Sequence[GitHubJob], job_id: int) -> GitHubJob | None:
    for job in jobs:
        if job.id == job_id:
            return job
    return None


def _claim_matches(
    current: Sequence[GitHubJob], inherited_refs: Sequence[GitHubJob], claim: JobClaim
) -> tuple[list[GitHubJob], str | None]:
    if claim.job_display_name is None and claim.job_key is None:
        return [], "claim_without_job_identity"
    pool = list(current)
    matches = _match_identity(pool, claim)
    if not matches:
        # A claim that only matches an earlier attempt's record binds
        # that record as inherited, never as a current execution.
        matches = _match_identity(list(inherited_refs), claim)
    if not matches:
        return [], "claim_identity_no_match"
    return matches, None


def _match_identity(jobs: Sequence[GitHubJob], claim: JobClaim) -> list[GitHubJob]:
    if claim.job_display_name is not None:
        return [job for job in jobs if job.name == claim.job_display_name]
    assert claim.job_key is not None
    return [job for job in jobs if _job_name_matches_key(job.name, claim.job_key)]


def _job_name_matches_key(name: str, key: str) -> bool:
    """HEURISTIC: whether a claimed GITHUB_JOB key plausibly denotes this job.

    The jobs API exposes display names, not job keys. A display name is
    "caller job / ... / innermost job name" with matrix values appended
    in parentheses, so a key matches when it equals the whole display
    name or its final segment, or that segment without a trailing
    matrix parenthetical. A unique match through this heuristic is a
    CANDIDATE, never a VERIFIED binding.
    """
    final = name.rsplit(" / ", 1)[-1]
    if final == key:
        return True
    if final.endswith(")") and " (" in final:
        return final[: final.rindex(" (")] == key
    return False


def _corroborations(claim: JobClaim, job: GitHubJob) -> tuple[tuple[Corroboration, ...], str | None]:
    checks: list[Corroboration] = []
    contradiction: str | None = None

    if claim.runner_name is None or job.runner_name is None:
        checks.append(Corroboration("runner_name", CorroborationStatus.NOT_CHECKED, "runner name missing on a side"))
    elif claim.runner_name == job.runner_name:
        checks.append(Corroboration("runner_name", CorroborationStatus.CONSISTENT, "both report the same runner name"))
    else:
        checks.append(Corroboration("runner_name", CorroborationStatus.CONTRADICTORY, "runner names differ"))
        contradiction = "runner name"

    if claim.observed_started_at is None or job.started_at is None or job.completed_at is None:
        checks.append(Corroboration("time_window", CorroborationStatus.NOT_CHECKED, "timestamps missing on a side"))
    elif claim.observed_started_at.utcoffset() is None or (
        claim.observed_completed_at is not None and claim.observed_completed_at.utcoffset() is None
    ):
        checks.append(Corroboration("time_window", CorroborationStatus.NOT_CHECKED, "claim timestamp lacks a timezone"))
    elif _window_contains(job.started_at, job.completed_at, claim.observed_started_at, claim.observed_completed_at):
        checks.append(
            Corroboration("time_window", CorroborationStatus.CONSISTENT, "observed window lies inside the job window")
        )
    else:
        checks.append(
            Corroboration("time_window", CorroborationStatus.CONTRADICTORY, "observed window violates the job window")
        )
        contradiction = "time window"

    return tuple(checks), contradiction


def _window_contains(job_start: datetime, job_end: datetime, claim_start: datetime, claim_end: datetime | None) -> bool:
    """The observed window must lie inside the job execution window.

    The claim start must fall within the job window, and a known claim
    end must not precede the claim start nor exceed the job end.
    """
    if claim_start < job_start or claim_start > job_end:
        return False
    return claim_end is None or (claim_start <= claim_end <= job_end)


def _execution_facts(run: GitHubRun, job: GitHubJob) -> tuple[bool | None, int | None]:
    """Whether the job executed in the bound attempt, and in which attempt
    its execution record actually lies.

    An earlier attempt's record (run_attempt below the bound attempt)
    never executed in it. Within one listing, a carried-over record of a
    re-run attempt keeps its earlier timestamps while the listing
    relabels its run_attempt, so a start before the attempt start means
    the execution belongs to an earlier, unrecoverable attempt. Missing
    timestamps on a re-run attempt leave both facts unknown.
    """
    if job.run_attempt < run.run_attempt:
        return False, job.run_attempt
    if job.conclusion == "skipped":
        return False, None
    if job.started_at is None:
        return None, None
    if run.run_attempt == 1:
        return True, run.run_attempt
    if run.run_started_at is None:
        return None, None
    if job.started_at < run.run_started_at:
        return False, None
    return True, run.run_attempt


def _attribute_attempt(
    run: GitHubRun, executed_jobs: Sequence[GitHubJob], created_at: datetime | None
) -> tuple[TimingAttribution, int | None]:
    """Attribute an artifact to the attempt through the execution windows of
    jobs that executed in it. Inherited jobs' old windows never attribute
    an artifact to the current attempt. Corroboration only: this never
    upgrades the artifact binding's verdict."""
    if created_at is None:
        return TimingAttribution.NO_TIMING_DATA, None
    windowed = [job for job in executed_jobs if job.started_at is not None and job.completed_at is not None]
    if not windowed:
        return TimingAttribution.NO_TIMING_DATA, None
    containing = [job for job in windowed if job.started_at <= created_at <= job.completed_at]
    if len(containing) == 1:
        return TimingAttribution.UNIQUE_JOB_WINDOW, run.run_attempt
    if len(containing) > 1:
        return TimingAttribution.AMBIGUOUS, None
    return TimingAttribution.NO_JOB_WINDOW, None


def _dedupe_artifacts(artifacts: Sequence[ArtifactMetadata]) -> list[ArtifactMetadata]:
    """Collapse repeated records of the same artifact id; conflicting
    records for one id are malformed and raise."""
    seen: dict[int, ArtifactMetadata] = {}
    for artifact in artifacts:
        known = seen.get(artifact.id)
        if known is None:
            seen[artifact.id] = artifact
        elif known != artifact:
            raise MalformedGitHubMetadataError(f"artifact records conflict for id {artifact.id}")
    return list(seen.values())


def _repo_of_run(raw: Mapping[str, JSONValue]) -> str:
    repository = raw.get("repository")
    if not isinstance(repository, Mapping):
        raise MalformedGitHubMetadataError("run record has no repository object")
    full_name = repository.get("full_name")
    if not isinstance(full_name, str) or not _REPO_RE.fullmatch(full_name):
        raise MalformedGitHubMetadataError("run repository.full_name must be owner/name")
    return full_name


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MalformedGitHubMetadataError(f"{field_name} must be a positive integer")
    return value


def _required_str(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise MalformedGitHubMetadataError(f"{field_name} must be a non-empty string")
    return value


def _optional_str(value: object, field_name: str) -> str | None:
    """A nullable API string. An empty string is an absence (the jobs API
    reports an empty runner name for jobs that have not run), not a value."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise MalformedGitHubMetadataError(f"{field_name} must be a non-empty string or null")
    return value


def _sha(value: object, field_name: str) -> str:
    text = _required_str(value, field_name)
    if not _SHA1_RE.fullmatch(text):
        raise MalformedGitHubMetadataError(f"{field_name} must be a 40-character hex commit SHA")
    return text


def _optional_digest(value: object, field_name: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise MalformedGitHubMetadataError(f"{field_name} must be a sha256:<64 hex> digest or null")
    return value


def _parse_timestamp(value: object, field_name: str) -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise MalformedGitHubMetadataError(f"{field_name} must be an ISO-8601 string with a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise MalformedGitHubMetadataError(f"{field_name} must be an ISO-8601 string with a timezone") from None
    if parsed.tzinfo is None:
        raise MalformedGitHubMetadataError(f"{field_name} must include a UTC offset")
    return parsed.astimezone(UTC)
