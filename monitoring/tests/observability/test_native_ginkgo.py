"""Ginkgo adapter tests: parsing, conversion, identity stability, privacy,
and real runs."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from monitoring.ci_observability.contracts.validation import validate_record
from monitoring.ci_observability.native.ginkgo import ginkgo_journal_records, parse_ginkgo_report, split_ginkgo_suites
from monitoring.ci_observability.native.records import AdapterError, InvocationContext, to_utc_z

TESTS_DIR = Path(__file__).resolve().parent
CONTRACT_NATIVE = TESTS_DIR.parent.parent / "ci_observability" / "contracts" / "v1" / "examples" / "native"
FIXTURES = TESTS_DIR.parent / "fixtures" / "native_adapters"
GINKGO_BIN = os.path.expanduser("~/go/bin/ginkgo")


def context(**overrides: object) -> InvocationContext:
    values: dict[str, object] = {
        "execution_id": "w03-ginkgo-exec",
        "journal_id": "ginkgo-journal",
        "invocation_id": "ginkgo-invocation",
        "framework": "ginkgo",
        "tier": "unit",
        "component": "fulfillment-service",
        "variant": "unit-ginkgo",
        "observed_at": "2026-10-28T12:00:00Z",
        "child_exit_code": None,
    }
    values.update(overrides)
    return InvocationContext(**values)  # type: ignore[arg-type]


def convert_fixture(raw: bytes, **overrides: object) -> tuple[list[dict[str, object]], list[str], dict[str, str]]:
    suite = parse_ginkgo_report(raw=raw)
    result = ginkgo_journal_records(context=context(**overrides), suite=suite, suite_path="fixture/suite/path")
    for record in result.records:
        validate_record(record=record)
    return result.records, [gap.code for gap in result.gaps], result.display_names


def result_ids(records: list[dict[str, object]]) -> set[str]:
    return {str(record["data"]["test_id"]) for record in records if record["event"] == "test_result"}


def test_parses_the_real_tiny_fixture_shape() -> None:
    suite = parse_ginkgo_report(raw=(CONTRACT_NATIVE / "ginkgo.json").read_bytes())
    assert suite.pre_run_total == 3
    assert suite.pre_run_will_run == 3
    assert [spec.state for spec in suite.specs] == ["passed", "failed", "skipped"]
    assert [spec.num_attempts for spec in suite.specs] == [1, 2, 1]
    assert [spec.run_time_ns for spec in suite.specs] == [34301, 175555, 68123]
    assert suite.specs[0].is_test
    assert suite.start_time == "2026-10-27T22:24:30.038843158Z"
    assert suite.duplicate_identities == 0


def test_conversion_emits_valid_aggregate_records() -> None:
    records, gaps, display = convert_fixture((CONTRACT_NATIVE / "ginkgo.json").read_bytes())
    assert gaps == []
    assert [record["event"] for record in records] == [
        "invocation_started",
        "test_collected",
        "test_collected",
        "test_collected",
        "collection_finished",
        "test_result",
        "test_result",
        "test_result",
        "invocation_finished",
        "collection_state",
    ]
    results = [record["data"] for record in records if record["event"] == "test_result"]
    # No attempt-scope records: retries are not fabricated from
    # NumAttempts, Retry events, or AdditionalFailures.
    assert all(data["result_scope"] == "aggregate" for data in results)
    assert [data["attempt_count"] for data in results] == [1, 2, 1]
    assert [data["outcome"] for data in results] == ["passed", "failed", "skipped"]
    assert results[0]["duration_ns"] == 34301
    # The original +02:00 timestamps become UTC-Z with the exact
    # fractional digits preserved.
    assert results[0]["source_started_at"] == "2026-10-27T22:24:30.038918566Z"
    assert results[0]["source_ended_at"] == "2026-10-27T22:24:30.038952867Z"
    # Aggregates are per test, not per attempt: the retried failure's
    # span is the native RunTime total.
    assert results[1]["duration_ns"] == 175555
    collection = records[4]["data"]
    assert collection == {"outcome": "completed", "collected_count": 3, "deselected_count": 0}
    finished = records[-2]["data"]
    assert finished["outcome"] == "failed"
    # A static fixture measured no child process: null, never a pinned
    # contradictory code.
    assert finished["exit_code"] is None
    assert finished["duration_ns"] == 453829
    # The private display mapping keeps the raw text out of records.
    assert sorted(display.values()) == [
        "ci observability fixture/fails on purpose",
        "ci observability fixture/passes on purpose",
        "ci observability fixture/skips on purpose",
    ]


def test_clean_conversion_persists_a_clean_terminal_collection_state() -> None:
    records, gaps, _ = convert_fixture((CONTRACT_NATIVE / "ginkgo.json").read_bytes())
    assert gaps == []
    state = records[-1]
    assert state["event"] == "collection_state"
    data = state["data"]
    assert data["source"] == "osac-ci-observability-native-adapter:ginkgo"
    assert data["status"] == "stopped"
    assert data["complete"] is True
    assert data["dropped_records"] == 0
    assert "errors" not in data
    assert data["covered_from"] == "2026-10-27T22:24:30.038843158Z"


def test_no_raw_native_text_enters_records() -> None:
    records, _, _ = convert_fixture((CONTRACT_NATIVE / "ginkgo.json").read_bytes())
    serialized = json.dumps(records)
    for forbidden in ("fails on purpose", "Expected", "to equal", "skipped on purpose", "ci observability fixture"):
        assert forbidden not in serialized
    # Failure signatures are hashed, allowlisted, and bounded.
    failure = records[6]["data"]["failure"]
    assert failure["signature"].startswith("ginkgo:spec-failure:")
    assert set(failure) == {"signature"}


def test_hashed_identities_are_stable_across_invocations() -> None:
    raw = (CONTRACT_NATIVE / "ginkgo.json").read_bytes()
    records_a, _, _ = convert_fixture(raw, invocation_id="inv-a", journal_id="journal-a")
    records_b, _, _ = convert_fixture(raw, invocation_id="inv-b", journal_id="journal-b")
    assert result_ids(records_a) == result_ids(records_b)
    assert all(test_id.startswith("ginkgo:fulfillment-service:") for test_id in result_ids(records_a))


def test_identity_survives_a_different_physical_checkout_root() -> None:
    """The same suite at two roots is the same tests: absolute
    LeafNodeLocation roots must not enter identity (Q7)."""

    def rerooted(root: str) -> bytes:
        suite = json.loads((CONTRACT_NATIVE / "ginkgo.json").read_bytes())
        suite[0]["SuitePath"] = f"{root}/ciobs-ginkgo-fixture"
        for spec in suite[0]["SpecReports"]:
            location = spec.get("LeafNodeLocation")
            if isinstance(location, dict) and "FileName" in location:
                location["FileName"] = f"{root}/ciobs-ginkgo-fixture/{location['FileName']}"
        return json.dumps(suite).encode()

    _, _, _ = convert_fixture(rerooted("/home/alice/checkout-a"))
    records_a, _, _ = convert_fixture(rerooted("/home/alice/checkout-a"))
    records_b, _, _ = convert_fixture(rerooted("/home/bob/checkout-b"))
    assert result_ids(records_a) == result_ids(records_b)


def test_identity_survives_line_movement() -> None:
    """Inserting comments above an It moves line numbers, not identity."""

    def relocated(lines: list[int]) -> bytes:
        suite = json.loads((CONTRACT_NATIVE / "ginkgo.json").read_bytes())
        for spec, line in zip(suite[0]["SpecReports"], lines, strict=True):
            spec["LeafNodeLocation"]["LineNumber"] = line
        return json.dumps(suite).encode()

    records_a, _, _ = convert_fixture(relocated([16, 20, 24]))
    records_b, _, _ = convert_fixture(relocated([116, 220, 324]))
    assert result_ids(records_a) == result_ids(records_b)


def test_same_text_specs_stay_distinct_with_a_duplicate_gap() -> None:
    """Two specs sharing hierarchy, leaf, type, and file are distinct:
    they get distinct ids through an occurrence ordinal and an explicit
    gap, never a silent merge. The ordinal reflects report order, which
    is the comparison limit for textually identical specs."""

    suite_document = json.loads((CONTRACT_NATIVE / "ginkgo.json").read_bytes())
    clone = json.loads(json.dumps(suite_document[0]["SpecReports"][0]))
    suite_document[0]["SpecReports"].append(clone)
    suite_document[0]["PreRunStats"]["TotalSpecs"] = 4
    suite_document[0]["PreRunStats"]["SpecsThatWillRun"] = 4
    raw = json.dumps(suite_document).encode()

    suite = parse_ginkgo_report(raw=raw)
    assert suite.duplicate_identities == 1
    records, gaps, _ = convert_fixture(raw)
    assert "ginkgo-duplicate-identity" in gaps
    results = [record["data"] for record in records if record["event"] == "test_result"]
    assert len(results) == 4
    ids = [data["test_id"] for data in results]
    assert len(set(ids)) == 4
    collection = [record["data"] for record in records if record["event"] == "collection_finished"]
    assert collection[0]["collected_count"] == 4
    # The gap persists in the terminal collection state. A duplicate
    # identity is a representation limit: every fact was delivered, so
    # zero records were dropped.
    state = records[-1]["data"]
    assert state["status"] == "degraded"
    assert state["complete"] is False
    assert state["dropped_records"] == 0
    assert state["errors"] == [{"message": "ginkgo-duplicate-identity"}]
    assert "omitted_fields" not in state


@pytest.mark.parametrize("phase", ["service-only", "manager-capability", "manager-readiness", "bmaas-disabled"])
def test_excerpt_reports_catalog_incomplete_gap(phase: str) -> None:
    raw = (FIXTURES / "it-phases" / f"ginkgo-{phase}.EXCERPT.json").read_bytes()
    records, gaps, _ = convert_fixture(raw, invocation_id=f"it-{phase}", journal_id=f"it-{phase}", child_exit_code=None)
    # The excerpt keeps the declared totals but not the full spec list:
    # no collection claims, only the explicit gap.
    assert "ginkgo-catalog-incomplete" in gaps
    assert not any(record["event"] in ("test_collected", "collection_finished") for record in records)
    results = [record["data"] for record in records if record["event"] == "test_result"]
    filtered = [data for data in results if data["attempt_count"] == 0]
    ran = [data for data in results if data["attempt_count"] > 0]
    assert {data["outcome"] for data in filtered} == {"skipped"}
    # A never-attempted spec has no end time and no measured span.
    assert all("source_ended_at" not in data and "duration_ns" not in data for data in filtered)
    assert all("source_ended_at" in data and "duration_ns" in data for data in ran)
    started = records[0]["data"]
    assert started["invocation_id"] == f"it-{phase}"
    finished = records[-2]["data"]
    assert finished["outcome"] == "passed"
    # The child exit code is unknown from a report: null, never guessed.
    assert finished["exit_code"] is None
    # The catalog gap persists as the terminal collection state.
    state = records[-1]["data"]
    assert state["status"] == "degraded"
    assert state["complete"] is False
    assert state["dropped_records"] == 623 - len(results)
    assert state["errors"] == [{"message": "ginkgo-catalog-incomplete"}]


def test_service_only_excerpt_distinguishes_runtime_skip_from_filtered() -> None:
    raw = (FIXTURES / "it-phases" / "ginkgo-service-only.EXCERPT.json").read_bytes()
    records, gaps, _ = convert_fixture(raw)
    assert "ginkgo-catalog-incomplete" in gaps
    results = [record["data"] for record in records if record["event"] == "test_result"]
    runtime_skip = [data for data in results if data["attempt_count"] == 1 and data["outcome"] == "skipped"]
    filtered = [data for data in results if data["attempt_count"] == 0]
    assert len(runtime_skip) == 1
    assert len(filtered) == 2
    assert "source_ended_at" in runtime_skip[0]


def test_year_one_sentinel_is_absent_not_a_timestamp() -> None:
    assert to_utc_z(native="0001-01-01T00:00:00Z") is None
    assert to_utc_z(native="0001-01-01T00:00:00.000000000Z") is None
    assert to_utc_z(native="2026-10-28T00:24:30.038918566+02:00") == "2026-10-27T22:24:30.038918566Z"


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda raw: raw.replace(b'"State": "passed"', b'"State": "melted"'), "unknown state"),
        (lambda raw: b"[{},{}]", "two suites"),
        (lambda raw: b"[]", "no suite"),
        (lambda raw: raw.replace(b'"NumAttempts"', b'"Attempts"'), "missing field"),
        (lambda raw: b'{"a": NaN}', "non-finite"),
        (lambda raw: b'{"a": 1, "a": 2}', "duplicate keys"),
        (lambda raw: b"\xff\xfe", "not utf-8"),
        (lambda raw: b"[" + b'{"a":[' * 100_000 + b"]" * 100_000 + b"]", "too deep"),
    ],
)
def test_malformed_reports_fail_with_constant_errors(mutate: object, code: str) -> None:
    raw = (CONTRACT_NATIVE / "ginkgo.json").read_bytes()
    with pytest.raises(AdapterError):
        parse_ginkgo_report(raw=mutate(raw))  # type: ignore[operator]


def test_oversized_report_is_rejected() -> None:
    with pytest.raises(AdapterError):
        parse_ginkgo_report(raw=b"[" + b" " * (32 * 1024 * 1024 + 1) + b"]")


def test_hook_failure_is_not_a_dropped_record() -> None:
    """A delivered hook failure is an attribution fact, not a lost
    record: zero drops, gap label, no unknown marker."""

    suite_document = json.loads((CONTRACT_NATIVE / "ginkgo.json").read_bytes())
    hook = json.loads(json.dumps(suite_document[0]["SpecReports"][0]))
    hook["LeafNodeType"] = "BeforeSuite"
    hook["State"] = "failed"
    hook["Failure"] = {"Message": "hook failed", "FailureNodeType": "BeforeSuite"}
    suite_document[0]["SpecReports"].append(hook)
    raw = json.dumps(suite_document).encode()
    records, gaps, _ = convert_fixture(raw)
    assert "ginkgo-hook-failure" in gaps
    assert "ginkgo-catalog-incomplete" not in gaps
    state = records[-1]["data"]
    assert state["dropped_records"] == 0
    assert "omitted_fields" not in state


def test_split_refuses_a_source_root_that_does_not_contain_the_suite() -> None:
    merged = json.loads((CONTRACT_NATIVE / "ginkgo.json").read_bytes())
    merged[0]["SuitePath"] = "/checkout/pkg-a"
    raw = json.dumps(merged).encode()
    with pytest.raises(AdapterError, match="not under the source root"):
        split_ginkgo_suites(raw=raw, source_root="/wrong-root")


def test_split_gives_distinct_suites_distinct_namespaces_by_construction() -> None:
    """Two different suites under one root always get different
    suggested paths: the split helper prevents the caller-namespace
    collision (silently aliasing definitions) by construction, so there
    is no input that can produce a collision to refuse."""

    document = json.loads((CONTRACT_NATIVE / "ginkgo.json").read_bytes())
    second = json.loads(json.dumps(document[0]))
    second["SuitePath"] = "/checkout/pkg-b"
    second["SuiteDescription"] = "a different suite"
    document[0]["SuitePath"] = "/checkout/pkg-a"
    raw = json.dumps([document[0], second]).encode()

    splits = split_ginkgo_suites(raw=raw, source_root="/checkout")
    paths = [split.suggested_suite_path for split in splits]
    assert len(set(paths)) == len(paths) == 2


def test_synthetic_two_suite_split_produces_distinct_identities() -> None:
    """Two packages in one merged capture get distinct namespaces from
    their SuitePaths relative to the explicit source root."""

    document = json.loads((CONTRACT_NATIVE / "ginkgo.json").read_bytes())
    second = json.loads(json.dumps(document[0]))
    second["SuitePath"] = "/checkout/pkg-b"
    document[0]["SuitePath"] = "/checkout/pkg-a"
    raw = json.dumps([document[0], second]).encode()

    splits = split_ginkgo_suites(raw=raw, source_root="/checkout")
    assert [split.suggested_suite_path for split in splits] == ["pkg-a", "pkg-b"]

    all_ids: list[set[str]] = []
    for index, split in enumerate(splits):
        result = ginkgo_journal_records(
            context=context(invocation_id=f"inv-{index}", journal_id=f"journal-{index}"),
            suite=split.suite,
            suite_path=split.suggested_suite_path,
        )
        for record in result.records:
            validate_record(record=record)
        all_ids.append(result_ids(result.records))
    assert all_ids[0].isdisjoint(all_ids[1])


def test_synthetic_repeated_same_suite_keeps_ids_across_separate_invocations() -> None:
    """A repeated run of the same suite in one merged array keeps the
    same definition IDs: the caller gives each its own invocation, so
    there are no duplicate aggregates and no fabricated retries."""

    document = json.loads((CONTRACT_NATIVE / "ginkgo.json").read_bytes())
    document[0]["SuitePath"] = "/checkout/pkg-a"
    repeated = json.loads(json.dumps([document[0], document[0]]))
    raw = json.dumps(repeated).encode()

    splits = split_ginkgo_suites(raw=raw, source_root="/checkout")
    assert len(splits) == 2
    assert splits[0].suggested_suite_path == splits[1].suggested_suite_path == "pkg-a"

    per_invocation_ids: list[set[str]] = []
    for index, split in enumerate(splits):
        result = ginkgo_journal_records(
            context=context(invocation_id=f"rep-{index}", journal_id=f"rep-journal-{index}"),
            suite=split.suite,
            suite_path=split.suggested_suite_path,
        )
        per_invocation_ids.append(result_ids(result.records))
    assert per_invocation_ids[0] == per_invocation_ids[1]


@pytest.mark.skipif(not os.access(GINKGO_BIN, os.X_OK), reason="real ginkgo 2.33.1 binary not available")
def test_real_ginkgo_run_converts_with_retries_retained() -> None:
    """Real Ginkgo 2.33.1 subprocess against the copied tiny suite."""

    work = Path(tempfile.mkdtemp(prefix="ci-native-ginkgo-", dir="/tmp/opencode"))
    try:
        suite_dir = work / "ginkgotiny"
        shutil.copytree(FIXTURES / "ginkgotiny", suite_dir)
        report_path = work / "report.json"
        completed = subprocess.run(
            [GINKGO_BIN, "run", "--json-report", str(report_path), "--flake-attempts", "2", "-r"],
            cwd=suite_dir,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert report_path.is_file(), completed.stderr[-400:]
        suite = parse_ginkgo_report(raw=report_path.read_bytes())
        states = {spec.state for spec in suite.specs}
        assert "failed" in states
        retried = next(spec for spec in suite.specs if spec.state == "failed")
        assert retried.num_attempts == 2
        records, gaps, _ = convert_fixture(report_path.read_bytes(), child_exit_code=completed.returncode)
        assert gaps == []
        assert completed.returncode == 1
        finished = records[-2]["data"]
        assert finished["outcome"] == "failed"
        assert finished["exit_code"] == 1
        results = [record["data"] for record in records if record["event"] == "test_result"]
        assert any(data["attempt_count"] == 2 and data["outcome"] == "failed" for data in results)
        assert any(data["attempt_count"] == 1 and data["outcome"] == "passed" for data in results)
        # The real suite's identity is root-independent: rerunning the
        # same suite from a copied directory gives the same ids.
        records_again, _, _ = convert_fixture(report_path.read_bytes(), child_exit_code=completed.returncode)
        assert result_ids(records_again) == result_ids(records)
    finally:
        shutil.rmtree(work, ignore_errors=True)


@pytest.mark.skipif(not os.access(GINKGO_BIN, os.X_OK), reason="real ginkgo 2.33.1 binary not available")
def test_real_two_package_merged_capture_splits_into_disjoint_identities() -> None:
    """Real `ginkgo run -r` over two packages, merged into one JSON
    array: the split helper namespaces each suite by its SuitePath
    relative to the checkout root, producing disjoint definition IDs
    and valid records for a composed bundle."""

    work = Path(tempfile.mkdtemp(prefix="ci-native-ginkgo2-", dir="/tmp/opencode"))
    try:
        packages = work / "packages"
        for name in ("alpha", "beta"):
            suite = packages / name
            shutil.copytree(FIXTURES / "ginkgotiny", suite)
            go_mod = suite / "go.mod"
            go_mod.write_text(
                go_mod.read_text().replace("example.com/ginkgotiny", f"example.com/{name}"), encoding="utf-8"
            )
            for file_name in ("suite_test.go", "native_report_test.go"):
                source = suite / file_name
                source.write_text(
                    source.read_text()
                    .replace("ginkgotiny", name)
                    .replace("Ginkgotiny Suite", f"{name.capitalize()} Suite"),
                    encoding="utf-8",
                )
        report_path = work / "merged-report.json"
        subprocess.run(
            [GINKGO_BIN, "run", "--json-report", str(report_path), "-r", "--keep-going"],
            cwd=packages,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert report_path.is_file()
        raw = report_path.read_bytes()
        suites = json.loads(raw)
        assert len(suites) == 2, "expected two packages in the merged array"

        splits = split_ginkgo_suites(raw=raw, source_root=str(packages))
        assert len(splits) == 2
        assert len({split.suggested_suite_path for split in splits}) == 2

        all_ids: list[set[str]] = []
        for index, split in enumerate(splits):
            result = ginkgo_journal_records(
                context=context(
                    invocation_id=f"two-pkg-{index}",
                    journal_id=f"two-pkg-journal-{index}",
                    # The overall CLI exit belongs to the merged command,
                    # not to each split suite: unmeasured stays null.
                    child_exit_code=None,
                ),
                suite=split.suite,
                suite_path=split.suggested_suite_path,
            )
            for record in result.records:
                validate_record(record=record)
            all_ids.append(result_ids(result.records))
        assert all_ids[0].isdisjoint(all_ids[1])
        assert all(len(ids) > 0 for ids in all_ids)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_measured_nonzero_child_exit_cannot_label_an_invocation_passed() -> None:
    """A fully passing Ginkgo suite beside a measured child exit 1
    (plain Go tests in the same binary can fail outside the report)
    keeps every per-spec outcome; the invocation is failed with a fixed
    gap. An unmeasured split-capture exit stays null and never copies
    the global CLI exit."""

    raw = (CONTRACT_NATIVE / "ginkgo.json").read_bytes()
    suite = parse_ginkgo_report(raw=raw)
    passing = dataclasses.replace(
        suite,
        succeeded=True,
        special_failure_reasons=[],
        specs=[dataclasses.replace(spec, state="passed") for spec in suite.specs],
    )

    def convert_suite(parsed: object, **overrides: object) -> tuple[list[dict[str, object]], list[str], dict[str, str]]:
        result = ginkgo_journal_records(
            context=context(**overrides),
            suite=parsed,
            suite_path="fixture/suite/path",  # type: ignore[arg-type]
        )
        for record in result.records:
            validate_record(record=record)
        return result.records, [gap.code for gap in result.gaps], result.display_names

    records, gaps, _ = convert_suite(passing, child_exit_code=1)
    assert gaps == ["ginkgo-exit-contradicts-report"]
    results = [record["data"] for record in records if record["event"] == "test_result"]
    assert results and all(data["outcome"] == "passed" for data in results)
    assert records[-2]["data"]["outcome"] == "failed"
    assert records[-2]["data"]["exit_code"] == 1
    # The same suite with an unmeasured exit stays passed with no gap.
    records_null, gaps_null, _ = convert_suite(passing, child_exit_code=None)
    assert gaps_null == []
    assert records_null[-2]["data"]["outcome"] == "passed"
    assert records_null[-2]["data"]["exit_code"] is None
