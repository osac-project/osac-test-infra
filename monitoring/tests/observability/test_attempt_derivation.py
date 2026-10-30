"""Tests for the reference attempt-result derivation."""

from __future__ import annotations

import pytest

from monitoring.ci_observability.contracts.derivation import derive_attempt_result


def phases(*entries: tuple[str, str, str | None]) -> list[dict[str, object]]:
    """Phase observations: (phase, outcome, failure signature or None)."""
    observed: list[dict[str, object]] = []
    for phase, outcome, signature in entries:
        item: dict[str, object] = {"phase": phase, "outcome": outcome}
        if signature is not None:
            item["failure"] = {"signature": signature}
        observed.append(item)
    return observed


def test_no_phases_means_unknown() -> None:
    result = derive_attempt_result(phases=[])
    assert result.outcome == "unknown"
    assert result.first_failure is None
    assert result.cleanup_failure is None


def test_setup_xfail_becomes_the_attempt_outcome() -> None:
    result = derive_attempt_result(phases=phases(("setup", "expected_failure", None)))
    assert result.outcome == "expected_failure"


def test_setup_skip_means_the_body_never_ran() -> None:
    result = derive_attempt_result(phases=phases(("setup", "skipped", None)))
    assert result.outcome == "skipped"


def test_call_outcome_stands_on_its_own() -> None:
    result = derive_attempt_result(
        phases=phases(("setup", "passed", None), ("call", "unexpected_pass", None), ("teardown", "passed", None))
    )
    assert result.outcome == "unexpected_pass"
    assert result.cleanup_failure is None


def test_teardown_failure_fails_a_passing_body_and_is_kept_separate() -> None:
    result = derive_attempt_result(
        phases=phases(
            ("setup", "passed", None), ("call", "passed", None), ("teardown", "failed", "NamespaceCleanupError")
        )
    )
    assert result.outcome == "failed"
    assert result.cleanup_failure == {"signature": "NamespaceCleanupError"}
    assert result.first_failure == result.cleanup_failure


def test_body_failure_stays_first_and_cleanup_failure_stays_separate() -> None:
    result = derive_attempt_result(
        phases=phases(
            ("setup", "passed", None), ("call", "failed", "AssertionError"), ("teardown", "timeout", "DeadlineExceeded")
        )
    )
    assert result.outcome == "failed"
    assert result.first_failure == {"signature": "AssertionError"}
    assert result.cleanup_failure == {"signature": "DeadlineExceeded"}


def test_interrupted_phase_wins_over_everything() -> None:
    result = derive_attempt_result(phases=phases(("setup", "passed", None), ("call", "interrupted", None)))
    assert result.outcome == "interrupted"


def test_unknown_phase_is_not_a_silent_pass() -> None:
    result = derive_attempt_result(phases=phases(("setup", "passed", None), ("call", "unknown", None)))
    assert result.outcome == "unknown"


def test_missing_call_phase_stays_unknown() -> None:
    result = derive_attempt_result(phases=phases(("setup", "passed", None)))
    assert result.outcome == "unknown"


def test_cleanup_failure_without_signature_still_fails_the_attempt() -> None:
    result = derive_attempt_result(phases=phases(("call", "passed", None), ("teardown", "failed", None)))
    assert result.outcome == "failed"
    assert result.first_failure is None
    assert result.cleanup_failure is None


@pytest.mark.parametrize("setup_outcome", ["skipped", "expected_failure"])
def test_cleanup_failure_is_preserved_after_setup_did_not_pass(setup_outcome: str) -> None:
    result = derive_attempt_result(
        phases=phases(("setup", setup_outcome, None), ("teardown", "failed", "CleanupError"))
    )
    assert result.outcome == "failed"
    assert result.first_failure == {"signature": "CleanupError"}
    assert result.cleanup_failure == result.first_failure


@pytest.mark.parametrize("ending", ["unknown", "interrupted"])
def test_incomplete_ending_never_erases_an_observed_failure(ending: str) -> None:
    result = derive_attempt_result(phases=phases(("call", "failed", "AssertionError"), ("teardown", ending, None)))
    assert result.outcome == ending
    assert result.first_failure == {"signature": "AssertionError"}


def test_unknown_body_failure_details_are_not_replaced_by_cleanup_details() -> None:
    result = derive_attempt_result(phases=phases(("call", "failed", None), ("teardown", "failed", "CleanupError")))
    assert result.outcome == "failed"
    assert result.first_failure is None
    assert result.cleanup_failure == {"signature": "CleanupError"}


def test_duplicate_phases_are_rejected_instead_of_overwritten() -> None:
    with pytest.raises(ValueError, match="duplicate-phase"):
        derive_attempt_result(phases=phases(("call", "failed", "AssertionError"), ("call", "passed", None)))
