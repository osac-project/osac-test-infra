"""Structural tests for the ci-observe Pull requests page.

`ci-observe-pull-requests.json` answers one question for one pull request:
where did its time go, what did it run, and what did the merge queue do?
The page reads the same endpoint as before, but its top is an identity line,
five numbers and a stacked stage chart; the tables below hold queue attempts and
E2E runs. The dropdown is the browser, so the page has no browse tables.

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
        "url": "https://github.com/osac-project/osac/pull/101",
    },
    "summary": [
        {
            "total": "1h 35m",
            "authoring": "20m",
            "review": "30m",
            "waiting": "10m",
            "queue": "34m",
            "total_s": 5700,
            "authoring_s": 1200,
            "review_s": 1800,
            "waiting_s": 600,
            "queue_s": 2040,
        }
    ],
    "chart": [
        {
            "category": "PR #101",
            "authoring": 1200,
            "review": 1800,
            "waiting": 600,
            "queue": 2040,
            "merged": 60,
        }
    ],
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
            "why": "The e2e / vmaas check failed.",
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


def test_the_identity_line_is_one_linkable_row() -> None:
    table = panel("Pull request")
    target = table["targets"][0]
    assert table["type"] == "table"
    assert target["url"] == PR_URL
    assert target["root_selector"] == "$.pr"
    assert_selectors_resolve(table, PR_DOCUMENT)
    assert table["options"]["showHeader"] is False
    assert display_names(table) == {
        "number": "PR",
        "title": "Title",
        "author": "Author",
        "repo": "Repository",
        "state": "State",
        "dates_display": "Dates",
    }

    for field, url_field in (("number", "url"), ("title", "url"), ("author", "author_url"), ("repo", "repo_url")):
        links = override(table, field)["links"]
        assert links == [{"title": links[0]["title"], "url": f"${{__data.fields.{url_field}}}"}], field
    for helper in ("url", "author_url", "repo_url"):
        assert override(table, helper)["custom.hidden"] is True, helper


def test_five_stat_tiles_read_the_summary_row() -> None:
    # Seconds, shown as every other duration on the pages (hh:mm:ss).
    tiles = {
        "Total": "total_s",
        "Authoring": "authoring_s",
        "Review": "review_s",
        "Waiting": "waiting_s",
        "Queue": "queue_s",
    }
    for title, field in tiles.items():
        tile = panel(title)
        assert tile["type"] == "stat"
        assert tile["fieldConfig"]["defaults"]["unit"] == "dtdhms", title
        target = tile["targets"][0]
        assert target["url"] == PR_URL
        assert target["root_selector"] == "$.summary"
        assert selectors(tile) == [field]
        assert tile["fieldConfig"]["defaults"]["noValue"].startswith("Not measured"), title
        row = resolve(PR_DOCUMENT, "$.summary")
        assert row[field], f"{title} must resolve"


def test_missing_states_have_no_status_zero() -> None:
    assert panel("Pull request")["fieldConfig"]["defaults"]["noValue"] == "Choose a pull request above."
    assert panel("Total")["fieldConfig"]["defaults"]["noValue"] == "Not measured."
    assert (
        panel("E2E runs")["fieldConfig"]["defaults"]["noValue"]
        == "No E2E run is stored for this pull request: it ran none, or ran them before the stored history began."
    )


def test_the_stage_chart_is_a_stacked_horizontal_bar() -> None:
    chart = panel("Stage timeline")
    assert chart["type"] == "barchart"
    assert chart["description"] == "Where the time went. A missing stage is absent, never zero."
    target = chart["targets"][0]
    assert target["url"] == PR_URL
    assert target["root_selector"] == "$.chart"
    assert target["format"] == "table"
    assert target["parser"] == "backend"
    columns = {column["selector"]: column["type"] for column in target["columns"]}
    assert columns == {
        "category": "string",
        "authoring": "number",
        "review": "number",
        "waiting": "number",
        "queue": "number",
        "merged": "number",
    }
    assert selectors(chart)[1:] == ["authoring", "review", "waiting", "queue", "merged"]
    assert_selectors_resolve(chart, PR_DOCUMENT)


def test_the_chart_is_fitted_to_durations_and_not_a_time_range() -> None:
    options = panel("Stage timeline")["options"]
    assert options["orientation"] == "horizontal"
    assert options["stacking"] == "normal"
    assert options["xField"] == "category"
    assert options["legend"] == {"showLegend": True, "displayMode": "list", "placement": "bottom", "calcs": []}
    assert options["tooltip"]["mode"] == "single"
    assert panel("Stage timeline")["fieldConfig"]["defaults"]["unit"] == "dtdhms"
    assert DASHBOARD["time"]["from"] == "now-30d"
    assert DASHBOARD["timepicker"]["hidden"] is True


# The stages wear the first five ci-observe palette colors in the order a
# pull request goes through them, here and in the weekly chart above.
STAGE_COLORS = {
    "authoring": "#3987e5",
    "review": "#d95926",
    "waiting": "#199e70",
    "queue": "#c98500",
    "merged": "#d55181",
}


def test_each_stage_series_has_its_name_and_fixed_color() -> None:
    chart = panel("Stage timeline")
    names = {"authoring": "Authoring", "review": "Review", "waiting": "Waiting", "queue": "Queue", "merged": "Merge"}
    for stage, name in names.items():
        assert override(chart, stage)["displayName"] == name
        assert override(chart, stage)["color"] == {"mode": "fixed", "fixedColor": STAGE_COLORS[stage]}


def test_the_weekly_chart_wears_the_stage_colors() -> None:
    chart = panel("Stage medians per week")
    fields = {
        "review_wait_median_s": "authoring",
        "review_median_s": "review",
        "waiting_median_s": "waiting",
        "queue_median_s": "queue",
    }
    for field, stage in fields.items():
        assert override(chart, field)["color"] == {"mode": "fixed", "fixedColor": STAGE_COLORS[stage]}


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
        "why": "Why",
    }
    assert override(table, "started_display")["links"][0]["url"] == "${__data.fields.run_url}"
    assert override(table, "queue_outcome")["links"][0]["url"] == "${__data.fields.why_url}"
    assert override(table, "why")["links"][0]["url"] == "${__data.fields.why_url}"
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


def test_the_browse_tables_are_gone() -> None:
    titles = {candidate.get("title") for candidate in walk(DASHBOARD["panels"])}
    for removed in (
        "Details",
        "Time in each stage",
        "All pull requests",
        "Recent pull requests, last $days days",
        "Stage times",
    ):
        assert removed not in titles, removed
    assert not any(candidate["type"] == "text" for candidate in walk(DASHBOARD["panels"]))
    assert not any(candidate["type"] == "state-timeline" for candidate in walk(DASHBOARD["panels"]))


def sections() -> tuple[list[dict], list[dict]]:
    """(every pull request, one pull request): the panels above and below
    the "One pull request" row."""
    top = DASHBOARD["panels"]
    split = next(index for index, candidate in enumerate(top)
                 if candidate["type"] == "row" and candidate["title"].startswith("One pull request"))
    return top[:split], top[split + 1:]


def test_every_detail_panel_reads_the_selected_pull_request() -> None:
    _, detail = sections()
    for candidate in walk(detail):
        for target in candidate.get("targets", []):
            assert target["url"].startswith(PR_URL), candidate["title"]


INDEX_URL = f"{PRS_URL}?days=${{days}}&repo=${{pr_repo}}"


def test_the_page_opens_on_every_pull_request_of_the_window() -> None:
    """Above the one-PR detail: the window's numbers, the stage medians per
    week and every pull request, all from the index document of the
    selected repository."""
    overview, _ = sections()
    assert overview[0]["type"] == "row"
    # A row title shows a variable's text ("30 days"), not its value.
    assert overview[0]["title"] == "All pull requests in ${pr_repo}, last ${days:text}"
    tiles = [candidate for candidate in overview if candidate["type"] == "stat"]
    assert len(tiles) == 16
    for tile in tiles:
        target = tile["targets"][0]
        assert target["url"] == INDEX_URL, tile["title"]
        assert target["root_selector"] == "kpis", tile["title"]
        assert "Population:" in tile["description"] and "Unit:" in tile["description"], tile["title"]
    fields = {tile["targets"][0]["columns"][0]["selector"] for tile in tiles}
    assert {"review_wait_median_s", "review_median_s", "waiting_median_s", "queue_median_s", "rebased_share",
            "force_pushes_mean", "e2e_retests_median", "ejected_share"} <= fields
    weekly = panel("Stage medians per week")
    assert weekly["targets"][0]["root_selector"] == "weekly"
    assert weekly["targets"][0]["format"] == "timeseries"
    table = panel("Pull requests")
    assert table["targets"][0]["url"] == INDEX_URL
    assert table["targets"][0]["root_selector"] == "prs"
    assert override(table, "number")["links"][0]["url"] == "${__data.fields.detail_url}"
    assert override(table, "title")["links"][0]["url"] == "${__data.fields.url}"
    assert {"force_push_count", "e2e_runs", "e2e_failures"} <= set(selectors(table))


def test_every_link_opens_a_dashboard_that_exists() -> None:
    uids = {json.loads(path.read_text())["uid"] for path in DASHBOARDS_DIR.glob("ci-observe-*.json")}
    for link in DASHBOARD["links"]:
        uid = link["url"].split("/d/")[1].split("/")[0]
        assert uid in uids, link["url"]


def test_no_template_token_is_left_unresolved() -> None:
    known = set(variables()) | {"__data", "__value"}
    for text in re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", json.dumps(DASHBOARD)):
        assert text in known or text.startswith("__"), text
