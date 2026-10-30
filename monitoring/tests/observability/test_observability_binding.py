"""Tests for the pure GitHub binding logic in monitoring/ci_observability/.

Fixture shapes are sanitized copies of GitHub API responses verified by
read-only GETs against osac-project/osac on 2026-10-27/28: a scheduled
VMaaS full install with its per-attempt job; an "E2E VMaaS PR suite"
dispatch with the commit status that names its tested commit; and re-run
attempt 40348910828, whose attempt-2 listing gives the carried-over
verify job a new numeric id, the new attempt label, and its attempt-1
timestamps (its attempt-1 record keeps the original id and label).
Identifiers, SHAs, digests, and timestamps are synthetic; field names
and name formats match the live responses.
"""

from __future__ import annotations

import dataclasses
import traceback
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from monitoring.ci_observability.github_binding import (
    ArtifactClaim,
    ArtifactMetadata,
    CommitStatus,
    CorroborationStatus,
    GitHubJob,
    GitHubRun,
    IntegrityStatus,
    JobClaim,
    JSONValue,
    MalformedGitHubMetadataError,
    RevisionBinding,
    RevisionOverride,
    RevisionProvenance,
    StatusTarget,
    TimingAttribution,
    Verdict,
    bind_artifact,
    bind_job_execution,
    parse_status_target_url,
    resolve_tested_revision,
)

REPO = "osac-project/osac"
SCHEDULED_SHA = "1f" * 20
MAIN_SHA = "2e" * 20
TESTED_SHA = "3d" * 20
NEW_HEAD_SHA = "4c" * 20
PULL_HEAD_SHA = "5b" * 20
MERGE_SHA = "6a" * 20
DIGEST_A = "sha256:" + "3a" * 32
DIGEST_B = "sha256:" + "4b" * 32
DIGEST_C = "sha256:" + "5c" * 32
TRUSTED_BOT_ID = 330000001

RUN_SCHEDULED_ID = 90000000001
RUN_PR_SUITE_ID = 90000000002
RUN_RERUN_ID = 90000000003
RUN_CI_ID = 90000000004
RUN_MATRIX_ID = 90000000005

E2E_SCHEDULED_JOB_ID = 91000000001
E2E_PR_JOB_ID = 91000000002
E2E_RERUN_JOB_ID = 91000000003
CARRIED_VERIFY_JOB_ID = 91000000004
VERIFY_PR_JOB_ID = 91000000005
UNIT_LEG_JOB_ID = 91000000006
MATRIX_A_JOB_ID = 91000000007
MATRIX_B_JOB_ID = 91000000008
ATTEMPT1_VERIFY_JOB_ID = 91000000009

VMaaS_CONTEXT = "E2E / VMaaS Full Install"
TRUSTED_CREATORS = frozenset({TRUSTED_BOT_ID})


def run_raw(**overrides: JSONValue) -> dict[str, JSONValue]:
    raw: dict[str, JSONValue] = {
        "id": RUN_SCHEDULED_ID,
        "run_attempt": 1,
        "event": "schedule",
        "head_sha": SCHEDULED_SHA,
        "run_started_at": "2026-10-27T17:15:19Z",
        "repository": {"full_name": REPO},
    }
    raw.update(overrides)
    return raw


def job_raw(**overrides: JSONValue) -> dict[str, JSONValue]:
    raw: dict[str, JSONValue] = {
        "id": E2E_SCHEDULED_JOB_ID,
        "run_id": RUN_SCHEDULED_ID,
        "run_attempt": 1,
        "name": "e2e-vmaas-full-install / e2e",
        "conclusion": "failure",
        "started_at": "2026-10-27T17:15:21Z",
        "completed_at": "2026-10-27T19:13:45Z",
        "runner_name": "osac-15-runner-04",
    }
    raw.update(overrides)
    return raw


def status_raw(**overrides: JSONValue) -> dict[str, JSONValue]:
    raw: dict[str, JSONValue] = {
        "context": VMaaS_CONTEXT,
        "target_url": f"https://github.com/{REPO}/actions/runs/{RUN_PR_SUITE_ID}/job/{E2E_PR_JOB_ID}",
        "creator": {"id": TRUSTED_BOT_ID, "type": "Bot"},
    }
    raw.update(overrides)
    return raw


def artifact_raw(**overrides: JSONValue) -> dict[str, JSONValue]:
    raw: dict[str, JSONValue] = {
        "id": 92000000001,
        "name": f"osac-logs-full-install-{RUN_SCHEDULED_ID}",
        "digest": DIGEST_A,
        "expired": False,
        "created_at": "2026-10-27T19:10:53Z",
        "workflow_run": {"id": RUN_SCHEDULED_ID},
    }
    raw.update(overrides)
    return raw


def scheduled_run() -> GitHubRun:
    return GitHubRun.from_api(run_raw())


def pr_suite_run() -> GitHubRun:
    return GitHubRun.from_api(
        run_raw(id=RUN_PR_SUITE_ID, event="workflow_dispatch", head_sha=MAIN_SHA, run_started_at="2026-10-27T20:14:00Z")
    )


def rerun_run() -> GitHubRun:
    return GitHubRun.from_api(
        run_raw(
            id=RUN_RERUN_ID,
            run_attempt=2,
            event="workflow_dispatch",
            head_sha=MAIN_SHA,
            run_started_at="2026-10-26T20:14:33Z",
        )
    )


def ci_run() -> GitHubRun:
    return GitHubRun.from_api(
        run_raw(id=RUN_CI_ID, event="pull_request", head_sha=PULL_HEAD_SHA, run_started_at="2026-10-27T18:00:00Z")
    )


def merge_group_run() -> GitHubRun:
    return GitHubRun.from_api(
        run_raw(id=RUN_MATRIX_ID, event="merge_group", head_sha=MERGE_SHA, run_started_at="2026-10-27T19:00:00Z")
    )


def e2e_scheduled_job() -> GitHubJob:
    return GitHubJob.from_api(job_raw())


def e2e_pr_job() -> GitHubJob:
    return GitHubJob.from_api(
        job_raw(
            id=E2E_PR_JOB_ID,
            run_id=RUN_PR_SUITE_ID,
            name="vmaas / e2e-vmaas-full-install / e2e",
            started_at="2026-10-27T20:14:05Z",
            completed_at="2026-10-27T22:12:40Z",
            runner_name="osac-7-runner-01",
        )
    )


def verify_pr_job() -> GitHubJob:
    return GitHubJob.from_api(
        job_raw(
            id=VERIFY_PR_JOB_ID,
            run_id=RUN_PR_SUITE_ID,
            name="verify / Verify the inputs against the pull request",
            conclusion="success",
            started_at="2026-10-27T20:14:02Z",
            completed_at="2026-10-27T20:14:16Z",
            runner_name="GitHub Actions 1027656358",
        )
    )


def e2e_rerun_job() -> GitHubJob:
    return GitHubJob.from_api(
        job_raw(
            id=E2E_RERUN_JOB_ID,
            run_id=RUN_RERUN_ID,
            run_attempt=2,
            name="vmaas / e2e-vmaas-full-install / e2e",
            conclusion="success",
            started_at="2026-10-26T20:14:37Z",
            completed_at="2026-10-26T21:43:32Z",
            runner_name="osac-12-runner-02",
        )
    )


def carried_verify_job() -> GitHubJob:
    """The attempt-2 listing's record of the job that only ran in attempt 1:
    new id, attempt-2 label, attempt-1 timestamps (native shape)."""

    return GitHubJob.from_api(
        job_raw(
            id=CARRIED_VERIFY_JOB_ID,
            run_id=RUN_RERUN_ID,
            run_attempt=2,
            name="verify / Verify the inputs against the pull request",
            conclusion="success",
            started_at="2026-10-26T15:23:57Z",
            completed_at="2026-10-26T15:24:11Z",
            runner_name="GitHub Actions 1027656358",
        )
    )


def attempt1_verify_ref() -> GitHubJob:
    """The attempt-1 listing's record of the same execution."""

    return GitHubJob.from_api(
        job_raw(
            id=ATTEMPT1_VERIFY_JOB_ID,
            run_id=RUN_RERUN_ID,
            run_attempt=1,
            name="verify / Verify the inputs against the pull request",
            conclusion="success",
            started_at="2026-10-26T15:23:57Z",
            completed_at="2026-10-26T15:24:11Z",
            runner_name="GitHub Actions 1027656358",
        )
    )


def matrix_jobs() -> list[GitHubJob]:
    return [
        GitHubJob.from_api(
            job_raw(
                id=MATRIX_A_JOB_ID,
                run_id=RUN_MATRIX_ID,
                name="Run tests (a)",
                conclusion="success",
                runner_name="GitHub Actions 1027656360",
            )
        ),
        GitHubJob.from_api(
            job_raw(
                id=MATRIX_B_JOB_ID,
                run_id=RUN_MATRIX_ID,
                name="Run tests (b)",
                conclusion="failure",
                runner_name="GitHub Actions 1027656361",
            )
        ),
    ]


def status(sha: str = TESTED_SHA, **overrides: JSONValue) -> CommitStatus:
    return CommitStatus.from_api(status_raw(**overrides), sha)


def scheduled_artifact() -> ArtifactMetadata:
    return ArtifactMetadata.from_api(artifact_raw())


def rerun_artifacts() -> list[ArtifactMetadata]:
    name = f"osac-logs-full-install-{RUN_RERUN_ID}"
    return [
        ArtifactMetadata.from_api(
            artifact_raw(
                id=92000000002,
                name=name,
                digest=DIGEST_B,
                created_at="2026-10-26T21:43:00Z",
                workflow_run={"id": RUN_RERUN_ID},
            )
        ),
        ArtifactMetadata.from_api(
            artifact_raw(
                id=92000000003,
                name=name,
                digest=DIGEST_C,
                created_at="2026-10-26T15:24:00Z",
                workflow_run={"id": RUN_RERUN_ID},
            )
        ),
    ]


def job_claim(run: GitHubRun, **overrides: str | int | datetime | None) -> JobClaim:
    values: dict[str, str | int | datetime | None] = {
        "repo": run.repo,
        "run_id": run.id,
        "run_attempt": run.run_attempt,
    }
    values.update(overrides)
    return JobClaim(**values)


def artifact_claim(run: GitHubRun, name: str, **overrides: str | int | None) -> ArtifactClaim:
    values: dict[str, str | int | None] = {"repo": run.repo, "run_id": run.id, "artifact_name": name}
    values.update(overrides)
    return ArtifactClaim(**values)


def at(day: int, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=UTC)


def resolve_pr_suite(**overrides: str | int | frozenset[int] | list[CommitStatus] | None) -> RevisionBinding:
    values: dict[str, str | int | frozenset[int] | list[CommitStatus] | None] = {
        "statuses": [status(), status()],
        "expected_context": VMaaS_CONTEXT,
        "trusted_creator_ids": TRUSTED_CREATORS,
        "expected_job_id": E2E_PR_JOB_ID,
    }
    values.update(overrides)
    return resolve_tested_revision(pr_suite_run(), [e2e_pr_job(), verify_pr_job()], **values)


def test_claim_match_without_trusted_mapping_is_only_a_candidate() -> None:
    run = scheduled_run()
    claim = job_claim(
        run,
        job_key="e2e",
        runner_name="osac-15-runner-04",
        observed_started_at=at(27, 17, 15, 25),
        observed_completed_at=at(27, 19, 10),
    )

    binding = bind_job_execution(run, [e2e_scheduled_job()], claim)

    assert binding.verdict is Verdict.CANDIDATE
    assert binding.numeric_job_id == E2E_SCHEDULED_JOB_ID
    assert binding.github_conclusion == "failure"
    assert binding.executed_in_attempt is True
    assert binding.execution_attempt == 1
    assert binding.candidates == (e2e_scheduled_job(),)
    assert [check.status for check in binding.corroborations] == [
        CorroborationStatus.CONSISTENT,
        CorroborationStatus.CONSISTENT,
    ]


def test_trusted_job_id_binds_verified() -> None:
    run = scheduled_run()

    binding = bind_job_execution(
        run, [e2e_scheduled_job()], job_claim(run, job_key="e2e"), expected_job_id=E2E_SCHEDULED_JOB_ID
    )

    assert binding.verdict is Verdict.VERIFIED
    assert binding.numeric_job_id == E2E_SCHEDULED_JOB_ID


def test_trusted_display_name_binds_verified() -> None:
    run = scheduled_run()

    binding = bind_job_execution(
        run, [e2e_scheduled_job()], job_claim(run, job_key="e2e"), expected_display_name="e2e-vmaas-full-install / e2e"
    )

    assert binding.verdict is Verdict.VERIFIED
    assert binding.numeric_job_id == E2E_SCHEDULED_JOB_ID


def test_expected_job_id_not_in_attempt_stays_unknown() -> None:
    run = scheduled_run()

    binding = bind_job_execution(run, [e2e_scheduled_job()], job_claim(run, job_key="e2e"), expected_job_id=424242)

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.reason == "expected_job_not_in_attempt"


def test_matrix_siblings_without_trusted_mapping_stay_ambiguous() -> None:
    run = merge_group_run()
    jobs = matrix_jobs()

    binding = bind_job_execution(run, jobs, job_claim(run, job_key="Run tests"))

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.reason == "ambiguous_logical_job"
    assert [job.id for job in binding.candidates] == [MATRIX_A_JOB_ID, MATRIX_B_JOB_ID]


def test_claimed_display_name_selects_a_candidate_not_a_verified_sibling() -> None:
    run = merge_group_run()
    jobs = matrix_jobs()

    binding = bind_job_execution(run, jobs, job_claim(run, job_display_name="Run tests (b)"))

    assert binding.verdict is Verdict.CANDIDATE
    assert binding.numeric_job_id == MATRIX_B_JOB_ID


def test_trusted_display_name_resolves_a_matrix_sibling_verified() -> None:
    run = merge_group_run()
    jobs = matrix_jobs()

    binding = bind_job_execution(run, jobs, job_claim(run, job_key="Run tests"), expected_display_name="Run tests (b)")

    assert binding.verdict is Verdict.VERIFIED
    assert binding.numeric_job_id == MATRIX_B_JOB_ID


def test_numeric_job_key_is_rejected() -> None:
    run = scheduled_run()

    binding = bind_job_execution(run, [e2e_scheduled_job()], job_claim(run, job_key="91000000001"))

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.reason == "numeric_job_key_rejected"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"repo": "evil-org/osac"}, "claim_repo_mismatch"),
        ({"run_id": 424242}, "claim_run_mismatch"),
        ({"run_attempt": 2}, "claim_attempt_mismatch"),
    ],
)
def test_claim_gates_stay_unknown(overrides: dict[str, JSONValue], reason: str) -> None:
    run = scheduled_run()
    claim = dataclasses.replace(JobClaim(repo=run.repo, run_id=run.id, run_attempt=1, job_key="e2e"), **overrides)

    binding = bind_job_execution(run, [e2e_scheduled_job()], claim)

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.reason == reason
    # Untrusted claim values never appear in diagnostics.
    assert "evil-org" not in binding.reason


def test_observed_window_after_the_job_end_is_a_contradiction() -> None:
    run = scheduled_run()
    claim = job_claim(run, job_key="e2e", observed_started_at=datetime(2030, 1, 1, tzinfo=UTC))

    binding = bind_job_execution(run, [e2e_scheduled_job()], claim)

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.reason == "claim_contradicts_job"
    assert binding.corroborations[1].status is CorroborationStatus.CONTRADICTORY


def test_observed_window_end_before_its_start_is_a_contradiction() -> None:
    run = scheduled_run()
    claim = job_claim(run, job_key="e2e", observed_started_at=at(27, 18, 0), observed_completed_at=at(27, 17, 30))

    binding = bind_job_execution(run, [e2e_scheduled_job()], claim)

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.corroborations[1].status is CorroborationStatus.CONTRADICTORY


def test_runner_contradiction_rejects_the_claim() -> None:
    run = scheduled_run()
    claim = job_claim(run, job_key="e2e", runner_name="osac-9-runner-99")

    binding = bind_job_execution(run, [e2e_scheduled_job()], claim)

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.corroborations[0].status is CorroborationStatus.CONTRADICTORY


def test_empty_runner_name_is_an_absence_not_a_malformed_record() -> None:
    run = scheduled_run()
    job = GitHubJob.from_api(job_raw(runner_name=""))

    binding = bind_job_execution(run, [job], job_claim(run, job_key="e2e"))

    assert binding.verdict is Verdict.CANDIDATE
    assert binding.job is not None and binding.job.runner_name is None
    assert binding.corroborations[0].status is CorroborationStatus.NOT_CHECKED


def test_naive_claim_timestamp_is_not_checked() -> None:
    run = scheduled_run()
    claim = job_claim(run, job_key="e2e", observed_started_at=datetime(2026, 10, 27, 17, 15, 25))

    binding = bind_job_execution(run, [e2e_scheduled_job()], claim)

    assert binding.verdict is Verdict.CANDIDATE
    assert binding.corroborations[1].status is CorroborationStatus.NOT_CHECKED


def test_naive_claim_end_is_not_checked() -> None:
    run = scheduled_run()
    claim = job_claim(
        run, job_key="e2e", observed_started_at=at(27, 18, 0), observed_completed_at=datetime(2026, 10, 27, 18, 30)
    )

    binding = bind_job_execution(run, [e2e_scheduled_job()], claim)

    assert binding.verdict is Verdict.CANDIDATE
    assert binding.corroborations[1].status is CorroborationStatus.NOT_CHECKED


@pytest.mark.parametrize("conclusion", [None, "skipped"])
def test_unstarted_job_is_not_counted_as_an_execution(conclusion: str | None) -> None:
    run = scheduled_run()
    job = GitHubJob.from_api(job_raw(started_at=None, completed_at=None, conclusion=conclusion, runner_name=""))

    binding = bind_job_execution(run, [job], job_claim(run, job_key="e2e"))

    assert binding.executed_in_attempt is (False if conclusion == "skipped" else None)
    assert binding.execution_attempt is None


def test_trusted_id_rejects_a_contradictory_claimed_display_name() -> None:
    run = merge_group_run()
    claim = job_claim(run, job_display_name="Run tests (b)")

    binding = bind_job_execution(run, matrix_jobs(), claim, expected_job_id=MATRIX_A_JOB_ID)

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.reason == "claim_contradicts_job"


def test_trusted_name_cannot_choose_between_old_attempts() -> None:
    run = dataclasses.replace(rerun_run(), run_attempt=3)
    first = attempt1_verify_ref()
    second = dataclasses.replace(first, id=first.id + 100, run_attempt=2)
    claim = job_claim(run, job_display_name=first.name)

    binding = bind_job_execution(run, [first, second], claim, expected_display_name=first.name)

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.reason == "ambiguous_expected_job"


def test_carried_over_job_is_inherited_not_reexecuted() -> None:
    run = rerun_run()
    jobs = [e2e_rerun_job(), carried_verify_job()]

    reexecuted = bind_job_execution(run, jobs, job_claim(run, job_key="e2e"))
    inherited = bind_job_execution(
        run, jobs, job_claim(run, job_display_name="verify / Verify the inputs against the pull request")
    )

    assert reexecuted.verdict is Verdict.CANDIDATE
    assert reexecuted.numeric_job_id == E2E_RERUN_JOB_ID
    assert reexecuted.executed_in_attempt is True
    assert reexecuted.execution_attempt == 2
    assert inherited.verdict is Verdict.CANDIDATE
    assert inherited.numeric_job_id == CARRIED_VERIFY_JOB_ID
    assert inherited.executed_in_attempt is False
    assert inherited.execution_attempt is None
    assert inherited.github_conclusion == "success"


def test_old_attempt_reference_is_inherited_not_a_current_execution() -> None:
    run = rerun_run()
    jobs = [e2e_rerun_job(), attempt1_verify_ref()]

    binding = bind_job_execution(
        run, jobs, job_claim(run, job_display_name="verify / Verify the inputs against the pull request")
    )

    assert binding.verdict is Verdict.CANDIDATE
    assert binding.numeric_job_id == ATTEMPT1_VERIFY_JOB_ID
    assert binding.executed_in_attempt is False
    assert binding.execution_attempt == 1
    assert binding.inherited_refs == (attempt1_verify_ref(),)


def test_expected_job_id_can_find_an_inherited_reference() -> None:
    run = rerun_run()
    jobs = [e2e_rerun_job(), attempt1_verify_ref()]

    binding = bind_job_execution(run, jobs, job_claim(run, job_key="e2e"), expected_job_id=ATTEMPT1_VERIFY_JOB_ID)

    assert binding.verdict is Verdict.VERIFIED
    assert binding.executed_in_attempt is False
    assert binding.execution_attempt == 1


def test_conflicting_job_records_are_rejected() -> None:
    run = scheduled_run()
    other = GitHubJob.from_api(job_raw(conclusion="success"))

    with pytest.raises(MalformedGitHubMetadataError):
        bind_job_execution(run, [e2e_scheduled_job(), other], job_claim(run, job_key="e2e"))


def test_identical_job_records_collapse() -> None:
    run = scheduled_run()

    binding = bind_job_execution(run, [e2e_scheduled_job(), e2e_scheduled_job()], job_claim(run, job_key="e2e"))

    assert binding.verdict is Verdict.CANDIDATE


def test_foreign_jobs_are_recorded_not_matched() -> None:
    run = pr_suite_run()
    foreign_run = GitHubJob.from_api(job_raw(run_id=99999999999))
    foreign_attempt = GitHubJob.from_api(job_raw(id=91000000099, run_attempt=7))

    binding = bind_job_execution(run, [e2e_pr_job(), foreign_run, foreign_attempt], job_claim(run, job_key="e2e"))

    assert binding.verdict is Verdict.CANDIDATE
    assert binding.numeric_job_id == E2E_PR_JOB_ID
    assert binding.foreign_jobs == (foreign_run, foreign_attempt)


def test_job_key_without_a_match_stays_unknown() -> None:
    run = GitHubRun.from_api(run_raw(id=RUN_CI_ID, event="pull_request", head_sha=PULL_HEAD_SHA))
    leg = GitHubJob.from_api(
        job_raw(
            id=UNIT_LEG_JOB_ID,
            run_id=RUN_CI_ID,
            name="Unit tests (fulfillment-service) / fulfillment-service",
            conclusion="success",
            runner_name="GitHub Actions 1027656359",
        )
    )

    binding = bind_job_execution(run, [leg], job_claim(run, job_key="run"))

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.reason == "claim_identity_no_match"


def test_nested_reusable_job_key_matches_as_a_candidate() -> None:
    run = GitHubRun.from_api(run_raw(id=RUN_CI_ID, event="pull_request", head_sha=PULL_HEAD_SHA))
    leg = GitHubJob.from_api(
        job_raw(
            id=UNIT_LEG_JOB_ID,
            run_id=RUN_CI_ID,
            name="Unit tests (fulfillment-service) / fulfillment-service",
            conclusion="success",
            runner_name="GitHub Actions 1027656359",
        )
    )

    binding = bind_job_execution(run, [leg], job_claim(run, job_key="fulfillment-service"))

    assert binding.verdict is Verdict.CANDIDATE
    assert binding.numeric_job_id == UNIT_LEG_JOB_ID


def test_claim_without_job_identity_stays_unknown() -> None:
    run = scheduled_run()

    binding = bind_job_execution(run, [e2e_scheduled_job()], job_claim(run))

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.reason == "claim_without_job_identity"


def test_binding_the_same_claim_twice_changes_nothing() -> None:
    run = scheduled_run()
    claim = job_claim(run, job_key="e2e")
    jobs = [e2e_scheduled_job()]

    assert bind_job_execution(run, jobs, claim) == bind_job_execution(run, jobs, claim)


def test_trusted_status_verifies_the_tested_sha() -> None:
    binding = resolve_pr_suite()

    assert binding.verdict is Verdict.VERIFIED
    assert binding.tested_sha == TESTED_SHA
    assert binding.run_head_sha == MAIN_SHA
    assert binding.tested_sha != binding.run_head_sha
    assert binding.provenance is RevisionProvenance.TRUSTED_COMMIT_STATUS
    assert binding.status_evidence is not None
    assert binding.status_evidence.creator_id == TRUSTED_BOT_ID
    assert binding.status_evidence.target_run_id == RUN_PR_SUITE_ID
    assert binding.status_evidence.target_job_id == E2E_PR_JOB_ID


def test_status_creator_id_not_configured_stays_unknown() -> None:
    binding = resolve_pr_suite(trusted_creator_ids=frozenset({999999999}))

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.reason == "no_trusted_status_binding"


def test_status_creator_not_a_bot_stays_unknown() -> None:
    binding = resolve_pr_suite(statuses=[status(creator={"id": TRUSTED_BOT_ID, "type": "User"})])

    assert binding.verdict is Verdict.UNKNOWN


def test_status_without_expected_job_id_stays_unknown() -> None:
    binding = resolve_pr_suite(expected_job_id=None)

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.reason == "missing_expected_job_id"


def test_status_with_a_wrong_expected_job_stays_unknown() -> None:
    binding = resolve_pr_suite(expected_job_id=VERIFY_PR_JOB_ID)

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.reason == "no_trusted_status_binding"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com.evil.com/osac-project/osac/actions/runs/90000000002/job/91000000002",
        "http://github.com/osac-project/osac/actions/runs/90000000002/job/91000000002",
        "https://github.com/osac-project/osac/actions/runs/90000000002/job/91000000002/extra",
        "https://github.com/osac-project/osac/actions/runs/90000000002/job/91000000002/",
        "https://github.com/osac-project/osac/actions/runs/90000000002/job/91000000002?x=1",
        "https://github.com/osac-project/osac/actions/runs/90000000002/job/91000000002#fragment",
        "https://github.com//osac-project/osac/actions/runs/90000000002/job/91000000002",
        "https://github.com/osac-project/osac/actions/runs/90000000002/jobs/91000000002",
        "https://github.com/osac-project/osac/actions/runs/90000000002/job/91000000002/../job/91000000002",
        # A broken IPv6 literal makes urlparse itself raise ValueError.
        "https://[::1/osac-project/osac/actions/runs/90000000002/job/91000000002",
        "https://user@github.com/osac-project/osac/actions/runs/90000000002/job/91000000002",
        "https://GITHUB.COM/osac-project/osac/actions/runs/90000000002/job/91000000002",
        "https://g\u0435ithub.com/osac-project/osac/actions/runs/90000000002/job/91000000002",
        "https://github.com/osac-project/osac/actions/runs/\x0090000000002/job/91000000002",
        # Non-ASCII decimal digits are digits to str.isdigit() and int().
        "https://github.com/osac-project/osac/actions/runs/٩٠/job/٩١",
        "https://github.com/osac-project/osac/actions/runs/0/job/91000000002",
        "https://github.com/osac-project/osac/actions/runs/90000000002/job/0",
        "https://github.com/osac-project/osac/actions/runs/" + "9" * 25 + "/job/91000000002",
        " https://github.com/osac-project/osac/actions/runs/90000000002/job/91000000002",
        "https://github.com/osac-project/osac/actions/runs/090000000002/job/91000000002",
        "https://github.com/osac-project/osac/actions/runs/90000000002/job/91000000002\n",
        "",
    ],
)
def test_spoofed_target_urls_never_bind(url: str) -> None:
    assert parse_status_target_url(url) is None

    binding = resolve_pr_suite(statuses=[status(target_url=url)])

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.tested_sha is None


def test_changed_pr_head_does_not_move_the_tested_sha() -> None:
    binding = resolve_pr_suite(
        statuses=[
            status(sha=TESTED_SHA),
            status(sha=NEW_HEAD_SHA, target_url=f"https://github.com/{REPO}/actions/runs/90000000009/job/91000000009"),
        ]
    )

    assert binding.verdict is Verdict.VERIFIED
    assert binding.tested_sha == TESTED_SHA
    assert binding.tested_sha != NEW_HEAD_SHA


def test_dispatch_without_a_trusted_status_stays_unknown() -> None:
    binding = resolve_pr_suite(statuses=[])

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.tested_sha is None
    assert binding.reason == "no_trusted_status_binding"


@pytest.mark.parametrize("tests_run_head", [False, True])
def test_pull_request_tested_revision_stays_unknown_with_the_head_recorded(tests_run_head: bool) -> None:
    binding = resolve_tested_revision(ci_run(), [], tests_run_head=tests_run_head)

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.tested_sha is None
    assert binding.run_head_sha == PULL_HEAD_SHA
    assert binding.reason == "pull_request_checkout_is_merge_commit"


@pytest.mark.parametrize("run_factory", [scheduled_run, merge_group_run])
def test_head_events_need_the_checkout_policy(run_factory: Callable[[], GitHubRun]) -> None:
    run = run_factory()

    without_policy = resolve_tested_revision(run, [])
    with_policy = resolve_tested_revision(run, [], tests_run_head=True)

    assert without_policy.verdict is Verdict.UNKNOWN
    assert without_policy.tested_sha is None
    assert without_policy.reason == "checkout_policy_not_supplied"
    assert without_policy.run_head_sha == run.head_sha
    assert with_policy.verdict is Verdict.VERIFIED
    assert with_policy.tested_sha == run.head_sha
    assert with_policy.provenance is RevisionProvenance.DEFAULT_CHECKOUT_POLICY


def test_override_revision_is_claimed_not_verified() -> None:
    run = scheduled_run()  # flake campaigns run on a schedule
    override = RevisionOverride(sha=TESTED_SHA, description="flake-campaign plan test_sha")

    binding = resolve_tested_revision(run, [], override=override)

    assert binding.verdict is Verdict.CLAIMED
    assert binding.tested_sha == TESTED_SHA
    assert binding.tested_sha != run.head_sha
    assert binding.provenance is RevisionProvenance.PRODUCER_OVERRIDE


def test_trusted_statuses_on_two_commits_stay_ambiguous() -> None:
    binding = resolve_pr_suite(statuses=[status(sha=TESTED_SHA), status(sha=NEW_HEAD_SHA)])

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.reason == "trusted_status_ambiguous_sha"


def test_conflicting_status_evidence_cannot_fall_back_to_checkout_policy() -> None:
    binding = resolve_tested_revision(
        pr_suite_run(),
        [e2e_pr_job()],
        [status(sha=TESTED_SHA), status(sha=NEW_HEAD_SHA)],
        expected_context=VMaaS_CONTEXT,
        trusted_creator_ids=TRUSTED_CREATORS,
        expected_job_id=E2E_PR_JOB_ID,
        tests_run_head=True,
    )

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.tested_sha is None
    assert binding.reason == "trusted_status_ambiguous_sha"


def test_event_without_a_rule_stays_unknown() -> None:
    run = GitHubRun.from_api(run_raw(event="workflow_run"))

    binding = resolve_tested_revision(run, [])

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.reason == "no_rule_for_event"


def test_push_requires_an_explicit_checkout_policy() -> None:
    run = GitHubRun.from_api(run_raw(event="push"))

    unknown = resolve_tested_revision(run, [])
    verified = resolve_tested_revision(run, [], tests_run_head=True)

    assert unknown.verdict is Verdict.UNKNOWN
    assert verified.verdict is Verdict.VERIFIED
    assert verified.tested_sha == run.head_sha


def test_status_target_url_parses_to_run_and_job() -> None:
    target = parse_status_target_url(status_raw()["target_url"])

    assert target == StatusTarget(repo=REPO, run_id=RUN_PR_SUITE_ID, job_id=E2E_PR_JOB_ID)


def test_artifact_binds_with_a_verified_digest() -> None:
    run = scheduled_run()
    claim = artifact_claim(run, f"osac-logs-full-install-{RUN_SCHEDULED_ID}")

    binding = bind_artifact(run, [e2e_scheduled_job()], [scheduled_artifact()], claim, received_digest=DIGEST_A)

    assert binding.verdict is Verdict.VERIFIED
    assert binding.artifact_id == 92000000001
    assert binding.integrity is IntegrityStatus.VERIFIED
    assert binding.expired is False
    assert binding.timing_attribution is TimingAttribution.UNIQUE_JOB_WINDOW
    assert binding.attributed_attempt == 1


def test_artifact_identity_from_a_unique_name_without_digest() -> None:
    run = scheduled_run()
    claim = artifact_claim(run, f"osac-logs-full-install-{RUN_SCHEDULED_ID}")

    binding = bind_artifact(run, [e2e_scheduled_job()], [scheduled_artifact()], claim)

    assert binding.verdict is Verdict.VERIFIED
    assert binding.integrity is IntegrityStatus.NOT_CHECKED


def test_artifact_of_a_foreign_run_is_rejected_even_with_matching_name_and_digest() -> None:
    run = scheduled_run()
    foreign = ArtifactMetadata.from_api(artifact_raw(workflow_run={"id": 99999999999}))
    claim = artifact_claim(run, f"osac-logs-full-install-{RUN_SCHEDULED_ID}")

    binding = bind_artifact(run, [e2e_scheduled_job()], [foreign], claim, received_digest=DIGEST_A)

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.reason == "foreign_run_artifact"
    assert binding.artifact_id is None


def test_same_name_artifacts_disambiguate_by_the_caller_digest() -> None:
    run = rerun_run()
    jobs = [e2e_rerun_job(), carried_verify_job()]
    artifacts = rerun_artifacts()
    name = f"osac-logs-full-install-{RUN_RERUN_ID}"

    bound = bind_artifact(run, jobs, artifacts, artifact_claim(run, name, run_attempt=2), received_digest=DIGEST_B)
    no_digest = bind_artifact(run, jobs, artifacts, artifact_claim(run, name))
    wrong_digest = bind_artifact(run, jobs, artifacts, artifact_claim(run, name), received_digest=DIGEST_A)

    assert bound.verdict is Verdict.VERIFIED
    assert bound.artifact_id == 92000000002
    assert bound.integrity is IntegrityStatus.VERIFIED
    assert bound.timing_attribution is TimingAttribution.UNIQUE_JOB_WINDOW
    assert bound.attributed_attempt == 2
    assert bound.attempt_conflict is False
    assert no_digest.verdict is Verdict.UNKNOWN
    assert no_digest.reason == "same_name_no_digest"
    assert wrong_digest.verdict is Verdict.UNKNOWN
    assert wrong_digest.reason == "digest_matches_no_same_name_artifact"


def test_digest_mismatch_stays_unknown() -> None:
    run = scheduled_run()
    claim = artifact_claim(run, f"osac-logs-full-install-{RUN_SCHEDULED_ID}")

    binding = bind_artifact(run, [e2e_scheduled_job()], [scheduled_artifact()], claim, received_digest=DIGEST_B)

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.reason == "digest_mismatch"
    assert binding.integrity is IntegrityStatus.MISMATCH
    assert binding.artifact_id == 92000000001


def test_missing_artifact_name_stays_unknown() -> None:
    run = scheduled_run()
    claim = artifact_claim(run, "osac-logs-full-install-42")

    binding = bind_artifact(run, [e2e_scheduled_job()], [], claim, received_digest=DIGEST_A)

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.reason == "artifact_name_not_in_run"
    assert binding.expired is None


def test_expired_is_recorded_separately_from_identity_and_integrity() -> None:
    run = scheduled_run()
    expired = ArtifactMetadata.from_api(artifact_raw(expired=True))
    claim = artifact_claim(run, f"osac-logs-full-install-{RUN_SCHEDULED_ID}")

    binding = bind_artifact(run, [e2e_scheduled_job()], [expired], claim, received_digest=DIGEST_A)

    assert binding.verdict is Verdict.VERIFIED
    assert binding.expired is True
    assert binding.integrity is IntegrityStatus.VERIFIED


def test_an_inherited_jobs_old_window_never_attributes_an_artifact() -> None:
    run = rerun_run()
    jobs = [e2e_rerun_job(), carried_verify_job()]
    # Created inside the carried-over verify job's attempt-1 window.
    leftover = ArtifactMetadata.from_api(
        artifact_raw(
            id=92000000004, digest=DIGEST_C, created_at="2026-10-26T15:24:00Z", workflow_run={"id": RUN_RERUN_ID}
        )
    )
    claim = artifact_claim(run, f"osac-logs-full-install-{RUN_SCHEDULED_ID}")

    binding = bind_artifact(run, jobs, [leftover], claim, received_digest=DIGEST_C)

    assert binding.verdict is Verdict.VERIFIED
    assert binding.timing_attribution is TimingAttribution.NO_JOB_WINDOW
    assert binding.attributed_attempt is None


def test_artifact_without_timestamps_has_no_timing_attribution() -> None:
    run = scheduled_run()
    artifact = ArtifactMetadata.from_api(artifact_raw(created_at=None))
    claim = artifact_claim(run, f"osac-logs-full-install-{RUN_SCHEDULED_ID}")

    binding = bind_artifact(run, [e2e_scheduled_job()], [artifact], claim, received_digest=DIGEST_A)

    assert binding.verdict is Verdict.VERIFIED
    assert binding.timing_attribution is TimingAttribution.NO_TIMING_DATA


def test_artifact_inside_overlapping_windows_stays_ambiguous() -> None:
    run = rerun_run()
    jobs = [
        GitHubJob.from_api(
            job_raw(
                id=E2E_RERUN_JOB_ID,
                run_id=RUN_RERUN_ID,
                run_attempt=2,
                started_at="2026-10-26T20:20:00Z",
                completed_at="2026-10-26T22:00:00Z",
            )
        ),
        GitHubJob.from_api(
            job_raw(
                id=91000000010,
                run_id=RUN_RERUN_ID,
                run_attempt=2,
                name="gather / Gather cluster logs",
                conclusion="success",
                started_at="2026-10-26T20:20:00Z",
                completed_at="2026-10-26T21:50:00Z",
                runner_name="osac-12-runner-02",
            )
        ),
    ]
    artifact = ArtifactMetadata.from_api(
        artifact_raw(
            id=92000000002,
            digest=DIGEST_B,
            name=f"osac-logs-full-install-{RUN_RERUN_ID}",
            created_at="2026-10-26T21:30:00Z",
            workflow_run={"id": RUN_RERUN_ID},
        )
    )
    claim = artifact_claim(run, f"osac-logs-full-install-{RUN_RERUN_ID}")

    binding = bind_artifact(run, jobs, [artifact], claim, received_digest=DIGEST_B)

    assert binding.verdict is Verdict.VERIFIED
    assert binding.timing_attribution is TimingAttribution.AMBIGUOUS
    assert binding.attributed_attempt is None


def test_claimed_attempt_conflicting_with_timing_is_flagged() -> None:
    run = rerun_run()
    jobs = [e2e_rerun_job(), carried_verify_job()]
    claim = artifact_claim(run, f"osac-logs-full-install-{RUN_RERUN_ID}", run_attempt=1)

    binding = bind_artifact(run, jobs, rerun_artifacts(), claim, received_digest=DIGEST_B)

    assert binding.verdict is Verdict.VERIFIED
    assert binding.attributed_attempt == 2
    assert binding.attempt_conflict is True


def test_identical_artifact_records_collapse() -> None:
    run = scheduled_run()
    claim = artifact_claim(run, f"osac-logs-full-install-{RUN_SCHEDULED_ID}")

    binding = bind_artifact(
        run, [e2e_scheduled_job()], [scheduled_artifact(), scheduled_artifact()], claim, received_digest=DIGEST_A
    )

    assert binding.verdict is Verdict.VERIFIED
    assert binding.artifact_id == 92000000001


def test_conflicting_artifact_records_are_rejected() -> None:
    run = scheduled_run()
    conflicting = ArtifactMetadata.from_api(artifact_raw(digest=DIGEST_B))
    claim = artifact_claim(run, f"osac-logs-full-install-{RUN_SCHEDULED_ID}")

    with pytest.raises(MalformedGitHubMetadataError):
        bind_artifact(run, [e2e_scheduled_job()], [scheduled_artifact(), conflicting], claim)


def test_malformed_received_digest_stays_unknown() -> None:
    run = scheduled_run()
    claim = artifact_claim(run, f"osac-logs-full-install-{RUN_SCHEDULED_ID}")

    binding = bind_artifact(run, [e2e_scheduled_job()], [scheduled_artifact()], claim, received_digest="md5:abc")

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.reason == "malformed_received_digest"
    assert "md5:abc" not in binding.reason


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [({"repo": "evil-org/osac"}, "claim_repo_mismatch"), ({"run_id": 424242}, "claim_run_mismatch")],
)
def test_artifact_claim_gates_stay_unknown(overrides: dict[str, JSONValue], reason: str) -> None:
    run = scheduled_run()
    claim = dataclasses.replace(
        ArtifactClaim(repo=run.repo, run_id=run.id, artifact_name=f"osac-logs-full-install-{RUN_SCHEDULED_ID}"),
        **overrides,
    )

    binding = bind_artifact(run, [e2e_scheduled_job()], [scheduled_artifact()], claim)

    assert binding.verdict is Verdict.UNKNOWN
    assert binding.reason == reason


@pytest.mark.parametrize(
    ("kind", "overrides"),
    [
        ("run", {"head_sha": "abc"}),
        ("run", {"head_sha": MAIN_SHA + "\n"}),
        ("run", {"repository": {"full_name": REPO + "\n"}}),
        ("run", {"repository": {}}),
        ("run", {"repository": None}),
        ("run", {"run_attempt": 0}),
        ("run", {"event": ""}),
        ("run", {"run_started_at": "2026-10-27T17:15:19"}),
        ("run", {"run_started_at": "not-a-date"}),
        ("run", {"run_started_at": 17}),
        ("job", {"id": "x"}),
        ("job", {"run_attempt": 0}),
        ("job", {"name": ""}),
        ("job", {"started_at": "2026-10-27T17:15:21"}),
        ("status", {"creator": "osac-ci[bot]"}),
        ("status", {"context": None}),
        ("artifact", {"digest": "md5:abc"}),
        ("artifact", {"expired": "no"}),
        ("artifact", {"workflow_run": None}),
        ("artifact", {"workflow_run": {"id": -1}}),
    ],
)
def test_malformed_metadata_raises(kind: str, overrides: dict[str, JSONValue]) -> None:
    with pytest.raises(MalformedGitHubMetadataError):
        if kind == "run":
            GitHubRun.from_api({**run_raw(), **overrides})
        elif kind == "job":
            GitHubJob.from_api({**job_raw(), **overrides})
        elif kind == "status":
            CommitStatus.from_api({**status_raw(), **overrides}, TESTED_SHA)
        else:
            ArtifactMetadata.from_api({**artifact_raw(), **overrides})


def test_malformed_metadata_errors_never_echo_the_value() -> None:
    with pytest.raises(MalformedGitHubMetadataError) as excinfo:
        GitHubRun.from_api(run_raw(head_sha="SHORTSECRETVALUE"))
    assert "SHORTSECRETVALUE" not in str(excinfo.value)

    invalid_timestamp = "2030-99-99T99:99:99SECRET"
    with pytest.raises(MalformedGitHubMetadataError) as excinfo:
        GitHubRun.from_api(run_raw(run_started_at=invalid_timestamp))
    assert "SECRET" not in str(excinfo.value)
    assert invalid_timestamp not in "".join(traceback.format_exception(excinfo.value))


def test_from_api_tolerates_unconsumed_fields() -> None:
    job = GitHubJob.from_api(
        job_raw(
            labels=["osac-ci"],
            status="completed",
            workflow_name="E2E",
            html_url="https://example.invalid",
            steps=[{"number": 1}],
            runner_group_name="default",
        )
    )
    run = GitHubRun.from_api(
        run_raw(
            name="E2E VMaaS Full Install",
            display_title=f"E2E suite \u00b7 vmaas \u00b7 {TESTED_SHA}",
            head_branch="main",
            conclusion="failure",
            status="completed",
            created_at="2026-10-27T17:15:19Z",
        )
    )

    assert job.id == E2E_SCHEDULED_JOB_ID
    assert run.head_sha == SCHEDULED_SHA
