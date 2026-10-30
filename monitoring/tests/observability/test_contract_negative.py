"""Negative bundle-level contract tests.

Each case copies a valid example bundle, breaks exactly one thing, and
asserts that the production bundle validator rejects it with the intended
code. The file-access cases prove that rejected files are never read:
a FIFO, a symlinked directory to an outside target, and an oversized
sparse file are all rejected before any parse.
"""

from __future__ import annotations

import json
import os
import traceback
from pathlib import Path

import pytest
from contract_helpers import (
    add_journal_entry,
    copy_bundle,
    load_journal,
    load_json,
    make_record,
    manifest_echo,
    remove_manifest_field,
    replace_journal_with_records,
    rewrite_journal,
    set_manifest_field,
    sha256_file,
)

from monitoring.ci_observability.contracts.validation import ContractError, validate_bundle


def execution_records(scaffold: Path, extra: list[dict[str, object]]) -> list[dict[str, object]]:
    """An execution_started and execution_finished pair around extra records.

    Records are renumbered and stamped with the scaffold's execution id so
    the crafted journal is consistent with the copied manifest.
    """
    manifest = load_json(scaffold / "bundle-manifest.json")
    echo = manifest_echo(scaffold)
    records = [
        make_record("execution_started", echo),
        *extra,
        make_record("execution_finished", {"outcome": "passed", "exit_code": 0}),
    ]
    for index, item in enumerate(records, start=1):
        item["sequence"] = index
        item["execution_id"] = manifest["execution"]["execution_id"]
        item["journal_id"] = "main"
    return records


def renumber(records: list[dict[str, object]]) -> list[dict[str, object]]:
    """Restore contiguous sequences after inserting or removing records."""
    for index, item in enumerate(records, start=1):
        item["sequence"] = index
    return records


def test_unsupported_manifest_schema_version_is_rejected(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    set_manifest_field(bundle, ("schema_version",), 2)
    with pytest.raises(ContractError, match="schema"):
        validate_bundle(bundle_dir=bundle)


def test_unsupported_journal_schema_version_is_rejected(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    records = load_journal(bundle / "journals" / "main.jsonl")
    records[0]["schema_version"] = 2
    rewrite_journal(bundle, "journals/main.jsonl", records)
    with pytest.raises(ContractError, match="schema_version"):
        validate_bundle(bundle_dir=bundle)


def test_unknown_event_type_is_rejected(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    records = load_journal(bundle / "journals" / "main.jsonl")
    records[0]["event"] = "test_exploded"
    rewrite_journal(bundle, "journals/main.jsonl", records)
    with pytest.raises(ContractError, match="must be one of"):
        validate_bundle(bundle_dir=bundle)


@pytest.mark.parametrize("stamp", ["2026-10-27T04:02:11", "2026-10-27T06:02:11+02:00", "2026-99-99T04:02:11Z"])
def test_invalid_journal_timestamps_are_rejected(tmp_path: Path, stamp: str) -> None:
    bundle = copy_bundle("serial", tmp_path)
    records = load_journal(bundle / "journals" / "main.jsonl")
    records[0]["recorded_at"] = stamp
    rewrite_journal(bundle, "journals/main.jsonl", records)
    with pytest.raises(ContractError, match="recorded_at"):
        validate_bundle(bundle_dir=bundle)


def test_impossible_manifest_calendar_date_is_rejected(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    set_manifest_field(bundle, ("created_at",), "2026-02-30T04:19:46Z")
    with pytest.raises(ContractError, match="created_at"):
        validate_bundle(bundle_dir=bundle)


@pytest.mark.parametrize(
    "path", ["../outside.jsonl", "/etc/passwd", "journals/../../main.jsonl", "journals\\main.jsonl"]
)
def test_traversal_file_paths_are_rejected(tmp_path: Path, path: str) -> None:
    bundle = copy_bundle("serial", tmp_path / path.replace("/", "_").replace("\\", "_"))
    set_manifest_field(bundle, ("files", 0, "path"), path)
    with pytest.raises(ContractError) as report:
        validate_bundle(bundle_dir=bundle)
    message = str(report.value)
    assert "properties/path" in message or "path-escapes-bundle" in message or "path-not-canonical" in message


def test_bad_checksum_and_record_count_are_detected(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    journal = bundle / "journals" / "main.jsonl"
    journal.write_bytes(journal.read_bytes() + b'{"schema":"tampered"}\n')
    with pytest.raises(ContractError) as report:
        validate_bundle(bundle_dir=bundle)
    message = str(report.value)
    assert "checksum-mismatch" in message
    assert "record-count-mismatch" in message


def test_malformed_checksum_value_is_rejected(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    set_manifest_field(bundle, ("files", 0, "sha256"), "deadbeef")
    with pytest.raises(ContractError, match="sha256"):
        validate_bundle(bundle_dir=bundle)


def test_cross_execution_reference_is_detected(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    records = load_journal(bundle / "journals" / "main.jsonl")
    records[5]["execution_id"] = "99999999-8888-4777-8666-555555555555"
    rewrite_journal(bundle, "journals/main.jsonl", records)
    with pytest.raises(ContractError, match="cross-execution-reference"):
        validate_bundle(bundle_dir=bundle)


def test_manifest_job_id_must_match_the_journal_echo(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    set_manifest_field(bundle, ("execution", "job", "github_job_id"), 99999)
    with pytest.raises(ContractError, match="identity-echo-job-mismatch"):
        validate_bundle(bundle_dir=bundle)


def test_string_github_job_id_is_rejected(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    set_manifest_field(bundle, ("execution", "job", "github_job_id"), "e2e-bmaas")
    with pytest.raises(ContractError, match="github_job_id"):
        validate_bundle(bundle_dir=bundle)


def test_producer_claimed_inherited_outcome_is_rejected(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    records = load_journal(bundle / "journals" / "main.jsonl")
    finished = next(r for r in records if r["event"] == "execution_finished")
    finished["data"]["outcome"] = "inherited"
    rewrite_journal(bundle, "journals/main.jsonl", records)
    with pytest.raises(ContractError, match="must be one of"):
        validate_bundle(bundle_dir=bundle)


def test_unlisted_file_in_bundle_is_detected(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    (bundle / "journals" / "rogue.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ContractError, match="unlisted-file"):
        validate_bundle(bundle_dir=bundle)


def test_symlink_to_an_internal_file_is_rejected_without_reading_it(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    os.symlink(bundle / "journals" / "main.jsonl", bundle / "journals" / "alias.jsonl")
    with pytest.raises(ContractError, match="symlink-in-bundle"):
        validate_bundle(bundle_dir=bundle)


def test_fifo_is_rejected_as_non_regular_without_reading_it(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    os.mkfifo(bundle / "journals" / "pipe.jsonl")
    # The manifest entry is written by hand: writing through the helper
    # would open the FIFO for writing and block forever, which is exactly
    # the read path this test proves the validator never takes.
    manifest = load_json(bundle / "bundle-manifest.json")
    manifest["files"].append(
        {
            "path": "journals/pipe.jsonl",
            "kind": "journal",
            "sha256": "0" * 64,
            "size_bytes": 0,
            "record_count": 0,
            "complete": True,
        }
    )
    (bundle / "bundle-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ContractError, match="file-not-regular"):
        validate_bundle(bundle_dir=bundle)


def test_symlinked_directory_to_an_outside_target_is_never_followed(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    outside = tmp_path / "outside-target"
    outside.mkdir()
    # A FIFO outside the bundle: following the symlink and reading it would
    # block this test forever, so the specific rejection code proves the
    # target was never opened.
    os.mkfifo(outside / "main.jsonl")
    (bundle / "journals" / "elsewhere").symlink_to(outside)
    set_manifest_field(bundle, ("files", 0, "path"), "journals/elsewhere/main.jsonl")
    with pytest.raises(ContractError, match="path-escapes-bundle"):
        validate_bundle(bundle_dir=bundle)


def test_declared_oversize_is_rejected_before_the_file_is_opened(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    journal = bundle / "journals" / "main.jsonl"
    with open(journal, "wb") as handle:
        handle.seek(40 * 1024 * 1024)
        handle.write(b"{}\n")
    manifest = load_json(bundle / "bundle-manifest.json")
    for entry in manifest["files"]:
        if entry["path"] == "journals/main.jsonl":
            entry["sha256"] = sha256_file(journal)
            entry["size_bytes"] = journal.stat().st_size
            entry["record_count"] = 1
    (bundle / "bundle-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ContractError, match="declared-file-oversize"):
        validate_bundle(bundle_dir=bundle)


def test_grown_file_is_rejected_by_streaming_scan(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    journal = bundle / "journals" / "main.jsonl"
    with open(journal, "wb") as handle:
        handle.seek(40 * 1024 * 1024)
        handle.write(b"{}\n")
    manifest = load_json(bundle / "bundle-manifest.json")
    for entry in manifest["files"]:
        if entry["path"] == "journals/main.jsonl":
            entry["sha256"] = sha256_file(journal)
            entry["size_bytes"] = 1024  # the manifest lies; the scan must catch reality
            entry["record_count"] = 1
    (bundle / "bundle-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ContractError) as report:
        validate_bundle(bundle_dir=bundle)
    message = str(report.value)
    assert "line-oversize" in message or "file-oversize" in message or "size-mismatch" in message


def test_no_journal_is_parsed_when_a_file_check_fails(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    (bundle / "journals" / "main.jsonl").write_text('{"schema":"tampered"}\n', encoding="utf-8")
    add_journal_entry(bundle, "journals/never-parsed.jsonl", "{not json at all\n")
    with pytest.raises(ContractError) as report:
        validate_bundle(bundle_dir=bundle)
    message = str(report.value)
    assert "checksum-mismatch" in message
    assert "malformed-json" not in message


def test_blank_lines_are_rejected_not_dropped(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    records = load_journal(bundle / "journals" / "main.jsonl")
    raw = json.dumps(records[0]) + "\n\n" + json.dumps(records[1]) + "\n"
    add_journal_entry(bundle, "journals/gapped.jsonl", raw)
    with pytest.raises(ContractError, match="blank-line"):
        validate_bundle(bundle_dir=bundle)


def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    add_journal_entry(bundle, "journals/dup-keys.jsonl", '{"a": 1, "a": 2}\n')
    with pytest.raises(ContractError, match="malformed-json"):
        validate_bundle(bundle_dir=bundle)


def test_non_finite_numbers_are_rejected(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    add_journal_entry(bundle, "journals/nan.jsonl", '{"value": NaN}\n')
    with pytest.raises(ContractError, match="malformed-json"):
        validate_bundle(bundle_dir=bundle)


def test_deeply_nested_json_is_rejected_constant_safe(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    add_journal_entry(bundle, "journals/deep.jsonl", "[" * 20000 + "]" * 20000 + "\n")
    with pytest.raises(ContractError, match="malformed-json"):
        validate_bundle(bundle_dir=bundle)


def test_error_messages_never_echo_secrets(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    records = load_journal(bundle / "journals" / "main.jsonl")
    records[0]["data"]["job"]["SECRET_FIELD"] = "SECRETPASSWORD"
    records[1]["data"]["test_id"] = "tests/SECRETDIR/test_secret.py::test_secretSECRET"
    rewrite_journal(bundle, "journals/main.jsonl", records)
    with pytest.raises(ContractError) as report:
        validate_bundle(bundle_dir=bundle)
    chain = "".join(traceback.format_exception(report.value))
    for secret in ("SECRETPASSWORD", "SECRET_FIELD", "SECRETDIR"):
        assert secret not in str(report.value)
        assert secret not in chain


def test_malformed_json_errors_never_echo_content(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    add_journal_entry(bundle, "journals/secret-broken.jsonl", '{"SECRETKEY": "SECRETPASSWORD"\n')
    with pytest.raises(ContractError) as report:
        validate_bundle(bundle_dir=bundle)
    chain = "".join(traceback.format_exception(report.value))
    assert "SECRETPASSWORD" not in str(report.value) and "SECRETPASSWORD" not in chain
    assert "SECRETKEY" not in str(report.value) and "SECRETKEY" not in chain


def test_sequence_gap_is_detected(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    records = load_journal(bundle / "journals" / "main.jsonl")
    del records[5]
    rewrite_journal(bundle, "journals/main.jsonl", records)
    with pytest.raises(ContractError, match="sequence-not-contiguous"):
        validate_bundle(bundle_dir=bundle)


def test_missing_terminal_event_in_complete_bundle_is_detected(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    records = load_journal(bundle / "journals" / "main.jsonl")
    assert records[-1]["event"] == "execution_finished"
    rewrite_journal(bundle, "journals/main.jsonl", records[:-1])
    with pytest.raises(ContractError, match="complete-missing-execution-finished"):
        validate_bundle(bundle_dir=bundle)


def test_dangling_phase_in_complete_bundle_is_detected(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    records = load_journal(bundle / "journals" / "main.jsonl")
    teardowns = [
        index
        for index, record in enumerate(records)
        if record["event"] == "test_phase_finished" and record["data"]["phase"] == "teardown"
    ]
    del records[teardowns[-1]]
    rewrite_journal(bundle, "journals/main.jsonl", records)
    with pytest.raises(ContractError, match="dangling"):
        validate_bundle(bundle_dir=bundle)


def test_incomplete_journal_in_complete_bundle_is_detected(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    set_manifest_field(bundle, ("files", 0, "complete"), False)
    with pytest.raises(ContractError, match="complete-journal-entry-not-complete"):
        validate_bundle(bundle_dir=bundle)


def test_complete_bundle_cannot_hide_drops_or_errors(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    set_manifest_field(bundle, ("completeness", "dropped_records"), 3)
    with pytest.raises(ContractError, match="complete-with-dropped-records"):
        validate_bundle(bundle_dir=bundle)


def test_collection_counts_must_match_test_collected_records(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    records = load_journal(bundle / "journals" / "main.jsonl")
    counts = next(r for r in records if r["event"] == "collection_finished")
    counts["data"]["collected_count"] = 99
    rewrite_journal(bundle, "journals/main.jsonl", records)
    with pytest.raises(ContractError, match="collected-count-mismatch"):
        validate_bundle(bundle_dir=bundle)


def test_executed_test_must_be_collected_as_selected(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    records = load_journal(bundle / "journals" / "main.jsonl")
    executed = next(r for r in records if r["event"] == "test_phase_started")
    for record in records:
        if record["event"] == "test_collected" and record["data"]["test_id"] == executed["data"]["test_id"]:
            record["data"]["deselected"] = True
    rewrite_journal(bundle, "journals/main.jsonl", records)
    with pytest.raises(ContractError, match="executed-not-collected"):
        validate_bundle(bundle_dir=bundle)


def test_fixture_missing_teardown_in_complete_bundle_is_detected(tmp_path: Path) -> None:
    bundle = copy_bundle("parallel", tmp_path)
    records = load_journal(bundle / "journals" / "gw0.jsonl")
    kept = [
        r
        for r in records
        if not (r["event"] in ("fixture_started", "fixture_finished") and r["data"]["phase"] == "teardown")
    ]
    rewrite_journal(bundle, "journals/gw0.jsonl", kept)
    with pytest.raises(ContractError, match="complete-fixture-missing-phase-finish"):
        validate_bundle(bundle_dir=bundle)


def test_fixture_identity_echo_mismatch_is_detected(tmp_path: Path) -> None:
    bundle = copy_bundle("parallel", tmp_path)
    records = load_journal(bundle / "journals" / "gw0.jsonl")
    started = next(r for r in records if r["event"] == "fixture_started")
    started["data"]["scope"] = "module"
    rewrite_journal(bundle, "journals/gw0.jsonl", records)
    with pytest.raises(ContractError, match="fixture-identity-echo-mismatch"):
        validate_bundle(bundle_dir=bundle)


def test_operation_test_link_must_resolve(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    records = load_journal(bundle / "journals" / "main.jsonl")
    started = next(r for r in records if r["event"] == "operation_started")
    started["data"]["test"]["attempt_index"] = 7
    rewrite_journal(bundle, "journals/main.jsonl", records)
    with pytest.raises(ContractError, match="operation-test-link-unknown-attempt"):
        validate_bundle(bundle_dir=bundle)


def test_operation_resource_echo_must_match_the_start(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    records = load_journal(bundle / "journals" / "main.jsonl")
    finished = next(r for r in records if r["event"] == "operation_finished")
    finished["data"]["resource"] = dict(finished["data"]["resource"], uid="another-uid")
    rewrite_journal(bundle, "journals/main.jsonl", records)
    with pytest.raises(ContractError, match="operation-resource-echo-mismatch"):
        validate_bundle(bundle_dir=bundle)


def test_operation_parent_link_must_resolve_within_the_invocation(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    records = load_journal(bundle / "journals" / "main.jsonl")
    started = next(r for r in records if r["event"] == "operation_started")
    started["data"]["parent_operation_id"] = "no-such-operation"
    rewrite_journal(bundle, "journals/main.jsonl", records)
    with pytest.raises(ContractError, match="operation-parent-unknown"):
        validate_bundle(bundle_dir=bundle)


def test_operation_parent_cycle_is_detected(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    records = load_journal(bundle / "journals" / "main.jsonl")
    started = next(r for r in records if r["event"] == "operation_started")
    operation_id = started["data"]["operation_id"]
    started["data"]["parent_operation_id"] = operation_id
    rewrite_journal(bundle, "journals/main.jsonl", records)
    with pytest.raises(ContractError, match="operation-parent-cycle"):
        validate_bundle(bundle_dir=bundle)


def test_fixture_use_edge_must_resolve(tmp_path: Path) -> None:
    bundle = copy_bundle("parallel", tmp_path)
    records = load_journal(bundle / "journals" / "gw0.jsonl")
    used = next(r for r in records if r["event"] == "fixture_used")
    used["data"]["test_id"] = "tests/e2e/vmaas/test_ghost.py::test_ghost"
    rewrite_journal(bundle, "journals/gw0.jsonl", records)
    with pytest.raises(ContractError, match="use-edge-unknown-attempt"):
        validate_bundle(bundle_dir=bundle)


def test_attempt_cannot_be_both_phases_and_native_result(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    records = load_journal(bundle / "journals" / "main.jsonl")
    started = next(r for r in records if r["event"] == "test_phase_started")
    records.append(
        make_record(
            "test_result",
            {
                "result_scope": "attempt",
                "test_id": started["data"]["test_id"],
                "attempt_index": started["data"]["attempt_index"],
                "outcome": "passed",
                "worker_id": None,
            },
        )
    )
    rewrite_journal(bundle, "journals/main.jsonl", records)
    with pytest.raises(ContractError, match="duplicate-result-representation"):
        validate_bundle(bundle_dir=bundle)


def test_aggregate_attempt_count_cannot_be_exceeded_by_attempt_records(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    records = load_journal(bundle / "journals" / "main.jsonl")
    started = next(r for r in records if r["event"] == "test_phase_started")
    records.append(
        make_record(
            "test_result",
            {
                "result_scope": "aggregate",
                "test_id": started["data"]["test_id"],
                "attempt_count": 1,
                "outcome": "passed",
                "worker_id": None,
            },
        )
    )
    records.append(
        make_record(
            "test_result",
            {
                "result_scope": "attempt",
                "test_id": started["data"]["test_id"],
                "attempt_index": 5,
                "outcome": "passed",
                "worker_id": None,
            },
        )
    )
    rewrite_journal(bundle, "journals/main.jsonl", records)
    with pytest.raises(ContractError) as report:
        validate_bundle(bundle_dir=bundle)
    assert "aggregate-attempt-index-exceeded" in str(report.value)


def test_invocation_records_must_have_a_start(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    records = load_journal(bundle / "journals" / "main.jsonl")
    extra = make_record(
        "test_result",
        {
            "result_scope": "aggregate",
            "test_id": "orphan-test",
            "attempt_count": 1,
            "outcome": "passed",
            "worker_id": None,
        },
    )
    extra["invocation_id"] = "never-started-invocation"
    records.append(extra)
    rewrite_journal(bundle, "journals/main.jsonl", records)
    with pytest.raises(ContractError, match="invocation-without-start"):
        validate_bundle(bundle_dir=bundle)


def test_complete_invocation_must_be_finished(tmp_path: Path) -> None:
    bundle = copy_bundle("mixed-aap", tmp_path)
    records = load_journal(bundle / "journals" / "pytest-aap.jsonl")
    kept = [r for r in records if r["event"] != "invocation_finished"]
    rewrite_journal(bundle, "journals/pytest-aap.jsonl", kept)
    with pytest.raises(ContractError, match="complete-invocation-without-finish"):
        validate_bundle(bundle_dir=bundle)


def test_invocation_cannot_declare_the_mixed_framework(tmp_path: Path) -> None:
    bundle = copy_bundle("mixed-aap", tmp_path)
    records = load_journal(bundle / "journals" / "pytest-aap.jsonl")
    started = next(r for r in records if r["event"] == "invocation_started")
    started["data"]["framework"] = "mixed"
    rewrite_journal(bundle, "journals/pytest-aap.jsonl", records)
    with pytest.raises(ContractError, match="invocation-framework-mixed"):
        validate_bundle(bundle_dir=bundle)


def test_mixed_suite_without_invocations_is_rejected(tmp_path: Path) -> None:
    bundle = copy_bundle("mixed-aap", tmp_path)
    records = load_journal(bundle / "journals" / "pytest-aap.jsonl")
    kept = [r for r in records if not r["event"].startswith("invocation_")]
    rewrite_journal(bundle, "journals/pytest-aap.jsonl", kept)
    records = load_journal(bundle / "journals" / "ansible-targets.jsonl")
    kept = [r for r in records if not r["event"].startswith("invocation_")]
    rewrite_journal(bundle, "journals/ansible-targets.jsonl", kept)
    with pytest.raises(ContractError, match="mixed-suite-without-invocations"):
        validate_bundle(bundle_dir=bundle)


def test_clock_id_cannot_change_within_one_journal(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    records = load_journal(bundle / "journals" / "main.jsonl")
    records[4]["clock_id"] = "some-other-process"
    rewrite_journal(bundle, "journals/main.jsonl", records)
    with pytest.raises(ContractError, match="clock-id-changed"):
        validate_bundle(bundle_dir=bundle)


def test_monotonic_clock_cannot_go_backwards(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    records = load_journal(bundle / "journals" / "main.jsonl")
    records[3]["mono_ns"] = 1
    rewrite_journal(bundle, "journals/main.jsonl", records)
    with pytest.raises(ContractError, match="monotonic-backwards"):
        validate_bundle(bundle_dir=bundle)


def test_complete_source_must_have_a_terminal_state(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    records = load_journal(bundle / "journals" / "main.jsonl")
    records.insert(
        -1,
        make_record(
            "collection_state",
            {"source": "cluster-observer", "status": "collecting", "complete": False, "dropped_records": 0},
        ),
    )
    rewrite_journal(bundle, "journals/main.jsonl", records)
    with pytest.raises(ContractError, match="complete-source-without-terminal-state"):
        validate_bundle(bundle_dir=bundle)


def test_missing_required_journal_manifest_field_is_rejected(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    remove_manifest_field(bundle, ("files", 0, "record_count"))
    with pytest.raises(ContractError, match="record_count"):
        validate_bundle(bundle_dir=bundle)


# ---------------------------------------------------------------------------
# The collection model: completed capture is separate from the
# availability of collection evidence.
# ---------------------------------------------------------------------------


def test_measured_empty_collection_completes(tmp_path: Path) -> None:
    scaffold = copy_bundle("serial", tmp_path / "scaffold")
    records = execution_records(
        scaffold,
        [make_record("collection_finished", {"outcome": "completed", "collected_count": 0, "deselected_count": 0})],
    )
    bundle = replace_journal_with_records(tmp_path, "measured-empty", records)
    validate_bundle(bundle_dir=bundle)


def test_collection_error_completes_with_unknown_counts(tmp_path: Path) -> None:
    scaffold = copy_bundle("serial", tmp_path / "scaffold")
    records = execution_records(scaffold, [make_record("collection_finished", {"outcome": "error"})])
    bundle = replace_journal_with_records(tmp_path, "collection-error", records)
    validate_bundle(bundle_dir=bundle)


def test_unknown_native_selection_completes_without_collection_evidence(tmp_path: Path) -> None:
    scaffold = copy_bundle("serial", tmp_path / "scaffold")
    extra = make_record(
        "test_result",
        {
            "result_scope": "aggregate",
            "test_id": "tests/unit/test_junit_only.py::test_from_report",
            "attempt_count": 1,
            "outcome": "passed",
            "worker_id": None,
        },
    )
    records = execution_records(scaffold, [extra])
    bundle = replace_journal_with_records(tmp_path, "unknown-selection", records)
    validate_bundle(bundle_dir=bundle)


def test_selected_but_never_run_is_not_evidence_loss(tmp_path: Path) -> None:
    bundle = copy_bundle("serial", tmp_path)
    records = load_journal(bundle / "journals" / "main.jsonl")
    counts = next(r for r in records if r["event"] == "collection_finished")
    counts["data"]["collected_count"] += 1
    template = next(r for r in records if r["event"] == "test_collected")
    extra = {
        **template,
        "data": {"test_id": "tests/e2e/bmaas/test_selected.py::test_never_reached", "deselected": False},
    }
    records.insert(1, extra)
    renumber(records)
    rewrite_journal(bundle, "journals/main.jsonl", records)
    validate_bundle(bundle_dir=bundle)
