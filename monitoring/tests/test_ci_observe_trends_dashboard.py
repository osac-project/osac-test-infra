"""Structural tests for the ci-observe Trends page.

`ci-observe-trends.json` answers one question about one unit (a test or a
job): is it failing more often or getting slower? The unit is chosen with
the Days, Unit and Name controls, and the page has three rows: pick a unit,
the history of the selected unit, and the older broad test search.

Every selector is resolved against fixture documents with the same keys as
`/api/ci-observe/history`, `/api/ci-observe/jobs` and
`/api/ci-observe/trends`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

DASHBOARDS_DIR = Path(__file__).resolve().parents[1] / "config" / "grafana" / "dashboards"
DASHBOARD = json.loads((DASHBOARDS_DIR / "ci-observe-trends.json").read_text())
API = "http://127.0.0.1:9103"
HISTORY_URL = (
    f"{API}/api/ci-observe/history?kind=${{kind}}"
    "&name=${name:percentencode}&days=${days}&repo=${repo:percentencode}"
)
TRENDS_URL = f"{API}/api/ci-observe/trends?days=${{days}}&test=${{name:percentencode}}"
MATCH_TESTS_URL = (
    f"{API}/api/ci-observe/trends?days=${{days}}&summary=test&test=${{name:percentencode}}"
)
JOBS_URL = (
    f"{API}/api/ci-observe/jobs?days=${{days}}&q=${{name:percentencode}}"
    "&limit=20&repo=${repo:percentencode}"
)

SUMMARY_DOCUMENT = {
    "summary": [
        {
            "runs": 4,
            "passes": 3,
            "failures": 1,
            "pass_rate": 0.75,
            "p50_duration_s": 12.0,
            "p95_duration_s": 30.0,
            "p99_duration_s": 31.0,
            "mean_duration_s": 14.0,
            "measured_runs": 4,
            "duration_basis": "true percentiles over 4 of 4 run(s) with a raw duration",
        }
    ]
}

DAILY_DOCUMENT = {
    "daily": [
        {
            "day": "2026-11-01",
            "runs": 4,
            "passes": 3,
            "failures": 1,
            "pass_rate": 0.75,
            "p50_duration_s": 12.0,
            "p95_duration_s": 30.0,
            "p99_duration_s": 31.0,
            "mean_duration_s": 14.0,
        }
    ]
}

RECENT_DOCUMENT = {
    "recent": [
        {
            "created_at": "2026-11-01T09:00:00Z",
            "conclusion": "success",
            "duration_s": 12.0,
            "commit": "a" * 40,
            "run_url": "https://github.com/osac-project/osac/actions/runs/1",
        }
    ]
}

MATCH_TESTS_DOCUMENT = {
    "tests": [
        {
            "test_id": "tests/e2e/vmaas/test_vm.py::test_create",
            "suite": "tests/e2e/vmaas",
            "runs": 4,
            "failures": 1,
            "history_url": "/d/ci-observe-trends/ci-observe-e28094-trends?var-kind=test",
        }
    ]
}

MATCH_JOBS_DOCUMENT = {
    "jobs": [
        {
            "repo": "osac",
            "display_name": "osac / E2E VMaaS Full Install",
            "runs": 4,
            "last_seen": "2026-11-01T09:00:00Z",
            "history_url": "/d/ci-observe-trends/ci-observe-e28094-trends?var-kind=job",
        }
    ]
}

ROWS_DOCUMENT = {
    "rows": [
        {
            "day": "2026-11-01",
            "passes": 3,
            "failures": 1,
            "p50_duration_s": 12.0,
            "p95_duration_s": 30.0,
            "p99_duration_s": 31.0,
            "mean_duration_s": 14.0,
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


def display_names(chart: dict) -> dict[str, str]:
    names = {}
    for entry in chart["fieldConfig"]["overrides"]:
        field = entry["matcher"].get("options")
        for prop in entry["properties"]:
            if prop["id"] == "displayName":
                names[field] = prop["value"]
    return names


def no_values(chart: dict) -> dict[str, str]:
    values = {}
    for entry in chart["fieldConfig"]["overrides"]:
        field = entry["matcher"].get("options")
        for prop in entry["properties"]:
            if prop["id"] == "noValue":
                values[field] = prop["value"]
    return values


def links(chart: dict) -> dict[str, str]:
    found = {}
    for entry in chart["fieldConfig"]["overrides"]:
        field = entry["matcher"].get("options")
        for prop in entry["properties"]:
            if prop["id"] == "links":
                found[field] = prop["value"][0]["url"]
    return found


def test_identity_and_folder() -> None:
    assert DASHBOARD["uid"] == "ci-observe-trends"
    assert DASHBOARD["title"] == "CI Observe — Trends"
    assert "ci-observe" in DASHBOARD["tags"]


def test_the_page_has_three_rows() -> None:
    rows = {row["title"]: row for row in DASHBOARD["panels"] if row["type"] == "row"}
    assert list(rows) == ["Pick a unit", "History", "Test history (all matching tests)"]
    assert rows["Pick a unit"]["collapsed"] is False
    assert rows["History"]["collapsed"] is False
    assert rows["Test history (all matching tests)"]["collapsed"] is True


def test_the_top_bar_chooses_a_unit() -> None:
    assert set(variables()) == {"days", "kind", "name", "repo"}
    assert variables()["days"]["current"]["value"] == "30"
    assert variables()["kind"]["current"]["value"] == "test"
    assert variables()["kind"]["query"] == "test,job"
    assert variables()["name"]["type"] == "textbox"
    assert variables()["repo"]["hide"] == 2, "the repository is set by a job link, not typed"


def test_the_history_row_is_a_summary_two_charts_and_recent_runs() -> None:
    row = next(candidate for candidate in DASHBOARD["panels"] if candidate.get("title") == "History")
    assert [child["title"] for child in row["panels"]] == [
        "Summary",
        "Daily passes and failures",
        "Duration by day",
        "Recent runs",
    ]


def test_every_history_panel_reads_the_selected_unit() -> None:
    for title in ("Summary", "Daily passes and failures", "Duration by day", "Recent runs"):
        target = panel(title)["targets"][0]
        assert target["url"] == HISTORY_URL, title
        assert "${kind}" in target["url"]
        assert "${name:percentencode}" in target["url"]
        assert "${repo:percentencode}" in target["url"], title


def test_the_summary_shows_the_numbers_without_hovering() -> None:
    chart = panel("Summary")
    assert selectors(chart) == [
        "runs", "passes", "failures", "pass_rate",
        "p50_duration_s", "p95_duration_s", "p99_duration_s", "mean_duration_s",
        "measured_runs", "duration_basis",
    ]
    assert display_names(chart) == {
        "runs": "Runs",
        "passes": "Passed",
        "failures": "Failed",
        "pass_rate": "Pass rate",
        "p50_duration_s": "p50",
        "p95_duration_s": "p95",
        "p99_duration_s": "p99",
        "mean_duration_s": "Mean",
        "measured_runs": "Measured runs",
        "duration_basis": "Basis",
    }


def test_the_summary_says_what_the_durations_measure() -> None:
    """A number a reader cannot place is not honest. The basis and the
    measured count are visible columns, not a footnote in the panel
    description: an old window shows p99 and mean as not measured with the
    basis saying so."""
    chart = panel("Summary")
    assert "measured_runs" in selectors(chart)
    assert "duration_basis" in selectors(chart)
    assert "Basis" in display_names(chart).values()
    assert no_values(chart)["measured_runs"] == "0"


def test_a_missing_percentile_says_not_measured_never_zero() -> None:
    assert no_values(panel("Summary")) == {
        "pass_rate": "not measured",
        "p50_duration_s": "not measured",
        "p95_duration_s": "not measured",
        "p99_duration_s": "not measured",
        "mean_duration_s": "not measured",
        "measured_runs": "0",
    }
    assert no_values(panel("Duration by day")) == {
        "p50_duration_s": "not measured",
        "p95_duration_s": "not measured",
        "p99_duration_s": "not measured",
        "mean_duration_s": "not measured",
    }
    units = {}
    for entry in panel("Summary")["fieldConfig"]["overrides"]:
        field = entry["matcher"].get("options")
        for prop in entry["properties"]:
            if prop["id"] == "unit":
                units[field] = prop["value"]
    assert units["p50_duration_s"] == "s"
    assert units["mean_duration_s"] == "s"
    assert units["pass_rate"] == "percentunit"


def test_the_duration_chart_shows_all_four_statistics() -> None:
    chart = panel("Duration by day")
    assert selectors(chart) == [
        "day", "p50_duration_s", "p95_duration_s", "p99_duration_s", "mean_duration_s",
    ]
    assert chart["targets"][0]["root_selector"] == "daily"


def test_the_recent_runs_table_has_when_result_duration_and_a_link() -> None:
    chart = panel("Recent runs")
    assert selectors(chart) == [
        "created_at", "conclusion", "duration_s", "commit", "run_url",
    ]
    assert links(chart)["created_at"] == "${__data.fields.run_url}"
    assert no_values(chart)["duration_s"] == "not measured"


def test_the_picker_lists_matching_tests_and_jobs() -> None:
    tests_table = panel("Matching tests")
    jobs_table = panel("Matching jobs")
    assert tests_table["targets"][0]["url"] == MATCH_TESTS_URL
    assert tests_table["targets"][0]["root_selector"] == "tests"
    assert jobs_table["targets"][0]["url"] == JOBS_URL
    assert jobs_table["targets"][0]["root_selector"] == "jobs"
    assert "history_url" in selectors(tests_table)
    assert "history_url" in selectors(jobs_table)
    assert links(tests_table)["test_id"] == "${__data.fields.history_url}"
    assert links(jobs_table)["display_name"] == "${__data.fields.history_url}"


def test_every_selector_resolves_against_its_endpoint_contract() -> None:
    documents = {
        HISTORY_URL: [SUMMARY_DOCUMENT, DAILY_DOCUMENT, RECENT_DOCUMENT],
        MATCH_TESTS_URL: [MATCH_TESTS_DOCUMENT],
        JOBS_URL: [MATCH_JOBS_DOCUMENT],
        TRENDS_URL: [ROWS_DOCUMENT],
    }
    for panel_definition in walk(DASHBOARD["panels"]):
        if panel_definition["type"] in ("row", "text"):
            continue
        target = panel_definition["targets"][0]
        candidates = documents[target["url"]]
        document = next(
            candidate for candidate in candidates
            if target["root_selector"] in candidate
        )
        row = document[target["root_selector"]][0]
        for column in selectors(panel_definition):
            assert column in row, f"{panel_definition['title']}: {column}"


def test_the_broad_test_search_still_works() -> None:
    for title in ("Passes and failures", "Test time"):
        target = panel(title)["targets"][0]
        assert target["url"] == TRENDS_URL
        assert target["root_selector"] == "rows"
        assert target["format"] == "timeseries"


def test_the_old_duration_chart_now_reads_p99_and_mean() -> None:
    chart = panel("Test time")
    assert display_names(chart) == {
        "p50_duration_s": "Typical time",
        "p95_duration_s": "95% finish by",
        "p99_duration_s": "99% finish by",
        "mean_duration_s": "Mean",
    }


def test_every_panel_names_its_population_and_time_range() -> None:
    """The repo rule: a panel says whose data it shows and over what time.
    A panel without a description is an unlabelled number."""
    for panel_definition in walk(DASHBOARD["panels"]):
        if panel_definition["type"] in ("row", "text"):
            continue
        description = panel_definition.get("description", "")
        assert "$days" in description, panel_definition["title"]
        assert "selected" in description.lower(), panel_definition["title"]


def test_every_link_opens_a_dashboard_that_exists() -> None:
    uids = {json.loads(path.read_text())["uid"] for path in DASHBOARDS_DIR.glob("ci-observe-*.json")}
    for link in DASHBOARD["links"]:
        uid = link["url"].split("/d/")[1].split("/")[0]
        assert uid in uids, link["url"]


def test_no_template_token_is_left_unresolved() -> None:
    known = set(variables()) | {"__data", "__value"}
    for text in re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", json.dumps(DASHBOARD)):
        assert text in known or text.startswith("__"), text
