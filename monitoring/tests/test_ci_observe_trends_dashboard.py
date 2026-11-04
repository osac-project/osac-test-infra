"""Structural tests for the ci-observe Trends dashboard.

monitoring/config/grafana/dashboards/ci-observe-trends.json is the Trends
surface: one test over time from the daily JUnit rollup, the Compare section
for a cohort of captured runs, and the overall pass rate, duration and failure
mix from Prometheus and the workflow exporter. These tests pin what the model
allows the page to be -- two populations, each panel naming its population and
window, no single-run detail -- and the query shapes the panels rely on.

The data-path half lives in test_ci_observe_trends.py: the real
/api/ci-observe/trends response is resolved against this dashboard's
selectors there.
"""

from __future__ import annotations

import json
from pathlib import Path

DASHBOARD_FILE = Path(__file__).resolve().parents[1] / "config" / "grafana" / "dashboards" / "ci-observe-trends.json"
DASHBOARD = json.loads(DASHBOARD_FILE.read_text())
PANELS = DASHBOARD["panels"]
DATA_PANELS = [panel for panel in PANELS if panel["type"] not in ("row", "text")]
INFINITY = {"type": "yesoreyeram-infinity-datasource", "uid": "workflow-api"}
PROMETHEUS = {"type": "prometheus", "uid": "prometheus"}
ROLLUP_URL = "http://127.0.0.1:9103/api/ci-observe/trends"

COMPARE_ROW_TITLE = "Compare — captured runs, selected cohort, dashboard time range"
COMPARE_TITLES = [
    "Pick a cohort",
    "Exact cohorts",
    "Source-revision cohorts (not exact)",
    "Sample status",
    "Tests using the most time",
    "Runs in this cohort",
    "How to read this comparison",
    "Query limits (verified against this backend on 2026-11-01)",
    "Compare two revisions",
    "Test deltas between two source revisions",
    "Failed tests in this cohort",
    "Failed runs in this cohort",
]
COMPARE_VARIABLES = {
    "test_ref",
    "test_sha",
    "test_suite",
    "test_filter",
    "test_marker",
    "test_infra_sha",
    "test_infra_workflow_sha",
    "compare_base",
    "compare_head",
}


def panels_titled(*needles: str) -> list[dict]:
    return [panel for panel in ALL_PANELS if all(needle in panel["title"] for needle in needles)]


def targets(panel: dict) -> list[dict]:
    return panel.get("targets") or []


def iter_panels(panels: list[dict]) -> object:
    for panel in panels:
        yield panel
        yield from iter_panels(panel.get("panels", []))


ALL_PANELS = list(iter_panels(PANELS))
COMPARE_ROW = next(panel for panel in PANELS if panel.get("title") == COMPARE_ROW_TITLE)
COMPARE_PANELS = list(iter_panels(COMPARE_ROW["panels"]))


def test_identity_and_folder() -> None:
    assert DASHBOARD["uid"] == "ci-observe-trends"
    assert DASHBOARD["title"] == "CI Observe — Trends"
    assert "ci-observe" in DASHBOARD["tags"]


def test_it_is_the_third_dashboard_not_a_fourth() -> None:
    """The model allows Capture, Pull requests and Trends, plus the entry
    page. Compare is a section of Trends, not a page with its own uid."""
    folder = DASHBOARD_FILE.parent
    names = sorted(path.name for path in folder.glob("ci-observe-*.json"))
    assert names == [
        "ci-observe-pull-requests.json",
        "ci-observe-runs-home.json",
        "ci-observe-runs.json",
        "ci-observe-trends.json",
    ]
    assert "ci-observe-comparison.json" not in names
    assert len({json.loads((folder / name).read_text())["uid"] for name in names}) == len(names)


def test_every_data_panel_names_its_population_and_window() -> None:
    """A rate, count or duration names its population (all jobs or
    captured runs) and its window. The all-jobs panels sit at the top level;
    the captured-runs panels sit in the Compare section and are checked by
    the compare section tests."""
    for panel in DATA_PANELS:
        text = f"{panel['title']} {panel.get('description', '')}"
        assert "all jobs" in text or "all workflow runs" in text or "E2E jobs" in text, panel["title"]
        assert "selected range" in text or "$days days" in text, panel["title"]
        assert "captured runs" not in panel["title"]


def test_populations_never_share_a_panel() -> None:
    """All-jobs panels come from the rollup, Prometheus or the exporter.
    None of them reads a capture, so no panel mixes the two populations."""
    for panel in DATA_PANELS:
        text = json.dumps(panel)
        assert "ci-observe-traces" not in text
        assert "ci-observe-metrics" not in text
        assert "ci-observe-logs" not in text


def test_test_history_panels_read_the_rollup() -> None:
    history = [panel for panel in DATA_PANELS if "all jobs" in panel["title"] and "last $days days" in panel["title"]]
    assert len(history) == 4, "pass/fail, duration, the rollup table and the coverage table"
    for panel in history:
        assert panel["datasource"] == INFINITY
        for target in targets(panel):
            assert target["url"].startswith(ROLLUP_URL)
            assert "days=${days}" in target["url"]


def test_pass_and_fail_counts_are_two_series_of_one_chart() -> None:
    panel = panels_titled("Passes and failures")[0]
    target = targets(panel)[0]

    assert panel["type"] == "timeseries"
    assert target["format"] == "timeseries"
    columns = {column["selector"]: column["type"] for column in target["columns"]}
    assert columns == {"day": "timestamp", "passes": "number", "failures": "number"}
    colors = {override["matcher"]["options"]: override["properties"] for override in panel["fieldConfig"]["overrides"]}
    assert colors["passes"][0]["value"]["fixedColor"] == "green"
    assert colors["failures"][0]["value"]["fixedColor"] == "red"


def test_duration_panel_shows_p50_and_p95() -> None:
    panel = panels_titled("Duration p50 and p95")[0]
    target = targets(panel)[0]

    columns = {column["selector"]: column["type"] for column in target["columns"]}
    assert columns == {"day": "timestamp", "p50_duration_s": "number", "p95_duration_s": "number"}
    assert panel["fieldConfig"]["defaults"]["unit"] == "s"


def test_the_rollup_table_links_to_the_runs_behind_a_point() -> None:
    """Flake is the same test passing and failing on one SHA, so the SHA is
    what a reader follows to the runs."""
    panel = panels_titled("Rollup rows")[0]
    sha_override = next(
        override for override in panel["fieldConfig"]["overrides"]
        if override["matcher"]["options"] == "source_sha"
    )
    link = sha_override["properties"][-1]
    assert link["id"] == "links"
    assert "/commit/${__value.raw}/checks" in link["value"][0]["url"]


def test_the_rollup_table_carries_every_rollup_column() -> None:
    target = targets(panels_titled("Rollup rows")[0])[0]
    assert {column["selector"] for column in target["columns"]} == {
        "day", "repo", "workflow", "suite", "test_id", "source_sha",
        "runs", "passes", "failures", "p50_duration_s", "p95_duration_s",
    }


def test_coverage_panel_makes_the_artifact_gap_visible() -> None:
    """A day whose artifacts had expired is partial, not green: the page
    says so from the rollup's own coverage rows."""
    panel = panels_titled("Rollup coverage")[0]
    target = targets(panel)[0]

    assert target["root_selector"] == "coverage"
    assert "runs_unavailable" in panel["description"]
    assert {column["selector"] for column in target["columns"]} >= {
        "day", "runs_listed", "runs_parsed", "runs_unavailable"
    }


def test_overview_reads_the_central_prometheus() -> None:
    overview = [panel for panel in DATA_PANELS if panel["datasource"] == PROMETHEUS]
    assert len(overview) >= 4

    exprs = {target["expr"] for panel in overview for target in targets(panel)}
    assert any("github_actions_completed_runs_total" in expr and "conclusion=\"success\"" in expr for expr in exprs)
    assert any("github_actions_run_duration_seconds_bucket" in expr for expr in exprs)
    for panel in overview:
        assert "all workflow runs" in panel["title"]
        assert "selected range" in panel["title"]


def test_failure_mix_comes_from_the_exporter_and_carries_the_time_range() -> None:
    mix = [panel for panel in DATA_PANELS if panel["datasource"] == INFINITY and "exporter" in panel["title"]]
    assert len(mix) == 2

    for panel in mix:
        assert "E2E jobs" in panel["title"]
        assert "selected range" in panel["title"]
        for target in targets(panel):
            assert target["url"].startswith("http://127.0.0.1:9103/api/presubmit-infra-failures")
            assert "category=e2e" in target["url"]
            assert "since=${__from:date:iso}" in target["url"]
            assert "until=${__to:date:iso}" in target["url"]


def test_the_compare_section_is_a_collapsed_row_at_the_bottom() -> None:
    """Compare is an action on a cohort of captured runs, so it is a section
    of Trends. Grafana runs a collapsed row's queries only when it expands,
    so the all-jobs panels above it load without the trace backend."""
    row = COMPARE_ROW
    assert row["type"] == "row"
    assert row["collapsed"] is True
    assert row["title"].startswith("Compare")
    assert "captured runs" in row["title"]
    assert "time range" in row["title"]
    assert PANELS[-1] is row, "the Compare row comes after the all-jobs sections"

    titles = [panel["title"] for panel in row["panels"] if panel["title"]]
    assert titles == COMPARE_TITLES
    positions = [(panel["gridPos"]["y"], panel["gridPos"]["x"]) for panel in row["panels"]]
    assert positions == sorted(positions)


def test_the_compare_section_reads_captured_runs_only() -> None:
    """The Compare section is the only part of Trends that reads captures,
    and it keeps to the ci-observe trace datasource."""
    for panel in COMPARE_PANELS:
        if panel["title"] == "Test deltas between two source revisions":
            # Q8 compares two source revisions from the rollup, not captures.
            (target,) = targets(panel)
            assert target["url"].startswith(
                "http://127.0.0.1:9103/api/ci-observe/trends/compare"
            )
            continue
        for target in targets(panel):
            assert target["datasource"]["type"] == "tempo"
            assert target["datasource"]["uid"] == "ci-observe-traces"
        text = json.dumps(panel)
        assert "prometheus" not in text
        assert "workflow-api" not in text


def test_the_compare_variables_extend_the_existing_pair() -> None:
    """The cohort variables are appended to the dashboard; the days and test
    variables keep their meaning and their order for the rollup URLs."""
    variables = [variable["name"] for variable in DASHBOARD["templating"]["list"]]
    assert variables[:2] == ["days", "test"]
    assert set(variables) == {"days", "test"} | COMPARE_VARIABLES
    assert len(variables) == len(set(variables))
    by_name = {variable["name"]: variable for variable in DASHBOARD["templating"]["list"]}
    for name in COMPARE_VARIABLES:
        assert by_name[name]["type"] == "textbox"


def test_there_is_no_single_run_detail() -> None:
    """One run belongs to Capture. This page links to runs, it does not
    render one, and the Compare section accepts a cohort, not a capture."""
    variables = {variable["name"] for variable in DASHBOARD["templating"]["list"]}
    assert variables == {"days", "test"} | COMPARE_VARIABLES

    body = json.dumps(PANELS)
    assert '"name": "capture_id"' not in body
    assert "/api/ci-observe/run" not in body
    assert "osac.ci.run_id" not in body


def test_links_point_at_the_run_index_and_github() -> None:
    urls = [link["url"] for link in DASHBOARD["links"]]

    assert any("/d/ci-observe-runs-home/" in url for url in urls)
    assert any("github.com/osac-project/osac/actions" in url for url in urls)


def test_every_infinity_target_declares_columns() -> None:
    """An Infinity target with no columns renders an empty panel, which
    would look like "no data" instead of a wiring mistake."""
    for panel in ALL_PANELS:
        if panel.get("datasource") != INFINITY:
            continue
        for target in targets(panel):
            assert target["root_selector"], panel["title"]
            assert target["columns"], panel["title"]
            for column in target["columns"]:
                assert column["type"] in ("string", "number", "timestamp"), panel["title"]


def test_the_page_states_what_the_seven_day_limit_means() -> None:
    text = [panel for panel in PANELS if panel["type"] == "text"]
    body = " ".join(panel["options"]["content"] for panel in text)

    assert "7 days" in body
    assert "first rollup" in body
    assert "cannot be reconstructed" in body
