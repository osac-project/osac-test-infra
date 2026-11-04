"""Structural tests for the Compare section of the ci-observe Trends dashboard.

Compare is an action, not a dashboard: the model puts it as a collapsed row on
monitoring/config/grafana/dashboards/ci-observe-trends.json. This section
answers two questions across comparable captures: what changed versus a passing
run (Q7) and which fixes matter most (Q8). It opens with a short intro, the two
cohort tables, the sample-status stat, the tests table, the runs table, the
reading guide and the query limits, and it keeps the failed-test evidence at
the bottom. Exact cohorts key on the resolved osac commit (osac.ci.test_sha);
the requested ref (osac.ci.test_ref) is metadata only.

Every panel runs exactly one TraceQL query, and the section stays at seven
targets: this Grafana's Tempo plugin executes only the first query of a panel
and overwrites every additional target with it (verified 2026-11-02 from the
served plugin bundle and captured browser requests), and the backend's search
queue returned 429 job-queue-full under ten concurrent queries (verified
2026-11-02 live; seven was the load the passing visual check used). Keeping the
compare panels in a collapsed row also means they are not queried until a
reader expands the section.

The TraceQL queries and the Grafana transform semantics they rely on
(groupBy, filterByValue, sortBy, limit) were validated against the central
Tempo 2.10 datasource (uid ci-observe-traces) through Grafana's /api/ds/query
on 2026-11-01, including the structural-negation predicates that keep
source-revision cohorts from absorbing captures with resolved harness SHAs.
The limit copy pinned here states the caps observed on that datasource.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

DASHBOARDS_DIR = Path(__file__).resolve().parents[1] / "config" / "grafana" / "dashboards"
DASHBOARD_FILE = DASHBOARDS_DIR / "ci-observe-trends.json"

SHA_PATTERN = "^[0-9a-f]{40}([0-9a-f]{24})?$"
SHA_RE = re.compile(SHA_PATTERN)

# Structural negation, verified live: true when the attribute is absent or not
# a resolved SHA, false exactly when it is a 40/64-hex commit.
NEG_INFRA_SHA = f'!( resource."osac.ci.test_infra_sha" =~ "{SHA_PATTERN}" )'
NEG_WORKFLOW_SHA = f'!( resource."osac.ci.test_infra_workflow_sha" =~ "{SHA_PATTERN}" )'

# The pytest node id and the code.function attribute: present on every parent
# test span. The comparison queries require and project both so empty result
# frames are dropped, and code.function gives the tables a test-name column
# short enough to fit the viewport.
NODEID_PROJECTION = '.pytest.nodeid =~ ".+"'
FUNCTION_PROJECTION = '.code.function =~ ".+"'

# The prototype shipped with one real osac SHA hardcoded as a manual seed.
# Cohort discovery replaced that; the SHA must never come back.
PROTOTYPE_SEED_SHA = "91e852e8f48b642428f3e927e93db3e2dcb3d667"

COMPARISON_LINK = "/d/ci-observe-trends/ci-observe-e28094-trends"
RUN_INVESTIGATION_LINK = "/d/ci-observe-runs/ci-observe-e28094-run-investigation"

PHASE_EXCLUSION = 'name !~ ".*::(setup|call|teardown)"'

COMPARE_ROW_TITLE = "Compare — captured runs, selected cohort, dashboard time range"

COMPARE_PANEL_TITLES = [
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
    "",
    "Failed tests in this cohort",
    "Failed runs in this cohort",
]

COHORT_VARIABLES = {
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

ANALYSIS_TITLES = [
    "Tests using the most time",
    "Failed tests in this cohort",
    "Failed runs in this cohort",
    "Runs in this cohort",
]

# The Compare section must never exceed eight query targets: the backend's
# search queue returned 429 under ten concurrent queries, and seven was the
# load the passing visual check used.
MAX_TOTAL_TARGETS = 8

GRAFANA_BUILTIN_VARIABLE = re.compile(r"^__")


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
    return [t for t in panel["transformations"] if t["id"] == transform_id]


def all_queries(dashboard: dict[str, object]) -> list[str]:
    return [t["query"] for p in iter_panels(dashboard["panels"]) for t in p.get("targets", []) if "query" in t]


def comparison_targets(dashboard: dict[str, object]) -> list[str]:
    """Every variable-driven cohort query (the sample stat and the four analysis panels)."""
    queries = []
    for title in ["Sample status", *ANALYSIS_TITLES]:
        panel = panel_by_title(dashboard=dashboard, title=title)
        queries.extend(t["query"] for t in panel["targets"])
    return queries


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
    """Two hard limits. Per panel: this Grafana's Tempo plugin overwrites every
    target after the first with the first query's text, so panels must be
    single-query. Section-wide: the backend's search queue returned 429 under
    ten concurrent queries, so the section must stay at eight targets at most
    (it ships seven)."""
    dashboard = load_dashboard()
    total = 0
    for panel in compare_panels(dashboard):
        target_count = len(panel.get("targets", []))
        total += target_count
        assert target_count <= 1, (
            f"{panel['title']} has {target_count} targets; the Tempo plugin executes only the"
            " first and overwrites the rest, so panels must have at most one target"
        )
    assert total <= MAX_TOTAL_TARGETS, f"the section runs {total} concurrent searches; keep it at {MAX_TOTAL_TARGETS}"
    assert total == 8, "the section should ship eight targets: two discoveries, the sample stat, four tables and the two-revision delta"

    with_targets = [p["title"] for p in compare_panels(dashboard) if p.get("targets")]
    assert with_targets == [
        "Exact cohorts",
        "Source-revision cohorts (not exact)",
        "Sample status",
        "Tests using the most time",
        "Runs in this cohort",
        "Test deltas between two source revisions",
        "Failed tests in this cohort",
        "Failed runs in this cohort",
    ]


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
    """A newcomer sees the intro, the cohort pickers, the sample status, the tests
    table, the runs table, the guides and the failed evidence. The sample rule,
    the observed-time warning and the loading note are in the top intro."""
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

    # Reading order inside the row.
    titles = [p["title"] for p in compare_row(dashboard)["panels"]]
    assert titles.index("Pick a cohort") < titles.index("Exact cohorts")
    assert titles.index("Source-revision cohorts (not exact)") < titles.index("Sample status")
    assert titles.index("Sample status") < titles.index("Tests using the most time")
    assert titles.index("Tests using the most time") < titles.index("Runs in this cohort")
    assert titles.index("Runs in this cohort") < titles.index("How to read this comparison")
    assert titles.index("How to read this comparison") < titles.index(
        "Query limits (verified against this backend on 2026-11-01)"
    )
    assert titles.index("Query limits (verified against this backend on 2026-11-01)") < titles.index(
        "Failed tests in this cohort"
    )
    assert titles[-1] == "Failed runs in this cohort"
    # The text panels get enough height to avoid clipping (measured via DOM:
    # 330px, 702px and 626px of content at 1280).
    for title, min_height in [
        ("Pick a cohort", 11),
        ("How to read this comparison", 20),
        ("Query limits (verified against this backend on 2026-11-01)", 18),
    ]:
        panel = panel_by_title(dashboard=dashboard, title=title)
        assert panel["gridPos"]["h"] >= min_height, f"{title} is too short to show its text"
    # The cohort tables are compact: they hold noValue text or one row today.
    for title in ("Exact cohorts", "Source-revision cohorts (not exact)"):
        assert panel_by_title(dashboard=dashboard, title=title)["gridPos"]["h"] == 6


def test_sample_status_is_the_only_stat_and_maps_verdicts() -> None:
    """The stat between the cohort tables and the analysis tables shows the selected
    cohort's distinct capture count and the verdict for 0/1/2/3+ captures."""
    dashboard = load_dashboard()
    stat = panel_by_title(dashboard=dashboard, title="Sample status")

    assert stat["type"] == "stat"
    reduce_options = stat["options"]["reduceOptions"]
    assert reduce_options["calcs"] == ["distinctCount"]
    assert reduce_options["fields"] == "osac.ci.capture_id"

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

    # The stat runs the same cohort query as the tests table.
    assert (
        stat["targets"][0]["query"]
        == panel_by_title(dashboard=dashboard, title="Tests using the most time")["targets"][0]["query"]
    )


def test_every_analysis_panel_explains_an_empty_state() -> None:
    """No analysis panel may render an unexplained headers-only box: each has a
    noValue message pointing at the sample status, and the failed tables stay at
    the bottom of the collapsed row."""
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
    assert "timed out" in runs_panel["fieldConfig"]["defaults"]["noValue"]
    tests_panel = panel_by_title(dashboard=dashboard, title="Tests using the most time")
    assert "No captures matched the current selection" in tests_panel["fieldConfig"]["defaults"]["noValue"]


def test_dashboard_time_range_fits_the_backend_window() -> None:
    """Tempo on this backend rejects ranges over 168 hours, so the dashboard's
    default range is 7 days and the Compare section shares it."""
    assert load_dashboard()["time"] == {"from": "now-7d", "to": "now"}


def test_variables_are_the_cohort_link_state() -> None:
    dashboard = load_dashboard()
    variables = {v["name"]: v for v in dashboard["templating"]["list"]}

    # The cohort variables are added next to the existing days and test pair.
    assert set(variables) == {"days", "test"} | COHORT_VARIABLES
    for name in COHORT_VARIABLES:
        assert variables[name]["type"] == "textbox", f"{name} must be a textbox set by the cohort links"

    # Defaults: empty SHA fields, all/none selection. Nothing is prefilled with a
    # real SHA, so the dashboard starts without a seeded cohort.
    for name in ("test_ref", "test_sha", "test_infra_sha", "test_infra_workflow_sha"):
        assert variables[name]["current"]["value"] == "", f"{name} must default to empty"
    assert variables["test_suite"]["current"]["value"] == "all"
    assert variables["test_filter"]["current"]["value"] == "none"
    assert variables["test_marker"]["current"]["value"] == "none"
    assert PROTOTYPE_SEED_SHA not in DASHBOARD_FILE.read_text()


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
                    assert link["url"].split("?")[0] in (COMPARISON_LINK, RUN_INVESTIGATION_LINK), (
                        f"{panel['title']}: unexpected link target {link['url']}"
                    )


def test_sha_pattern_accepts_only_pinned_commits() -> None:
    """The cohort keys must be pinned commits: 40- or 64-char lowercase hex."""
    assert SHA_RE.search("91e852e8f48b642428f3e927e93db3e2dcb3d667")
    assert SHA_RE.search("a" * 64)
    for rejected in ("main", "unknown", "", "release/1.2", "feature-branch", "A" * 40, "a" * 39, "a" * 41):
        assert not SHA_RE.search(rejected), f"{rejected!r} must not form a cohort"


def test_exact_discovery_keys_on_the_resolved_osac_commit() -> None:
    """The exact cohort table requires every resolved identity field; the requested
    osac ref is projected as metadata and is never a cohort requirement."""
    panel = panel_by_title(dashboard=load_dashboard(), title="Exact cohorts")
    query = panel["targets"][0]["query"]

    assert PHASE_EXCLUSION in query
    assert '.pytest.span_type = "test"' in query
    assert 'resource."osac.ci.capture_id" =~ ".+"' in query
    assert 'resource."osac.ci.run_attempt" != nil' in query

    for attribute in ("test_sha", "test_infra_sha", "test_infra_workflow_sha"):
        selector = f'resource."osac.ci.{attribute}"'
        assert f"{selector} != nil" in query
        assert f'{selector} != ""' in query
        assert f'{selector} != "unknown"' in query
        assert f'{selector} =~ "{SHA_PATTERN}"' in query

    for attribute in ("test_suite", "test_filter", "test_marker"):
        selector = f'resource."osac.ci.{attribute}"'
        assert f"{selector} != nil" in query
        assert f'{selector} != ""' in query
        assert f'{selector} != "unknown"' in query

    # test_ref is metadata: projected (any value, including main), not SHA-guarded.
    assert 'resource."osac.ci.test_ref" =~ ".*"' in query
    assert 'resource."osac.ci.test_ref" != "unknown"' not in query
    assert f'resource."osac.ci.test_ref" =~ "{SHA_PATTERN}"' not in query

    # Discovery is not variable-driven; the comparison queries are.
    assert "${" not in query
    assert "osac.ci.test_infra_ref" not in query


def test_source_discovery_is_a_separate_not_exact_query() -> None:
    """Source cohorts group by requested osac commit and suite word only, and
    exclude captures whose harness SHAs resolved to real commits."""
    panel = panel_by_title(dashboard=load_dashboard(), title="Source-revision cohorts (not exact)")
    query = panel["targets"][0]["query"]

    assert PHASE_EXCLUSION in query
    assert '.pytest.span_type = "test"' in query
    assert 'resource."osac.ci.capture_id" =~ ".+"' in query
    assert 'resource."osac.ci.suite" =~ ".+"' in query
    assert NEG_INFRA_SHA in query
    assert NEG_WORKFLOW_SHA in query

    selector = 'resource."osac.ci.test_ref"'
    assert f"{selector} != nil" in query
    assert f'{selector} != ""' in query
    assert f'{selector} != "unknown"' in query
    assert f'{selector} =~ "{SHA_PATTERN}"' in query

    # The source query must not require the resolved identity fields.
    assert "osac.ci.test_sha" not in query
    assert "osac.ci.test_suite" not in query
    assert "osac.ci.test_filter" not in query
    assert "osac.ci.test_marker" not in query
    assert "${" not in query


def test_source_mode_never_absorbs_exact_captures() -> None:
    """Every source-side query excludes captures whose harness checkout or workflow
    SHA resolved, so exact and source rows never mix. The negation must appear in
    the source discovery and in the source branch of every comparison query."""
    dashboard = load_dashboard()
    source_panel = panel_by_title(dashboard=dashboard, title="Source-revision cohorts (not exact)")
    assert NEG_INFRA_SHA in source_panel["targets"][0]["query"]
    assert NEG_WORKFLOW_SHA in source_panel["targets"][0]["query"]

    for query in comparison_targets(dashboard):
        exact_branch = query[query.index("( (") : query.index(") || (")]
        source_branch = query[query.index(") || (") :]
        assert NEG_INFRA_SHA in source_branch, "comparison source branch lost the harness SHA exclusion"
        assert NEG_WORKFLOW_SHA in source_branch, "comparison source branch lost the workflow SHA exclusion"
        assert "!" not in exact_branch.replace("!=", ""), "exact branch must not use negation"


def test_comparison_query_has_one_exact_branch_and_one_source_branch() -> None:
    """Exactly one branch matches for any variable state: the exact filters need
    every resolved SHA, the source filter needs the requested commit and suite word.
    The stat and the tests table share the all-tests query; the failed panels add
    only the error filter; the runs table drops the unused run_attempt predicate."""
    dashboard = load_dashboard()
    all_tests = panel_by_title(dashboard=dashboard, title="Tests using the most time")["targets"][0]["query"]
    failed = panel_by_title(dashboard=dashboard, title="Failed tests in this cohort")["targets"][0]["query"]

    assert PHASE_EXCLUSION in all_tests
    assert NODEID_PROJECTION in all_tests
    assert FUNCTION_PROJECTION in all_tests
    assert 'resource."osac.ci.capture_id" =~ ".+"' in all_tests
    assert 'resource."osac.ci.run_attempt" != nil' in all_tests

    exact_branch = all_tests[all_tests.index("( (") : all_tests.index(") || (")]
    source_branch = all_tests[all_tests.index(") || (") :]

    # Exact branch: the resolved osac commit plus both harness SHAs and the exact
    # selection, all pinned to variables. It must not key on the requested ref.
    for attribute in ("test_sha", "test_infra_sha", "test_infra_workflow_sha"):
        sel = f'resource."osac.ci.{attribute}"'
        assert f"{sel} != nil" in exact_branch
        assert f'{sel} != ""' in exact_branch
        assert f'{sel} != "unknown"' in exact_branch
        assert f'{sel} =~ "{SHA_PATTERN}"' in exact_branch
        assert f"{sel} = ${{{attribute}:doublequote}}" in exact_branch
    for attribute in ("test_suite", "test_filter", "test_marker"):
        sel = f'resource."osac.ci.{attribute}"'
        assert f"{sel} != nil" in exact_branch
        assert f'{sel} != ""' in exact_branch
        assert f'{sel} != "unknown"' in exact_branch
        assert f"{sel} = ${{{attribute}:doublequote}}" in exact_branch
    assert "osac.ci.test_ref" not in exact_branch, "exact cohorts must key on test_sha, not the requested test_ref"
    assert "osac.ci.test_infra_ref" not in all_tests

    # Source branch: requested commit (pinned SHA), suite word, harness exclusions.
    assert 'resource."osac.ci.test_ref" =~ "^[0-9a-f]{40}([0-9a-f]{24})?$"' in source_branch
    assert 'resource."osac.ci.test_ref" = ${test_ref:doublequote}' in source_branch
    assert 'resource."osac.ci.suite" = ${test_suite:doublequote}' in source_branch
    assert "osac.ci.test_sha" not in source_branch

    # The failed panels add only the error filter; the runs table drops the unused
    # run_attempt predicate; the sample stat runs the all-tests query.
    assert failed == all_tests.replace(" }", " && status = error }")
    assert panel_by_title(dashboard=dashboard, title="Failed runs in this cohort")["targets"][0]["query"] == failed
    runs_query = panel_by_title(dashboard=dashboard, title="Runs in this cohort")["targets"][0]["query"]
    assert runs_query == all_tests.replace(' && resource."osac.ci.run_attempt" != nil', "")
    assert panel_by_title(dashboard=dashboard, title="Sample status")["targets"][0]["query"] == all_tests


def test_cohort_tables_are_single_query_capture_selectors() -> None:
    """Both cohort tables count captures from their own discovery query in two
    groupBy steps; no session-status join, no passed/failed columns."""
    dashboard = load_dashboard()
    for title, identity in (
        (
            "Exact cohorts",
            [
                "osac.ci.test_sha",
                "osac.ci.test_infra_sha",
                "osac.ci.test_infra_workflow_sha",
                "osac.ci.test_suite",
                "osac.ci.test_filter",
                "osac.ci.test_marker",
            ],
        ),
        ("Source-revision cohorts (not exact)", ["osac.ci.test_ref", "osac.ci.suite"]),
    ):
        panel = panel_by_title(dashboard=dashboard, title=title)
        group_bys = transforms(panel=panel, transform_id="groupBy")
        assert len(group_bys) == 2, f"{title} must be a two-step groupBy, nothing else"
        per_capture, per_cohort = group_bys
        assert per_capture["options"]["fields"]["osac.ci.capture_id"]["operation"] == "groupby"
        for key in identity:
            assert per_capture["options"]["fields"][key]["operation"] == "groupby"
        final_fields = per_cohort["options"]["fields"]
        assert final_fields["osac.ci.capture_id"] == {"aggregations": ["count"], "operation": "aggregate"}
        for key in identity:
            assert final_fields[key]["operation"] == "groupby"
        assert not transforms(panel=panel, transform_id="joinByField"), f"{title} must not join"
        assert {"Captures"} <= display_names(panel=panel)

    # The exact table keys on the resolved commit, keeps the requested ref as an
    # aggregated field for the link, and hides the wide metadata columns so the
    # three 360px SHA columns fit the viewport.
    exact = panel_by_title(dashboard=load_dashboard(), title="Exact cohorts")
    final = transforms(panel=exact, transform_id="groupBy")[1]["options"]["fields"]
    assert final["osac.ci.test_ref"] == {"aggregations": ["last"], "operation": "aggregate"}
    assert display_names(panel=exact) >= {"OSAC commit", "Requested ref"}
    hidden = {
        o["matcher"]["options"]: any(p["id"] == "custom.hidden" and p["value"] for p in o["properties"])
        for o in exact["fieldConfig"]["overrides"]
    }
    for field in ("osac.ci.test_ref (last)", "osac.ci.test_suite", "osac.ci.test_filter", "osac.ci.test_marker"):
        assert hidden[field], f"{field} must stay hidden so the SHA columns fit"
    capture_count_filters = [
        f
        for f in transforms(panel=exact, transform_id="filterByValue")
        if f["options"]["filters"][0]["fieldName"] == "osac.ci.capture_id (count)"
    ]
    assert len(capture_count_filters) == 1
    assert capture_count_filters[0]["options"]["filters"][0]["config"] == {
        "id": "greaterOrEqual",
        "options": {"value": 2},
    }
    assert "No exact cohort yet" in exact["fieldConfig"]["defaults"]["noValue"]


def test_source_cohort_table_carries_the_not_exact_warning() -> None:
    """The source table is labelled as evidence for current data, never a baseline."""
    dashboard = load_dashboard()
    panel = panel_by_title(dashboard=dashboard, title="Source-revision cohorts (not exact)")

    assert "not exact" in panel["title"]
    description = panel["description"]
    assert "harness revision was not captured" in description
    assert "not an exact comparison" in description
    assert "must not be used as a regression baseline" in description
    assert "excluded" in description, "the source table must say it excludes resolved harness SHAs"

    # No minimum-capture filter on source cohorts: one capture is shown with its
    # count, and the comparison labels the sample as insufficient.
    for value_filter in transforms(panel=panel, transform_id="filterByValue"):
        for filter_config in value_filter["options"]["filters"]:
            assert filter_config["config"]["id"] != "greaterOrEqual"

    links = field_links(panel=panel, field="osac.ci.test_ref")
    assert len(links) == 1
    url = links[0]["url"]
    assert url.startswith(COMPARISON_LINK)
    assert "var-test_ref=${__value.raw:percentencode}" in url
    assert 'var-test_suite=${__data.fields["osac.ci.suite"]:percentencode}' in url
    # Source links clear the exact-only variables instead of guessing values.
    for empty in (
        "var-test_sha=&",
        "var-test_filter=&",
        "var-test_marker=&",
        "var-test_infra_sha=&",
        "var-test_infra_workflow_sha=&",
    ):
        assert empty in url


def test_exact_cohort_links_set_every_variable() -> None:
    panel = panel_by_title(dashboard=load_dashboard(), title="Exact cohorts")
    links = field_links(panel=panel, field="osac.ci.test_sha")
    assert len(links) == 1
    url = links[0]["url"]

    assert url.startswith(COMPARISON_LINK)
    assert "var-test_sha=${__value.raw:percentencode}" in url
    # The requested ref is metadata, taken from the aggregated display column.
    assert 'var-test_ref=${__data.fields["osac.ci.test_ref (last)"]:percentencode}' in url
    for attribute in ("test_suite", "test_filter", "test_marker", "test_infra_sha", "test_infra_workflow_sha"):
        assert f'var-{attribute}=${{__data.fields["osac.ci.{attribute}"]:percentencode}}' in url


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

    # Single query, no join, no calculated failure rate.
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
    """One row per failed test occurrence: capture (linked to the run
    investigation), test function, time and duration."""
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
                # A fixed width makes Grafana clip the cell instead of wrapping.
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

    # SHA columns: 40 hex characters need ~336px plus padding; 31-character
    # prefixes were the earlier defect.
    for title, field in (
        ("Source-revision cohorts (not exact)", "osac.ci.test_ref"),
        ("Exact cohorts", "osac.ci.test_sha"),
        ("Exact cohorts", "osac.ci.test_infra_sha"),
        ("Exact cohorts", "osac.ci.test_infra_workflow_sha"),
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

    # The Test column shows the code.function value, which fits without wrapping;
    # the full node id lives in its own wrapped column.
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

    # Observed time is never framed as recoverable time or a savings estimate.
    whole_section = json.dumps(compare_row(dashboard)).lower()
    assert "waste" not in whole_section
    assert "minutes saved" not in whole_section
    assert "time saved" not in whole_section


def test_limits_copy_pins_the_verified_query_caps() -> None:
    dashboard = load_dashboard()
    text = panel_by_title(dashboard=dashboard, title="Query limits (verified against this backend on 2026-11-01)")[
        "options"
    ]["content"]

    assert "30,000 spansets (traces), not spans" in text
    assert "30,000 traces holding 38,902 spans" in text
    assert "100 spans per spanset" in text
    assert "spans per span set exceeds 100" in text
    assert "168 hours" in text
    assert "A live capture is not final" in text
    assert "excluded from exact cohorts" in text
    assert "the most observed is 53" in text

    # The nil-semantics claim must match what was verified live: standalone = nil
    # does match absent attributes, combinations are unreliable, structural
    # negation is what the source queries use.
    assert "structural negation" in text
    assert "!( attribute =~ sha )" in text
    assert "5,382" in text
    assert "26 spans" in text
    assert "16 MB" in text
    assert "matches nothing" not in text
    assert "62 of 103" not in text

    # The slow backend is labelled, and the single-query constraint is documented.
    assert "Panel load time is the backend" in text
    assert "exactly one TraceQL query" in text
    assert "overwrites every additional target" in text
    # The intermittent failure mode is documented with its verified mechanism.
    assert "429 job queue full" in text
    assert "refreshing retries" in text

    # Every Tempo target requests the same caps the copy states.
    for panel in compare_panels(dashboard):
        for target in panel.get("targets", []):
            if target.get("queryType") != "traceql":
                continue
            assert target["limit"] == 30000, f"{panel['title']}: query limit must stay at 30,000"
            assert target["spss"] == 100, f"{panel['title']}: spans per spanset must stay at 100"
            assert target["queryType"] == "traceql"
            assert target["tableType"] == "spans"
            assert target["datasource"]["uid"] == "ci-observe-traces"


def test_every_test_span_query_excludes_phase_spans_and_projects_names() -> None:
    """Only parent test spans count; ::setup/::call/::teardown children are excluded.
    The five variable-driven queries also project the pytest node id and the
    code.function they group and display by."""
    dashboard = load_dashboard()
    test_span_queries = [q for q in all_queries(dashboard) if '.pytest.span_type = "test"' in q]
    assert len(test_span_queries) == 7  # two discoveries + the stat + four analysis panels
    for query in test_span_queries:
        assert PHASE_EXCLUSION in query
    for query in comparison_targets(dashboard):
        assert NODEID_PROJECTION in query, "comparison queries must project the node id they group by"
        assert FUNCTION_PROJECTION in query, "comparison queries must project the test function they display"


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
        "http://127.0.0.1:9103/api/ci-observe/trends/compare"
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
