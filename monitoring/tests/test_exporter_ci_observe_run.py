"""Two endpoints of workflow-exporter.py for the E2E run page.

/api/ci-observe/run: capture-id parsing and the run from the SQLite job
history (no GitHub artifact).

/api/ci-observe/failed-tests: the failed tests of one capture, from the
JUnit file in its suite's artifact, read from the one place the run's
trigger says (the run itself for pull-request and merge-queue runs,
osac-ci-artifacts for scheduled and manual ones). Every way that read can
fail is an error that says what failed, where and what to fix; none falls
back to another place or another source.

The last tests resolve the real responses against the run page's panels."""

from __future__ import annotations

import io
import json
import sys
import threading
import urllib.error
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
# A pull-request or merge-queue run's own artifact listing.
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
    # The run document reads no artifact: the failed tests have their own endpoint.
    assert "native" not in doc
    assert not [call for call in github.calls if "/artifacts" in call]


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
    assert any("latest attempt" in note for note in doc["coverage"]["notes"])


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
    assert any("not in the exporter's job history" in n for n in doc["coverage"]["notes"])


def test_invalid_and_missing_capture_id(evidence):
    invalid = evidence.get_ci_observe_run_json({"capture_id": ["vmaas-main-abc-a1"]})
    assert invalid["parsed"] == "invalid"
    assert invalid["run_id"] is None

    empty = evidence.get_ci_observe_run_json({})
    assert empty["parsed"] == "invalid"
    assert empty["capture_id"] == ""
    assert any("missing or empty" in n for n in empty["coverage"]["notes"])


# -- /api/ci-observe/failed-tests ------------------------------------------------

PRIVATE_LIST_PATH = f"/repos/osac-project/osac-ci-artifacts/actions/artifacts?name={ARTIFACT_NAME}&per_page=100"
PRIVATE_DOWNLOAD_PATH = "/repos/osac-project/osac-ci-artifacts/actions/artifacts/77/zip"
PRIVATE_ARTIFACT_URL = "https://github.com/osac-project/osac-ci-artifacts/actions/runs/9001/artifacts/77"
TOKEN_FIX = "PRIVATE_GITHUB_TOKEN needs read access to the Actions artifacts of osac-project/osac-ci-artifacts"
PASSING_JUNIT = (b'<testsuites><testsuite name="t"><testcase classname="t" name="test_ok" time="1"/>'
                 b"</testsuite></testsuites>")


def private_artifact_payload(api_url, *, name=ARTIFACT_NAME, expired=False, size=1024, created="2026-10-10T12:00:00Z",
                             art_id=77, published_by=9001):
    """One artifact as the private repository's name-filtered listing returns
    it: it belongs to the private collect run, not to the E2E run."""
    entry = artifact_entry(api_url, art_id=art_id, name=name, expired=expired, size=size)
    entry["archive_download_url"] = f"{api_url}/repos/osac-project/osac-ci-artifacts/actions/artifacts/{art_id}/zip"
    entry["workflow_run"] = {"id": published_by}
    entry["created_at"] = created
    return entry


def publish_privately(github, api_url, junit=VALID_JUNIT, **entry):
    """A scheduled run's artifact, where its job put it."""
    github.add(PRIVATE_LIST_PATH, FakeResponse(payload={"total_count": 1, "artifacts": [
        private_artifact_payload(api_url, **entry)]}))
    github.add(PRIVATE_DOWNLOAD_PATH, FakeResponse(content=zip_bytes({"junit.xml": junit})))


def failed_tests(evidence, capture_id=CAPTURE):
    return evidence.get_ci_observe_failed_tests_json({"capture_id": [capture_id]})


def refused(evidence, capture_id=CAPTURE):
    """The error the endpoint answers, as text."""
    evidence_module = sys.modules["ci_observe_run_evidence"]
    with pytest.raises(evidence_module.EvidenceError) as raised:
        failed_tests(evidence, capture_id)
    return str(raised.value)


def seed_merge_queue_run(exporter, **caas):
    """A merge-queue run: its own row and one row per suite."""
    exporter._upsert_job(make_job(
        id=RUN_ID, workflow="E2E for the merge queue", display_name="osac / E2E for the merge queue",
        category="automation", event="merge_group", conclusion="failure",
        url=f"https://github.com/osac-project/osac/actions/runs/{RUN_ID}"))
    exporter._upsert_job(suite_row(101, workflow="E2E vmaas (merge queue)",
                                   display_name="osac / E2E vmaas (merge queue)", event="merge_group",
                                   conclusion="success"))
    exporter._upsert_job(suite_row(102, workflow="E2E caas (merge queue)",
                                   display_name="osac / E2E caas (merge queue)", event="merge_group",
                                   conclusion="failure",
                                   failed_step="caas / e2e-caas-full-install / e2e → Run E2E tests", **caas))


class TestFailedTests:
    """The happy path: one artifact, at the one place the trigger says."""

    def test_a_scheduled_run_is_read_from_the_private_repository(self, evidence, github, wfe):
        seed_direct_run(evidence, failed_step="e2e → Run E2E tests")
        publish_privately(github, wfe.API_URL)

        doc = failed_tests(evidence)

        assert (doc["suite"], doc["trigger"]) == ("vmaas", "scheduled")
        assert doc["artifact"] == {"name": ARTIFACT_NAME, "place": "osac-project/osac-ci-artifacts",
                                   "url": PRIVATE_ARTIFACT_URL}
        # Counts are counts of test cases, so they agree with the rows.
        assert (doc["tests"], doc["failures"], doc["errors"], doc["skipped"]) == (4, 1, 1, 1)
        assert [(t["name"], t["type"], t["message"]) for t in doc["failed_tests"]] == [
            ("test_fail", "AssertionError", "assert 1 == 2"),
            ("test_error", "TimeoutError", "timed out after 30s"),
        ]
        assert all(t["artifact_url"] == PRIVATE_ARTIFACT_URL for t in doc["failed_tests"])
        assert github.count(LIST_PATH) == 0, "a scheduled run's own artifacts are not asked"

    def test_a_manual_run_is_read_from_the_private_repository(self, evidence, github, wfe):
        seed_direct_run(evidence, event="workflow_dispatch", failed_step="e2e → Run E2E tests")
        publish_privately(github, wfe.API_URL)

        assert failed_tests(evidence)["trigger"] == "manual"

    def test_a_run_that_passed_lists_no_failure(self, evidence, github, wfe):
        seed_direct_run(evidence, conclusion="success", status="success")
        junit = PASSING_JUNIT
        publish_privately(github, wfe.API_URL, junit=junit)

        doc = failed_tests(evidence)

        assert doc["failed_tests"] == [] and doc["tests"] == 1

    def test_a_merge_queue_capture_reads_its_own_suites_artifact_on_the_run(self, evidence, github, wfe):
        seed_merge_queue_run(evidence)
        names = (ARTIFACT_NAME, f"osac-logs-caas-full-install-{RUN_ID}", f"osac-logs-bmaas-full-install-{RUN_ID}")
        entries = [artifact_entry(wfe.API_URL, art_id=60 + index, name=name) for index, name in enumerate(names)]
        github.add(LIST_PATH, FakeResponse(payload={"total_count": 3, "artifacts": entries}))
        github.add("/repos/osac-project/osac/actions/artifacts/61/zip",
                   FakeResponse(content=zip_bytes({"junit.xml": VALID_JUNIT})))

        doc = failed_tests(evidence, f"caas-main-{RUN_ID}-a1")

        assert (doc["trigger"], doc["artifact"]["name"]) == ("merge_queue", f"osac-logs-caas-full-install-{RUN_ID}")
        assert doc["artifact"]["place"] == f"run {RUN_ID} of osac-project/osac"
        downloads = [call for call in github.calls if call.endswith("/zip")]
        assert downloads == ["/repos/osac-project/osac/actions/artifacts/61/zip"]
        assert not [call for call in github.calls if "osac-ci-artifacts" in call]

    def test_a_pull_request_run_is_read_on_the_run(self, evidence, github, wfe):
        evidence._upsert_job(run_row(workflow="E2E suite · vmaas · #42 · 0123abc", event="workflow_dispatch",
                                     failed_step="e2e → Run E2E tests"))
        github.add(LIST_PATH, artifact_payload(wfe.API_URL))
        github.add(DOWNLOAD_PATH, FakeResponse(content=zip_bytes({"junit.xml": VALID_JUNIT})))

        doc = failed_tests(evidence, f"vmaas-pr42-{RUN_ID}-a1")

        assert (doc["trigger"], doc["artifact"]["url"]) == ("pr", ARTIFACT_URL)
        assert len(doc["failed_tests"]) == 2

    def test_the_newest_of_several_published_artifacts_is_read(self, evidence, github, wfe):
        """A re-run publishes again under the same name: the newest is read."""
        seed_direct_run(evidence, failed_step="e2e → Run E2E tests")
        older = private_artifact_payload(wfe.API_URL, art_id=76, created="2026-10-10T11:00:00Z", published_by=9000)
        newer = private_artifact_payload(wfe.API_URL)
        github.add(PRIVATE_LIST_PATH, FakeResponse(payload={"total_count": 2, "artifacts": [newer, older]}))
        github.add(PRIVATE_DOWNLOAD_PATH, FakeResponse(content=zip_bytes({"junit.xml": VALID_JUNIT})))

        assert failed_tests(evidence)["artifact"]["url"] == PRIVATE_ARTIFACT_URL

    def test_multiple_junit_files_are_summed(self, evidence, github, wfe):
        seed_direct_run(evidence, failed_step="e2e → Run E2E tests")
        second = (b'<testsuites><testsuite name="b"><testcase classname="b" name="test_b" time="2">'
                  b'<failure message="no"/></testcase></testsuite></testsuites>')
        github.add(PRIVATE_LIST_PATH, FakeResponse(payload={"total_count": 1, "artifacts": [
            private_artifact_payload(wfe.API_URL)]}))
        github.add(PRIVATE_DOWNLOAD_PATH, FakeResponse(content=zip_bytes({
            "junit.xml": VALID_JUNIT, "part/junit-b.xml": second})))

        doc = failed_tests(evidence)

        assert (doc["tests"], doc["failures"]) == (5, 2)
        assert [t["name"] for t in doc["failed_tests"]] == ["test_fail", "test_error", "test_b"]

    def test_every_failed_test_is_listed(self, evidence, github, wfe):
        seed_direct_run(evidence, failed_step="e2e → Run E2E tests")
        cases = "".join(f'<testcase classname="t" name="test_{n}" time="1"><failure message="m"/></testcase>'
                        for n in range(150))
        junit = f"<testsuites><testsuite name='t'>{cases}</testsuite></testsuites>".encode()
        publish_privately(github, wfe.API_URL, junit=junit)

        assert len(failed_tests(evidence)["failed_tests"]) == 150

    def test_message_bounded_and_control_chars_stripped(self, evidence, github, wfe):
        # \x01/\x02 are illegal anywhere in XML 1.0 (the pre-parse clean
        # removes them so the file still parses); \x7f is legal XML but is
        # still stripped from the displayed message.
        seed_direct_run(evidence, failed_step="e2e → Run E2E tests")
        junit = (
            b'<testsuites><testsuite name="t" tests="1" failures="1" time="1.0">'
            b'<testcase classname="t" name="test_long" time="1.0">'
            b'<failure type="AssertionError" message="boom\x01\x02\x7f' + b"x" * 5000 + b'"/>'
            b"</testcase></testsuite></testsuites>"
        )
        publish_privately(github, wfe.API_URL, junit=junit)

        doc = failed_tests(evidence)

        message = doc["failed_tests"][0]["message"]
        assert len(message) == wfe.WorkflowExporter.NATIVE_MAX_MESSAGE_CHARS
        for char in ("\x01", "\x02", "\x7f"):
            assert char not in message
        assert any("truncated" in note for note in doc["notes"])

    def test_non_finite_numbers_are_not_numbers(self, evidence, github, wfe):
        """A crafted tests="inf" or time="1e400" must not reach the JSON."""
        seed_direct_run(evidence, failed_step="e2e → Run E2E tests")
        junit = (b'<testsuites><testsuite name="t" tests="inf" failures="nan" time="inf">'
                 b'<testcase classname="t" name="test_x" time="1e400"><failure type="AssertionError" message="boom"/>'
                 b"</testcase></testsuite></testsuites>")
        publish_privately(github, wfe.API_URL, junit=junit)

        doc = failed_tests(evidence)

        assert (doc["tests"], doc["failures"]) == (1, 1), "counted from the test cases, not the attributes"
        assert doc["failed_tests"][0]["time_s"] is None
        json.dumps(doc, allow_nan=False)


class TestFailedTestsErrors:
    """Everything else is an error that says what failed, where, and how to fix it."""

    @pytest.mark.parametrize("capture_id", [
        f"run-{RUN_ID}-attempt-1", str(RUN_ID), f"netris-main-{RUN_ID}-a1", "garbage", "",
    ])
    def test_a_capture_id_that_names_no_suite(self, evidence, capture_id):
        assert "is not <vmaas|caas|bmaas>-<prN|main>-<run id>-a<attempt>" in refused(evidence, capture_id)

    def test_no_token(self, exporter, github):
        seed_direct_run(exporter)

        assert "no GitHub token is configured (PRIVATE_GITHUB_TOKEN)" in refused(exporter)
        assert github.calls == []

    def test_a_run_not_in_the_job_history(self, evidence, github):
        assert f"run {RUN_ID} is not in the exporter's job history" in refused(evidence)
        assert github.calls == []

    @pytest.mark.parametrize("status", [401, 403, 404])
    def test_a_private_repository_that_refuses_the_token(self, evidence, github, status):
        """The incident of 2026-11-14: a scheduled run's artifact is in
        osac-ci-artifacts, and the token cannot read it."""
        seed_direct_run(evidence, failed_step="e2e → Run E2E tests")
        github.add(PRIVATE_LIST_PATH, FakeResponse(status, text="Not Found"))

        message = refused(evidence)

        assert f"osac-project/osac-ci-artifacts answered HTTP {status} to the artifact listing" in message
        assert TOKEN_FIX in message
        assert github.count(LIST_PATH) == 0, "no second place is tried"

    def test_github_failing_on_the_run(self, evidence, github):
        seed_merge_queue_run(evidence)
        github.add(LIST_PATH, FakeResponse(502, text="bad gateway"))

        assert refused(evidence, f"caas-main-{RUN_ID}-a1") == (
            f"run {RUN_ID} of osac-project/osac answered HTTP 502 to the artifact listing")

    def test_a_network_error(self, evidence, github, wfe):
        seed_direct_run(evidence)
        github.add(PRIVATE_LIST_PATH, wfe.requests.exceptions.ConnectionError("connection reset"))

        assert "listing the artifacts of osac-project/osac-ci-artifacts failed: connection reset" in refused(evidence)

    @pytest.mark.parametrize("payload", [None, {"artifacts": 1}, {"artifacts": [None]}, {"no": "artifacts"}])
    def test_an_answer_that_is_not_a_listing(self, evidence, github, payload):
        seed_direct_run(evidence)
        github.add(PRIVATE_LIST_PATH, FakeResponse(payload=payload))

        assert "answered something that is not an artifact listing" in refused(evidence)

    def test_no_artifact_where_the_trigger_says(self, evidence, github):
        seed_direct_run(evidence)
        github.add(PRIVATE_LIST_PATH, FakeResponse(payload={"total_count": 0, "artifacts": []}))

        assert refused(evidence) == (
            f"no {ARTIFACT_NAME} artifact in osac-project/osac-ci-artifacts, where scheduled runs keep it")

    def test_another_suites_or_runs_artifact_is_not_this_captures(self, evidence, github, wfe):
        seed_merge_queue_run(evidence)
        github.add(LIST_PATH, FakeResponse(payload={"total_count": 2, "artifacts": [
            artifact_entry(wfe.API_URL, name=ARTIFACT_NAME),
            artifact_entry(wfe.API_URL, art_id=43, name=f"osac-logs-caas-full-install-{RUN_ID + 1}")]}))

        assert refused(evidence, f"caas-main-{RUN_ID}-a1") == (
            f"no osac-logs-caas-full-install-{RUN_ID} artifact in run {RUN_ID} of osac-project/osac, "
            "where merge_queue runs keep it")
        assert not [call for call in github.calls if call.endswith("/zip")]

    def test_a_trigger_with_no_artifact_place(self, evidence, github):
        seed_direct_run(evidence, event="push")

        assert "only pull-request and merge-queue runs" in refused(evidence)
        assert not [call for call in github.calls if "/artifacts" in call]

    def test_expired(self, evidence, github, wfe):
        seed_direct_run(evidence)
        github.add(PRIVATE_LIST_PATH, FakeResponse(payload={"total_count": 1, "artifacts": [
            private_artifact_payload(wfe.API_URL, expired=True)]}))

        assert refused(evidence) == (
            f"{ARTIFACT_NAME} in osac-project/osac-ci-artifacts expired on 2026-10-17T10:00:00Z "
            "(GitHub keeps artifacts 30 days)")
        assert github.count(PRIVATE_DOWNLOAD_PATH) == 0

    def test_too_large_without_download(self, evidence, github, wfe):
        seed_direct_run(evidence)
        size = wfe.WorkflowExporter.NATIVE_MAX_DOWNLOAD_BYTES + 1
        github.add(PRIVATE_LIST_PATH, FakeResponse(payload={"total_count": 1, "artifacts": [
            private_artifact_payload(wfe.API_URL, size=size)]}))

        assert "over the" in refused(evidence) and "download bound" in refused(evidence)
        assert github.count(PRIVATE_DOWNLOAD_PATH) == 0

    def test_download_bounded_mid_stream(self, evidence, github, wfe, monkeypatch):
        seed_direct_run(evidence)
        monkeypatch.setattr(wfe.WorkflowExporter, "NATIVE_MAX_DOWNLOAD_BYTES", 10)
        github.add(PRIVATE_LIST_PATH, FakeResponse(payload={"total_count": 1, "artifacts": [
            private_artifact_payload(wfe.API_URL, size=1)]}))
        github.add(PRIVATE_DOWNLOAD_PATH, FakeResponse(content=b"x" * 64))

        assert refused(evidence).endswith("stopped: it is over the download bound")

    def test_a_failed_download(self, evidence, github, wfe):
        seed_direct_run(evidence)
        github.add(PRIVATE_LIST_PATH, FakeResponse(payload={"total_count": 1, "artifacts": [
            private_artifact_payload(wfe.API_URL)]}))
        github.add(PRIVATE_DOWNLOAD_PATH, FakeResponse(500, text="boom"))

        assert refused(evidence) == (f"downloading {ARTIFACT_NAME} from osac-project/osac-ci-artifacts failed: "
                                     "artifact download failed with HTTP 500")

    @pytest.mark.parametrize(("content", "words"), [
        (b"not a zip", "artifact is not a valid zip archive"),
        (zip_bytes({"e2e.log": b"logs"}), "artifact has no junit.xml"),
        (zip_bytes({"junit.xml": b"<testsuites><testsuite"}), "junit.xml is not valid XML"),
        (zip_bytes({"junit.xml": b"<nothing/>"}), "junit.xml has no testsuite element"),
    ])
    def test_an_artifact_that_does_not_hold_a_readable_junit_file(self, evidence, github, wfe, content, words):
        seed_direct_run(evidence)
        github.add(PRIVATE_LIST_PATH, FakeResponse(payload={"total_count": 1, "artifacts": [
            private_artifact_payload(wfe.API_URL)]}))
        github.add(PRIVATE_DOWNLOAD_PATH, FakeResponse(content=content))

        assert refused(evidence) == f"{ARTIFACT_NAME} from osac-project/osac-ci-artifacts: {words}"

    def test_a_failed_test_step_with_no_failed_test_in_the_junit_file(self, evidence, github, wfe):
        seed_direct_run(evidence, failed_step="e2e → Run E2E tests (ci-observe capture)")
        junit = PASSING_JUNIT
        publish_privately(github, wfe.API_URL, junit=junit)

        message = refused(evidence)

        assert message.startswith("the test step failed (Run E2E tests (ci-observe capture)), but the junit.xml of ")
        assert "The job log has the cause" in message

    def test_an_earlier_attempt_cannot_be_told_apart(self, evidence, github):
        seed_direct_run(evidence, run_attempt=2)

        assert refused(evidence).startswith("the capture is attempt 1, and the job history holds attempt 2")
        assert github.calls == []


class TestFailedTestsCacheLogAndSecrets:
    def test_a_result_is_cached(self, evidence, github, wfe):
        seed_direct_run(evidence, failed_step="e2e → Run E2E tests")
        publish_privately(github, wfe.API_URL)

        first = failed_tests(evidence)
        second = failed_tests(evidence)

        assert first == second
        assert github.count(PRIVATE_LIST_PATH) == 1
        assert github.count(PRIVATE_DOWNLOAD_PATH) == 1

    def test_an_error_is_logged_once_and_cached_briefly(self, evidence, github, wfe, caplog, monkeypatch):
        seed_direct_run(evidence)
        github.add(PRIVATE_LIST_PATH, FakeResponse(404, text="Not Found"))

        first = refused(evidence)
        second = refused(evidence)

        assert first == second
        assert github.count(PRIVATE_LIST_PATH) == 1
        errors = [record for record in caplog.records if record.levelname == "ERROR"]
        assert len(errors) == 1
        assert f"failed tests of {CAPTURE} cannot be read" in errors[0].getMessage()
        assert TOKEN_FIX in errors[0].getMessage()
        # Once the brief cache ends, GitHub is asked again.
        monkeypatch.setattr(sys.modules["ci_observe_run_evidence"].time, "monotonic",
                            lambda: 10 ** 9)
        refused(evidence)
        assert github.count(PRIVATE_LIST_PATH) == 2

    def test_no_token_leakage(self, evidence, github, wfe):
        seed_direct_run(evidence, failed_step="e2e → Run E2E tests")
        publish_privately(github, wfe.API_URL)

        body = json.dumps(failed_tests(evidence))

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

    def test_the_run_endpoint_json_shape_and_cors(self, base_url, github, wfe):
        seed_direct_run(wfe.ExporterHandler.exporter)
        github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))

        status, headers, body = self.get(f"{base_url}/api/ci-observe/run?capture_id={CAPTURE}")

        assert status == 200
        assert headers.get("Content-Type") == "application/json"
        assert headers.get("Access-Control-Allow-Origin") == "*"
        doc = json.loads(body)
        assert doc["parsed"] == "exact"
        assert doc["run"]["head_sha"] == "a" * 40

    def test_the_run_endpoint_with_an_empty_capture_id(self, base_url):
        status, headers, body = self.get(f"{base_url}/api/ci-observe/run")
        assert status == 200
        assert headers.get("Content-Type") == "application/json"
        assert json.loads(body)["parsed"] == "invalid"

    def test_the_failed_tests_endpoint_answers_the_junit_file(self, base_url, github, wfe):
        seed_direct_run(wfe.ExporterHandler.exporter, failed_step="e2e → Run E2E tests")
        publish_privately(github, wfe.API_URL)

        status, headers, body = self.get(f"{base_url}/api/ci-observe/failed-tests?capture_id={CAPTURE}")

        assert status == 200
        assert headers.get("Content-Type") == "application/json"
        assert headers.get("Access-Control-Allow-Origin") == "*"
        assert [t["name"] for t in json.loads(body)["failed_tests"]] == ["test_fail", "test_error"]

    def test_the_failed_tests_endpoint_fails_with_the_reason(self, base_url, github, wfe):
        """Grafana shows a panel's HTTP error by its status line, so the
        reason is there, and in the body."""
        seed_direct_run(wfe.ExporterHandler.exporter)
        github.add(PRIVATE_LIST_PATH, FakeResponse(404, text="Not Found"))

        with pytest.raises(urllib.error.HTTPError) as raised:
            self.get(f"{base_url}/api/ci-observe/failed-tests?capture_id={CAPTURE}")

        error = raised.value
        assert error.code == 502
        assert "osac-project/osac-ci-artifacts answered HTTP 404 to the artifact listing" in error.reason
        assert TOKEN_FIX in error.reason
        body = json.loads(error.read())
        assert body["capture_id"] == CAPTURE
        assert TOKEN_FIX in body["error"]


# The run page reads both endpoints. The real responses must resolve every
# root_selector and column its panels query, so the endpoints and the page
# cannot drift apart.
DASHBOARDS_DIR = Path(__file__).resolve().parents[1] / "config" / "grafana" / "dashboards"
RUN_PAGE = DASHBOARDS_DIR / "ci-observe-runs.json"


def _iter_panels(panels):
    for panel in panels:
        yield panel
        yield from _iter_panels(panel.get("panels", []))


def test_query_shape_resolves_against_the_endpoint_responses(evidence, github, wfe):
    dashboard = json.loads(RUN_PAGE.read_text())
    seed_direct_run(
        evidence,
        steps=[{"name": "Run E2E tests", "duration_s": 500}],
        failed_step="e2e → Run E2E tests",
        failure_reason="test",
    )
    github.add(RUN_PATH, FakeResponse(payload={"head_sha": "a" * 40}))
    publish_privately(github, wfe.API_URL)
    documents = {
        "/api/ci-observe/run": evidence.get_ci_observe_run_json({"capture_id": [CAPTURE]}),
        "/api/ci-observe/failed-tests": failed_tests(evidence),
    }
    checked = set()
    for panel in _iter_panels(dashboard["panels"]):
        for target in panel.get("targets", []):
            path = next((p for p in documents if f"9103{p}?" in target.get("url", "")), None)
            if path is None:
                continue
            node = documents[path]
            selector = target["root_selector"]
            if selector.startswith("$."):
                # Object roots use a JSONPath so Infinity keeps them as one row.
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
            checked.add(path)
    assert checked == set(documents), "every endpoint has a panel that reads it"


def test_deployment_ships_the_run_evidence_module():
    """The exporter imports a sibling module, so the container mount and the
    setup copy must both include it or the deployed service fails on import."""
    container = (MONITORING / "quadlet" / "workflow-exporter.container").read_text()
    setup = (MONITORING / "scripts" / "monitoring-setup.sh").read_text()
    assert "ci_observe_run_evidence.py:/app/ci_observe_run_evidence.py" in container
    assert "scripts/ci_observe_run_evidence.py" in setup
