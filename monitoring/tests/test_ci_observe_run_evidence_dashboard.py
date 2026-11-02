"""Structural tests for the CI Observe run-evidence dashboard.

monitoring/config/grafana/dashboards/ci-observe-run-evidence.json queries
the workflow exporter's /api/ci-observe/run endpoint through the Infinity
datasource (uid workflow-api). These tests pin the capture_id variable, the
panel structure, the native-status copy, the back-links to the run detail
and home dashboards, and that every root_selector/columns pair resolves
against ENDPOINT_DOCUMENT -- the endpoint's response contract, pinned here
by hand so this file passes in a tree without the backend change.

The backend PR (workflow-exporter.py + test_exporter_ci_observe_run.py)
pins the same contract from the other side: its
test_query_shape_resolves_against_the_endpoint_response resolves the real
endpoint's response against this dashboard's queries, and skips until the
dashboard JSON exists in its tree. Keep ENDPOINT_DOCUMENT and the backend
in sync when the response shape changes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

DASHBOARDS_DIR = Path(__file__).resolve().parents[1] / "config" / "grafana" / "dashboards"
DASHBOARD_FILE = DASHBOARDS_DIR / "ci-observe-run-evidence.json"

DATASOURCE = {"type": "yesoreyeram-infinity-datasource", "uid": "workflow-api"}
ENDPOINT = "http://127.0.0.1:9103/api/ci-observe/run?capture_id=${capture_id:percentencode}"
RUN_DETAIL_LINK = "/d/ci-observe-runs/ci-observe-e28094-run-investigation?var-capture_id=${capture_id}"
HOME_LINK = "/d/ci-observe-runs-home/ci-observe-e28094-test-runs"

# Every native status the endpoint can return, with the color it must show:
# only "available" is green, so a missing or expired artifact can never be
# mistaken for a passing run.
NATIVE_STATUS_COLORS = {
    "available": "green",
    "missing": "red",
    "expired": "orange",
    "too_large": "red",
    "invalid": "red",
    "not_requested": "blue",
}

# What every data panel shows in place of a value when the query could not
# load data -- the undeployed-backend case the top warning panel explains.
NOT_DEPLOYED = "Not deployed or unavailable"

# The endpoint's response contract: one realistic document with every field
# the dashboard's tables read. Counts are null (never 0) whenever the
# status is not "available". This is the dashboard-side half of the
# contract; the backend PR asserts its real response against this
# dashboard's queries.
ENDPOINT_DOCUMENT = {
    "capture_id": "vmaas-main-41310072671-a1",
    "parsed": "exact",
    "run_id": 41310072671,
    "run_attempt": 1,
    "run": {
        "repo": "osac",
        "workflow": "E2E VMaaS Full Install",
        "conclusion": "failure",
        "status": "failure",
        "duration_s": 5412,
        "url": "https://github.com/osac-project/osac/actions/runs/41310072671",
        "runner_name": "osac-7-runner-02",
        "head_sha": "9f1c2ab37d54e6f0a1b2c3d4e5f60718293a4b5c",
        "branch": "main",
        "pr_display": "",
        "created_at": "2026-10-31T10:00:00Z",
        "run_attempt": 1,
    },
    "jobs": [
        {
            "name": "osac / E2E VMaaS Full Install",
            "workflow": "E2E VMaaS Full Install",
            "conclusion": "failure",
            "duration_s": 5412,
            "failed_step": "e2e → Run E2E tests",
            "failure_reason": "test",
            "steps": [{"name": "Run E2E tests", "duration_s": 245}],
            "url": "https://github.com/osac-project/osac/actions/runs/41310072671",
        }
    ],
    "steps": [
        {
            "job_name": "osac / E2E VMaaS Full Install",
            "step_name": "Run E2E tests",
            "duration_s": 245,
            "conclusion": "failure",
        }
    ],
    "native": {
        "status": "available",
        "artifact_name": "osac-logs-full-install-41310072671",
        "artifact_url": "https://github.com/osac-project/osac/actions/runs/41310072671/artifacts/42",
        "tests": 4,
        "failures": 1,
        "errors": 1,
        "skipped": 1,
        "time_s": 245.6,
        "failed_tests": [
            {
                "classname": "tests/e2e/networking/vmaas",
                "name": "test_vm_restart",
                "type": "AssertionError",
                "message": "assert VMI phase == Running, got Failed",
                "time_s": 60.1,
                "artifact_url": "https://github.com/osac-project/osac/actions/runs/41310072671/artifacts/42",
            }
        ],
        "notes": ["2 artifacts matched; used osac-logs-full-install-41310072671"],
    },
    "coverage": {
        "job_data": "GitHub job history stored by this workflow exporter (SQLite, 60-day retention)",
        "native_data": "JUnit XML parsed on demand from the run's osac-logs-[<suite>-]full-install-"
        "<run id> workflow artifact (GitHub artifact retention is 7 days)",
        "capture_identity": "capture id format <suite>-<prN|main>-<run id>-a<attempt>: run 41310072671 attempt 1",
        "notes": [],
    },
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
    """Panels that query the endpoint (rows group, text explains)."""
    return [p for p in _iter_panels(dashboard["panels"]) if p["type"] not in ("row", "text")]


def _panels_by_title(dashboard: dict) -> dict[str, dict]:
    return {p["title"]: p for p in _iter_panels(dashboard["panels"])}


def _override(panel: dict, field: str) -> dict | None:
    for entry in panel["fieldConfig"]["overrides"]:
        if entry["matcher"]["options"] == field:
            return entry
    return None


def _mapping_options(panel: dict, field: str) -> dict[str, str]:
    """The {status value: color} map a field's value mappings define."""
    override = _override(panel, field)
    assert override is not None, f"no override for field {field}"
    mappings = next(p["value"] for p in override["properties"] if p["id"] == "mappings")
    (mapping,) = mappings
    assert mapping["type"] == "value"
    return {value: options["color"] for value, options in mapping["options"].items()}


def test_uid_and_title(dashboard: dict) -> None:
    assert dashboard["uid"] == "ci-observe-run-evidence"
    assert dashboard["title"] == "CI Observe — Run evidence"
    assert dashboard["schemaVersion"] == 39
    assert dashboard["version"] == 1


def test_capture_id_variable_is_an_empty_textbox(dashboard: dict) -> None:
    (variable,) = dashboard["templating"]["list"]
    assert variable["name"] == "capture_id"
    assert variable["type"] == "textbox"
    assert variable["current"]["value"] == ""


def test_sections_in_reading_order(dashboard: dict) -> None:
    rows = [p["title"] for p in dashboard["panels"] if p["type"] == "row"]
    assert rows == ["Run and job summary", "Job and step timing", "Native test result", "Failed tests and evidence"]
    text = [p["title"] for p in dashboard["panels"] if p["type"] == "text"]
    assert text == ["If the native panels are empty", "Coverage and limits"]
    positions = [(p["gridPos"]["y"], p["gridPos"]["x"]) for p in dashboard["panels"]]
    assert positions == sorted(positions)


def test_warning_panel_comes_first(dashboard: dict) -> None:
    """The undeployed-backend warning is the first thing on the page, above
    every data panel, so a newcomer cannot mistake an unreachable backend
    for a passing or empty run."""
    warning = dashboard["panels"][0]
    assert warning["type"] == "text"
    assert warning["title"] == "If the native panels are empty"
    assert warning["gridPos"]["y"] == 0
    assert warning["gridPos"]["w"] == 24
    for panel in dashboard["panels"][1:]:
        assert panel["gridPos"]["y"] >= warning["gridPos"]["y"] + warning["gridPos"]["h"], (
            f"{panel['title']} overlaps the warning panel"
        )
    content = warning["options"]["content"]
    assert "not deployed or unreachable" in content
    assert NOT_DEPLOYED in content
    assert "was not loaded" in content
    assert "not a passing result" in content
    assert "workflow-exporter endpoint" in content
    # Blank panels are part of the undeployed state too: Grafana does not
    # replay an errored query to panels that mount after it failed, so
    # far-down tables render no content at all.
    assert "no content at all" in content


def test_data_panels_say_not_deployed_rather_than_na(dashboard: dict) -> None:
    """With the backend undeployed, every data panel's empty values read as
    an unavailable backend, not a bare n/a. The failed-tests table has two
    honest empty meanings, so it points at the JUnit Status row instead of
    claiming either one."""
    for panel in _content_panels(dashboard):
        no_value = panel["fieldConfig"]["defaults"]["noValue"]
        if panel["title"] == "Failed tests":
            assert "No failed tests" in no_value and "JUnit summary Status" in no_value
            continue
        assert no_value == NOT_DEPLOYED, panel["title"]


def test_null_values_that_mean_something_else_are_overridden(dashboard: dict) -> None:
    """Two fields are null even when the backend is up, so they must not
    claim the backend is down: JUnit counts (null when the artifact is
    missing/expired/too large/invalid -- the Status cell says which) point
    at the status instead, and a step conclusion that was not recorded
    stays empty."""
    junit = _panels_by_title(dashboard)["JUnit summary"]
    for field in ("tests", "failures", "errors", "skipped", "time_s"):
        override = _override(junit, field)
        assert override is not None, f"JUnit summary: no override for {field}"
        no_value = next(p["value"] for p in override["properties"] if p["id"] == "noValue")
        assert no_value == "no result (see Status)"

    steps = _panels_by_title(dashboard)["Steps"]
    override = _override(steps, "conclusion")
    assert override is not None
    properties = {prop["id"]: prop["value"] for prop in override["properties"]}
    assert properties["noValue"] == ""


def test_coverage_panel_is_tall_enough(dashboard: dict) -> None:
    """The coverage markdown was clipped at 1440 and 1280 (498-564px of
    content in a 246px box); the panel is now tall enough to hold it
    without scrolling (verified with scrollHeight <= clientHeight against
    the live dashboard)."""
    coverage = _panels_by_title(dashboard)["Coverage and limits"]
    assert coverage["gridPos"]["h"] >= 20


def test_every_panel_has_title_and_description(dashboard: dict) -> None:
    for panel in _iter_panels(dashboard["panels"]):
        assert panel.get("title"), f"panel without a title: {panel.get('id')}"
        assert panel.get("description"), f"panel without a description: {panel['title']}"


def test_content_panels_use_the_workflow_api_datasource(dashboard: dict) -> None:
    panels = _content_panels(dashboard)
    assert panels, "dashboard has no content panels"
    for panel in panels:
        assert panel["datasource"] == DATASOURCE, panel["title"]


def test_every_target_queries_the_run_endpoint(dashboard: dict) -> None:
    targets = [t for p in _content_panels(dashboard) for t in p.get("targets", [])]
    assert targets, "dashboard has no query targets"
    for target in targets:
        assert target["url"] == ENDPOINT
        assert target["url_options"] == {"method": "GET", "data": ""}
        assert (target["type"], target["source"], target["format"]) == ("json", "url", "table")
        assert target["root_selector"], "a table target needs a root_selector"


def test_object_roots_use_jsonpath(dashboard: dict) -> None:
    """Infinity replaces a plain object root with its first nested array.
    `native` contains `failed_tests` and `notes`, so the shipped
    root_selector `native` rendered zero rows: the empty `failed_tests`
    array became the dataset. A `$`-prefixed selector takes the JSONPath
    branch, which keeps the object as one row."""
    junit = _panels_by_title(dashboard)["JUnit summary"]
    assert junit["targets"][0]["root_selector"] == "$.native"


def test_native_status_is_never_a_green_zero(dashboard: dict) -> None:
    panel = _panels_by_title(dashboard)["JUnit summary"]
    assert _mapping_options(panel, "status") == NATIVE_STATUS_COLORS
    # Counts the endpoint could not fill never render as a number: with the
    # backend down they say so, with a gap they point at the status.
    assert panel["fieldConfig"]["defaults"]["noValue"] == NOT_DEPLOYED
    selectors = [c["selector"] for c in panel["targets"][0]["columns"]]
    for count in ("tests", "failures", "errors", "skipped", "time_s"):
        assert count in selectors


def test_timing_copy_says_steps_are_not_runner_minutes(dashboard: dict) -> None:
    row = _panels_by_title(dashboard)["Job and step timing"]
    assert "not runner minutes" in row["description"]
    assert "skipped or cancelled job has no steps" in row["description"]


def test_failed_tests_copy_is_evidence_not_diagnosis(dashboard: dict) -> None:
    row = _panels_by_title(dashboard)["Failed tests and evidence"]
    assert "not a root-cause diagnosis" in row["description"]
    panel = _panels_by_title(dashboard)["Failed tests"]
    assert "stack traces are never included" in panel["description"]
    assert panel["targets"][0]["root_selector"] == "native.failed_tests"


def test_coverage_text_panel(dashboard: dict) -> None:
    content = _panels_by_title(dashboard)["Coverage and limits"]["options"]["content"]
    assert "60-day retention" in content
    assert "7 days" in content
    assert "No automatic root-cause diagnosis" in content
    for status in NATIVE_STATUS_COLORS:
        assert f"`{status}`" in content, f"coverage text does not explain {status}"
    # The undeployed-backend display state is documented too, explicitly not
    # an endpoint status, including the blank-panel case.
    assert "`unavailable`" in content
    assert NOT_DEPLOYED in content
    assert "not a status the endpoint returns" in content
    assert "no content at all" in content


def test_back_links(dashboard: dict) -> None:
    urls = {link["url"] for link in dashboard["links"]}
    assert RUN_DETAIL_LINK in urls
    assert HOME_LINK in urls


def test_no_raw_trace_ids_or_secrets(dashboard: dict) -> None:
    raw = DASHBOARD_FILE.read_text()
    for banned in ("PRIVATE_GITHUB_TOKEN", "Authorization", "ghp_", "Bearer ", "Bearer:"):
        assert banned not in raw
    assert not re.findall(r"\b[0-9a-f]{32}\b", raw), "raw trace id in dashboard"


def test_no_leftover_template_tokens(dashboard: dict) -> None:
    variables = {v["name"] for v in dashboard["templating"]["list"]}
    token = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
    for panel in _iter_panels(dashboard["panels"]):
        for target in panel.get("targets", []):
            for match in token.findall(target["url"]):
                assert match in variables or GRAFANA_BUILTIN_VARIABLE.match(match), (
                    f"unresolved template token {match!r} in {panel['title']}"
                )


def test_query_shape_resolves_against_the_endpoint_contract(dashboard: dict) -> None:
    """Every root_selector and column selector must resolve in the endpoint
    contract, so the dashboard cannot drift from the response shape."""
    for panel in _content_panels(dashboard):
        for target in panel["targets"]:
            node = ENDPOINT_DOCUMENT
            # Object roots use a JSONPath so Infinity keeps them as one row.
            selector = target["root_selector"]
            if selector.startswith("$."):
                selector = selector[2:]
            for part in selector.split("."):
                node = node[part]  # KeyError means the contract drifted
            rows = node if isinstance(node, list) else [node]
            assert rows, f"{panel['title']}: {target['root_selector']} is empty"
            for column in target["columns"]:
                assert column["selector"] in rows[0], (
                    f"{panel['title']}: column {column['selector']} is not in the endpoint contract"
                )
