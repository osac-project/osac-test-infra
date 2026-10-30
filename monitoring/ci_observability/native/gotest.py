"""go test -json stream parsing and conversion to frozen v1 journal records.

Test identity is Package plus Test (subtests are already part of the Test
name). With -count > 1 the stream repeats the run/terminal cycle for a
test, and each cycle is one measured attempt; the attempt index is the
ordinal of its run event.

A cached package replays its previous results without re-executing
anything. Those outcomes are attributed previous results, not measured
executions: they keep their outcome with attempt_count null, no
duration, and no source timestamps, and the non-execution is an explicit
gap. Package-level events, build output, and events without a Time are
not test cases; a failed build is a gap plus a failed invocation. An
empty stream is an unknown outcome, never a pass.
"""

from __future__ import annotations

from dataclasses import dataclass

from .jsoninput import load_jsonl_bytes
from .records import (
    AdapterError,
    ConversionResult,
    InvocationContext,
    JSONValue,
    RecordWriter,
    check_exit_contradiction,
    collection_state_data,
    duration_ns_from_span,
    hashed_test_id,
    to_utc_z,
)

MAX_GO_EVENTS = 500_000

KNOWN_ACTIONS = {"start", "run", "pause", "cont", "output", "pass", "fail", "skip", "build-output", "build-fail"}
TERMINAL_ACTIONS = {"pass": "passed", "fail": "failed", "skip": "skipped"}


@dataclass(frozen=True, kw_only=True)
class GoTestEvent:
    """One go test -json event. Output text stays out of records: only
    the constant go-format "(cached)" marker is detected, never kept."""

    action: str
    package: str | None
    test: str | None
    time: str | None
    import_path: str | None
    cached: bool
    cached_output: bool
    failed_build: bool


@dataclass(frozen=True, kw_only=True)
class GoTestReport:
    """One parsed event stream."""

    events: list[GoTestEvent]
    cached_packages: frozenset[str]


def parse_go_test_stream(*, raw: bytes) -> GoTestReport:
    """Parse and shape-check a go test -json stream."""

    document = load_jsonl_bytes(raw=raw, max_lines=MAX_GO_EVENTS)
    events = [_parse_event(event) for event in document]
    cached = frozenset(
        event.package for event in events if event.package is not None and (event.cached or event.cached_output)
    )
    return GoTestReport(events=events, cached_packages=cached)


def _parse_event(event: object) -> GoTestEvent:
    if not isinstance(event, dict):
        raise AdapterError("go test event must be an object")
    action = event.get("Action")
    if not isinstance(action, str) or action not in KNOWN_ACTIONS:
        raise AdapterError("go test event has an unknown action")
    package = event.get("Package")
    test = event.get("Test")
    time = event.get("Time")
    import_path = event.get("ImportPath")
    for name, value in (("Package", package), ("Test", test), ("Time", time), ("ImportPath", import_path)):
        if value is not None and not isinstance(value, str):
            raise AdapterError(f"go test event {name} must be a string or absent")
    if time is not None:
        # Validate the shape now so conversion cannot fail mid-stream.
        to_utc_z(native=time)
    cached = event.get("Cached")
    if cached is not None and not isinstance(cached, bool):
        raise AdapterError("go test event Cached must be a boolean or absent")
    output = event.get("Output")
    if output is not None and not isinstance(output, str):
        raise AdapterError("go test event Output must be a string or absent")
    return GoTestEvent(
        action=action,
        package=package,
        test=test,
        time=time,
        import_path=import_path,
        # go emits the Cached field only on the package-level terminal
        # event; a Cached field on a test event is not a cache signal.
        cached=bool(cached) and test is None,
        cached_output=_is_package_cache_marker(action=action, package=package, test=test, output=output),
        failed_build="FailedBuild" in event,
    )


def _is_package_cache_marker(*, action: str, package: str | None, test: str | None, output: str | None) -> bool:
    """True only for the genuine package-level cache signal.

    go test announces a cached package with one package-result output
    line, ``ok  <tab>pkg<tab>(cached)``, on an output event that names
    no test, and the package inside the line matches the event's own
    Package. An arbitrary test's Output that happens to contain
    ``(cached)`` is ordinary test text and must not erase a measured
    attempt.
    """
    if action != "output" or test is not None or package is None or output is None:
        return False
    parts = output.rstrip("\n").split("\t")
    return len(parts) == 3 and parts[0].strip() == "ok" and parts[1] == package and parts[2] == "(cached)"


@dataclass(frozen=True, kw_only=True)
class _Attempt:
    test_id: str
    attempt_index: int
    outcome: str
    duration_ns: int | None
    start_time: str | None
    end_time: str | None


def go_test_journal_records(*, context: InvocationContext, report: GoTestReport) -> ConversionResult:
    """Convert one parsed stream into journal records plus explicit gaps."""

    writer = RecordWriter(context=context)
    display_names: dict[str, str] = {}

    started: dict[str, JSONValue] = {
        "invocation_id": context.invocation_id,
        "framework": "go-test",
        "tier": context.tier,
        "component": context.component,
        "variant": context.variant,
    }
    if context.selection is not None:
        started["selection"] = context.selection
    first_time = next((event.time for event in report.events if event.time is not None), None)
    writer.emit(
        event="invocation_started",
        data=started,
        source_at=to_utc_z(native=first_time) if first_time is not None else None,
    )

    # One aggregate per test. Live tests also carry measured attempts.
    attempts: dict[str, list[_Attempt]] = {}
    open_runs: dict[str, tuple[str, str | None]] = {}
    run_ordinals: dict[str, int] = {}
    cached_outcomes: dict[str, str] = {}
    cached_keys: dict[str, str] = {}
    failed_any = False
    saw_test_outcome = False

    for event in report.events:
        if event.action == "build-fail" or (event.action == "fail" and event.failed_build):
            writer.add_gap(code="go-test-build-failed")
            failed_any = True
        if event.test is None:
            continue
        key = f"{event.package}\n{event.test}"
        test_id = hashed_test_id(framework="go-test", component=context.component, native_key=key)
        display_names[test_id] = f"{event.package}/{event.test}"
        if event.package in report.cached_packages:
            # Replayed results are attributed previous outcomes, never
            # measured executions of this invocation.
            if event.action in TERMINAL_ACTIONS:
                saw_test_outcome = True
                cached_outcomes[test_id] = TERMINAL_ACTIONS[event.action]
                cached_keys[test_id] = key
                if event.action == "fail":
                    failed_any = True
            continue
        if event.action == "run":
            run_ordinals[key] = run_ordinals.get(key, 0) + 1
            open_runs[key] = (test_id, event.time)
        elif event.action in TERMINAL_ACTIONS:
            saw_test_outcome = True
            outcome = TERMINAL_ACTIONS[event.action]
            if outcome == "failed":
                failed_any = True
            run_time = open_runs.pop(key, None)
            attempt_index = run_ordinals.get(key, 1)
            start_time: str | None = None
            duration_ns: int | None = None
            if run_time is None:
                writer.add_gap(code="go-test-attempt-without-run")
            elif run_time[1] is None or event.time is None:
                writer.add_gap(code="go-test-attempt-missing-time")
            else:
                start_time = to_utc_z(native=run_time[1])
                duration_ns = duration_ns_from_span(start=run_time[1], end=event.time)
            end_time = to_utc_z(native=event.time) if event.time is not None else None
            attempts.setdefault(key, []).append(
                _Attempt(
                    test_id=test_id,
                    attempt_index=attempt_index,
                    outcome=outcome,
                    duration_ns=duration_ns,
                    start_time=start_time,
                    end_time=end_time,
                )
            )

    for test_attempts in attempts.values():
        test_id = test_attempts[0].test_id
        # The aggregate carries the measured attempt count, the last
        # terminal outcome, and the summed attempt spans; the total is
        # never charged to the final retry alone.
        aggregate: dict[str, JSONValue] = {
            "result_scope": "aggregate",
            "test_id": test_id,
            "attempt_count": len(test_attempts),
            "outcome": test_attempts[-1].outcome,
        }
        durations = [attempt.duration_ns for attempt in test_attempts]
        if all(duration is not None for duration in durations):
            aggregate["duration_ns"] = sum(durations)
        first_start = next((attempt.start_time for attempt in test_attempts if attempt.start_time is not None), None)
        last_end = next((attempt.end_time for attempt in reversed(test_attempts) if attempt.end_time is not None), None)
        if first_start is not None:
            aggregate["source_started_at"] = first_start
        if last_end is not None:
            aggregate["source_ended_at"] = last_end
        writer.emit(event="test_result", data=aggregate, source_at=last_end or first_start)
        for attempt in test_attempts:
            data: dict[str, JSONValue] = {
                "result_scope": "attempt",
                "test_id": attempt.test_id,
                "attempt_index": attempt.attempt_index,
                "outcome": attempt.outcome,
            }
            if attempt.duration_ns is not None:
                data["duration_ns"] = attempt.duration_ns
            if attempt.start_time is not None:
                data["source_started_at"] = attempt.start_time
            if attempt.end_time is not None:
                data["source_ended_at"] = attempt.end_time
            writer.emit(event="test_result", data=data, source_at=attempt.end_time or attempt.start_time)

    for test_id, outcome in cached_outcomes.items():
        # Attributed previous result: outcome retained, everything the
        # replay cannot know stays absent.
        writer.emit(
            event="test_result",
            data={"result_scope": "aggregate", "test_id": test_id, "attempt_count": None, "outcome": outcome},
        )

    for _package in sorted(report.cached_packages):
        # A cached package was not re-executed: the replayed results stay
        # as attributed outcomes and the non-execution is an explicit gap.
        writer.add_gap(code="go-test-package-cached")
    if not saw_test_outcome:
        # An empty or build-only stream observed no test results.
        writer.add_gap(code="go-test-no-test-events")
    report_outcome = "failed" if failed_any else ("passed" if saw_test_outcome else "unknown")
    invocation_outcome = check_exit_contradiction(
        writer=writer, framework="go-test", report_outcome=report_outcome, child_exit_code=context.child_exit_code
    )
    last_time = next((event.time for event in reversed(report.events) if event.time is not None), None)
    writer.emit(
        event="invocation_finished",
        data={
            "invocation_id": context.invocation_id,
            "outcome": invocation_outcome,
            "exit_code": context.child_exit_code,
        },
        source_at=to_utc_z(native=last_time) if last_time is not None else None,
    )
    writer.emit(
        event="collection_state",
        data=collection_state_data(
            framework="go-test",
            gaps=writer.gaps,
            covered_from=to_utc_z(native=first_time) if first_time is not None else None,
            covered_to=to_utc_z(native=last_time) if last_time is not None else None,
        ),
        source_at=to_utc_z(native=last_time) if last_time is not None else None,
    )
    return writer.result(display_names=display_names)
