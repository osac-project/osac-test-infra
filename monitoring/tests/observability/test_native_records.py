"""Shared records-primitive boundary tests.

The InvocationContext is the adapter's only source of suite taxonomy and
child exit identity. Its bounds are the frozen v1 contract's bounds,
validated here against the owning frozen validator rather than a private
mini-schema: every context the constructor approves must produce
invocation records the frozen validator accepts, and every bad present
field must fail as a fixed AdapterError.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time

import pytest

from monitoring.ci_observability.contracts.validation import validate_record
from monitoring.ci_observability.native.records import (
    AdapterError,
    InvocationContext,
    epoch_ms_to_utc_z,
    journal_record,
)

VALID_FRAMEWORKS = ("pytest", "ginkgo", "go-test", "vitest", "ansible")
VALID_TIERS = ("unit", "envtest", "component-integration", "contract", "e2e")


def context(**overrides: object) -> InvocationContext:
    values: dict[str, object] = {
        "execution_id": "w03-records-exec",
        "journal_id": "records-journal",
        "invocation_id": "records-invocation",
        "framework": "pytest",
        "tier": "unit",
        "component": "osac-operator",
        "variant": "unit-go",
        "observed_at": "2026-10-28T12:00:00Z",
        "child_exit_code": None,
    }
    values.update(overrides)
    return InvocationContext(**values)  # type: ignore[arg-type]


def invocation_started_record(approved: InvocationContext) -> dict[str, object]:
    """One invocation_started record built exactly as the converters
    build it, for validation against the frozen contract."""
    started: dict[str, object] = {
        "invocation_id": approved.invocation_id,
        "framework": approved.framework,
        "tier": approved.tier,
        "component": approved.component,
        "variant": approved.variant,
    }
    if approved.selection is not None:
        started["selection"] = approved.selection
    return journal_record(context=approved, sequence=1, event="invocation_started", data=started)


@pytest.mark.parametrize("framework", VALID_FRAMEWORKS)
@pytest.mark.parametrize("tier", VALID_TIERS)
def test_every_approved_taxonomy_produces_frozen_valid_records(framework: str, tier: str) -> None:
    approved = context(framework=framework, tier=tier, component="c" * 64, variant="v" * 64)
    record = invocation_started_record(approved)
    validate_record(record=record)


def test_approved_selection_shapes_produce_frozen_valid_records() -> None:
    approved = context(
        selection={"paths": ["p" * 256] * 32, "markers": "m" * 256, "keyword": "k" * 256, "worker_count": 4}
    )
    validate_record(record=invocation_started_record(approved))
    null_workers = context(selection={"worker_count": None, "paths": []})
    validate_record(record=invocation_started_record(null_workers))


@pytest.mark.parametrize(
    "overrides",
    [
        {"component": "c" * 65},
        {"variant": "v" * 65},
        {"component": ""},
        {"framework": "unsupported"},
        {"framework": "mixed"},
        {"tier": "unsupported"},
        {"tier": "mixed"},
        {"framework": True},
        {"tier": 3},
        {"component": 7},
        {"selection": {"unknown": 1}},
        {"selection": {"paths": ["p"] * 33}},
        {"selection": {"paths": ["p" * 257]}},
        {"selection": {"paths": [""]}},
        {"selection": {"markers": "m" * 257}},
        {"selection": {"keyword": "k" * 257}},
        {"selection": {"worker_count": -1}},
        {"selection": {"worker_count": True}},
        {"selection": {"worker_count": "4"}},
        {"selection": ["not", "an", "object"]},
    ],
)
def test_bad_present_taxonomy_fields_fail_as_adapter_errors(overrides: dict[str, object]) -> None:
    with pytest.raises(AdapterError):
        context(**overrides)


def test_taxonomy_bounds_come_from_the_frozen_contract_not_a_drift() -> None:
    """The context's approved set is exactly the frozen invocation set:
    the five native frameworks (the job-level 'mixed' value is not a
    native invocation), the five frozen tiers, and the 64-character
    component and variant bounds."""
    assert len(VALID_FRAMEWORKS) == 5 and "mixed" not in VALID_FRAMEWORKS
    assert len(VALID_TIERS) == 5


def test_real_signalled_child_exit_is_preserved_exactly() -> None:
    """A real child killed by SIGTERM reports a negative Popen return
    code. The frozen invocation_finished.exit_code admits any plain
    integer, so the measured signal code is preserved exactly: never
    coerced to a POSIX 128+n value, never replaced with null."""

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        time.sleep(0.2)  # let the child reach the sleep
        child.send_signal(signal.SIGTERM)
        measured = child.wait(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=30)
    assert measured == -signal.SIGTERM

    approved = context(child_exit_code=measured)
    finished = journal_record(
        context=approved,
        sequence=2,
        event="invocation_finished",
        data={"invocation_id": approved.invocation_id, "outcome": "failed", "exit_code": measured},
    )
    validate_record(record=finished)
    assert finished["data"]["exit_code"] == -15


@pytest.mark.parametrize("bad_code", [True, False, "1", 1.5, []])
def test_non_integer_exit_codes_fail(bad_code: object) -> None:
    with pytest.raises(AdapterError):
        context(child_exit_code=bad_code)  # type: ignore[arg-type]


@pytest.mark.parametrize("extreme", [1e30, 10**30, -1])
def test_extreme_epoch_values_fail_as_adapter_errors(extreme: float) -> None:
    """A finite JSON number far outside the supported calendar must be
    refused with the curated error, not raise OverflowError from
    timedelta arithmetic."""
    with pytest.raises(AdapterError):
        epoch_ms_to_utc_z(value=extreme)
