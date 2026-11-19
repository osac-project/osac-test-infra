"""The run page's Failed tests panel, run the way Grafana runs it.

The panel's own query (its URL, root selector and columns, read from
ci-observe-runs.json) against the served exporter. It shows the failed tests
of the capture's JUnit file, "No failures." when that file lists none, and
an error, with the reason in the HTTP status line Grafana displays, for
anything else. It never shows another source's tests and never an empty
table for a file it could not read.

The incident this replays is run 43552935524 (E2E VMaaS Production Install,
2026-11-14): a scheduled run, so its job published the artifact to
osac-ci-artifacts, and the exporter's token got a 404 there. The page said
"No failures." for a run whose test step had failed.
"""

from __future__ import annotations

import io
import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from helpers import FakeResponse, make_job

RUN_PAGE = Path(__file__).resolve().parents[1] / "config" / "grafana" / "dashboards" / "ci-observe-runs.json"

RUN_ID = 43552935524
CAPTURE = f"vmaas-main-{RUN_ID}-a1"
REPO = "osac-test-infra"
ARTIFACT = f"osac-logs-full-install-{RUN_ID}"
PRIVATE_LISTING = f"/repos/osac-project/osac-ci-artifacts/actions/artifacts?name={ARTIFACT}&per_page=100"
PRIVATE_DOWNLOAD = "/repos/osac-project/osac-ci-artifacts/actions/artifacts/501/zip"
RUN_ARTIFACTS = f"/repos/osac-project/{REPO}/actions/runs/{RUN_ID}/artifacts?per_page=100"
TEST_STEP = "e2e-vmaas-prod-install / e2e → Run E2E tests (ci-observe capture)"
# The two tests the run's annotations name.
FAILED = ("test_compute_instance_cli_explicit_fields", "test_compute_instance_restart_past_timestamp_ignored")


def junit_zip(failed=FAILED, passed=146):
    cases = [f'<testcase classname="tests.e2e.vmaas.test_compute_instance" name="{name}" time="31.5">'
             f'<failure type="AssertionError" message="assert {name}">trace</failure></testcase>' for name in failed]
    cases += [f'<testcase classname="tests.e2e.vmaas.p" name="test_passing_{n}" time="1.0"/>' for n in range(passed)]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("junit.xml", f'<testsuites><testsuite name="pytest">{"".join(cases)}</testsuite></testsuites>')
        archive.writestr("e2e/failures/gw0/01-test/log.txt", "a log")
    return buffer.getvalue()


def private_artifact(api_url):
    return {"id": 501, "name": ARTIFACT, "size_in_bytes": 4096, "expired": False, "created_at": "2026-11-14T05:30:00Z",
            "archive_download_url": f"{api_url}{PRIVATE_DOWNLOAD}", "workflow_run": {"id": 9001}}


def panel():
    def walk(panels):
        for entry in panels:
            yield entry
            yield from walk(entry.get("panels") or [])

    document = json.loads(RUN_PAGE.read_text())
    return next(entry for entry in walk(document["panels"]) if entry.get("title") == "Failed tests")


class Page:
    """The Failed tests panel as Grafana runs it."""

    def __init__(self, base_url):
        self.base_url = base_url
        self.panel = panel()
        self.target = self.panel["targets"][0]

    def shows(self, capture_id=CAPTURE):
        """The rows the table shows, "No failures." for none, or the error
        Grafana shows on the panel: the HTTP status line."""
        url = self.target["url"].replace("${capture_id:percentencode}", urllib.parse.quote(capture_id, safe=""))
        url = url.replace("http://127.0.0.1:9103", self.base_url)
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                document = json.loads(response.read())
        except urllib.error.HTTPError as error:
            return f"error {error.code}: {error.reason}"
        rows = document
        for part in self.target["root_selector"].split("."):
            rows = rows[part]
        for row in rows:
            for column in self.target["columns"]:
                assert column["selector"] in row, column["selector"]
        return rows or self.panel["fieldConfig"]["defaults"]["noValue"]


def seed_run(exporter, **overrides):
    """The incident's run as the exporter stores it: scheduled, failed in
    its test step."""
    row = make_job(
        id=RUN_ID, repo=REPO, workflow="E2E VMaaS Production Install",
        display_name=f"{REPO} / E2E VMaaS Production Install", category="e2e", event="schedule",
        status="completed", conclusion="failure", duration_s=6578,
        url=f"https://github.com/osac-project/{REPO}/actions/runs/{RUN_ID}",
        created_at="2026-11-14T03:37:12Z", updated_at="2026-11-14T05:26:50Z", run_attempt=1, failed_step=TEST_STEP,
    )
    row.update(overrides)
    with exporter._db() as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (RUN_ID,))
    exporter._upsert_job(row)


@pytest.fixture
def page(wfe, evidence, github, monkeypatch):
    seed_run(evidence)
    monkeypatch.setattr(wfe.ExporterHandler, "exporter", evidence)
    server = ThreadingHTTPServer(("127.0.0.1", 0), wfe.ExporterHandler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield Page(f"http://127.0.0.1:{server.server_address[1]}")
    server.shutdown()
    server.server_close()


def test_the_incident_shows_an_error_that_says_what_to_fix(page, github, caplog):
    # osac-ci-artifacts answers 404: the token cannot see it.
    github.add(PRIVATE_LISTING, FakeResponse(404, text="Not Found"))
    with caplog.at_level(logging.ERROR):
        shown = page.shows()

    assert shown != "No failures."
    assert shown.startswith("error 502: osac-project/osac-ci-artifacts answered HTTP 404 to the artifact listing")
    assert "PRIVATE_GITHUB_TOKEN needs read access to the Actions artifacts of osac-project/osac-ci-artifacts" in shown
    assert f"failed tests of {CAPTURE} cannot be read" in caplog.text
    assert github.count(RUN_ARTIFACTS) == 0, "a refusal is not followed by the run"


def test_with_the_token_fixed_the_page_lists_the_failed_tests(page, github, wfe):
    github.add(PRIVATE_LISTING, FakeResponse(payload={"total_count": 1, "artifacts": [private_artifact(wfe.API_URL)]}))
    github.add(PRIVATE_DOWNLOAD, FakeResponse(content=junit_zip()))

    rows = page.shows()

    assert [row["name"] for row in rows] == list(FAILED)
    assert all(row["message"].startswith("assert ") for row in rows)
    assert all(row["artifact_url"] == "https://github.com/osac-project/osac-ci-artifacts/actions/runs/9001/artifacts/501"
               for row in rows)


def test_a_passed_run_shows_no_failures(page, github, wfe, evidence):
    seed_run(evidence, conclusion="success", status="success", failed_step="")
    github.add(PRIVATE_LISTING, FakeResponse(payload={"total_count": 1, "artifacts": [private_artifact(wfe.API_URL)]}))
    github.add(PRIVATE_DOWNLOAD, FakeResponse(content=junit_zip(failed=())))

    assert page.shows() == "No failures."


def test_a_failed_test_step_with_no_failed_test_in_the_junit_file_is_an_error(page, github, wfe):
    """pytest stopped before it reported a failure: the page must not say
    "No failures." for a run whose test step failed."""
    github.add(PRIVATE_LISTING, FakeResponse(payload={"total_count": 1, "artifacts": [private_artifact(wfe.API_URL)]}))
    github.add(PRIVATE_DOWNLOAD, FakeResponse(content=junit_zip(failed=())))

    shown = page.shows()

    assert shown.startswith("error 502: the test step failed (Run E2E tests (ci-observe capture))")


def test_a_merge_queue_page_shows_its_own_suites_failures(page, github, wfe, evidence):
    """The merge queue runs the three suites in one run; each suite's
    artifact stays on the run, and each page reads its own."""
    queue = 43600000001
    evidence._upsert_job(make_job(id=queue, workflow="E2E for the merge queue", category="automation",
                                  event="merge_group", conclusion="failure",
                                  url=f"https://github.com/osac-project/osac/actions/runs/{queue}"))
    for job_id, suite, conclusion, failed_step in (
        (1, "vmaas", "failure", "vmaas / e2e-vmaas-full-install / e2e → Run E2E tests"),
        (2, "caas", "success", ""),
    ):
        evidence._upsert_job(make_job(id=job_id, workflow=f"E2E {suite} (merge queue)", category="e2e",
                                      event="merge_group", conclusion=conclusion, failed_step=failed_step,
                                      url=f"https://github.com/osac-project/osac/actions/runs/{queue}/job/{job_id}"))
    names = {"vmaas": f"osac-logs-full-install-{queue}", "caas": f"osac-logs-caas-full-install-{queue}"}
    entries = [{"id": 70 + index, "name": name, "size_in_bytes": 4096, "expired": False,
                "archive_download_url": f"{wfe.API_URL}/repos/osac-project/osac/actions/artifacts/{70 + index}/zip"}
               for index, name in enumerate(names.values())]
    github.add(f"/repos/osac-project/osac/actions/runs/{queue}/artifacts?per_page=100",
               FakeResponse(payload={"total_count": 2, "artifacts": entries}))
    github.add("/repos/osac-project/osac/actions/artifacts/70/zip", FakeResponse(content=junit_zip(failed=FAILED[:1])))
    github.add("/repos/osac-project/osac/actions/artifacts/71/zip", FakeResponse(content=junit_zip(failed=())))

    assert [row["name"] for row in page.shows(f"vmaas-main-{queue}-a1")] == [FAILED[0]]
    assert page.shows(f"caas-main-{queue}-a1") == "No failures."
