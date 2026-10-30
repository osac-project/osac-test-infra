"""Vitest adapter tests: parsing, precision, identity, gaps, and the
configured-tool subprocess boundary for both verified versions.

The sanitized fixtures are regenerated real captures (2026-10-28
recovery; see the fixtures README): a 4.1.9 run and a 4.1.11 run whose
suite deliberately declares one duplicate display name, so the
duplicate-identity path is exercised on captured data too."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from monitoring.ci_observability.contracts.validation import validate_bundle, validate_record
from monitoring.ci_observability.native.records import ADAPTER_NAME, ADAPTER_VERSION, AdapterError, InvocationContext
from monitoring.ci_observability.native.vitest import parse_vitest_report, vitest_journal_records

TESTS_DIR = Path(__file__).resolve().parent
CONTRACT_NATIVE = TESTS_DIR.parent.parent / "ci_observability" / "contracts" / "v1" / "examples" / "native"
FIXTURES = TESTS_DIR.parent / "fixtures" / "native_adapters"
# Per-version configured tools (see the native README's prerequisite
# section): CI_OBSERVABILITY_VITEST_411_BIN for the current lockfile
# boundary, CI_OBSERVABILITY_VITEST_419_BIN for the historical one. A
# generic CI_OBSERVABILITY_VITEST_BIN runs whichever version it actually
# is and explicitly skips the other. A configured tool that is missing,
# unusable, or the wrong version FAILS; only an unset, unconfigured
# boundary skips. No hardcoded workstation path is a prerequisite.
VITEST_411_BIN = os.environ.get("CI_OBSERVABILITY_VITEST_411_BIN", "").strip()
VITEST_419_BIN = os.environ.get("CI_OBSERVABILITY_VITEST_419_BIN", "").strip()
VITEST_BIN = os.environ.get("CI_OBSERVABILITY_VITEST_BIN", "").strip()


def context(**overrides: object) -> InvocationContext:
    values: dict[str, object] = {
        "execution_id": "w03-vitest-exec",
        "journal_id": "vitest-journal",
        "invocation_id": "vitest-invocation",
        "framework": "vitest",
        "tier": "unit",
        "component": "osac-ui",
        "variant": "unit-vitest",
        "observed_at": "2026-10-28T12:00:00Z",
        "child_exit_code": None,
    }
    values.update(overrides)
    return InvocationContext(**values)  # type: ignore[arg-type]


def convert(
    raw: bytes, source_root: str = "/checkout", **overrides: object
) -> tuple[list[dict[str, object]], list[str]]:
    report = parse_vitest_report(raw=raw, source_root=source_root)
    result = vitest_journal_records(context=context(**overrides), report=report)
    for record in result.records:
        validate_record(record=record)
    return result.records, [gap.code for gap in result.gaps]


def test_parses_the_actual_4_1_9_capture_exactly() -> None:
    report = parse_vitest_report(raw=(FIXTURES / "vitest-4.1.9.sanitized.json").read_bytes(), source_root="/checkout")
    assert report.total == 3
    assert (report.passed, report.failed, report.pending) == (1, 1, 1)
    assert report.success is False
    suite = report.suites[0]
    assert suite.start_time == "2026-10-28T16:17:07.499Z"
    # The float epoch-milliseconds end time keeps its significant digits.
    assert suite.end_time == "2026-10-28T16:17:07.5032615Z"
    durations = [case.duration_ns for case in suite.cases]
    # 1.0793049999999909 ms quantized once, half-even, at the ns.
    assert 1079305 in durations


def test_conversion_matches_the_contract_fixture() -> None:
    for raw in (
        (CONTRACT_NATIVE / "vitest.json").read_bytes(),
        (FIXTURES / "vitest-4.1.9.sanitized.json").read_bytes(),
    ):
        records, gaps = convert(raw)
        assert gaps == []
        results = [record["data"] for record in records if record["event"] == "test_result"]
        assert len(results) == 3
        # Vitest 4.1.9 has no retry fields: unknown, never invented one.
        assert all(data["attempt_count"] is None for data in results)
        assert sorted(data["outcome"] for data in results) == ["failed", "passed", "skipped"]
        # Cases carry only durations; no invented case timestamps.
        assert all("source_started_at" not in data for data in results)
        failed = next(data for data in results if data["outcome"] == "failed")
        assert failed["failure"]["signature"].startswith("vitest:assertion:")
        assert set(failed["failure"]) == {"signature"}
        assert records[-2]["data"]["outcome"] == "failed"
        # A static fixture measured no child process: the exit stays
        # null, never a pinned contradictory code.
        assert records[-2]["data"]["exit_code"] is None
        state = records[-1]["data"]
        assert state["source"] == "osac-ci-observability-native-adapter:vitest"
        assert state["status"] == "stopped" and state["complete"] is True
        # Raw failure text never enters records.
        assert "expected 1 to be 2" not in json.dumps(records)


def test_counter_contradiction_has_unknown_loss_not_a_zero_claim() -> None:
    """A counter contradiction loses an unknowable number of results:
    the count stays at the schema-required 0 but is explicitly marked
    as a placeholder the source could not fill."""

    document = json.loads((FIXTURES / "vitest-4.1.9.sanitized.json").read_bytes())
    document["numTotalTests"] = 4
    records, gaps = convert(json.dumps(document).encode())[:2]
    assert "vitest-count-mismatch" in gaps
    state = records[-1]["data"]
    assert state["dropped_records"] == 0
    assert state["omitted_fields"] == ["dropped_records"]


def test_counter_contradictions_are_explicit_gaps() -> None:
    document = json.loads((FIXTURES / "vitest-4.1.9.sanitized.json").read_bytes())
    document["numTotalTests"] = 4
    raw = json.dumps(document).encode()
    _, gaps = convert(raw)
    assert "vitest-count-mismatch" in gaps
    document["numTotalTests"] = 3
    document["numPendingTests"] = 0
    raw = json.dumps(document).encode()
    _, gaps = convert(raw)
    assert "vitest-counter-mismatch" in gaps


@pytest.mark.parametrize(
    "raw",
    [
        b"[]",
        b"{}",
        b'{"success": true}',
        (
            b'{"success": "yes", "testResults": [], "numTotalTests": 0,'
            b' "numPassedTests": 0, "numFailedTests": 0, "numPendingTests": 0}'
        ),
    ],
)
def test_malformed_reports_fail_with_constant_errors(raw: bytes) -> None:
    with pytest.raises(AdapterError):
        parse_vitest_report(raw=raw, source_root="/checkout")


def test_unknown_case_status_fails() -> None:
    document = json.loads((FIXTURES / "vitest-4.1.9.sanitized.json").read_bytes())
    document["testResults"][0]["assertionResults"][0]["status"] = "flaky"
    with pytest.raises(AdapterError):
        parse_vitest_report(raw=json.dumps(document).encode(), source_root="/checkout")


def test_actual_4_1_11_fixture_matches_the_captured_shape() -> None:
    """A real 4.1.11 capture (regenerated), sanitized: the adapter claims
    format compatibility with the current lockfile boundary, not only
    with the older 4.1.9 capture. Key sets and types are identical; no
    retry or worker fields exist, so the attempt count stays null. The
    suite deliberately declares one duplicate display name: both
    occurrences stay represented with distinct identities and an honest
    duplicate-identity gap, never a fabricated retry."""

    raw = (FIXTURES / "vitest-4.1.11.sanitized.json").read_bytes()
    report = parse_vitest_report(raw=raw, source_root="/checkout")
    assert report.total == 4
    assert (report.passed, report.failed, report.pending) == (2, 1, 1)
    assert report.success is False
    assert report.duplicate_identities == 1
    suite = report.suites[0]
    assert isinstance(suite.start_time, str) and isinstance(suite.end_time, str)
    records, gaps = convert(raw)
    assert gaps == ["vitest-duplicate-identity"]
    results = [record["data"] for record in records if record["event"] == "test_result"]
    assert len(results) == 4
    assert sorted(data["outcome"] for data in results) == ["failed", "passed", "passed", "skipped"]
    assert all(data["attempt_count"] is None for data in results)
    # Every represented test keeps a distinct identity: four results,
    # four ids, no silent merge and no invented retry.
    assert len({data["test_id"] for data in results}) == 4
    # Private display names never enter journal records.
    assert "passes on purpose" not in json.dumps(records)


def test_source_root_bounds_file_identity() -> None:
    """The caller supplies the root; the adapter never infers it.

    The same suite at two physical checkout roots keeps one identity,
    and an absolute suite outside the root is rejected rather than
    hashed with a workstation path."""

    raw = (FIXTURES / "vitest-4.1.9.sanitized.json").read_bytes()
    document = json.loads(raw)
    document["testResults"][0]["name"] = "/checkout-one/sample.test.ts"
    one = json.dumps(document).encode()
    document["testResults"][0]["name"] = "/checkout-two/sample.test.ts"
    two = json.dumps(document).encode()
    records_one, _ = convert(one, source_root="/checkout-one")
    records_two, _ = convert(two, source_root="/checkout-two")
    ids_one = {r["data"]["test_id"] for r in records_one if r["event"] == "test_result"}
    ids_two = {r["data"]["test_id"] for r in records_two if r["event"] == "test_result"}
    assert ids_one == ids_two
    # An absolute suite outside the root is refused.
    with pytest.raises(AdapterError):
        parse_vitest_report(raw=one, source_root="/other-root")
    # Identity fields stay separate: a file and a full name that would
    # alias under one packed delimiter cannot alias here.
    tricky = json.loads(raw)
    tricky["testResults"][0]["name"] = "a/secret.test.ts"
    tricky["testResults"][0]["assertionResults"][0]["fullName"] = "x\nsame"
    other = json.loads(raw)
    other["testResults"][0]["name"] = "a/secret.test.ts\nx"
    other["testResults"][0]["assertionResults"][0]["fullName"] = "same"
    first_ids = {r["data"]["test_id"] for r in convert(json.dumps(tricky).encode())[0] if r["event"] == "test_result"}
    second_ids = {r["data"]["test_id"] for r in convert(json.dumps(other).encode())[0] if r["event"] == "test_result"}
    assert first_ids != second_ids


def test_present_but_malformed_optional_fields_fail() -> None:
    """A present field with an unsupported type is rejected, not
    normalized into unknown. Only genuinely absent or null stays None."""

    document = json.loads((FIXTURES / "vitest-4.1.9.sanitized.json").read_bytes())
    document["testResults"][0]["assertionResults"][0]["duration"] = "1.5"
    with pytest.raises(AdapterError):
        parse_vitest_report(raw=json.dumps(document).encode(), source_root="/checkout")
    document["testResults"][0]["assertionResults"][0]["duration"] = None
    report = parse_vitest_report(raw=json.dumps(document).encode(), source_root="/checkout")
    assert report.suites[0].cases[0].duration_ns is None


def _resolve_vitest_tool(wanted_version: str) -> str | None:
    """Resolve the tool for one wanted version, or None to skip.

    A per-version env path is the explicit configuration: the tool must
    exist, be executable, and report exactly the wanted version, or the
    test fails. A generic override runs whichever version it actually
    is: the matching case uses it, the other case skips with an explicit
    reason. An unset, unconfigured boundary returns None for an honest
    skip.
    """

    per_version = VITEST_411_BIN if wanted_version == "4.1.11" else VITEST_419_BIN
    candidates: list[tuple[str, str]] = []
    if per_version:
        candidates.append(("per-version", per_version))
    if VITEST_BIN:
        candidates.append(("generic", VITEST_BIN))
    for kind, path in candidates:
        if not os.path.isfile(path) or not os.access(path, os.X_OK):
            pytest.fail(f"{kind} Vitest tool is configured but missing or not executable: {path}")
        probe = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=60, check=False)
        actual = probe.stdout.strip()
        if wanted_version not in actual:
            if kind == "per-version":
                pytest.fail(f"per-version Vitest tool is configured for {wanted_version} but reports: {actual}")
            return None  # generic override runs its own version; skip this case
        return path
    return None


@pytest.mark.parametrize("wanted_version", ["4.1.11", "4.1.9"])
def test_real_vitest_run_converts_with_matching_outcomes(wanted_version: str) -> None:
    """Real-subprocess boundary for both verified versions: the current
    lockfile boundary 4.1.11 and the previously captured 4.1.9. A
    configured tool that is missing, unusable, or the wrong version
    fails; only an unset boundary skips with an explicit reason."""

    tool_path = _resolve_vitest_tool(wanted_version)
    if tool_path is None:
        pytest.skip(f"real Vitest {wanted_version} subprocess not configured for this run")

    work = Path(tempfile.mkdtemp(prefix=f"ci-native-vitest-{wanted_version.replace('.', '-')}-", dir="/tmp/opencode"))
    try:
        (work / "package.json").write_text('{"name": "scratch", "private": true, "type": "module"}\n', encoding="utf-8")
        (work / "sample.test.js").write_text(
            "import { test, expect } from 'vitest';\n"
            "test('passes on purpose', () => { expect(1 + 1).toBe(2); });\n"
            "test('fails on purpose', () => { expect(1 + 1).toBe(3); });\n"
            "test.skip('skips on purpose', () => {});\n",
            encoding="utf-8",
        )
        report_path = work / "report.json"
        completed = subprocess.run(
            [
                tool_path,
                "run",
                "--globals",
                "--maxWorkers=1",
                "--no-file-parallelism",
                "--reporter=json",
                "--outputFile",
                str(report_path),
            ],
            cwd=work,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        assert report_path.is_file(), f"vitest {wanted_version} produced no report: {completed.stderr[-400:]}"
        records, gaps = convert(report_path.read_bytes(), source_root=str(work), child_exit_code=completed.returncode)
        assert gaps == []
        results = [record["data"] for record in records if record["event"] == "test_result"]
        assert sorted(data["outcome"] for data in results) == ["failed", "passed", "skipped"]
        assert records[-2]["data"]["outcome"] == "failed"
        # The caller keeps the child's actual exit code: a failing test
        # run stays exit 1, never reinterpreted as an infra failure.
        assert records[-2]["data"]["exit_code"] == completed.returncode == 1
        state = records[-1]["data"]
        assert state["status"] == "stopped" and state["complete"] is True
    finally:
        shutil.rmtree(work, ignore_errors=True)


REPOSITORY_ID = 812345678
RUN_ID = 1122334600


def write_bundle(work: Path, records: list[dict[str, object]], *, complete: bool) -> Path:
    """Assemble a minimal, fully valid frozen-v1 bundle around the
    adapter journal plus the controller execution envelope, and validate
    it with the real bundle validator. A degraded source (the duplicate
    gap) means the bundle is honestly incomplete."""

    journal_id = str(records[0]["journal_id"])
    bundle = work / f"bundle-{journal_id}-{records[0]['invocation_id']}"
    (bundle / "journals").mkdir(parents=True)
    payload = "".join(json.dumps(record) + "\n" for record in records).encode("utf-8")
    (bundle / "journals" / f"{journal_id}.jsonl").write_bytes(payload)
    execution_id = str(records[0]["execution_id"])
    controller = [
        {
            "schema": "osac.ci-observability.journal-record",
            "schema_version": 1,
            "execution_id": execution_id,
            "journal_id": "controller",
            "sequence": 1,
            "recorded_at": "2026-10-28T11:59:00Z",
            "mono_ns": 100011223,
            "event": "execution_started",
            "data": {
                "job": {"logical_job_key": "tests.yml#vitest", "github_job_id": 4588112300, "job_name": "vitest"},
                "suite": {"framework": "vitest", "tier": "unit", "component": "osac-ui", "variant": "unit-vitest"},
            },
            "clock_id": "controller",
        },
        {
            "schema": "osac.ci-observability.journal-record",
            "schema_version": 1,
            "execution_id": execution_id,
            "journal_id": "controller",
            "sequence": 2,
            "recorded_at": "2026-10-28T12:01:00Z",
            "mono_ns": 350022445,
            "event": "execution_finished",
            "data": {"outcome": "passed", "exit_code": 0},
            "clock_id": "controller",
        },
    ]
    controller_payload = "".join(json.dumps(record) + "\n" for record in controller).encode("utf-8")
    (bundle / "journals" / "controller.jsonl").write_bytes(controller_payload)
    manifest = {
        "schema": "osac.ci-observability.bundle-manifest",
        "schema_version": 1,
        "created_at": "2026-10-28T12:00:00Z",
        "producer": {"name": ADAPTER_NAME, "version": ADAPTER_VERSION},
        "execution": {
            "execution_id": records[0]["execution_id"],
            "repository_id": REPOSITORY_ID,
            "repository": "osac-project/osac",
            "workflow": {"path": ".github/workflows/tests.yml", "revision": "a31fea65edcf9ff0f6a0012e7150f6150bde4c58"},
            "run_id": RUN_ID,
            "run_attempt": 1,
            "event": "pull_request",
            "job": {"logical_job_key": "tests.yml#vitest", "github_job_id": 4588112300, "job_name": "vitest"},
            "cohort": "pull-request",
        },
        "suite": {"framework": "vitest", "tier": "unit", "component": "osac-ui", "variant": "unit-vitest"},
        "environment": {
            "sources": [
                {
                    "role": "tested",
                    "repository": "osac-project/osac",
                    "revision": "a31fea65edcf9ff0f6a0012e7150f6150bde4c58",
                },
                {
                    "role": "workflow",
                    "repository": "osac-project/osac",
                    "revision": "a31fea65edcf9ff0f6a0012e7150f6150bde4c58",
                },
            ]
        },
        "files": [
            {
                "path": f"journals/{journal_id}.jsonl",
                "kind": "journal",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
                "record_count": len(records),
                "complete": True,
            },
            {
                "path": "journals/controller.jsonl",
                "kind": "journal",
                "sha256": hashlib.sha256(controller_payload).hexdigest(),
                "size_bytes": len(controller_payload),
                "record_count": len(controller),
                "complete": True,
            },
        ],
        "completeness": {
            "complete": complete,
            "end_reason": "finished" if complete else "unknown",
            "dropped_records": 0,
            "collection_errors": []
            if complete
            else [{"source": "osac-ci-observability-native-adapter:vitest", "message": "vitest-duplicate-identity"}],
        },
        "redaction": {"applied": True, "findings": 0},
    }
    (bundle / "bundle-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    validate_bundle(bundle_dir=bundle)
    return bundle


@pytest.mark.parametrize("wanted_version", ["4.1.11", "4.1.9"])
def test_real_duplicate_names_keep_distinct_identities(wanted_version: str) -> None:
    """The decisive duplicate-name case on real processes for both
    verified versions: one suite declares two same-named tests. Every
    occurrence stays represented with a distinct identity and an honest
    duplicate-identity gap, and the resulting journal passes the frozen
    whole-bundle validator."""

    tool_path = _resolve_vitest_tool(wanted_version)
    if tool_path is None:
        pytest.skip(f"real Vitest {wanted_version} subprocess not configured for this run")

    prefix = f"ci-native-vitest-dup-{wanted_version.replace('.', '-')}-"
    work = Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp/opencode"))
    try:
        (work / "package.json").write_text('{"name": "scratch", "private": true, "type": "module"}\n', encoding="utf-8")
        (work / "dup.test.js").write_text(
            "import { test, expect } from 'vitest';\n"
            "test('same name', () => { expect(1 + 1).toBe(2); });\n"
            "test('same name', () => { expect(2 + 2).toBe(4); });\n",
            encoding="utf-8",
        )
        report_path = work / "report.json"
        completed = subprocess.run(
            [
                tool_path,
                "run",
                "--globals",
                "--maxWorkers=1",
                "--no-file-parallelism",
                "--reporter=json",
                "--outputFile",
                str(report_path),
            ],
            cwd=work,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        assert report_path.is_file(), f"vitest {wanted_version} produced no report: {completed.stderr[-400:]}"
        records, gaps = convert(report_path.read_bytes(), source_root=str(work), child_exit_code=completed.returncode)
        assert gaps == ["vitest-duplicate-identity"]
        results = [record["data"] for record in records if record["event"] == "test_result"]
        assert len(results) == 2
        assert all(data["outcome"] == "passed" for data in results)
        # Two same-named tests: two distinct ids, no silent merge.
        ids = {data["test_id"] for data in results}
        assert len(ids) == 2
        # Private display names never enter journal records.
        assert "same name" not in json.dumps(records)
        # The duplicate journal passes the frozen whole-bundle validator;
        # a degraded source makes the bundle honestly incomplete.
        write_bundle(work, records, complete=False)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_measured_nonzero_child_exit_cannot_label_an_invocation_passed() -> None:
    document = json.loads((FIXTURES / "vitest-4.1.9.sanitized.json").read_bytes())
    document["numFailedTests"] = 0
    document["numPendingTests"] = 0
    document["numTotalTests"] = 1
    document["numPassedTests"] = 1
    document["success"] = True
    document["testResults"][0]["assertionResults"] = document["testResults"][0]["assertionResults"][:1]
    document["testResults"][0]["assertionResults"][0]["status"] = "passed"
    records, gaps = convert(json.dumps(document).encode(), child_exit_code=1)
    assert gaps == ["vitest-exit-contradicts-report"]
    results = [record["data"] for record in records if record["event"] == "test_result"]
    assert results[0]["outcome"] == "passed"
    assert records[-2]["data"]["outcome"] == "failed"
    assert records[-2]["data"]["exit_code"] == 1


@pytest.mark.parametrize("extreme", [1e30, 10**30])
def test_extreme_epoch_and_duration_values_fail_as_adapter_errors(extreme: float) -> None:
    """Finite JSON numbers far outside the supported calendar and the
    supported duration span must be refused with curated AdapterErrors
    at parse time, never OverflowError or decimal.InvalidOperation, and
    never normalized into unknown durations or fake times."""

    document = json.loads((FIXTURES / "vitest-4.1.9.sanitized.json").read_bytes())
    document["startTime"] = extreme
    with pytest.raises(AdapterError):
        parse_vitest_report(raw=json.dumps(document).encode(), source_root="/checkout")

    document = json.loads((FIXTURES / "vitest-4.1.9.sanitized.json").read_bytes())
    document["testResults"][0]["assertionResults"][0]["duration"] = 1e300
    with pytest.raises(AdapterError):
        parse_vitest_report(raw=json.dumps(document).encode(), source_root="/checkout")
