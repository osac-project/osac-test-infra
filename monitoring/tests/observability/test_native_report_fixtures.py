"""Sanity checks for the native report fixtures.

Every fixture in examples/native/ comes from a real framework run (see the
provenance README next to them). These checks pin the structural facts the
W03 adapters rely on, exactly as the frameworks emit them. They are not
adapter implementations.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree

from contract_helpers import NATIVE_DIR, load_journal, load_json


def test_pytest_junit_fixture_carries_the_real_outcome_shapes() -> None:
    root = ElementTree.parse(NATIVE_DIR / "pytest-junit.xml").getroot()
    cases = root.findall(".//testcase")
    assert len(cases) == 5
    skipped = root.findall(".//testcase/skipped")
    # A skip and an expected failure both appear as <skipped>, told apart
    # only by the type attribute.
    assert {s.get("type") for s in skipped} == {"pytest.skip", "pytest.xfail"}
    # An unexpected pass (xpass without xfail_strict) is indistinguishable
    # from a plain pass in JUnit XML: no marker element exists for it.
    unexpected_pass = [c for c in cases if c.get("name") == "test_unexpected_pass"]
    assert len(unexpected_pass) == 1
    assert len(list(unexpected_pass[0])) == 0


def test_ginkgo_fixture_matches_the_real_report_shape() -> None:
    suites = load_json(NATIVE_DIR / "ginkgo.json")
    assert len(suites) == 1
    suite = suites[0]
    assert suite["SuiteSucceeded"] is False
    assert suite["PreRunStats"] == {"TotalSpecs": 3, "SpecsThatWillRun": 3}
    specs = suite["SpecReports"]
    assert [spec["State"] for spec in specs] == ["passed", "failed", "skipped"]
    # The fields the first review found invented do not exist in the real
    # format; the true names are the hierarchy and leaf text.
    for spec in specs:
        assert "FullTestText" not in spec
        assert "Attempt" not in spec
    failing = specs[1]
    # Retries are not duplicate SpecReports: the attempt count lives in
    # NumAttempts, earlier failures in AdditionalFailures, and the retry
    # boundaries in SpecEvents.
    assert failing["NumAttempts"] == 2
    assert failing["AdditionalFailures"][0]["Failure"]["Message"].startswith("Failure recorded during attempt 1")
    assert any(event["SpecEventType"] == "Retry" for event in failing["SpecEvents"])
    assert failing["ContainerHierarchyTexts"] == ["ci observability fixture"]
    assert failing["LeafNodeText"] == "fails on purpose"
    skipped = specs[2]
    assert skipped["Failure"]["Message"] == "skipped on purpose"


def test_go_test_fixture_matches_the_real_stream_shape() -> None:
    events = load_journal(NATIVE_DIR / "go-test.jsonl")
    actions = {event["Action"] for event in events}
    assert {"start", "build-output", "build-fail", "run", "output", "pause", "cont", "pass", "fail", "skip"} <= actions
    # Build output uses ImportPath and carries no Time; test events do.
    build_outputs = [event for event in events if event["Action"] == "build-output"]
    assert build_outputs
    assert all("ImportPath" in event and "Time" not in event for event in build_outputs)
    # A failed build marks the package-level fail with FailedBuild.
    package_fail = next(event for event in events if event["Action"] == "fail" and "FailedBuild" in event)
    assert package_fail["FailedBuild"].endswith(".test")
    # Test-level events name their test; package conclusions do not.
    test_events = [event for event in events if "Test" in event]
    assert {event["Action"] for event in test_events} >= {"run", "pass", "fail", "skip"}
    assert any(event["Action"] == "fail" and "Test" not in event for event in events)


def test_vitest_fixture_matches_the_real_4_1_9_report_shape() -> None:
    report = load_json(NATIVE_DIR / "vitest.json")
    cases = [case for suite in report["testResults"] for case in suite["assertionResults"]]
    assert len(cases) == report["numTotalTests"]
    assert sum(case["status"] == "passed" for case in cases) == report["numPassedTests"]
    assert sum(case["status"] == "failed" for case in cases) == report["numFailedTests"]
    # Vitest 4.1.9 reports status 'skipped'; the pending counter still
    # names it numPendingTests.
    assert sum(case["status"] == "skipped" for case in cases) == report["numPendingTests"]
    assert not any(case["status"] == "pending" for case in cases)
    failed = next(case for case in cases if case["status"] == "failed")
    assert failed["ancestorTitles"]
    assert failed["fullName"]
    assert failed["failureMessages"]
    # Every spec file path was sanitized to a repo-relative form.
    assert all("/tmp/" not in suite["name"] for suite in report["testResults"])


def test_ansible_fixture_is_explicitly_unavailable() -> None:
    marker = NATIVE_DIR / "ansible-target-report.UNAVAILABLE.md"
    assert marker.is_file()
    assert not (NATIVE_DIR / "ansible-target.json").exists()
    assert "no built-in JSON result report" in marker.read_text(encoding="utf-8")
