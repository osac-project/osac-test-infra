"""Structural tests for the CI Observe pull-request dashboard.

monitoring/config/grafana/dashboards/ci-observe-pull-requests.json queries
the workflow exporter's /api/ci-observe/prs endpoint through the Infinity
datasource (uid workflow-api). These tests pin the sections, the variables,
the derived-ejection wording on every panel, the GitHub link, the stage
funnel columns, and that every selector resolves against the endpoint
contract pinned at the bottom of this file. The backend half of the same
contract is in test_ci_observe_prs.py.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

DASHBOARD_FILE = (
    Path(__file__).resolve().parents[1] / "config" / "grafana" / "dashboards" / "ci-observe-pull-requests.json"
)
DATASOURCE = {"type": "yesoreyeram-infinity-datasource", "uid": "workflow-api"}
ENDPOINT = "http://127.0.0.1:9103/api/ci-observe/prs"
DERIVED = "derived, not recorded by GitHub"
ENQUEUED = "Enqueued is the start of the pull request's first merge_group run"

# The response contract, pinned here so this file passes in a tree without
# the backend. Every field is one the dashboard's tables read.
INDEX_DOCUMENT = {
    "mode": "index",
    "generated_at": "2026-11-01T00:00:00Z",
    "days": 30,
    "prs": [
        {
            "repo": "osac",
            "number": 101,
            "title": "Add VM restart coverage",
            "author": "author1",
            "state": "merged",
            "final_stage": "merged",
            "age_display": "1d 1h",
            "age_seconds": 90000,
            "opened_at": "2026-10-01T10:00:00Z",
            "merged_at": "2026-10-01T11:35:00Z",
            "closed_at": "2026-10-01T11:35:00Z",
            "queue_attempt_count": 2,
            "ejected_attempt_count": 1,
            "url": "https://github.com/osac-project/osac/pull/101",
        }
    ],
    "funnel": [
        {"stage": "authoring", "label": "Authoring", "samples": 3, "median_seconds": 1200, "p95_seconds": 1740},
        {"stage": "review", "label": "Review", "samples": 2, "median_seconds": 1500, "p95_seconds": 1770},
        {"stage": "waiting", "label": "Waiting", "samples": 2, "median_seconds": 1200, "p95_seconds": 1740},
        {"stage": "queue", "label": "Queue", "samples": 2, "median_seconds": 2850, "p95_seconds": 3525},
        {"stage": "merged", "label": "Merged", "samples": 2, "median_seconds": 330, "p95_seconds": 573},
    ],
    "notes": [],
    "derivation": {"enqueued": ENQUEUED, "ejections": "Queue ejections are derived, not recorded by GitHub."},
}

PR_DOCUMENT = {
    "mode": "pr",
    "generated_at": "2026-11-01T00:00:00Z",
    "days": 30,
    "pr": {
        "repo": "osac",
        "number": 101,
        "title": "Add VM restart coverage",
        "author": "author1",
        "state": "merged",
        "labels": "ok-to-test",
        "final_stage": "merged",
        "opened_at": "2026-10-01T10:00:00Z",
        "first_review_at": "2026-10-01T10:20:00Z",
        "first_approval_at": "2026-10-01T10:50:00Z",
        "queued_at": "2026-10-01T11:00:00Z",
        "merged_at": "2026-10-01T11:35:00Z",
        "closed_at": "2026-10-01T11:35:00Z",
        "total_seconds": 5700,
        "retest_count": 2,
        "queue_attempt_count": 2,
        "queue_history": "present",
        "url": "https://github.com/osac-project/osac/pull/101",
    },
    "stages": [
        {
            "stage": "authoring",
            "label": "Authoring",
            "start_at": "2026-10-01T10:00:00Z",
            "end_at": "2026-10-01T10:20:00Z",
            "seconds": 1200,
            "state": "measured",
            "missing_reason": "",
        }
    ],
    "attempts": [
        {
            "repo": "osac",
            "number": 101,
            "run_id": 5001,
            "run_attempt": 1,
            "run_ids": [5001, 5003],
            "run_count": 2,
            "run_urls": [
                "https://github.com/osac-project/osac/actions/runs/5001",
                "https://github.com/osac-project/osac/actions/runs/5003",
            ],
            "conclusion": "failure",
            "started_at": "2026-10-01T11:00:00Z",
            "completed_at": "2026-10-01T11:05:00Z",
            "duration_seconds": 300,
            "ejected": True,
            "ejection": "ejected",
            "ejection_kind": "check_failed",
            "ejection_reason": "The merge queue failed for this pull request.",
            "reason_source": "comment",
            "comment_url": "https://github.com/osac-project/osac/pull/101#issuecomment-2",
            "run_url": "https://github.com/osac-project/osac/actions/runs/5001",
            "capture_url": "",
        }
    ],
    "notes": ["Queue ejections are derived, not recorded by GitHub."],
    "derivation": {"enqueued": ENQUEUED},
}

GRAFANA_BUILTIN_VARIABLE = re.compile(r"^__")


@pytest.fixture(scope="module")
def dashboard() -> dict:
    return json.loads(DASHBOARD_FILE.read_text())


def _iter_panels(panels: list[dict]) -> Iterator[dict]:
    for panel in panels:
        yield panel
        yield from _iter_panels(panel.get("panels", []))


def _content_panels(dashboard: dict) -> list[dict]:
    return [p for p in _iter_panels(dashboard["panels"]) if p["type"] not in ("row", "text")]


def _panels_by_title(dashboard: dict) -> dict[str, dict]:
    return {p["title"]: p for p in _iter_panels(dashboard["panels"])}


def _override(panel: dict, field: str) -> dict | None:
    for entry in panel["fieldConfig"]["overrides"]:
        if entry["matcher"]["options"] == field:
            return entry
    return None


def test_uid_and_title(dashboard: dict) -> None:
    assert dashboard["uid"] == "ci-observe-pull-requests"
    assert dashboard["title"] == "CI Observe — Pull requests"
    assert dashboard["schemaVersion"] == 39
    assert dashboard["version"] == 1


def test_variables(dashboard: dict) -> None:
    variables = {v["name"]: v for v in dashboard["templating"]["list"]}
    assert set(variables) == {"days", "pr_number", "pr_repo"}
    assert variables["days"]["type"] == "custom"
    assert variables["days"]["current"]["value"] == "30"
    assert variables["pr_number"]["type"] == "textbox"
    assert variables["pr_number"]["current"]["value"] == ""
    assert variables["pr_repo"]["current"]["value"] == "osac"


def test_sections_in_reading_order(dashboard: dict) -> None:
    rows = [p["title"] for p in dashboard["panels"] if p["type"] == "row"]
    assert rows == ["Recent pull requests", "Stage funnel", "Pull request detail", "Queue attempts"]
    positions = [(p["gridPos"]["y"], p["gridPos"]["x"]) for p in dashboard["panels"]]
    assert positions == sorted(positions)


def test_reading_guide_comes_first(dashboard: dict) -> None:
    first = dashboard["panels"][0]
    assert first["type"] == "text"
    assert first["gridPos"]["y"] == 0
    assert first["gridPos"]["w"] == 24
    content = first["options"]["content"]
    assert ENQUEUED in content
    assert DERIVED in content
    assert "no queue history" in content


def test_every_panel_states_ejections_are_derived(dashboard: dict) -> None:
    """A reader must never take an ejection for a recorded GitHub event."""
    for panel in _iter_panels(dashboard["panels"]):
        assert panel.get("title"), f"panel without a title: {panel.get('id')}"
        description = panel.get("description") or ""
        assert DERIVED.lower() in description.lower(), f"{panel['title']} does not say ejections are derived"


def test_queue_section_defines_enqueued(dashboard: dict) -> None:
    row = _panels_by_title(dashboard)["Queue attempts"]
    assert ENQUEUED in row["description"]
    assert "capture" in row["description"].lower()


def test_index_links_to_github_and_to_the_detail(dashboard: dict) -> None:
    panel = _panels_by_title(dashboard)["Recent pull requests"]
    url_override = _override(panel, "url")
    links = next(p["value"] for p in url_override["properties"] if p["id"] == "links")
    assert any(link["url"] == "${__value.raw}" for link in links), "GitHub link missing"
    number_override = _override(panel, "number")
    number_links = next(p["value"] for p in number_override["properties"] if p["id"] == "links")
    assert any("var-pr_number=${__value.raw}" in link["url"] for link in number_links)


def test_funnel_shows_median_and_p95_seconds(dashboard: dict) -> None:
    panel = _panels_by_title(dashboard)["Stage funnel, median and p95"]
    selectors = [column["selector"] for column in panel["targets"][0]["columns"]]
    assert selectors == ["label", "samples", "median_seconds", "p95_seconds"]
    for field in ("median_seconds", "p95_seconds"):
        override = _override(panel, field)
        properties = {p["id"]: p["value"] for p in override["properties"]}
        assert properties["unit"] == "s"


def test_queue_attempts_shows_run_conclusion_and_derived_reason(dashboard: dict) -> None:
    panel = _panels_by_title(dashboard)["Queue attempts"]
    selectors = [column["selector"] for column in panel["targets"][0]["columns"]]
    for required in ("run_id", "conclusion", "ejected", "ejection_reason", "reason_source", "run_url"):
        assert required in selectors
    ejected = _override(panel, "ejected")
    mapping = next(p["value"] for p in ejected["properties"] if p["id"] == "mappings")[0]["options"]
    assert mapping["true"]["text"] == "ejected (derived)"
    assert mapping["false"]["text"] == "not ejected"


def test_content_panels_use_the_workflow_api_datasource(dashboard: dict) -> None:
    for panel in _content_panels(dashboard):
        assert panel["datasource"] == DATASOURCE, panel["title"]


def test_every_target_queries_the_prs_endpoint(dashboard: dict) -> None:
    for panel in _content_panels(dashboard):
        for target in panel["targets"]:
            assert target["url"].startswith(ENDPOINT), panel["title"]
            assert target["url_options"] == {"method": "GET", "data": ""}
            assert (target["type"], target["source"], target["format"]) == ("json", "url", "table")
            assert target["root_selector"], "a table target needs a root_selector"
            assert target["columns"], "a table target needs columns"


def test_no_leftover_template_tokens(dashboard: dict) -> None:
    variables = {v["name"] for v in dashboard["templating"]["list"]}
    token = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
    for panel in _iter_panels(dashboard["panels"]):
        for target in panel.get("targets", []):
            for match in token.findall(target["url"]):
                assert match in variables or GRAFANA_BUILTIN_VARIABLE.match(match), (
                    f"unresolved template token {match!r} in {panel['title']}"
                )


def test_no_raw_secrets(dashboard: dict) -> None:
    raw = DASHBOARD_FILE.read_text()
    for banned in ("PRIVATE_GITHUB_TOKEN", "Authorization", "ghp_", "Bearer "):
        assert banned not in raw


def test_every_selector_resolves_against_the_endpoint_contract(dashboard: dict) -> None:
    """Every root_selector and column must exist in the response contract,
    so the dashboard cannot drift from the endpoint."""
    checked = 0
    for panel in _content_panels(dashboard):
        for target in panel["targets"]:
            document = INDEX_DOCUMENT if "days=" in target["url"] else PR_DOCUMENT
            selector = target["root_selector"]
            if selector.startswith("$."):
                selector = selector[2:]
            node = document
            for part in selector.split("."):
                node = node[part]
            rows = node if isinstance(node, list) else [node]
            assert rows, f"{panel['title']}: {target['root_selector']} is empty"
            for column in target["columns"]:
                assert column["selector"] in rows[0], (
                    f"{panel['title']}: column {column['selector']} is not in the contract"
                )
            checked += 1
    assert checked >= 5
