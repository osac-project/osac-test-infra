"""Vitest 4.1.9 JSON report parsing and conversion to frozen v1 records.

Vitest 4.1.9 has no retry fields: the attempt count is null (unknown),
never invented as one. Case status 'skipped' is counted by the native
counter numPendingTests. Case durations are float milliseconds read
through Decimal; suite times are epoch milliseconds. Counters are
cross-checked and any contradiction is an explicit gap.

Test-file identity is the suite name relative to an explicit source
root the caller supplies: the same suite at two physical checkout roots
keeps one identity, and an absolute file outside the root is rejected
rather than hashed with a workstation path. A relative suite name is
already root-relative and is kept as-is. The root is never inferred
from the report set.

Valid Vitest suites can contain duplicate display names within one
file: each occurrence gets a distinct identity through its occurrence
ordinal, with an honest duplicate-identity gap, never a fabricated
retry. The comparison limit is that the ordinal follows report order:
inserting a same-named case before an existing one shifts the later
ordinals. Identity fields stay separate: the file and the full name are
encoded as a JSON array, so a name containing the delimiter cannot
alias another test.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .jsoninput import load_json_bytes
from .records import (
    AdapterError,
    ConversionResult,
    InvocationContext,
    JSONValue,
    RecordWriter,
    check_exit_contradiction,
    collection_state_data,
    duration_ns_from_ms,
    epoch_ms_to_utc_z,
    failure_signature,
    hashed_test_id,
)

MAX_VITEST_CASES = 100_000

STATUS_OUTCOMES = {"passed": "passed", "failed": "failed", "skipped": "skipped", "todo": "skipped"}


@dataclass(frozen=True, kw_only=True)
class VitestCase:
    """One assertion result. Raw text fields are private, never journaled."""

    status: str
    duration_ns: int | None
    failure_text: str | None
    native_key: str
    display_name: str


@dataclass(frozen=True, kw_only=True)
class VitestSuite:
    """One testResults entry."""

    status: str
    start_time: str | None
    end_time: str | None
    cases: list[VitestCase]
    relative_file: str


@dataclass(frozen=True, kw_only=True)
class VitestReport:
    """One parsed Vitest report."""

    success: bool
    start_time: str | None
    suites: list[VitestSuite]
    total: int
    passed: int
    failed: int
    pending: int
    duplicate_identities: int


def _relative_suite_file(*, suite_name: str, source_root: str) -> str:
    """The suite's file path relative to the caller's source root.

    An absolute suite file must lie under the explicit root: a file
    outside it is refused, because a wrong root would silently change
    every identity. A relative suite name is already root-relative (the
    shipped captures are sanitized that way) and is kept as-is.
    """
    root = source_root.rstrip("/")
    if not root:
        raise AdapterError("vitest source_root must be a non-empty path")
    if suite_name.startswith("/"):
        if not suite_name.startswith(root + "/"):
            raise AdapterError("vitest suite file is not under the source root")
        relative = suite_name[len(root) + 1 :]
    else:
        relative = suite_name
    if not relative:
        raise AdapterError("vitest suite file equals the source root")
    if any(segment == ".." for segment in relative.split("/")):
        raise AdapterError("vitest suite file must not escape the source root")
    return relative


def parse_vitest_report(*, raw: bytes, source_root: str) -> VitestReport:
    """Parse and shape-check one Vitest JSON report."""

    document = load_json_bytes(raw=raw)
    if not isinstance(document, dict):
        raise AdapterError("vitest report must be an object")
    for name in ("success", "testResults", "numTotalTests", "numPassedTests", "numFailedTests", "numPendingTests"):
        if name not in document:
            raise AdapterError("vitest report is missing a required field")
    for name in ("numTotalTests", "numPassedTests", "numFailedTests", "numPendingTests"):
        value = document[name]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise AdapterError("vitest counters must be non-negative integers")
    raw_suites = document["testResults"]
    if not isinstance(raw_suites, list):
        raise AdapterError("vitest testResults must be a list")
    suites: list[VitestSuite] = []
    total_cases = 0
    duplicate_identities = 0
    for raw_suite in raw_suites:
        suite, case_count, duplicates = _parse_suite(raw_suite, source_root=source_root)
        suites.append(suite)
        total_cases += case_count
        duplicate_identities += duplicates
    if total_cases > MAX_VITEST_CASES:
        raise AdapterError("vitest report exceeds the case count bound")
    start_raw = document.get("startTime")
    if start_raw is not None and not isinstance(start_raw, (int, float)):
        raise AdapterError("vitest startTime must be a number or absent")
    start_time = epoch_ms_to_utc_z(value=start_raw) if isinstance(start_raw, (int, float)) else None
    return VitestReport(
        success=_required_bool(document["success"]),
        start_time=start_time,
        suites=suites,
        total=document["numTotalTests"],
        passed=document["numPassedTests"],
        failed=document["numFailedTests"],
        pending=document["numPendingTests"],
        duplicate_identities=duplicate_identities,
    )


def _parse_suite(raw_suite: object, *, source_root: str) -> tuple[VitestSuite, int, int]:
    if not isinstance(raw_suite, dict):
        raise AdapterError("vitest suite must be an object")
    for name in ("status", "assertionResults", "name"):
        if name not in raw_suite:
            raise AdapterError("vitest suite is missing a required field")
    status = raw_suite["status"]
    if not isinstance(status, str):
        raise AdapterError("vitest suite status must be a string")
    raw_cases = raw_suite["assertionResults"]
    if not isinstance(raw_cases, list):
        raise AdapterError("vitest assertionResults must be a list")
    suite_name = raw_suite["name"]
    if not isinstance(suite_name, str) or not suite_name:
        raise AdapterError("vitest suite name must be non-empty text")
    relative_file = _relative_suite_file(suite_name=suite_name, source_root=source_root)
    start_raw = raw_suite.get("startTime")
    end_raw = raw_suite.get("endTime")
    if start_raw is not None and not isinstance(start_raw, (int, float)):
        raise AdapterError("vitest suite startTime must be a number or absent")
    if end_raw is not None and not isinstance(end_raw, (int, float)):
        raise AdapterError("vitest suite endTime must be a number or absent")
    cases: list[VitestCase] = []
    seen: dict[tuple[str, str], int] = {}
    duplicates = 0
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise AdapterError("vitest case must be an object")
        for name in ("status", "fullName", "title"):
            if name not in raw_case:
                raise AdapterError("vitest case is missing a required field")
        case_status = raw_case["status"]
        full_name = raw_case["fullName"]
        title = raw_case["title"]
        if not isinstance(case_status, str) or case_status not in STATUS_OUTCOMES:
            raise AdapterError("vitest case has an unknown status")
        if not isinstance(full_name, str) or not isinstance(title, str):
            raise AdapterError("vitest case identity fields must be strings")
        duration = raw_case.get("duration")
        # A present but malformed field is rejected, not silently turned
        # into unknown. Only genuinely absent or null stays None.
        duration_ns = duration_ns_from_ms(value=duration) if duration is not None else None
        failure_messages = raw_case.get("failureMessages")
        failure_text: str | None = None
        if failure_messages is not None:
            if not isinstance(failure_messages, list) or not all(isinstance(item, str) for item in failure_messages):
                raise AdapterError("vitest failureMessages must be a list of strings or absent")
            if failure_messages:
                failure_text = failure_messages[0]
        occurrence = seen.get((relative_file, full_name), 0) + 1
        seen[(relative_file, full_name)] = occurrence
        if occurrence > 1:
            duplicates += 1
        # JSON encoding keeps the file and the full name separate: a
        # name containing any delimiter cannot alias another test.
        native_key = json.dumps([relative_file, full_name, occurrence], ensure_ascii=False, separators=(",", ":"))
        cases.append(
            VitestCase(
                status=case_status,
                duration_ns=duration_ns,
                failure_text=failure_text,
                native_key=native_key,
                display_name=full_name,
            )
        )
    start_time = epoch_ms_to_utc_z(value=start_raw) if isinstance(start_raw, (int, float)) else None
    end_time = epoch_ms_to_utc_z(value=end_raw) if isinstance(end_raw, (int, float)) else None
    return (
        VitestSuite(status=status, start_time=start_time, end_time=end_time, cases=cases, relative_file=relative_file),
        len(cases),
        duplicates,
    )


def _required_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise AdapterError("vitest success must be a boolean")
    return value


def vitest_journal_records(*, context: InvocationContext, report: VitestReport) -> ConversionResult:
    """Convert one parsed Vitest report into journal records plus gaps."""

    writer = RecordWriter(context=context)
    display_names: dict[str, str] = {}

    started: dict[str, JSONValue] = {
        "invocation_id": context.invocation_id,
        "framework": "vitest",
        "tier": context.tier,
        "component": context.component,
        "variant": context.variant,
    }
    if context.selection is not None:
        started["selection"] = context.selection
    writer.emit(event="invocation_started", data=started, source_at=report.start_time)

    all_cases = [case for suite in report.suites for case in suite.cases]
    counted = {
        "passed": sum(1 for case in all_cases if case.status == "passed"),
        "failed": sum(1 for case in all_cases if case.status == "failed"),
        "skipped": sum(1 for case in all_cases if case.status in ("skipped", "todo")),
    }
    if len(all_cases) != report.total:
        writer.add_gap(code="vitest-count-mismatch", count=abs(len(all_cases) - report.total))
    if counted["passed"] != report.passed or counted["failed"] != report.failed or counted["skipped"] != report.pending:
        writer.add_gap(code="vitest-counter-mismatch")
    if report.duplicate_identities:
        writer.add_gap(code="vitest-duplicate-identity", count=report.duplicate_identities)

    for suite in report.suites:
        for case in suite.cases:
            test_id = hashed_test_id(framework="vitest", component=context.component, native_key=case.native_key)
            display_names[test_id] = case.display_name
            outcome = STATUS_OUTCOMES[case.status]
            data: dict[str, JSONValue] = {
                "result_scope": "aggregate",
                "test_id": test_id,
                "attempt_count": None,
                "outcome": outcome,
            }
            if case.duration_ns is not None:
                data["duration_ns"] = case.duration_ns
            if outcome == "failed" and case.failure_text is not None:
                data["failure"] = {
                    "signature": failure_signature(
                        framework="vitest", failure_class="assertion", raw_text=case.failure_text
                    )
                }
            writer.emit(event="test_result", data=data)

    any_failed = counted["failed"] > 0 or not report.success
    last_end = next((suite.end_time for suite in reversed(report.suites) if suite.end_time is not None), None)
    report_outcome = "failed" if any_failed else ("passed" if all_cases else "unknown")
    invocation_outcome = check_exit_contradiction(
        writer=writer, framework="vitest", report_outcome=report_outcome, child_exit_code=context.child_exit_code
    )
    writer.emit(
        event="invocation_finished",
        data={
            "invocation_id": context.invocation_id,
            "outcome": invocation_outcome,
            "exit_code": context.child_exit_code,
        },
        source_at=last_end,
    )
    writer.emit(
        event="collection_state",
        data=collection_state_data(
            framework="vitest", gaps=writer.gaps, covered_from=report.start_time, covered_to=last_end
        ),
        source_at=last_end,
    )
    return writer.result(display_names=display_names)
