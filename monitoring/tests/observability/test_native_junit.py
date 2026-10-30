"""JUnit XML adapter tests: parsing, safety, duplicates, bundles, and
real runs."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from monitoring.ci_observability.contracts.validation import validate_bundle, validate_record
from monitoring.ci_observability.native.junit import junit_journal_records, parse_junit_xml
from monitoring.ci_observability.native.records import ADAPTER_NAME, ADAPTER_VERSION, AdapterError, InvocationContext

TESTS_DIR = Path(__file__).resolve().parent
CONTRACT_NATIVE = TESTS_DIR.parent.parent / "ci_observability" / "contracts" / "v1" / "examples" / "native"

REPOSITORY_ID = 812345678
RUN_ID = 1122334600


def context(**overrides: object) -> InvocationContext:
    values: dict[str, object] = {
        "execution_id": "w03-junit-exec",
        "journal_id": "junit-journal",
        "invocation_id": "junit-invocation",
        "framework": "pytest",
        "tier": "e2e",
        "component": "osac-e2e",
        "variant": "serial",
        "observed_at": "2026-10-28T12:00:00Z",
        "child_exit_code": None,
    }
    values.update(overrides)
    return InvocationContext(**values)  # type: ignore[arg-type]


def convert(raw: bytes, **overrides: object) -> tuple[list[dict[str, object]], list[str], dict[str, str]]:
    report = parse_junit_xml(raw=raw)
    result = junit_journal_records(context=context(**overrides), report=report)
    for record in result.records:
        validate_record(record=record)
    return result.records, [gap.code for gap in result.gaps], result.display_names


def write_bundle(work: Path, records: list[dict[str, object]], *, complete: bool) -> Path:
    """Assemble a minimal, fully valid frozen-v1 bundle around one
    adapter journal plus the controller execution envelope, and validate
    it with the real bundle validator. The journal file is named after
    the records' own journal_id."""

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
                "job": {"logical_job_key": "tests.yml#junit", "github_job_id": 4588112299, "job_name": "junit"},
                "suite": {"framework": "pytest", "tier": "e2e", "component": "osac-e2e", "variant": "serial"},
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
            "job": {"logical_job_key": "tests.yml#junit", "github_job_id": 4588112299, "job_name": "junit"},
            "cohort": "pull-request",
        },
        "suite": {"framework": "pytest", "tier": "e2e", "component": "osac-e2e", "variant": "serial"},
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
            else [{"source": "osac-ci-observability-native-adapter:pytest", "message": "junit-duplicate-identity"}],
        },
        "redaction": {"applied": True, "findings": 0},
    }
    (bundle / "bundle-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    validate_bundle(bundle_dir=bundle)
    return bundle


def test_parses_the_real_pytest_fixture() -> None:
    report = parse_junit_xml(raw=(CONTRACT_NATIVE / "pytest-junit.xml").read_bytes())
    assert len(report.cases) == 5
    outcomes = {case.name: case.outcome for case in report.cases}
    assert outcomes["test_passes"] == "passed"
    assert outcomes["test_fails"] == "failed"
    assert outcomes["test_skips"] == "skipped"
    assert outcomes["test_expected_failure"] == "expected_failure"
    # A non-strict unexpected pass is indistinguishable from a plain
    # pass in JUnit XML; the format simply cannot say.
    assert outcomes["test_unexpected_pass"] == "passed"


def test_conversion_omits_what_the_format_does_not_carry() -> None:
    records, gaps, _ = convert((CONTRACT_NATIVE / "pytest-junit.xml").read_bytes())
    assert gaps == []
    # JUnit has no collection catalog and no case timestamps.
    assert not any(record["event"] in ("test_collected", "collection_finished") for record in records)
    results = [record["data"] for record in records if record["event"] == "test_result"]
    assert len(results) == 5
    # No retry fields exist: the attempt count is unknown, never one.
    assert all(data["attempt_count"] is None for data in results)
    assert all("source_started_at" not in data and "source_ended_at" not in data for data in results)
    failing = next(data for data in results if data["outcome"] == "failed")
    assert failing["failure"]["signature"].startswith("pytest:failure:")
    assert set(failing["failure"]) == {"signature"}
    assert records[-2]["data"]["outcome"] == "failed"
    # A static fixture measured no child process: the exit stays null,
    # never a pinned contradictory code.
    assert records[-2]["data"]["exit_code"] is None
    state = records[-1]["data"]
    assert state["status"] == "stopped" and state["complete"] is True


def test_agreeing_duplicates_collapse_into_one_aggregate_with_a_gap() -> None:
    raw = (
        b'<testsuites><testsuite name="s">'
        b'<testcase classname="tests.test_a" name="test_dup" time="0.001"/>'
        b'<testcase classname="tests.test_a" name="test_dup" time="0.002"/>'
        b"</testsuite></testsuites>"
    )
    records, gaps, _ = convert(raw)
    assert gaps == ["junit-duplicate-identity"]
    results = [record["data"] for record in records if record["event"] == "test_result"]
    # One identity, one aggregate: never an invented retry.
    assert len(results) == 1
    assert results[0]["outcome"] == "passed"
    assert results[0]["attempt_count"] is None
    # Duplicate durations disagree; none is a span of the single aggregate.
    assert "duration_ns" not in results[0]
    state = records[-1]["data"]
    # Duplicate identities delivered every fact: zero drops, gap label.
    assert state["dropped_records"] == 0
    assert state["errors"] == [{"message": "junit-duplicate-identity"}]
    assert "omitted_fields" not in state


def test_conflicting_duplicates_become_unknown() -> None:
    raw = (
        b'<testsuites><testsuite name="s">'
        b'<testcase classname="tests.test_a" name="test_dup"/>'
        b'<testcase classname="tests.test_a" name="test_dup"><failure message="boom"/></testcase>'
        b"</testsuite></testsuites>"
    )
    records, gaps, _ = convert(raw)
    assert gaps == ["junit-duplicate-identity"]
    results = [record["data"] for record in records if record["event"] == "test_result"]
    assert len(results) == 1
    assert results[0]["outcome"] == "unknown"


def test_duplicate_bundles_pass_the_real_frozen_bundle_validator(tmp_path: Path) -> None:
    """The whole v1 bundle, not only each record: the frozen validator
    accepts a duplicate-identity conversion end to end."""

    raw = (
        b'<testsuites><testsuite name="s">'
        b'<testcase classname="tests.test_a" name="test_dup" time="0.001"/>'
        b'<testcase classname="tests.test_a" name="test_dup" time="0.002"/>'
        b'<testcase classname="tests.test_b" name="test_ok"/>'
        b"</testsuite></testsuites>"
    )
    records, gaps, _ = convert(raw)
    assert gaps == ["junit-duplicate-identity"]
    # A degraded source means the bundle is honestly incomplete.
    write_bundle(tmp_path, records, complete=False)

    clean_records, clean_gaps, _ = convert(
        (CONTRACT_NATIVE / "pytest-junit.xml").read_bytes(), invocation_id="junit-invocation-clean"
    )
    assert clean_gaps == []
    write_bundle(tmp_path, clean_records, complete=True)


def test_empty_report_is_unknown_with_a_gap() -> None:
    records, gaps = convert(b"<testsuites></testsuites>")[:2]
    assert gaps == ["junit-no-test-cases"]
    assert records[-2]["data"]["outcome"] == "unknown"


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32"])
def test_dtd_rejection_survives_utf16_and_utf32(encoding: str) -> None:
    """The byte scan cannot see a UTF-16 DOCTYPE: the expat declaration
    handlers, which run after encoding detection, must reject it."""

    raw = (
        '<?xml version="1.0" encoding="' + encoding + '"?>'
        '<!DOCTYPE testsuites [<!ENTITY xxe "file:///etc/passwd">]>'
        "<testsuites/>"
    ).encode(encoding)
    with pytest.raises(AdapterError):
        parse_junit_xml(raw=raw)


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32"])
def test_internal_entity_expansion_is_rejected_and_never_reaches_output(encoding: str) -> None:
    raw = (
        '<?xml version="1.0" encoding="' + encoding + '"?>'
        '<!DOCTYPE testsuites [<!ENTITY secret "LEAKED-ENTITY-TEXT">]>'
        "<testsuites><testsuite><testcase classname='c' name='n'>&secret;</testcase></testsuite></testsuites>"
    ).encode(encoding)
    with pytest.raises(AdapterError):
        parse_junit_xml(raw=raw)


def test_entity_declaration_rejection_message_is_constant() -> None:
    raw = b'<?xml version="1.0"?><!DOCTYPE testsuites [<!ENTITY a "b">]><testsuites/>'
    with pytest.raises(AdapterError, match="must not contain a DTD"):
        parse_junit_xml(raw=raw)


def test_bounded_parsing_rejects_oversized_nodes_depth_and_text() -> None:
    with pytest.raises(AdapterError):
        parse_junit_xml(raw=b"<testsuites>" + b"<suite/>" * 300_000 + b"</testsuites>")
    deep = b"<testsuites>" + b"<suite>" * 100 + b"</suite>" * 100 + b"</testsuites>"
    with pytest.raises(AdapterError):
        parse_junit_xml(raw=deep)
    text = b"<testsuites><suite>" + b"x" * (17 * 1024 * 1024) + b"</suite></testsuites>"
    with pytest.raises(AdapterError):
        parse_junit_xml(raw=text)


@pytest.mark.parametrize(
    "raw",
    [
        b'<?xml version="1.0"?><!DOCTYPE testsuites [<!ENTITY x "y">]><testsuites/>',
        b"<testsuites><testcase classname='c' name='n' time='abc'/></testsuites>",
        b"<testsuites><testcase name='n'/></testsuites>",
        b"<wrong/>",
        b"not xml at all",
    ],
)
def test_unsafe_or_malformed_xml_is_rejected(raw: bytes) -> None:
    with pytest.raises(AdapterError):
        parse_junit_xml(raw=raw)


def test_oversized_document_is_rejected() -> None:
    raw = b"<testsuites>" + b"<suite/>" * 300_000 + b"</testsuites>"
    with pytest.raises(AdapterError):
        parse_junit_xml(raw=raw)


def test_real_pytest_run_converts_with_matching_outcomes() -> None:
    """Real pytest 8.3.5 JUnit XML from a scratch suite."""

    work = Path(tempfile.mkdtemp(prefix="ci-native-junit-", dir="/tmp/opencode"))
    try:
        (work / "test_scratch.py").write_text(
            "import pytest\n\n"
            "def test_passes():\n"
            "    assert 1 + 1 == 2\n\n"
            "def test_fails():\n"
            "    assert 1 + 1 == 3\n\n"
            "@pytest.mark.skip(reason='skipped on purpose')\n"
            "def test_skips():\n"
            "    assert False\n\n"
            "@pytest.mark.xfail(strict=False)\n"
            "def test_expected_failure():\n"
            "    assert False\n",
            encoding="utf-8",
        )
        report_path = work / "report.xml"
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-o", "addopts=", "--junitxml", str(report_path), "test_scratch.py"],
            cwd=work,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert report_path.is_file(), completed.stderr[-400:]
        assert completed.returncode == 1
        records, gaps, _ = convert(report_path.read_bytes(), child_exit_code=completed.returncode)
        assert gaps == []
        outcomes = [str(record["data"]["outcome"]) for record in records if record["event"] == "test_result"]
        assert sorted(outcomes) == ["expected_failure", "failed", "passed", "skipped"]
        assert records[-2]["data"]["outcome"] == "failed"
        # The caller keeps the child's measured exit: a failing pytest
        # run stays exit 1, never reinterpreted.
        assert records[-2]["data"]["exit_code"] == completed.returncode == 1
        write_bundle(work, records, complete=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_times_fail_as_adapter_errors(value: str) -> None:
    """time='NaN' and time='Infinity' are rejected at the curated
    AdapterError boundary, not leaked as decimal exceptions and never
    replaced with an unknown duration."""

    raw = (
        f'<testsuites><testsuite name="s" tests="1">'
        f'<testcase classname="c" name="t" time="{value}"/></testsuite></testsuites>'
    ).encode()
    with pytest.raises(AdapterError):
        parse_junit_xml(raw=raw)


def test_measured_nonzero_child_exit_cannot_label_an_invocation_passed() -> None:
    """A passing JUnit report beside a measured exit 1 (plain tests in
    the same binary can fail outside the XML) keeps its per-case
    outcomes but the invocation is failed with a fixed gap: no invented
    failing test, no cause category."""

    raw = (
        b'<testsuites><testsuite name="s" tests="1">'
        b'<testcase classname="c" name="t" time="0.001"/></testsuite></testsuites>'
    )
    records, gaps, _ = convert(raw, child_exit_code=1)
    # The junit converter's framework identity is pytest.
    assert gaps == ["pytest-exit-contradicts-report"]
    results = [record["data"] for record in records if record["event"] == "test_result"]
    assert results[0]["outcome"] == "passed"
    finished = records[-2]["data"]
    assert finished["outcome"] == "failed"
    assert finished["exit_code"] == 1


def test_extreme_duration_literal_fails_as_adapter_error() -> None:
    """A finite scientific seconds literal far outside the supported
    duration span must be refused with the curated error at parse time,
    never decimal.Overflow, and never normalized into an unknown
    duration."""

    raw = (
        b'<testsuites><testsuite name="s" tests="1">'
        b'<testcase classname="safe" name="safe" time="1e999999999"/></testsuite></testsuites>'
    )
    with pytest.raises(AdapterError):
        parse_junit_xml(raw=raw)


def test_repeated_escaped_text_stays_bounded_and_exact() -> None:
    """Character data accumulates per node: a large run of entity
    escapes (well under every byte bound) must parse without quadratic
    blowup and keep the exact decoded text."""

    def build(count: int) -> bytes:
        # No message attribute: the failure text is the element's own
        # character data, which is the accumulating path under test.
        return (
            b'<testsuites><testsuite name="s" tests="1">'
            b'<testcase classname="c" name="t"><failure>'
            + b"&amp;" * count
            + b"</failure></testcase></testsuite></testsuites>"
        )

    small, large = build(2_000), build(100_000)
    report_small = parse_junit_xml(raw=small)
    report_large = parse_junit_xml(raw=large)
    assert len(report_large.cases) == len(report_small.cases) == 1
    # Exact decoded text: each &amp; is one '&'.
    assert report_large.cases[0].failure_text == "&" * 100_000
    assert report_small.cases[0].failure_text == "&" * 2_000
    # Bounded scaling evidence: 50x the entities must not cost ~50^2.
    start = time.perf_counter()
    parse_junit_xml(raw=large)
    large_seconds = time.perf_counter() - start
    start = time.perf_counter()
    parse_junit_xml(raw=small)
    small_seconds = time.perf_counter() - start
    # A quadratic parser needs ~2500x the small time for 50x the input;
    # the accumulated-chunks parser stays within a generous linear
    # factor. Structural equality above is the primary proof; this
    # guards only against reintroducing accidental quadratic joins.
    assert large_seconds < max(small_seconds * 100, 2.0)
