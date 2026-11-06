"""Structural tests for the ci-observe entry page.

`ci-observe-runs-home.json` lists recent E2E runs and opens one. It has a
heading and one table. There is no lookup box: the table is the way in.

Every selector is resolved against a fixture document with the same keys as
`/api/ci-observe/runs`, so a column rename fails here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

DASHBOARDS_DIR = Path(__file__).resolve().parents[1] / "config" / "grafana" / "dashboards"
DASHBOARD = json.loads((DASHBOARDS_DIR / "ci-observe-runs-home.json").read_text())
RUNS_URL = "http://127.0.0.1:9103/api/ci-observe/runs"

# The response contract, pinned here so this file passes without a backend.
RUNS_DOCUMENT = {
    "days": 30,
    "runs": [
        {
            "id": "41310072671",
            "workflow": "E2E VMaaS Full Install",
            "repo": "osac-test-infra",
            "branch": "main",
            "trigger": "scheduled",
            "pr": "",
            "pr_url": "",
            "conclusion": "failure",
            "created_at": "2026-11-03T10:00:00Z",
            "started_at_ms": 1762164060000,
            "done_at_ms": 1762166520000,
            "url": "https://github.com/osac-project/osac/actions/runs/41310072671",
            "capture_state": "capture",
            "capture_id": "vmaas-pr123-41310072671-a1",
        },
        {
            "id": "41310072672",
            "workflow": "E2E VMaaS Full Install",
            "repo": "osac",
            "branch": "refs/pull/1620/head",
            "trigger": "PR",
            "pr": "#1620",
            "pr_url": "https://github.com/osac-project/osac/pull/1620",
            "conclusion": "success",
            "created_at": "2026-11-03T09:00:00Z",
            "started_at_ms": 1762160460000,
            "done_at_ms": 1762163400000,
            "url": "https://github.com/osac-project/osac/actions/runs/41310072672",
            "capture_state": "not collected",
            "capture_id": "",
        },
    ],
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


def override(table: dict, field: str) -> dict | None:
    return next(
        (entry for entry in table["fieldConfig"]["overrides"] if entry["matcher"].get("options") == field),
        None,
    )


def display_name(table: dict, field: str) -> str | None:
    entry = override(table, field)
    if entry is None:
        return None
    for prop in entry["properties"]:
        if prop["id"] == "displayName":
            return prop["value"]
    return None


def test_identity_and_folder() -> None:
    assert DASHBOARD["uid"] == "ci-observe-runs-home"
    assert DASHBOARD["title"] == "CI Observe — Start here"
    assert "ci-observe" in DASHBOARD["tags"]


def test_the_page_is_a_heading_and_one_table() -> None:
    assert [panel["type"] for panel in DASHBOARD["panels"]] == ["text", "table"]


def test_the_only_variable_is_days() -> None:
    assert set(variables()) == {"days"}
    days = variables()["days"]
    assert days["current"]["value"] == "30"
    assert {"7", "30"} <= {option["value"] for option in days["options"]}


def test_the_table_reads_the_runs_endpoint() -> None:
    table = panel("Recent E2E runs, last $days days")
    target = table["targets"][0]
    assert target["url"] == f"{RUNS_URL}?days=${{days}}"
    assert target["root_selector"] == "$.runs"
    assert target["format"] == "table"


def test_every_column_resolves_against_the_endpoint_contract() -> None:
    table = panel("Recent E2E runs, last $days days")
    row = RUNS_DOCUMENT["runs"][0]
    for column in selectors(table):
        assert column in row, column
    assert "capture_id" in selectors(table), "the run link needs the capture id"
    assert "started_at_ms" in selectors(table), "the run link needs the start time"
    assert "done_at_ms" in selectors(table), "the run link needs the finish time"


def test_column_labels_are_plain_words() -> None:
    table = panel("Recent E2E runs, last $days days")
    assert {
        "id": "Run",
        "workflow": "Job",
        "repo": "Repo",
        "pr": "PR",
        "branch": "Branch",
        "trigger": "Trigger",
        "conclusion": "Result",
        "capture_state": "Recorded",
        "created_at": "Created",
        "url": "GitHub",
    } == {
        field: display_name(table, field)
        for field in (
            "id", "workflow", "repo", "pr", "branch", "trigger",
            "conclusion", "capture_state", "created_at", "url",
        )
    }


def test_a_run_opens_the_e2e_run_page_with_its_own_window() -> None:
    table = panel("Recent E2E runs, last $days days")
    links = next(prop["value"] for prop in override(table, "id")["properties"] if prop["id"] == "links")
    url = links[0]["url"]
    assert "/d/ci-observe-runs/" in url
    assert "var-capture_id=${__data.fields.capture_id}" in url
    assert "from=${__data.fields.started_at_ms}" in url
    assert "to=${__data.fields.done_at_ms}" in url


def test_a_run_opens_github() -> None:
    table = panel("Recent E2E runs, last $days days")
    links = next(prop["value"] for prop in override(table, "url")["properties"] if prop["id"] == "links")
    assert links[0]["url"] == "${__value.raw}"


def test_every_link_opens_a_dashboard_that_exists() -> None:
    uids = {json.loads(path.read_text())["uid"] for path in DASHBOARDS_DIR.glob("ci-observe-*.json")}
    for link in DASHBOARD["links"]:
        uid = link["url"].split("/d/")[1].split("/")[0]
        assert uid in uids, link["url"]


def test_no_template_token_is_left_unresolved() -> None:
    known = set(variables()) | {"__data", "__value"}
    for text in re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", json.dumps(DASHBOARD)):
        assert text in known or text.startswith("__"), text
