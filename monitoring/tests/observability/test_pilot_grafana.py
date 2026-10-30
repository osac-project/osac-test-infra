"""Check the read-only pilot through an actual Grafana instance.

These tests never fetch GitHub, never write the shared dashboard, and
never need a browser: they use Grafana's read APIs and the anonymous
Viewer role only.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

from monitoring.ci_observability.pilot_report import build_report

PROVIDER_RELOAD_TIMEOUT_SECONDS = 40
PROVIDER_POLL_SECONDS = 2


@dataclass(frozen=True, kw_only=True)
class Pilot:
    url: str
    dashboard: dict[str, object]
    snapshot: dict[str, object]


@pytest.fixture(scope="module")
def report(*, pilot: Pilot) -> dict[str, object]:
    return build_report(snapshot=pilot.snapshot)


@pytest.fixture(scope="module")
def pilot() -> Pilot:
    names = (
        "CI_OBSERVABILITY_PILOT_GRAFANA_URL",
        "CI_OBSERVABILITY_PILOT_DASHBOARD",
        "CI_OBSERVABILITY_PILOT_SNAPSHOT",
    )
    values = [os.environ.get(name) for name in names]
    if not any(values):
        pytest.skip("local Grafana pilot is not configured")
    assert all(values), "all three pilot environment settings are required"
    url = str(values[0]).rstrip("/")
    assert re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", url), "pilot tests require loopback HTTP"
    dashboard = json.loads(Path(str(values[1])).read_text())
    snapshot = json.loads(Path(str(values[2])).read_text())
    return Pilot(url=url, dashboard=dashboard, snapshot=snapshot)


def request_json(*, url: str, data: dict[str, object] | None = None) -> dict[str, object]:
    encoded = None if data is None else json.dumps(data).encode()
    request = urllib.request.Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/json", "Origin": url.split("/api/")[0]},
        method="GET" if data is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read(32 * 1024 * 1024 + 1)
    assert len(body) <= 32 * 1024 * 1024, "Grafana response exceeded the test bound"
    result = json.loads(body)
    assert isinstance(result, dict)
    return result


def inline_data_signature(*, dashboard: dict[str, object]) -> list[tuple[str, str, str]]:
    signature: list[tuple[str, str, str]] = []
    for panel in dashboard["panels"]:
        signature.append((panel["title"], panel["type"], json.dumps(panel, sort_keys=True)))
    return signature


def fetch_served_dashboard(*, pilot: Pilot) -> dict[str, object]:
    result = request_json(url=f"{pilot.url}/api/dashboards/uid/ci-run-pilot")
    served = result["dashboard"]
    assert isinstance(served, dict)
    return served


def test_pilot_dashboard_is_served_and_current(*, pilot: Pilot) -> None:
    deadline = time.monotonic() + PROVIDER_RELOAD_TIMEOUT_SECONDS
    while True:
        try:
            served = fetch_served_dashboard(pilot=pilot)
            if inline_data_signature(dashboard=served) == inline_data_signature(dashboard=pilot.dashboard):
                break
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        if time.monotonic() >= deadline:
            pytest.fail("served pilot dashboard is stale or missing")
        time.sleep(PROVIDER_POLL_SECONDS)
    assert served["uid"] == "ci-run-pilot"
    assert served["editable"] is False


def test_signature_detects_as_of_text_and_display_config_changes() -> None:
    earlier = {
        "panels": [
            {"title": "", "type": "text", "options": {"content": "Snapshot taken at 08:00Z"}},
            {
                "title": "Job span",
                "type": "stat",
                "targets": [{"refId": "A", "data": '[{"value":"118.40 min"}]'}],
                "options": {"reduceOptions": {"fields": "Span"}},
            },
        ]
    }
    later = json.loads(json.dumps(earlier))
    assert inline_data_signature(dashboard=earlier) == inline_data_signature(dashboard=later)
    later["panels"][0]["options"]["content"] = "Snapshot taken at 09:00Z"
    assert inline_data_signature(dashboard=earlier) != inline_data_signature(dashboard=later)
    later = json.loads(json.dumps(earlier))
    later["panels"][1]["options"]["reduceOptions"]["fields"] = "Wrong field"
    assert inline_data_signature(dashboard=earlier) != inline_data_signature(dashboard=later)


def test_pilot_queries_match_report_rows(*, pilot: Pilot, report: dict[str, object]) -> None:
    expected_counts = {
        "Jobs": len(report["jobs"]),
        "All steps (slowest first)": len(report["steps"]),
        "Artifacts and evidence": len(report["artifacts"]),
        "Failure observations": len(report["failures"]["steps"]) + len(report["failures"]["jobs"]),
    }
    panels = pilot.dashboard["panels"]
    titles = {panel["title"] for panel in panels}
    assert {"Jobs", "All steps (slowest first)"}.issubset(titles)
    stat_panels = [panel for panel in panels if panel["type"] == "stat"]
    assert len(stat_panels) == 4, "dashboard must have four stat cards"
    assert sum(panel["type"] == "barchart" for panel in panels) == 1

    stat_query_count = 0
    non_stat_query_count = 0
    for panel in panels:
        if panel["type"] == "text":
            continue
        targets = panel["targets"]
        assert len(targets) == 1, f"panel {panel['title']} needs one query"
        if panel["type"] == "stat":
            expected_count = 1
            assert panel["datasource"]["uid"] == "ci-pilot-inline"
        elif panel["type"] == "barchart":
            expected_count = len(report["slowest_steps"])
        else:
            assert panel["type"] == "table"
            expected_count = expected_counts[panel["title"]]
        for target in targets:
            assert target["source"] == "inline", "pilot must not request arbitrary URLs"
            assert target["url"] == ""
            assert target["parser"] == "backend"
            rows = json.loads(target["data"])
            assert isinstance(rows, list)
            assert len(rows) == expected_count, panel["title"]
            query = dict(target)
            query["datasource"] = {"type": "yesoreyeram-infinity-datasource", "uid": "ci-pilot-inline"}
            result = request_json(url=f"{pilot.url}/api/ds/query", data={"from": "0", "to": "1", "queries": [query]})[
                "results"
            ][query["refId"]]
            assert result["status"] == 200, result.get("error")
            assert "error" not in result
            frames = result["frames"]
            actual_rows = 0
            for frame in frames:
                columns = frame["data"]["values"]
                assert columns, "a returned frame must contain its configured columns"
                lengths = {len(column) for column in columns}
                assert len(lengths) == 1
                actual_rows += len(columns[0])
            assert actual_rows == len(rows), panel["title"]
            if panel["type"] == "stat":
                stat_query_count += 1
                fields = panel["options"]["reduceOptions"]["fields"]
                frame_fields = []
                for frame in frames:
                    frame_fields.extend(field["name"] for field in frame["schema"]["fields"])
                assert fields in frame_fields, (
                    f"stat panel {panel['title']}: reduceOptions field {fields!r} not in frame schema {frame_fields}"
                )
            else:
                non_stat_query_count += 1
    assert stat_query_count == 4, "all four stat cards must have actual queried targets"
    assert non_stat_query_count >= 2, "pilot needs the jobs and steps data panels"


def test_pilot_data_source_refuses_url_queries(*, pilot: Pilot) -> None:
    query = {
        "refId": "URL",
        "datasource": {"type": "yesoreyeram-infinity-datasource", "uid": "ci-pilot-inline"},
        "type": "json",
        "source": "url",
        "format": "table",
        "parser": "backend",
        "url": f"{pilot.url}/api/health",
        "columns": [{"selector": "version", "text": "Version", "type": "string"}],
        "root_selector": "",
    }
    try:
        result = request_json(url=f"{pilot.url}/api/ds/query", data={"from": "0", "to": "1", "queries": [query]})
    except urllib.error.HTTPError as error:
        assert error.code == 400
        result = json.load(error)
    denied = result["results"]["URL"]
    assert denied["status"] == 500
    assert "requested URL not allowed" in denied["error"]


def test_pilot_exposes_only_a_viewer(*, pilot: Pilot) -> None:
    payload = {"dashboard": {"uid": "ci-pilot-write-probe", "title": "Write must be denied", "panels": []}}
    with pytest.raises(urllib.error.HTTPError) as error:
        request_json(url=f"{pilot.url}/api/dashboards/db", data=payload)
    assert error.value.code in (401, 403), "anonymous pilot access must be read-only"
