"""Structural tests for the ci-observe Pull requests page.

`ci-observe-pull-requests.json` answers one question for one pull request:
where did its time go, what did it run, and what did the merge queue do?
The page reads the same endpoint as before, but its top is a data status
line, an identity header with named columns, five stage numbers, a stage
order line and a horizontal bar gauge; the tables below hold queue attempts
and E2E runs. The dropdown is the browser, so the page has no browse tables.

Every selector is resolved against a fixture document with the same keys as
`/api/ci-observe/prs`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

DASHBOARDS_DIR = Path(__file__).resolve().parents[1] / "config" / "grafana" / "dashboards"
DASHBOARD = json.loads((DASHBOARDS_DIR / "ci-observe-pull-requests.json").read_text())
PRS_URL = "http://127.0.0.1:9103/api/ci-observe/prs"
PR_URL = f"{PRS_URL}?number=${{pr_number}}&repo=${{pr_repo}}"

PR_DOCUMENT = {
    "pr": {
        "number": 101,
        "title": "Add VM restart coverage",
        "author": "author1",
        "author_url": "https://github.com/author1",
        "repo": "osac",
        "repo_url": "https://github.com/osac-project/osac",
        "state": "merged",
        "dates_display": "Opened 01 Oct 10:00 · Merged 01 Oct 11:35",
        "opened_display": "01 Oct 10:00",
        "merged_display": "01 Oct 11:35",
        "total_display": "1h 35m",
        "url": "https://github.com/osac-project/osac/pull/101",
    },
    "summary": [
        {
            "total": "1h 35m",
            "authoring": "20m · 21%",
            "review": "30m · 32%",
            "waiting": "10m · 11%",
            "queue": "34m · 36%",
            "merged": "1m · 1%",
        }
    ],
    "chart": [
        {
            "authoring": 0.333,
            "review": 0.5,
            "waiting": 0.167,
            "queue": 0.567,
            "merged": 0.017,
        }
    ],
    "data_status": "complete",
    "data_error": "",
    "status_message": "",
    "runs": [
        {
            "conclusion": "success",
            "status_display": "success",
            "failed_step_name": "",
            "code_display": "v1 · 1234567890ab",
            "commit_url": "https://github.com/osac-project/osac/commit/1234567890ab",
            "version": 1,
            "head": "1234567890ab",
            "suite": "VMaaS",
            "recorded": "yes",
            "created_at": "2026-10-01T10:30:00Z",
            "created_display": "01 Oct 10:30",
            "duration_display": "12m",
            "run_display": "9001",
            "run_link": "/d/ci-observe-runs/ci-observe-e28094-e2e-run?var-capture_id=x",
            "url": "https://github.com/osac-project/osac/actions/runs/9001",
            "run_url": "/d/ci-observe-runs/ci-observe-e28094-e2e-run?var-capture_id=x",
        }
    ],
    "attempts": [
        {
            "started_at": "2026-10-01T11:00:00Z",
            "started_display": "01 Oct 11:00",
            "duration_display": "5m",
            "queue_outcome": "ejected",
            "why_display": "The e2e / vmaas check failed.",
            "run_url": "https://github.com/osac-project/osac/actions/runs/5001",
            "comment_url": "https://github.com/osac-project/osac/pull/101#issuecomment-1",
            "why_url": "https://github.com/osac-project/osac/pull/101#issuecomment-1",
        }
    ],
}


def walk(panels: list[dict]) -> Iterator[dict]:
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


def override(table: dict, field: str) -> dict:
    for entry in table["fieldConfig"]["overrides"]:
        if entry["matcher"].get("options") == field:
            return {prop["id"]: prop["value"] for prop in entry["properties"]}
    raise AssertionError(f"{table['title']}: no override for {field}")


def hidden_fields(table: dict) -> set[str]:
    hidden = set()
    for entry in table["fieldConfig"]["overrides"]:
        field = entry["matcher"].get("options")
        for prop in entry["properties"]:
            if prop["id"] == "custom.hidden" and prop["value"] is True:
                hidden.add(field)
    return hidden


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
    assert DASHBOARD["uid"] == "ci-observe-pull-requests"
    assert DASHBOARD["title"] == "CI Observe — Pull requests"
    assert "ci-observe" in DASHBOARD["tags"]


def test_the_page_keeps_three_variables() -> None:
    assert set(variables()) == {"days", "pr_number", "pr_repo"}
    assert variables()["days"]["current"]["value"] == "30"
    assert variables()["pr_repo"]["hide"] == 0, "the repository is a visible control"
    assert variables()["pr_number"]["type"] == "query", "the PR is a drop-down of saved PRs"
    assert variables()["pr_number"]["allowCustomValue"] is True, "an open PR can still be typed"


def test_the_repository_control_defaults_to_osac() -> None:
    repo = variables()["pr_repo"]
    assert repo["type"] == "custom"
    assert repo["current"]["value"] == "osac"
    assert [option["value"] for option in repo["options"]] == ["osac", "osac-test-infra"]


def test_the_pull_request_control_lists_saved_pull_requests() -> None:
    pr_number = variables()["pr_number"]
    assert pr_number["datasource"] == {"type": "yesoreyeram-infinity-datasource", "uid": "workflow-api"}
    infinity = pr_number["query"]["infinityQuery"]
    assert pr_number["query"]["queryType"] == "infinity"
    assert infinity["type"] == "json"
    assert infinity["source"] == "url"
    assert infinity["parser"] == "backend"
    assert infinity["url"] == f"{PRS_URL}?days=${{days}}&repo=${{pr_repo}}"
    assert infinity["url_options"] == {"method": "GET", "data": ""}
    assert infinity["root_selector"] == "$.prs"
    assert infinity["format"] == "table"
    assert infinity["columns"] == [
        {"selector": "option_label", "text": "__text", "type": "string"},
        {"selector": "number", "text": "__value", "type": "string"},
    ]
    assert pr_number["refresh"] == 1
    assert pr_number["multi"] is False


def test_the_pull_request_options_resolve_against_the_index() -> None:
    row = {
        "repo": "osac",
        "number": 101,
        "title": "Add VM restart coverage",
        "final_stage": "merged",
        "option_label": "#101 · merged · Add VM restart coverage",
    }
    for column in variables()["pr_number"]["query"]["infinityQuery"]["columns"]:
        assert column["selector"] in row, column["selector"]


def test_the_data_status_line_says_complete_or_data_error() -> None:
    table = panel("Data status")
    assert table["type"] == "table"
    assert table["options"]["showHeader"] is True, "the header row names the two columns"
    target = table["targets"][0]
    assert target["url"] == PR_URL
    assert target["root_selector"] == "$"
    assert selectors(table) == ["data_status", "data_error"]
    assert display_names(table) == {"data_status": "Data", "data_error": "Details"}
    for column in target["columns"]:
        assert column["selector"] in PR_DOCUMENT, column["selector"]
    options = override(table, "data_status")["mappings"][0]["options"]
    assert options["complete"] == {"text": "Data complete", "color": "green", "index": 0}
    assert options["incomplete"] == {"text": "DATA ERROR", "color": "red", "index": 1}


def test_the_identity_header_is_one_row_with_named_columns() -> None:
    table = panel("Pull request")
    target = table["targets"][0]
    assert table["type"] == "table"
    assert target["url"] == PR_URL
    assert target["root_selector"] == "$.pr"
    assert_selectors_resolve(table, PR_DOCUMENT)
    assert table["options"]["showHeader"] is True, "a static row still needs its label row"
    assert hidden_fields(table) == {"url", "author_url", "repo_url"}
    visible = [field for field in selectors(table) if field not in hidden_fields(table)]
    assert visible == [
        "title",
        "number",
        "state",
        "repo",
        "author",
        "opened_display",
        "merged_display",
        "total_display",
    ], "Title first, then PR, State, Repository, Author, Opened, Merged, Total"
    assert display_names(table) == {
        "title": "Title",
        "number": "PR",
        "state": "State",
        "repo": "Repository",
        "author": "Author",
        "opened_display": "Opened",
        "merged_display": "Merged",
        "total_display": "Total",
    }
    for field, url_field in (
        ("title", "url"),
        ("number", "url"),
        ("repo", "repo_url"),
        ("author", "author_url"),
    ):
        links = override(table, field)["links"]
        assert links == [{"title": links[0]["title"], "url": f"${{__data.fields.{url_field}}}"}], field
    assert override(table, "title")["custom.cellOptions"]["wrapText"] is True
    assert override(table, "title")["custom.width"] > override(table, "number")["custom.width"]


STAGE_TILES = {
    "Before review": "authoring",
    "In review": "review",
    "Waiting to queue": "waiting",
    "Queue checks": "queue",
    "Merge": "merged",
}


def test_five_stage_tiles_read_their_own_summary_field() -> None:
    stat_titles = {
        candidate["title"] for candidate in walk(DASHBOARD["panels"]) if candidate["type"] == "stat"
    }
    assert stat_titles == set(STAGE_TILES), "five stage tiles; there is no Total tile"
    for title, field in STAGE_TILES.items():
        tile = panel(title)
        target = tile["targets"][0]
        assert target["url"] == PR_URL
        assert target["root_selector"] == "$.summary"
        assert selectors(tile) == [field]
        assert tile["fieldConfig"]["defaults"]["noValue"] == "No data"
        # An empty field matcher means "numeric fields only" in Grafana, and
        # the summary values are strings. Select the string field by name.
        reduce = tile["options"]["reduceOptions"]
        assert reduce["fields"] == field, f"{title} must select its string field"
        assert reduce["calcs"] == ["lastNotNull"], f"{title} needs a string-safe reducer"
        assert reduce["values"] is False
        row = resolve(PR_DOCUMENT, "$.summary")
        assert row[field], f"{title} must resolve"


def test_the_tiles_show_their_stage_definition() -> None:
    assert panel("Before review")["description"] == "Opened to first human review."
    assert panel("In review")["description"] == "First human review to first approval."
    assert panel("Waiting to queue")["description"] == "Approval to first queue run."
    assert panel("Queue checks")["description"] == "First queue run to last queue run."
    assert panel("Merge")["description"] == "Last queue run to merged."


def test_missing_states_have_no_status_zero() -> None:
    assert panel("Pull request")["fieldConfig"]["defaults"]["noValue"] == "Choose a pull request above."
    assert (
        panel("E2E runs")["fieldConfig"]["defaults"]["noValue"]
        == "No E2E runs. The job history may not be written yet."
    )


def test_no_panel_says_not_measured() -> None:
    assert "Not measured" not in json.dumps(DASHBOARD)
    assert "not measured" not in json.dumps(DASHBOARD)


def test_the_stage_chain_names_the_boundaries_in_order() -> None:
    chain = panel("Stage order")
    assert chain["type"] == "text"
    assert chain["options"]["content"] == (
        "Opened → first review → approval → queue start → last queue run → merged"
    )


def test_the_stage_graph_is_a_horizontal_bar_gauge() -> None:
    graph = panel("Stage graph")
    assert graph["type"] == "bargauge"
    target = graph["targets"][0]
    assert target["url"] == PR_URL
    assert target["root_selector"] == "$.chart"
    assert target["format"] == "table"
    assert target["parser"] == "backend"
    assert selectors(graph) == ["authoring", "review", "waiting", "queue", "merged"], "fixed stage order"
    assert {column["type"] for column in target["columns"]} == {"number"}
    assert_selectors_resolve(graph, PR_DOCUMENT)
    assert graph["fieldConfig"]["defaults"]["unit"] == "h"
    assert graph["fieldConfig"]["defaults"]["noValue"] == "Not reached"
    options = graph["options"]
    assert options["orientation"] == "horizontal"
    assert "xField" not in options, "a bar gauge draws one row per numeric field"
    assert options["showUnfilled"] is True
    assert options["legend"]["showLegend"] is False, "the stage names label the rows"
    assert DASHBOARD["time"]["from"] == "now-30d"
    assert DASHBOARD["timepicker"]["hidden"] is True


def test_the_graph_has_one_color_and_the_stage_names() -> None:
    graph = panel("Stage graph")
    assert graph["fieldConfig"]["defaults"]["color"] == {"mode": "fixed", "fixedColor": "#1f6feb"}
    for stage, name in (
        ("authoring", "Before review"),
        ("review", "In review"),
        ("waiting", "Waiting to queue"),
        ("queue", "Queue checks"),
        ("merged", "Merge"),
    ):
        assert override(graph, stage)["displayName"] == name
    for entry in graph["fieldConfig"]["overrides"]:
        assert "color" not in {prop["id"] for prop in entry["properties"]}, "one blue for every stage"


def test_the_bottom_status_table_is_the_only_place_for_explanations() -> None:
    table = panel("Status")
    assert table["type"] == "table"
    assert table["options"]["showHeader"] is False
    assert table["fieldConfig"]["defaults"]["noValue"] == ""
    assert table["gridPos"]["y"] == max(candidate["gridPos"]["y"] for candidate in walk(DASHBOARD["panels"]))
    target = table["targets"][0]
    assert target["url"] == PR_URL
    assert target["root_selector"] == "$"
    assert selectors(table) == ["status_message"]


def test_the_queue_table_names_four_columns() -> None:
    table = panel("Queue attempts")
    assert table["targets"][0]["root_selector"] == "$.attempts"
    assert table["targets"][0]["url"] == PR_URL
    assert_selectors_resolve(table, PR_DOCUMENT)
    assert display_names(table) == {
        "started_display": "Entered",
        "duration_display": "Duration",
        "queue_outcome": "Outcome",
        "why_display": "Why",
    }
    assert override(table, "started_display")["links"][0]["url"] == "${__data.fields.run_url}"
    assert override(table, "queue_outcome")["links"][0]["url"] == "${__data.fields.why_url}"
    assert override(table, "why_display")["links"][0]["url"] == "${__data.fields.why_url}"
    assert override(table, "why_display")["noValue"] == "DATA ERROR", "Why is never blank"
    for helper in ("run_url", "comment_url", "why_url"):
        assert override(table, helper)["custom.hidden"] is True


def test_the_runs_table_is_newest_first_and_linkable() -> None:
    table = panel("E2E runs")
    assert table["targets"][0]["root_selector"] == "$.runs"
    assert table["targets"][0]["url"] == f"{PR_URL}&days=${{days}}"
    assert_selectors_resolve(table, PR_DOCUMENT)
    assert display_names(table) == {
        "status_display": "Status",
        "failed_step_name": "Failed step",
        "code_display": "Code",
        "suite": "Suite",
        "recorded": "Test data",
        "created_display": "Started",
        "duration_display": "Duration",
        "run_display": "Run",
    }
    assert override(table, "code_display")["links"][0]["url"] == "${__data.fields.commit_url}"
    assert override(table, "run_display")["links"][0]["url"] == "${__data.fields.run_link}"
    for helper in ("conclusion", "version", "head", "commit_url", "url", "run_url", "run_link"):
        assert override(table, helper)["custom.hidden"] is True, helper
    assert table["fieldConfig"]["defaults"]["custom"]["filterable"] is True


def test_the_old_panels_and_names_are_gone() -> None:
    titles = {candidate.get("title") for candidate in walk(DASHBOARD["panels"])}
    for removed in (
        "Details",
        "Time in each stage",
        "All pull requests",
        "Recent pull requests, last $days days",
        "Stage times",
        "Total",
        "Authoring",
        "Review",
        "Waiting",
        "Queue",
        "Stage timeline",
    ):
        assert removed not in titles, removed
    assert not any(candidate["type"] == "state-timeline" for candidate in walk(DASHBOARD["panels"]))


def test_every_detail_panel_reads_the_selected_pull_request() -> None:
    for candidate in walk(DASHBOARD["panels"]):
        for target in candidate.get("targets", []):
            assert target["url"].startswith(PR_URL), candidate["title"]


def test_every_link_opens_a_dashboard_that_exists() -> None:
    uids = {json.loads(path.read_text())["uid"] for path in DASHBOARDS_DIR.glob("ci-observe-*.json")}
    for link in DASHBOARD["links"]:
        uid = link["url"].split("/d/")[1].split("/")[0]
        assert uid in uids, link["url"]


def test_no_template_token_is_left_unresolved() -> None:
    known = set(variables()) | {"__data", "__value"}
    for text in re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", json.dumps(DASHBOARD)):
        assert text in known or text.startswith("__"), text
