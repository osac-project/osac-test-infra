"""Structural tests for the ci-observe Pull requests page.

`ci-observe-pull-requests.json` answers two questions. Above the "One pull
request" row: how do the pull requests of the window move, filtered by
repository, author and working groups to approve: tiles, the stage medians
per week, every merged pull request as a dot, time to merge per week, where
the time goes by working groups and by files, review rounds and queue
entries, and the slowest pull requests stretch by stretch. Below it, for one
pull request: where did its time go (an identity line, five numbers, the
lifecycle bar, every event and its numbers), what did it run, and what did
the merge queue do.

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
INDEX_URL = f"{PRS_URL}?days=${{days}}&repo=${{pr_repo}}&author=${{pr_author:percentencode}}&wgs=${{wgs}}"

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
    "lifecycle": [{"category": "PR #101", "01 · Waiting for a reviewer": 1200, "02 · Approved, not queued": 600}],
    "events": [
        {
            "at": "2026-10-01T10:00:00Z",
            "when": "01 Oct 10:00",
            "since_open_s": 0,
            "what": "Opened",
            "who": "author1",
            "detail": "",
            "url": "https://github.com/osac-project/osac/pull/101",
            "kind": "opened",
        }
    ],
    "facts": [{"fact": "Review rounds", "value": "2"}],
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


def test_the_page_has_five_controls() -> None:
    assert [variable["name"] for variable in DASHBOARD["templating"]["list"]] == [
        "days", "pr_repo", "pr_author", "wgs", "pr_number",
    ]
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
    assert infinity["url"] == INDEX_URL, "the list follows the author and working-group filters"
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


def test_the_author_and_working_group_filters() -> None:
    author = variables()["pr_author"]
    infinity = author["query"]["infinityQuery"]
    assert author["type"] == "query"
    assert infinity["url"] == f"{PRS_URL}?days=${{days}}&repo=${{pr_repo}}", "the list does not filter itself"
    assert infinity["root_selector"] == "$.authors"
    assert [column["text"] for column in infinity["columns"]] == ["__text", "__value"]
    assert (author["includeAll"], author["allValue"], author["current"]["value"]) == (True, "all", "$__all")
    wgs = variables()["wgs"]
    assert wgs["type"] == "custom"
    assert [option["value"] for option in wgs["options"]] == ["all", "0", "1", "2", "3plus"]
    assert wgs["current"]["value"] == "all"
    assert wgs["query"] == ",".join(f"{option['text']} : {option['value']}" for option in wgs["options"])


def test_the_lifecycle_is_a_stacked_horizontal_bar_in_event_order() -> None:
    chart = panel("Lifecycle")
    assert chart["type"] == "barchart"
    assert "Population:" in chart["description"] and "Unit: duration." in chart["description"]
    target = chart["targets"][0]
    assert target["url"] == PR_URL
    assert target["root_selector"] == "lifecycle"
    # The API names a field per stretch ("01 · Waiting for a reviewer"), in
    # order; the default parser keeps that order.
    assert target["columns"] == []
    assert "parser" not in target
    options = chart["options"]
    assert (options["orientation"], options["stacking"], options["xField"]) == ("horizontal", "normal", "category")
    assert options["showValue"] == "never"
    assert options["legend"]["displayMode"] == "table" and options["legend"]["calcs"] == ["sum"]
    assert chart["fieldConfig"]["defaults"]["unit"] == "dtdhms"
    assert chart["fieldConfig"]["defaults"]["noValue"].startswith("Not measured yet")
    assert DASHBOARD["time"]["from"] == "now-30d"
    assert DASHBOARD["timepicker"]["hidden"] is True
    assert "Stage timeline" not in {candidate.get("title") for candidate in walk(DASHBOARD["panels"])}


def regex_colors(chart: dict) -> dict[str, str]:
    return {
        entry["matcher"]["options"]: prop["value"]["fixedColor"]
        for entry in chart["fieldConfig"]["overrides"]
        if entry["matcher"]["id"] == "byRegexp"
        for prop in entry["properties"] if prop["id"] == "color"
    }


# A lifecycle state that continues one of the five stages wears its color.
STATE_COLORS = {
    "draft": "#898781",
    "waiting_review": "#3987e5",
    "with_author": "#d95926",
    "partly_approved": "#9085e9",
    "approved": "#199e70",
    "in_queue": "#c98500",
    "out_of_queue": "#e66767",
}


def test_every_lifecycle_state_wears_one_color_on_every_chart(prs) -> None:
    assert [key for key, _ in prs.LIFECYCLE_STATES] == list(STATE_COLORS)
    bar = regex_colors(panel("Lifecycle"))
    slowest = regex_colors(panel("The slowest pull requests, stretch by stretch"))
    for key, label in prs.LIFECYCLE_STATES:
        color = STATE_COLORS[key]
        assert [value for pattern, value in bar.items() if re.search(pattern.strip("/"), f"07 · {label}")] == [
            color
        ], label
        assert [value for pattern, value in slowest.items() if re.search(pattern.strip("/"), f"{label} (3)")] == [
            color
        ], label
        for title in ("Where the time goes, by working groups to approve", "Where the time goes, by files changed"):
            split = panel(title)
            assert override(split, key)["color"] == {"mode": "fixed", "fixedColor": color}, (title, key)
            assert override(split, key)["displayName"] == label, (title, key)
    assert STATE_COLORS["waiting_review"] == STAGE_COLORS["authoring"]
    assert STATE_COLORS["with_author"] == STAGE_COLORS["review"]
    assert STATE_COLORS["approved"] == STAGE_COLORS["waiting"]
    assert STATE_COLORS["in_queue"] == STAGE_COLORS["queue"]


# The stages wear the first five ci-observe palette colors in the order a
# pull request goes through them, in the weekly chart and the tiles above.
STAGE_COLORS = {
    "authoring": "#3987e5",
    "review": "#d95926",
    "waiting": "#199e70",
    "queue": "#c98500",
    "merged": "#d55181",
}


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


def test_the_page_opens_on_every_pull_request_of_the_window() -> None:
    """Above the one-PR detail: the window's numbers, the stage medians per
    week, the across-PR views and every pull request, all from the index
    document of the selected repository, author and working groups."""
    overview, _ = sections()
    assert overview[0]["type"] == "row"
    # A row title shows a variable's text ("30 days"), not its value.
    assert overview[0]["title"] == "All pull requests in ${pr_repo}, last ${days:text}"
    tiles = [candidate for candidate in overview if candidate["type"] == "stat"]
    assert len(tiles) == 24
    for tile in tiles:
        target = tile["targets"][0]
        assert target["url"] == INDEX_URL, tile["title"]
        assert target["root_selector"] == "kpis", tile["title"]
        assert "Population:" in tile["description"] and "Unit:" in tile["description"], tile["title"]
    for candidate in overview:
        for target in candidate.get("targets", []):
            assert target["url"] == INDEX_URL, candidate["title"]
    fields = {column["selector"] for tile in tiles for column in tile["targets"][0]["columns"]}
    assert {"review_wait_median_s", "review_median_s", "waiting_median_s", "queue_median_s", "rebased_share",
            "force_pushes_mean", "e2e_retests_median", "ejected_share", "review_rounds_median",
            "reapproved_share", "rebase_rereview_share", "rereview_median_s", "last_approval_wait_median_s",
            "files_median", "multi_wg_share", "lifecycle_prs"} <= fields
    assert panel("First approval to queue")["targets"][0]["columns"][0]["selector"] == "waiting_median_s"
    assert panel("Last approval to queue")["targets"][0]["columns"][0]["selector"] == "last_approval_wait_median_s"
    weekly = panel("Stage medians per week")
    assert weekly["targets"][0]["root_selector"] == "weekly"
    assert weekly["targets"][0]["format"] == "timeseries"
    table = panel("Pull requests")
    assert table["targets"][0]["url"] == INDEX_URL
    assert table["targets"][0]["root_selector"] == "prs"
    assert override(table, "number")["links"][0]["url"] == "${__data.fields.detail_url}"
    assert override(table, "title")["links"][0]["url"] == "${__data.fields.url}"
    assert {"force_push_count", "e2e_runs", "e2e_failures", "files_changed", "wgs_required", "teams_display",
            "review_rounds", "reapprovals"} <= set(selectors(table))


def test_every_merged_pull_request_is_a_dot_colored_by_working_groups() -> None:
    chart = panel("Every merged pull request: time to merge")
    assert chart["type"] == "xychart"
    roots = [target["root_selector"] for target in chart["targets"]]
    assert roots == ["scatter_wg0", "scatter_wg1", "scatter_wg2", "scatter_wg3plus", "scatter_unknown"]
    assert [target["refId"] for target in chart["targets"]] == ["A", "B", "C", "D", "E"]
    series = chart["options"]["series"]
    assert chart["options"]["mapping"] == "manual"
    assert [item["frame"]["matcher"] for item in series] == [{"id": "byIndex", "options": index} for index in range(5)]
    assert [item["name"]["fixed"] for item in series] == [
        "No code owner", "1 WG", "2 WGs", "3 or more WGs", "Not measured yet",
    ]
    assert {item["x"]["matcher"]["options"] for item in series} == {"merged_at"}
    assert {target["columns"][0]["type"] for target in chart["targets"]} == {"timestamp"}, "a time axis"
    assert {item["y"]["matcher"]["options"] for item in series} == {"hours"}
    colors = [
        next(prop["value"]["fixedColor"] for prop in entry["properties"] if prop["id"] == "color")
        for entry in chart["fieldConfig"]["overrides"] if entry["matcher"]["id"] == "byFrameRefID"
    ]
    assert len(set(colors)) == 5
    assert override(chart, "hours")["custom.scaleDistribution"] == {"type": "log", "log": 10}
    assert override(chart, "hours")["unit"] == "suffix:h"
    assert chart["fieldConfig"]["defaults"]["links"][0]["url"] == "${__data.fields.detail_url}"


def test_time_to_merge_per_week_is_a_heatmap_of_bands() -> None:
    chart = panel("Time to merge, week by week")
    assert chart["type"] == "heatmap"
    assert chart["options"]["calculate"] is False
    target = chart["targets"][0]
    assert target["root_selector"] == "merge_time_heatmap"
    assert target["columns"][0] == {"selector": "week", "text": "week", "type": "timestamp"}
    assert [column["selector"] for column in target["columns"][1:]] == [
        "under_1h", "h1_4", "h4_24", "d1_3", "d3_7", "d7_plus",
    ]
    # Time panels follow the Window, not the hidden time picker.
    for title in ("Time to merge, week by week", "Stage medians per week"):
        assert (panel(title)["timeFrom"], panel(title)["hideTimeOverride"]) == ("${days}d", True), title


def test_the_across_pr_bars_read_their_lists() -> None:
    for title, root, x_field, stacking in (
        ("Where the time goes, by working groups to approve", "time_by_wgs", "group", "normal"),
        ("Where the time goes, by files changed", "time_by_files", "group", "normal"),
        ("Review rounds per pull request", "rounds_histogram", "bucket", "none"),
        ("Times in the merge queue", "queue_histogram", "bucket", "none"),
        ("Working groups that had to approve", "wgs_histogram", "bucket", "none"),
        ("The slowest pull requests, stretch by stretch", "lifecycles", "pull_request", "normal"),
    ):
        chart = panel(title)
        assert chart["type"] == "barchart", title
        assert chart["targets"][0]["root_selector"] == root, title
        assert chart["options"]["xField"] == x_field, title
        assert chart["options"]["stacking"] == stacking, title
        assert "Population:" in chart["description"] and "$days" in chart["description"], title


def test_the_slowest_pull_requests_stack_their_stretches_in_order(prs) -> None:
    chart = panel("The slowest pull requests, stretch by stretch")
    columns = chart["targets"][0]["columns"]
    assert [column["selector"] for column in columns[:2]] == ["pull_request", "detail_url"]
    labels = [label for _, label in prs.LIFECYCLE_STATES]
    expected = [
        label if cycle == 1 else f"{label} ({cycle})"
        for cycle in range(1, prs.LIFECYCLE_CYCLES + 1) for label in labels
    ]
    assert [column["text"] for column in columns[2:]] == expected
    assert [column["selector"] for column in columns[2:]] == prs._cycle_fields()
    hidden = [entry for entry in chart["fieldConfig"]["overrides"]
              if any(prop["id"] == "custom.hideFrom" for prop in entry["properties"])]
    assert hidden and hidden[0]["matcher"] == {"id": "byRegexp", "options": r"/ \(\d+\)$/"}, (
        "the legend names each state once"
    )
    assert chart["fieldConfig"]["defaults"]["links"][0]["url"] == "${__data.fields.detail_url}"


def test_one_pull_request_lists_what_happened_and_its_numbers() -> None:
    events = panel("What happened")
    assert events["targets"][0]["url"] == PR_URL
    assert events["targets"][0]["root_selector"] == "events"
    assert_selectors_resolve(events, PR_DOCUMENT)
    assert display_names(events) == {
        "when": "When",
        "since_open_s": "After opening",
        "what": "What happened",
        "who": "Who",
        "detail": "Detail",
    }
    assert override(events, "since_open_s")["unit"] == "dtdhms"
    assert override(events, "what")["links"][0]["url"] == "${__data.fields.url}"
    assert override(events, "url")["custom.hidden"] is True
    facts = panel("This pull request in numbers")
    assert facts["targets"][0]["root_selector"] == "facts"
    assert_selectors_resolve(facts, PR_DOCUMENT)
    assert facts["options"]["showHeader"] is False
    lifecycle_y = panel("Lifecycle")["gridPos"]["y"]
    assert events["gridPos"]["y"] == facts["gridPos"]["y"] > lifecycle_y


def test_every_link_opens_a_dashboard_that_exists() -> None:
    uids = {json.loads(path.read_text())["uid"] for path in DASHBOARDS_DIR.glob("ci-observe-*.json")}
    for link in DASHBOARD["links"]:
        uid = link["url"].split("/d/")[1].split("/")[0]
        assert uid in uids, link["url"]


def test_no_template_token_is_left_unresolved() -> None:
    known = set(variables()) | {"__data", "__value"}
    for text in re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", json.dumps(DASHBOARD)):
        assert text in known or text.startswith("__"), text
