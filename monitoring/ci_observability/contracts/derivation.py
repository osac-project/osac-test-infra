"""Reference derivation of a test attempt outcome from its phase outcomes.

The contract stores phase outcomes as observed and derives the attempt
outcome, so a cleanup failure can never overwrite the first failure and a
passing body can never hide a failing teardown. Consumers may implement
their own derivation, but it must agree with this one on the cases below.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

_FAILURE_OUTCOMES = ("failed", "error", "timeout")
_PHASES = ("setup", "call", "teardown")


@dataclass(frozen=True, kw_only=True)
class AttemptResult:
    """One derived attempt outcome.

    Missing failure details stay unknown. Cleanup never replaces an earlier
    failure, including when that earlier failure has no signature.
    """

    outcome: str
    first_failure: Mapping[str, object] | None
    cleanup_failure: Mapping[str, object] | None


def derive_attempt_result(*, phases: Sequence[Mapping[str, object]]) -> AttemptResult:
    """Derive one attempt outcome from its observed phase outcomes.

    Callers supply validated phase outcomes. Missing observations remain
    unknown; collection and invocation evidence establishes not-run tests.
    """
    if not phases:
        return AttemptResult(outcome="unknown", first_failure=None, cleanup_failure=None)

    by_phase: dict[str, Mapping[str, object]] = {}
    for phase in phases:
        name = cast(str, phase["phase"])
        if name not in _PHASES or name in by_phase:
            raise ValueError("invalid-or-duplicate-phase")
        by_phase[name] = phase

    first_failure = None
    for name in _PHASES:
        observed = by_phase.get(name)
        if observed is not None and observed["outcome"] in _FAILURE_OUTCOMES:
            first_failure = _failure(phase=observed)
            break

    teardown = by_phase.get("teardown")
    cleanup_failed = teardown is not None and teardown["outcome"] in _FAILURE_OUTCOMES
    cleanup_failure = _failure(phase=teardown) if cleanup_failed else None

    if any(phase["outcome"] == "interrupted" for phase in phases):
        return AttemptResult(outcome="interrupted", first_failure=first_failure, cleanup_failure=cleanup_failure)
    if any(phase["outcome"] == "unknown" for phase in phases):
        return AttemptResult(outcome="unknown", first_failure=first_failure, cleanup_failure=cleanup_failure)

    setup = by_phase.get("setup")
    if setup is not None and setup["outcome"] != "passed":
        outcome = cast(str, setup["outcome"])
    else:
        call = by_phase.get("call")
        outcome = cast(str, call["outcome"]) if call is not None else "unknown"
    if cleanup_failed and outcome in ("passed", "skipped", "expected_failure", "unexpected_pass"):
        outcome = "failed"

    return AttemptResult(outcome=outcome, first_failure=first_failure, cleanup_failure=cleanup_failure)


def _failure(*, phase: Mapping[str, object]) -> Mapping[str, object] | None:
    if "failure" not in phase:
        return None
    return cast(Mapping[str, object], phase["failure"])
