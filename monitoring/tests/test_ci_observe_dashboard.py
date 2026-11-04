"""Structural tests for the ci-observe dashboards.

monitoring/config/grafana/dashboards/ci-observe-runs.json (one capture at a
time) and ci-observe-runs-home.json (the run list) are provisioned by
monitoring/config/grafana/dashboards.yml into the ci-observe folder next to the
other dashboards.

The Capture page reads one document from the workflow exporter's
/api/ci-observe/capture endpoint through Infinity. That document lists every
classified span of the session trace, so the page shows exact counts and no
Tempo search page size. These tests pin the endpoint URL, the document fields
each panel selects, and the absence of TraceQL targets.

The Start here page keeps its Tempo queries; its tests are at the bottom of
this file. Live-verified Grafana/Tempo behaviors the Start here queries rely
on:

- A TraceQL search response only includes attribute columns for attributes
  referenced in the predicate, so every displayed attribute is referenced.
- The stat panel inherits the reduced field's unit, so count stats reduce a
  unitless field (Name) instead of the nanosecond Duration field; panel
  defaults alone do not override the datasource-provided ns unit.
- The Tempo datasource stamps displayNameFromDS on every field (name ->
  "Name", duration -> "Duration", time -> "Start time", spanID -> "Span
  ID"), and the groupBy transformation names aggregated output columns
  "<displayName> (agg)" while its config keys stay raw names.
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

# The one document every Capture panel reads. Panels may share it because the
# endpoint caches each capture for 10 minutes.
CAPTURE_URL = "http://127.0.0.1:9103/api/ci-observe/capture?capture_id=${capture_id:percentencode}"
RUN_EVIDENCE_URL = "http://127.0.0.1:9103/api/ci-observe/run?capture_id=${capture_id:percentencode}"

# Datasource uids owned by the ci-observe backend. ci-observe-metrics carries
# the cluster metrics in the Environment row, ci-observe-logs the Kubernetes
# events in the Events row, and workflow-api the Infinity panels.
CI_OBSERVE_DATASOURCE_UIDS = {"ci-observe-traces", "ci-observe-logs", "ci-observe-metrics"}

# Grafana built-in variables ($__interval, $__value.raw, ...) are allowed
# in queries; every other $name must be a dashboard template variable.
GRAFANA_BUILTIN_VARIABLE = re.compile(r"^__")

# The Start here run-id column links into the investigation page.
DETAIL_LINK_PREFIX = "/d/ci-observe-runs/ci-observe-e28094-run-investigation"

# The collapsed sections that keep GitHub and cluster evidence on the page.
CI_EVIDENCE_ROW = "CI evidence"
IDENTITY_ROW = "Run identity (source commit, harness commit, selection)"
ENVIRONMENT_ROW = "Environment"
EVENTS_ROW = "Events"

# A capture document with exactly the keys the read-path spec defines. Every
# Infinity column selector on the page must resolve against this document.
CAPTURE_DOC = {
    "capture_id": "vmaas-main-41709318840-a1",
    "state": "ok",
    "transient": False,
    "session": {
        "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "started_at": "2026-11-04T09:12:03Z",
        "duration_seconds": 1554.5,
        "status": "error",
        "tests": 148,
        "test_attempts": 444,
        "workers": 8,
        "span_total": 1854,
    },
    "coverage": {
        "spans_total": 1854,
        "spans_classified": 1854,
        "spans_unclassified": 0,
        "session_span": True,
        "service_calls": {"sampled": True},
    },
    "identity": {
        "capture_id": "vmaas-main-41709318840-a1",
        "run_id": "41709318840",
        "run_attempt": "1",
        "suite": "vmaas",
        "pr": "752",
        "workflow": "E2E VMaaS Full Install",
        "test_ref": "main",
        "test_sha": "d290a6e4f7cd2f2c7b6f1a5c1f2e3d4c5b6a7980",
        "test_infra_ref": "main",
        "test_infra_sha": "4cacab7a1b2c3d4e5f60718293a4b5c6d7e8f901",
        "test_infra_workflow_sha": "d290a6e4f7cd2f2c7b6f1a5c1f2e3d4c5b6a7980",
        "test_suite": "tests/e2e/vmaas/ tests/e2e/references/",
        "test_filter": "none",
        "test_marker": "not requires_caas and not requires_bmaas",
        "session_trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "identity_source": "info",
        "capture_started_at": "2026-11-04T09:12:03Z",
    },
    "tests": [
        {
            "name": "tests/e2e/vmaas/test_cluster_create.py::TestCluster::test_create",
            "nodeid": "tests/e2e/vmaas/test_cluster_create.py::TestCluster::test_create",
            "worker": "gw3",
            "status": "error",
            "started_at": "2026-11-04T09:13:10Z",
            "duration_s": 95.2,
        },
        {
            "name": "tests/e2e/vmaas/test_network.py::test_virtual_network",
            "nodeid": "tests/e2e/vmaas/test_network.py::test_virtual_network",
            "worker": "gw1",
            "status": "ok",
            "started_at": "2026-11-04T09:14:45Z",
            "duration_s": 12.4,
        },
    ],
    "waits": [
        {
            "name": "wait for ComputeInstance RUNNING",
            "resource_kind": "ComputeInstance",
            "resource_id": "ci-9f2d",
            "attempts": 47,
            "condition_met": "true",
            "status": "ok",
            "started_at": "2026-11-04T09:15:00Z",
            "duration_s": 282.0,
        }
    ],
    "fixtures": [
        {
            "name": "cluster",
            "scope": "session",
            "worker": "gw0",
            "started_at": "2026-11-04T09:12:05Z",
            "duration_s": 610.0,
        }
    ],
    "fixture_uses": [
        {
            "fixture_name": "cluster",
            "nodeid": "tests/e2e/vmaas/test_cluster_create.py::TestCluster::test_create",
            "worker": "gw3",
            "started_at": "2026-11-04T09:13:10Z",
            "duration_s": 0.4,
        }
    ],
    "failed_tests": [
        {
            "classname": "tests.e2e.vmaas.test_cluster_create.TestCluster",
            "name": "test_create",
            "type": "failure",
            "message": "AssertionError: cluster did not reach Ready",
            "time_s": 95.2,
        }
    ],
    "notes": [],
}

# Counts the page must take from the endpoint, never from a Tempo search.
# Root selector -> selected column; a list root is counted by row, a scalar
# root by its own count field.
COUNT_SOURCES = {
    "Tests": ("session", "tests", ["lastNotNull"]),
    "Test attempts": ("session", "test_attempts", ["lastNotNull"]),
    "Wait spans": ("waits", "name", ["count"]),
    "Fixture setups": ("fixtures", "name", ["count"]),
    "Fixture uses": ("fixture_uses", "fixture_name", ["count"]),
    "Failed": ("failed_tests", "name", ["count"]),
}


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


def _row(dashboard: dict, title: str) -> dict:
    (row,) = [p for p in dashboard["panels"] if p["type"] == "row" and p["title"] == title]
    return row


def _root_value(document: object, root_selector: str) -> object:
    """Resolve an Infinity root selector (JSONPath "$" or a dotted path)."""
    if root_selector in ("", "$", "$."):
        return document
    value = document
    for part in root_selector.removeprefix("$").strip(".").split("."):
        assert isinstance(value, dict) and part in value, f"root selector {root_selector!r} does not resolve"
        value = value[part]
    return value


def _first_row(value: object) -> dict:
    if isinstance(value, list):
        assert value, "the fixture document has no row for this list root"
        return value[0]
    assert isinstance(value, dict), f"fixture document value is not an object: {type(value)}"
    return value


def _capture_targets(dash: dict) -> list[tuple[dict, dict]]:
    return [
        (panel, target)
        for panel in _iter_panels(dash["panels"])
        for target in panel.get("targets", [])
        if target.get("url") == CAPTURE_URL
    ]


# ---------------------------------------------------------------------------
# Capture dashboard: file-level structure
# ---------------------------------------------------------------------------


def test_json_is_valid_and_parses(dashboard: dict) -> None:
    assert DASHBOARD_FILE.exists()
    assert isinstance(dashboard, dict)


def test_required_uid_and_title(dashboard: dict) -> None:
    assert dashboard["uid"] == "ci-observe-runs"
    assert dashboard["title"] == "CI Observe — Capture"
    assert dashboard["schemaVersion"] == 39
    assert dashboard["version"] == 1


def test_datasources_referenced(dashboard: dict) -> None:
    """Infinity carries the capture document and the GitHub evidence,
    Prometheus carries the Environment metrics, Loki the Events."""
    raw = DASHBOARD_FILE.read_text()
    assert "workflow-api" in raw, "datasource uid workflow-api is not referenced anywhere"
    assert "ci-observe-logs" in raw, "datasource uid ci-observe-logs is not referenced anywhere"
    assert "ci-observe-metrics" in raw, "datasource uid ci-observe-metrics is not referenced anywhere"


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
    allowed = CI_OBSERVE_DATASOURCE_UIDS | {"workflow-api"} | _existing_datasource_uids()
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
        "CI evidence",
        IDENTITY_ROW,
        "Tests and workers",
        "Fixture work",
        "Waits",
        "Service requests",
        ENVIRONMENT_ROW,
        EVENTS_ROW,
    ]


def test_panels_are_in_reading_order(dashboard: dict) -> None:
    """The panels array must read top to bottom, left to right (by gridPos)."""
    positions = [(p["gridPos"]["y"], p["gridPos"]["x"]) for p in dashboard["panels"]]
    assert positions == sorted(positions)


def test_every_panel_has_title_and_description(dashboard: dict) -> None:
    for panel in _iter_panels(dashboard["panels"]):
        assert panel.get("title"), f"panel without a title: {panel.get('id')}"
        assert panel.get("description"), f"panel without a description: {panel['title']}"


def test_dashboard_description_explains_reading_order(dashboard: dict) -> None:
    description = dashboard["description"]
    for row in [
        "Run overview",
        "Collection coverage",
        "CI evidence",
        "Run identity",
        "Tests and workers",
        "Fixture work",
        "Waits",
        "Service requests",
        "Environment",
        "Events",
    ]:
        assert row in description, f"dashboard description does not mention the {row} row"
    assert "capture endpoint" in description
    assert "Tempo search page size" in description


# ---------------------------------------------------------------------------
# Capture dashboard: one endpoint, no TraceQL
# ---------------------------------------------------------------------------


def test_every_capture_panel_reads_the_capture_endpoint(dashboard: dict) -> None:
    """Every summary, test, wait, fixture, failure and identity panel uses the
    one endpoint URL. Panels share the endpoint's cached document."""
    capture_panels = {
        "Capture state",
        "Tests",
        "Failed",
        "Result",
        "Test time",
        "Test attempts",
        "Test paths",
        "PR",
        "Workflow",
        "Wait spans",
        "Fixture setups",
        "Fixture uses",
        "Unclassified spans",
        "Test timeline",
        "Slowest tests",
        "Capture failures",
        "Fixture setup",
        "Fixture use",
        "Longest waits",
        "Code under test",
        "Selection and run",
        "Capture record",
    }
    targets = _capture_targets(dashboard)
    assert {panel["title"] for panel, _ in targets} == capture_panels
    for panel, target in targets:
        assert len(panel["targets"]) == 1, f"{panel['title']}: one URL per panel"
        assert target["url"] == CAPTURE_URL, f"{panel['title']}: wrong capture endpoint"
        assert target["source"] == "url"
        assert target["format"] == "table"
        assert target["type"] == "json"


def test_no_traceql_target_remains(dashboard: dict) -> None:
    """The Capture page reads no Tempo target at all; service calls are an
    Explore link, not a capped search posing as a count."""
    for panel in _iter_panels(dashboard["panels"]):
        datasource = (panel.get("datasource") or {}).get("uid")
        assert datasource != "ci-observe-traces", f"{panel['title']}: Tempo datasource on the Capture page"
        for target in panel.get("targets", []):
            assert target.get("queryType") != "traceql", f"{panel['title']}: TraceQL target on the Capture page"
    raw = DASHBOARD_FILE.read_text()
    assert "spss" not in raw, "the spans-per-span-set limit must not appear as a count or a target"
    assert "100 spans per trace" not in raw
    assert "30,000" not in raw


def test_every_selector_resolves_against_the_capture_document(dashboard: dict) -> None:
    """Each Infinity column names a field the endpoint document really has.

    The fixture document carries exactly the keys the read-path spec defines;
    a selector the endpoint does not return would render an empty column.
    """
    assert set(CAPTURE_DOC) == {
        "capture_id",
        "state",
        "session",
        "coverage",
        "identity",
        "tests",
        "waits",
        "fixtures",
        "fixture_uses",
        "failed_tests",
        "transient",
        "notes",
    }
    assert set(CAPTURE_DOC["session"]) == {
        "trace_id",
        "started_at",
        "duration_seconds",
        "status",
        "tests",
        "test_attempts",
        "workers",
        "span_total",
    }
    assert set(CAPTURE_DOC["coverage"]) == {
        "spans_total",
        "spans_classified",
        "spans_unclassified",
        "session_span",
        "service_calls",
    }

    targets = _capture_targets(dashboard)
    assert targets, "no panel reads the capture endpoint"
    for panel, target in targets:
        resolved = _first_row(_root_value(CAPTURE_DOC, target["root_selector"]))
        assert target["columns"], f"{panel['title']}: target selects no column"
        for column in target["columns"]:
            assert column["selector"] in resolved, (
                f"{panel['title']}: selector {column['selector']!r} is not in the capture document "
                f"root {target['root_selector']!r}"
            )


def test_coverage_counts_come_from_the_endpoint(dashboard: dict) -> None:
    """Tests, attempts, waits, fixtures and fixture uses are endpoint counts."""
    panels = _panels_by_title(dashboard)
    for title, (root, column, calcs) in COUNT_SOURCES.items():
        panel = panels[title]
        (target,) = panel["targets"]
        assert target["url"] == CAPTURE_URL, f"{title}: count must come from the capture endpoint"
        assert target["root_selector"] == root, f"{title}: wrong document root"
        assert [c["selector"] for c in target["columns"]] == [column], f"{title}: wrong count column"
        assert panel["options"]["reduceOptions"]["calcs"] == calcs, f"{title}: wrong reducer"
        assert panel["options"]["reduceOptions"]["fields"] == column, f"{title}: wrong reduced field"
        assert panel["fieldConfig"]["defaults"]["unit"] == "none", f"{title}: a count must be unitless"
    # The two scalar counts the endpoint computed itself.
    assert panels["Tests"]["targets"][0]["root_selector"] == "session"
    assert panels["Test attempts"]["targets"][0]["root_selector"] == "session"


def test_no_search_limit_or_spss_text_remains(dashboard: dict) -> None:
    """Tempo search page sizes must not survive as a bare count anywhere."""
    raw = DASHBOARD_FILE.read_text()
    assert 'limit":' not in raw.replace('"limitField"', ""), "a search limit must not remain"
    assert "spss" not in raw.lower()


def test_slowest_tests_keeps_the_twenty_longest(dashboard: dict) -> None:
    panel = _panels_by_title(dashboard)["Slowest tests"]
    pipeline = [t["id"] for t in panel["transformations"]]
    assert pipeline == ["sortBy", "limit"], pipeline
    assert panel["transformations"][1]["options"]["limitField"] == 20
    assert panel["transformations"][0]["options"]["sort"] == [{"field": "duration_s", "desc": True}]


# ---------------------------------------------------------------------------
# Capture dashboard: states and failures
# ---------------------------------------------------------------------------


def test_page_states_loading_not_indexed_and_error(dashboard: dict) -> None:
    panels = _panels_by_title(dashboard)
    state = panels["Capture state"]
    assert state["targets"][0]["root_selector"] == "$"
    assert [c["selector"] for c in state["targets"][0]["columns"]] == ["state"]
    assert "Loading" in state["fieldConfig"]["defaults"]["noValue"]
    texts = [
        option["text"]
        for mapping in state["fieldConfig"]["defaults"].get("mappings", [])
        for option in mapping.get("options", {}).values()
        if isinstance(option, dict) and "text" in option
    ]
    assert "indexed" in texts
    assert "not indexed" in texts
    assert "missing" in texts
    assert "busy" in texts
    assert "too large" in texts
    assert "unknown" in texts

    content = panels["How these counts are made"]["options"]["content"]
    assert "Loading" in content
    assert "not indexed" in content
    assert "Error" in content
    assert "unreachable" in content
    assert "no zero on this page is evidence" in content


def test_failed_tests_panel_shows_the_failed_test(dashboard: dict) -> None:
    """The failed test from the endpoint's list is visible with its message."""
    panels = _panels_by_title(dashboard)
    panel = panels["Capture failures"]
    (target,) = panel["targets"]
    assert target["url"] == CAPTURE_URL
    assert target["root_selector"] == "failed_tests"
    selectors = [c["selector"] for c in target["columns"]]
    for field in ("classname", "name", "type", "message", "time_s"):
        assert field in selectors, f"Capture failures: missing {field} column"
    failed = CAPTURE_DOC["failed_tests"][0]
    assert failed["name"] and failed["message"], "the fixture must carry a visible failed test"
    assert "JUnit evidence" in panel["description"]
    assert "failure.json" in panel["description"]

    count = panels["Failed"]
    assert count["targets"][0]["root_selector"] == "failed_tests"
    assert "JUnit evidence" in count["description"]

    # The same failure stays reachable in the CI evidence row.
    junit = panels["Failed tests"]
    assert junit["targets"][0]["url"] == RUN_EVIDENCE_URL
    assert junit["targets"][0]["root_selector"] == "native.failed_tests"


# ---------------------------------------------------------------------------
# Capture dashboard: run identity
# ---------------------------------------------------------------------------


IDENTITY_SELECTORS = {
    "Code under test": ["test_ref", "test_sha", "test_infra_ref", "test_infra_sha", "test_infra_workflow_sha"],
    "Selection and run": [
        "run_id",
        "run_attempt",
        "suite",
        "pr",
        "workflow",
        "test_suite",
        "test_filter",
        "test_marker",
    ],
    "Capture record": ["session_trace_id", "identity_source", "capture_started_at"],
}


def test_identity_values_bind_to_the_endpoint_fields(dashboard: dict) -> None:
    """Each identity table selects the registry fields by their endpoint names."""
    panels = _panels_by_title(dashboard)
    for title, fields in IDENTITY_SELECTORS.items():
        panel = panels[title]
        (target,) = panel["targets"]
        assert target["url"] == CAPTURE_URL
        assert target["root_selector"] == "identity", f"{title}: identity must come from the endpoint"
        assert [c["selector"] for c in target["columns"]] == fields, f"{title}: identity binding drifted"
        for field in fields:
            assert field in CAPTURE_DOC["identity"], f"{title}: {field} is not a registry field"
    identity_row = _row(dashboard, IDENTITY_ROW)
    assert identity_row["collapsed"] is True
    assert "identity_source" in identity_row["description"]
    assert {c["title"] for c in identity_row["panels"]} == set(IDENTITY_SELECTORS)


# ---------------------------------------------------------------------------
# Capture dashboard: CI evidence, environment, events, links
# ---------------------------------------------------------------------------


def test_ci_evidence_row_is_kept(dashboard: dict) -> None:
    row = _row(dashboard, CI_EVIDENCE_ROW)
    assert row["collapsed"] is True
    titles = {p["title"] for p in row["panels"]}
    for title in ("Run", "Jobs", "Steps", "JUnit summary", "Failed tests"):
        assert title in titles, f"CI evidence row lost the {title} panel"
    for panel in row["panels"]:
        (target,) = panel["targets"]
        assert target["url"] == RUN_EVIDENCE_URL


def test_environment_and_events_are_scoped_to_the_capture(dashboard: dict) -> None:
    environment = {id(p) for p in _iter_panels(_row(dashboard, ENVIRONMENT_ROW)["panels"])}
    events = {id(p) for p in _iter_panels(_row(dashboard, EVENTS_ROW)["panels"])}
    for panel in _iter_panels(dashboard["panels"]):
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            if not expr:
                continue
            scoped = 'osac_ci_capture_id="$capture_id"' in expr
            assert scoped, f"{panel['title']}: expression is not scoped to the capture"
            datasource = (panel.get("datasource") or {}).get("uid")
            if datasource == "ci-observe-metrics":
                assert id(panel) in environment, f"{panel['title']}: kubelet metrics belong to Environment"
            if datasource == "ci-observe-logs":
                assert id(panel) in events, f"{panel['title']}: Kubernetes events belong to Events"
    for row, label in ((ENVIRONMENT_ROW, "Environment"), (EVENTS_ROW, "Events")):
        assert "from and to" in _row(dashboard, row)["description"], (
            f"{label}: the row must say that links into the page set from/to"
        )


def test_links_carry_the_time_range(dashboard: dict) -> None:
    assert dashboard["links"], "the Capture page needs links back to the entry pages"
    for link in dashboard["links"]:
        assert "from=${__from}" in link["url"] and "to=${__to}" in link["url"], (
            f"{link['title']}: a link into a time-scoped page must carry from/to"
        )


def test_service_calls_are_sampled_and_link_to_explore(dashboard: dict) -> None:
    panel = _panels_by_title(dashboard)["Service calls are sampled"]
    content = panel["options"]["content"]
    assert "/explore?" in content, "service calls must be readable in Explore"
    assert "sampled" in content.lower()
    assert "no total" in content.lower()
    assert "$capture_id" in content, "the Explore query must open the current capture"
    assert "not test failures" in content
    assert "no count" in content.lower() or "never a complete count" in content.lower()


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
