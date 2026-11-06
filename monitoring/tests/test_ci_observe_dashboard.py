"""Structural tests for the ci-observe E2E run page.

`ci-observe-runs.json` answers one question about one run: did it pass, what
failed, and how long did the tests take? The page is a summary row, a
Failures row, a Test list row and a Run info row. Every row except the
summary starts collapsed.

Every selector is resolved against a fixture document with the same keys as
`/api/ci-observe/capture` and `/api/ci-observe/run`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

DASHBOARDS_DIR = Path(__file__).resolve().parents[1] / "config" / "grafana" / "dashboards"
DASHBOARD = json.loads((DASHBOARDS_DIR / "ci-observe-runs.json").read_text())
CAPTURE_URL = "http://127.0.0.1:9103/api/ci-observe/capture?capture_id=${capture_id:percentencode}"
RUN_URL = "http://127.0.0.1:9103/api/ci-observe/run?capture_id=${capture_id:percentencode}"

CAPTURE_DOCUMENT = {
    "session": {
        "status": "error",
        "tests": 148,
        "duration_seconds": 1554.5,
    },
    "summary": [
        {
            "status": "error",
            "tests": 148,
            "duration_seconds": 1554.5,
        }
    ],
    "tests": [
        {
            "name": "tests/e2e/vmaas/test_cluster_create.py::TestCluster::test_create",
            "status": "error",
            "started_at": "2026-11-04T09:13:10Z",
            "duration_s": 95.2,
        }
    ],
    "identity": {
        "run_id": "41709318840",
        "pr": "752",
        "workflow": "E2E VMaaS Full Install",
        "test_sha": "a" * 40,
        "test_suite": "tests/e2e/vmaas/ tests/e2e/references/",
        "repo": "osac",
        "run_url": "https://github.com/osac-project/osac/actions/runs/41709318840",
        "pr_url": "https://github.com/osac-project/osac/pull/752",
        "commit_url": "https://github.com/osac-project/osac/commit/" + "a" * 40,
    },
}

RUN_DOCUMENT = {
    "jobs": [
        {
            "name": "e2e",
            "workflow": "E2E VMaaS Full Install",
            "conclusion": "failure",
            "duration_s": 3600,
            "failed_step": "Run E2E tests",
            "failure_reason": "test",
            "url": "https://github.com/osac-project/osac/actions/runs/41709318840/job/1",
        }
    ],
    "steps": [
        {
            "job_name": "e2e",
            "step_name": "Run E2E tests",
            "duration_s": 1800,
            "conclusion": "failure",
        }
    ],
    "native": {
        "failed_tests": [
            {
                "name": "test_create",
                "message": "AssertionError: cluster did not reach Ready",
                "time_s": 95.2,
                "artifact_url": "https://github.com/osac-project/osac/actions/runs/41709318840",
            }
        ]
    },
}


def walk(panels: list[dict]):
    for panel in panels:
        yield panel
        yield from walk(panel.get("panels", []))


def panel(titled: str) -> dict:
    return next(candidate for candidate in walk(DASHBOARD["panels"]) if candidate.get("title") == titled)


def variables() -> dict[str, dict]:
    return {variable["name"]: variable for variable in DASHBOARD["templating"]["list"]}


def selectors(table: dict) -> list[str]:
    return [column["selector"] for column in table["targets"][0]["columns"]]


def display_names(table: dict) -> dict[str, str]:
    names = {}
    for entry in table["fieldConfig"]["overrides"]:
        field = entry["matcher"].get("options")
        for prop in entry["properties"]:
            if prop["id"] == "displayName":
                names[field] = prop["value"]
    return names


def resolve(document: dict, root_selector: str) -> dict:
    node = document
    for part in root_selector.lstrip("$.").split("."):
        node = node[part]
    return node[0] if isinstance(node, list) else node


def assert_selectors_resolve(table: dict, document: dict) -> None:
    row = resolve(document, table["targets"][0]["root_selector"])
    for column in selectors(table):
        assert column in row, f"{table['title']}: {column}"


def test_identity_and_folder() -> None:
    assert DASHBOARD["uid"] == "ci-observe-runs"
    assert DASHBOARD["title"] == "CI Observe — E2E run"
    assert "ci-observe" in DASHBOARD["tags"]


def test_the_page_refreshes_while_a_run_is_live() -> None:
    assert DASHBOARD["refresh"] == "1m"


def test_the_only_variable_is_the_recording() -> None:
    assert set(variables()) == {"capture_id"}


def test_the_summary_answers_three_questions() -> None:
    assert [panel(title)["type"] for title in ("Result", "Tests run", "Test time")] == ["stat", "stat", "stat"]


def test_a_live_capture_reads_as_still_running() -> None:
    result = panel("Result")
    defaults = result["fieldConfig"]["defaults"]
    options = defaults["mappings"][0]["options"]
    assert options["running"]["text"] == "Still running"
    assert options["null"]["text"] == "Not finished"
    assert options["cancelled"]["text"] == "Canceled"
    assert defaults["noValue"] == "Not finished"


def test_the_summary_reads_the_capture_endpoint() -> None:
    for title, selector in (("Result", "status"), ("Tests run", "tests"), ("Test time", "duration_seconds")):
        target = panel(title)["targets"][0]
        assert target["url"] == CAPTURE_URL
        assert target["root_selector"] == "summary"
        assert selectors(panel(title)) == [selector]
    assert_selectors_resolve(panel("Result"), CAPTURE_DOCUMENT)


def test_the_summary_row_is_open_and_detail_rows_are_collapsed() -> None:
    rows = {row["title"]: row for row in DASHBOARD["panels"] if row["type"] == "row"}
    assert rows["Run summary"]["collapsed"] is False
    assert rows["Failures"]["collapsed"] is True
    assert rows["Jobs and steps"]["collapsed"] is True
    assert rows["Test list"]["collapsed"] is True
    assert rows["Run info"]["collapsed"] is True


def test_jobs_read_the_run_endpoint() -> None:
    table = panel("Jobs")
    assert table["targets"][0]["url"] == RUN_URL
    assert table["targets"][0]["root_selector"] == "jobs"
    assert_selectors_resolve(table, RUN_DOCUMENT)
    assert display_names(table) == {
        "name": "Job",
        "workflow": "Workflow",
        "conclusion": "Result",
        "duration_s": "Duration",
        "failed_step": "Failed step",
        "failure_reason": "Failure kind",
    }


def test_steps_read_the_run_endpoint() -> None:
    table = panel("Steps")
    assert table["targets"][0]["url"] == RUN_URL
    assert table["targets"][0]["root_selector"] == "steps"
    assert_selectors_resolve(table, RUN_DOCUMENT)
    assert display_names(table) == {
        "job_name": "Job",
        "step_name": "Step",
        "duration_s": "Duration",
        "conclusion": "Result",
    }


def test_failures_read_the_run_endpoint() -> None:
    table = panel("Failed tests")
    assert table["targets"][0]["url"] == RUN_URL
    assert table["targets"][0]["root_selector"] == "native.failed_tests"
    assert_selectors_resolve(table, RUN_DOCUMENT)


def test_the_test_list_reads_the_capture_endpoint() -> None:
    table = panel("All tests")
    assert table["targets"][0]["url"] == CAPTURE_URL
    assert table["targets"][0]["root_selector"] == "tests"
    assert_selectors_resolve(table, CAPTURE_DOCUMENT)


def test_run_info_reads_the_capture_identity() -> None:
    table = panel("Details")
    assert table["targets"][0]["url"] == CAPTURE_URL
    assert table["targets"][0]["root_selector"] == "identity"
    assert_selectors_resolve(table, CAPTURE_DOCUMENT)
    assert display_names(table) == {
        "run_id": "GitHub run",
        "pr": "Pull request",
        "workflow": "Workflow",
        "test_sha": "Commit",
        "test_suite": "Test paths",
        "run_url": "run_url",
        "pr_url": "pr_url",
        "commit_url": "commit_url",
    }


def test_run_info_links_every_identifier() -> None:
    details = panel("Details")
    assert "run_url" in selectors(details)
    assert "pr_url" in selectors(details)
    assert "commit_url" in selectors(details)
    links = {}
    for entry in details["fieldConfig"]["overrides"]:
        field = entry["matcher"].get("options")
        for prop in entry["properties"]:
            if prop["id"] == "links":
                links[field] = prop["value"][0]["url"]
    assert links["run_id"] == "${__data.fields.run_url}"
    assert links["pr"] == "${__data.fields.pr_url}"
    assert links["test_sha"] == "${__data.fields.commit_url}"


def test_cluster_graphs_are_one_collapsed_row() -> None:
    row = next(panel for panel in DASHBOARD["panels"] if panel.get("title") == "Cluster")
    assert row["type"] == "row"
    assert row["collapsed"] is True
    assert [child["title"] for child in row["panels"]] == [
        "Pods",
        "CPU by pod",
        "Memory by pod",
        "Node CPU",
        "Node memory",
    ]
    for child in row["panels"]:
        assert child["datasource"]["uid"] == "ci-observe-metrics"
        assert "$capture_id" in child["targets"][0]["expr"]


def test_every_panel_has_a_title() -> None:
    assert all(candidate.get("title") for candidate in walk(DASHBOARD["panels"]))


def test_every_link_opens_a_dashboard_that_exists() -> None:
    uids = {json.loads(path.read_text())["uid"] for path in DASHBOARDS_DIR.glob("ci-observe-*.json")}
    for link in DASHBOARD["links"]:
        uid = link["url"].split("/d/")[1].split("/")[0]
        assert uid in uids, link["url"]


def test_no_template_token_is_left_unresolved() -> None:
    known = set(variables()) | {"__data", "__value"}
    for text in re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", json.dumps(DASHBOARD)):
        assert text in known or text.startswith("__"), text
