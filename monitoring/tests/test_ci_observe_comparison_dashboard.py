"""Structural tests for the Compare section of the ci-observe Trends dashboard.

Compare is an action, not a dashboard: the model puts it as a collapsed row on
monitoring/config/grafana/dashboards/ci-observe-trends.json. This section
answers two questions across comparable captures: what changed versus a passing
run (Q7) and which fixes matter most (Q8). It opens with a short intro, the
exact-cohort table, the ungrouped-capture table, the sample-status stat, the
tests table, the runs table, the reading guide and the query limits, and it
keeps the failed-test evidence at the bottom.

Cohort discovery reads /api/ci-observe/cohorts, not Tempo. The registry groups
exact cohorts on the six identity fields and returns captures whose
`identity_source` is `none` in a separate ungrouped list. The page never merges
the two lists and never uses `unknown` as a cohort value. The span tables and
the failed evidence still read Tempo, but only after the collapsed row expands,
so a page load runs no Tempo query. Q8 stays on /api/ci-observe/trends/compare.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

DASHBOARDS_DIR = Path(__file__).resolve().parents[1] / "config" / "grafana" / "dashboards"
DASHBOARD_FILE = DASHBOARDS_DIR / "ci-observe-trends.json"

SHA_PATTERN = "^[0-9a-f]{40}([0-9a-f]{24})?$"
SHA_RE = re.compile(SHA_PATTERN)

PHASE_EXCLUSION = 'name !~ ".*::(setup|call|teardown)"'
NODEID_PROJECTION = '.pytest.nodeid =~ ".+"'
FUNCTION_PROJECTION = '.code.function =~ ".+"'

COMPARISON_LINK = "/d/ci-observe-trends/ci-observe-e28094-trends"
RUN_INVESTIGATION_LINK = "/d/ci-observe-runs/ci-observe-e28094-run-investigation"

COMPARE_ROW_TITLE = "Compare — captured runs, selected cohort, dashboard time range"
UNGROUPED_TITLE = "Ungrouped captures (incomplete identity, not a cohort)"
LIMITS_TITLE = "Query limits and data sources (capture registry + Tempo on expand)"

COMPARE_PANEL_TITLES = [
    "Pick a cohort",
    "Exact cohorts",
    UNGROUPED_TITLE,
    "Sample status",
    "Tests using the most time",
    "Runs in this cohort",
    "How to read this comparison",
    LIMITS_TITLE,
    "Compare two revisions",
    "Test deltas between two source revisions",
    "",
    "Failed tests in this cohort",
    "Failed runs in this cohort",
]

IDENTITY_FIELDS = [
    "test_sha",
    "test_infra_sha",
    "test_infra_workflow_sha",
    "test_suite",
    "test_filter",
    "test_marker",
]

COHORT_VARIABLES = {
    *IDENTITY_FIELDS,
    "test_ref",
    "compare_base",
    "compare_head",
}

ANALYSIS_TITLES = [
    "Tests using the most time",
    "Failed tests in this cohort",
    "Failed runs in this cohort",
    "Runs in this cohort",
]

TARGET_TITLES = [
    "Exact cohorts",
    UNGROUPED_TITLE,
    "Sample status",
    "Tests using the most time",
    "Runs in this cohort",
    "Test deltas between two source revisions",
    "Failed tests in this cohort",
    "Failed runs in this cohort",
]

MAX_TOTAL_TARGETS = 8
GRAFANA_BUILTIN_VARIABLE = re.compile(r"^__")
API = "http://127.0.0.1:9103/api/ci-observe"


def load_dashboard() -> dict[str, object]:
    return json.loads(DASHBOARD_FILE.read_text())


def iter_panels(panels: list[dict]) -> object:
    """Yield top-level panels and any panels nested in collapsed rows."""
    for panel in panels:
        yield panel
        yield from iter_panels(panel.get("panels", []))


def panel_by_title(*, dashboard: dict[str, object], title: str) -> dict[str, object]:
    for panel in iter_panels(dashboard["panels"]):
        if panel["title"] == title:
            return panel
    raise AssertionError(f"dashboard panel {title!r} is missing")


def compare_row(dashboard: dict[str, object]) -> dict[str, object]:
    row = panel_by_title(dashboard=dashboard, title=COMPARE_ROW_TITLE)
    assert row["type"] == "row", "Compare must be a row on Trends, not a panel"
    return row


def compare_panels(dashboard: dict[str, object]) -> list[dict[str, object]]:
    return list(iter_panels(compare_row(dashboard)["panels"]))


def display_names(*, panel: dict[str, object]) -> set[str]:
    names: set[str] = set()
    for override in panel["fieldConfig"]["overrides"]:
        for field_property in override["properties"]:
            if field_property["id"] == "displayName":
                names.add(field_property["value"])
    return names


def field_links(*, panel: dict[str, object], field: str) -> list[dict[str, object]]:
    for override in panel["fieldConfig"]["overrides"]:
        if override["matcher"]["options"] == field:
            for field_property in override["properties"]:
                if field_property["id"] == "links":
                    return field_property["value"]
    return []


def transforms(*, panel: dict[str, object], transform_id: str) -> list[dict[str, object]]:
    return [t for t in panel.get("transformations", []) if t["id"] == transform_id]


def selectors(panel: dict[str, object]) -> set[str]:
    return {column["selector"] for column in panel["targets"][0]["columns"]}


def resolve_rows(document: dict, root_selector: str) -> list[dict]:
    if root_selector == "$":
        return [document]
    node = document
    for part in root_selector.lstrip("$.").split("."):
        node = node[part]
    return node if isinstance(node, list) else [node]


def test_compare_section_identity_and_titles() -> None:
    dashboard = load_dashboard()

    assert dashboard["uid"] == "ci-observe-trends"
    assert dashboard["schemaVersion"] == 39
    assert not (DASHBOARDS_DIR / "ci-observe-comparison.json").exists(), (
        "the separate comparison dashboard was merged into Trends"
    )

    row = compare_row(dashboard)
    assert row["collapsed"] is True
    assert [panel["title"] for panel in row["panels"]] == COMPARE_PANEL_TITLES
    positions = [(p["gridPos"]["y"], p["gridPos"]["x"]) for p in row["panels"]]
    assert positions == sorted(positions)
    for panel in compare_panels(dashboard):
        if panel["type"] == "row" or panel["title"] == "":
            continue
        assert panel["description"], f"panel {panel['title']!r} has no description"


def test_every_panel_has_at_most_one_target_and_section_stays_within_eight() -> None:
    """Two hard limits. Per panel: Grafana's Tempo plugin would overwrite every
    target after the first, so a panel keeps one target. Section-wide: the
    section ships eight targets."""
    dashboard = load_dashboard()
    total = 0
    for panel in compare_panels(dashboard):
        target_count = len(panel.get("targets", []))
        total += target_count
        assert target_count <= 1, (
            f"{panel['title']} has {target_count} targets; keep one per panel"
        )
    assert total <= MAX_TOTAL_TARGETS, f"the section runs {total} queries; keep it at {MAX_TOTAL_TARGETS}"
    assert total == 8, "the section ships eight targets: two cohort lists, the sample stat, four span tables and the two-revision delta"

    with_targets = [p["title"] for p in compare_panels(dashboard) if p.get("targets")]
    assert with_targets == TARGET_TITLES


def test_no_duplicate_count_stats_remain() -> None:
    """The count stats that doubled the query load are gone; the sample status is
    the section's only stat."""
    dashboard = load_dashboard()
    titles = {p["title"] for p in compare_panels(dashboard)}
    for removed in ("Captures in cohort", "Failed tests in cohort", "Failed runs in cohort"):
        assert removed not in titles, f"{removed} was removed to cut the query load"
    stats = [p for p in compare_panels(dashboard) if p["type"] == "stat"]
    assert [p["title"] for p in stats] == ["Sample status"]


def test_failed_evidence_lives_at_the_bottom_of_the_collapsed_row() -> None:
    """The failed tables render headers-only when empty, so they stay last inside
    the collapsed Compare row, behind an explanatory text panel."""
    dashboard = load_dashboard()
    row = compare_row(dashboard)
    assert row["collapsed"] is True
    nested = row["panels"]
    assert [p["title"] for p in nested if p["type"] == "table"][-2:] == [
        "Failed tests in this cohort",
        "Failed runs in this cohort",
    ]
    assert [p["title"] for p in nested[-2:]] == ["Failed tests in this cohort", "Failed runs in this cohort"], (
        "the failed evidence stays at the end of the section"
    )
    note = next(p for p in nested if p["type"] == "text" and p["title"] == "")
    content = note["options"]["content"]
    assert "If either table is empty, no failed tests or failed runs were recorded for this cohort" in content
    assert "sample status above shows the cohort size" in content
    intro = panel_by_title(dashboard=dashboard, title="Pick a cohort")["options"]["content"]
    assert "bottom of this section" in intro


def test_page_opens_with_the_sample_rule_before_the_analysis_tables() -> None:
    """A newcomer sees the intro, the cohort tables, the sample status, the tests
    table, the runs table, the guides and the failed evidence."""
    dashboard = load_dashboard()
    intro = panel_by_title(dashboard=dashboard, title="Pick a cohort")
    content = intro["options"]["content"]

    assert not content.startswith("#"), "the panel title must not be repeated as a heading"
    assert "not exact" in content
    assert "not a baseline" in content
    assert "at least 2 captures" in content
    assert "stable median needs 3" in content
    assert "Sample status" in content
    assert "Observed time is not runner time" in content
    assert "cannot be converted into runner minutes" in content
    assert "up to two minutes to load" in content, "the intro must set loading expectations"
    assert "not an empty result" in content
    assert "bottom of this section" in content, "the intro must explain where the failed evidence is"
    assert "You pick the cohort here" in content
    assert "does not preselect a capture" in content

    titles = [p["title"] for p in compare_row(dashboard)["panels"]]
    assert titles.index("Pick a cohort") < titles.index("Exact cohorts")
    assert titles.index(UNGROUPED_TITLE) < titles.index("Sample status")
    assert titles.index("Sample status") < titles.index("Tests using the most time")
    assert titles.index("Tests using the most time") < titles.index("Runs in this cohort")
    assert titles.index("Runs in this cohort") < titles.index("How to read this comparison")
    assert titles.index("How to read this comparison") < titles.index(LIMITS_TITLE)
    assert titles.index(LIMITS_TITLE) < titles.index("Failed tests in this cohort")
    assert titles[-1] == "Failed runs in this cohort"
    for title, min_height in [
        ("Pick a cohort", 11),
        ("How to read this comparison", 20),
        (LIMITS_TITLE, 18),
    ]:
        panel = panel_by_title(dashboard=dashboard, title=title)
        assert panel["gridPos"]["h"] >= min_height, f"{title} is too short to show its text"
    for title in ("Exact cohorts", UNGROUPED_TITLE):
        assert panel_by_title(dashboard=dashboard, title=title)["gridPos"]["h"] == 6


def test_sample_status_reads_the_registry_count_for_the_selected_cohort() -> None:
    """The stat between the cohort tables and the analysis tables shows the
    selected exact cohort's distinct capture count from /cohorts."""
    dashboard = load_dashboard()
    stat = panel_by_title(dashboard=dashboard, title="Sample status")

    assert stat["type"] == "stat"
    assert stat["datasource"] == {"type": "yesoreyeram-infinity-datasource", "uid": "workflow-api"}
    (target,) = stat["targets"]
    assert target["url"] == f"{API}/cohorts?days=${{days}}"
    assert target["root_selector"] == "$.cohorts"
    assert selectors(stat) >= set(IDENTITY_FIELDS) | {"captures"}

    reduce_options = stat["options"]["reduceOptions"]
    assert reduce_options["calcs"] == ["lastNotNull"]
    assert reduce_options["fields"] == "captures"

    mappings = stat["fieldConfig"]["defaults"]["mappings"]
    value_options = mappings[0]["options"]
    assert value_options["1"]["text"] == "No baseline: 1 capture only"
    assert value_options["2"]["text"] == "Not enough for a stable median"
    range_mapping = mappings[1]
    assert range_mapping["type"] == "range"
    assert range_mapping["options"]["from"] == 3
    assert "Usable sample" in range_mapping["options"]["result"]["text"]
    assert "at least 2 captures" in stat["description"]
    assert "No cohort selected" in stat["fieldConfig"]["defaults"]["noValue"]

    steps = stat["fieldConfig"]["defaults"]["thresholds"]["steps"]
    assert [(s["value"], s["color"]) for s in steps] == [(None, "red"), (2, "yellow"), (3, "green")]


def test_sample_status_filters_on_all_six_identity_fields() -> None:
    stat = panel_by_title(dashboard=load_dashboard(), title="Sample status")
    (value_filter,) = transforms(panel=stat, transform_id="filterByValue")
    filters = value_filter["options"]["filters"]
    assert value_filter["options"]["match"] == "all"
    assert {f["fieldName"] for f in filters} == set(IDENTITY_FIELDS)
    for filter_config in filters:
        assert filter_config["config"] == {
            "id": "equal",
            "options": {"value": "${" + filter_config["fieldName"] + "}"},
        }


def test_every_analysis_panel_explains_an_empty_state() -> None:
    """No analysis panel may render an unexplained headers-only box: each has a
    noValue message pointing at the sample status."""
    dashboard = load_dashboard()
    for title in ANALYSIS_TITLES:
        panel = panel_by_title(dashboard=dashboard, title=title)
        no_value = panel["fieldConfig"]["defaults"]["noValue"]
        assert no_value, f"{title} has no noValue message"
        assert "cohort" in no_value, f"{title} noValue must say why it is empty"
        assert "Pick a cohort above" not in no_value, (
            f"{title} noValue must not claim no cohort is selected while one is"
        )
        assert "sample status above" in no_value, f"{title} must point at the sample status"

    runs_panel = panel_by_title(dashboard=dashboard, title="Runs in this cohort")
    assert "No captures matched the selected cohort" in runs_panel["fieldConfig"]["defaults"]["noValue"]
    tests_panel = panel_by_title(dashboard=dashboard, title="Tests using the most time")
    assert "No captures matched the current selection" in tests_panel["fieldConfig"]["defaults"]["noValue"]


def test_dashboard_time_range_fits_the_backend_window() -> None:
    """Tempo on this backend rejects ranges over 168 hours, so the dashboard's
    default range is 7 days and the Compare span tables share it."""
    assert load_dashboard()["time"] == {"from": "now-7d", "to": "now"}


def test_variables_are_the_cohort_link_state() -> None:
    dashboard = load_dashboard()
    variables = {v["name"]: v for v in dashboard["templating"]["list"]}

    assert set(variables) == {"days", "test"} | COHORT_VARIABLES
    for name in COHORT_VARIABLES:
        assert variables[name]["type"] == "textbox", f"{name} must be a textbox set by the cohort links"

    for name in ("test_ref", "test_sha", "test_infra_sha", "test_infra_workflow_sha"):
        assert variables[name]["current"]["value"] == "", f"{name} must default to empty"
    assert variables["test_suite"]["current"]["value"] == "all"
    assert variables["test_filter"]["current"]["value"] == "none"
    assert variables["test_marker"]["current"]["value"] == "none"


def test_no_dead_dropdown_dead_links_or_capture_preselection() -> None:
    """The cohort_mode dropdown and the run-evidence link are gone, and this
    section does not pretend to accept a capture selection from elsewhere."""
    dashboard = load_dashboard()
    raw = json.dumps(compare_row(dashboard))
    assert "cohort_mode" not in raw, "the dead cohort_mode dropdown must not come back"
    assert "var-cohort_mode" not in raw
    assert "run-evidence" not in raw, "links to the not-yet-existing run-evidence dashboard must not come back"
    assert "ci-observe-run-evidence" not in raw
    assert "links onward" not in raw, "the run investigation page has no outbound links; do not claim any"
    assert "Current mode" not in raw
    assert '"name": "capture_id"' not in raw, "this section must not add a capture_id variable"
    assert "var-capture_id=" not in "".join(
        link["url"]
        for p in compare_panels(dashboard)
        for o in (p.get("fieldConfig") or {}).get("overrides", [])
        for prop in o["properties"]
        if prop["id"] == "links"
        for link in prop["value"]
        if link["url"].startswith(COMPARISON_LINK)
    ), "cohort links must not pass var-capture_id"

    for panel in compare_panels(dashboard):
        for override in (panel.get("fieldConfig") or {}).get("overrides", []):
            for field_property in override["properties"]:
                if field_property["id"] != "links":
                    continue
                for link in field_property["value"]:
                    assert link["url"].split("?")[0] in (
                        COMPARISON_LINK,
                        RUN_INVESTIGATION_LINK,
                    ), f"{panel['title']}: unexpected link target {link['url']}"


def test_sha_pattern_accepts_only_pinned_commits() -> None:
    """A resolved identity SHA is a pinned commit: 40 or 64 lowercase hex."""
    assert SHA_RE.search("91e852e8f48b642428f3e927e93db3e2dcb3d667")
    assert SHA_RE.search("a" * 64)
    for rejected in ("main", "unknown", "", "release/1.2", "feature-branch", "A" * 40, "a" * 39, "a" * 41):
        assert not SHA_RE.search(rejected), f"{rejected!r} must not form a cohort"


def test_exact_cohorts_read_the_registry_grouped_on_six_identity_fields() -> None:
    """The exact table asks /cohorts for rows the registry already grouped on
    the six identity fields. The requested ref is metadata, not a group key."""
    panel = panel_by_title(dashboard=load_dashboard(), title="Exact cohorts")
    (target,) = panel["targets"]

    assert panel["datasource"] == {"type": "yesoreyeram-infinity-datasource", "uid": "workflow-api"}
    assert target["url"] == f"{API}/cohorts?days=${{days}}"
    assert target["root_selector"] == "$.cohorts"
    assert selectors(panel) == set(IDENTITY_FIELDS) | {"test_ref", "captures"}
    assert not transforms(panel=panel, transform_id="groupBy"), "grouping belongs to the registry, not the page"
    assert not transforms(panel=panel, transform_id="joinByField")

    description = panel["description"]
    assert "six identity fields" in description
    assert "requested ref is metadata" in description
    assert "ungrouped capture is never part of" in description
    assert "No exact cohort" in panel["fieldConfig"]["defaults"]["noValue"]


def test_exact_cohort_links_set_every_variable() -> None:
    panel = panel_by_title(dashboard=load_dashboard(), title="Exact cohorts")
    links = field_links(panel=panel, field="test_sha")
    assert len(links) == 1
    url = links[0]["url"]

    assert url.startswith(COMPARISON_LINK)
    assert "var-test_sha=${__value.raw:percentencode}" in url
    assert 'var-test_ref=${__data.fields.test_ref:percentencode}' in url
    for attribute in ("test_suite", "test_filter", "test_marker", "test_infra_sha", "test_infra_workflow_sha"):
        assert f'var-{attribute}=${{__data.fields.{attribute}:percentencode}}' in url
    assert "var-capture_id" not in url


def test_ungrouped_captures_are_a_separate_list_never_a_cohort() -> None:
    """identity_source=none rows are returned by the same endpoint in their own
    list. They are shown on their own, never merged into an exact cohort."""
    panel = panel_by_title(dashboard=load_dashboard(), title=UNGROUPED_TITLE)
    (target,) = panel["targets"]

    assert panel["datasource"] == {"type": "yesoreyeram-infinity-datasource", "uid": "workflow-api"}
    assert target["url"] == f"{API}/cohorts?days=${{days}}"
    assert target["root_selector"] == "$.ungrouped"
    assert selectors(panel) >= {"capture_id", "run_id", "suite", "test_ref", "identity_source", "reason"}
    assert not transforms(panel=panel, transform_id="groupBy")
    assert not transforms(panel=panel, transform_id="joinByField"), "the ungrouped list is never joined to cohorts"

    description = panel["description"]
    assert "identity_source=none" in description
    assert "not exact" in description
    assert "not a baseline" in description
    assert "never merged into an exact cohort" in description

    identity = next(
        o for o in panel["fieldConfig"]["overrides"] if o["matcher"]["options"] == "identity_source"
    )
    mapping = next(p["value"] for p in identity["properties"] if p["id"] == "mappings")
    assert mapping[0]["options"]["none"]["text"] == "none"


def test_ungrouped_links_open_capture_with_the_capture_window() -> None:
    panel = panel_by_title(dashboard=load_dashboard(), title=UNGROUPED_TITLE)
    links = field_links(panel=panel, field="capture_id")
    assert len(links) == 1
    url = links[0]["url"]
    assert url.startswith(RUN_INVESTIGATION_LINK)
    assert "var-capture_id=${__value.raw}" in url
    # Registry-only rows can have no info-metric start time; the endpoint
    # supplies a window that always contains the capture.
    # Grafana ignores a 20-character ISO value in from/to; epoch ms works.
    assert "from=${__data.fields.capture_window_from_ms}" in url
    assert "to=${__data.fields.capture_window_to_ms}" in url


def test_unknown_is_never_a_cohort_value() -> None:
    """The registry never emits `unknown` as an identity value, and the page
    never writes one into a cohort link."""
    dashboard = load_dashboard()
    for title in ("Exact cohorts", UNGROUPED_TITLE):
        panel = panel_by_title(dashboard=dashboard, title=title)
        for link in (
            link
            for override in panel["fieldConfig"]["overrides"]
            for prop in override["properties"]
            if prop["id"] == "links"
            for link in prop["value"]
        ):
            assert "unknown" not in link["url"].lower()

    limits = panel_by_title(dashboard=dashboard, title=LIMITS_TITLE)["options"]["content"]
    assert "`unknown` is never a cohort value" in limits


def test_no_tempo_discovery_or_span_cap_remains() -> None:
    """Cohort discovery is SQL in the registry. The remaining span tables are
    selected by the six identity variables, not by a wildcard capture search,
    and no target asks Tempo for an `spss` cap."""
    dashboard = load_dashboard()
    for panel in compare_panels(dashboard):
        for target in panel.get("targets", []):
            assert "spss" not in target, panel["title"]
            query = target.get("query", "")
            assert 'resource."osac.ci.capture_id"' not in query, panel["title"]
            assert "osac.ci.test_ref" not in query, panel["title"]
            assert ") || (" not in query, panel["title"]


def test_span_tables_use_the_exact_identity_variables_only() -> None:
    for title in ANALYSIS_TITLES:
        panel = panel_by_title(dashboard=load_dashboard(), title=title)
        query = panel["targets"][0]["query"]
        assert PHASE_EXCLUSION in query
        assert NODEID_PROJECTION in query
        assert FUNCTION_PROJECTION in query
        for field in IDENTITY_FIELDS:
            assert f'resource."osac.ci.{field}" = ${{{field}:doublequote}}' in query
        assert 'resource."osac.ci.suite"' not in query


def test_time_ranking_groups_per_capture_then_per_test() -> None:
    """Sum per (test, capture, attempt) first, then take sum/median/min/max per test.
    Rows are keyed by the pytest node id, and the Test column shows the test's
    code.function so the name fits the viewport; the full node id stays in its
    own wrapped column. The capture denominator is a distinct count, so
    duplicate spans for one test in one capture cannot inflate it."""
    panel = panel_by_title(dashboard=load_dashboard(), title="Tests using the most time")
    group_bys = transforms(panel=panel, transform_id="groupBy")

    per_capture, per_test = group_bys
    assert per_capture["options"]["fields"] == {
        "pytest.nodeid": {"aggregations": [], "operation": "groupby"},
        "osac.ci.capture_id": {"aggregations": [], "operation": "groupby"},
        "osac.ci.run_attempt": {"aggregations": [], "operation": "groupby"},
        "duration": {"aggregations": ["sum"], "operation": "aggregate"},
    }
    assert per_test["options"]["fields"]["pytest.nodeid"]["operation"] == "groupby"
    assert per_test["options"]["fields"]["Duration (sum)"]["aggregations"] == ["sum", "median", "min", "max"]
    assert per_test["options"]["fields"]["osac.ci.capture_id"] == {
        "aggregations": ["distinctCount"],
        "operation": "aggregate",
    }
    assert per_test["options"]["fields"]["code.function"] == {"aggregations": ["last"], "operation": "aggregate"}

    sort = transforms(panel=panel, transform_id="sortBy")[0]
    assert sort == {"id": "sortBy", "options": {"sort": [{"field": "Duration (sum) (sum)", "desc": True}]}}
    limit = transforms(panel=panel, transform_id="limit")[0]
    assert limit == {"id": "limit", "options": {"limitField": 30}}

    assert not transforms(panel=panel, transform_id="joinByField")
    assert not transforms(panel=panel, transform_id="calculateField")

    assert {"Test", "Full test id", "Total time", "Median", "Shortest", "Longest", "Captures"} == display_names(
        panel=panel
    )


def test_failed_tests_table_lists_failed_tests_per_capture() -> None:
    """The failed-tests table groups failed spans per (test, capture) and then per
    test: failed-capture counts, the capture list, and failed-run durations. No
    computed failure rate, no all-captures statistics."""
    dashboard = load_dashboard()
    panel = panel_by_title(dashboard=dashboard, title="Failed tests in this cohort")
    query = panel["targets"][0]["query"]

    assert query.endswith("&& status = error }")
    assert NODEID_PROJECTION in query
    assert FUNCTION_PROJECTION in query

    per_capture, per_test = transforms(panel=panel, transform_id="groupBy")
    assert per_capture["options"]["fields"]["pytest.nodeid"]["operation"] == "groupby"
    assert per_capture["options"]["fields"]["osac.ci.capture_id"]["operation"] == "groupby"
    assert per_capture["options"]["fields"]["duration"]["aggregations"] == ["sum"]
    assert per_test["options"]["fields"]["pytest.nodeid"]["operation"] == "groupby"
    assert per_test["options"]["fields"]["osac.ci.capture_id"]["aggregations"] == ["distinctCount", "allValues"]
    assert per_test["options"]["fields"]["Duration (sum)"]["aggregations"] == ["median", "min", "max"]
    assert per_test["options"]["fields"]["code.function"] == {"aggregations": ["last"], "operation": "aggregate"}

    sort = transforms(panel=panel, transform_id="sortBy")[0]
    assert sort["options"]["sort"] == [{"field": "osac.ci.capture_id (distinctCount)", "desc": True}]

    assert not transforms(panel=panel, transform_id="joinByField")
    assert not transforms(panel=panel, transform_id="calculateField")

    assert {
        "Test",
        "Full test id",
        "Failed captures",
        "Failed in captures",
        "Median",
        "Shortest",
        "Longest",
    } == display_names(panel=panel)
    assert "evidence, not a diagnosis" in panel["description"]
    assert "Failure rate" not in json.dumps(compare_row(dashboard))


def test_failed_runs_table_shows_each_failed_capture_with_links() -> None:
    """One row per failed test occurrence: capture (linked to Capture),
    test function, time and duration."""
    panel = panel_by_title(dashboard=load_dashboard(), title="Failed runs in this cohort")
    query = panel["targets"][0]["query"]

    assert query.endswith("&& status = error }")
    assert NODEID_PROJECTION in query
    assert FUNCTION_PROJECTION in query

    group_bys = transforms(panel=panel, transform_id="groupBy")
    assert len(group_bys) == 1
    fields = group_bys[0]["options"]["fields"]
    assert fields["osac.ci.capture_id"]["operation"] == "groupby"
    assert fields["pytest.nodeid"]["operation"] == "groupby"
    assert fields["duration"]["aggregations"] == ["sum"]
    assert fields["time"]["aggregations"] == ["min"]
    assert fields["code.function"] == {"aggregations": ["last"], "operation": "aggregate"}

    sort = transforms(panel=panel, transform_id="sortBy")[0]
    assert sort["options"]["sort"] == [{"field": "Start time (min)", "desc": True}]

    links = field_links(panel=panel, field="osac.ci.capture_id")
    assert len(links) == 1
    assert links[0]["url"].startswith(RUN_INVESTIGATION_LINK)
    assert "var-capture_id=${__value.raw}" in links[0]["url"]

    assert {"Capture", "Failed at", "Test", "Test time"} == display_names(panel=panel)


def test_runs_table_makes_the_sample_transparent() -> None:
    """The runs table lists the cohort's captures from the cohort's own test
    spans: capture (linked), started, tests, total observed time. No session
    join; the description says session status lives on the run page."""
    panel = panel_by_title(dashboard=load_dashboard(), title="Runs in this cohort")
    query = panel["targets"][0]["query"]

    assert not query.endswith("&& status = error }")
    assert NODEID_PROJECTION in query
    assert FUNCTION_PROJECTION in query
    assert 'name = "test run"' not in query, "the runs table no longer queries session spans"
    assert 'resource."osac.ci.run_attempt" != nil' not in query, "the runs table drops the unused predicate"

    group_bys = transforms(panel=panel, transform_id="groupBy")
    assert len(group_bys) == 1
    fields = group_bys[0]["options"]["fields"]
    assert fields["osac.ci.capture_id"]["operation"] == "groupby"
    assert fields["duration"]["aggregations"] == ["sum"]
    assert fields["pytest.nodeid"]["aggregations"] == ["distinctCount"]
    assert fields["time"]["aggregations"] == ["min"]

    sort = transforms(panel=panel, transform_id="sortBy")[0]
    assert sort["options"]["sort"] == [{"field": "Start time (min)", "desc": True}]

    links = field_links(panel=panel, field="osac.ci.capture_id")
    assert len(links) == 1
    assert links[0]["url"].startswith(RUN_INVESTIGATION_LINK)
    assert "var-capture_id=${__value.raw}" in links[0]["url"]

    assert {"Capture", "Started", "Tests", "Total time"} == display_names(panel=panel)
    assert "not joined here" in panel["description"]


def test_table_columns_fit_1280_and_show_full_names() -> None:
    """Every table's fixed column widths must fit the ~1150px usable at 1280 with
    the sidebar expanded, every header must fit its column, and the long-text
    columns (full node id, capture lists) wrap instead of truncating. SHA and
    capture columns are wide enough for all 40 (or 26) characters."""
    dashboard = load_dashboard()
    wrap_fields = {
        "Tests using the most time": {"pytest.nodeid"},
        "Failed tests in this cohort": {"pytest.nodeid", "osac.ci.capture_id (allValues)"},
    }
    for title, wrapped in wrap_fields.items():
        panel = panel_by_title(dashboard=dashboard, title=title)
        total = 0
        for override in (panel.get("fieldConfig") or {}).get("overrides", []):
            field = override["matcher"]["options"]
            width = next((p["value"] for p in override["properties"] if p["id"] == "custom.width"), None)
            has_wrap = any(
                p["id"] == "custom.cellOptions" and p["value"].get("wrapText") is True for p in override["properties"]
            )
            if field in wrapped:
                assert has_wrap, f"{title}: column {field} must wrap instead of truncating"
                assert width is None, f"{title}: wrapped column {field} must take the remaining width"
                continue
            assert width is not None, f"{title}: column {field} needs a fixed width"
            total += width
        assert total <= 1150, f"{title}: column widths sum to {total}px, above the 1280 usable width"

    for title in ("Failed runs in this cohort", "Runs in this cohort"):
        panel = panel_by_title(dashboard=dashboard, title=title)
        total = sum(
            next(p["value"] for p in o["properties"] if p["id"] == "custom.width")
            for o in panel["fieldConfig"]["overrides"]
        )
        assert total <= 1150, f"{title}: column widths sum to {total}px"

    for title, field in (
        ("Exact cohorts", "test_sha"),
        ("Exact cohorts", "test_infra_sha"),
        ("Exact cohorts", "test_infra_workflow_sha"),
    ):
        panel = panel_by_title(dashboard=dashboard, title=title)
        width = next(
            p["value"]
            for o in panel["fieldConfig"]["overrides"]
            if o["matcher"]["options"] == field
            for p in o["properties"]
            if p["id"] == "custom.width"
        )
        assert width >= 350, f"{title}.{field}: {width}px is too narrow for a 40-character SHA"

    tests = panel_by_title(dashboard=dashboard, title="Tests using the most time")
    overrides = {
        o["matcher"]["options"]: {
            p["id"]: p["value"] for p in o["properties"] if p["id"] in ("displayName", "custom.width")
        }
        for o in tests["fieldConfig"]["overrides"]
    }
    assert overrides["code.function (last)"]["displayName"] == "Test"
    assert overrides["code.function (last)"]["custom.width"] >= 280
    assert overrides["pytest.nodeid"]["displayName"] == "Full test id"
    assert "custom.width" not in overrides["pytest.nodeid"]


def test_wrapped_columns_use_a_valid_cell_option() -> None:
    """Wrapping is a cell option. `custom.wrapText` is not a Grafana field option,
    so the browser ignored it and clipped long test ids."""
    dashboard = load_dashboard()
    for panel in compare_panels(dashboard):
        for override in panel.get("fieldConfig", {}).get("overrides", []):
            ids = [p["id"] for p in override["properties"]]
            assert "custom.wrapText" not in ids, f"{panel['title']}: custom.wrapText is not a field option"
    tests = panel_by_title(dashboard=dashboard, title="Tests using the most time")
    nodeid = next(o for o in tests["fieldConfig"]["overrides"] if o["matcher"]["options"] == "pytest.nodeid")
    cell = next(p["value"] for p in nodeid["properties"] if p["id"] == "custom.cellOptions")
    assert cell["wrapText"] is True
    assert cell["dynamicHeight"] is True


def test_observed_time_copy_and_no_waste_claim() -> None:
    dashboard = load_dashboard()
    ranking = panel_by_title(dashboard=dashboard, title="Tests using the most time")
    intro = panel_by_title(dashboard=dashboard, title="Pick a cohort")["options"]["content"]
    guide = panel_by_title(dashboard=dashboard, title="How to read this comparison")["options"]["content"]

    assert "observed test-span time" in ranking["description"]
    assert "setup and call time" in ranking["description"]
    assert "not runner wall time" in ranking["description"]
    assert "parallel tests overlap" in ranking["description"]
    assert "Fewer than 3 captures is an insufficient sample" in ranking["description"]
    assert "Observed time is not runner time" in intro
    assert "cannot be converted into runner minutes" in intro
    assert "not a diagnosis" in guide

    whole_section = json.dumps(compare_row(dashboard)).lower()
    assert "waste" not in whole_section
    assert "minutes saved" not in whole_section
    assert "time saved" not in whole_section


def test_limits_copy_names_the_registry_and_the_expand_only_tempo_reads() -> None:
    dashboard = load_dashboard()
    text = panel_by_title(dashboard=dashboard, title=LIMITS_TITLE)["options"]["content"]

    assert "/api/ci-observe/cohorts" in text
    assert "not Tempo" in text
    assert "collapsed" in text
    assert "six identity fields" in text
    assert "`identity_source=none`" in text
    assert "never merged" in text
    assert "`unknown` is never a cohort value" in text
    assert "both the runs shown and the total" in text
    assert "exactly one TraceQL query" in text
    assert "168 hours" in text
    assert "spans per span set exceeds 100" not in text
    assert "30,000 spansets" not in text


def test_every_test_span_query_excludes_phase_spans_and_projects_names() -> None:
    """Only parent test spans count; ::setup/::call/::teardown children are excluded.
    The four span tables project the pytest node id and code.function they group
    and display by, and select the cohort with the six identity variables."""
    dashboard = load_dashboard()
    test_span_queries = [
        q
        for panel in compare_panels(dashboard)
        for q in (t.get("query", "") for t in panel.get("targets", []))
        if '.pytest.span_type = "test"' in q
    ]
    assert len(test_span_queries) == 4  # tests, runs, failed tests, failed runs
    for query in test_span_queries:
        assert PHASE_EXCLUSION in query
        assert NODEID_PROJECTION in query
        assert FUNCTION_PROJECTION in query


def test_no_leftover_template_tokens() -> None:
    dashboard = load_dashboard()
    variables = {v["name"] for v in dashboard["templating"]["list"]}
    token = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")

    def walk(value: object) -> None:
        if isinstance(value, str):
            for match in token.findall(value):
                assert match in variables or GRAFANA_BUILTIN_VARIABLE.match(match), (
                    f"unresolved template token {match!r} in dashboard strings"
                )
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(dashboard)


def test_two_revision_delta_reads_the_compare_endpoint() -> None:
    """Q8 needs a before/after table, not only a within-cohort ranking."""
    dashboard = load_dashboard()
    panel = panel_by_title(dashboard=dashboard, title="Test deltas between two source revisions")
    (target,) = panel["targets"]
    assert target["url"] == (
        f"{API}/trends/compare"
        "?base=${compare_base:percentencode}&head=${compare_head:percentencode}&days=${days}"
    )
    assert target["root_selector"] == "rows"
    assert [column["selector"] for column in target["columns"]] == [
        "test_id",
        "workflow",
        "base_runs",
        "base_failures",
        "base_p50_duration_s",
        "head_runs",
        "head_failures",
        "head_p50_duration_s",
        "delta_failures",
        "delta_p50_s",
    ]
    assert "all CI jobs" in panel["description"]
    assert "head minus base" in panel["description"]
    variables = {v["name"] for v in dashboard["templating"]["list"]}
    assert {"compare_base", "compare_head"} <= variables


# ---------------------------------------------------------------------------
# Data path: every selector against the endpoint contract
# ---------------------------------------------------------------------------
COHORTS_DOCUMENT = {
    "days": 30,
    "window_from": "2026-10-05T10:00:00Z",
    "window_to": "2026-11-04T10:00:00Z",
    "cohorts": [{
        "test_sha": "a" * 40,
        "test_infra_sha": "b" * 40,
        "test_infra_workflow_sha": "c" * 40,
        "test_suite": "vmaas",
        "test_filter": "none",
        "test_marker": "none",
        "test_ref": "main",
        "captures": 3,
    }, {
        "test_sha": "d" * 40,
        "test_infra_sha": "e" * 40,
        "test_infra_workflow_sha": "f" * 40,
        "test_suite": "caas",
        "test_filter": "none",
        "test_marker": "none",
        "test_ref": "main",
        "captures": 1,
    }],
    "ungrouped": [{
        "capture_id": "vmaas-main-41535773459-a1",
        "run_id": "41535773459",
        "run_attempt": "1",
        "suite": "vmaas",
        "test_ref": "main",
        "identity_source": "none",
        "capture_started_at": "2026-11-03T10:00:00Z",
        "last_seen_at": "2026-11-04T09:00:00Z",
        "capture_window_from": "2026-11-03T10:00:00Z",
        "capture_window_to": "2026-11-04T09:00:00Z",
        "capture_window_from_ms": 1762164000000,
        "capture_window_to_ms": 1762246800000,
        "reason": "the registry has no osac.ci.test_infra_sha for this capture",
    }],
    "truncated": False,
    "shown": 2,
    "total": 2,
    "notes": [],
}


def test_every_exact_cohort_column_resolves_against_the_cohorts_contract() -> None:
    panel = panel_by_title(dashboard=load_dashboard(), title="Exact cohorts")
    rows = resolve_rows(COHORTS_DOCUMENT, panel["targets"][0]["root_selector"])
    assert rows, "the cohort contract has no rows"
    for column in panel["targets"][0]["columns"]:
        assert column["selector"] in rows[0], f"column {column['selector']} is not in the contract"


def test_every_ungrouped_column_resolves_against_the_cohorts_contract() -> None:
    panel = panel_by_title(dashboard=load_dashboard(), title=UNGROUPED_TITLE)
    rows = resolve_rows(COHORTS_DOCUMENT, panel["targets"][0]["root_selector"])
    assert rows, "the ungrouped contract has no rows"
    for column in panel["targets"][0]["columns"]:
        assert column["selector"] in rows[0], f"column {column['selector']} is not in the contract"


def test_every_sample_status_column_resolves_against_the_cohorts_contract() -> None:
    panel = panel_by_title(dashboard=load_dashboard(), title="Sample status")
    rows = resolve_rows(COHORTS_DOCUMENT, panel["targets"][0]["root_selector"])
    for column in panel["targets"][0]["columns"]:
        assert column["selector"] in rows[0], f"column {column['selector']} is not in the contract"


def test_the_cohort_contract_keeps_grouped_and_ungrouped_apart() -> None:
    """Exact cohorts never contain an identity_source value, and the ungrouped
    list never appears in the exact list. `unknown` is not an identity value."""
    cohorts = COHORTS_DOCUMENT["cohorts"]
    ungrouped = COHORTS_DOCUMENT["ungrouped"]
    assert cohorts and ungrouped
    for row in cohorts:
        assert "identity_source" not in row
        assert "capture_id" not in row
        assert all(row[field] != "unknown" for field in IDENTITY_FIELDS)
        assert SHA_RE.search(row["test_sha"])
    assert {row["identity_source"] for row in ungrouped} == {"none"}
    assert len({row["capture_id"] for row in ungrouped}) == len(ungrouped)
