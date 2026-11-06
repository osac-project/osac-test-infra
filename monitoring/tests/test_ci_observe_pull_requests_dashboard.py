"""Structural tests for the ci-observe Pull requests page.

`ci-observe-pull-requests.json` starts from one pull request: its lifecycle,
its E2E runs, and its merge-queue attempts. A second section lists recent
pull requests and the stage funnel for browsing.

Every selector is resolved against a fixture document with the same keys as
`/api/ci-observe/prs`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

DASHBOARDS_DIR = Path(__file__).resolve().parents[1] / "config" / "grafana" / "dashboards"
DASHBOARD = json.loads((DASHBOARDS_DIR / "ci-observe-pull-requests.json").read_text())
PRS_URL = "http://127.0.0.1:9103/api/ci-observe/prs"
PR_URL = f"{PRS_URL}?number=${{pr_number}}&repo=${{pr_repo}}"

INDEX_DOCUMENT = {
    "prs": [
        {
            "repo": "osac",
            "number": 101,
            "title": "Add VM restart coverage",
            "final_stage": "merged",
            "age_display": "1d 1h",
            "queue_attempt_count": 2,
            "ejected_attempt_count": 1,
            "option_label": "#101 · merged · Add VM restart coverage",
            "url": "https://github.com/osac-project/osac/pull/101",
        }
    ],
    "funnel": [
        {"label": "Review", "samples": 2, "median_seconds": 1500, "p95_seconds": 1770},
    ],
}

PR_DOCUMENT = {
    "pr": {
        "number": 101,
        "title": "Add VM restart coverage",
        "author": "author1",
        "repo": "osac",
        "state": "merged",
        "final_stage": "merged",
        "opened_at": "2026-10-01T10:00:00Z",
        "first_review_at": "2026-10-01T10:20:00Z",
        "first_approval_at": "2026-10-01T10:50:00Z",
        "queued_at": "2026-10-01T11:00:00Z",
        "last_attempt_at": "2026-10-01T11:05:00Z",
        "merged_at": "2026-10-01T11:06:00Z",
        "closed_at": None,
        "total_display": "1h 6m",
        "url": "https://github.com/osac-project/osac/pull/101",
    },
    "stages": [
        {
            "label": "Review",
            "start_at": "2026-10-01T10:20:00Z",
            "end_at": "2026-10-01T10:50:00Z",
            "seconds": 1800,
            "time_display": "30m",
            "state": "measured",
            "missing_reason": "",
        }
    ],
    "runs": [
        {
            "version": 1,
            "head": "1234567890ab",
            "commit_url": "https://github.com/osac-project/osac/commit/1234567890ab",
            "suite": "VMaaS",
            "trigger": "PR #101",
            "conclusion": "success",
            "recorded": "yes",
            "created_at": "2026-10-01T10:30:00Z",
            "duration_display": "12m",
            "run_url": "/d/ci-observe-runs/ci-observe-e28094-e2e-run?var-capture_id=x",
            "url": "https://github.com/osac-project/osac/actions/runs/1",
        }
    ],
    "attempts": [
        {
            "started_at": "2026-10-01T11:00:00Z",
            "completed_at": "2026-10-01T11:05:00Z",
            "conclusion": "failure",
            "run_count": 2,
            "ejected": True,
            "queue_outcome": "ejected",
            "ejection_kind": "check_failed",
            "ejection_reason": "merge group failed",
            "duration_display": "5m",
            "run_url": "https://github.com/osac-project/osac/actions/runs/2",
            "comment_url": "https://github.com/osac-project/osac/pull/101#issuecomment-1",
        }
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
    row = INDEX_DOCUMENT["prs"][0]
    for column in variables()["pr_number"]["query"]["infinityQuery"]["columns"]:
        assert column["selector"] in row, column["selector"]


def test_the_page_asks_for_a_pull_request_until_one_is_chosen() -> None:
    assert panel("Details")["fieldConfig"]["defaults"]["noValue"] == "Choose a pull request above."
    heading = next(candidate for candidate in DASHBOARD["panels"] if candidate["type"] == "text")
    assert "Choose one above" in heading["options"]["content"]


def test_the_selected_pull_request_comes_first_and_is_open() -> None:
    row = next(candidate for candidate in DASHBOARD["panels"] if candidate.get("title") == "Selected pull request")
    assert row["type"] == "row"
    assert row["collapsed"] is False
    assert DASHBOARD["panels"].index(row) < next(
        index
        for index, candidate in enumerate(DASHBOARD["panels"])
        if candidate.get("title") == "All pull requests"
    )


def test_the_details_table_reads_one_pr() -> None:
    table = panel("Details")
    target = table["targets"][0]
    assert target["url"] == PR_URL
    assert target["root_selector"] == "$.pr"
    assert_selectors_resolve(table, PR_DOCUMENT)
    assert display_names(table) == {
        "number": "PR",
        "title": "Title",
        "author": "Author",
        "repo": "Repository",
        "state": "State",
        "opened_at": "Opened",
        "first_review_at": "First review",
        "first_approval_at": "Approved",
        "queued_at": "Entered queue",
        "last_attempt_at": "Last queue attempt",
        "merged_at": "Merged",
        "closed_at": "Closed",
        "total_display": "Total time",
        "url": "GitHub",
    }


def test_the_stage_table_reads_the_selected_pr_stages() -> None:
    table = panel("Time in each stage")
    target = table["targets"][0]
    assert target["url"] == PR_URL
    assert target["root_selector"] == "$.stages"
    assert_selectors_resolve(table, PR_DOCUMENT)
    assert display_names(table) == {
        "label": "Stage",
        "start_at": "From",
        "end_at": "To",
        "time_display": "Time",
        "state": "State",
        "missing_reason": "Why not measured",
    }


def test_the_e2e_runs_table_reads_the_runs() -> None:
    table = panel("E2E runs")
    target = table["targets"][0]
    assert target["url"] == PR_URL
    assert target["root_selector"] == "$.runs"
    assert_selectors_resolve(table, PR_DOCUMENT)
    assert display_names(table) == {
        "version": "Code version",
        "head": "Commit",
        "suite": "Suite",
        "trigger": "Why it ran",
        "conclusion": "Result",
        "recorded": "Recorded",
        "created_at": "Started",
        "duration_display": "Duration",
        "run_url": "Open run",
        "url": "GitHub",
    }


def test_the_queue_attempts_table_reads_the_attempts() -> None:
    table = panel("Queue attempts")
    target = table["targets"][0]
    assert target["url"] == PR_URL
    assert target["root_selector"] == "$.attempts"
    assert_selectors_resolve(table, PR_DOCUMENT)
    assert display_names(table) == {
        "started_at": "Entered queue",
        "completed_at": "Left queue",
        "conclusion": "Result",
        "run_count": "Runs",
        "queue_outcome": "Queue",
        "ejection_kind": "Why it left",
        "ejection_reason": "Reason",
        "duration_display": "Duration",
        "run_url": "GitHub run",
        "comment_url": "Comment",
    }


def test_the_browse_section_keeps_the_index_and_funnel() -> None:
    recent = panel("Recent pull requests, last $days days")
    assert recent["targets"][0]["url"] == f"{PRS_URL}?days=${{days}}&repo=${{pr_repo}}"
    assert recent["targets"][0]["root_selector"] == "$.prs"
    assert_selectors_resolve(recent, INDEX_DOCUMENT)
    assert display_names(recent) == {
        "number": "PR",
        "repo": "Repository",
        "title": "Title",
        "final_stage": "Last stage",
        "age_display": "Age",
        "queue_attempt_count": "Queue runs",
        "ejected_attempt_count": "Left queue",
        "url": "GitHub",
    }

    stages = panel("Stage times")
    assert stages["targets"][0]["url"] == f"{PRS_URL}?days=${{days}}&repo=${{pr_repo}}"
    assert stages["targets"][0]["root_selector"] == "$.funnel"
    assert_selectors_resolve(stages, INDEX_DOCUMENT)
    assert display_names(stages) == {
        "label": "Stage",
        "samples": "PRs",
        "median_seconds": "Typical",
        "p95_seconds": "Slow end",
    }


def test_the_tables_can_be_filtered() -> None:
    for title in ("E2E runs", "Queue attempts", "Recent pull requests, last $days days"):
        assert panel(title)["fieldConfig"]["defaults"]["custom"]["filterable"] is True, title


def test_every_link_opens_a_dashboard_that_exists() -> None:
    uids = {json.loads(path.read_text())["uid"] for path in DASHBOARDS_DIR.glob("ci-observe-*.json")}
    for link in DASHBOARD["links"]:
        uid = link["url"].split("/d/")[1].split("/")[0]
        assert uid in uids, link["url"]


def test_no_template_token_is_left_unresolved() -> None:
    known = set(variables()) | {"__data", "__value"}
    for text in re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", json.dumps(DASHBOARD)):
        assert text in known or text.startswith("__"), text
