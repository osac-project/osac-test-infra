"""Structural tests for the ci-observe Pull requests page.

`ci-observe-pull-requests.json` answers two questions: how long does review
take, and how long does the merge queue take? It has a recent pull request
table, a stage time table and one collapsed detail row for a chosen pull
request.

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
        "final_stage": "merged",
        "opened_at": "2026-10-01T10:00:00Z",
        "total_seconds": 5700,
    }
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
    assert variables()["pr_repo"]["hide"] == 2, "the repository defaults to osac"


def test_the_recent_table_reads_the_prs_endpoint() -> None:
    table = panel("Recent pull requests, last $days days")
    target = table["targets"][0]
    assert target["url"] == f"{PRS_URL}?days=${{days}}"
    assert target["root_selector"] == "$.prs"
    assert_selectors_resolve(table, INDEX_DOCUMENT)


def test_the_recent_table_uses_plain_column_labels() -> None:
    table = panel("Recent pull requests, last $days days")
    assert display_names(table) == {
        "number": "PR",
        "repo": "Repository",
        "title": "Title",
        "final_stage": "Last stage",
        "age_display": "Age",
        "queue_attempt_count": "Queue runs",
        "ejected_attempt_count": "Left queue",
        "url": "GitHub",
    }


def test_the_stage_table_reads_the_funnel() -> None:
    table = panel("Stage times")
    target = table["targets"][0]
    assert target["url"] == f"{PRS_URL}?days=${{days}}"
    assert target["root_selector"] == "$.funnel"
    assert_selectors_resolve(table, INDEX_DOCUMENT)
    assert display_names(table) == {
        "label": "Stage",
        "samples": "PRs",
        "median_seconds": "Typical",
        "p95_seconds": "Slow end",
    }


def test_the_selected_pull_request_row_is_collapsed() -> None:
    row = next(candidate for candidate in DASHBOARD["panels"] if candidate.get("title") == "Selected pull request")
    assert row["type"] == "row"
    assert row["collapsed"] is True
    assert [child.get("title") for child in row["panels"]] == ["Details"]


def test_the_selected_pull_request_reads_one_pr() -> None:
    table = panel("Details")
    target = table["targets"][0]
    assert target["url"] == f"{PRS_URL}?number=${{pr_number}}&repo=${{pr_repo}}"
    assert target["root_selector"] == "$.pr"
    assert_selectors_resolve(table, PR_DOCUMENT)
    assert display_names(table) == {
        "number": "PR",
        "title": "Title",
        "final_stage": "Last stage",
        "opened_at": "Opened",
        "total_seconds": "Total time",
    }


def test_every_link_opens_a_dashboard_that_exists() -> None:
    uids = {json.loads(path.read_text())["uid"] for path in DASHBOARDS_DIR.glob("ci-observe-*.json")}
    for link in DASHBOARD["links"]:
        uid = link["url"].split("/d/")[1].split("/")[0]
        assert uid in uids, link["url"]


def test_no_template_token_is_left_unresolved() -> None:
    known = set(variables()) | {"__data", "__value"}
    for text in re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", json.dumps(DASHBOARD)):
        assert text in known or text.startswith("__"), text
