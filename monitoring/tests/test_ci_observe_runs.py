"""The ci-observe runs data path: /api/ci-observe/runs and the Tempo join.

The workflow exporter knows every E2E run but stores no capture id; Tempo
knows every capture but not the runs that never sent one. This endpoint joins
them on the GitHub run id and answers the front door's question per run:
capture, not collected, or unknown.

These tests pin the join (both directions), and above all the rule that keeps
a data problem from becoming a false verdict: Tempo down, a truncated search,
an unreadable response, a run too fresh for the last lookup, or a run older
than the capture retention are all `unknown`, never `not collected`. They also
pin the bounded lookup (one Tempo query per minute, window and limit clamps)
and the deployment wiring the container needs to reach the host's Tempo.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import pytest

from helpers import make_job

MONITORING = Path(__file__).resolve().parents[1]
DASHBOARD = MONITORING / "config" / "grafana" / "dashboards" / "ci-observe-runs-home.json"
RUN_ID = 41310072671
SECOND_RUN_ID = 41310072672
CAPTURE = f"vmaas-main-{RUN_ID}-a1"


def iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_job(run_id=RUN_ID, **overrides) -> dict:
    """A run-level E2E row: the id is the GitHub run id and the url has no
    /job/ segment, which is what the endpoint lists."""
    row = make_job(
        id=run_id,
        workflow="E2E VMaaS Full Install",
        display_name="osac / E2E VMaaS Full Install",
        category="e2e",
        trigger="PR #123",
        conclusion="success",
        created_at=iso(60),
        url=f"https://github.com/osac-project/osac/actions/runs/{run_id}",
    )
    row.update(overrides)
    return row


def tempo_trace(capture_id=CAPTURE, run_id=RUN_ID, *, scoped=True, with_attributes=True):
    """One /api/search hit as Tempo 2.10 returns it: matched spans under
    spanSets, attributes as KeyValue pairs."""
    attributes = []
    if with_attributes:
        prefix = "resource." if scoped else ""
        attributes.append({
            "key": f"{prefix}osac.ci.capture_id",
            "value": {"stringValue": capture_id},
        })
        if run_id is not None:
            attributes.append({
                "key": f"{prefix}osac.ci.run_id",
                "value": {"stringValue": str(run_id)},
            })
    return {
        "traceID": "a" * 32,
        "rootServiceName": "tests",
        "rootTraceName": "test run",
        "spanSets": [{"matched": 1, "spans": [{"spanID": "b" * 16, "attributes": attributes}]}],
    }


class FakeTempo:
    """Stands in for Tempo's /api/search: one payload, recorded requests."""

    def __init__(self, payload=None, status=200):
        self.payload = {"traces": []} if payload is None else payload
        self.status = status
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append(dict(parse_qsl(urlparse(self.path).query)))
                body = json.dumps(outer.payload).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def tempo():
    """Tempo answering with one capture for RUN_ID."""
    server = FakeTempo({"traces": [tempo_trace()]})
    yield server
    server.close()


@pytest.fixture
def client(runs, exporter, tempo):
    """The endpoint on the exporter's real job history, pointed at the fake
    Tempo."""
    return runs.CIObserveRuns(exporter, tempo_url=tempo.url)


@pytest.fixture
def jobs(exporter):
    for row in (
        run_job(RUN_ID),
        run_job(SECOND_RUN_ID, trigger="scheduled", conclusion="failure"),
    ):
        exporter._upsert_job(row)
    return exporter


# ---------------------------------------------------------------------------
# The join
# ---------------------------------------------------------------------------
def test_a_run_with_a_capture_is_marked_capture(client, jobs):
    doc = client.get_json({"days": ["7"]})

    states = {run["id"]: run["capture_state"] for run in doc["runs"]}
    assert states == {str(RUN_ID): "capture", str(SECOND_RUN_ID): "not collected"}
    captured = next(run for run in doc["runs"] if run["id"] == str(RUN_ID))
    assert captured["capture_ids"] == [CAPTURE]
    assert captured["capture_reason"] == ""
    assert doc["tempo"]["state"] == "ok"
    assert doc["tempo"]["captures"] == 1


def test_the_run_id_attribute_wins_over_the_one_in_the_capture_id(client, jobs, tempo):
    """The capture id embeds the run it was made for, and so does the run id
    attribute. They agree in production; when they do not, Tempo's own
    attribute decides which run the capture belongs to."""
    tempo.payload = {"traces": [tempo_trace(capture_id="vmaas-main-111-a1", run_id=RUN_ID)]}

    doc = client.get_json({"days": ["7"]})

    states = {run["id"]: run["capture_state"] for run in doc["runs"]}
    assert states[str(RUN_ID)] == "capture"
    assert states[str(SECOND_RUN_ID)] == "not collected"


def test_a_capture_id_found_only_in_the_trace_text_still_joins(runs, exporter, jobs):
    """Tempo's response shape varies between versions; a capture id is
    self-describing, so it is read wherever it appears in the hit."""
    server = FakeTempo({"traces": [{"traceID": "c" * 32, "rootTraceName": CAPTURE}]})
    try:
        doc = runs.CIObserveRuns(exporter, tempo_url=server.url).get_json({"days": ["7"]})
    finally:
        server.close()

    assert next(run for run in doc["runs"] if run["id"] == str(RUN_ID))["capture_state"] == "capture"


def test_a_capture_missing_the_session_span_is_still_a_capture(runs, exporter, jobs):
    """The query must not select only the session span: a partial capture is
    a capture, and calling it not collected is exactly the false verdict the
    model forbids."""
    server = FakeTempo({"traces": [tempo_trace()]})
    try:
        assert runs.CIObserveRuns.CAPTURE_QUERY == '{ resource."osac.ci.capture_id" =~ ".+" }'
        doc = runs.CIObserveRuns(exporter, tempo_url=server.url).get_json({"days": ["7"]})
    finally:
        server.close()

    states = {run["id"]: run["capture_state"] for run in doc["runs"]}
    assert states[str(RUN_ID)] == "capture"


def test_job_rows_are_not_listed_as_runs(client, exporter):
    """A pull-request-flow run's suite rows are jobs: their ids never appear
    in a capture id, so listing them would invent not collected rows."""
    exporter._upsert_job(run_job(RUN_ID))
    exporter._upsert_job(make_job(
        id=41310072001,
        workflow="E2E vmaas (pull requests)",
        category="e2e",
        created_at=iso(30),
        url=f"https://github.com/osac-project/osac/actions/runs/{RUN_ID}/job/41310072001",
    ))

    doc = client.get_json({"days": ["7"]})

    assert [run["id"] for run in doc["runs"]] == [str(RUN_ID)]


# ---------------------------------------------------------------------------
# Never a fake verdict
# ---------------------------------------------------------------------------
def test_tempo_down_is_unknown_never_not_collected(runs, exporter, jobs):
    """A refused connection, the shape a network mistake takes, must not turn
    every run into "not collected"."""
    client = runs.CIObserveRuns(exporter, tempo_url="http://127.0.0.1:1")

    doc = client.get_json({"days": ["7"]})

    assert {run["capture_state"] for run in doc["runs"]} == {"unknown"}
    assert doc["tempo"]["state"] == "unknown"
    assert "never as not collected" in doc["tempo"]["note"]
    assert all("did not answer" in run["capture_reason"] for run in doc["runs"])


def test_an_http_error_from_tempo_is_unknown(runs, exporter, jobs):
    server = FakeTempo(status=503, payload={"error": "unavailable"})
    try:
        doc = runs.CIObserveRuns(exporter, tempo_url=server.url).get_json({"days": ["7"]})
    finally:
        server.close()

    assert {run["capture_state"] for run in doc["runs"]} == {"unknown"}
    assert doc["tempo"]["state"] == "unknown"


def test_traces_without_readable_capture_ids_are_unknown(runs, exporter, jobs):
    """Tempo answered but this code could not read the ids: a shape change.
    Reporting not collected would blame CI for our bug."""
    server = FakeTempo({"traces": [tempo_trace(with_attributes=False, capture_id="")]})
    try:
        doc = runs.CIObserveRuns(exporter, tempo_url=server.url).get_json({"days": ["7"]})
    finally:
        server.close()

    assert {run["capture_state"] for run in doc["runs"]} == {"unknown"}
    assert doc["tempo"]["state"] == "unreadable"
    assert "response shape may have changed" in doc["tempo"]["note"]


def test_a_truncated_capture_query_is_unknown(runs, exporter, jobs, monkeypatch):
    """Tempo's search limit counts traces, so a full response means captures
    may be missing. An absent capture then proves nothing."""
    server = FakeTempo({"traces": [tempo_trace()]})
    try:
        client = runs.CIObserveRuns(exporter, tempo_url=server.url)
        monkeypatch.setattr(client, "TEMPO_MAX_TRACES", 1)
        doc = client.get_json({"days": ["7"]})
    finally:
        server.close()

    assert doc["tempo"]["state"] == "truncated"
    states = {run["id"]: run["capture_state"] for run in doc["runs"]}
    assert states == {str(RUN_ID): "capture", str(SECOND_RUN_ID): "unknown"}


def test_a_run_newer_than_the_lookup_is_unknown(client, exporter):
    exporter._upsert_job(run_job(RUN_ID))
    exporter._upsert_job(run_job(SECOND_RUN_ID, created_at=iso(0.5)))

    doc = client.get_json({"days": ["7"]})

    fresh = next(run for run in doc["runs"] if run["id"] == str(SECOND_RUN_ID))
    assert fresh["capture_state"] == "unknown"
    assert "too early to tell" in fresh["capture_reason"]


def test_a_run_older_than_the_capture_retention_is_unknown(client, exporter):
    """Tempo keeps 30 days. A 45-day-old run's capture may have expired, so
    its absence is not evidence."""
    exporter._upsert_job(run_job(SECOND_RUN_ID, created_at=iso(45 * 24 * 60)))

    doc = client.get_json({"days": ["60"]})

    assert doc["runs"][0]["capture_state"] == "unknown"
    assert "predates the capture retention" in doc["runs"][0]["capture_reason"]


def test_a_run_before_the_live_tempo_cutover_is_unknown(client, exporter):
    """Before 2026-11-01 captures went to the archive, which this endpoint
    does not query."""
    exporter._upsert_job(run_job(SECOND_RUN_ID, created_at="2026-10-20T10:00:00Z"))

    doc = client.get_json({"days": ["60"]})

    assert doc["runs"][0]["capture_state"] == "unknown"
    assert "live Tempo cutover" in doc["runs"][0]["capture_reason"]


# ---------------------------------------------------------------------------
# Bounds: window, limit, one Tempo query per minute
# ---------------------------------------------------------------------------
def test_the_window_is_sent_to_tempo(client, jobs, tempo):
    before = datetime.now(timezone.utc)

    client.get_json({"days": ["3"]})

    request = tempo.requests[0]
    start = datetime.fromtimestamp(int(request["start"]), tz=timezone.utc)
    end = datetime.fromtimestamp(int(request["end"]), tz=timezone.utc)
    assert request["q"] == client.CAPTURE_QUERY
    assert request["limit"] == str(client.TEMPO_MAX_TRACES)
    assert abs((before - timedelta(days=3)) - start) < timedelta(seconds=30)
    assert abs(before - end) < timedelta(seconds=30)


@pytest.mark.parametrize(
    ("params", "expected_days", "expected_limit"),
    [
        ({}, 7, 200),
        ({"days": ["0"]}, 1, 200),
        ({"days": ["9999"]}, 60, 200),
        ({"days": ["nonsense"]}, 7, 200),
        ({"limit": ["0"]}, 7, 1),
        ({"limit": ["999999"]}, 7, 2000),
        ({"limit": ["nonsense"]}, 7, 200),
    ],
)
def test_days_and_limit_are_clamped(client, jobs, params, expected_days, expected_limit):
    doc = client.get_json(params)

    assert doc["days"] == expected_days
    assert 0 < len(doc["runs"]) <= expected_limit


def test_limit_bounds_the_number_of_runs(client, exporter):
    for index in range(5):
        exporter._upsert_job(run_job(RUN_ID + index))

    doc = client.get_json({"days": ["7"], "limit": ["2"]})

    assert len(doc["runs"]) == 2


def test_suite_rows_do_not_starve_the_run_list(client, exporter):
    """The history returns the newest e2e rows first, and a pull-request flow
    adds a job row per suite. Those are not runs, so the fetch asks for more
    rows than the page needs; without that, a busy PR window would show an
    almost empty run list."""
    for index in range(8):
        exporter._upsert_job(make_job(
            id=41310072000 + index,
            workflow="E2E vmaas (pull requests)",
            category="e2e",
            created_at=iso(1 + index),
            url=f"https://github.com/osac-project/osac/actions/runs/{RUN_ID}/job/{41310072000 + index}",
        ))
    exporter._upsert_job(run_job(RUN_ID, created_at=iso(30)))
    exporter._upsert_job(run_job(SECOND_RUN_ID, created_at=iso(40)))

    doc = client.get_json({"days": ["7"], "limit": ["2"]})

    assert [run["id"] for run in doc["runs"]] == [str(RUN_ID), str(SECOND_RUN_ID)]


def test_the_capture_map_is_cached_for_a_minute(client, jobs, tempo):
    first = client.get_json({"days": ["7"]})
    second = client.get_json({"days": ["7"]})

    assert len(tempo.requests) == 1, "one Tempo query serves repeated dashboard loads"
    assert first["tempo"]["cached"] is False
    assert second["tempo"]["cached"] is True
    assert second["runs"] == first["runs"]


def test_the_cache_expires(client, jobs, tempo):
    client.get_json({"days": ["7"]})
    client._cache["expires_at"] = 0

    client.get_json({"days": ["7"]})

    assert len(tempo.requests) == 2


def test_the_cache_is_per_window(client, jobs, tempo):
    client.get_json({"days": ["7"]})
    client.get_json({"days": ["30"]})

    assert len(tempo.requests) == 2, "a 30-day request cannot be served by a 7-day capture map"


def test_an_empty_job_history_still_answers(client, tempo):
    doc = client.get_json({"days": ["7"]})

    assert doc["runs"] == []
    assert doc["tempo"]["state"] == "ok"
    assert tempo.requests == [], "no runs means no reason to query Tempo"
    assert "no E2E runs" in doc["notes"][0]


# ---------------------------------------------------------------------------
# Wiring: the exporter method and the HTTP route
# ---------------------------------------------------------------------------
def test_the_exporter_exposes_the_endpoint(exporter, jobs, tempo, monkeypatch):
    monkeypatch.setattr(exporter._runs, "tempo_url", tempo.url)

    doc = exporter.get_ci_observe_runs_json({"days": ["7"]})

    assert {run["id"] for run in doc["runs"]} == {str(RUN_ID), str(SECOND_RUN_ID)}
    assert {run["capture_state"] for run in doc["runs"]} == {"capture", "not collected"}


class TestHTTP:
    @pytest.fixture
    def base_url(self, wfe, exporter, monkeypatch):
        monkeypatch.setattr(wfe.ExporterHandler, "exporter", exporter)
        server = ThreadingHTTPServer(("127.0.0.1", 0), wfe.ExporterHandler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_address[1]}"
        server.shutdown()
        server.server_close()

    @staticmethod
    def get(url):
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.headers, response.read()

    def test_json_shape_and_cors(self, exporter, jobs, tempo, base_url, monkeypatch):
        monkeypatch.setattr(exporter._runs, "tempo_url", tempo.url)

        status, headers, body = self.get(f"{base_url}/api/ci-observe/runs?days=7&limit=10")

        assert status == 200
        assert headers.get("Content-Type") == "application/json"
        assert headers.get("Access-Control-Allow-Origin") == "*"
        doc = json.loads(body)
        assert doc["days"] == 7
        assert set(doc) == {"days", "window_from", "window_to", "tempo", "runs", "notes"}
        assert {run["id"]: run["capture_state"] for run in doc["runs"]} == {
            str(RUN_ID): "capture",
            str(SECOND_RUN_ID): "not collected",
        }

    def test_tempo_down_is_still_200(self, exporter, jobs, base_url):
        exporter._runs.tempo_url = "http://127.0.0.1:1"

        status, _, body = self.get(f"{base_url}/api/ci-observe/runs")

        assert status == 200
        assert {run["capture_state"] for run in json.loads(body)["runs"]} == {"unknown"}


# ---------------------------------------------------------------------------
# Deployment: the container must reach the host's Tempo
# ---------------------------------------------------------------------------
def test_the_runs_module_is_mounted_and_copied():
    quadlet = (MONITORING / "quadlet" / "workflow-exporter.container").read_text()
    setup = (MONITORING / "scripts" / "monitoring-setup.sh").read_text()

    assert "ci_observe_runs.py:/app/ci_observe_runs.py" in quadlet
    assert "scripts/ci_observe_runs.py" in setup


def test_the_quadlet_reaches_the_host_tempo_without_wide_exposure():
    """Tempo publishes its API on the host's loopback only, so the container
    needs host networking; the API's own listen address is then pinned back to
    loopback, because a host-network container would otherwise expose it on
    every interface."""
    quadlet = (MONITORING / "quadlet" / "workflow-exporter.container").read_text()

    assert "Network=host" in quadlet
    assert "Environment=BIND_ADDRESS=127.0.0.1" in quadlet
    assert "Environment=CI_OBSERVE_TEMPO_URL=http://127.0.0.1:3210" in quadlet
    assert "\nPublishPort=" not in quadlet, "host networking rejects published ports"


def test_the_tempo_url_default_is_a_documented_constant(runs):
    """The code must not assume host networking: the address is one env var
    with a default that matches the deployed layout."""
    assert runs.DEFAULT_TEMPO_URL == "http://127.0.0.1:3210"
    assert runs.TEMPO_URL_ENV == "CI_OBSERVE_TEMPO_URL"


# ---------------------------------------------------------------------------
# Contract: the dashboard reads what the endpoint returns
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not DASHBOARD.exists(), reason="the entry page ships with the dashboard")
def test_the_dashboard_selectors_resolve_against_the_endpoint(exporter, jobs, tempo, monkeypatch):
    monkeypatch.setattr(exporter._runs, "tempo_url", tempo.url)
    dashboard = json.loads(DASHBOARD.read_text())
    panel = next(p for p in dashboard["panels"] if p["id"] == 12)
    target = panel["targets"][0]

    doc = exporter.get_ci_observe_runs_json({"days": ["7"]})
    node = doc[target["root_selector"].lstrip("$.")]

    assert node, "the contract document has no rows"
    for column in target["columns"]:
        assert column["selector"] in node[0], f"column {column['selector']} is not in the response"
