"""Schema-level contract tests for CI observability v1.

Each frozen field distinction gets a positive and a negative case through
the production validators: event types, phase outcomes as real frameworks
produce them, UTC calendar timestamps, identity fields, source roles,
native result scopes, and the manifest's security-sensitive constraints.
"""

from __future__ import annotations

from typing import Any

import pytest
from contract_helpers import make_manifest, make_record

from monitoring.ci_observability.contracts.validation import ContractError, validate_manifest, validate_record

EVENT_PAYLOADS: dict[str, dict[str, Any]] = {
    "execution_started": {
        "job": {"logical_job_key": "tests.yml#unit", "github_job_id": 1},
        "suite": {"framework": "pytest", "tier": "unit", "component": "e2e", "variant": "unit"},
    },
    "execution_finished": {"outcome": "passed", "exit_code": 0},
    "invocation_started": {
        "invocation_id": "ginkgo-run-1",
        "framework": "ginkgo",
        "tier": "unit",
        "component": "fulfillment-service",
        "variant": "unit-ginkgo",
        "selection": {"worker_count": 0},
        "dependencies": ["envtest"],
    },
    "invocation_finished": {"invocation_id": "ginkgo-run-1", "outcome": "failed", "exit_code": 1},
    "worker_started": {"worker_id": "gw0", "host": "osac-ci-3", "pid": 4242},
    "worker_stopped": {"worker_id": "gw0", "outcome": "completed"},
    "collection_finished": {"outcome": "completed", "collected_count": 3, "deselected_count": 1},
    "test_collected": {"test_id": "tests/a.py::test_a", "deselected": False},
    "test_phase_started": {"test_id": "tests/a.py::test_a", "attempt_index": 1, "phase": "call", "worker_id": "gw0"},
    "test_phase_finished": {
        "test_id": "tests/a.py::test_a",
        "attempt_index": 1,
        "phase": "call",
        "worker_id": "gw0",
        "outcome": "passed",
    },
    "test_result": {
        "result_scope": "aggregate",
        "test_id": "tests/a.py::test_a",
        "attempt_count": 2,
        "outcome": "failed",
        "worker_id": None,
        "duration_ns": 184801,
    },
    "fixture_started": {
        "fixture_instance_id": "fx-1",
        "fixture_id": "tests.conftest.f",
        "scope": "session",
        "worker_id": "gw0",
        "phase": "setup",
    },
    "fixture_finished": {
        "fixture_instance_id": "fx-1",
        "fixture_id": "tests.conftest.f",
        "scope": "session",
        "worker_id": "gw0",
        "phase": "teardown",
        "outcome": "passed",
    },
    "fixture_used": {
        "fixture_instance_id": "fx-1",
        "test_id": "tests/a.py::test_a",
        "attempt_index": 1,
        "worker_id": "gw0",
    },
    "operation_started": {
        "operation_id": "wait-1",
        "name": "wait_for_provision",
        "expected_condition": "ComputeInstance phase Ready",
        "timeout_ms": 60000,
        "resource": {
            "kind": "ComputeInstance",
            "uid": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "cluster_uid": "c1c1c1c1-aaaa-bbbb-cccc-dddddddddddd",
            "cluster_role": "management",
        },
        "test": {
            "test_id": "tests/a.py::test_a",
            "attempt_index": 1,
            "worker_id": "gw0",
            "invocation_id": "ginkgo-run-1",
        },
    },
    "operation_progress": {"operation_id": "wait-1", "elapsed_ms": 30000, "observed_state": "Provisioning"},
    "operation_finished": {"operation_id": "wait-1", "outcome": "passed", "duration_ns": 60000000000},
    "resource_observed": {
        "resource": {
            "kind": "BareMetalHost",
            "uid": "b0b0b0b0-1111-2222-3333-444444444444",
            "cluster_uid": "c1c1c1c1-aaaa-bbbb-cccc-dddddddddddd",
            "cluster_role": "lab",
            "name": "osp-master-0",
            "api_version": "metal3.io/v1beta1",
            "namespace": "osac",
            "owner_uids": ["a0a0a0a0-1111-2222-3333-444444444444"],
            "tenant": "tenant-1",
            "api_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        },
        "generation": 3,
        "ready": False,
        "status": "Provisioning",
        "finalizers": ["baremetalhost.metal3.io"],
        "conditions": [{"type": "Provisioned", "status": "False", "reason": "ExternallyProvisioned"}],
        "containers": [
            {
                "name": "manager",
                "init": False,
                "ready": False,
                "restarts": 3,
                "state": "Waiting",
                "reason": "CrashLoopBackOff",
            },
            {
                "name": "install-verification",
                "init": True,
                "ready": True,
                "restarts": 0,
                "state": "Terminated",
                "exit_code": 0,
            },
        ],
        "replicas": {"desired": 2, "ready": 1, "observed_generation": 4},
        "warning_events": [{"reason": "BackOff", "type": "Warning", "count": 42}],
    },
    "collection_state": {"source": "cluster-observer", "status": "collecting", "complete": False, "dropped_records": 0},
}


def journal_record(**overrides: object) -> dict[str, Any]:
    event = overrides.pop("event", "collection_state")
    if not isinstance(event, str):
        raise TypeError("event must be a string")
    payload = overrides.pop("data", None)
    if not isinstance(payload, dict):
        payload = EVENT_PAYLOADS.get(event, EVENT_PAYLOADS["collection_state"])
    return make_record(event, payload, **overrides)


@pytest.mark.parametrize("event", sorted(EVENT_PAYLOADS))
def test_every_event_type_has_a_valid_record(event: str) -> None:
    validate_record(record=journal_record(event=event))


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema_version": 2},
        {"schema": "osac.ci-observability.journal-record-v2"},
        {"event": "test_exploded"},
        {"recorded_at": "2026-10-27T10:00:00"},
        {"recorded_at": "2026-10-27T12:00:00+02:00"},
        {"recorded_at": "2026-99-99T10:00:00Z"},
        {"recorded_at": "2026-02-30T10:00:00Z"},
        {"sequence": 0},
        {"mono_ns": -1},
        {"execution_id": "../escape"},
        {"clock_uncertainty_ns": -1},
        {"extra_field": "value"},
    ],
)
def test_invalid_envelope_fields_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ContractError):
        validate_record(record=journal_record(**overrides))


def test_adapter_records_carry_no_monotonic_clock() -> None:
    validate_record(record=journal_record(mono_ns=None, source_at="2026-10-27T09:15:00.500Z"))
    with pytest.raises(ContractError):
        validate_record(record=journal_record(source_at="2026-10-27 09:15:00"))


def test_clock_domains_are_distinguished() -> None:
    validate_record(record=journal_record(clock_id="gw0", clock_uncertainty_ns=250000))


def test_invocation_id_scopes_records_to_one_native_process() -> None:
    validate_record(record=journal_record(invocation_id="ginkgo-run-2"))


def test_null_github_job_id_is_valid_and_a_string_is_rejected() -> None:
    job = {"logical_job_key": "tests.yml#unit", "github_job_id": None}
    suite = EVENT_PAYLOADS["execution_started"]["suite"]
    validate_record(record=journal_record(event="execution_started", data={"job": job, "suite": suite}))
    job["github_job_id"] = "unit"  # the GITHUB_JOB string
    with pytest.raises(ContractError):
        validate_record(record=journal_record(event="execution_started", data={"job": job, "suite": suite}))


def test_execution_outcome_cannot_claim_inherited_or_skipped() -> None:
    for outcome in ("inherited", "skipped", "not_run"):
        with pytest.raises(ContractError):
            validate_record(
                record=journal_record(event="execution_finished", data={"outcome": outcome, "exit_code": 0})
            )


def test_lost_process_exit_code_is_null_not_absent() -> None:
    validate_record(record=journal_record(event="execution_finished", data={"outcome": "unknown", "exit_code": None}))
    with pytest.raises(ContractError):
        validate_record(record=journal_record(event="execution_finished", data={"outcome": "unknown"}))


@pytest.mark.parametrize(
    "phase,outcome",
    [
        # Real pytest produces these: xfail markers evaluated in setup,
        # skips raised in teardown, and teardown timeouts.
        ("setup", "expected_failure"),
        ("teardown", "expected_failure"),
        ("setup", "skipped"),
        ("teardown", "skipped"),
        ("teardown", "timeout"),
        ("call", "unexpected_pass"),
        ("call", "interrupted"),
    ],
)
def test_native_phase_outcomes_are_preserved_in_their_phase(phase: str, outcome: str) -> None:
    data = {"test_id": "tests/a.py::test_a", "attempt_index": 1, "phase": phase, "worker_id": "gw0", "outcome": outcome}
    validate_record(record=journal_record(event="test_phase_finished", data=data))


@pytest.mark.parametrize("outcome", ["inherited", "not_run"])
def test_phase_outcomes_reserved_for_derivation_are_rejected(outcome: str) -> None:
    data = {
        "test_id": "tests/a.py::test_a",
        "attempt_index": 1,
        "phase": "call",
        "worker_id": "gw0",
        "outcome": outcome,
    }
    with pytest.raises(ContractError):
        validate_record(record=journal_record(event="test_phase_finished", data=data))


def test_native_result_distinguishes_attempt_and_aggregate_scopes() -> None:
    aggregate = {
        "result_scope": "aggregate",
        "test_id": "tests/a.py::test_a",
        "attempt_count": 2,
        "outcome": "failed",
        "worker_id": None,
        "duration_ns": 184801,
        "source_started_at": "2026-10-28T00:40:12.000Z",
        "source_ended_at": "2026-10-28T00:40:13.000Z",
    }
    validate_record(record=journal_record(event="test_result", data=aggregate))
    attempt = {
        "result_scope": "attempt",
        "test_id": "tests/a.py::test_a",
        "attempt_index": 1,
        "outcome": "failed",
        "worker_id": "ginkgo-run-1",
        "duration_ns": 114538,
        "source_started_at": "2026-10-28T00:40:12.000Z",
        "source_ended_at": "2026-10-28T00:40:12.100Z",
    }
    validate_record(record=journal_record(event="test_result", data=attempt))
    # Unknown retry count, worker, and times stay null, never zero.
    unknown = {
        "result_scope": "aggregate",
        "test_id": "tests/a.py::test_a",
        "attempt_count": None,
        "outcome": "unknown",
        "worker_id": None,
        "duration_ns": None,
    }
    validate_record(record=journal_record(event="test_result", data=unknown))


@pytest.mark.parametrize(
    "data",
    [
        {"result_scope": "attempt", "test_id": "t", "outcome": "passed"},
        {"result_scope": "aggregate", "test_id": "t", "outcome": "passed"},
        {"result_scope": "neither", "test_id": "t", "outcome": "passed"},
        {"result_scope": "attempt", "test_id": "t", "attempt_index": None, "outcome": "passed", "extra": "field"},
    ],
)
def test_native_result_scope_requirements_are_enforced(data: dict[str, object]) -> None:
    with pytest.raises(ContractError):
        validate_record(record=journal_record(event="test_result", data=data))


def test_collection_outcome_models_availability_separately_from_capture() -> None:
    validate_record(
        record=journal_record(
            event="collection_finished", data={"outcome": "completed", "collected_count": 0, "deselected_count": 0}
        )
    )
    validate_record(record=journal_record(event="collection_finished", data={"outcome": "error"}))
    validate_record(
        record=journal_record(
            event="collection_finished",
            data={"outcome": "interrupted", "collected_count": None, "deselected_count": None},
        )
    )
    with pytest.raises(ContractError):
        validate_record(record=journal_record(event="collection_finished", data={"outcome": "completed"}))


def test_kubernetes_observations_require_cluster_identity() -> None:
    data = dict(EVENT_PAYLOADS["resource_observed"])
    validate_record(record=journal_record(event="resource_observed", data=data))
    del data["resource"]["cluster_uid"]
    del data["resource"]["cluster_role"]
    with pytest.raises(ContractError):
        validate_record(record=journal_record(event="resource_observed", data=data))


def test_observation_allowlist_rejects_unknown_status_fields() -> None:
    data = dict(EVENT_PAYLOADS["resource_observed"])
    data["spec"] = {"replicas": 1}
    with pytest.raises(ContractError):
        validate_record(record=journal_record(event="resource_observed", data=data))
    pod = dict(EVENT_PAYLOADS["resource_observed"])
    pod["containers"] = [{"name": "manager", "logs": "secret"}]
    with pytest.raises(ContractError):
        validate_record(record=journal_record(event="resource_observed", data=pod))


def test_minimal_manifest_is_valid() -> None:
    validate_manifest(manifest=make_manifest())


@pytest.mark.parametrize(
    "keys,value",
    [
        (("schema_version",), 2),
        (("created_at",), "2026-10-27T10:00:00"),
        (("created_at",), "2026-13-40T10:00:00Z"),
        (("execution", "job", "github_job_id"), "unit"),
        (("execution", "run_attempt"), 0),
        (("execution", "event"), "periodic_something"),
        (("execution", "workflow", "path"), ".github/workflows/tests.txt"),
        (("environment", "sources", 0, "revision"), "short"),
        (("suite", "framework"), "made-up-framework"),
    ],
)
def test_invalid_manifest_values_are_rejected(keys: tuple[str | int, ...], value: object) -> None:
    manifest = make_manifest()
    target: Any = manifest
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    with pytest.raises(ContractError):
        validate_manifest(manifest=manifest)


@pytest.mark.parametrize(
    "keys",
    [
        ("completeness", "dropped_records"),
        ("completeness", "end_reason"),
        ("redaction", "findings"),
        ("files", 0, "record_count"),
        ("files", 0, "complete"),
        ("environment", "sources", 0, "revision"),
    ],
)
def test_missing_required_manifest_fields_are_rejected(keys: tuple[str | int, ...]) -> None:
    manifest = make_manifest()
    target: Any = manifest
    for key in keys[:-1]:
        target = target[key]
    del target[keys[-1]]
    with pytest.raises(ContractError):
        validate_manifest(manifest=manifest)


@pytest.mark.parametrize(
    "keys,value",
    [
        (("files", 0, "path"), "../outside.jsonl"),
        (("files", 0, "path"), "/etc/passwd"),
        (("files", 0, "path"), "journals/../../main.jsonl"),
        (("files", 0, "path"), "journals\\main.jsonl"),
        (("files", 0, "kind"), "database"),
        (("files", 0, "sha256"), "deadbeef"),
        (("completeness", "end_reason"), "vanished"),
    ],
)
def test_invalid_manifest_structures_are_rejected(keys: tuple[str | int, ...], value: object) -> None:
    manifest = make_manifest()
    target: Any = manifest
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    with pytest.raises(ContractError):
        validate_manifest(manifest=manifest)


def test_extra_manifest_field_is_rejected() -> None:
    manifest = make_manifest()
    manifest["extra_field"] = "value"
    with pytest.raises(ContractError):
        validate_manifest(manifest=manifest)


@pytest.mark.parametrize(
    "keys,value",
    [
        (("files", 0, "size_bytes"), 0),
        (("files", 0, "record_count"), 0),
        (("completeness", "dropped_records"), 0),
        (("redaction", "findings"), 0),
        (("execution", "job", "github_job_id"), None),
        (("environment", "sources", 0, "revision"), None),
    ],
)
def test_measured_zero_and_unknown_stay_representable(keys: tuple[str | int, ...], value: object) -> None:
    manifest = make_manifest()
    target: Any = manifest
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    validate_manifest(manifest=manifest)


def test_workflow_paths_accept_both_extensions_and_real_triggers() -> None:
    manifest = make_manifest()
    manifest["execution"]["workflow"]["path"] = ".github/workflows/nightly-build.yaml"
    manifest["execution"]["event"] = "workflow_run"
    manifest["execution"]["cohort"] = "nightly"
    validate_manifest(manifest=manifest)
    manifest["execution"]["event"] = "pull_request_target"
    validate_manifest(manifest=manifest)


def test_source_roles_are_distinct_and_required() -> None:
    manifest = make_manifest()
    manifest["environment"]["sources"].append(
        {"role": "installer", "repository": "osac-project/osac", "revision": "1" * 40}
    )
    validate_manifest(manifest=manifest)

    without_tested = [s for s in manifest["environment"]["sources"] if s["role"] != "tested"]
    manifest["environment"]["sources"] = without_tested
    with pytest.raises(ContractError, match="source-role-missing-tested"):
        validate_manifest(manifest=manifest)

    manifest["environment"]["sources"].append(dict(without_tested[0]))
    with pytest.raises(ContractError, match="source-role-duplicate"):
        validate_manifest(manifest=manifest)


def test_suite_selection_and_campaign_are_structured() -> None:
    manifest = make_manifest()
    manifest["suite"]["selection"] = {
        "paths": ["tests/e2e/caas/"],
        "markers": "not requires_netris",
        "keyword": "test_cluster_lifecycle",
        "worker_count": 4,
    }
    manifest["suite"]["campaign"] = {
        "campaign_id": "flake-20261027-caas",
        "group": "caas",
        "repetition": 1,
        "repetitions_total": 3,
    }
    validate_manifest(manifest=manifest)
    # null worker count is unknown, 0 is serial: both representable.
    manifest["suite"]["selection"]["worker_count"] = None
    validate_manifest(manifest=manifest)
    manifest["suite"]["selection"]["worker_count"] = 0
    validate_manifest(manifest=manifest)
    manifest["suite"]["campaign"]["repetition"] = 4
    with pytest.raises(ContractError, match="campaign-repetition-exceeds-total"):
        validate_manifest(manifest=manifest)
    manifest["suite"]["campaign"].pop("campaign_id")
    manifest["suite"]["campaign"]["repetition"] = 1
    with pytest.raises(ContractError):
        validate_manifest(manifest=manifest)


def test_mixed_framework_is_reservable_for_job_level_suites() -> None:
    manifest = make_manifest()
    manifest["suite"]["framework"] = "mixed"
    validate_manifest(manifest=manifest)
    manifest["suite"]["framework"] = "pytest"
    validate_manifest(manifest=manifest)
