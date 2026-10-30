"""Regressions from independent contract review."""

from __future__ import annotations

import json
import os
import traceback
from collections.abc import Mapping
from pathlib import Path

import pytest
from contract_helpers import (
    add_journal_entry,
    copy_bundle,
    load_journal,
    load_json,
    make_manifest,
    make_record,
    manifest_echo,
    replace_journal_with_records,
)

from monitoring.ci_observability.contracts import validation
from monitoring.ci_observability.contracts.validation import ContractError, validate_bundle


def records_for(*, scaffold: Path, observations: list[dict[str, object]]) -> list[dict[str, object]]:
    manifest = load_json(scaffold / "bundle-manifest.json")
    records = [
        make_record("execution_started", manifest_echo(scaffold)),
        *observations,
        make_record("execution_finished", {"outcome": "passed", "exit_code": 0}),
    ]
    for sequence, record in enumerate(records, 1):
        record["sequence"] = sequence
        record["execution_id"] = manifest["execution"]["execution_id"]
        record["journal_id"] = "main"
        record["mono_ns"] = None
    return records


def test_parse_pass_verifies_the_bytes_it_actually_parses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = copy_bundle("serial", tmp_path)
    journal = bundle / "journals/main.jsonl"
    original = journal.read_bytes()
    prefix, marker, suffix = original.rpartition(b'"outcome": "passed"')
    assert marker
    replacement = prefix + b'"outcome": "failed"' + suffix
    assert len(replacement) == len(original)
    check_directory = validation._check_directory

    def replace_after_checks(directory: Path, manifest: Mapping[str, object]) -> list[str]:
        problems = check_directory(directory, manifest)
        assert not problems
        temporary = journal.with_suffix(".replacement")
        temporary.write_bytes(replacement)
        temporary.replace(journal)
        return problems

    monkeypatch.setattr(validation, "_check_directory", replace_after_checks)
    with pytest.raises(ContractError, match="checksum-mismatch"):
        validate_bundle(bundle_dir=bundle)


def test_directory_errors_never_echo_producer_names(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    secret_name = "SECRETTOKEN-do-not-leak"
    blocked = bundle / secret_name
    blocked.mkdir(mode=0)
    try:
        with pytest.raises(ContractError, match="directory-not-openable") as rejected:
            validate_bundle(bundle_dir=bundle)
        assert secret_name not in str(rejected.value)
        assert secret_name not in "".join(traceback.format_exception(rejected.value))
    finally:
        blocked.chmod(0o700)


def test_schema_diagnostics_do_not_echo_dynamic_object_keys() -> None:
    secret_key = "SECRETTOKEN-private-image-name"
    manifest = make_manifest()
    manifest["environment"]["image_digests"] = {secret_key: "invalid-digest"}
    with pytest.raises(ContractError, match="additionalProperties") as rejected:
        validation.validate_manifest(manifest=manifest)
    assert secret_key not in str(rejected.value)
    assert secret_key not in "".join(traceback.format_exception(rejected.value))


def test_hardlinked_files_are_rejected(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    outside = tmp_path / "outside.jsonl"
    journal = bundle / "journals/main.jsonl"
    os.link(journal, outside)
    with pytest.raises(ContractError, match="file-hardlink"):
        validate_bundle(bundle_dir=bundle)


def test_symlink_loops_have_safe_diagnostics(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    journal = bundle / "journals/main.jsonl"
    journal.unlink()
    journal.symlink_to(journal.name)
    with pytest.raises(ContractError, match="path-escapes-bundle"):
        validate_bundle(bundle_dir=bundle)


def test_declared_bounds_fail_before_opening_journals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = copy_bundle("serial", tmp_path)
    manifest_path = bundle / "bundle-manifest.json"
    manifest = load_json(manifest_path)
    manifest["files"][0]["size_bytes"] = validation.MAX_FILE_BYTES + 1
    manifest_path.write_text(json.dumps(manifest))

    def reject_file_checks(directory: Path, document: Mapping[str, object]) -> list[str]:
        pytest.fail("declared bounds must fail before file checks")

    monkeypatch.setattr(validation, "_check_listed_files", reject_file_checks)
    with pytest.raises(ContractError, match="declared-file-oversize"):
        validate_bundle(bundle_dir=bundle)


@pytest.mark.parametrize("collection_outcome", [None, "error", "unknown", "interrupted"])
def test_attempt_results_do_not_invent_collection_evidence(tmp_path: Path, collection_outcome: str | None) -> None:
    scaffold = copy_bundle("serial", tmp_path / "scaffold")
    observations = []
    if collection_outcome is not None:
        observations.append(make_record("collection_finished", {"outcome": collection_outcome}))
    observations.append(
        make_record(
            "test_result",
            {"result_scope": "attempt", "test_id": "native::test", "attempt_index": None, "outcome": "passed"},
        )
    )
    records = records_for(scaffold=scaffold, observations=observations)
    bundle = replace_journal_with_records(tmp_path, "unknown-collection", records)
    validate_bundle(bundle_dir=bundle)


def test_unknown_attempt_index_can_coexist_with_a_known_aggregate(tmp_path: Path) -> None:
    scaffold = copy_bundle("serial", tmp_path / "scaffold")
    observations = [
        make_record(
            "test_result",
            {"result_scope": "attempt", "test_id": "native::test", "attempt_index": None, "outcome": "passed"},
        ),
        make_record(
            "test_result",
            {"result_scope": "aggregate", "test_id": "native::test", "attempt_count": 1, "outcome": "passed"},
        ),
    ]
    bundle = replace_journal_with_records(
        tmp_path, "unknown-index", records_for(scaffold=scaffold, observations=observations)
    )
    validate_bundle(bundle_dir=bundle)


def test_native_zero_attempt_count_is_measured_zero(tmp_path: Path) -> None:
    scaffold = copy_bundle("serial", tmp_path / "scaffold")
    observation = make_record(
        "test_result",
        {"result_scope": "aggregate", "test_id": "native::filtered", "attempt_count": 0, "outcome": "skipped"},
    )
    bundle = replace_journal_with_records(
        tmp_path, "zero-native-attempts", records_for(scaffold=scaffold, observations=[observation])
    )
    validate_bundle(bundle_dir=bundle)


def test_duplicate_execution_finishes_are_rejected(tmp_path: Path) -> None:
    scaffold = copy_bundle("serial", tmp_path / "scaffold")
    observation = make_record("execution_finished", {"outcome": "failed", "exit_code": 1})
    bundle = replace_journal_with_records(
        tmp_path, "duplicate-finish", records_for(scaffold=scaffold, observations=[observation])
    )
    with pytest.raises(ContractError, match="duplicate-execution-finish"):
        validate_bundle(bundle_dir=bundle)


def invocation_records(*, invocation_id: str, worker_id: str) -> list[dict[str, object]]:
    fixture = {
        "fixture_instance_id": "shared-name",
        "fixture_id": "local.fixture",
        "scope": "session",
        "worker_id": worker_id,
    }
    attempt = {"test_id": "local::test", "attempt_index": 1, "worker_id": worker_id}
    payloads = [
        (
            "invocation_started",
            {
                "invocation_id": invocation_id,
                "framework": "pytest",
                "tier": "unit",
                "component": "aap",
                "variant": "unit",
            },
        ),
        ("worker_started", {"worker_id": worker_id}),
        ("fixture_started", {**fixture, "phase": "setup"}),
        ("fixture_finished", {**fixture, "phase": "setup", "outcome": "passed"}),
        ("test_phase_started", {**attempt, "phase": "call"}),
        ("fixture_used", {**attempt, "fixture_instance_id": "shared-name"}),
        ("test_phase_finished", {**attempt, "phase": "call", "outcome": "passed"}),
        ("fixture_started", {**fixture, "phase": "teardown"}),
        ("fixture_finished", {**fixture, "phase": "teardown", "outcome": "passed"}),
        ("worker_stopped", {"worker_id": worker_id, "outcome": "completed"}),
        ("invocation_finished", {"invocation_id": invocation_id, "outcome": "passed", "exit_code": 0}),
    ]
    return [make_record(event, data, invocation_id=invocation_id) for event, data in payloads]


def test_fixture_identity_and_lifetime_are_scoped_to_the_invocation(tmp_path: Path) -> None:
    scaffold = copy_bundle("serial", tmp_path / "scaffold")
    observations = invocation_records(invocation_id="first", worker_id="gw-first")
    observations += invocation_records(invocation_id="second", worker_id="gw-second")
    bundle = replace_journal_with_records(
        tmp_path, "scoped-fixtures", records_for(scaffold=scaffold, observations=observations)
    )
    validate_bundle(bundle_dir=bundle)


def test_another_invocation_cannot_supply_a_missing_fixture_setup(tmp_path: Path) -> None:
    scaffold = copy_bundle("serial", tmp_path / "scaffold")
    first = invocation_records(invocation_id="first", worker_id="gw-first")
    second = invocation_records(invocation_id="second", worker_id="gw-second")
    second = [
        record
        for record in second
        if not (record["event"] in ("fixture_started", "fixture_finished") and record["data"]["phase"] == "setup")
    ]
    bundle = replace_journal_with_records(
        tmp_path, "missing-fixture-setup", records_for(scaffold=scaffold, observations=first + second)
    )
    with pytest.raises(ContractError, match="use-edge-without-fixture-setup"):
        validate_bundle(bundle_dir=bundle)


def test_worker_stop_in_another_invocation_cannot_close_a_lost_worker(tmp_path: Path) -> None:
    scaffold = copy_bundle("serial", tmp_path / "scaffold")
    first = invocation_records(invocation_id="first", worker_id="main")
    first = [record for record in first if record["event"] != "worker_stopped"]
    second = invocation_records(invocation_id="second", worker_id="main")
    bundle = replace_journal_with_records(
        tmp_path, "missing-worker-stop", records_for(scaffold=scaffold, observations=first + second)
    )
    with pytest.raises(ContractError, match="complete-worker-without-stop"):
        validate_bundle(bundle_dir=bundle)


def test_unavailable_source_is_an_explicit_terminal_state(tmp_path: Path) -> None:
    scaffold = copy_bundle("serial", tmp_path / "scaffold")
    observation = make_record(
        "collection_state",
        {"source": "cluster-observer", "status": "unavailable", "complete": False, "dropped_records": 0},
    )
    bundle = replace_journal_with_records(
        tmp_path, "unavailable-source", records_for(scaffold=scaffold, observations=[observation])
    )
    validate_bundle(bundle_dir=bundle)


def test_a_heartbeat_after_a_terminal_state_is_not_complete(tmp_path: Path) -> None:
    scaffold = copy_bundle("serial", tmp_path / "scaffold")
    observations = [
        make_record(
            "collection_state", {"source": "observer", "status": "stopped", "complete": True, "dropped_records": 0}
        ),
        make_record(
            "collection_state", {"source": "observer", "status": "collecting", "complete": False, "dropped_records": 0}
        ),
    ]
    bundle = replace_journal_with_records(
        tmp_path, "reopened-source", records_for(scaffold=scaffold, observations=observations)
    )
    with pytest.raises(ContractError, match="complete-source-without-terminal-state"):
        validate_bundle(bundle_dir=bundle)


def test_duplicate_test_phases_in_different_journals_are_rejected(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    records = load_journal(bundle / "journals/main.jsonl")
    start = next(
        record for record in records if record["event"] == "test_phase_started" and record["data"]["phase"] == "call"
    )
    finish = next(
        record
        for record in records
        if record["event"] == "test_phase_finished"
        and record["data"]["phase"] == "call"
        and record["data"]["test_id"] == start["data"]["test_id"]
    )
    duplicate = [dict(record, sequence=index, journal_id="other") for index, record in enumerate((start, finish), 1)]
    add_journal_entry(bundle, "journals/other.jsonl", "".join(json.dumps(record) + "\n" for record in duplicate))
    with pytest.raises(ContractError, match="duplicate-test-phase"):
        validate_bundle(bundle_dir=bundle)


def test_invocation_finish_without_a_start_is_rejected(tmp_path: Path) -> None:
    scaffold = copy_bundle("serial", tmp_path / "scaffold")
    observation = make_record("invocation_finished", {"invocation_id": "orphan", "outcome": "passed", "exit_code": 0})
    bundle = replace_journal_with_records(
        tmp_path, "orphan-finish", records_for(scaffold=scaffold, observations=[observation])
    )
    with pytest.raises(ContractError, match="invocation-finish-without-start"):
        validate_bundle(bundle_dir=bundle)


@pytest.mark.parametrize("phase", ["setup", "teardown"])
@pytest.mark.parametrize("outcome", ["skipped", "expected_failure"])
def test_fixture_phase_preserves_native_skip_and_xfail(phase: str, outcome: str) -> None:
    record = make_record(
        "fixture_finished",
        {
            "fixture_instance_id": "fixture-1",
            "fixture_id": "local.fixture",
            "scope": "function",
            "worker_id": "main",
            "phase": phase,
            "outcome": outcome,
        },
    )
    validation.validate_record(record=record)


def test_native_fixture_provenance_is_present() -> None:
    provenance = validation.CONTRACTS_V1_DIR / "examples/native/README.md"
    assert provenance.is_file()
    text = provenance.read_text()
    for framework in ("pytest", "Ginkgo", "Go", "Vitest 4.1.9"):
        assert framework in text


class CountedOperations(dict[tuple[str, str], tuple[int, object, object]]):
    lookups = 0

    def __getitem__(self, key: tuple[str, str]) -> tuple[int, object, object]:
        self.lookups += 1
        return super().__getitem__(key)


def test_parent_validation_does_not_rescan_a_long_dependency_chain() -> None:
    operations = CountedOperations()
    for index in range(2000):
        operations[("invocation", str(index))] = (index, None, str(index - 1) if index else None)
    state = validation._BundleState()
    state.operation_starts = operations
    problems: list[str] = []

    validation._check_operation_parents(state, problems)

    assert problems == []
    assert operations.lookups <= 3 * len(operations)


def test_directory_and_record_caps_are_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = copy_bundle("serial", tmp_path)
    monkeypatch.setattr(validation, "MAX_TOTAL_RECORDS", 2)
    with pytest.raises(ContractError, match="total-record-limit"):
        validate_bundle(bundle_dir=bundle)
    monkeypatch.setattr(validation, "MAX_TOTAL_RECORDS", 2_000_000)
    monkeypatch.setattr(validation, "MAX_DIRECTORY_ENTRIES", 2)
    with pytest.raises(ContractError, match="directory-entries-oversize"):
        validate_bundle(bundle_dir=bundle)


def test_directory_depth_limit_is_enforced(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    deepest = bundle
    for _ in range(validation.MAX_DIRECTORY_DEPTH + 1):
        deepest /= "nested"
        deepest.mkdir()
    with pytest.raises(ContractError, match="directory-depth-exceeded"):
        validate_bundle(bundle_dir=bundle)
