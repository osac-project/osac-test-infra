"""The /api/ci-observe/run endpoint of workflow-exporter.py: capture-id
parsing, run evidence from the SQLite job history, and the native JUnit
lookup against the run's osac-logs-[<suite>-]full-install-<run id> artifact
(missing, expired, too large, invalid, and the cached happy path). The
last test is the backend half of the dashboard contract: it resolves the
real endpoint response against the CI evidence row of the Capture
dashboard."""

from __future__ import annotations

import io
import json
import threading
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from helpers import FakeResponse, make_job

MONITORING = Path(__file__).resolve().parents[1]
RUN_ID = 41310072671
CAPTURE = f"vmaas-main-{RUN_ID}-a1"
ARTIFACT_NAME = f"osac-logs-full-install-{RUN_ID}"
# The run-scoped artifact listing (not the repo-wide ?name= filter, which
# cannot express the suite-qualified name pattern).
LIST_PATH = f"/repos/osac-project/osac/actions/runs/{RUN_ID}/artifacts?per_page=100"
DOWNLOAD_PATH = f"/repos/osac-project/osac/actions/artifacts/42/zip"
RUN_PATH = f"/repos/osac-project/osac/actions/runs/{RUN_ID}"
ARTIFACT_URL = f"https://github.com/osac-project/osac/actions/runs/{RUN_ID}/artifacts/42"
TOKEN = "secret-token-do-not-leak"  # the value the shared `evidence` fixture sets

VALID_JUNIT = b"""<?xml version="1.0" encoding="utf-8"?>
<testsuites name="e2e" tests="4" failures="1" errors="1" skipped="1" time="12.5">
  <testsuite name="tests/e2e/vmaas" tests="4" failures="1" errors="1" skipped="1" time="12.5">
    <testcase classname="tests/e2e/vmaas" name="test_pass" time="1.5"/>
    <testcase classname="tests/e2e/vmaas" name="test_fail" time="2.5">
      <failure type="AssertionError" message="assert 1 == 2">Traceback line 1
Traceback line 2</failure>
    </testcase>
    <testcase classname="tests/e2e/vmaas" name="test_error" time="0.5">
      <error type="TimeoutError" message="timed out after 30s"/>
    </testcase>
    <testcase classname="tests/e2e/vmaas" name="test_skip" time="0.01">
      <skipped type="pytest.skip" message="no cluster"/>
    </testcase>
  </testsuite>
</testsuites>
"""


def run_row(**overrides):
    """The run's own stored row (id is the run id), as a direct full-install
    workflow run produces."""
    row = make_job(
        id=RUN_ID,
        workflow="E2E VMaaS Full Install",
        display_name="osac / E2E VMaaS Full Install",
        category="e2e",
        event="schedule",
        conclusion="failure",
        status="failure",
        duration_s=5400,
        runner_name="box-runner-01",
        url=f"https://github.com/osac-project/osac/actions/runs/{RUN_ID}",
        created_at="2026-10-10T10:00:00Z",
        run_attempt=1,
    )
    row.update(overrides)
    return row


def suite_row(job_id, run_id=RUN_ID, **overrides):
    """A pull-request-flow suite row: id is a job id, url is the job's
    .../actions/runs/<run id>/job/<job id> link (the only persisted trace
    of the parent run)."""
    row = make_job(
        id=job_id,
        workflow="E2E vmaas (pull requests)",
        display_name="osac / E2E vmaas (pull requests)",
        category="e2e",
        event="pull_request",
        pr_display="#42",
        url=f"https://github.com/osac-project/osac/actions/runs/{run_id}/job/{job_id}",
        run_attempt=1,
    )
    row.update(overrides)
    return row


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def artifact_entry(api_url: str, *, art_id=42, name=ARTIFACT_NAME, expired=False, size=1024) -> dict:
    """One artifact object as the run-scoped listing returns it."""
    return {
        "id": art_id,
        "name": name,
        "size_in_bytes": size,
        "expired": expired,
        "expires_at": "2026-10-17T10:00:00Z",
        "archive_download_url": f"{api_url}/repos/osac-project/osac/actions/artifacts/{art_id}/zip",
    }


def artifact_payload(
    api_url: str, *, expired=False, size=1024, art_id=42, name=ARTIFACT_NAME, extra_artifacts=()
) -> FakeResponse:
    artifacts = [artifact_entry(api_url, art_id=art_id, name=name, expired=expired, size=size)]
    artifacts.extend(extra_artifacts)
    return FakeResponse(payload={"total_count": len(artifacts), "artifacts": artifacts})


def seed_direct_run(exporter, **overrides):
    exporter._upsert_job(run_row(**overrides))


class TestParseCaptureId:
    @pytest.mark.parametrize(
        ("capture_id", "parsed", "run_id", "attempt"),
        [
            ("vmaas-main-41310072671-a1", "exact", 41310072671, 1),
            ("caas-pr42-9876543210-a3", "exact", 9876543210, 3),
            ("run-1234567890-attempt-2", "exact", 1234567890, 2),
            ("41310072671", "inferred", 41310072671, None),
            ("vmaas-main-41310072671", "invalid", None, None),
            ("vmaas-main-41310072671-a", "invalid", None, None),
            ("vmaas-main-abc-a1", "invalid", None, None),
            ("VMaaS-main-41310072671-a1", "invalid", None, None),
            ("run-123-attempt", "invalid", None, None),
            ("garbage", "invalid", None, None),
            ("", "invalid", None, None),
        ],
    )
    def test_formats(self, wfe, capture_id, parsed, run_id, attempt):
        assert wfe.WorkflowExporter._parse_capture_id(capture_id) == (parsed, run_id, attempt)


def test_a_step_result_comes_from_the_job_timeline(evidence, github, wfe):
    """Step durations are stored without a result; the job timeline has one
    per step. Without it the Result column could only say failed or nothing."""
    timeline = [{
        "name": "e2e", "created_at": "2026-04-17T10:00:00Z",
        "steps": [
            {"name": "Set up job", "conclusion": "success",
             "started_at": "2026-04-17T10:00:05Z", "completed_at": "2026-04-17T10:00:06Z"},
            {"name": "Authorize fork PR", "conclusion": "skipped",
             "started_at": "2026-04-17T10:00:06Z", "completed_at": "2026-04-17T10:00:06Z"},
            {"name": "Run E2E tests", "conclusion": "failure",
             "started_at": "2026-04-17T10:00:07Z", "completed_at": "2026-04-17T10:08:27Z"},
        ],
    }]
    seed_direct_run(
        evidence,
        steps=[{"name": "Set up job", "duration_s": 1}, {"name": "Authorize fork PR", "duration_s": 0},
               {"name": "Run E2E tests", "duration_s": 500}, {"name": "Teardown", "duration_s": 30}],
        failed_step="e2e → Run E2E tests",
        timeline_json=json.dumps(timeline),
    )
    github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))
    github.add(LIST_PATH, artifact_payload(wfe.API_URL))
    github.add(DOWNLOAD_PATH, FakeResponse(content=zip_bytes({"junit.xml": VALID_JUNIT})))

    doc = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})

    assert [(step["step_name"], step["conclusion"]) for step in doc["steps"]] == [
        ("Set up job", "success"), ("Authorize fork PR", "skipped"), ("Run E2E tests", "failure"),
        ("Teardown", None),
    ]


def test_direct_run_evidence(evidence, github, wfe):
    seed_direct_run(
        evidence,
        steps=[{"name": "Run E2E tests", "duration_s": 500}],
        failed_step="e2e → Run E2E tests",
        failure_reason="test",
    )
    github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))
    github.add(LIST_PATH, artifact_payload(wfe.API_URL))
    github.add(DOWNLOAD_PATH, FakeResponse(content=zip_bytes({"junit.xml": VALID_JUNIT})))

    doc = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})

    assert doc["parsed"] == "exact"
    assert (doc["run_id"], doc["run_attempt"]) == (RUN_ID, 1)
    assert doc["run"]["repo"] == "osac"
    assert doc["run"]["workflow"] == "E2E VMaaS Full Install"
    assert doc["run"]["conclusion"] == "failure"
    assert doc["run"]["duration_s"] == 5400
    assert doc["run"]["runner_name"] == "box-runner-01"
    assert doc["run"]["head_sha"] == "a" * 40
    assert doc["run"]["run_attempt"] == 1
    assert doc["run"]["url"] == f"https://github.com/osac-project/osac/actions/runs/{RUN_ID}"
    assert [j["name"] for j in doc["jobs"]] == ["osac / E2E VMaaS Full Install"]
    assert doc["jobs"][0]["failure_reason"] == "test"
    assert doc["jobs"][0]["cause_label"] == "Tests failed", "the cause Why runs fail gives the same failure"
    # The job name opens its suite's Where the time goes page.
    assert doc["jobs"][0]["history_url"].startswith("/d/ci-observe-time/")
    assert "var-suite=vmaas" in doc["jobs"][0]["history_url"]
    assert "var-repo=osac" in doc["jobs"][0]["history_url"]
    assert doc["steps"] == [
        {
            "job_name": "osac / E2E VMaaS Full Install",
            "step_name": "Run E2E tests",
            "duration_s": 500,
            "conclusion": "failure",
        }
    ]


def test_multiple_suite_rows_are_all_returned(evidence, github, wfe):
    evidence._upsert_job(
        make_job(
            id=RUN_ID,
            workflow="E2E for pull requests",
            display_name="osac / E2E for pull requests",
            category="automation",
            conclusion="success",
            url=f"https://github.com/osac-project/osac/actions/runs/{RUN_ID}",
        )
    )
    evidence._upsert_job(
        suite_row(
            101,
            conclusion="failure",
            failure_reason="test",
            failed_step="vmaas / e2e-vmaas-full-install / e2e → Run E2E tests",
            steps=[{"name": "Run E2E tests", "duration_s": 900}],
        )
    )
    evidence._upsert_job(
        suite_row(
            102,
            workflow="E2E caas (pull requests)",
            display_name="osac / E2E caas (pull requests)",
            conclusion="success",
            steps=[{"name": "Run CaaS e2e tests", "duration_s": 100}],
        )
    )
    github.add(RUN_PATH, FakeResponse(payload={"head_sha": "b" * 40}))

    doc = evidence.get_ci_observe_run_json({"capture_id": [f"vmaas-pr42-{RUN_ID}-a1"]})

    assert len(doc["jobs"]) == 3  # the run's own row plus both suite rows
    assert {j["workflow"] for j in doc["jobs"]} == {
        "E2E for pull requests",
        "E2E vmaas (pull requests)",
        "E2E caas (pull requests)",
    }
    assert {s["job_name"] for s in doc["steps"]} == {
        "osac / E2E vmaas (pull requests)",
        "osac / E2E caas (pull requests)",
    }
    assert doc["run"]["pr_display"] == "#42"
    assert doc["run"]["conclusion"] == "failure"  # a failed suite fails the summary
    assert any("2 suite rows" in note for note in doc["coverage"]["notes"])


def test_rerun_attempt_falls_back_to_suite_rows(evidence, github):
    # The run's own row was overwritten by attempt 2; the capture is a1.
    evidence._upsert_job(run_row(run_attempt=2, conclusion="success"))
    evidence._upsert_job(suite_row(101, conclusion="failure", failure_reason="test"))

    doc = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})

    assert doc["run"]["conclusion"] == "failure"  # from the a1 suite row
    assert [j["workflow"] for j in doc["jobs"]] == ["E2E vmaas (pull requests)"]
    notes = doc["coverage"]["notes"]
    assert any("latest attempt" in note for note in notes)
    assert any("overwrite the artifact" in note for note in notes)


def test_bare_run_id_shows_latest_attempt(evidence, github):
    evidence._upsert_job(suite_row(101, run_attempt=1, conclusion="success"))
    evidence._upsert_job(suite_row(102, run_attempt=2, conclusion="failure"))

    doc = evidence.get_ci_observe_run_json({"capture_id": [str(RUN_ID)]})

    assert doc["parsed"] == "inferred"
    assert doc["run_attempt"] == 2
    assert len(doc["jobs"]) == 1
    assert any("latest stored attempt" in note for note in doc["coverage"]["notes"])


def test_run_not_found(evidence):
    doc = evidence.get_ci_observe_run_json({"capture_id": ["vmaas-main-99999999999-a1"]})

    assert doc["parsed"] == "exact"
    assert doc["run"] is None and doc["jobs"] == [] and doc["steps"] == []
    assert doc["native"]["status"] == "not_requested"
    assert any("not in the exporter's job history" in n for n in doc["coverage"]["notes"])


def test_invalid_and_missing_capture_id(evidence):
    invalid = evidence.get_ci_observe_run_json({"capture_id": ["vmaas-main-abc-a1"]})
    assert invalid["parsed"] == "invalid"
    assert invalid["run_id"] is None
    assert invalid["native"]["status"] == "not_requested"

    empty = evidence.get_ci_observe_run_json({})
    assert empty["parsed"] == "invalid"
    assert empty["capture_id"] == ""
    assert any("missing or empty" in n for n in empty["coverage"]["notes"])


class TestNative:
    def route_available(self, github, wfe, junit=VALID_JUNIT, extra_files=None):
        github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))
        github.add(LIST_PATH, artifact_payload(wfe.API_URL))
        files = {"junit.xml": junit}
        files.update(extra_files or {})
        github.add(DOWNLOAD_PATH, FakeResponse(content=zip_bytes(files)))

    def test_available(self, evidence, github, wfe):
        seed_direct_run(evidence)
        self.route_available(github, wfe)

        native = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})["native"]

        assert native["status"] == "available"
        assert native["artifact_name"] == ARTIFACT_NAME
        assert native["artifact_url"] == ARTIFACT_URL
        assert (native["tests"], native["failures"], native["errors"], native["skipped"], native["time_s"]) == (
            4,
            1,
            1,
            1,
            12.5,
        )
        assert [t["name"] for t in native["failed_tests"]] == ["test_fail", "test_error"]
        failure = native["failed_tests"][0]
        assert failure == {
            "classname": "tests/e2e/vmaas",
            "name": "test_fail",
            "type": "AssertionError",
            "message": "assert 1 == 2",
            "time_s": 2.5,
            "artifact_url": ARTIFACT_URL,
        }
        assert "Traceback" not in json.dumps(native)  # no raw stack traces

    def test_multiple_junit_files_are_summed(self, evidence, github, wfe):
        second = (
            b'<testsuites><testsuite name="xdist" tests="2" failures="1" time="3.5">'
            b'<testcase classname="t" name="test_x" time="1.0">'
            b'<failure type="AssertionError" message="boom"/></testcase>'
            b'<testcase classname="t" name="test_y" time="2.5"/>'
            b"</testsuite></testsuites>"
        )
        seed_direct_run(evidence)
        self.route_available(github, wfe, extra_files={"junit-xdist1.xml": second, "logs.txt": b"ignored"})

        native = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})["native"]

        assert native["status"] == "available"
        assert (native["tests"], native["failures"], native["time_s"]) == (6, 2, 16.0)

    def test_missing(self, evidence, github, wfe):
        seed_direct_run(evidence)
        github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))
        github.add(LIST_PATH, FakeResponse(payload={"total_count": 0, "artifacts": []}))

        native = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})["native"]

        assert native["status"] == "missing"
        assert native["tests"] is None  # never a green zero
        assert f"full-install-{RUN_ID}" in native["notes"][0]

    @pytest.mark.parametrize("suite", ["bmaas", "caas"])
    def test_suite_qualified_artifact_name(self, evidence, github, wfe, suite):
        """BMaaS and CaaS insert the suite in the artifact name; those runs
        are native evidence too, not a permanent "missing"."""
        seed_direct_run(evidence)
        github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))
        github.add(LIST_PATH, artifact_payload(wfe.API_URL, name=f"osac-logs-{suite}-full-install-{RUN_ID}"))
        github.add(DOWNLOAD_PATH, FakeResponse(content=zip_bytes({"junit.xml": VALID_JUNIT})))

        native = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})["native"]

        assert native["status"] == "available"
        assert native["artifact_name"] == f"osac-logs-{suite}-full-install-{RUN_ID}"

    def test_wrong_run_and_arbitrary_artifacts_are_missing(self, evidence, github, wfe):
        seed_direct_run(evidence)
        github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))
        github.add(
            LIST_PATH,
            artifact_payload(
                wfe.API_URL,
                name=f"osac-logs-full-install-{RUN_ID + 1}",
                extra_artifacts=[
                    # Another run's suite-qualified name, and an artifact of
                    # this run that is not a full-install log bundle.
                    artifact_entry(wfe.API_URL, art_id=43, name=f"osac-logs-bmaas-full-install-{RUN_ID + 1}"),
                    artifact_entry(wfe.API_URL, art_id=44, name=f"ci-observe-capture-{RUN_ID}"),
                ],
            ),
        )
        github.add(DOWNLOAD_PATH, FakeResponse(content=zip_bytes({"junit.xml": VALID_JUNIT})))

        native = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})["native"]

        assert native["status"] == "missing"
        assert github.count(DOWNLOAD_PATH) == 0  # nothing was downloaded

    def test_multiple_matches_prefer_the_plain_name(self, evidence, github, wfe):
        seed_direct_run(evidence)
        github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))
        github.add(
            LIST_PATH,
            artifact_payload(
                wfe.API_URL,
                extra_artifacts=[artifact_entry(wfe.API_URL, art_id=43, name=f"osac-logs-bmaas-full-install-{RUN_ID}")],
            ),
        )
        github.add(DOWNLOAD_PATH, FakeResponse(content=zip_bytes({"junit.xml": VALID_JUNIT})))

        native = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})["native"]

        assert native["status"] == "available"
        assert native["artifact_name"] == ARTIFACT_NAME  # the plain name won
        assert any("2 artifacts matched" in n for n in native["notes"])

    def test_multiple_matches_break_ties_lexically(self, evidence, github, wfe):
        seed_direct_run(evidence)
        github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))
        github.add(
            LIST_PATH,
            artifact_payload(
                wfe.API_URL,
                art_id=44,
                name=f"osac-logs-caas-full-install-{RUN_ID}",
                extra_artifacts=[artifact_entry(wfe.API_URL, art_id=43, name=f"osac-logs-bmaas-full-install-{RUN_ID}")],
            ),
        )
        # Only the lexically-first match's download is routed (art_id 43);
        # picking the caas one (44) would hit an unrouted URL and fail.
        github.add(
            f"/repos/osac-project/osac/actions/artifacts/43/zip",
            FakeResponse(content=zip_bytes({"junit.xml": VALID_JUNIT})),
        )

        native = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})["native"]

        assert native["status"] == "available"
        assert native["artifact_name"] == f"osac-logs-bmaas-full-install-{RUN_ID}"

    def test_live_artifact_preferred_over_expired(self, evidence, github, wfe):
        seed_direct_run(evidence)
        github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))
        github.add(
            LIST_PATH,
            artifact_payload(
                wfe.API_URL,
                expired=True,
                extra_artifacts=[artifact_entry(wfe.API_URL, art_id=43, name=f"osac-logs-bmaas-full-install-{RUN_ID}")],
            ),
        )
        github.add(
            f"/repos/osac-project/osac/actions/artifacts/43/zip",
            FakeResponse(content=zip_bytes({"junit.xml": VALID_JUNIT})),
        )

        native = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})["native"]

        assert native["status"] == "available"
        assert native["artifact_name"] == f"osac-logs-bmaas-full-install-{RUN_ID}"

    def test_suite_named_artifact_without_junit(self, evidence, github, wfe):
        seed_direct_run(evidence)
        github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))
        github.add(LIST_PATH, artifact_payload(wfe.API_URL, name=f"osac-logs-bmaas-full-install-{RUN_ID}"))
        github.add(DOWNLOAD_PATH, FakeResponse(content=zip_bytes({"e2e.log": b"logs"})))

        native = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})["native"]

        assert native["status"] == "invalid"
        assert "no junit.xml" in native["notes"][0]

    def test_expired(self, evidence, github, wfe):
        seed_direct_run(evidence)
        github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))
        github.add(LIST_PATH, artifact_payload(wfe.API_URL, expired=True))

        native = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})["native"]

        assert native["status"] == "expired"
        assert any("30-day retention" in n for n in native["notes"])
        assert native["artifact_url"] == ARTIFACT_URL

    def test_too_large_without_download(self, evidence, github, wfe):
        seed_direct_run(evidence)
        github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))
        github.add(LIST_PATH, artifact_payload(wfe.API_URL, size=wfe.WorkflowExporter.NATIVE_MAX_DOWNLOAD_BYTES + 1))

        native = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})["native"]

        assert native["status"] == "too_large"
        assert github.count(DOWNLOAD_PATH) == 0  # never downloaded at all

    def test_download_bounded_mid_stream(self, evidence, github, wfe, monkeypatch):
        monkeypatch.setattr(wfe.WorkflowExporter, "NATIVE_MAX_DOWNLOAD_BYTES", 1024)
        seed_direct_run(evidence)
        github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))
        github.add(LIST_PATH, artifact_payload(wfe.API_URL))
        github.add(DOWNLOAD_PATH, FakeResponse(content=b"x" * 4096))

        native = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})["native"]

        assert native["status"] == "too_large"

    def test_invalid_zip(self, evidence, github, wfe):
        seed_direct_run(evidence)
        github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))
        github.add(LIST_PATH, artifact_payload(wfe.API_URL))
        github.add(DOWNLOAD_PATH, FakeResponse(content=b"this is not a zip archive"))

        native = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})["native"]

        assert native["status"] == "invalid"
        assert "not a valid zip" in native["notes"][0]

    def test_zip_without_junit(self, evidence, github, wfe):
        seed_direct_run(evidence)
        github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))
        github.add(LIST_PATH, artifact_payload(wfe.API_URL))
        github.add(DOWNLOAD_PATH, FakeResponse(content=zip_bytes({"e2e.log": b"logs"})))

        native = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})["native"]

        assert native["status"] == "invalid"
        assert "no junit.xml" in native["notes"][0]

    def test_invalid_junit_xml(self, evidence, github, wfe):
        seed_direct_run(evidence)
        self.route_available(github, wfe, junit=b"<testsuites><testsuite ")

        native = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})["native"]

        assert native["status"] == "invalid"
        assert "not valid XML" in native["notes"][0]

    def test_message_bounded_and_control_chars_stripped(self, evidence, github, wfe):
        # \x01/\x02 are illegal anywhere in XML 1.0 (the pre-parse clean
        # removes them so the file still parses); \x7f is legal XML but is
        # still stripped from the displayed message.
        junit = (
            b'<testsuites><testsuite name="t" tests="1" failures="1" time="1.0">'
            b'<testcase classname="t" name="test_long" time="1.0">'
            b'<failure type="AssertionError" message="boom\x01\x02\x7f' + b"x" * 5000 + b'"/>'
            b"</testcase></testsuite></testsuites>"
        )
        seed_direct_run(evidence)
        self.route_available(github, wfe, junit=junit)

        native = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})["native"]

        assert native["status"] == "available"  # control chars did not kill the parse
        message = native["failed_tests"][0]["message"]
        assert len(message) == wfe.WorkflowExporter.NATIVE_MAX_MESSAGE_CHARS
        for char in ("\x01", "\x02", "\x7f"):
            assert char not in message
        assert any("truncated" in n for n in native["notes"])

    def test_failed_tests_capped(self, evidence, github, wfe):
        cases = "".join(
            f'<testcase classname="t" name="test_{i}" time="0.1">'
            f'<failure type="AssertionError" message="boom"/></testcase>'
            for i in range(150)
        )
        junit = (
            f'<testsuites><testsuite name="t" tests="150" failures="150" time="15.0">{cases}</testsuite></testsuites>'
        ).encode()
        seed_direct_run(evidence)
        self.route_available(github, wfe, junit=junit)

        native = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})["native"]

        assert len(native["failed_tests"]) == wfe.WorkflowExporter.NATIVE_MAX_FAILED_TESTS
        assert any("only the first" in n for n in native["notes"])

    def test_non_finite_time_attribute(self, evidence, github, wfe):
        """inf times get the missing-value sentinel (None / 0), never a bare
        Infinity in the JSON."""
        junit = (
            b'<testsuites><testsuite name="t" tests="1" failures="1" time="inf">'
            b'<testcase classname="t" name="test_x" time="1e400">'
            b'<failure type="AssertionError" message="boom"/></testcase>'
            b"</testsuite></testsuites>"
        )
        seed_direct_run(evidence)
        self.route_available(github, wfe, junit=junit)

        native = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})["native"]

        assert native["status"] == "available"
        assert native["time_s"] == 0  # the non-finite suite time contributed nothing
        assert native["failed_tests"][0]["time_s"] is None
        assert "Infinity" not in json.dumps(native) and "NaN" not in json.dumps(native)

    @pytest.mark.parametrize("count", ["inf", "1e400", "nan"])
    def test_non_finite_count_attribute(self, evidence, github, wfe, count):
        """A crafted tests="inf" must not raise out of the endpoint
        (int(float("inf")) is an OverflowError); the count falls back to
        counting the <testcase> children instead."""
        junit = (
            f'<testsuites><testsuite name="t" tests="{count}" failures="1" time="1.0">'
            f'<testcase classname="t" name="test_x" time="1.0">'
            f'<failure type="AssertionError" message="boom"/></testcase>'
            f"</testsuite></testsuites>"
        ).encode()
        seed_direct_run(evidence)
        self.route_available(github, wfe, junit=junit)

        doc = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})

        assert doc["native"]["status"] == "available"
        assert doc["native"]["tests"] == 1  # fell back to the testcase count
        assert doc["native"]["failures"] == 1

    def test_no_token_skips_lookup(self, exporter, github):
        seed_direct_run(exporter)  # exporter fixture has no token

        doc = exporter.get_ci_observe_run_json({"capture_id": [CAPTURE]})

        assert doc["run"]["workflow"] == "E2E VMaaS Full Install"  # job data still served
        assert doc["native"]["status"] == "not_requested"
        assert any("no GitHub token" in n for n in doc["native"]["notes"])
        assert github.calls == []

    def test_lookup_failure_is_reported_not_raised(self, evidence, github, wfe):
        import requests

        seed_direct_run(evidence)
        github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))
        github.add(LIST_PATH, requests.exceptions.ConnectionError())

        doc = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})

        assert doc["run"]["repo"] == "osac"  # the response did not fail
        assert doc["native"]["status"] == "not_requested"
        assert any("network error" in n for n in doc["native"]["notes"])


class TestCacheAndSecrets:
    def test_available_result_is_cached(self, evidence, github, wfe):
        seed_direct_run(evidence)
        github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))
        github.add(LIST_PATH, artifact_payload(wfe.API_URL))
        github.add(DOWNLOAD_PATH, FakeResponse(content=zip_bytes({"junit.xml": VALID_JUNIT})))

        first = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})
        second = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})

        assert first["native"] == second["native"]
        assert github.count(LIST_PATH) == 1
        assert github.count(DOWNLOAD_PATH) == 1

    def test_failed_lookup_is_cached_briefly(self, evidence, github, wfe):
        seed_direct_run(evidence)
        github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))
        github.add(LIST_PATH, FakeResponse(payload={"total_count": 0, "artifacts": []}))

        evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})
        evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})
        assert github.count(LIST_PATH) == 1  # brief cache absorbs the repeat

        # ... and expires, so a genuinely new state is picked up.
        clock = {"now": 1000.0}
        real_monotonic = wfe.time.monotonic
        wfe.time.monotonic = lambda: clock["now"]
        try:
            clock["now"] = real_monotonic() + wfe.WorkflowExporter.NATIVE_FAILURE_CACHE_TTL_SECONDS + 1
            evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})
        finally:
            wfe.time.monotonic = real_monotonic
        assert github.count(LIST_PATH) == 2

    def test_head_sha_fetch_is_cached(self, evidence, github, wfe):
        seed_direct_run(evidence)
        github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))
        github.add(LIST_PATH, FakeResponse(payload={"total_count": 0, "artifacts": []}))

        evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})
        evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})
        assert github.count(RUN_PATH) == 1

    def test_stored_head_sha_needs_no_github_call(self, evidence, github):
        """The run payload stored the SHA on the job row, so the endpoint
        serves it without the live metadata fetch. Nothing is routed for
        RUN_PATH here: a fetch would 404 and blank the field."""
        seed_direct_run(evidence, head_sha="c" * 40)
        github.add(LIST_PATH, FakeResponse(payload={"total_count": 0, "artifacts": []}))

        doc = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})

        assert doc["run"]["head_sha"] == "c" * 40
        assert github.count(RUN_PATH) == 0

    def test_no_token_leakage(self, evidence, github, wfe):
        seed_direct_run(evidence)
        github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))
        github.add(LIST_PATH, artifact_payload(wfe.API_URL))
        github.add(DOWNLOAD_PATH, FakeResponse(content=zip_bytes({"junit.xml": VALID_JUNIT})))

        body = json.dumps(evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]}))

        assert TOKEN not in body
        assert "Authorization" not in body
        assert "token " not in body


class TestHTTP:
    @pytest.fixture
    def base_url(self, wfe, evidence, github, monkeypatch):
        monkeypatch.setattr(wfe.ExporterHandler, "exporter", evidence)
        server = ThreadingHTTPServer(("127.0.0.1", 0), wfe.ExporterHandler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_address[1]}"
        server.shutdown()
        server.server_close()

    def get(self, url):
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.headers, resp.read()

    def test_endpoint_json_shape_and_cors(self, base_url, github, wfe):
        seed_direct_run(github and wfe.ExporterHandler.exporter)
        github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))
        github.add(LIST_PATH, artifact_payload(wfe.API_URL))
        github.add(DOWNLOAD_PATH, FakeResponse(content=zip_bytes({"junit.xml": VALID_JUNIT})))

        status, headers, body = self.get(f"{base_url}/api/ci-observe/run?capture_id={CAPTURE}")

        assert status == 200
        assert headers.get("Content-Type") == "application/json"
        assert headers.get("Access-Control-Allow-Origin") == "*"
        doc = json.loads(body)
        assert doc["parsed"] == "exact"
        assert doc["run"]["head_sha"] == "a" * 40
        assert doc["native"]["status"] == "available"

    def test_endpoint_survives_non_finite_junit_counts(self, base_url, github, wfe):
        """A crafted tests="inf" in the artifact must not drop the HTTP
        connection: the response is still 200 with bounded, valid JSON."""
        exporter = wfe.ExporterHandler.exporter
        seed_direct_run(exporter)
        github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))
        github.add(LIST_PATH, artifact_payload(wfe.API_URL))
        junit = (
            b'<testsuites><testsuite name="t" tests="inf" failures="nan" time="inf">'
            b'<testcase classname="t" name="test_x" time="1e400">'
            b'<failure type="AssertionError" message="boom"/></testcase>'
            b"</testsuite></testsuites>"
        )
        github.add(DOWNLOAD_PATH, FakeResponse(content=zip_bytes({"junit.xml": junit})))

        status, headers, body = self.get(f"{base_url}/api/ci-observe/run?capture_id={CAPTURE}")

        assert status == 200
        assert headers.get("Content-Type") == "application/json"
        doc = json.loads(body)  # would raise on a bare Infinity/NaN
        assert doc["native"]["status"] == "available"
        assert doc["native"]["tests"] == 1  # testcase-count fallback, not a dropped reply
        assert doc["native"]["time_s"] == 0

    def test_endpoint_empty_capture_id(self, base_url):
        status, headers, body = self.get(f"{base_url}/api/ci-observe/run")
        assert status == 200
        assert headers.get("Content-Type") == "application/json"
        assert json.loads(body)["parsed"] == "invalid"


# The failed-test table on the E2E run page reads /api/ci-observe/run. This
# is the other half of that contract: the real endpoint's response must
# resolve every root_selector/columns pair the table queries.
DASHBOARDS_DIR = Path(__file__).resolve().parents[1] / "config" / "grafana" / "dashboards"
CAPTURE_DASHBOARD = DASHBOARDS_DIR / "ci-observe-runs.json"
RUN_ENDPOINT = "/api/ci-observe/run"


def _iter_panels(panels):
    for panel in panels:
        yield panel
        yield from _iter_panels(panel.get("panels", []))


def test_query_shape_resolves_against_the_endpoint_response(evidence, github, wfe):
    """The real /api/ci-observe/run response must resolve every
    root_selector and column the failed-test table queries, so the endpoint
    and the dashboard cannot drift apart."""
    dashboard = json.loads(CAPTURE_DASHBOARD.read_text())
    panels = [
        panel
        for panel in _iter_panels(dashboard["panels"])
        if panel["type"] not in ("row", "text")
        and any(RUN_ENDPOINT in target.get("url", "") for target in panel.get("targets", []))
    ]
    assert panels, "no panel reads the run endpoint"
    seed_direct_run(
        evidence,
        steps=[{"name": "Run E2E tests", "duration_s": 500}],
        failed_step="e2e → Run E2E tests",
        failure_reason="test",
    )
    github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))
    github.add(LIST_PATH, artifact_payload(wfe.API_URL))
    github.add(DOWNLOAD_PATH, FakeResponse(content=zip_bytes({"junit.xml": VALID_JUNIT})))

    doc = evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]})
    assert doc["native"]["status"] == "available"
    assert doc["native"]["failed_tests"]

    for panel in panels:
        for target in panel["targets"]:
            node = doc
            # Object roots use a JSONPath so Infinity keeps them as one row.
            selector = target["root_selector"]
            if selector.startswith("$."):
                selector = selector[2:]
            for part in selector.split("."):
                node = node[part]  # KeyError means dashboard/endpoint drift
            rows = node if isinstance(node, list) else [node]
            if not rows and target["root_selector"] == "timeline":
                # This run was stored before job timelines existed, so it has
                # no step timeline; test_ci_observe_dashboard_contract checks
                # the column shape against seeded timelines.
                continue
            assert rows, f"{panel['title']}: {target['root_selector']} is empty"
            for column in target["columns"]:
                assert column["selector"] in rows[0], (
                    f"{panel['title']}: column {column['selector']} not in the response"
                )


def test_deployment_ships_the_run_evidence_module():
    """The exporter imports a sibling module, so the container mount and the
    setup copy must both include it or the deployed service fails on import."""
    container = (MONITORING / "quadlet" / "workflow-exporter.container").read_text()
    setup = (MONITORING / "scripts" / "monitoring-setup.sh").read_text()
    assert "ci_observe_run_evidence.py:/app/ci_observe_run_evidence.py" in container
    assert "scripts/ci_observe_run_evidence.py" in setup
