"""Structural tests for the ci-observe Trends page.

`ci-observe-trends.json` answers one question about one test: is it failing
more often or getting slower? It has a heading, a Find a test box and two
charts.

Every selector is resolved against a fixture document with the same keys as
`/api/ci-observe/trends`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

DASHBOARDS_DIR = Path(__file__).resolve().parents[1] / "config" / "grafana" / "dashboards"
DASHBOARD = json.loads((DASHBOARDS_DIR / "ci-observe-trends.json").read_text())
TRENDS_URL = "http://127.0.0.1:9103/api/ci-observe/trends"

ROWS_DOCUMENT = {
    "rows": [
        {
            "day": "2026-11-01",
            "test": "tests/e2e/vmaas/test_cluster_create.py::test_create",
            "passes": 3,
            "failures": 1,
            "p50_duration_s": 12.0,
            "p95_duration_s": 30.0,
        }
    ]
}


def walk(panels: list[dict]):
    for panel in panels:
        yield panel
        yield from walk(panel.get("panels", []))


def panel(titled: str) -> dict:
    return next(candidate for candidate in walk(DASHBOARD["panels"]) if candidate.get("title") == titled)


def variables() -> dict[str, dict]:
    return {variable["name"]: variable for variable in DASHBOARD["templating"]["list"]}


def selectors(chart: dict) -> list[str]:
    return [column["selector"] for column in chart["targets"][0]["columns"]]


def test_identity_and_folder() -> None:
    assert DASHBOARD["uid"] == "ci-observe-trends"
    assert DASHBOARD["title"] == "CI Observe — Trends"
    assert "ci-observe" in DASHBOARD["tags"]


def test_the_page_is_a_heading_and_two_charts() -> None:
    row = next(candidate for candidate in DASHBOARD["panels"] if candidate.get("type") == "row")
    assert row["title"] == "Test history"
    assert row["collapsed"] is False, "the charts are the reason to open the page"
    assert [child["title"] for child in row["panels"]] == ["Passes and failures", "Test time"]
    assert all(child["type"] == "timeseries" for child in row["panels"])


def test_the_page_keeps_days_and_test() -> None:
    assert set(variables()) == {"days", "test"}
    assert variables()["days"]["current"]["value"] == "30"


def test_both_charts_read_the_trends_endpoint() -> None:
    expected = f"{TRENDS_URL}?days=${{days}}&test=${{test:percentencode}}"
    for title in ("Passes and failures", "Test time"):
        target = panel(title)["targets"][0]
        assert target["url"] == expected
        assert target["root_selector"] == "rows"
        assert target["format"] == "timeseries"


def test_every_selector_resolves_against_the_endpoint_contract() -> None:
    row = ROWS_DOCUMENT["rows"][0]
    for title in ("Passes and failures", "Test time"):
        for column in selectors(panel(title)):
            assert column in row, f"{title}: {column}"


def test_the_charts_use_plain_column_labels() -> None:
    def display_names(chart: dict) -> dict[str, str]:
        names = {}
        for entry in chart["fieldConfig"]["overrides"]:
            field = entry["matcher"].get("options")
            for prop in entry["properties"]:
                if prop["id"] == "displayName":
                    names[field] = prop["value"]
        return names

    assert display_names(panel("Passes and failures")) == {"passes": "Passes", "failures": "Failures"}
    assert display_names(panel("Test time")) == {
        "p50_duration_s": "Typical time",
        "p95_duration_s": "95% finish by",
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
