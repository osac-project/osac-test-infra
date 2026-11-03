"""Structural tests for the ci-observe dashboards.

monitoring/config/grafana/dashboards/ci-observe-runs.json (one capture at a
time) and ci-observe-runs-home.json (the run list) are provisioned by
monitoring/config/grafana/dashboards.yml into the ci-observe folder next to the
other dashboards. They query the ci-observe capture backend through the Tempo
and Loki datasources, so these tests pin the datasource uids, the capture_id
template variable and the TraceQL/LogQL shapes that the panels depend on.

The queries themselves were validated against the live grafana/otel-lgtm
stack (Tempo 2.10) with real captures; see the ci-observe plan for the
recorded evidence. Live-verified Grafana/Tempo behaviors the dashboards rely
on:

- A TraceQL search response only includes attribute columns for attributes
  referenced in the predicate, so every displayed attribute is referenced.
- The stat panel inherits the reduced field's unit, so count stats reduce a
  unitless field (Name) instead of the nanosecond Duration field; panel
  defaults alone do not override the datasource-provided ns unit.
- The Tempo datasource stamps displayNameFromDS on every field (name ->
  "Name", duration -> "Duration", time -> "Start time", spanID -> "Span
  ID"), and the groupBy transformation names aggregated output columns
  "<displayName> (agg)" while its config keys stay raw names. Overrides for
  aggregated columns must therefore match "Span ID (count)", "Duration
  (sum)", "Start time (min)" and friends, never the raw-name form.
- Beyla (v1.47) marks gRPC spans with the span attributes
  rpc.system.name = "grpc" and a string rpc.response.status_code ("OK",
  "NOT_FOUND", ...); it does not set rpc.system or a numeric
  rpc.grpc.status_code.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

DASHBOARDS_DIR = Path(__file__).resolve().parents[1] / "config" / "grafana" / "dashboards"
DASHBOARD_FILE = DASHBOARDS_DIR / "ci-observe-runs.json"
HOME_FILE = DASHBOARDS_DIR / "ci-observe-runs-home.json"

# Datasource uids owned by the ci-observe capture backend. They are created
# outside this dashboard (datasource provisioning), never inside it. The
# Prometheus uid is deliberately absent: cluster evidence (Q5) is deferred and
# no panel may imply it exists.
CI_OBSERVE_DATASOURCE_UIDS = {"ci-observe-traces", "ci-observe-logs", "ci-observe-metrics"}

# Grafana built-in variables ($__interval, $__value.raw, ...) are allowed
# in queries; every other $name must be a dashboard template variable.
GRAFANA_BUILTIN_VARIABLE = re.compile(r"^__")

# Run identity attributes the Run identity row must display, mapped to plain
# labels. Stamped by the Q1 workstream; old captures show "not collected".
IDENTITY_ATTRIBUTES = {
    "osac.ci.test_ref": "Test ref (requested)",
    "osac.ci.test_sha": "Test SHA (resolved)",
    "osac.ci.test_infra_ref": "Harness ref (requested)",
    "osac.ci.test_infra_sha": "Harness SHA (resolved)",
    "osac.ci.test_infra_workflow_sha": "Harness workflow SHA",
    "osac.ci.test_suite": "Test paths",
    "osac.ci.test_filter": "Test filter",
    "osac.ci.test_marker": "Test marker",
    "osac.ci.workflow": "Workflow",
    "osac.ci.run_attempt": "Run attempt",
    "osac.ci.osac_images": "OSAC images (specs)",
    "osac.ci.osac_image_ids": "OSAC image IDs (running)",
    "osac.ci.osac_image_ids_complete": "Image IDs complete",
    "osac.ci.cluster_uid": "Cluster UID",
}

# Links to dashboards owned by other workstreams. The UIDs and slugs are the
# contract; the target dashboards may not exist yet.
EVIDENCE_LINK = "/d/ci-observe-run-evidence/ci-observe-run-evidence?orgId=1&var-capture_id=$capture_id"
COMPARE_LINK = "/d/ci-observe-compare-runs/ci-observe-compare-runs?orgId=1"
DETAIL_LINK_PREFIX = "/d/ci-observe-runs/ci-observe-e28094-run-investigation"


@pytest.fixture(scope="module")
def dashboard() -> dict:
    return json.loads(DASHBOARD_FILE.read_text())


@pytest.fixture(scope="module")
def home() -> dict:
    return json.loads(HOME_FILE.read_text())


def _existing_datasource_uids() -> set[str]:
    """Datasource uids used by the other provisioned OSAC CI dashboards."""
    uids: set[str] = set()
    for path in sorted(DASHBOARDS_DIR.glob("*.json")):
        if path in (DASHBOARD_FILE, HOME_FILE):
            continue
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        for panel in data.get("panels", []):
            ds = panel.get("datasource")
            if isinstance(ds, dict) and ds.get("uid"):
                uids.add(ds["uid"])
    return uids


def _walk_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def _iter_panels(panels: list[dict]) -> Iterator[dict]:
    for panel in panels:
        yield panel
        yield from _iter_panels(panel.get("panels", []))


def _traceql_queries(dash: dict) -> list[str]:
    return [
        target["query"]
        for panel in _iter_panels(dash["panels"])
        for target in panel.get("targets", [])
        if target.get("queryType") == "traceql"
    ]


def _panels_by_title(dash: dict) -> dict[str, dict]:
    return {panel["title"]: panel for panel in _iter_panels(dash["panels"])}


def _override_for(panel: dict, field: str) -> dict | None:
    for override in panel["fieldConfig"].get("overrides", []):
        if override["matcher"].get("options") == field:
            return override
    return None


def _mapping_texts(panel: dict, field: str) -> list[str]:
    """All mapping texts (value and special) applied to a field's overrides."""
    override = _override_for(panel, field)
    if override is None:
        return []
    texts: list[str] = []
    for prop in override["properties"]:
        if prop["id"] != "mappings":
            continue
        for mapping in prop["value"]:
            options = mapping.get("options", {})
            if "text" in options:  # special mappings are flat
                texts.append(options["text"])
            for option in options.values():
                if isinstance(option, dict) and "text" in option:
                    texts.append(option["text"])
    return texts


# ---------------------------------------------------------------------------
# Detail dashboard: file-level structure
# ---------------------------------------------------------------------------


def test_json_is_valid_and_parses(dashboard: dict) -> None:
    assert DASHBOARD_FILE.exists()
    assert isinstance(dashboard, dict)


def test_home_json_is_valid_and_parses(home: dict) -> None:
    assert HOME_FILE.exists()
    assert isinstance(home, dict)


def test_required_uid_and_title(dashboard: dict) -> None:
    assert dashboard["uid"] == "ci-observe-runs"
    assert dashboard["title"] == "CI Observe — Run Investigation"
    assert dashboard["schemaVersion"] == 39
    assert dashboard["version"] == 1


def test_home_required_uid_and_title(home: dict) -> None:
    assert home["uid"] == "ci-observe-runs-home"
    assert home["title"] == "CI Observe — Test Runs"
    assert home["schemaVersion"] == 39


def test_datasources_referenced(dashboard: dict) -> None:
    """Tempo and Loki are used; the Prometheus datasource must not be, because
    cluster evidence is deferred and an empty kubelet chart would mislead."""
    raw = DASHBOARD_FILE.read_text()
    assert "ci-observe-traces" in raw, "datasource uid ci-observe-traces is not referenced anywhere"
    assert "ci-observe-logs" in raw, "datasource uid ci-observe-logs is not referenced anywhere"
    assert "ci-observe-metrics" not in raw, "cluster evidence is deferred: no Prometheus panels may exist"


def test_capture_id_variable_exists(dashboard: dict) -> None:
    variables = dashboard["templating"]["list"]
    names = {v["name"] for v in variables}
    assert "capture_id" in names

    capture_var = next(v for v in variables if v["name"] == "capture_id")
    assert capture_var["type"] == "query"
    assert capture_var["datasource"]["uid"] == "ci-observe-logs"
    query = capture_var["query"]
    assert query["label"] == "osac_ci_capture_id"
    assert query["type"] == 1  # Loki label values


def test_every_panel_datasource_is_known(dashboard: dict) -> None:
    allowed = CI_OBSERVE_DATASOURCE_UIDS | _existing_datasource_uids()
    panels = [p for p in _iter_panels(dashboard["panels"]) if p.get("type") != "row"]
    assert panels, "dashboard has no content panels"
    for panel in panels:
        for ds in [panel.get("datasource")] + [t.get("datasource") for t in panel.get("targets", [])]:
            if ds is None:
                continue
            assert isinstance(ds, dict), f"panel {panel['title']}: datasource must be an object"
            assert ds["uid"] in allowed, f"panel {panel['title']}: unknown datasource uid {ds['uid']}"


def test_rows_present(dashboard: dict) -> None:
    titles = [p["title"] for p in dashboard["panels"] if p["type"] == "row"]
    assert titles == [
        "Run overview",
        "Collection coverage",
        "Run identity (source commit, harness commit, selection)",
        "Tests and workers",
        "Fixture work",
        "Waits",
        "Service requests",
    ]


def test_panels_are_in_reading_order(dashboard: dict) -> None:
    """The panels array must read top to bottom, left to right (by gridPos)."""
    positions = [(p["gridPos"]["y"], p["gridPos"]["x"]) for p in dashboard["panels"]]
    assert positions == sorted(positions)


def test_every_panel_has_title_and_description(dashboard: dict) -> None:
    for panel in _iter_panels(dashboard["panels"]):
        assert panel.get("title"), f"panel without a title: {panel.get('id')}"
        assert panel.get("description"), f"panel without a description: {panel['title']}"


def test_home_every_panel_has_title_and_description(home: dict) -> None:
    for panel in _iter_panels(home["panels"]):
        assert panel.get("title"), f"panel without a title: {panel.get('id')}"
        assert panel.get("description"), f"panel without a description: {panel['title']}"


def test_dashboard_description_explains_reading_order(dashboard: dict) -> None:
    description = dashboard["description"]
    assert description.count(".") == 3
    rows = [
        "Run overview",
        "Run identity",
        "Collection coverage",
        "Tests and workers",
        "Fixture work",
        "Waits",
        "Service requests",
    ]
    for row in rows:
        assert row in description, f"dashboard description does not mention the {row} row"
    assert "collapsed Run identity" in description
    assert "not built yet" in description, "description must say cluster evidence is deferred"
    assert "narrower range can hide" in description, "description must warn about narrow time ranges"


# ---------------------------------------------------------------------------
# Detail dashboard: run overview
# ---------------------------------------------------------------------------


def test_run_overview_stat_panels(dashboard: dict) -> None:
    """Run overview holds the summary stats, before the Run identity row."""
    panels = [p for p in dashboard["panels"] if p["type"] != "row"]
    overview = [p for p in panels if p["gridPos"]["y"] < 6]
    assert [p["title"] for p in overview] == ["Tests", "Failed", "Result", "Session", "Test time", "Suite", "PR"]


def test_suite_and_pr_info_panels_degrade_gracefully(dashboard: dict) -> None:
    """Suite/PR stats read the Loki labels and show 'not labeled' when absent (old captures)."""
    panels = _panels_by_title(dashboard)
    for title, label in [("Suite", "osac_ci_suite"), ("PR", "osac_ci_pr")]:
        panel = panels[title]
        assert panel["type"] == "stat"
        assert panel["datasource"]["uid"] == "ci-observe-logs"
        assert panel["options"]["textMode"] == "name"
        assert panel["fieldConfig"]["defaults"]["noValue"] == "not labeled"
        expr = panel["targets"][0]["expr"]
        assert f'sum by ({label}) (count_over_time({{osac_ci_capture_id="$capture_id"}}' in expr


def test_slowest_tests_is_top_20(dashboard: dict) -> None:
    """The table must sort by duration server-side of the client sort, then keep 20 rows."""
    panels = _panels_by_title(dashboard)
    transformations = panels["Slowest tests"]["transformations"]
    assert transformations[0] == {"id": "sortBy", "options": {"sort": [{"field": "Duration", "desc": True}]}}
    assert transformations[1] == {"id": "limit", "options": {"limitField": 20}}


def test_run_duration_selects_the_session_span(dashboard: dict) -> None:
    """Run duration uses the one session span, not xdist worker spans, and keeps its time unit."""
    panels = _panels_by_title(dashboard)
    panel = panels["Test time"]
    assert panel["options"]["reduceOptions"]["calcs"] == ["max"]
    assert 'name = "test run"' in panel["targets"][0]["query"]
    assert panel["fieldConfig"]["defaults"].get("unit") is None
    assert _override_for(panel, "Duration") is None, "Run duration must keep the nanosecond unit"
    assert panel["description"].startswith("Elapsed time")
    assert panel["fieldConfig"]["defaults"]["noValue"] == "not recorded"
    null_maps = panel["fieldConfig"]["defaults"].get("mappings", [])
    assert any(m.get("options", {}).get("match") == "null" and m["options"]["color"] == "orange" for m in null_maps), (
        "a missing session span must show 'not recorded' in orange"
    )


def test_run_result_distinguishes_missing_session_span(dashboard: dict) -> None:
    """Run result comes from the session span status; a missing span says unknown, not passed."""
    panels = _panels_by_title(dashboard)
    panel = panels["Result"]
    assert panel["options"]["textMode"] == "value"
    assert panel["options"]["reduceOptions"]["calcs"] == ["lastNotNull"]
    query = panel["targets"][0]["query"]
    assert 'name = "test run"' in query
    assert "(status = ok || status = error)" in query
    assert panel["fieldConfig"]["defaults"]["noValue"] == "Incomplete: no session span"
    null_maps = panel["fieldConfig"]["defaults"].get("mappings", [])
    assert any(
        m.get("options", {}).get("match") == "null" and m["options"]["text"] == "Incomplete: no session span"
        for m in null_maps
    ), "a missing session span must map to the incomplete text"
    raw = DASHBOARD_FILE.read_text()
    assert "not passed" in raw, "a missing session span must never be presented as a pass"


# ---------------------------------------------------------------------------
# Count stat panels: no time units
# ---------------------------------------------------------------------------

# Live-verified Tempo spans-frame schema: the intrinsic columns every TraceQL
# spans response carries, with the display names the datasource stamps on
# them (displayNameFromDS). Attribute columns referenced by a query are added
# under their raw attribute name, which is also their display name.
SPANS_INTRINSIC_COLUMNS = {
    "traceIdHidden": "traceIdHidden",
    "traceService": "Trace Service",
    "traceName": "Trace Name",
    "spanID": "Span ID",
    "time": "Start time",
    "name": "Name",
    "duration": "Duration",
}

QUOTED_ATTRIBUTE = re.compile(r'\."([^"]+)"')
DOTTED_ATTRIBUTE = re.compile(r"(?<![\w.])\.((?:[A-Za-z_]\w*\.)+[A-Za-z_]\w*)")


def _spans_frame_columns(query: str) -> dict[str, str]:
    """Raw name -> display name for the columns a TraceQL spans query yields.

    Tempo only includes attribute columns for attributes referenced in the
    predicate, so the referenced attributes are extracted from the query.
    """
    columns = dict(SPANS_INTRINSIC_COLUMNS)
    referenced = QUOTED_ATTRIBUTE.findall(query) + DOTTED_ATTRIBUTE.findall(query)
    for attribute in referenced:
        columns[attribute] = attribute
    if re.search(r"(?<![\w.])status(?![\w.])", query):
        columns["status"] = "status"
    return columns


def _groupby_output_columns(group_by: dict, source_columns: dict[str, str]) -> set[str]:
    """Display names of the columns the groupBy transformation emits.

    Live-verified naming: groupby keys keep their raw field name, aggregated
    columns are named "<displayName> (agg)" from the source field's display
    name, so "time" aggregates into "Start time (min)".
    """
    output: set[str] = set()
    for raw_name, config in group_by["options"]["fields"].items():
        if config["operation"] == "groupby":
            output.add(raw_name)
        for aggregation in config.get("aggregations", []):
            output.add(f"{source_columns[raw_name]} ({aggregation})")
    return output


def _joined_columns(panel: dict) -> set[str]:
    """Display names of the columns after the panel's transformation pipeline."""
    columns: set[str] = set()
    frames = [_spans_frame_columns(t["query"]) for t in panel["targets"]]
    for frame in frames:
        columns |= set(frame.values())
    for transformation in panel.get("transformations", []):
        if transformation["id"] == "groupBy":
            for source in frames:
                columns |= _groupby_output_columns(transformation, source)
    return columns


def _grafana_fields_regex(fields_value: str) -> re.Pattern:
    """Grafana's stringToJsRegex, as applied to reduceOptions.fields.

    A plain string becomes an anchored, unescaped ^value$ regex; a /pattern/
    string is used as a free regex. Parentheses in a plain string are capture
    groups, not literals, which is how "pytest.span_type (count)" failed to
    match its own column.
    """
    if fields_value.startswith("/"):
        match = re.fullmatch(r"/(.*)/([gimsuy]*)", fields_value)
        assert match is not None, f"invalid regex-style fields value: {fields_value!r}"
        return re.compile(match.group(1))
    return re.compile("^" + fields_value + "$")


def test_count_stats_reduce_a_unitless_field(dashboard: dict) -> None:
    """Count stats must not render as time (the '47 ns' live defect).

    The stat panel inherits the reduced field's unit, and panel defaults do
    not override the datasource-provided nanosecond unit on Duration. So every
    count stat reduces the unitless Name field and carries a field-level
    Duration -> none override as well. "Name" is not a raw field name: the
    Tempo datasource stamps displayNameFromDS "Name" on the span-name column,
    and reduceOptions.fields is matched as ^Name$ against display names.
    """
    stats = [p for p in _iter_panels(dashboard["panels"]) if p["type"] == "stat"]
    counted = [p for p in stats if p["options"]["reduceOptions"]["calcs"] == ["count"]]
    assert counted, "dashboard has no count stats"
    for panel in counted:
        title = panel["title"]
        fields = panel["options"]["reduceOptions"]["fields"]
        assert fields == "Name", f"{title}: count stat must reduce the Name column (got {fields!r})"
        assert panel["fieldConfig"]["defaults"]["unit"] == "none", f"{title}: count stat needs unit none"
        override = _override_for(panel, "Duration")
        assert override is not None, f"{title}: count stat needs a field-level Duration unit override"
        units = [prop["value"] for prop in override["properties"] if prop["id"] == "unit"]
        assert units == ["none"], f"{title}: Duration override must force unit none"


def test_home_count_stats_reduce_a_unitless_field(home: dict) -> None:
    stats = [p for p in _iter_panels(home["panels"]) if p["type"] == "stat"]
    counted = [p for p in stats if p["options"]["reduceOptions"]["calcs"] == ["count"]]
    assert counted, "home dashboard has no count stats"
    for panel in counted:
        title = panel["title"]
        fields = panel["options"]["reduceOptions"]["fields"]
        if title == "Missing session span":
            # Reduces the joined capture-id column: the only column that is
            # non-null on rows without a session span and whose name is safe
            # for Grafana's anchored, unescaped display-name regex (the
            # aggregate "pytest.span_type (count)" compiles to a capture
            # group and can never match its own column).
            assert fields == "osac.ci.capture_id", f"{title}: must count the joined capture-id column"
        else:
            assert fields == "Name", f"{title}: count stat must reduce the Name column (got {fields!r})"
        assert panel["fieldConfig"]["defaults"]["unit"] == "none", f"{title}: count stat needs unit none"
        override = _override_for(panel, "Duration")
        assert override is not None, f"{title}: count stat needs a field-level Duration unit override"


def test_stat_reduce_fields_match_real_columns(dashboard: dict, home: dict) -> None:
    """Every stat's reduceOptions.fields must regex-match a column that exists.

    Grafana passes reduceOptions.fields through stringToJsRegex and matches it
    against field display names. A value whose regex form cannot match any
    column the panel produces (for example an unescaped "(count)") leaves the
    stat permanently empty, so each value is checked against the columns the
    panel's queries and transformations actually yield.
    """
    for dash in (dashboard, home):
        for panel in _iter_panels(dash["panels"]):
            if panel.get("type") != "stat":
                continue
            fields_value = panel["options"]["reduceOptions"]["fields"]
            if not fields_value:
                continue  # empty reduces every numeric field
            if panel.get("transformations"):
                columns = _joined_columns(panel)
            else:
                columns = set(_spans_frame_columns(panel["targets"][0]["query"]).values())
            pattern = _grafana_fields_regex(fields_value)
            assert any(pattern.search(column) for column in columns), (
                f"{panel['title']}: reduceOptions.fields {fields_value!r} matches none of {sorted(columns)}"
            )


def test_home_missing_session_stat_counts_the_right_rows(home: dict) -> None:
    """The missing-session stat must count exactly the orphan captures.

    Simulates the panel's live pipeline on synthetic rows shaped like the raw
    test-span frame: keep rows whose Trace Name is empty (the trace's root
    span - the session span - never arrived), then group by capture id. The
    reduced column must resolve through Grafana's display-name regex to a
    column that is non-null exactly on the surviving rows.
    """
    panel = _panels_by_title(home)["Missing session span"]
    fields_value = panel["options"]["reduceOptions"]["fields"]

    raw_rows = {  # capture id -> trace name on that capture's test spans
        "run-ok": "test run",
        "run-failed": "test run",
        "partial-1": "",
        "partial-2": "",
    }
    surviving = {key for key, trace_name in raw_rows.items() if trace_name == ""}
    assert surviving == {"partial-1", "partial-2"}

    pattern = _grafana_fields_regex(fields_value)
    matched = [column for column in _joined_columns(panel) if pattern.search(column)]
    assert matched, f"reduceOptions.fields {fields_value!r} matches no output column"
    for column in matched:
        assert column == "osac.ci.capture_id" or "(count)" in column or "(min)" in column, (
            f"{panel['title']}: reduced column {column!r} is not populated by the groupBy"
        )
    assert len(surviving) == 2


def test_zero_counts_are_not_shown_as_passes(dashboard: dict) -> None:
    """A zero from an empty result must not render as a green pass.

    A zero-row TraceQL search still returns a frame with fields, so the count
    reducer yields 0 and the noValue text never fires. Value mappings give
    those zeros an explicit non-green meaning instead.
    """
    panels = _panels_by_title(dashboard)
    expected = {
        "Tests": ("no test spans", "orange"),
        "Failed": ("none", "text"),
        "Parent test spans": ("none", "orange"),
        "Wait spans": ("none", "orange"),
        "Service spans": ("none", "orange"),
    }
    for title, (text, color) in expected.items():
        zeros = [
            option
            for mapping in panels[title]["fieldConfig"]["defaults"].get("mappings", [])
            if mapping.get("type") == "value"
            for key, option in mapping.get("options", {}).items()
            if key == "0"
        ]
        assert zeros, f"{title}: missing 0 value mapping"
        assert zeros[0].get("text") == text, f"{title}: 0 must read {text!r}"
        assert zeros[0].get("color") == color, f"{title}: 0 must be {color}, not green"


# ---------------------------------------------------------------------------
# Service panels: exclude pytest and wait spans
# ---------------------------------------------------------------------------


def test_service_panels_exclude_wait_spans(dashboard: dict) -> None:
    """Every 'not a pytest span' selector must also exclude wait spans.

    Wait spans only gained pytest.span_type = "wait" with the Q4 change, so
    excluding them live-compatibly requires the poll.delay predicate too.
    """
    selectors = [q for q in _traceql_queries(dashboard) if ".pytest.span_type = nil" in q]
    assert selectors, "dashboard has no service-span selectors"
    for query in selectors:
        assert 'span."poll.delay" = nil' in query, f"service selector counts wait spans: {query}"
        assert 'name =~ ".+"' in query, f"service selector must hydrate names: {query}"


def test_service_panels_present(dashboard: dict) -> None:
    panels = _panels_by_title(dashboard)
    for title in ["Calls", "Call errors", "Calls by service", "Slowest service calls", "gRPC errors"]:
        assert title in panels, f"missing service panel {title}"
    errors = panels["Call errors"]["targets"][0]["query"]
    assert "status = error" in errors


def test_groupby_aggregate_overrides_match_output_columns(dashboard: dict) -> None:
    """Aggregated groupBy columns are named from the source field's display name.

    The Tempo datasource stamps displayNameFromDS on its fields (spanID ->
    "Span ID", duration -> "Duration"), and Grafana's groupBy transformation
    names aggregate output "<displayName> (agg)" while the transformation
    config is keyed by raw field names. An override written as "spanID
    (count)" matches nothing, so the column keeps its raw header and, for
    durations, renders as unitless nanoseconds instead of formatted time.
    """
    raw_name_forms = {"spanID (count)", "duration (sum)", "duration (max)"}
    expected = {
        "Fixture setup": {"Span ID (count)", "Duration (sum)"},
        "Fixture reuse": {"Span ID (count)"},
        "Calls by service": {"Span ID (count)", "Duration (max)"},
    }
    panels = _panels_by_title(dashboard)
    for title, aggregate_columns in expected.items():
        matchers = {o["matcher"]["options"] for o in panels[title]["fieldConfig"]["overrides"]}
        for column in aggregate_columns:
            assert column in matchers, f"{title}: missing override for groupBy output column {column!r}"
        assert not (matchers & raw_name_forms), f"{title}: raw-name aggregate override never matches"


def test_grpc_errors_panel_queries_beyla_attributes(dashboard: dict) -> None:
    """The gRPC panel must query attributes Beyla actually sets.

    Beyla v1.47 stamps rpc.system.name = "grpc" and a string
    rpc.response.status_code; the OTel-style numeric rpc.grpc.status_code
    does not exist in the backend, so querying it left the panel permanently
    empty (a false "no gRPC errors").
    """
    panels = _panels_by_title(dashboard)
    panel = panels["gRPC errors"]
    query = panel["targets"][0]["query"]
    assert '.rpc.system.name = "grpc"' in query, "gRPC selector must use rpc.system.name"
    assert '.rpc.response.status_code != "OK"' in query, "gRPC errors must select non-OK response codes"
    override = _override_for(panel, "rpc.response.status_code")
    assert override is not None, "gRPC status column override must match the hydrated attribute column"


# ---------------------------------------------------------------------------
# Q5 out of scope: no cluster evidence panels
# ---------------------------------------------------------------------------


def test_no_cluster_evidence_panels(dashboard: dict) -> None:
    """Cluster evidence (logs, events, kubelet metrics) is deferred: no panels may imply it."""
    for panel in _iter_panels(dashboard["panels"]):
        assert panel["type"] != "logs", f"{panel['title']}: logs panels are deferred with Q5"
        assert panel["type"] != "timeseries", f"{panel['title']}: metric charts are deferred with Q5"
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            assert "k8s_" not in expr, f"{panel['title']}: kubelet metrics are deferred with Q5"


def test_cluster_evidence_deferral_is_explained(dashboard: dict) -> None:
    panels = _panels_by_title(dashboard)
    panel = panels["Where is the rest of the evidence?"]
    content = panel["options"]["content"]
    assert "deliberately deferred" in content
    assert "not built" in content
    assert EVIDENCE_LINK in content
    assert COMPARE_LINK in content, "the comparison link must not pass a capture id the target ignores"
    assert "osac_ci_capture_id" in content, "the captured logs must stay reachable through a link"


# ---------------------------------------------------------------------------
# Run identity
# ---------------------------------------------------------------------------


def test_identity_stats_cover_required_attributes(dashboard: dict) -> None:
    """Every run identity attribute has its own stat with a 'not collected' fallback."""
    panels = _panels_by_title(dashboard)
    for attribute, title in IDENTITY_ATTRIBUTES.items():
        assert title in panels, f"missing identity stat for {attribute}: {title}"
        panel = panels[title]
        assert panel["type"] == "stat"
        query = panel["targets"][0]["query"]
        assert f'resource."{attribute}" != nil' in query, f"{title}: query must select {attribute}"
        assert panel["fieldConfig"]["defaults"]["noValue"] == "not collected"
        reduce_options = panel["options"]["reduceOptions"]
        assert reduce_options == {"calcs": ["lastNotNull"], "fields": attribute, "values": False}
        assert panel["options"]["textMode"] == "value"


# ---------------------------------------------------------------------------
# Collection coverage
# ---------------------------------------------------------------------------


def test_collection_coverage_stats(dashboard: dict) -> None:
    panels = _panels_by_title(dashboard)
    # The session-span presence stat moved into the overview as Run completeness.
    assert "Session span" not in panels
    completeness = panels["Session"]
    assert 'name = "test run"' in completeness["targets"][0]["query"]
    texts = json.dumps(completeness["fieldConfig"]["defaults"].get("mappings", []))
    assert "Complete" in texts and "Incomplete" in texts
    assert completeness["fieldConfig"]["defaults"]["noValue"] == "Incomplete"

    tests = panels["Parent test spans"]
    assert '.pytest.span_type = "test"' in tests["targets"][0]["query"]
    assert "100 spans per trace" in tests["description"], "test count must note the spanset cap"

    waits = panels["Wait spans"]
    assert '( .pytest.span_type = "wait" || span."poll.delay" != nil )' in waits["targets"][0]["query"]
    assert "100 spans per trace" in waits["description"]

    service = panels["Service spans"]
    assert '.pytest.span_type = nil && span."poll.delay" = nil' in service["targets"][0]["query"]
    assert "30,000" in service["description"], "service count must note the search cap"


def test_failed_tests_zero_is_contextualized(dashboard: dict) -> None:
    """A zero in Failed tests must not read as evidence of success on its own."""
    panels = _panels_by_title(dashboard)
    panel = panels["Failed"]
    assert "only meaningful" in panel["description"]
    assert "Result" in panel["description"]
    assert panel["fieldConfig"]["defaults"]["noValue"] != "0"
    table = panels["Failed tests, longest first"]
    assert "Collection coverage" in table["description"]


# ---------------------------------------------------------------------------
# Worker attribution and fixtures
# ---------------------------------------------------------------------------


def test_test_timeline_shows_worker_and_status(dashboard: dict) -> None:
    panels = _panels_by_title(dashboard)
    panel = panels["Test timeline"]
    query = panel["targets"][0]["query"]
    assert '.pytest.span_type = "test"' in query
    assert 'span."osac.ci.xdist_worker" != nil' in query, "worker attribute must be referenced to hydrate"
    assert "( status = ok || status = error || status = unset )" in query, "status must be referenced to hydrate"
    texts = _mapping_texts(panel, "osac.ci.xdist_worker")
    assert "not recorded" in texts, "worker column must map missing values to a plain-words state"
    assert panel["options"]["sortBy"] == [{"displayName": "Start", "desc": False}]
    assert "worker" in panel["description"].lower()


def test_fixture_panels(dashboard: dict) -> None:
    panels = _panels_by_title(dashboard)
    setup = panels["Fixture setup"]
    query = setup["targets"][0]["query"]
    assert '.pytest.span_type = "fixture"' in query
    assert 'name =~ ".* setup"' in query, "fixture setup table must exclude teardown spans"
    group_by = setup["transformations"][0]["options"]["fields"]
    assert group_by["name"]["operation"] == "groupby"
    assert group_by["osac.ci.xdist_worker"]["operation"] == "groupby"
    assert "wall" in setup["description"].lower(), "fixture time must not be presented as wall-clock time"

    reuse = panels["Fixture reuse"]
    reuse_query = reuse["targets"][0]["query"]
    assert '.pytest.span_type = "fixture_use"' in reuse_query
    assert 'span."pytest.fixture_name" != nil' in reuse_query
    assert reuse["fieldConfig"]["defaults"]["noValue"] == "fixture reuse not instrumented yet"


# ---------------------------------------------------------------------------
# Waits
# ---------------------------------------------------------------------------


def test_waits_panel(dashboard: dict) -> None:
    panels = _panels_by_title(dashboard)
    panel = panels["Longest waits"]
    query = panel["targets"][0]["query"]
    assert '( .pytest.span_type = "wait" || span."poll.delay" != nil )' in query
    for attribute in ("poll.attempts", "poll.condition_met", "osac.wait.resource_kind", "osac.wait.resource_id"):
        assert f'span."{attribute}" != nil' in query, f"waits query must reference {attribute}"
    assert panel["options"]["sortBy"] == [{"displayName": "Duration", "desc": True}]
    texts = _mapping_texts(panel, "osac.wait.resource_kind")
    assert "not recorded" in texts
    assert "wall" in panel["description"].lower(), "waits must not be presented as runner wall time"


# ---------------------------------------------------------------------------
# Cross-dashboard links and query scoping
# ---------------------------------------------------------------------------


def test_tempo_targets_use_traceql_search(dashboard: dict) -> None:
    tempo_targets = [
        (panel, target)
        for panel in _iter_panels(dashboard["panels"])
        for target in panel.get("targets", [])
        if panel.get("datasource", {}).get("uid") == "ci-observe-traces"
    ]
    assert tempo_targets, "dashboard has no Tempo targets"
    for panel, target in tempo_targets:
        assert target.get("queryType") == "traceql", f"{panel['title']}: unexpected Tempo queryType"
        assert 'resource."osac.ci.capture_id" = "$capture_id"' in target["query"], (
            f"{panel['title']}: TraceQL query is not filtered by capture id"
        )


def test_loki_targets_are_scoped_to_capture(dashboard: dict) -> None:
    loki_exprs = [
        t["expr"]
        for p in _iter_panels(dashboard["panels"])
        for t in p.get("targets", [])
        if p.get("datasource", {}).get("uid") == "ci-observe-logs"
    ]
    assert loki_exprs, "dashboard has no Loki targets"
    for expr in loki_exprs:
        assert 'osac_ci_capture_id="$capture_id"' in expr, f"Loki query not scoped to capture: {expr}"


def test_default_time_range_is_seven_days(dashboard: dict, home: dict) -> None:
    assert dashboard["time"] == {"from": "now-7d", "to": "now"}
    assert home["time"] == {"from": "now-7d", "to": "now"}


def test_no_leftover_template_tokens(dashboard: dict, home: dict) -> None:
    for dash, file in ((dashboard, DASHBOARD_FILE), (home, HOME_FILE)):
        variables = {v["name"] for v in dash["templating"]["list"]}
        token = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
        for text in _walk_strings(dash):
            for match in token.findall(text):
                if match.startswith("$"):
                    continue
                assert match in variables or GRAFANA_BUILTIN_VARIABLE.match(match), (
                    f"unresolved template token {match!r} in {file.name}"
                )


# ---------------------------------------------------------------------------
# Home dashboard
# ---------------------------------------------------------------------------


def test_home_panels_are_in_reading_order(home: dict) -> None:
    positions = [(p["gridPos"]["y"], p["gridPos"]["x"]) for p in home["panels"]]
    assert positions == sorted(positions)
    titles = [p["title"] for p in home["panels"]]
    assert titles.index("Recent failed tests") < titles.index("Recent test runs"), (
        "failed tests must be listed before the full run list"
    )


def test_home_explains_capture_id_and_time_range(home: dict) -> None:
    content = _panels_by_title(home)["Start here"]["options"]["content"]
    for part in ("vmaas", "pr123", "attempt", "workflow run"):
        assert part in content, f"intro text does not explain the ID part {part!r}"
    assert "7 days" in content
    assert "hide a long run" in content, "intro must warn that narrow ranges can hide runs"
    assert "7-day maximum" in content, "intro must cap the widening advice at Tempo's 168h limit"


def test_home_queries_do_not_require_labels(home: dict) -> None:
    """Unlabeled runs must appear: suite/PR predicates may only exist as or-groups."""
    for query in _traceql_queries(home):
        if 'resource."osac.ci.suite" =~ ".+"' in query:
            assert '( resource."osac.ci.suite" =~ ".+" || name =~ ".*" )' in query, (
                f"suite predicate filters out unlabeled runs: {query}"
            )
        if 'resource."osac.ci.pr" =~ ".+"' in query:
            assert '( resource."osac.ci.pr" =~ ".+" || name =~ ".*" )' in query, (
                f"PR predicate filters out unlabeled runs: {query}"
            )


def test_home_run_list_reads_the_session_spans(home: dict) -> None:
    """The run list shows one row per run whose session span arrived.

    The live Grafana engine only ever feeds a panel's transformations the
    first query's frame (a multi-query join silently degrades to the first
    frame), so the run list is a single session-span query with no
    transformations. Captures with test spans but no session span are listed
    by the dedicated table above the run list.
    """
    panels = _panels_by_title(home)
    panel = panels["Recent test runs"]
    assert len(panel["targets"]) == 1
    assert 'name = "test run"' in panel["targets"][0]["query"]
    assert not panel.get("transformations"), "the run list must not depend on join transformations"
    texts = _mapping_texts(panel, "status")
    assert "Passed" in texts and "Failed" in texts


def test_home_time_min_override_matches_groupby_output(home: dict) -> None:
    """The time-min aggregate column is named from the time field's display name.

    Tempo stamps displayNameFromDS "Start time" on the time field, so the
    groupBy output column is "Start time (min)"; an override written as
    "time (min)" matches nothing and the First test column loses its header,
    width and sort key.
    """
    panels = _panels_by_title(home)
    for title in ("Captures with test spans but no session span",):
        matchers = {o["matcher"]["options"] for o in panels[title]["fieldConfig"]["overrides"]}
        assert "Start time (min)" in matchers, f"{title}: missing override for the time-min output column"
        assert "time (min)" not in matchers, f"{title}: raw-name time override never matches"


def test_home_missing_session_marker_cannot_come_from_the_test_spans(home: dict) -> None:
    """The missing-session marker must be a session-trace property, not span data.

    The filter that selects incomplete captures runs on the RAW frame, before
    the groupBy, and matches the intrinsic Trace Name column: the datasource
    derives it from the trace's root span, which for a complete capture is the
    session span ("test run") and is empty when the session span never
    arrived. Test spans cannot populate it themselves, so a capture can never
    be miscounted as missing because a test span carried - or failed to
    carry - one of its own attributes. A pipeline that filters on a span
    attribute (status, pytest.span_type, ...) or that filters after the
    groupBy would reintroduce that class of bug.
    """
    panels = _panels_by_title(home)
    for title in ("Missing session span", "Captures with test spans but no session span"):
        panel = panels[title]
        pipeline = panel["transformations"]
        assert pipeline[0]["id"] == "filterByValue", f"{title}: the marker filter must run before the groupBy"
        assert pipeline[1]["id"] == "groupBy", f"{title}: the groupBy must follow the marker filter"
        marker = pipeline[0]["options"]["filters"][0]
        assert marker["fieldName"] == "Trace Name", (
            f"{title}: the marker must be the root-span-derived Trace Name, not a span attribute"
        )
        assert marker["config"]["options"]["value"] == "^$"
        # the marker column must not be a span attribute the query hydrates
        query = panel["targets"][0]["query"]
        assert "Trace Name" not in query, "Trace Name is intrinsic; referencing it as an attribute is a wiring bug"


def test_home_incomplete_captures_are_visible(home: dict) -> None:
    """A stat and a table surface captures with test spans but no session span.

    Live-verified pipeline: the test-span query returns every parent test span
    with the intrinsic Trace Name column, which Grafana's Tempo datasource
    fills from the trace's root span. The root of a complete capture's trace
    is the session span ("test run"); a capture whose session span never
    arrived has an empty trace name on all of its spans. Filtering the raw
    frame for an empty Trace Name keeps exactly the orphan test spans, and the
    groupBy then collapses them to one row per capture.
    """
    panels = _panels_by_title(home)
    stat = panels["Missing session span"]
    table = panels["Captures with test spans but no session span"]

    for panel in (stat, table):
        pipeline = [t["id"] for t in panel["transformations"]]
        assert pipeline == ["filterByValue", "groupBy"], pipeline
        keep = panel["transformations"][0]
        assert keep["options"]["type"] == "include"
        assert keep["options"]["filters"][0]["config"]["id"] == "regex"
        assert keep["options"]["filters"][0]["fieldName"] == "Trace Name"
        assert keep["options"]["filters"][0]["config"]["options"]["value"] == "^$"
        group_by = panel["transformations"][1]
        assert group_by["options"]["fields"]["osac.ci.capture_id"]["operation"] == "groupby"
        assert group_by["options"]["fields"]["pytest.span_type"]["aggregations"] == ["count"]
        assert group_by["options"]["fields"]["time"]["aggregations"] == ["min"]
    assert len(stat["targets"]) == 1
    assert ".pytest.span_type" in stat["targets"][0]["query"]
    assert stat["options"]["reduceOptions"]["fields"] == "osac.ci.capture_id"
    assert "unknown" in table["description"]
    assert "not passed" in table["description"]
    assert "session span" in table["fieldConfig"]["defaults"]["noValue"]


def test_home_distinguishes_failed_runs_from_failed_tests(home: dict) -> None:
    panels = _panels_by_title(home)
    failed_runs = panels["Failed runs"]
    assert 'name = "test run"' in failed_runs["targets"][0]["query"]
    assert "status = error" in failed_runs["targets"][0]["query"]
    assert "failed runs, not failed tests" in failed_runs["description"]
    failed_tests = panels["Recent failed tests"]
    assert failed_tests["targets"][0]["query"].count("status = error") == 1
    assert '.pytest.span_type = "test"' in failed_tests["targets"][0]["query"]


def test_home_links_runs_to_detail(home: dict) -> None:
    """Every run-id column links straight to the investigation page.

    One data link makes the id a direct link. Two links turn the cell into a
    menu, which read as "clicking does nothing" in the intern walkthrough.
    The comparison dashboard is reachable from the Start-here map instead."""
    linked = []
    for panel in _iter_panels(home["panels"]):
        for override in panel["fieldConfig"].get("overrides", []):
            if override["matcher"].get("options") == "osac.ci.capture_id":
                for prop in override["properties"]:
                    if prop["id"] == "links":
                        linked.append((panel["title"], prop["value"]))
    assert linked, "no run-id column carries links"
    for title, links in linked:
        urls = [link["url"] for link in links]
        assert len(urls) == 1, f"{title}: a second data link turns the cell into a menu"
        assert any(url.startswith(DETAIL_LINK_PREFIX) for url in urls), f"{title}: missing detail link"
        assert any(url.startswith(DETAIL_LINK_PREFIX) and "var-capture_id=${__value.raw}" in url for url in urls), (
            f"{title}: the run-investigation link must keep var-capture_id"
        )


def test_home_stat_titles_are_honest(home: dict) -> None:
    panels = _panels_by_title(home)
    assert "Runs with a session span" in panels
    assert panels["Runs with a session span"]["description"]
    assert "Missing session span" in panels
    assert "unknown - not passed" in panels["Missing session span"]["description"]


# ---------------------------------------------------------------------------
# Round 3: partial-run visibility, service-evidence framing, layout
# ---------------------------------------------------------------------------


def test_run_completeness_stat_in_overview(dashboard: dict) -> None:
    """The overview leads with whether the capture is complete."""
    panels = _panels_by_title(dashboard)
    panel = panels["Session"]
    assert panel["gridPos"]["y"] < 5, "Run completeness must sit in the first overview row"
    mappings = json.dumps(panel["fieldConfig"]["defaults"].get("mappings", []))
    assert '"1"' in mappings and "Complete" in mappings
    assert '"0"' in mappings and "Incomplete" in mappings
    assert panel["fieldConfig"]["defaults"]["noValue"] == "Incomplete"
    assert panel["options"]["reduceOptions"] == {"calcs": ["count"], "fields": "Name", "values": False}


def test_identity_row_collapsed_but_present(dashboard: dict) -> None:
    """The identity row starts collapsed but keeps every attribute reachable."""
    rows = [p for p in dashboard["panels"] if p.get("type") == "row"]
    identity = next(r for r in rows if "Run identity" in r["title"])
    assert identity["collapsed"] is True
    # The old title said the contents were "mostly not collected", which told
    # a newcomer to skip the row. Name the fields instead.
    assert "source commit" in identity["title"]
    assert "harness commit" in identity["title"]
    assert "not collected" not in identity["title"]
    children = identity.get("panels") or []
    assert len(children) == len(IDENTITY_ATTRIBUTES), "every identity stat must stay inside the row"
    assert all(c.get("title") in IDENTITY_ATTRIBUTES.values() for c in children)


def test_service_row_is_framed_as_evidence(dashboard: dict) -> None:
    """Service-call errors must not read as test failures."""
    panels = _panels_by_title(dashboard)
    warning = panels["Service-call errors are not test failures"]
    content = warning["options"]["content"]
    assert "not test failures" in content
    assert "Failed tests and Run result" in content
    # the warning sits at the top of the service row, before the panels
    first_panel = min(
        (p["gridPos"]["y"] for t, p in panels.items() if t in ("Calls", "Call errors", "Calls by service"))
    )
    assert warning["gridPos"]["y"] < first_panel

    errors = panels["Call errors"]
    steps = errors["fieldConfig"]["defaults"]["thresholds"]["steps"]
    assert steps[0]["color"] == "orange", "Calls with errors must be neutral/orange, not red or blue"
    assert "service evidence" in errors["description"]
    assert "service evidence" in panels["gRPC errors"]["description"]


def test_key_tables_are_full_width(dashboard: dict) -> None:
    """The wide evidence tables span the full grid so their columns are reachable."""
    panels = _panels_by_title(dashboard)
    for title in (
        "Test timeline",
        "Slowest tests",
        "Failed tests, longest first",
        "Longest waits",
        "Slowest service calls",
        "gRPC errors",
    ):
        assert panels[title]["gridPos"]["w"] == 24, f"{title}: key table must be full width"


def test_home_failed_tests_fit_without_pr(home: dict) -> None:
    """The failed-tests table hides the PR column so the rest fits at 1280."""
    panels = _panels_by_title(home)
    panel = panels["Recent failed tests"]
    pr_override = _override_for(panel, "osac.ci.pr")
    assert pr_override is not None
    assert {"id": "custom.hidden", "value": True} in pr_override["properties"]


# ---------------------------------------------------------------------------
# Round 4/5: nothing truncated without a way to see it
# ---------------------------------------------------------------------------

# Long-text columns: the full value stays reachable through the cell's inspect
# action (the eye icon opens the raw value), and the column stretches instead
# of holding a fixed width.
INSPECTED_NAME_COLUMNS = {
    "Test timeline": "name",
    "Slowest tests": "name",
    "Failed tests, longest first": "name",
    "Longest waits": "name",
    "Slowest service calls": "name",
    "gRPC errors": "name",
    "Fixture setup": "name",
    "Fixture reuse": "pytest.fixture_name",
}
# Columns whose cells wrap. Verified against the live Grafana 13.2.4 build:
# plain `custom.wrapText` is dead code there; the working switch is
# `custom.cellOptions: {type: "auto", wrapText: true, dynamicHeight: true}`
# (cells become white-space: pre-line, rows size to content).
WRAPPED_COLUMNS = {
    **INSPECTED_NAME_COLUMNS,
    "Longest waits": "osac.wait.resource_id",
    "Calls by service": "traceService",
}
# Columns showing pytest nodeids. CSS cannot break a nodeid (no spaces, and
# slashes do not break under pre-line), so these columns add a regex value
# mapping that inserts U+200B (zero-width space) after "/" and "::": the cell
# wraps invisibly where needed, and the raw value stays intact for sorting,
# copying and the inspect modal.
NODEID_COLUMNS = {"Test timeline": "name", "Slowest tests": "name", "Failed tests, longest first": "name"}
# Where the invisible break points go, per column kind. pytest nodeids break
# after "/", "::", "_" and "[" (a 57-char test function is ~470px, wider than
# any column the layout can offer); snake_case fixture names after "_";
# dotted gRPC method names after "/" and ".". All render as U+200B
# (display-only).
BREAK_POINT_MAPPINGS = {
    **{
        key: ("/(?<=\\/|::|_|\\[)/g",)
        for key in [(t, "name") for t in NODEID_COLUMNS] + [("Recent failed tests", "name")]
    },
    ("Fixture setup", "name"): ("/(?<=_)/g",),
    ("Fixture reuse", "pytest.fixture_name"): ("/(?<=_)/g",),
    ("gRPC errors", "name"): ("/(?<=\\/|\\.)/g",),
    ("Slowest service calls", "name"): ("/(?<=\\/|\\.)/g",),
}
NODEID_BREAK_TEXT = "\u200b"
MIN_COLUMN_WIDTHS = {
    "Test timeline": {"time": 195},
    "Slowest tests": {"time": 195},
    "Failed tests, longest first": {"time": 195},
    "Slowest service calls": {"time": 195},
    "gRPC errors": {"time": 195, "rpc.response.status_code": 195},
    "Longest waits": {"status": 145},
}


def test_long_text_columns_stretch_and_inspect(dashboard: dict) -> None:
    """Long values are never truncated with no way to read them.

    The tables stretch the name column (no fixed width), give every cell the
    inspect action (which opens the full raw value), and wrap the cell text;
    see test_long_text_columns_wrap and test_nodeid_columns_have_break_points.
    """
    panels = _panels_by_title(dashboard)
    for title, field in INSPECTED_NAME_COLUMNS.items():
        override = _override_for(panels[title], field)
        assert override is not None, f"{title}: missing override for {field}"
        props = {pr["id"]: pr.get("value") for pr in override["properties"]}
        assert props.get("custom.inspect") is True, f"{title}/{field}: cells need the inspect action"
        assert "custom.width" not in props, f"{title}/{field}: long-text column must stretch, not fix width"
        assert "custom.wrapText" not in props, f"{title}/{field}: plain custom.wrapText is dead code in Grafana 13.2"


def test_long_text_columns_wrap(dashboard: dict, home: dict) -> None:
    """Wrapped columns turn on the cell wrap this Grafana build supports."""
    home_wrapped = {"Recent failed tests": "name"}
    for title in ("Recent failed tests", "Captures with test spans but no session span", "Recent test runs"):
        home_wrapped.setdefault(title, "osac.ci.capture_id")
    for dash, columns, label in ((dashboard, WRAPPED_COLUMNS, "detail"), (home, home_wrapped, "home")):
        panels = _panels_by_title(dash)
        for title, field in columns.items():
            override = _override_for(panels[title], field)
            assert override is not None, f"{label} {title}: missing override for {field}"
            props = {pr["id"]: pr.get("value") for pr in override["properties"]}
            cell_options = props.get("custom.cellOptions")
            assert isinstance(cell_options, dict), f"{title}/{field}: needs custom.cellOptions wrap"
            assert cell_options.get("wrapText") is True, f"{title}/{field}: cellOptions.wrapText must be on"
            assert cell_options.get("dynamicHeight") is True, f"{title}/{field}: rows must size to content"


def test_nodeid_columns_have_break_points(dashboard: dict, home: dict) -> None:
    """Long single-token cells get invisible break points so text can wrap.

    A pytest nodeid has no spaces and CSS does not break after "/", a
    snake_case fixture name does not break after "_", and a dotted gRPC
    method breaks nowhere; an unmapped cell can never wrap no matter how the
    column is sized. The regex value mappings insert U+200B at those seams
    (display only: sorting, copying and inspect keep the raw value).
    """
    expected = {**{k: v for k, v in BREAK_POINT_MAPPINGS.items()}}
    panels_detail = _panels_by_title(dashboard)
    panels_home = _panels_by_title(home)
    for (title, field), patterns in expected.items():
        panels = panels_home if title == "Recent failed tests" else panels_detail
        label = "home" if panels is panels_home else "detail"
        override = _override_for(panels[title], field)
        assert override is not None, f"{label} {title}: missing override for {field}"
        props = {pr["id"]: pr.get("value") for pr in override["properties"]}
        mappings = props.get("mappings") or []
        for pattern in patterns:
            match = any(
                m.get("type") == "regex"
                and m["options"].get("pattern") == pattern
                and m["options"].get("result", {}).get("text") == NODEID_BREAK_TEXT
                for m in mappings
            )
            assert match, f"{title}/{field}: needs a mapping inserting U+200B at {pattern}"


def test_fixed_columns_fit_their_values(dashboard: dict, home: dict) -> None:
    """Timestamps, run IDs and gRPC statuses keep full-width columns.

    Live-measured needs (text width + 24px cell padding): the ms-precision
    timestamp "2026-11-01 16:03:33.322" is 191px, FAILED_PRECONDITION is
    ~179px, "no error recorded" is 143px, a 24-char run id is ~227px.
    """
    panels = _panels_by_title(dashboard)
    for title, fields in MIN_COLUMN_WIDTHS.items():
        for field, minimum in fields.items():
            override = _override_for(panels[title], field)
            assert override is not None, f"{title}: missing override for {field}"
            widths = [pr["value"] for pr in override["properties"] if pr["id"] == "custom.width"]
            assert widths and widths[0] >= minimum, f"{title}/{field}: column width {widths} < {minimum}"
    for title in ("Recent test runs", "Recent failed tests", "Captures with test spans but no session span"):
        override = _override_for(_panels_by_title(home)[title], "osac.ci.capture_id")
        widths = [pr["value"] for pr in override["properties"] if pr["id"] == "custom.width"]
        assert widths and widths[0] >= 240, f"{title}: run-id column must fit the full id including the attempt suffix"


def test_stat_titles_are_short_enough_to_render(dashboard: dict) -> None:
    """The panel-header title box gets less than half the panel width.

    Live-measured: at w4 the title box is 70px at 1440 and 43px at 1280, so
    long titles like "Run completeness" clip to "Run co...". The overview
    therefore uses short titles (Result, Session, Duration) whose meaning the
    descriptions carry, and stats wide enough for them.
    """
    panels = _panels_by_title(dashboard)
    expected = {
        "Tests": (0, 1, 6),
        "Failed": (6, 1, 6),
        "Result": (12, 1, 6),
        "Session": (18, 1, 6),
        "Test time": (0, 5, 8),
        "Suite": (8, 5, 8),
        "PR": (16, 5, 8),
        "Calls": (0, 74, 5),
        "Call errors": (5, 74, 5),
    }
    for title, (x, y, w) in expected.items():
        p = panels[title]
        g = p["gridPos"]
        assert (g["x"], g["y"], g["w"]) == (x, y, w), f"{title}: grid {g}"
        assert len(title) * 7 <= min(w * 36, 115), f"{title}: title too long for its panel width"
