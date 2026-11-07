"""Structural tests for the ci-observe entry page.

`ci-observe-runs-home.json` lists recent E2E runs and opens one. It has a
heading and one table. The filters narrow the data, not just the visible
rows; the table also enables column filters for ad-hoc narrowing.

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
            "pr_number": "",
            "pr_url": "",
            "conclusion": "failure",
            "created_at": "2026-11-03T10:00:00Z",
            "url": "https://github.com/osac-project/osac/actions/runs/41310072671",
            "capture_state": "capture",
            "capture_id": "vmaas-pr123-41310072671-a1",
            "failure_reason": "infra",
            "failure_area": "setup",
            "failed_step_name": "Build and load component images",
            "failed_step": "vmaas / e2e-vmaas-full-install / build → Build and load component images",
            "details_url": "/d/ci-observe-runs/ci-observe-e28094-e2e-run?var-capture_id=x",
        },
        {
            "id": "41310072672",
            "workflow": "E2E VMaaS Full Install",
            "repo": "osac",
            "branch": "refs/pull/1620/head",
            "trigger": "PR",
            "pr": "#1620",
            "pr_number": "1620",
            "pr_url": "https://github.com/osac-project/osac/pull/1620",
            "conclusion": "success",
            "created_at": "2026-11-03T09:00:00Z",
            "url": "https://github.com/osac-project/osac/actions/runs/41310072672",
            "capture_state": "not collected",
            "capture_id": "",
            "failure_reason": "",
            "failure_area": "",
            "failed_step_name": "",
            "failed_step": "",
            "details_url": "https://github.com/osac-project/osac/actions/runs/41310072672",
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


def link_url(table: dict, field: str) -> str:
    entry = override(table, field)
    return next(prop["value"] for prop in entry["properties"] if prop["id"] == "links")[0]["url"]


def test_identity_and_folder() -> None:
    assert DASHBOARD["uid"] == "ci-observe-runs-home"
    assert DASHBOARD["title"] == "CI Observe — Start here"
    assert "ci-observe" in DASHBOARD["tags"]


def test_the_page_is_a_heading_and_one_table() -> None:
    assert [panel["type"] for panel in DASHBOARD["panels"]] == ["text", "table"]


def test_the_heading_explains_the_filters_and_the_github_fallback() -> None:
    heading = next(candidate for candidate in DASHBOARD["panels"] if candidate["type"] == "text")
    content = heading["options"]["content"].lower()
    assert "filter by repository, suite or result" in content
    assert "no test data opens github" in content
    # "Recorded: No" is not a mystery: the heading names the column that says
    # which step the run stopped in.
    assert "**no** under recorded means no test data" in content
    assert "failed step" in content
    assert "install osac" in content


def test_the_table_description_explains_recorded_and_the_failed_step() -> None:
    table = panel("Recent E2E runs, last $days days")
    description = table["description"].lower()
    assert "no under recorded means no test data" in description
    assert "failed step" in description
    assert "column filter narrows by it" in description


def test_the_filters_are_days_repository_suite_and_result() -> None:
    assert set(variables()) == {"days", "repo", "suite", "result"}
    assert variables()["days"]["current"]["value"] == "30"
    assert {"7", "30"} <= {option["value"] for option in variables()["days"]["options"]}
    for name in ("repo", "suite", "result"):
        variable = variables()[name]
        assert variable["current"]["value"] == "all", f"{name} defaults to All"
        assert variable["type"] == "custom"
        assert variable["query"].startswith("All : all,"), f"{name} has an All option"
        assert {"text": "All", "value": "all", "selected": True} in variable["options"]


def test_the_result_options_match_the_endpoint(runs) -> None:
    values = [option["value"] for option in variables()["result"]["options"]]
    assert values == ["all", "success", "failure", "cancelled"]
    for value in values:
        assert value == "all" or value in runs.RESULTS, value


def test_the_table_reads_the_runs_endpoint_with_every_filter() -> None:
    table = panel("Recent E2E runs, last $days days")
    target = table["targets"][0]
    assert target["url"] == (
        f"{RUNS_URL}?days=${{days}}&repo=${{repo}}&suite=${{suite}}&result=${{result}}"
    )
    assert target["root_selector"] == "$.runs"
    assert target["format"] == "table"


def test_every_column_resolves_against_the_endpoint_contract() -> None:
    table = panel("Recent E2E runs, last $days days")
    row = RUNS_DOCUMENT["runs"][0]
    for column in selectors(table):
        assert column in row, column
    assert "details_url" in selectors(table), "the run link needs its details url"
    assert "pr_number" in selectors(table), "the PR link needs the bare number"


def test_the_old_recording_columns_are_gone() -> None:
    table = panel("Recent E2E runs, last $days days")
    for gone in ("capture_id", "started_at_ms", "done_at_ms"):
        assert gone not in selectors(table), gone
        assert override(table, gone) is None, gone


def test_the_link_fields_are_hidden() -> None:
    table = panel("Recent E2E runs, last $days days")
    for field in ("pr_number", "details_url"):
        entry = override(table, field)
        hidden = next(prop["value"] for prop in entry["properties"] if prop["id"] == "custom.hidden")
        assert hidden is True, field


def test_the_failed_step_is_available_but_hidden() -> None:
    table = panel("Recent E2E runs, last $days days")
    assert "failed_step" in selectors(table)
    entry = override(table, "failed_step")
    hidden = next(prop["value"] for prop in entry["properties"] if prop["id"] == "custom.hidden")
    assert hidden is True


def test_the_failed_step_column_resolves_and_is_filterable() -> None:
    """A failed run names the exact step it stopped in. A pass or a
    cancelled run leaves the cell blank."""
    table = panel("Recent E2E runs, last $days days")
    assert display_name(table, "failed_step_name") == "Failed step"
    assert "failed_step_name" in selectors(table)
    entry = override(table, "failed_step_name")
    properties = {prop["id"]: prop["value"] for prop in entry["properties"]}
    assert properties["noValue"] == "", "a blank step must not show the table's empty message"
    assert "mappings" not in properties, "the step is shown as stored, not mapped to an area"
    assert table["fieldConfig"]["defaults"]["custom"]["filterable"] is True


def test_the_area_column_is_gone_but_failure_area_stays_in_the_api() -> None:
    table = panel("Recent E2E runs, last $days days")
    assert "failure_area" not in selectors(table)
    assert override(table, "failure_area") is None
    assert RUNS_DOCUMENT["runs"][0]["failure_area"] == "setup"


def test_the_newest_run_is_first() -> None:
    table = panel("Recent E2E runs, last $days days")
    assert table["options"]["sortBy"] == [{"displayName": "Started", "desc": True}]


def test_column_labels_are_plain_words() -> None:
    table = panel("Recent E2E runs, last $days days")
    assert {
        "id": "Run",
        "workflow": "Workflow",
        "repo": "Repository",
        "pr": "PR",
        "branch": "Branch",
        "trigger": "Started by",
        "conclusion": "Result",
        "capture_state": "Recorded",
        "failed_step_name": "Failed step",
        "created_at": "Started",
        "url": "GitHub",
    } == {
        field: display_name(table, field)
        for field in (
            "id", "workflow", "repo", "pr", "branch", "trigger",
            "conclusion", "capture_state", "failed_step_name", "created_at", "url",
        )
    }


def test_a_run_opens_its_details_url_or_github_when_unrecorded() -> None:
    table = panel("Recent E2E runs, last $days days")
    assert link_url(table, "id") == "${__data.fields.details_url}"


def test_a_pr_cell_opens_the_pull_requests_page() -> None:
    table = panel("Recent E2E runs, last $days days")
    url = link_url(table, "pr")
    assert "/d/ci-observe-pull-requests/" in url
    assert "var-pr_number=${__data.fields.pr_number}" in url
    assert "var-pr_repo=${__data.fields.repo}" in url


def test_a_run_opens_github() -> None:
    table = panel("Recent E2E runs, last $days days")
    assert link_url(table, "url") == "${__value.raw}"


def test_the_table_can_be_filtered() -> None:
    table = panel("Recent E2E runs, last $days days")
    assert table["fieldConfig"]["defaults"]["custom"]["filterable"] is True


def test_every_link_opens_a_dashboard_that_exists() -> None:
    uids = {json.loads(path.read_text())["uid"] for path in DASHBOARDS_DIR.glob("ci-observe-*.json")}
    for link in DASHBOARD["links"]:
        uid = link["url"].split("/d/")[1].split("/")[0]
        assert uid in uids, link["url"]


def test_no_template_token_is_left_unresolved() -> None:
    known = set(variables()) | {"__data", "__value"}
    for text in re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", json.dumps(DASHBOARD)):
        assert text in known or text.startswith("__"), text
