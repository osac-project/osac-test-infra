"""go test -json adapter tests: parsing, attempts, cached replay, gaps,
and real runs."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from monitoring.ci_observability.contracts.validation import validate_record
from monitoring.ci_observability.native.gotest import go_test_journal_records, parse_go_test_stream
from monitoring.ci_observability.native.records import AdapterError, InvocationContext

TESTS_DIR = Path(__file__).resolve().parent
CONTRACT_NATIVE = TESTS_DIR.parent.parent / "ci_observability" / "contracts" / "v1" / "examples" / "native"
FIXTURES = TESTS_DIR.parent / "fixtures" / "native_adapters"


def context(**overrides: object) -> InvocationContext:
    values: dict[str, object] = {
        "execution_id": "w03-gotest-exec",
        "journal_id": "gotest-journal",
        "invocation_id": "gotest-invocation",
        "framework": "go-test",
        "tier": "unit",
        "component": "osac-operator",
        "variant": "unit-go",
        "observed_at": "2026-10-28T12:00:00Z",
        "child_exit_code": None,
    }
    values.update(overrides)
    return InvocationContext(**values)  # type: ignore[arg-type]


def convert(raw: bytes, **overrides: object) -> tuple[list[dict[str, object]], list[str], dict[str, str]]:
    report = parse_go_test_stream(raw=raw)
    result = go_test_journal_records(context=context(**overrides), report=report)
    for record in result.records:
        validate_record(record=record)
    return result.records, [gap.code for gap in result.gaps], result.display_names


def by_test(records: list[dict[str, object]], display: dict[str, str], name: str) -> list[dict[str, object]]:
    """Results for one test, looked up through the private display mapping."""
    test_id = next(key for key, value in display.items() if value.endswith(f"/{name}"))
    return [
        record["data"]
        for record in records
        if record["event"] == "test_result" and record["data"]["test_id"] == test_id
    ]


def test_parses_the_real_fixture_stream() -> None:
    report = parse_go_test_stream(raw=(CONTRACT_NATIVE / "go-test.jsonl").read_bytes())
    actions = {event.action for event in report.events}
    assert {"run", "pass", "fail", "skip", "build-output", "build-fail"} <= actions
    assert all(event.time is None for event in report.events if event.action == "build-output")


def test_conversion_builds_attempts_and_aggregates() -> None:
    records, gaps, display = convert((CONTRACT_NATIVE / "go-test.jsonl").read_bytes())
    assert "go-test-build-failed" in gaps
    passing = by_test(records, display, "TestPassAlways")[0]
    # Aggregate first, then the single attempt, both from real cycles.
    assert passing["result_scope"] == "aggregate"
    assert passing["attempt_count"] == 1
    assert passing["outcome"] == "passed"
    assert passing["duration_ns"] > 0
    attempt = by_test(records, display, "TestPassAlways")[1]
    assert attempt["result_scope"] == "attempt"
    assert attempt["attempt_index"] == 1
    assert attempt["duration_ns"] == passing["duration_ns"]
    failing = by_test(records, display, "TestFailAlways")[0]
    assert failing["outcome"] == "failed"
    assert failing["attempt_count"] == 1
    finished = records[-2]["data"]
    assert finished["outcome"] == "failed"
    # A static stream measured no child process: null, never a pinned
    # contradictory code.
    assert finished["exit_code"] is None
    # No raw go output text enters records.
    assert "missing ','" not in json.dumps(records)


def test_empty_stream_is_unknown_not_passed() -> None:
    records, gaps, _ = convert(b"")
    assert "go-test-no-test-events" in gaps
    assert records[-2]["data"]["outcome"] == "unknown"
    state = records[-1]["data"]
    assert state["status"] == "degraded"


def test_cached_replay_is_an_attributed_previous_result_not_an_execution() -> None:
    """A real cached package replays run/pass events with replay-time
    timestamps: the outcome stays, but no measured attempt, duration, or
    source interval is invented."""

    raw = b'{"Time":"2026-10-28T00:00:00Z","Action":"start","Package":"example.com/p"}\n'
    raw += b'{"Time":"2026-10-28T00:00:01Z","Action":"run","Package":"example.com/p","Test":"TestPass"}\n'
    raw += b'{"Time":"2026-10-28T00:00:02Z","Action":"pass","Package":"example.com/p","Test":"TestPass","Elapsed":1}\n'
    raw += b'{"Time":"2026-10-28T00:00:03Z","Action":"pass","Package":"example.com/p","Elapsed":1,"Cached":true}\n'
    records, gaps, display = convert(raw)
    assert "go-test-package-cached" in gaps
    results = by_test(records, display, "TestPass")
    # One aggregate only: the replayed run/pass cycle is not an attempt
    # of this invocation.
    assert len(results) == 1
    aggregate = results[0]
    assert aggregate["result_scope"] == "aggregate"
    assert aggregate["outcome"] == "passed"
    assert aggregate["attempt_count"] is None
    assert "duration_ns" not in aggregate
    assert "source_started_at" not in aggregate
    assert "source_ended_at" not in aggregate
    # The non-execution persists in the terminal collection state.
    state = records[-1]["data"]
    assert state["status"] == "degraded"
    assert state["errors"] == [{"message": "go-test-package-cached"}]


def test_cached_package_has_zero_drops_because_every_fact_was_delivered() -> None:
    """A cached package retains all outcomes: the gap is a
    representation limit, not a lost record, so dropped_records is a
    measured zero and the loss is not flagged as unknown."""

    raw = b'{"Time":"2026-10-28T00:00:00Z","Action":"start","Package":"example.com/p"}\n'
    raw += b'{"Time":"2026-10-28T00:00:01Z","Action":"run","Package":"example.com/p","Test":"TestPass"}\n'
    raw += b'{"Time":"2026-10-28T00:00:02Z","Action":"pass","Package":"example.com/p","Test":"TestPass","Elapsed":1}\n'
    raw += b'{"Time":"2026-10-28T00:00:03Z","Action":"pass","Package":"example.com/p","Elapsed":1,"Cached":true}\n'
    records, gaps, _ = convert(raw)
    assert "go-test-package-cached" in gaps
    assert "go-test-no-test-events" not in gaps
    state = records[-1]["data"]
    assert state["status"] == "degraded"
    assert state["dropped_records"] == 0
    assert "omitted_fields" not in state


def test_build_failure_has_unknown_loss_not_a_zero_claim() -> None:
    """A build failure loses an unknowable number of results: the count
    stays at the schema-required 0 but is explicitly marked as a
    placeholder the source could not fill."""

    raw = b'{"ImportPath":"example.com/broken.test","Action":"build-output","Output":"err\\n"}\n'
    raw += b'{"ImportPath":"example.com/broken.test","Action":"build-fail"}\n'
    records, gaps, _ = convert(raw)
    assert "go-test-build-failed" in gaps
    assert "go-test-no-test-events" in gaps
    state = records[-1]["data"]
    assert state["dropped_records"] == 0
    assert state["omitted_fields"] == ["dropped_records"]


def test_missing_time_gap_is_not_a_dropped_record() -> None:
    """A cycle with missing timestamps delivered its outcome: the gap
    describes absent timing fields, not a lost record."""

    raw = b'{"Action":"run","Package":"example.com/p","Test":"TestNoTime"}\n'
    raw += b'{"Action":"pass","Package":"example.com/p","Test":"TestNoTime"}\n'
    records, gaps, _ = convert(raw)
    assert "go-test-attempt-missing-time" in gaps
    state = records[-1]["data"]
    assert state["dropped_records"] == 0
    assert "omitted_fields" not in state


def test_cached_marker_in_output_text_is_detected_without_keeping_text() -> None:
    raw = b'{"Time":"2026-10-28T00:00:00Z","Action":"start","Package":"example.com/q"}\n'
    raw += (
        b'{"Time":"2026-10-28T00:00:01Z","Action":"output","Package":"example.com/q",'
        b'"Output":"ok  \\texample.com/q\\t(cached)\\n"}\n'
    )
    raw += b'{"Time":"2026-10-28T00:00:01Z","Action":"pass","Package":"example.com/q","Elapsed":0}\n'
    report = parse_go_test_stream(raw=raw)
    assert report.cached_packages == frozenset({"example.com/q"})
    assert not any(hasattr(event, "output") for event in report.events)


def test_count_repetition_proves_attempt_indexes() -> None:
    base = json.loads(b'{"x":1}'.decode())
    lines = []
    for index in range(1, 3):
        lines.append(
            {
                "Time": f"2026-10-28T00:00:0{index}.000000001Z",
                "Action": "run",
                "Package": "example.com/p",
                "Test": "TestRepeated",
            }
        )
        lines.append(
            {
                "Time": f"2026-10-28T00:00:0{index}.500000001Z",
                "Action": "pass",
                "Package": "example.com/p",
                "Test": "TestRepeated",
            }
        )
    raw = ("\n".join(json.dumps({**base, **line}) for line in lines)).encode()
    records, gaps, display = convert(raw)
    assert gaps == []
    results = by_test(records, display, "TestRepeated")
    aggregate = results[0]
    attempts = results[1:]
    assert aggregate["attempt_count"] == 2
    # The aggregate sums the measured spans; it never charges the whole
    # time to the final retry.
    assert aggregate["duration_ns"] == sum(attempt["duration_ns"] for attempt in attempts)
    assert [attempt["attempt_index"] for attempt in attempts] == [1, 2]
    assert attempts[0]["source_started_at"].endswith("01.000000001Z")
    assert attempts[1]["source_started_at"].endswith("02.000000001Z")


def test_missing_time_and_missing_run_are_distinct_gaps() -> None:
    raw = b'{"Action":"run","Package":"example.com/p","Test":"TestNoTime"}\n'
    raw += b'{"Action":"pass","Package":"example.com/p","Test":"TestNoTime"}\n'
    raw += b'{"Time":"2026-10-28T00:00:01Z","Action":"fail","Package":"example.com/p","Test":"TestNoRun"}\n'
    records, gaps, display = convert(raw)
    assert "go-test-attempt-missing-time" in gaps
    assert "go-test-attempt-without-run" in gaps
    no_time = by_test(records, display, "TestNoTime")[0]
    assert "duration_ns" not in no_time and "source_started_at" not in no_time


@pytest.mark.parametrize(
    "raw",
    [
        b'{"Action":"teleport","Package":"p"}',
        b'{"Action":"run","Package":1}',
        b'{"Action":"run","Package":"p","Time":"not-a-time"}',
        b"[1,2]",
    ],
)
def test_malformed_streams_fail_with_constant_errors(raw: bytes) -> None:
    with pytest.raises(AdapterError):
        parse_go_test_stream(raw=raw)


@pytest.mark.skipif(shutil.which("go") is None, reason="real go toolchain not available")
def test_real_go_test_run_with_repetition_and_failure() -> None:
    """Real `go test -json -count=2` against the copied tiny suite."""

    work = Path(tempfile.mkdtemp(prefix="ci-native-gotest-", dir="/tmp/opencode"))
    try:
        suite_dir = work / "gotiny"
        shutil.copytree(FIXTURES / "gotiny", suite_dir)
        # A skip case the tiny suite does not ship; scratch only.
        skip_source = (
            'package gotiny\n\nimport "testing"\n\n'
            'func TestSkipAlways(t *testing.T) {\n\tt.Skip("skipped on purpose")\n}\n'
        )
        (suite_dir / "skip_scratch_test.go").write_text(skip_source, encoding="utf-8")
        completed = subprocess.run(
            ["go", "test", "-json", "-count=2", "./..."],
            cwd=suite_dir,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert completed.returncode == 1
        records, gaps, display = convert(completed.stdout.encode(), child_exit_code=completed.returncode)
        assert gaps == []
        passing = by_test(records, display, "TestPassAlways")[0]
        failing = by_test(records, display, "TestFailAlways")[0]
        skipped = by_test(records, display, "TestSkipAlways")[0]
        assert passing["attempt_count"] == 2 and passing["outcome"] == "passed"
        assert failing["attempt_count"] == 2 and failing["outcome"] == "failed"
        assert skipped["attempt_count"] == 2 and skipped["outcome"] == "skipped"
        failing_attempts = [
            data for data in by_test(records, display, "TestFailAlways") if data["result_scope"] == "attempt"
        ]
        assert [attempt["attempt_index"] for attempt in failing_attempts] == [1, 2]
        assert records[-2]["data"]["outcome"] == "failed"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@pytest.mark.skipif(shutil.which("go") is None, reason="real go toolchain not available")
def test_real_cached_run_retains_outcomes_without_measured_attempts() -> None:
    """Real `go test -json` twice: the second run replays cached results,
    which must not become measured executions."""

    work = Path(tempfile.mkdtemp(prefix="ci-native-gotest2-", dir="/tmp/opencode"))
    try:
        (work / "go.mod").write_text("module example.com/scratch\n\ngo 1.25\n", encoding="utf-8")
        passing = work / "passing"
        passing.mkdir()
        (passing / "passing_test.go").write_text(
            'package passing\n\nimport "testing"\n\nfunc TestPass(t *testing.T) {}\n', encoding="utf-8"
        )
        first = subprocess.run(
            ["go", "test", "-json", "./passing"], cwd=work, capture_output=True, text=True, timeout=300, check=False
        )
        second = subprocess.run(
            ["go", "test", "-json", "./passing"], cwd=work, capture_output=True, text=True, timeout=300, check=False
        )
        records_first, gaps_first, display_first = convert(first.stdout.encode(), child_exit_code=first.returncode)
        assert gaps_first == []
        live = by_test(records_first, display_first, "TestPass")[0]
        assert live["attempt_count"] == 1
        assert "duration_ns" in live

        records_second, gaps_second, display_second = convert(second.stdout.encode(), child_exit_code=second.returncode)
        # The cached run re-executed nothing: replayed outcomes stay as
        # attributed previous results, with no invented attempt, span,
        # or source interval.
        assert "go-test-package-cached" in gaps_second
        cached = by_test(records_second, display_second, "TestPass")
        assert len(cached) == 1
        assert cached[0]["outcome"] == "passed"
        assert cached[0]["attempt_count"] is None
        assert "duration_ns" not in cached[0]
        assert "source_started_at" not in cached[0]
        assert "source_ended_at" not in cached[0]
    finally:
        shutil.rmtree(work, ignore_errors=True)


@pytest.mark.skipif(shutil.which("go") is None, reason="real go toolchain not available")
def test_real_output_text_mentioning_cached_keeps_the_measured_attempt() -> None:
    """A real `go test -json` run whose test body prints the cache
    marker as ordinary output: the package really executed, so a
    measured attempt must remain. Only the genuine package-level Cached
    field or the full package-result marker is a cache signal."""

    work = Path(tempfile.mkdtemp(prefix="ci-native-gotest3-", dir="/tmp/opencode"))
    try:
        (work / "go.mod").write_text("module example.com/scratch\n\ngo 1.25\n", encoding="utf-8")
        noisy = work / "noisy"
        noisy.mkdir()
        (noisy / "noisy_test.go").write_text(
            'package noisy\n\nimport "testing"\n\n'
            "func TestMentionsCached(t *testing.T) {\n"
            '\tt.Log("test output mentions (cached)")\n'
            "}\n",
            encoding="utf-8",
        )
        completed = subprocess.run(
            ["go", "test", "-json", "./noisy"], cwd=work, capture_output=True, text=True, timeout=300, check=False
        )
        assert completed.returncode == 0
        records, gaps, _ = convert(completed.stdout.encode(), child_exit_code=completed.returncode)
        # The package executed for real: no cached-package gap.
        assert gaps == []
        assert "go-test-package-cached" not in gaps
        results = [record["data"] for record in records if record["event"] == "test_result"]
        measured = next(data for data in results if data["outcome"] == "passed")
        assert measured["attempt_count"] == 1
        assert "duration_ns" in measured
        # The raw output text never enters records.
        assert "test output mentions" not in json.dumps(records)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_measured_nonzero_child_exit_cannot_label_an_invocation_passed() -> None:
    raw = b'{"Time":"2026-10-28T00:00:00Z","Action":"start","Package":"example.com/p"}\n'
    raw += (
        b'{"Time":"2026-10-28T00:00:01Z","Action":"run","Package":"example.com/p","Test":"TestOk"}\n'
        b'{"Time":"2026-10-28T00:00:02Z","Action":"pass","Package":"example.com/p","Test":"TestOk","Elapsed":0.1}\n'
        b'{"Time":"2026-10-28T00:00:03Z","Action":"pass","Package":"example.com/p","Elapsed":0.2}\n'
    )
    records, gaps, _ = convert(raw, child_exit_code=1)
    # The measured attempt and its outcome stand; only the invocation
    # claim is corrected, with a fixed gap.
    assert gaps == ["go-test-exit-contradicts-report"]
    results = [record["data"] for record in records if record["event"] == "test_result"]
    assert all(data["outcome"] == "passed" for data in results)
    assert records[-2]["data"]["outcome"] == "failed"
    assert records[-2]["data"]["exit_code"] == 1
